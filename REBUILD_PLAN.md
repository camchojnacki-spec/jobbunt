# Jobbunt Rebuild: Comprehensive Implementation Plan

**Created:** 2026-03-20
**Status:** Planning Complete — Ready for Execution

---

## Execution Order

```
Phase 1 (Foundation) ──┬──> Phase 2 (Models) ──> Phase 5 (Core Features)
                       │                          │
                       └──> Phase 3 (UX) ────────> Phase 6 (Intelligence)
                            │
                            └──> Phase 4 (CSS)

Phase 7 (Infrastructure) -- parallel with Phases 3-6
Phase 8 (Advanced) -- last, depends on everything
```

---

## PHASE 1: Foundation & Backend Architecture (3-4 weeks)

**Dependencies:** None (foundational)

### 1.1 Create `backend/utils.py` [S]
- Extract `_safe_json()` from 5 files: api.py, scraper.py, scorer.py, agent.py, automator.py
- Extract `_safe_json_list()` from api.py
- Unify behavior (logging, error handling)

### 1.2 Create `backend/constants.py` [S]
- Move `SENIORITY_TIERS`, `TIER_TITLE_KEYWORDS`, `TITLE_SYNONYMS` from scorer.py
- Move `SENIORITY_TIERS` from scraper.py
- Add status constants: `"pending"`, `"liked"`, `"passed"`, `"applied"` etc.

### 1.3 Create `backend/serializers.py` [M]
- Move `_profile_dict`, `_job_dict`, `_application_dict`, `_question_dict`, `_build_ai_synthesis` from api.py
- Modify `_job_dict` to accept pre-loaded company (N+1 fix prep)

### 1.4 Split `api.py` (4,225 lines) into route modules [L]
- `routes/profiles.py` (~800 lines) — profile CRUD, resume, parse, analyze, reporter corner
- `routes/jobs.py` (~600 lines) — job listing, swipe, enrich, verify, search, import
- `routes/applications.py` (~300 lines) — application CRUD, pipeline status, questions
- `routes/intelligence.py` (~1200 lines) — career-stats, scouting-report, insights, readiness, skills-audit, resume improvement, search-advisor
- `routes/config.py` (~300 lines) — prompt lab, model config, source config
- `routes/dispatch.py` (~200 lines) — dispatch flow
- `routes/_helpers.py` — shared helpers (_safe_enrich, _safe_score, _get_profile_for_user)
- `api.py` becomes thin aggregator mounting sub-routers

### 1.5 Fix Gemini async blocking [S]
- `ai.py` line 164: `client.models.generate_content()` is synchronous in async function
- Fix: `await client.aio.models.generate_content(...)` or `asyncio.to_thread()`

### 1.6 Pool AI clients (singleton) [S]
- Lines 126, 145: new client created every call
- Fix: module-level lazy singletons with `_get_anthropic_client()`, `_get_gemini_client()`

### 1.7 Fix N+1 queries on job listings [M]
- `_job_dict` queries Company individually for every job
- Fix: `joinedload(Job.company_rel)` in list endpoints, pass pre-loaded company to serializer

### 1.8 Add pagination to list endpoints [M]
- `GET /profiles/{id}/jobs` returns ALL jobs, no limit/offset
- Add `?page=1&per_page=50` with total count in response
- Keep backward compat initially with large default per_page

### 1.9 Add Pydantic request/response models [M]
- Create `backend/schemas.py`
- Models: ProfileCreate, ProfileUpdate, SearchRequest, SwipeAction, JobImport, ApplicationUpdate
- Response: JobResponse, ProfileResponse, PaginatedResponse[T]

### 1.10 Restrict CORS [S]
- `app.py` line 50: `allow_origins=["*"]`
- Fix: restrict to actual frontend origins + localhost

### 1.11 Wire prompt_registry.py [M]
- 908 lines of dead code — prompt templates exist but never called
- Add `get_prompt(key, **variables)` function
- Replace inline prompts in scraper.py, scorer.py, enrichment.py

### 1.12 Standardize error handling [S]
- Create `backend/exceptions.py`: JobbuntError, NotFoundError, ProfileAccessDenied
- Register FastAPI exception handlers in app.py

### 1.13 Replace manual migrations with Alembic [M]
- Remove `_run_migrations()` from database.py
- Set up alembic.ini, env.py, initial migration
- Use `render_as_batch=True` for SQLite compat

### 1.14 Add health check endpoint [S]
- `GET /health` — check DB connectivity, AI provider, return version

