# Jobbunt — QA Audit Documentation Package

**Version**: 2.0.0
**Date**: March 20, 2026
**Production URL**: https://jobbunt-829547589641.us-east1.run.app
**Repository**: https://github.com/camchojnacki-spec/jobbunt

---

## Table of Contents

1. [Architecture & Tech Stack](#1-architecture--tech-stack-overview)
2. [Feature Map & User Flows](#2-feature-map--user-flows)
3. [User Flow Diagrams](#3-user-flow-diagrams)
4. [Known Issues & Technical Debt](#4-known-issues--technical-debt)
5. [AI Prompt Inventory](#5-ai-prompt-inventory)
6. [Data Model & State Management](#6-data-model--state-management)
7. [Test Accounts & Environment](#7-test-accounts--environment)
8. [Design System & Brand Guidelines](#8-design-system--brand-guidelines)
9. [Analytics & Success Metrics](#9-analytics--success-metrics)
10. [Competitive & Market Context](#10-competitive--market-context)

---

# 1. Architecture & Tech Stack Overview

## Frontend

| Aspect | Detail |
|--------|--------|
| Framework | Vanilla JavaScript (no framework) — single-page application |
| UI Library | Custom component system via CSS classes + template literals |
| State Management | Single `state` object in `app.js` (in-memory, per-session) |
| Routing | Custom `showView()` function toggling `display` on `<div>` elements |
| Form Handling | Native HTML forms + manual `fetch()` calls |
| Build System | None — no bundling, no transpilation, no minification |
| Main Files | `app.js` (~7,145 lines), `style.css` (~6,577 lines), `index.html` (~1,143 lines) |
| Cache Busting | Manual query string versioning (`?v=20260320a`) |

## Backend

| Aspect | Detail |
|--------|--------|
| Framework | FastAPI (Python 3.11) |
| API Style | REST — 78 endpoints across 6 route modules |
| Entry Point | `backend/app.py` → `uvicorn` ASGI server |
| Route Modules | `routes/profiles.py` (18), `routes/jobs.py` (22), `routes/applications.py` (11), `routes/intelligence.py` (14), `routes/api.py` (13 remaining), `routes/auth.py` (8) |
| Validation | Pydantic v2 models (`backend/schemas.py`) |
| Error Handling | Custom exception hierarchy (`backend/exceptions.py`) → `JobbuntError` base |
| Background Tasks | `asyncio.create_task()` with in-memory task tracking (`backend/tasks.py`) |

## Database

| Aspect | Detail |
|--------|--------|
| Production | PostgreSQL via Google Cloud SQL |
| Development | SQLite (`data/jobbunt.db`) |
| ORM | SQLAlchemy 2.0 with declarative base |
| Tables | 13 tables: `users`, `profiles`, `companies`, `jobs`, `applications`, `agent_questions`, `profile_questions`, `interviews`, `saved_searches`, `follow_ups`, `documents`, `contacts`, `ai_cache` |
| Migrations | Custom idempotent migration system (`ALTER TABLE ADD COLUMN` + `CREATE TABLE IF NOT EXISTS`) |
| Indexes | Composite indexes on `jobs(profile_id, status, match_score)`, `jobs(profile_id, fingerprint)`, `jobs(created_at)`, plus individual indexes on all foreign keys |

## Authentication

| Aspect | Detail |
|--------|--------|
| Method | Signed cookie-based sessions using `itsdangerous.URLSafeTimedSerializer` |
| Providers | Google OAuth 2.0 (Authorization Code flow) + Local email/password |
| Password Hashing | PBKDF2-SHA256, 100,000 iterations, random 16-byte hex salt |
| Session Cookie | `jobbunt_session`, HttpOnly, SameSite=Lax, Secure when HTTPS |
| Session Duration | 7 days (604,800 seconds) |
| Token Refresh | No refresh tokens — session is re-validated on each request by decoding the signed cookie |
| Dev Bypass | `DEV_SKIP_AUTH=1` skips all authentication (single-user dev mode) |

## Hosting & Deployment

| Aspect | Detail |
|--------|--------|
| Platform | Google Cloud Run (us-east1) |
| Container | Python 3.11-slim Docker image |
| CI/CD | GitHub Actions → push to `main` triggers `deploy-cloudrun@v2` |
| Build | Source-based Cloud Build (Dockerfile) |
| Database | Cloud SQL (PostgreSQL) via Cloud SQL Auth Proxy sidecar |
| File Storage | Google Cloud Storage (resume uploads in production) |
| Local Dev | `uvicorn backend.app:app --reload --host 0.0.0.0 --port 8000` |
| Resources | 2 vCPU, 1 GB RAM, 600s request timeout |

## AI/LLM Integration

| Aspect | Detail |
|--------|--------|
| Primary Provider | Google Gemini (via `google-genai` SDK v1.14.0) |
| Fallback Provider | Anthropic Claude (via `anthropic` SDK v0.42.0) |
| Provider Selection | Automatic based on API key presence (Anthropic preferred if key set) |
| Prompt Management | Inline in service files. Prompt Registry exists (909 lines, 18 prompts) but is NOT wired into services — serves as documentation/future override layer |
| AI Call Location | Server-side only — all `ai_generate()` calls in Python backend |
| Total Prompts | 28 distinct AI prompts across 9 backend files |
| Rate Limiting | Application-level: 30 AI calls/min/IP (configurable via `RATE_LIMIT_AI`). SerpAPI: 20 calls per search session |
| Cost Controls | 4-tier model system: `flash` (cheapest) → `fast` → `balanced` → `deep` (most expensive). Lighter tiers used for parsing/filtering, heavier tiers for analysis/strategy |
| Fallback Behavior | On AI failure: returns empty string. Most features have tier fallback (deep → balanced → flash). Job scoring falls back to rule-based multi-dimensional scoring |
| Caching | `ai_cache` table exists (hash-based, 7-day TTL) but not yet wired into `ai_generate()` |
| Fine-tuning | None |
| RAG | None |
| Vector DB | None |

### Model Tier Mapping

| Tier | Gemini Model | Anthropic Model | Use Cases |
|------|-------------|-----------------|-----------|
| `flash` | gemini-3.1-flash-lite-preview | claude-haiku-4-5 | Relevance filtering, email classification, follow-up drafts |
| `fast` | gemini-3.1-flash-lite-preview | claude-haiku-4-5 | Job scoring, enrichment, query expansion, dedup |
| `balanced` | gemini-2.5-flash | claude-sonnet-4-6 | Cover letters, profile analysis, Q&A synthesis, scouting reports |
| `deep` | gemini-2.5-pro | claude-sonnet-4-6 | Resume parsing, career advisor, market intelligence, interview prep, resume tailoring |

## Third-Party Integrations

| Service | Purpose | Env Var |
|---------|---------|---------|
| Google Gemini API | Primary AI provider | `GEMINI_API_KEY` |
| Anthropic Claude API | Fallback AI provider | `ANTHROPIC_API_KEY` |
| SerpAPI | Google Jobs aggregation (Indeed, LinkedIn, Glassdoor) | `SERPAPI_KEY` |
| Adzuna API | Job board API | `ADZUNA_APP_ID`, `ADZUNA_API_KEY` |
| Google OAuth 2.0 | User authentication | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` |
| Google Cloud Storage | Resume file storage (production) | `GCS_BUCKET` |
| Google Cloud SQL | Production database | `DATABASE_URL`, `CLOUD_SQL_CONNECTION` |
| Careerjet | Direct job scraping (no API key needed) | — |
| LinkedIn | Guest job API scraping (no API key) | — |
| JobBank Canada | Government job board scraping | — |
| Talent.com | Job board scraping | — |
| RemoteOK | Remote job API | — |

## Complete Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | SQLite fallback | PostgreSQL connection string |
| `GEMINI_API_KEY` | None | Google Gemini API |
| `GOOGLE_API_KEY` | None | Alternative Gemini key |
| `ANTHROPIC_API_KEY` | None | Anthropic Claude API |
| `AI_PROVIDER` | `gemini` | Reported in health check |
| `GOOGLE_CLIENT_ID` | `""` | Google OAuth |
| `GOOGLE_CLIENT_SECRET` | `""` | Google OAuth |
| `SESSION_SECRET` | fallback chain | Cookie signing |
| `OAUTH_REDIRECT_URI` | auto-detected | OAuth callback URL |
| `DEV_SKIP_AUTH` | `""` | Bypass auth in dev |
| `LOCAL_AUTH` | `""` | Enable email/password auth |
| `SERPAPI_KEY` | `""` | SerpAPI for job search |
| `ADZUNA_APP_ID` | `""` | Adzuna job API |
| `ADZUNA_API_KEY` | `""` | Adzuna job API |
| `GCS_BUCKET` | None | Cloud Storage bucket |
| `ALLOWED_ORIGINS` | `localhost` | CORS whitelist |
| `RATE_LIMIT_AI` | `30` | AI calls/min/IP |
| `RATE_LIMIT_SEARCH` | `10` | Searches/min/IP |
| `RATE_LIMIT_AUTH` | `20` | Auth attempts/min/IP |
| `CHROME_USER_DATA` | auto-detected | Browser automation |
| `ENV` | None | `production` on Cloud Run |
| `PORT` | `8080` | Server port |

---

# 2. Feature Map & User Flows

## Onboarding

| Step | Feature | Status | AI? |
|------|---------|--------|-----|
| Account creation | Google OAuth or email/password registration | ✅ Implemented | No |
| Profile creation | Name entry, automatic profile creation on registration | ✅ Implemented | No |
| Resume upload/paste | PDF/DOCX upload or raw text paste → AI parses into structured fields | ✅ Implemented | Yes — 2-stage deep AI extraction |
| Spring Training | 5-level progressive onboarding (Rookie → The Show) | ✅ Implemented | No |
| Reporter Corner | AI-generated interview questions to deepen profile | ✅ Implemented | Yes — balanced tier question generation |

**Required data**: Name, email (for auth). **Optional**: Everything else.
**First AI engagement**: Resume upload triggers 2-stage AI parsing (deep tier). First value moment.

### Spring Training Levels (Feature Gating)
| Level | Name | Unlock Criteria | Unlocks |
|-------|------|-----------------|---------|
| 0 | Rookie | Account created | Profile editing |
| 1 | Single-A | Resume uploaded | **Job search**, basic features |
| 2 | Double-A | AI profile analysis run | Reporter Corner |
| 3 | Triple-A | 3+ Reporter Corner answers | AI tools (skills audit, advisor) |
| 4 | The Show | All complete | All features |

## Profile & Resume

| Feature | Status | AI? | Detail |
|---------|--------|-----|--------|
| Resume upload (PDF/DOCX) | ✅ Implemented | Yes | Parsed via PyPDF2/python-docx → AI extraction |
| Resume paste (raw text) | ✅ Implemented | Yes | Direct AI parsing of pasted text |
| 2-stage AI parsing | ✅ Implemented | Deep tier | Stage 1: extraction with confidence scores. Stage 2: inference (target roles, summary, trajectory) |
| Profile field editing | ✅ Implemented | No | All fields editable via form |
| Skills management | ✅ Implemented | No | Add/remove individual skills, AI-suggested additions |
| AI profile analysis | ✅ Implemented | Deep tier | Extracts summary, trajectory, values, deal-breakers, strengths, growth areas, culture fit |
| Reporter Corner Q&A | ✅ Implemented | Balanced | AI generates tiered questions; answers auto-synthesize into profile every 3 answers |
| Baseball Card | ✅ Implemented | Balanced | Visual career stat card with work history extracted from resume |
| Resume improvement | ✅ Implemented | Balanced | AI-scored resume with section-by-section suggestions and ATS tips |
| Resume tailoring | ✅ Implemented | Deep tier | Per-job tailored resume rewrite saved as Document |
| Profile suggestions | ✅ Implemented | Deep tier | Career advisor generates field-level suggestions (current → suggested) |

## Culture/Fit Matching

| Feature | Status | AI? | Detail |
|---------|--------|-----|--------|
| 6-dimension scoring | ✅ Implemented | Fast tier | Role fit, skills, location, compensation, seniority, culture fit |
| AI scoring | ✅ Implemented | Fast tier | AI scores each dimension with reasons and concerns |
| Rule-based fallback | ✅ Implemented | No | Multi-dimensional scoring when AI unavailable |
| Company enrichment | ✅ Implemented | Fast tier | Glassdoor/Indeed ratings, AI-generated culture summary, 6-dimension company scorecard |
| Deep research | ✅ Implemented | Deep tier | Phase 2: culture insights, interview process, growth opportunities, day-in-life |
| Score transparency | ✅ Implemented | — | Score breakdown visible on job cards with per-dimension bars and text reasons |

**Scoring inputs**: Target roles, skills (matched count), location proximity, salary range overlap, seniority tier match, profile values vs company culture, deal-breakers, ideal culture preferences.

**Scoring weights**: Role fit (30%), Skills (25%), Location (15%), Compensation (10%), Seniority (10%), Culture (10%). Deep-researched jobs: adds Research Fit dimension with rebalanced weights.

## Skill Gap Analysis

| Feature | Status | AI? | Detail |
|---------|--------|-----|--------|
| Skills audit | ✅ Implemented | Balanced | Analyzes job postings to find missing high-demand, moderate, and low-value skills |
| Skill frequency analysis | ✅ Implemented | No | Rule-based counting of skill mentions across all jobs |
| Recommendation chips | ✅ Implemented | — | Clickable "Add" chips for recommended skills |
| Remove recommendations | ✅ Implemented | — | Identifies profile skills rarely requested in job postings |

**Benchmark**: Against actual job postings found for the user's target roles and locations.
**Recommendation engine**: AI identifies skill categories (technical, frameworks, leadership, domain) and recommends additions/removals. No course or certification linking yet.

## Job Search

| Feature | Status | AI? | Detail |
|---------|--------|-----|--------|
| Multi-source search | ✅ Implemented | — | SerpAPI (Google Jobs), LinkedIn, Careerjet, JobBank, Talent.com, Adzuna, RemoteOK |
| AI query expansion | ✅ Implemented | Fast tier | Generates alternative job titles + negative keywords from profile |
| AI relevance filtering | ✅ Implemented | Flash tier | Post-search filter removes wrong-domain jobs in batches of 20 |
| AI deduplication | ✅ Implemented | Fast tier | Cross-source duplicate detection and merging |
| 3 browse modes | ✅ Implemented | — | Swipe cards, grid view, list view |
| Filtering | ✅ Implemented | — | Score, remote type, salary, keyword, date posted, source |
| Sorting | ✅ Implemented | — | Score, date, salary, company, source |
| Source multi-tagging | ✅ Implemented | — | Jobs found on multiple sources tracked via `sources_seen` |
| Job verification | ✅ Implemented | — | URL validity checking |
| Shortlisting | ✅ Implemented | — | Swipe right or click to save |
| Application tracking | ✅ Implemented | — | Pipeline: Applied → Screening → Interview → Offer → Accepted/Rejected |
| Cover letter generation | ✅ Implemented | Balanced | Auto-generated on application with profile-specific content |
| Interview prep | ✅ Implemented | Deep tier | STAR framework answers, technical questions, questions to ask |
| Follow-up reminders | ✅ Implemented | Flash tier | Auto-detects stale applications, generates draft follow-up emails |
| Dispatch Scout | ✅ Implemented | — | Browser automation for Indeed scraping via Playwright |

## Baseball Metaphor Glossary

| Baseball Term | Career Equivalent | Where Used |
|---------------|-------------------|------------|
| **Dugout** | Dashboard / Home | Home view, nav alias |
| **Scouting** | Job searching across boards | Nav alias for Jobs |
| **Spring Training / The Climb** | Profile completeness progression | Dashboard, 5 levels |
| **Rookie Ball** | Level 1: Resume uploaded | Spring Training |
| **Single-A** | Level 2: Basic info complete | Spring Training |
| **Double-A** | Level 3: AI analysis + salary/seniority | Spring Training |
| **Triple-A** | Level 4: Preferences set | Spring Training |
| **The Majors / The Show** | Level 5: Fully ready | Spring Training |
| **Reporter Corner** | Guided onboarding Q&A (pre-game interview) | Dashboard, slide-up panel |
| **The Clubhouse** | Post-completion state | Reporter Corner final state |
| **Coach's Note** | AI contextual guidance | Dashboard section |
| **Player Card / Baseball Card** | User profile summary card | Dashboard visual |
| **Prospects** | Shortlisted jobs | Jobs sub-tab |
| **Dispatch Scout** | Indeed browser automation agent | Jobs/Settings |
| **Box Score** | Analytics dashboard | Dashboard section |
| **Season Stats** | Summary metrics row | Dashboard section |
| **At Bats (AB)** | Total jobs reviewed | Box Score / Season Stats |
| **Hits (H)** | Jobs liked/applied to | Box Score / Season Stats |
| **Batting Average (AVG)** | Like/apply rate | Box Score |
| **Base on Balls (BB)** | Jobs shortlisted | Box Score |
| **Strikeouts (K)** | Jobs passed on | Box Score |
| **On-Base Percentage (OBP)** | Engagement rate | Box Score |
| **Slugging (SLG)** | Quality engagement metric | Box Score |
| **OPS** | Combined effectiveness | Box Score |
| **On Deck** | Jobs waiting for review | Season Stats |
| **On Base** | Active interviews/applications | Season Stats |
| **Pregame Report** | Analysis overview hub | Tools sub-tab |
| **Scoreboard** | Search intelligence / market stats | Tools sub-tab |
| **Coaching Staff** | AI strategy advisor | Tools sub-tab |
| **Batting Practice** | Skills gap analysis | Tools sub-tab |
| **Equipment Check** | Resume optimization/analysis | Tools sub-tab |
| **Batting Cage** | Resume improvement tool | Profile section |
| **Bullpen** | Tools view | Nav alias |
| **Warm-Up** | Interview prep (STAR framework) | Per-job action |
| **Home Field** | User's location | Profile field |
| **Game Day Gear** | Application materials (resume, cover letter) | Profile section |
| **Scouting Networks** | Job board sources | Profile section |
| **Free Agent** | User with no target roles set | Coach Note context |
| **Pipeline** | Application tracking stages | Jobs Applied sub-tab |
| **What Position Are You Playing?** | Target role/job preferences | Profile section |
| **The Basics** | Core contact info | Profile section |
| **Dry Run** | Test mode for applications | Settings toggle |
| **Prompt Lab** | AI prompt editor | Settings admin tool |
| **Danger Zone** | Destructive admin actions | Settings section |
| Walk-Up Song | Elevator Pitch | *Planned (not implemented)* |
| Farm System | Skills Development | *Planned (not implemented)* |
| Double Play | Company Comparison | *Planned (not implemented)* |
| Seventh Inning Stretch | Weekly Digest | *Planned (not implemented)* |

## Engagement/Retention Mechanisms

| Mechanism | Status | Detail |
|-----------|--------|--------|
| Spring Training progression | ✅ Implemented | Gamified onboarding with unlock rewards |
| Coach's Note | ✅ Implemented | Dynamic contextual tip on dashboard based on profile state |
| Follow-up reminders | ✅ Implemented | "Follow-Ups Due" section on Home for stale applications |
| Box Score analytics | ✅ Implemented | Funnel metrics showing search activity |
| Season Stats | ✅ Implemented | Quick stats (jobs found, shortlisted, applied, interviews, score avg) |
| Score-based sorting | ✅ Implemented | Best matches surfaced first |
| Profile suggestions | ✅ Implemented | Career advisor generates actionable profile improvements |
| Deep research | ✅ Implemented | Phase 2 unlocks more data on shortlisted jobs |
| Activity feed | ✅ Implemented | Recent actions shown on dashboard |
| Notifications | ❌ Not implemented | No email/push notifications |
| Streaks | ❌ Not implemented | No daily/weekly streak tracking |

**Day 1 experience**: Upload resume → see AI-parsed profile → search jobs → browse/swipe results with match scores.
**Week 3 experience**: Follow-up reminders on stale applications, Box Score showing funnel progress, deep research on top picks, tailored resumes for applications, interview prep for upcoming interviews.

## Freemium Boundary

**Current status**: No paywall implemented. All features are free. Freemium tiers are planned but not built.

| Planned Tier | Features |
|--------------|----------|
| Free | Basic profile, limited searches (3/day), top 50 jobs, basic scoring |
| Pro ($15/mo) | Unlimited searches, resume tailoring, interview prep, cover letters, deep research |
| Premium ($30/mo) | Career advisor, market intelligence, application automation, priority support |

**Conversion trigger**: Not yet defined. Likely after user experiences value from free tier and hits a limit.

---

# 3. User Flow Diagrams

## Happy Path User Journey

```
┌─────────────┐    ┌──────────────┐    ┌───────────────────┐
│   Sign Up    │───>│ Create       │───>│ Upload Resume     │
│ Google/Email │    │ Profile      │    │ (PDF/DOCX/Paste)  │
└─────────────┘    └──────────────┘    └────────┬──────────┘
                                                │
                                    ┌───────────▼───────────┐
                                    │ AI Parses Resume      │
                                    │ (2-stage deep tier)    │
                                    │ → Extracts skills,     │
                                    │   roles, experience    │
                                    └───────────┬───────────┘
                                                │
                              ┌─────────────────▼─────────────────┐
                              │ 🎯 AHA MOMENT: User sees their   │
                              │ profile auto-populated with       │
                              │ skills, target roles, summary     │
                              │ (~2 minutes from signup)          │
                              └─────────────────┬─────────────────┘
                                                │
                                    ┌───────────▼───────────┐
                                    │ Search for Jobs       │
                                    │ (AI-expanded queries) │
                                    └───────────┬───────────┘
                                                │
                               ┌────────────────▼────────────────┐
                               │ Browse Results (3 modes)        │
                               │ • Card swipe (Tinder-style)     │
                               │ • Grid view                     │
                               │ • List view                     │
                               │ Each with match score + reasons │
                               └──────────┬─────────┬────────────┘
                                          │         │
                              ┌───────────▼──┐  ┌───▼───────────┐
                              │ Pass (left)  │  │ Like (right)  │
                              │ → Next job   │  │ → Shortlist   │
                              └──────────────┘  └───────┬───────┘
                                                        │
                                          ┌─────────────▼──────────────┐
                                          │ Shortlisted Job Actions    │
                                          │ • Deep Research            │
                                          │ • Tailor Resume            │
                                          │ • Interview Prep (Warm-Up) │
                                          │ • Apply                    │
                                          └─────────────┬──────────────┘
                                                        │
                                              ┌─────────▼─────────┐
                                              │ Application       │
                                              │ Pipeline Tracking  │
                                              │ Applied → Screen  │
                                              │ → Interview →     │
                                              │ Offer → Accept    │
                                              └─────────┬─────────┘
                                                        │
                                              ┌─────────▼─────────┐
                                              │ Follow-Up         │
                                              │ Reminders         │
                                              │ (7+ days stale)   │
                                              └───────────────────┘
```

## Error/Edge Case Flows

```
AI Call Failure:
  ai_generate() → returns "" → feature-specific fallback:
    Job scoring → rule-based multi-dimensional scoring
    Resume parsing → regex-based extraction
    Cover letter → template fallback
    Other features → error message to user

Incomplete Profile:
  Missing resume → Spring Training blocks at Rookie level
  Missing target roles → search uses profile summary for query generation
  Missing skills → scoring uses neutral defaults (50/100)
  Missing salary → compensation score defaults to 50/100

Session Expiry:
  Cookie expires after 7 days → redirected to /login
  API calls return 401 → frontend shows login prompt

Duplicate Accounts:
  Google OAuth: email-based dedup (same email = same user)
  Local auth: unique email constraint prevents duplicates
  Profile claiming: unclaimed profiles can be claimed by authenticated user

Search Failures (Cloud Run):
  Google → 429 rate limit → falls back to Bing → DuckDuckGo
  Indeed → 403 blocked → SerpAPI covers via Google Jobs aggregation
  All sources fail → returns 0 results with source health status
```

## Time to "Aha Moment"

| Step | Action | Time |
|------|--------|------|
| 1 | Click "Sign Up" | 0:00 |
| 2 | Google OAuth or form | 0:15 |
| 3 | Upload resume | 0:30 |
| 4 | AI parses → profile populated | **1:30 — AHA MOMENT** |
| 5 | Click "Search for Jobs" | 1:45 |
| 6 | See scored results | **2:30 — Second AHA** |

**~2 minutes to first value**, 4-5 clicks.

---

# 4. Known Issues & Technical Debt

## Critical Bugs

| # | Issue | Detail |
|---|-------|--------|
| 1 | Cloud Run scraping failures | Google (429), Indeed (403), Talent.com (0 results) from Cloud Run IPs. Only SerpAPI, LinkedIn, Careerjet, JobBank work reliably. Desktop works fine. |
| 2 | SerpAPI quota exhaustion | Indeed/Glassdoor source handlers double-dip SerpAPI calls. 10 role-location combos × 2-3 SerpAPI calls each = quota hit before search completes |
| 3 | SerpAPI pagination broken | Google discontinued `start` parameter, requires `next_page_token`. Returns 400 on page 2+ |
| 4 | Deploy kills in-progress searches | Cloud Run revision deployment terminates running instance. In-memory task state lost. Frontend polls stale task ID (404) |

## Major Bugs

| # | Issue | Detail |
|---|-------|--------|
| 5 | Location data inaccuracy | Careerjet sometimes returns user's location instead of job's location (Holland Bloorview: Toronto job showed as Milton) |
| 6 | Prompt Registry not wired | 909 lines of prompt management code unused — all prompts inline |
| 7 | AI cache not wired | `ai_cache` table exists but `ai_generate()` doesn't check/write to it |
| 8 | No pagination on job list API | `GET /profiles/{id}/jobs` returns ALL jobs — unbounded response |

## Minor Bugs

| # | Issue | Detail |
|---|-------|--------|
| 9 | CSS token dual system | Old tokens (`--accent`, `--void`) and new `--jb-*` tokens coexist |
| 10 | `.page-btn` CSS defined twice | Lines ~4116 and ~4547 in style.css |
| 11 | Inline styles in JS | Coach's Note, Spring Training, pipeline funnel built with inline styles instead of CSS classes |
| 12 | Triple `window.applyArraySuggestion` assignment | Fixed in recent commit but verify |

## UX Gaps

| # | Issue | Detail |
|---|-------|--------|
| 13 | No saved searches | Users can't save/name search configurations |
| 14 | No job comparison view | Can't side-by-side compare shortlisted jobs |
| 15 | No onboarding tour | No tooltip walkthrough for first-time users |
| 16 | Filter bar overflow on mobile | 6+ filter controls wrap and consume excessive viewport |
| 17 | Baseball metaphor learning curve | Navigation labels require decoding (somewhat mitigated by recent rename to Home/Jobs/Tools/Profile) |

## Performance Concerns

| # | Issue | Detail |
|---|-------|--------|
| 18 | N+1 company queries | `_job_dict()` queries Company table individually per job. 50 jobs = 50 queries |
| 19 | Sequential AI rescore | Rescoring 100 jobs = 100 sequential AI calls (should use `asyncio.gather` with semaphore) |
| 20 | No AI response caching | Career stats, scouting reports, market intelligence make full AI calls on each request |
| 21 | Single monolithic JS file | 7,145 lines loaded upfront, no code splitting or lazy loading |
| 22 | No image optimization | No lazy loading of images, no srcset/responsive images |
| 23 | All in-memory state lost on restart | Task progress, rate limit counters, source health — all reset on deploy |

## Accessibility Gaps

| # | Issue | Detail |
|---|-------|--------|
| 24 | No ARIA labels on tab bar | Tab buttons lack `role="tab"`, `aria-selected`, `role="tablist"` |
| 25 | Color-only score indicators | Score rings use color alone (green/amber/red) — no text/icon alternative for colorblind users |
| 26 | No skip-to-content link | No skip navigation for keyboard users |
| 27 | Toast notifications lack `role="alert"` | `#toast-container` missing `aria-live="polite"` |
| 28 | Skip button below touch target minimum | 38×38px on mobile (WCAG minimum: 44×44px) |
| 29 | `.btn-gated` CSS blocks pointer events | `pointer-events: none` prevents JS gated-click handler from showing toast explaining why |

## Mobile Responsiveness

**Status**: Partially responsive. Breakpoints at 640px, 680px, 1100px, 1500px. Bottom tab bar is fixed and touch-friendly. Grid layouts collapse on mobile. Filter bar overflows on small screens.

## Browser Compatibility

**Tested**: Chrome (primary). **Not tested**: Firefox, Safari, Edge. Uses modern JS (optional chaining, template literals, async/await) — IE11 incompatible.

---

# 5. AI Prompt Inventory

## Summary

| # | Feature | Trigger | Tier | Max Tokens | Approx Cost/Call |
|---|---------|---------|------|------------|-----------------|
| 1 | Resume Parse (Stage 1) | Resume upload/paste | deep | 4000 | ~$0.05-0.10 |
| 2 | Resume Parse (Stage 2) | Auto after Stage 1 | deep | 2000 | ~$0.03-0.05 |
| 3 | Q&A Synthesis | Every 3rd profile answer | balanced | 2000 | ~$0.01-0.02 |
| 4 | Question Generator | Spring Training / profile tier unlock | balanced | 1500 | ~$0.01-0.02 |
| 5 | AI Draft Answer | User clicks "Draft" on a question | balanced | 500 | ~$0.005 |
| 6 | Career Stats (Baseball Card) | User clicks "Career Stats" | balanced | 1000 | ~$0.01 |
| 7 | Scouting Report | User clicks "Scouting Report" | balanced | 400 | ~$0.005 |
| 8 | Career Search Advisor | User opens "Coaching Staff" tab | deep | 4000 | ~$0.10-0.20 |
| 9 | Market Intelligence | User opens "Scoreboard" tab | deep | 1800 | ~$0.05-0.10 |
| 10 | Skills Audit | User clicks "Skills Audit" | balanced | 1200 | ~$0.01-0.02 |
| 11 | Resume Improvement | User clicks "Equipment Check" | balanced | 3000 | ~$0.02-0.04 |
| 12 | Resume Tailoring | User clicks "Tailor Resume" on job card | deep | 3000 | ~$0.05-0.10 |
| 13 | Interview Prep | User clicks "Warm-Up" on job card | deep | 4000 | ~$0.10-0.20 |
| 14 | Follow-Up Draft (Short) | Auto when stale application detected | flash | 200 | ~$0.001 |
| 15 | Follow-Up Email (Full) | User clicks "Draft Email" on follow-up | flash | 500 | ~$0.002 |
| 16 | Job Scoring | Auto after search/rescore | fast | 700 | ~$0.002 |
| 17 | Company Enrichment | Auto after search (first encounter) | fast | 800 | ~$0.003 |
| 18 | Job Enrichment | Auto after search | fast | 800 | ~$0.003 |
| 19 | Deep Research | User clicks "Deep Research" on job | deep→balanced | 2000 | ~$0.05-0.10 |
| 20 | Profile Analysis | User triggers "Pregame" analysis | deep | 1000 | ~$0.03-0.05 |
| 21 | Cover Letter | User applies to job | balanced | 1500 | ~$0.01-0.02 |
| 22 | Application Analysis | User applies to job | balanced | 1000 | ~$0.01 |
| 23 | Search Query Expansion | Auto before search | fast | 600 | ~$0.002 |
| 24 | Relevance Filter | Auto after search (per batch of 20) | flash | 400 | ~$0.001 |
| 25 | Job Deduplication | User triggers dedup | fast | 200 | ~$0.001 |
| 26 | Automation Plan | User views automation steps | fast | 800 | ~$0.003 |
| 27 | Email Classification | Email monitoring (when active) | flash | 300 | ~$0.001 |
| 28 | Prompt Enhancement | Admin uses Prompt Lab | balanced | 2000 | ~$0.02 |

### Estimated Cost Per User Journey

| Journey | AI Calls | Est. Cost |
|---------|----------|-----------|
| Signup + resume parse | 2-3 | ~$0.10-0.15 |
| First job search (100 jobs) | ~120 (score + enrich) | ~$0.50-0.80 |
| Browse + shortlist 5 jobs | 5 (deep research) | ~$0.25-0.50 |
| Apply to 1 job | 2 (cover letter + analysis) | ~$0.02-0.04 |
| Full advisor + insights | 2 | ~$0.15-0.30 |
| Resume tailor + interview prep | 2 | ~$0.15-0.30 |
| **Total first-week cost** | **~135** | **~$1.20-2.10** |

### Error Handling by Feature

| Feature | On AI Failure |
|---------|---------------|
| Resume parsing | Falls back to regex extraction |
| Job scoring | Falls back to rule-based multi-dimensional scoring |
| Cover letter | Falls back to template with variable substitution |
| Career advisor | Retries with balanced tier, then returns `{"advisor": None}` |
| Market intelligence | Retries with balanced tier, returns `{"ai_insights": None}` |
| Resume improvement | Retries with flash tier, returns `{"error": "..."}` |
| Question generation | Retries with flash tier, returns `{"generated": 0}` |
| Job enrichment | Returns early, job remains un-enriched |
| All others | Returns empty string or raises HTTP 500 |

### User Feedback Loop

- **Regeneration**: No explicit "regenerate" button on any AI output. User must re-trigger the action.
- **Rating**: No thumbs up/down or quality rating on AI outputs.
- **Correction**: Users can edit their profile fields after AI parsing. AI-generated cover letters can be manually edited before use. Interview prep is read-only.

---

# 6. Data Model & State Management

## User Profile Schema

See Section 1 database tables for complete field listing. Key distinction:

**User-entered fields**: name, email, phone, location, target_roles, target_locations, salary range, remote_preference, skills, resume_text, cover_letter_template, career_history, additional_info, additional_notes, company_size, industry_preference, top_priority, security_clearance, travel_willingness, availability, employment_type, commute_tolerance, relocation

**AI-generated fields**: profile_summary, career_trajectory, leadership_style, industry_preferences, values, deal_breakers, strengths, growth_areas, ideal_culture, advisor_data

## Session/Progress Data

- **Spring Training level**: Computed dynamically from profile state (not stored). Checks: resume uploaded → profile analyzed → 3+ Q&A answers → all complete.
- **Job browse position**: `state.currentCardIndex` in frontend JS state (reset on page refresh)
- **Filter/sort preferences**: In-memory JS state (reset on page refresh)
- **Active view**: In-memory (reset on page refresh)
- **Search history**: Jobs are persisted to DB. Each search adds to the job pool. Jobs have `first_seen` and `last_seen` timestamps.

## Job Search History

- **Stored**: Yes — all jobs saved to `jobs` table with `scraped_at`, `first_seen`, `last_seen`, `source`, `sources_seen`
- **Searchable**: Via keyword filter in browse view
- **Influences recommendations**: Job data feeds career advisor, market intelligence, skills audit, and re-scoring

## AI Interaction History

- **Resume parses**: Overwrite profile fields (no versioning of parse results)
- **Cover letters**: Stored in `applications.cover_letter`
- **Interview prep**: Stored in `jobs.user_notes` and `interviews.prep_notes`
- **Tailored resumes**: Stored in `documents` table with versioning
- **Career advisor**: Cached in `profiles.advisor_data` (JSON)
- **Follow-up drafts**: Stored in `follow_ups.draft_content`
- **All others**: Not persisted — generated fresh each time

## Data Retention

- **Retention policy**: No explicit policy defined. Data persists indefinitely.
- **User export**: Not implemented. No GDPR export endpoint.
- **User deletion**: Not implemented. No account deletion endpoint.
- **Soft delete**: `archived_at` column on Jobs and Profiles (recently added, not yet wired to UI)

---

# 7. Test Accounts & Environment

## Environments

| Environment | URL | Database |
|-------------|-----|----------|
| Production | https://jobbunt-829547589641.us-east1.run.app | Cloud SQL (PostgreSQL) |
| Local Dev | http://localhost:8000 | SQLite (`data/jobbunt.db`) |
| No separate staging | — | — |

## Test Accounts

> **Note**: Production uses Google OAuth. Test accounts require Google accounts or local auth enabled via `LOCAL_AUTH=1`.

| Account | Stage | Description | Credentials |
|---------|-------|-------------|-------------|
| User 1 | Advanced | Full profile, 300+ jobs, applications, interview data | Google OAuth (primary dev account) |
| New test | Fresh | Create via Google OAuth or local registration | Any Google account |
| — | Mid-journey | Not pre-created. Can simulate by uploading resume and running 1 search | — |

**To create a test account at any stage**: Register → upload resume → run search → shortlist jobs → apply to some → wait 7+ days for follow-up reminders to trigger.

## Payment Test Mode

Not applicable — no payment integration exists. All features are free.

## Feature Flags

No formal feature flag system. Gating is via Spring Training levels (computed, not flagged).

## Admin/Debug Tools

| Tool | Access |
|------|--------|
| Health check | `GET /health` — DB, AI provider, API key status |
| Source health | `GET /api/sources/health` — scraper source success rates |
| Task status | `GET /api/tasks/{id}` — background task polling |
| Prompt Lab | `GET /api/prompt-lab/*` — view/edit/test prompt templates (if prompt_registry wired) |
| Cloud Run logs | `gcloud run services logs read jobbunt --region=us-east1` |

---

# 8. Design System & Brand Guidelines

## Color Palette

### Primary Palette
| Token | Hex | Usage |
|-------|-----|-------|
| `--jb-bg-deep` | `#090E18` | Page background |
| `--jb-bg-primary` | `#0D1B30` | Main content background |
| `--jb-bg-secondary` | `#111F38` | Card backgrounds |
| `--jb-bg-tertiary` | `#162744` | Section backgrounds |
| `--jb-bg-elevated` | `#1A2F50` | Elevated elements, hover states |
| `--jb-bg-hover` | `#1E3660` | Interactive hover |

### Text
| Token | Hex | Usage |
|-------|-----|-------|
| `--jb-text-1` | `#E0E6ED` | Primary text |
| `--jb-text-2` | `#8A9BB5` | Secondary text |
| `--jb-text-3` | `#566A87` | Muted text |
| `--jb-text-muted` | `#3B4F6B` | Ghost text |

### Accent Colors
| Token | Hex | Usage |
|-------|-----|-------|
| `--jb-bright` | `#4A90D9` | Primary accent, links, highlights |
| `--jb-bright-light` | `#6BAAEB` | Light accent variant |
| `--jb-navy` | `#1D2D5C` | Dark blue accent |
| `--jb-royal` | `#134A8E` | Royal blue accent |
| `--jb-red` | `#E8291C` | CTA buttons, destructive actions |
| `--jb-red-dark` | `#B8211A` | CTA hover state |
| `--jb-success` | `#34B87A` | Positive indicators, high scores |
| `--jb-warning` | `#E5A030` | Warnings, medium scores |
| `--jb-danger` | `#E05252` | Errors, low scores |
| `--jb-info` | `#4A90D9` | Informational |

### Border & Radius
| Token | Value | Usage |
|-------|-------|-------|
| `--jb-border` | `rgba(74,144,217,.08)` | Default borders |
| `--jb-border-hover` | `rgba(74,144,217,.15)` | Hover borders |
| `--jb-border-active` | `rgba(74,144,217,.25)` | Active/focus borders |
| `--jb-r-sm` | `6px` | Small border radius |
| `--jb-r-md` | `12px` | Medium border radius |
| `--jb-r-lg` | `14px` | Large border radius |
| `--jb-r-xl` | `24px` | Extra-large (pills) |

## Typography

| Aspect | Value |
|--------|-------|
| Primary Font | `Outfit` (Google Fonts, sans-serif) |
| Monospace Font | `JetBrains Mono` (Google Fonts) |
| Base Size | 14px |
| H1 | 24px, weight 700 |
| H2 | 20px, weight 600 |
| H3 | 16px, weight 600 |
| Body | 14px, weight 400, line-height 1.5 |
| Small | 12px |
| Micro | 11px |

## Component Library

No Storybook or component library exists. Components are CSS class-based:

- `.btn`, `.btn-primary`, `.btn-secondary`, `.btn-sm` — Button variants
- `.section-card` — Bordered content card
- `.modal-overlay`, `.modal-content` — Modal system
- `.score-ring` — Circular score indicator (CSS gradient)
- `.nav-link` — Bottom tab bar items
- `.filter-select` — Filter dropdowns
- `.skill-chip` — Skill tag pills
- `.toast` — Notification toasts
- `.cta-btn` — Red call-to-action buttons

## Baseball Visual Language

- **Tone**: Professional with personality. Not cartoonish or retro — modern sports analytics aesthetic.
- **Icons**: SVG icons in tab bar (home, magnifying glass, star, wrench, user). No baseball-specific icons.
- **Illustrations**: SVG empty-state illustrations (magnifying glass, baseball diamond shapes).
- **Animations**: CSS transitions on cards, hover states. No elaborate animations.
- **Score Ring**: Circular progress indicator using CSS conic-gradient — green (75+), amber (50-74), red (<50).

## Voice & Tone

- **App copy**: Direct, encouraging, slightly playful. "Hit SEARCH JOBS to find opportunities." "Your profile is game-ready!"
- **AI output**: Professional and specific. Cover letters are formal. Advisor recommendations are actionable. Scouting reports use baseball metaphors.
- **Error messages**: Friendly but clear. "Not enough data — search for jobs first."
- **Match with AI**: AI output is not explicitly styled to match the baseball theme (except the Scouting Report prompt which explicitly requests baseball metaphors).

## Accessibility Standards

**Target**: No formal target defined. Currently does not meet WCAG 2.1 AA (see Section 4 for gaps).

---

# 9. Analytics & Success Metrics

## Current Analytics Setup

**No analytics platform is integrated.** No Google Analytics, Mixpanel, PostHog, or custom event tracking.

The only data available is:
- Cloud Run request logs (access patterns, error rates)
- Database queries (can derive usage from job counts, application counts, timestamps)
- Box Score endpoint (funnel metrics per user)

## Key Metrics (Derivable from DB)

| Metric | How to Derive |
|--------|---------------|
| Total users | `SELECT COUNT(*) FROM users` |
| Profile completion rate | Profiles with `resume_text IS NOT NULL` / total profiles |
| Jobs per user | `SELECT profile_id, COUNT(*) FROM jobs GROUP BY profile_id` |
| Shortlist rate | Jobs with `status='liked'` / total jobs per user |
| Application rate | Applications / shortlisted jobs |
| AI feature usage | Count of Documents, Interviews, FollowUps per user |
| Score distribution | `SELECT ROUND(match_score/10)*10 as bucket, COUNT(*) FROM jobs GROUP BY bucket` |

## Funnel Data

No funnel tracking. Would need to instrument:
1. Signup → Profile creation (should be 100%, auto-linked)
2. Profile → Resume upload (key dropout risk)
3. Resume → First search (gated at Spring Training level 1)
4. Search → First shortlist
5. Shortlist → First application

## User Feedback

No surveys, NPS, interviews, or support ticket system implemented.

---

# 10. Competitive & Market Context

## Positioning Statement

Jobbunt is an AI-powered career development platform that combines deep candidate profiling, multi-source job discovery, and intelligent matching into a single app — wrapped in a baseball metaphor that makes the job search feel like a season to win rather than a chore to endure. Unlike LinkedIn (passive network), Teal/Huntr (tracking-focused), or Jobscan (keyword optimization), Jobbunt actively scouts jobs across 8+ sources, scores them against a 6-dimensional match model, and provides AI-powered interview prep and resume tailoring — features competitors charge $30-44/month for.

## Target User Stories

1. **Senior tech leader exploring options**: A CISO with 15 years of experience wants to passively monitor the market for director/VP roles in cybersecurity. Uses Jobbunt to auto-scout across sources, get scored matches, and maintain interview readiness without active job hunting.

2. **Career changer into tech**: A nurse with 8 years of clinical experience wants to transition into health tech. Uses AI profile analysis to identify transferable skills, skills audit to find gaps, and resume tailoring to reframe clinical experience for tech roles.

3. **New grad building momentum**: A recent CS graduate needs to apply at volume. Uses multi-source search to cast a wide net, cover letter generation to apply faster, and Box Score analytics to track their conversion funnel.

4. **Executive in active search**: A VP of Engineering laid off in a restructuring needs structured search management. Uses the pipeline tracker, follow-up reminders, interview prep, and career advisor to run a disciplined search.

5. **Government/public sector job seeker**: A Canadian professional targeting federal jobs. Uses JobBank and GC Jobs integration alongside private sector sources to find opportunities across both sectors simultaneously.

## Baseball Metaphor Rationale

**Why baseball?**
- Baseball is the most statistics-driven sport. The metaphor naturalizes the quantified, data-heavy nature of job matching (scores, percentages, comparisons).
- A baseball season is long — 162 games. Job searches are long too. The metaphor frames the search as a season with progression, not a single win/lose event.
- Baseball's farm system (development through levels) maps perfectly to career development stages.
- The language is familiar to North American users and provides memorable, distinctive branding.

**For the skeptic**: The metaphor is brand and personality, not barrier. Navigation labels use plain language (Home, Jobs, Tools, Profile). Baseball terminology appears as section names and flavor text within views, not as the primary interface language. Users who don't care about baseball experience a professional job search tool; users who enjoy it get an extra layer of engagement.

---

*End of QA Audit Documentation Package*
