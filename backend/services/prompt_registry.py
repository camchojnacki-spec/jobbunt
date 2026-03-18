"""Prompt Registry - centralized catalog of all AI prompts used across Jobbunt.

Provides a single place to view, edit, and manage all AI prompt templates.
Prompts are loaded from their source files at startup. Runtime edits are
stored in memory (lost on restart) — this is intentional for a "lab" tool.
"""
import copy
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ── Model tier overrides (per-feature) ────────────────────────────────────
_model_overrides: dict[str, str] = {}  # feature_key -> tier override

# ── Default prompts (populated below) ─────────────────────────────────────
_default_templates: dict[str, str] = {}

# ── Runtime prompt overrides ──────────────────────────────────────────────
_prompt_overrides: dict[str, str] = {}  # key -> overridden template

CATEGORIES = {
    "search": "Search & Discovery",
    "scoring": "Scoring & Matching",
    "applications": "Applications & Cover Letters",
    "profile": "Profile & Resume",
    "intelligence": "Intelligence & Strategy",
}

PROMPT_REGISTRY: dict[str, dict] = {
    "search_expansion": {
        "name": "Search Query Expansion",
        "description": "Generates alternative search queries and negative keywords based on the full candidate profile",
        "category": "search",
        "model_tier": "fast",
        "file": "scraper.py",
        "function": "_ai_expand_queries",
        "variables": ["base_roles", "seniority", "tiers_down", "tiers_up", "skills", "industries", "profile_summary", "career_trajectory", "strengths", "deal_breakers"],
        "prompt_template": """You are an executive job search strategist. Given this candidate's FULL profile context, generate highly targeted search queries and exclusion keywords.

**Candidate profile:**
- Target roles: {base_roles}
- Current seniority: {seniority}
- Search tiers down: {tiers_down} (0 = only target level, 1 = one level below OK, etc.)
- Search tiers up: {tiers_up} (0 = only target level, 1 = one level above OK, etc.)
- Key skills: {skills}
- Industries: {industries}
- Profile summary: {profile_summary}
- Career trajectory: {career_trajectory}
- Key strengths: {strengths}
- Deal breakers: {deal_breakers}

**CRITICAL SEARCH RULES:**
1. This candidate's domain is VERY specific. If they target "Chief Information SECURITY Officer" (CISO), do NOT include "Chief Information Officer" (CIO) — those are DIFFERENT roles.
2. Queries must be EXACT JOB TITLES, not skills or buzzwords.
3. Be precise about the domain: cybersecurity ≠ general IT, security engineering ≠ physical security.
4. Respect the seniority tier preferences.

Return a JSON object with:
{{
    "queries": ["5-8 alternative EXACT JOB TITLES that match this candidate's specific domain and seniority."],
    "negative_keywords": ["words/phrases to EXCLUDE — wrong-domain titles and terms."]
}}""",
    },

    "relevance_filter": {
        "name": "AI Relevance Filter",
        "description": "Post-search filter that removes jobs from completely wrong professional domains",
        "category": "search",
        "model_tier": "flash",
        "file": "scraper.py",
        "function": "filter_irrelevant_jobs",
        "variables": ["domain_context", "job_lines"],
        "prompt_template": """You are a job relevance filter. Given a candidate's profile domain, determine which jobs are RELEVANT (same professional domain) vs IRRELEVANT (completely wrong field/domain).

CANDIDATE DOMAIN:
{domain_context}

JOBS TO EVALUATE:
{job_lines}

Return a JSON object with:
- "keep": [list of job indices (integers) that ARE relevant to this candidate's domain]
- "reject": [list of job indices (integers) that are clearly WRONG domain]

Rules:
- REJECT jobs from a completely different professional field (e.g., physical security guard for an IT security professional)
- KEEP anything that's even plausibly related — err on the side of keeping
- Only reject clear domain mismatches, not merely imperfect fits""",
    },

    "job_scoring": {
        "name": "AI Job Scoring",
        "description": "Multi-dimensional scoring of job-candidate match across 6 dimensions with calibrated scoring rules",
        "category": "scoring",
        "model_tier": "fast",
        "file": "scorer.py",
        "function": "score_job_ai",
        "variables": ["profile_context", "job_title", "job_company", "job_location", "job_seniority", "job_salary", "job_description", "company_context", "research_context"],
        "prompt_template": """You are a calibrated career match analyst. Score this job match across 6 dimensions.

CRITICAL SCORING RULES:
- Be HONEST and DISCRIMINATING. Most jobs should score 40-70 overall. Reserve 80+ for genuinely strong matches.
- DOMAIN MISMATCH: If the candidate targets IT/cyber security but the job is physical security (guard, patrol, loss prevention, CCTV), score role_fit 0-5.
- Role Fit: Consider career trajectory, not just keyword overlap.
- Skills: Consider depth of match — core skills or peripheral ones?
- Compensation: Consider salary appropriateness for seniority level.
- Location: Exact city match = high. Remote when they want remote = high.
- Seniority: Consider whether this is a step up, lateral, or step down.
- Culture Fit: Use company data if available. Generic/unknown = 45-55.

CANDIDATE:
{profile_context}

JOB:
Title: {job_title}
Company: {job_company}
Location: {job_location}
Seniority: {job_seniority}
Salary: {job_salary}
Remote: {job_remote}
Description: {job_description}

{company_context}
{research_context}

Return JSON:
{{
    "breakdown": {{
        "role_fit": <0-100>,
        "skills": <0-100>,
        "location": <0-100>,
        "compensation": <0-100>,
        "seniority": <0-100>,
        "culture_fit": <0-100>
    }},
    "reasons": ["top 3-5 SHORT reasons"],
    "concerns": ["1-3 specific concerns"],
    "domain_match": true/false
}}

CALIBRATION CHECK: Before answering, ask yourself - would a thoughtful career advisor recommend this specific role to this specific candidate?""",
    },

    "profile_parse": {
        "name": "Profile Document Parser",
        "description": "Extracts structured profile data (roles, skills, salary, etc.) from pasted text or documents",
        "category": "profile",
        "model_tier": "fast",
        "file": "api.py",
        "function": "parse_profile_text",
        "variables": ["text"],
        "prompt_template": """Extract structured job search profile data from this document. Be precise and thorough.

EXTRACTION RULES:
- **target_roles**: Extract the EXACT job titles the candidate is targeting. Include all mentioned variations.
- **target_locations**: Extract all cities, regions, states/provinces they mention. Include "Remote" if they mention it.
- **skills**: Extract ALL professional skills, technologies, certifications, and competencies mentioned. Each skill should be 1-4 words. Max 20 skills.
- **seniority_level**: Based on ACTUAL career level (not aspirational). Must be: entry, mid, senior, director, vp, c-suite
- **experience_years**: Total years of professional experience (integer).
- **min_salary / max_salary**: Extract as integers (no currency symbols).
- **remote_preference**: Must be: remote, hybrid, onsite, any
- **cover_letter_template**: Extract any instructions about tone, style, or approach.

Return ONLY valid JSON:
{{
    "name": "full name",
    "email": "email",
    "phone": "phone",
    "location": "city, state/province",
    "target_roles": ["Role 1", "Role 2"],
    "target_locations": ["City, ST"],
    "min_salary": 165000,
    "max_salary": 200000,
    "remote_preference": "any",
    "experience_years": 15,
    "skills": ["Skill 1", "Skill 2"],
    "seniority_level": "director",
    "cover_letter_template": "professional tone..."
}}

DOCUMENT:
{text}""",
    },

    "profile_analysis": {
        "name": "Deep Profile Analysis",
        "description": "Analyzes raw profile/resume to extract career trajectory, values, strengths, and ideal culture",
        "category": "profile",
        "model_tier": "balanced",
        "file": "enrichment.py",
        "function": "analyze_profile",
        "variables": ["source_text"],
        "prompt_template": """You are a career analyst. Deeply analyze this candidate profile and extract structured insights.

PROFILE DOCUMENT:
{source_text}

Return JSON:
{{
    "profile_summary": "3-4 sentence executive summary in third person",
    "career_trajectory": "2-3 sentence narrative of career arc",
    "leadership_style": "1-2 sentences about management/leadership approach",
    "industry_preferences": ["industries/sectors they'd thrive in"],
    "values": ["work values important to them"],
    "deal_breakers": ["things they likely wouldn't accept"],
    "strengths": ["top 5-7 key differentiators"],
    "growth_areas": ["areas they want to develop"],
    "ideal_culture": "2-3 sentence ideal work environment",
    "seniority_level": "entry|mid|senior|director|vp|c-suite"
}}

IMPORTANT:
- Base everything on evidence in the document, but read between the lines
- Be specific about strengths — actual differentiators, not generic qualities""",
    },

    "company_enrichment": {
        "name": "Company Profile Enrichment",
        "description": "Generates culture summary, pros/cons, multi-axis scorecard, and sentiment for companies",
        "category": "intelligence",
        "model_tier": "fast",
        "file": "enrichment.py",
        "function": "_ai_enrich_company",
        "variables": ["context_parts"],
        "prompt_template": """You are a company research analyst. Given what you know about this company, provide a comprehensive employer profile and scorecard.

{context_parts}

Return JSON:
{{
    "culture_summary": "2-3 sentence assessment of work culture and employee experience",
    "pros": ["pro1", "pro2", "pro3", "pro4"],
    "cons": ["con1", "con2", "con3"],
    "description": "1-2 sentence company description",
    "website": "company domain (e.g. google.com)",
    "industry": "industry sector",
    "scorecard": {{
        "culture": <0-100>,
        "compensation": <0-100>,
        "growth": <0-100>,
        "wlb": <0-100>,
        "leadership": <0-100>,
        "diversity": <0-100>
    }},
    "scorecard_summary": "1-2 sentence strongest and weakest areas",
    "sentiment": {{
        "positive": <0-100>,
        "negative": <0-100>,
        "neutral": <0-100>
    }}
}}

SCORING GUIDELINES: 80-100 Exceptional | 60-79 Good | 40-59 Average | 20-39 Below Average | 0-19 Poor""",
    },

    "job_enrichment": {
        "name": "Job Posting Enrichment",
        "description": "Extracts seniority, salary estimates, role summary, red flags, and key requirements from job postings",
        "category": "scoring",
        "model_tier": "fast",
        "file": "enrichment.py",
        "function": "_ai_enrich_job",
        "variables": ["job_title", "job_company", "job_location", "job_salary", "description"],
        "prompt_template": """Analyze this job posting and extract structured insights.

Title: {job_title}
Company: {job_company}
Location: {job_location}
Listed salary: {job_salary}
Description:
{description}

Return JSON:
{{
    "seniority_level": "entry|mid|senior|director|vp|c-suite",
    "reports_to": "title of who this role reports to",
    "team_size": "team size if mentioned",
    "remote_type": "remote|hybrid|onsite|unclear",
    "salary_min_estimate": estimated_min_salary_int_or_null,
    "salary_max_estimate": estimated_max_salary_int_or_null,
    "posted_date": "when posted or empty string",
    "closing_date": "deadline or empty string",
    "role_summary": "2-3 sentence plain-language summary of day-to-day",
    "red_flags": ["concerns"],
    "why_apply": ["compelling reasons to apply"],
    "key_requirements": ["top 3-5 requirements"]
}}

Be honest about red flags. Estimate salary based on title, location, and industry norms if not listed.""",
    },

    "deep_research": {
        "name": "Deep Job Research",
        "description": "Phase 2 deep-dive on shortlisted jobs: culture, interview process, growth opportunities, day-in-life",
        "category": "intelligence",
        "model_tier": "deep",
        "file": "enrichment.py",
        "function": "deep_research_job",
        "variables": ["job_title", "job_company", "job_location", "job_seniority", "job_salary", "company_context", "description", "glassdoor_context", "profile_name", "profile_level", "profile_skills"],
        "prompt_template": """You are a career intelligence analyst doing deep research on a specific job opportunity.

JOB: {job_title} at {job_company}
Location: {job_location}
Seniority: {job_seniority}
Salary: {job_salary}

{company_context}
{description}
{glassdoor_context}

CANDIDATE: {profile_name} | Level: {profile_level} | Skills: {profile_skills}

Return JSON:
{{
    "culture_insights": "3-4 sentences about work culture specific to this role level",
    "interview_process": "2-3 sentences about likely interview process, rounds, and tips",
    "growth_opportunities": "2-3 sentences about career growth from this role in 2-3 years",
    "day_in_life": "2-3 sentences about a typical week in this role",
    "hiring_sentiment": "1-2 sentences about hiring climate for this type of role"
}}

Be specific to THIS role at THIS company, not generic advice.""",
    },

    "cover_letter": {
        "name": "Cover Letter Generator",
        "description": "Generates tailored, compelling 4-5 paragraph cover letters with specific accomplishments and metrics",
        "category": "applications",
        "model_tier": "balanced",
        "file": "agent.py",
        "function": "generate_cover_letter",
        "variables": ["profile_name", "skills", "experience_years", "resume_text", "career_trajectory", "leadership_style", "strengths", "job_title", "job_company", "job_description"],
        "prompt_template": """Write a compelling, detailed cover letter for this candidate applying to this job.

STRUCTURE (4-5 substantive paragraphs):
1. Opening hook — why this specific role at this specific company excites them.
2. Core experience — 2-3 specific accomplishments with metrics/outcomes.
3. Leadership & strategic value — unique perspective beyond the job description.
4. Cultural & mission alignment — why they'd thrive at this company specifically.
5. Strong close — confident but not arrogant, clear call to action.

RULES:
- Be SPECIFIC: name actual technologies, methodologies, outcomes
- Include METRICS where possible (team sizes, budgets, % improvements)
- Sound like a real human — not a template
- 400-600 words total
- Do NOT use "I am writing to express my interest" or "I believe I would be a great fit"

CANDIDATE:
Name: {profile_name}
Skills: {skills}
Experience: {experience_years} years
Resume: {resume_text}
Career trajectory: {career_trajectory}
Leadership style: {leadership_style}
Key strengths: {strengths}

JOB:
Title: {job_title}
Company: {job_company}
Description: {job_description}

Write the complete cover letter including salutation and closing.""",
    },

    "application_analysis": {
        "name": "Application Requirements Analyzer",
        "description": "Analyzes job postings to determine application method, platform, required fields, and missing info",
        "category": "applications",
        "model_tier": "balanced",
        "file": "agent.py",
        "function": "_analyze_application",
        "variables": ["job_title", "job_company", "job_url", "job_description", "profile_name", "profile_email", "profile_skills"],
        "prompt_template": """You are a job application assistant. Analyze this job posting and determine:
1. What information is needed to apply (beyond standard resume/cover letter)
2. Whether there are any questions that need the candidate's input
3. What platform/method should be used to apply

Job: {job_title} at {job_company}
URL: {job_url}
Description: {job_description}

Candidate info we already have:
- Name: {profile_name}
- Email: {profile_email}
- Skills: {profile_skills}

Respond in JSON:
{{
    "can_proceed": true/false,
    "missing_info": ["list of info we still need"],
    "questions_for_candidate": ["specific questions to ask"],
    "application_strategy": "step-by-step how to apply",
    "application_type": "easy_apply|company_site|ats|email|unknown",
    "platform": "linkedin|indeed|workday|greenhouse|lever|company_direct|other",
    "form_fields_expected": ["name", "email", "resume", "cover_letter", "..."],
    "notes": "any other observations"
}}

Only ask questions that we genuinely cannot answer from the profile.""",
    },

    "deduplication": {
        "name": "Job Deduplication",
        "description": "Identifies duplicate job postings from different sources with slightly different titles or company names",
        "category": "search",
        "model_tier": "fast",
        "file": "api.py",
        "function": "ai_dedup_jobs",
        "variables": ["pairs_text"],
        "prompt_template": """You are a job listing deduplication expert. Determine which pairs are duplicates
(the SAME job posting from different sources or with slightly different titles).

Consider a DUPLICATE if:
- Same company (even with different name formats like "RBC" vs "Royal Bank of Canada")
- Same or very similar role (e.g., "Sr Developer" vs "Senior Software Developer")
- Similar location (same city counts)

NOT a duplicate if:
- Different seniority levels (Junior vs Senior)
- Genuinely different roles at the same company
- Different departments/teams (if distinguishable)

Pairs to check:
{pairs_text}

Return a JSON array of pair indices that ARE duplicates. Example: [0, 3, 7]
If none are duplicates, return: []""",
    },

    "qa_synthesis": {
        "name": "Profile Q&A Synthesis",
        "description": "Synthesizes interview Q&A answers into structured profile insights (summary, trajectory, strengths, values)",
        "category": "profile",
        "model_tier": "balanced",
        "file": "api.py",
        "function": "_synthesize_qa_into_profile",
        "variables": ["profile_name", "seniority", "target_roles", "experience_years", "profile_summary", "qa_text"],
        "prompt_template": """You are an expert career profiler. Synthesize the candidate's interview Q&A answers into structured profile fields.

CURRENT PROFILE:
Name: {profile_name}
Seniority: {seniority}
Target Roles: {target_roles}
Experience: {experience_years} years
Current Summary: {profile_summary}

INTERVIEW Q&A:
{qa_text}

Return UPDATED profile insights as JSON. Only include fields where Q&A provides new information.

{{
    "profile_summary": "3-4 sentence executive summary incorporating Q&A insights",
    "career_trajectory": "2-3 sentence narrative updated with new context",
    "leadership_style": "1-2 sentences if Q&A reveals leadership approach",
    "strengths": ["top 5-7 differentiators informed by Q&A"],
    "growth_areas": ["areas they want to develop"],
    "values": ["work values revealed by Q&A"],
    "deal_breakers": ["things they won't accept"],
    "ideal_culture": "2-3 sentences about ideal work culture",
    "industry_preferences": ["preferred industries"]
}}

CRITICAL: Merge new information with existing data. Don't lose existing insights — enhance them.""",
    },

    "question_generation": {
        "name": "Profile Question Generator",
        "description": "Generates tiered interview questions to build the strongest possible application profile",
        "category": "profile",
        "model_tier": "balanced",
        "file": "api.py",
        "function": "_generate_questions_worker",
        "variables": ["tier", "tier_instructions", "profile_name", "experience_years", "seniority", "skills", "target_roles", "job_desc_context", "answered_context"],
        "prompt_template": """You are a career coach helping a job seeker build the strongest possible application profile.
Your approach is PRACTICAL and APPLICATION-DRIVEN.

CURRENT TIER: {tier} (based on answers already given)
{tier_instructions}

Generate 5 targeted questions for this tier.

CANDIDATE PROFILE:
Name: {profile_name}
Experience: {experience_years} years
Current Seniority: {seniority}
Skills: {skills}
Target Roles: {target_roles}

JOB MARKET CONTEXT:
{job_desc_context}

ALREADY ANSWERED:
{answered_context}

Return ONLY valid JSON array:
[
    {{
        "question": "Your question here",
        "category": "application_basics|resume_improvement|experience|motivation|preferences|culture|leadership|technical|self_assessment",
        "priority": 1-10,
        "purpose": "Why this question matters practically"
    }}
]""",
    },

    "career_advisor": {
        "name": "Career Search Advisor",
        "description": "Comprehensive career analysis: trajectory assessment, ambition calibration, profile suggestions, action plan",
        "category": "intelligence",
        "model_tier": "deep",
        "file": "api.py",
        "function": "search_advisor",
        "variables": ["profile_context", "job_data", "market_context"],
        "prompt_template": """You are a senior career strategist providing a comprehensive job search assessment.

{profile_context}

{job_data}

{market_context}

Return JSON with: overall_assessment, career_trajectory_analysis (current_level, target_realism, trajectory_narrative, gap_to_target, recommended_level), ambition_assessment (verdict, explanation, confidence), search_strategy, profile_suggestions (field, current_value, suggested_value, reason), roles_to_consider, market_fit_score, resume_feedback, skills_to_highlight, skills_to_develop, positioning_tips, quick_wins, action_plan (30/60/90 days), networking_targets, keywords_for_ats, industry_targets, companies_to_target, red_flags_in_profile, differentiators, questions_to_explore.

BE SPECIFIC: Reference actual resume content, job titles, companies, and data.""",
    },

    "market_intelligence": {
        "name": "Market Intelligence Report",
        "description": "Analyzes job search data to identify market trends, opportunities, risks, and strategic recommendations",
        "category": "intelligence",
        "model_tier": "deep",
        "file": "api.py",
        "function": "job_insights",
        "variables": ["profile_context", "job_summaries", "stats"],
        "prompt_template": """You are a labor market analyst providing strategic intelligence for a job seeker.

{profile_context}

JOB SEARCH DATA:
{job_summaries}

STATS:
{stats}

ANALYSIS REQUIREMENTS:
1. Assess competitive position (0-100 score)
2. Identify unexplored niches
3. Market timing advice
4. Competitor landscape
5. Top skills in demand
6. Application strategy advice

Return JSON:
{{
    "market_summary": "2-3 sentence summary",
    "themes": ["4 patterns/trends observed"],
    "opportunities": ["actionable opportunities"],
    "risks": ["market risks"],
    "salary_insight": "compensation trends vs expectations",
    "demand_signals": ["demand indicators"],
    "recommendations": ["3 strategic recommendations"],
    "skill_gaps": ["frequently requested but missing skills"],
    "hot_companies": ["actively hiring companies"],
    "market_position": <0-100>,
    "underexplored_areas": ["unexplored niches"],
    "timing_advice": "timing considerations",
    "top_skills_in_demand": ["most requested skills"],
    "application_strategy": "prioritization advice"
}}""",
    },

    "resume_improvement": {
        "name": "Resume Improvement Analyzer",
        "description": "ATS optimization specialist that provides specific resume suggestions with scoring and keyword analysis",
        "category": "profile",
        "model_tier": "balanced",
        "file": "api.py",
        "function": "improve_resume",
        "variables": ["resume_text", "target_roles", "experience_years", "skills", "profile_summary", "qa_context", "job_keywords_context"],
        "prompt_template": """You are an expert resume reviewer and ATS optimization specialist.

RESUME:
{resume_text}

CANDIDATE CONTEXT:
- Target Roles: {target_roles}
- Experience: {experience_years} years
- Skills: {skills}
{profile_summary}

INTERVIEW Q&A:
{qa_context}
{job_keywords_context}

Return JSON:
{{
    "suggestions": [
        {{
            "section": "Which resume section",
            "type": "quantify|add|reword|remove|reorder",
            "current": "Current text or N/A",
            "suggested": "Your specific suggested improvement",
            "reason": "Why this improves the resume"
        }}
    ],
    "overall_score": 0-100,
    "missing_keywords": ["keywords from job descriptions missing from resume"],
    "ats_tips": ["specific ATS optimization tips"]
}}

Provide 8-15 specific suggestions, prioritized by impact.""",
    },

    "skills_audit": {
        "name": "Skills Gap Audit",
        "description": "Analyzes job postings to find in-demand skills the candidate is missing and low-value skills to remove",
        "category": "intelligence",
        "model_tier": "balanced",
        "file": "api.py",
        "function": "skills_audit",
        "variables": ["profile_skills", "sample_reqs"],
        "prompt_template": """Analyze these job posting requirements and extract the most in-demand SKILLS.

CANDIDATE'S CURRENT SKILLS: {profile_skills}

JOB POSTINGS (top 20 by match score):
{sample_reqs}

Return JSON:
{{
    "missing_high_demand": ["skills in 5+ postings candidate doesn't have"],
    "missing_moderate": ["skills in 2-4 postings candidate is missing"],
    "low_value_skills": ["candidate skills in <5% of postings"],
    "skill_categories": {{
        "technical": ["technical/tool skills to add"],
        "frameworks": ["frameworks, standards, certifications"],
        "leadership": ["leadership/management skills"],
        "domain": ["domain/industry expertise"]
    }},
    "recommended_additions": ["top 5-8 most impactful skills to add"],
    "recommended_removals": ["skills to consider removing"]
}}

Use standard industry terminology. Each skill should be 1-4 words.""",
    },

    "automation_plan": {
        "name": "Application Automation Plan",
        "description": "Generates step-by-step browser automation instructions for submitting job applications",
        "category": "applications",
        "model_tier": "fast",
        "file": "automator.py",
        "function": "_ai_automation_plan",
        "variables": ["platform", "job_url", "job_title", "job_company", "job_description", "experience_years"],
        "prompt_template": """You are a job application automation expert. Create specific step-by-step instructions for submitting an application.

Platform: {platform}
Job URL: {job_url}
Job Title: {job_title}
Company: {job_company}
Description: {job_description}

The candidate has: full name, email, phone, location, resume file, cover letter, {experience_years} years of experience.

Return JSON:
{{
    "steps": ["detailed step 1", "detailed step 2", ...],
    "requires_account": true/false,
    "requires_signin": true/false,
    "expected_questions": ["screening questions that might be asked"],
    "notes": "important observations about this application"
}}

Be specific about what to click, fill in, and watch out for.""",
    },

    "career_stats": {
        "name": "Career History Extractor",
        "description": "Extracts all work history positions from resume text for baseball-card style stats",
        "category": "profile",
        "model_tier": "balanced",
        "file": "api.py",
        "function": "_do_career_stats",
        "variables": ["resume"],
        "prompt_template": """Extract ALL work history positions from this resume. Look through the ENTIRE document.

CRITICAL RULES:
- Extract EXACTLY what's in the resume. Do NOT guess or expand abbreviations.
- Copy company names character-for-character as they appear.

Return a JSON array of ALL positions, ordered most recent first:
- "company": company name EXACTLY as written
- "role": job title (max 35 chars)
- "start_year": start year int or null
- "end_year": end year int or null (null if current)
- "years": duration in years as integer
- "highlight": one key achievement (max 35 chars)

Include EVERY position listed.

Full Resume:
{resume}""",
    },

    "scouting_report": {
        "name": "Scouting Report Generator",
        "description": "Generates baseball-scout-style narrative using sports metaphors for candidate assessment",
        "category": "profile",
        "model_tier": "balanced",
        "file": "api.py",
        "function": "_do_scouting_report",
        "variables": ["name", "experience_years", "roles", "skills", "career_summary", "resume"],
        "prompt_template": """Write a baseball-scout-style scouting report (4-6 sentences) on this job candidate. Use baseball metaphors: "tools" (skills), "arm" (leadership), "bat" (technical chops), "speed" (adaptability), "baseball IQ" (strategy).

{name} | {experience_years} yrs | Targeting: {roles}
Skills: {skills}
Career: {career_summary}
Resume: {resume}

Return ONLY the scouting report paragraph. No labels or headers.""",
    },

    "email_classification": {
        "name": "Email Classification",
        "description": "Classifies incoming emails as application confirmations, interview requests, rejections, or follow-ups",
        "category": "applications",
        "model_tier": "flash",
        "file": "email_monitor.py",
        "function": "classify_email_ai",
        "variables": ["subject", "from_addr", "snippet"],
        "prompt_template": """Classify this email related to a job application.

Subject: {subject}
From: {from_addr}
Preview: {snippet}

Return JSON:
{{
    "classification": "confirmed|interview|rejected|follow_up|irrelevant",
    "company_name": "company name mentioned or inferred",
    "job_title": "job title if mentioned, or null",
    "confidence": 0.0-1.0,
    "summary": "1-sentence summary"
}}

Rules:
- "confirmed" = application received
- "interview" = scheduling interview or next steps
- "rejected" = not moving forward
- "follow_up" = need more info
- "irrelevant" = not job application related""",
    },

    "draft_answer": {
        "name": "Interview Answer Drafter",
        "description": "Drafts first-person answers to profile interview questions based on resume and experience",
        "category": "profile",
        "model_tier": "balanced",
        "file": "api.py",
        "function": "draft_answer",
        "variables": ["profile_context", "resume_excerpt", "qa_context", "question"],
        "prompt_template": """Based on this candidate's profile and resume, draft a concise answer to this interview question. Be specific, use concrete examples. Keep it 2-4 sentences.

Candidate Profile:
{profile_context}
{resume_excerpt}
{qa_context}

Interview Question: {question}

Draft a first-person answer as the candidate. Be natural and conversational, not robotic. Return ONLY the answer text.""",
    },
}