**Sprint order within Phase 1:**
1. 1.1 + 1.2 (quick wins, zero risk)
2. 1.5 + 1.6 (immediate perf win)
3. 1.3 + 1.7 (serializer enables N+1 fix)
4. 1.4 (largest task, after helpers extracted)
5. 1.8–1.14 (any order after 1.4)

---

## PHASE 2: Data Model Expansion (1 week)

**Dependencies:** Phase 1.13 (Alembic)

### 2.1 Interview model [M]
```
interviews: id, application_id, profile_id, round_number, interview_type,
  scheduled_at, duration_minutes, interviewer_names (JSON), prep_notes,
  questions_asked (JSON), outcome, feedback, created_at, updated_at
```

### 2.2 SavedSearch model [S]
```
saved_searches: id, profile_id, name, query_config (JSON), alert_frequency,
  last_run_at, results_count, is_active, created_at
```

### 2.3 FollowUp model [S]
```
follow_ups: id, application_id, profile_id, type, due_date, completed,
  completed_at, draft_content, notes, created_at
```

### 2.4 Document model [S]
```
documents: id, profile_id, job_id (nullable), doc_type, version, title,
  content, file_path, is_active, created_at
```

### 2.5 Contact model [S]
```
contacts: id, profile_id, company_id, name, title, email, linkedin_url,
  phone, relationship_type, notes, last_contacted, created_at
```

### 2.6 Add soft-delete and timestamps [S]
- Add `archived_at` to Job, Profile
- Add `updated_at` to Company, AgentQuestion, ProfileQuestion

### 2.7 Add explicit indexes [S]
- `ix_jobs_profile_status_score` (profile_id, status, match_score)
- `ix_jobs_company_id`, `ix_jobs_created_at`
- `ix_applications_profile_id`, `ix_applications_pipeline_status`
- `ix_companies_enriched`

### 2.8 Generate Alembic migration [S]

---

## PHASE 3: UX Restructure (3-4 weeks)

**Dependencies:** Phase 1 (serializers, pagination)

### 3.1 Restructure tabs: 5 → 4 [L]
| Old | New | Content |
|-----|-----|---------|
| Dugout | Home | Coach's Note, Spring Training, Season Stats, Activity Feed |
| Hunt + Pipeline | Jobs | Browse / Shortlist / Applied as toggles |
| Intel | Tools | AI tools as runnable cards (3 groups instead of 5 sub-tabs) |
| Profile | Profile | + Reporter Corner, + Baseball Card, Settings |

### 3.2 Lower feature gating [S]
- Search: unlock at level 1 (resume uploaded), not level 5
- AI tools: unlock at level 1

### 3.3 Fix keyboard shortcuts bug [S]
- `setupKeyboard()` line 2103: `view-swipe` → `view-hunt` (or `view-jobs` post-restructure)

### 3.4 Break up buildJobCard() (420 lines) [M]
- Decompose into: buildCardHeader, buildCardScore, buildCardMeta, buildCardReasons, buildCardCompanyInfo, buildCardDeepResearch, buildCardActions

### 3.5 Consolidate duplicate features [M]
- Skills audit × 2 → 1 (keep in Tools)
- Resume improvement × 2 → 1 (keep in Tools)
- Readiness display × 2 → 1

### 3.6 Remove dead code [S]
- `loadScoutingReport` alias (just calls loadSpringTraining)
- `loadIntelData()` empty function
- Triple `applyArraySuggestion` assignment

### 3.7 Mobile filter drawer [M]
- Replace inline filter bar with slide-up drawer on mobile
- Keep inline on desktop

### 3.8 Add ARIA attributes [S]
- `role="tablist/tab/tabpanel"`, `aria-selected` on tabs
- `role="alert"`, `aria-live="polite"` on toasts
- `aria-label` on score indicators

---

## PHASE 4: Frontend Code Quality (2 weeks)

**Dependencies:** Phase 3

### 4.1 Split style.css (6,534 lines) [L]
- tokens.css, layout.css, nav.css
- views/home.css, views/jobs.css, views/tools.css, views/profile.css
- components/buttons.css, components/modals.css, components/toast.css, components/forms.css
- responsive.css

### 4.2 Migrate CSS tokens to --jb-* namespace [M]
- Replace all `--accent`, `--bg`, `--void`, `--steel` etc. with `--jb-*` equivalents
- Search app.js for old token refs in inline styles

### 4.3 Move inline styles from JS to CSS classes [L]
- Hundreds of template literals with `style=` in app.js
- Create corresponding CSS classes

### 4.4 Remove duplicate CSS [S]
- `.page-btn` defined twice
- CSS linting for other duplicates

### 4.5 Add skip-to-content link [S]
### 4.6 Fix mobile touch targets [S]
- All interactive elements ≥ 44x44px

---

## PHASE 5: New Features — Core (3-4 weeks)

**Dependencies:** Phases 2, 3

### 5.1 Resume tailoring per job [L] ⭐ #1 Feature Gap
- `POST /jobs/{id}/tailor-resume`
- AI rewrites resume emphasizing matching skills per job description
- Save as Document model, option to download as DOCX
- "Tailor Resume" button on job detail cards

### 5.2 Interview prep / "Warm-Up" [L] ⭐ Unique Differentiator
- `POST /jobs/{id}/interview-prep`
- Generate 10 likely questions (behavioral + technical)
- STAR-format answer frameworks from career_history
- Company-specific talking points
- Dedicated prep panel in job detail

### 5.3 Follow-up reminders [M]
- `GET /profiles/{id}/follow-ups`
- Auto-detect: applied > 7 days ago, no status change
- Display on Home view: "3 applications need follow-up"

### 5.4 "Box Score" analytics dashboard [L]
- `GET /profiles/{id}/box-score`
- Application funnel visualization
- Batting average (offers / applications)
- Response rates, source effectiveness
- Score distribution histogram

### 5.5 Cover letter generation improvements [M]
- More prominent "Generate Cover Letter" on job cards
- Deep tier AI with career_history context
- Tone selection: formal, conversational, enthusiastic

---

## PHASE 6: New Features — Intelligence (2-3 weeks)

**Dependencies:** Phase 2 (SavedSearch), Phase 5

### 6.1 Job alerts / saved searches [L]
- CRUD for SavedSearch model
- Periodic background task runs searches
- Surface new high-score jobs on Home view
- Cloud Scheduler for periodic execution

### 6.2 "Seventh Inning Stretch" weekly digest [M]
- `GET /profiles/{id}/weekly-digest`
- AI summary: jobs found, applications sent, pipeline updates, suggestions

### 6.3 Job comparison view [M]
- Select 2-3 jobs from shortlist
- Side-by-side: score breakdown, salary, location, company ratings, pros/cons
- No new backend needed — all data exists

### 6.4 "Walk-Up Song" personal pitch [S]
- `POST /profiles/{id}/elevator-pitch`
- AI 30-second elevator pitch from profile summary + strengths

### 6.5 Follow-up email drafting [M]
- `POST /follow-ups/{id}/draft-email`
- Contextual follow-up using application details + time elapsed

---

## PHASE 7: Infrastructure & Performance (2-3 weeks)

**Dependencies:** Phase 1 (can run parallel with 3-6)

### 7.1 Parallelize rescore [M]
- `asyncio.gather()` with semaphore (max 5 concurrent)
- Careful with SQLAlchemy session lifecycle

### 7.2 Cache AI responses in DB [M]
- `AICache` model: cache_key (prompt hash), response, ttl_hours
- Wrap `ai_generate` with cache lookup
- Especially for company enrichment (same company queried multiple times)

### 7.3 Add Redis for state [L]
- Replace in-memory `_rate_limits`, `_tasks`, `_rescore_progress`, `_domain_last_request`
- GCP: Redis Memorystore or Upstash

### 7.4 WebSocket for real-time progress [L]
- Replace polling (`GET /tasks/{id}`) with WebSocket push
- Cloud Run supports WebSockets (60-min timeout)

### 7.5 Structured logging [M]
- Request IDs, user context in every log entry
- JSON format for Cloud Logging

### 7.6 Consolidate Indeed scrapers [M]
- Strategy pattern: HttpxIndeed, PlaywrightIndeed, DispatchIndeed
- Select based on environment (Cloud Run → httpx, local → Playwright)

---

## PHASE 8: New Features — Advanced (4-6 weeks, cherry-pick)

**Dependencies:** Phases 2, 5, 6

### 8.1 "Farm System" skills development tracker [L]
### 8.2 "Double Play" company comparison [M]
### 8.3 Networking/contact tracker [M]
### 8.4 Email response detection [L]
### 8.5 Chrome extension [XL]
### 8.6 Application auto-fill improvements [L]

---

## Total Estimated Effort

| Phase | Weeks | Priority |
|-------|-------|----------|
| 1. Foundation | 3-4 | Critical |
| 2. Data Models | 1 | High |
| 3. UX Restructure | 3-4 | High |
| 4. CSS Quality | 2 | Medium |
| 5. Core Features | 3-4 | High |
| 6. Intelligence | 2-3 | Medium |
| 7. Infrastructure | 2-3 | Medium |
| 8. Advanced | 4-6 | Low |
| **Total** | **~20-28 weeks** | |