# Store defaults at module load time
for _key, _entry in PROMPT_REGISTRY.items():
    _default_templates[_key] = _entry["prompt_template"]


# ── Public API ────────────────────────────────────────────────────────────

def get_prompt(key: str) -> Optional[dict]:
    """Return prompt metadata for a given key, with any runtime overrides applied."""
    entry = PROMPT_REGISTRY.get(key)
    if not entry:
        return None
    result = dict(entry)
    if key in _prompt_overrides:
        result["prompt_template"] = _prompt_overrides[key]
        result["is_modified"] = True
    else:
        result["is_modified"] = False
    # Apply model override if any
    if key in _model_overrides:
        result["model_tier_override"] = _model_overrides[key]
    return result


def get_all_prompts() -> dict:
    """Return all prompts grouped by category."""
    grouped = {}
    for cat_key, cat_label in CATEGORIES.items():
        grouped[cat_key] = {"label": cat_label, "prompts": []}

    for key, entry in PROMPT_REGISTRY.items():
        cat = entry.get("category", "intelligence")
        prompt_data = {
            "key": key,
            "name": entry["name"],
            "description": entry["description"],
            "category": cat,
            "model_tier": entry["model_tier"],
            "file": entry["file"],
            "function": entry["function"],
            "variables": entry.get("variables", []),
            "prompt_template": _prompt_overrides.get(key, entry["prompt_template"]),
            "is_modified": key in _prompt_overrides,
        }
        if key in _model_overrides:
            prompt_data["model_tier_override"] = _model_overrides[key]
        if cat in grouped:
            grouped[cat]["prompts"].append(prompt_data)
        else:
            grouped.setdefault("intelligence", {"label": "Intelligence & Strategy", "prompts": []})
            grouped["intelligence"]["prompts"].append(prompt_data)

    return grouped


def update_prompt(key: str, new_template: str) -> bool:
    """Update a prompt template at runtime. Returns True if successful."""
    if key not in PROMPT_REGISTRY:
        return False
    _prompt_overrides[key] = new_template
    logger.info(f"Prompt '{key}' updated at runtime ({len(new_template)} chars)")
    return True


def reset_prompt(key: str) -> bool:
    """Reset a prompt to its default template. Returns True if it was modified."""
    if key not in PROMPT_REGISTRY:
        return False
    was_modified = key in _prompt_overrides
    _prompt_overrides.pop(key, None)
    if was_modified:
        logger.info(f"Prompt '{key}' reset to default")
    return was_modified


def get_default_template(key: str) -> Optional[str]:
    """Return the original default template for a prompt."""
    return _default_templates.get(key)


# ── Model Configuration ──────────────────────────────────────────────────

def get_model_config() -> dict:
    """Return current model configuration including tiers and overrides."""
    from backend.services.ai import GEMINI_MODELS, ANTHROPIC_MODELS, get_provider

    provider = get_provider()
    models = ANTHROPIC_MODELS if provider == "anthropic" else GEMINI_MODELS

    tiers = {}
    for tier_name in ["flash", "balanced", "deep"]:
        tiers[tier_name] = {
            "gemini": GEMINI_MODELS.get(tier_name, ""),
            "anthropic": ANTHROPIC_MODELS.get(tier_name, ""),
            "active": models.get(tier_name, ""),
        }

    # Build per-feature overrides view
    feature_overrides = {}
    for key, entry in PROMPT_REGISTRY.items():
        feature_overrides[key] = {
            "default_tier": entry["model_tier"],
            "override_tier": _model_overrides.get(key),
            "active_tier": _model_overrides.get(key, entry["model_tier"]),
        }

    return {
        "provider": provider,
        "tiers": tiers,
        "feature_overrides": feature_overrides,
        "overrides": dict(_model_overrides),
    }


def set_model_override(feature_key: str, model_tier: str) -> bool:
    """Set a per-feature model tier override."""
    if feature_key not in PROMPT_REGISTRY:
        return False
    valid_tiers = ["flash", "balanced", "deep"]
    if model_tier not in valid_tiers:
        return False
    _model_overrides[feature_key] = model_tier
    logger.info(f"Model override: {feature_key} -> {model_tier}")
    return True


def clear_model_override(feature_key: str) -> bool:
    """Remove a per-feature model tier override."""
    was_set = feature_key in _model_overrides
    _model_overrides.pop(feature_key, None)
    return was_set
