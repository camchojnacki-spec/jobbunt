"""Job and company enrichment service - validates, fills gaps, builds company profiles."""
import json
import logging
import re
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from backend.models.models import Job, Company, Profile
from backend.services.scorer import score_and_update_job
from backend.services.ai import ai_generate, ai_generate_json, get_provider

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── Company Enrichment ───────────────────────────────────────────────────

def get_or_create_company(db: Session, company_name: str) -> Company:
    """Find or create a company record by normalized name."""
    normalized = company_name.lower().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    # Strip common suffixes for matching
    for suffix in [" inc", " inc.", " ltd", " ltd.", " corp", " corp.", " llc", " co.", " group"]:
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)].strip()

    company = db.query(Company).filter(Company.name_normalized == normalized).first()
    if not company:
        company = Company(
            name=company_name.strip(),
            name_normalized=normalized,
        )
        db.add(company)
        db.commit()
        db.refresh(company)
    return company


async def enrich_company(db: Session, company: Company) -> Company:
    """Enrich a company with ratings, reviews, and insights from multiple sources."""
    if company.enriched:
        return company

    # Try scraping Glassdoor for ratings
    glassdoor_data = await _scrape_glassdoor_company(company.name)
    if glassdoor_data:
        company.glassdoor_rating = glassdoor_data.get("rating")
        company.glassdoor_reviews_count = glassdoor_data.get("reviews_count")
        company.glassdoor_url = glassdoor_data.get("url")
        company.size = glassdoor_data.get("size") or company.size
        company.industry = glassdoor_data.get("industry") or company.industry
        company.headquarters = glassdoor_data.get("headquarters") or company.headquarters
        company.website = glassdoor_data.get("website") or company.website
        company.recommend_pct = glassdoor_data.get("recommend_pct")
        company.ceo_approval = glassdoor_data.get("ceo_approval")

    # Try Indeed ratings
    indeed_data = await _scrape_indeed_company(company.name)
    if indeed_data:
        company.indeed_rating = indeed_data.get("rating")
        if not company.size:
            company.size = indeed_data.get("size")
        if not company.industry:
            company.industry = indeed_data.get("industry")

    # Use AI to generate insights if we have any data
    if get_provider() != "none":
        await _ai_enrich_company(company)

    company.enriched = True
    company.enriched_at = datetime.utcnow()
    db.commit()
    return company


async def _scrape_glassdoor_company(name: str) -> dict:
    """Scrape Glassdoor for company overview and ratings."""
    try:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
            # Search for company
            resp = await client.get(
                "https://www.glassdoor.com/Search/results.htm",
                params={"keyword": name, "typeName": "company"},
            )
            if resp.status_code != 200:
                return {}

            soup = BeautifulSoup(resp.text, "html.parser")

            # Try to extract rating from search results
            rating_el = soup.select_one("[data-test='rating'], .rating, .css-1nuumx7")
            rating = None
            if rating_el:
                try:
                    rating = float(rating_el.get_text(strip=True))
                except (ValueError, TypeError):
                    pass

            # Look for review count
            reviews_el = soup.select_one("[data-test='reviews-count'], .numReviews")
            reviews_count = None
            if reviews_el:
                match = re.search(r"([\d,]+)", reviews_el.get_text())
                if match:
                    reviews_count = int(match.group(1).replace(",", ""))

            # Size and industry from overview
            size = None
            industry = None
            for el in soup.select("span, div"):
                text = el.get_text(strip=True)
                if "employees" in text.lower() and any(c.isdigit() for c in text):
                    size = text
                if el.get("data-test") == "employer-industry":
                    industry = text

            return {
                "rating": rating,
                "reviews_count": reviews_count,
                "size": size,
                "industry": industry,
                "url": str(resp.url),
            }
    except Exception as e:
        logger.debug(f"Glassdoor scrape failed for {name}: {e}")
        return {}


async def _scrape_indeed_company(name: str) -> dict:
    """Scrape Indeed for company info."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
            resp = await client.get(
                f"https://www.indeed.com/cmp/{name.replace(' ', '-')}"
            )
            if resp.status_code != 200:
                return {}

            soup = BeautifulSoup(resp.text, "html.parser")
            rating_el = soup.select_one("[itemprop='ratingValue'], .css-1nuumx7")
            rating = None
            if rating_el:
                try:
                    rating = float(rating_el.get("content", rating_el.get_text(strip=True)))
                except (ValueError, TypeError):
                    pass

            return {"rating": rating}
    except Exception as e:
        logger.debug(f"Indeed company scrape failed for {name}: {e}")
        return {}


async def _ai_enrich_company(company: Company):
    """Use AI to generate culture summary, insights, and multi-axis scorecard."""
    context_parts = [f"Company: {company.name}"]
    if company.industry:
        context_parts.append(f"Industry: {company.industry}")
    if company.size:
        context_parts.append(f"Size: {company.size}")
    if company.headquarters:
        context_parts.append(f"HQ: {company.headquarters}")
    if company.glassdoor_rating:
        context_parts.append(f"Glassdoor rating: {company.glassdoor_rating}/5")
    if company.glassdoor_reviews_count:
        context_parts.append(f"Number of Glassdoor reviews: {company.glassdoor_reviews_count}")
    if company.indeed_rating:
        context_parts.append(f"Indeed rating: {company.indeed_rating}/5")
    if company.recommend_pct:
        context_parts.append(f"Recommend to friend: {company.recommend_pct}%")
    if company.ceo_approval:
        context_parts.append(f"CEO approval: {company.ceo_approval}%")

    prompt = f"""You are a company research analyst. Given what you know about this company, provide a comprehensive employer profile and scorecard. Use your training data knowledge plus the ratings provided. Be honest and calibrated - don't give inflated scores.

{chr(10).join(context_parts)}

Return JSON:
{{
    "culture_summary": "2-3 sentence assessment of work culture, reputation, and employee experience",
    "pros": ["pro1", "pro2", "pro3", "pro4"],
    "cons": ["con1", "con2", "con3"],
    "description": "1-2 sentence company description",
    "website": "company main website domain (e.g. uhn.ca, google.com) - just the domain, no https",
    "industry": "industry sector",
    "scorecard": {{
        "culture": <0-100 score for workplace culture and values>,
        "compensation": <0-100 score for pay competitiveness and benefits>,
        "growth": <0-100 score for career growth and learning opportunities>,
        "wlb": <0-100 score for work-life balance>,
        "leadership": <0-100 score for management quality and direction>,
        "diversity": <0-100 score for diversity, equity, and inclusion>
    }},
    "scorecard_summary": "1-2 sentence explanation of strongest and weakest areas",
    "sentiment": {{
        "positive": <0-100 overall positive sentiment>,
        "negative": <0-100 overall negative sentiment>,
        "neutral": <0-100 overall neutral sentiment>
    }}
}}

SCORING GUIDELINES:
- 80-100: Exceptional, among the best employers
- 60-79: Good, above average
- 40-59: Average, mixed signals
- 20-39: Below average, concerning
- 0-19: Poor, significant red flags
- If you have limited data, score closer to 50 (neutral) and note it"""

    data = await ai_generate_json(prompt, max_tokens=800, model_tier="fast")
    if data:
        company.culture_summary = data.get("culture_summary", "")
        company.pros = json.dumps(data.get("pros", []))
        company.cons = json.dumps(data.get("cons", []))
        if not company.description:
            company.description = data.get("description", "")
        if not company.website and data.get("website"):
            company.website = data["website"]
        if not company.industry:
            company.industry = data.get("industry", "")

        # Scorecard
        sc = data.get("scorecard", {})
        if sc:
            company.score_culture = sc.get("culture")
            company.score_compensation = sc.get("compensation")
            company.score_growth = sc.get("growth")
            company.score_wlb = sc.get("wlb")
            company.score_leadership = sc.get("leadership")
            company.score_diversity = sc.get("diversity")
            scores = [v for v in [
                company.score_culture, company.score_compensation,
                company.score_growth, company.score_wlb,
                company.score_leadership, company.score_diversity,
            ] if v is not None]
            if scores:
                company.score_overall = round(sum(scores) / len(scores), 1)

        company.scorecard_summary = data.get("scorecard_summary", "")
        sentiment = data.get("sentiment")
        if sentiment:
            company.sentiment = json.dumps(sentiment)


# ── Job Enrichment ───────────────────────────────────────────────────────

async def enrich_job(db: Session, job: Job, profile: Profile) -> Job:
    """Enrich a job with full description, salary estimates, validation, and insights."""
    if job.enriched:
        return job

    # 1. Fetch full description if missing/short
    if job.url and (not job.description or len(job.description) < 200):
        full_desc = await _fetch_full_description(job.url)
        if full_desc:
            job.description = full_desc

    # 2. Validate URL is still active
    if job.url:
        is_valid = await _check_url_valid(job.url)
        job.url_valid = is_valid
        job.url_checked_at = datetime.utcnow()

    # 3. Link to company record
    if not job.company_id:
        company = get_or_create_company(db, job.company)
        job.company_id = company.id
        # Enrich company if not yet done
        if not company.enriched:
            await enrich_company(db, company)

    # 4. AI enrichment - always run if AI is available
    # Even without a description, AI can infer details from title/company/location
    if get_provider() != "none":
        await _ai_enrich_job(job, profile)

    job.enriched = True
    job.enriched_at = datetime.utcnow()
    db.commit()
    return job


async def _fetch_full_description(url: str) -> Optional[str]:
    """Fetch and extract the full job description from the posting URL."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()

            desc_el = (
                soup.select_one("#jobDescriptionText") or
                soup.select_one("div.show-more-less-html__markup") or
                soup.select_one("div.jobDescriptionContent") or
                soup.select_one("div[id*='description']") or
                soup.select_one("div[class*='description']") or
                soup.select_one("article") or
                soup.select_one("main")
            )
            if desc_el:
                return desc_el.get_text(separator="\n", strip=True)[:5000]
    except Exception as e:
        logger.debug(f"Failed to fetch description from {url}: {e}")
    return None


async def _check_url_valid(url: str) -> bool:
    """Check if a job posting URL is still active by examining page content."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
            resp = await client.get(url)
            if resp.status_code >= 400:
                return False

            # Check for common "expired" indicators in the final URL
            final_url = str(resp.url).lower()
            url_expired = ["expired", "no-longer", "not-found", "removed", "closed", "404", "error"]
            if any(ind in final_url for ind in url_expired):
                return False

            # Check page content for expired/closed indicators
            text = resp.text.lower()
            expired_phrases = [
                "this job is no longer available",
                "this position has been filled",
                "this job has expired",
                "this listing has expired",
                "job not found",
                "posting has been removed",
                "no longer accepting applications",
                "this job posting is closed",
                "this position is no longer available",
                "sorry, this job has been closed",
                "this requisition has been closed",
                "the job you are looking for is no longer available",
                "this job has been archived",
                "position has been closed",
                "application deadline has passed",
                "this role has been filled",
            ]
            for phrase in expired_phrases:
                if phrase in text:
                    return False

            # Check for minimal content (some pages redirect to generic search)
            # If the page has no job-related content, it's likely dead
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            page_text = soup.get_text(strip=True)
            if len(page_text) < 100:
                return False

            return True
    except Exception:
        return False


async def verify_job_active(db: Session, job: Job) -> dict:
    """Deep-verify a job is still active. Updates url_valid and returns result."""
    if not job.url:
        return {"verified": False, "reason": "No URL"}

    is_valid = await _check_url_valid(job.url)
    job.url_valid = is_valid
    job.url_checked_at = datetime.utcnow()

    if not is_valid:
        # If we can, try to extract WHY it's expired for logging
        job.status = "expired" if job.status == "pending" else job.status
        db.commit()
        return {"verified": False, "reason": "Job posting appears expired or removed", "job_id": job.id}

    # If valid, also try to grab any updated details from the page
    if not job.description or len(job.description) < 200:
        full_desc = await _fetch_full_description(job.url)
        if full_desc:
            job.description = full_desc

    db.commit()
    return {"verified": True, "job_id": job.id}


async def _ai_enrich_job(job: Job, profile: Profile):
    """Use AI to extract structured insights from the job description."""
    desc_excerpt = (job.description or "")[:3000]
    has_desc = bool(desc_excerpt and len(desc_excerpt) > 50)

    prompt = f"""Analyze this job posting and extract structured insights.
{"If the description is missing or very short, use your knowledge of the company and role to infer reasonable details. Clearly base estimates on the job title, company, and location." if not has_desc else ""}

Title: {job.title}
Company: {job.company}
Location: {job.location or 'Not specified'}
Listed salary: {job.salary_text or 'Not listed'}
{"" if not has_desc else f"Description:{chr(10)}{desc_excerpt}"}

Return JSON (use empty string "" instead of null for unknown text fields):
{{
    "seniority_level": "entry|mid|senior|director|vp|c-suite",
    "reports_to": "title of who this role reports to, or empty string if unknown",
    "team_size": "team size if mentioned, or empty string if unknown",
    "remote_type": "remote|hybrid|onsite|unclear",
    "salary_min_estimate": estimated_min_salary_int_or_null,
    "salary_max_estimate": estimated_max_salary_int_or_null,
    "posted_date": "when the job was posted if mentioned (e.g. 'March 1, 2026', '2 weeks ago') or empty string",
    "closing_date": "application deadline/closing date if mentioned (e.g. 'March 29, 2026') or empty string",
    "role_summary": "2-3 sentence plain-language summary of what this job actually involves day-to-day",
    "red_flags": ["any concerns - vague requirements, unrealistic expectations, high turnover signals, missing info"],
    "why_apply": ["compelling reasons to apply based on the posting"],
    "key_requirements": ["top 3-5 actual requirements"]
}}

IMPORTANT:
- Be honest about red flags. If the posting lacks a description, flag that.
- Estimate salary based on title, location, and industry norms if not listed.
- Look carefully for ANY dates - posting date, closing date, application deadline.
- For seniority_level, always provide your best estimate based on the title even if not stated.
- For role_summary, always provide something useful even if you have to infer from the title."""

    data = await ai_generate_json(prompt, max_tokens=800, model_tier="fast")
    if not data:
        return

    # Helper to clean null/None/"null" values to empty string
    def clean(val):
        if val is None or val == "null" or val == "None":
            return ""
        return str(val).strip()

    job.seniority_level = clean(data.get("seniority_level")) or None
    job.reports_to = clean(data.get("reports_to")) or None
    job.team_size = clean(data.get("team_size")) or None
    if data.get("remote_type") and data["remote_type"] != "unclear":
        job.remote_type = data["remote_type"]
    job.role_summary = clean(data.get("role_summary"))
    job.red_flags = json.dumps(data.get("red_flags", []))
    job.why_apply = json.dumps(data.get("why_apply", []))

    # Dates
    posted = clean(data.get("posted_date"))
    if posted and not job.posted_date:
        job.posted_date = posted
    closing = clean(data.get("closing_date"))
    if closing:
        job.closing_date = closing

    # Fill salary if not already present
    if not job.salary_min and data.get("salary_min_estimate"):
        job.salary_min = data["salary_min_estimate"]
        job.salary_max = data.get("salary_max_estimate", data["salary_min_estimate"])
        job.salary_estimated = True
        if not job.salary_text:
            job.salary_text = f"~${job.salary_min:,} - ${job.salary_max:,} (est.)"


# ── Deep Research (Phase 2) ──────────────────────────────────────────────

async def deep_research_job(db: Session, job: Job, profile: Profile, company: Optional[Company] = None):
    """Phase 2: Deep research a job for culture, interview process, growth, hiring sentiment.
    This is meant for shortlisted (liked/high-score) jobs where we want maximum intel."""

    if job.deep_researched:
        return job

    if get_provider() == "none":
        return job

    research_sources = []

    # 1. If we have a URL, try to re-fetch the full page for any additional context
    page_context = ""
    if job.url:
        try:
            full_desc = await _fetch_full_description(job.url)
            if full_desc and len(full_desc) > len(job.description or ""):
                job.description = full_desc
                page_context = full_desc[:3000]
                research_sources.append("Job posting page")
        except Exception:
            pass

    # 2. Try to find Glassdoor reviews/interview data for the company
    glassdoor_context = ""
    if company and company.name:
        try:
            gd_data = await _scrape_glassdoor_interview_data(company.name)
            if gd_data:
                glassdoor_context = gd_data
                research_sources.append("Glassdoor interviews & reviews")
        except Exception as e:
            logger.debug(f"Glassdoor interview scrape failed: {e}")

    # 3. AI deep research using all available context
    company_context = ""
    if company:
        parts = [f"Company: {company.name}"]
        if company.industry: parts.append(f"Industry: {company.industry}")
        if company.size: parts.append(f"Size: {company.size}")
        if company.culture_summary: parts.append(f"Culture: {company.culture_summary}")
        if company.pros: parts.append(f"Pros: {company.pros}")
        if company.cons: parts.append(f"Cons: {company.cons}")
        if company.glassdoor_rating: parts.append(f"Glassdoor: {company.glassdoor_rating}/5")
        company_context = "\n".join(parts)

    desc_excerpt = (job.description or page_context or "")[:4000]

    prompt = f"""You are a career intelligence analyst doing deep research on a specific job opportunity. Provide comprehensive insights that would help a candidate decide whether to apply and prepare for the process.

JOB: {job.title} at {job.company}
Location: {job.location or 'Not specified'}
Seniority: {job.seniority_level or 'Not specified'}
Salary: {job.salary_text or 'Not listed'}

{company_context}

{f"JOB DESCRIPTION:{chr(10)}{desc_excerpt}" if desc_excerpt else "No job description available - infer from title and company."}

{f"GLASSDOOR/INTERVIEW DATA:{chr(10)}{glassdoor_context}" if glassdoor_context else ""}

CANDIDATE PROFILE:
- Name: {profile.name}
- Level: {profile.seniority_level or 'VP/Director'}
- Skills: {profile.skills}

Return JSON:
{{
    "culture_insights": "3-4 sentences about the work culture, team dynamics, and what it's really like working at this company in this type of role. Be specific to the role level and function, not generic.",
    "interview_process": "2-3 sentences describing the likely interview process for this role - number of rounds, types of interviews (behavioral, case study, panel), timeline, and tips.",
    "growth_opportunities": "2-3 sentences about career growth paths from this role - where could this lead in 2-3 years, what skills you'd develop.",
    "day_in_life": "2-3 sentences painting a picture of what a typical week looks like in this role - meetings, deliverables, stakeholder interactions.",
    "hiring_sentiment": "1-2 sentences about the current hiring climate for this type of role at this company or in this sector. Is it competitive? Are they growing?"
}}

IMPORTANT:
- Be specific to THIS role at THIS company, not generic advice
- Draw on your knowledge of the company's reputation, industry norms, and role expectations
- If you have limited data, say so honestly rather than making things up
- For interview_process, think about what's standard at companies of this size/type for this seniority level"""

    data = await ai_generate_json(prompt, max_tokens=2000, model_tier="deep")
    if not data:
        # Retry with balanced model if deep model failed
        data = await ai_generate_json(prompt, max_tokens=2000, model_tier="balanced")
    if data:
        job.culture_insights = data.get("culture_insights", "")
        job.interview_process = data.get("interview_process", "")
        job.growth_opportunities = data.get("growth_opportunities", "")
        job.day_in_life = data.get("day_in_life", "")
        job.hiring_sentiment = data.get("hiring_sentiment", "")
        research_sources.append("AI Analysis")

    job.research_sources = json.dumps(research_sources)
    job.deep_researched = True
    job.deep_research_at = datetime.utcnow()
    db.commit()

    # Re-score with deep research data now incorporated
    from backend.services.scorer import score_and_update_job, score_and_update_job_ai
    try:
        await score_and_update_job_ai(db, job, profile, company)
    except Exception:
        score_and_update_job(db, job, profile, company)

    return job


async def _scrape_glassdoor_interview_data(company_name: str) -> str:
    """Try to scrape Glassdoor interview experience data for a company."""
    try:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", company_name.lower()).strip("-")
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15) as client:
            resp = await client.get(
                "https://www.glassdoor.com/Interview/index.htm",
                params={"sc.keyword": company_name},
            )
            if resp.status_code != 200:
                return ""

            soup = BeautifulSoup(resp.text, "html.parser")
            # Extract interview snippets
            snippets = []
            for el in soup.select("p, span, div.interview-content"):
                text = el.get_text(strip=True)
                if len(text) > 50 and ("interview" in text.lower() or "process" in text.lower()):
                    snippets.append(text[:200])
                if len(snippets) >= 5:
                    break

            return "\n".join(snippets) if snippets else ""
    except Exception:
        return ""


# ── Profile Analysis ─────────────────────────────────────────────────────

async def analyze_profile(db: Session, profile: Profile) -> Profile:
    """Deep-analyze a profile's raw document to extract structured insights."""
    if profile.profile_analyzed:
        return profile

    if not profile.raw_profile_doc and not profile.resume_text:
        return profile

    if get_provider() == "none":
        return profile

    source_text = ""
    if profile.raw_profile_doc:
        source_text += profile.raw_profile_doc[:5000]
    if profile.resume_text:
        source_text += "\n\nRESUME:\n" + profile.resume_text[:3000]

    prompt = f"""You are a career analyst. Deeply analyze this candidate profile and extract BOTH structured data fields AND narrative insights. This candidate could be from ANY industry — tech, construction, healthcare, finance, trades, education, government, etc. Do NOT assume IT/software terminology.

PROFILE DOCUMENT:
{source_text}

Return JSON with two sections — hard data fields AND soft narrative fields:
{{
    "skills": ["list of 10-25 market-standard skills extracted from the document. Include technical skills, certifications, tools, methodologies, and domain expertise relevant to the candidate's ACTUAL industry. Examples for construction: 'Project Scheduling', 'P6 Primavera', 'LEED Certification', 'Blueprint Reading', 'OSHA Compliance'. Examples for healthcare: 'Patient Care', 'EMR Systems', 'HIPAA Compliance'. Examples for IT: 'Python', 'AWS', 'Kubernetes'. Use terms that would appear in real job postings for this person's field."],
    "target_roles": ["list of 3-12 job titles this candidate would target, derived from their most recent 2-3 positions. Generate searchable title variations a recruiter would use. For example if they were a 'Construction Project Manager', generate 'Construction Project Manager', 'Senior Project Manager', 'Project Manager - Construction', 'Construction Manager', etc."],
    "experience_years": <integer total years of professional experience, counted from earliest role to present, or null if cannot determine>,
    "location": "City, State/Province if mentioned in the document, or null",
    "seniority_level": "entry|mid|senior|director|vp|c-suite",
    "profile_summary": "3-4 sentence executive summary of who this candidate is, what they bring, and what they're looking for. Write in third person.",
    "career_trajectory": "2-3 sentence narrative of their career arc - where they've been, where they're heading",
    "leadership_style": "1-2 sentences about their management/leadership approach based on experience",
    "industry_preferences": ["list of industries/sectors they'd thrive in based on their background"],
    "values": ["work values important to them - e.g. innovation, impact, stability, autonomy, collaboration, growth"],
    "deal_breakers": ["things they likely wouldn't accept - infer from preferences and seniority"],
    "strengths": ["top 5-7 key differentiators that set them apart"],
    "growth_areas": ["areas they seem interested in developing or moving into"],
    "ideal_culture": "2-3 sentence description of their ideal work environment"
}}

IMPORTANT:
- SKILLS: Extract skills relevant to the candidate's ACTUAL industry, not just IT skills. Read every bullet point and job description for domain-specific expertise, tools, certifications, and methodologies.
- TARGET_ROLES: Derive from actual job titles held, not generic titles. Generate variations a recruiter would search for.
- EXPERIENCE_YEARS: Calculate from the earliest work date mentioned to today's date.
- LOCATION: Extract from contact info, header, or address section if present.
- Base everything on evidence in the document, but read between the lines
- For values and deal_breakers, infer from their seniority, preferences, and career history
- Be specific about strengths - not generic qualities but actual differentiators
- The ideal_culture should reflect what someone at their level and in their field would value"""

    data = await ai_generate_json(prompt, max_tokens=2000, model_tier="deep")
    if not data:
        return profile

    def clean(val):
        if val is None or val == "null" or val == "None":
            return ""
        return str(val).strip()

    # Only fill fields that are still None/empty — do not overwrite values
    # already populated by the resume parse pipeline

    # ── Hard fields (skills, target_roles, experience_years, location) ──
    # Merge AI-extracted skills with any existing skills (dedup, case-insensitive)
    ai_skills = data.get("skills") or []
    if ai_skills and isinstance(ai_skills, list):
        existing_skills = []
        if profile.skills:
            try:
                existing_skills = json.loads(profile.skills) if isinstance(profile.skills, str) else profile.skills
            except (json.JSONDecodeError, TypeError):
                existing_skills = []
        if not existing_skills or existing_skills in ([], [""], [None]):
            # No existing skills — use AI-extracted skills directly
            profile.skills = json.dumps([s.strip() for s in ai_skills if isinstance(s, str) and s.strip()][:25])
        else:
            # Merge: existing skills take priority, then add new AI skills
            seen_lower = {s.strip().lower() for s in existing_skills if isinstance(s, str)}
            merged = list(existing_skills)
            for skill in ai_skills:
                if isinstance(skill, str) and skill.strip() and skill.strip().lower() not in seen_lower:
                    seen_lower.add(skill.strip().lower())
                    merged.append(skill.strip())
            profile.skills = json.dumps(merged[:25])

    ai_target_roles = data.get("target_roles") or []
    if ai_target_roles and isinstance(ai_target_roles, list):
        existing_roles = []
        if profile.target_roles:
            try:
                existing_roles = json.loads(profile.target_roles) if isinstance(profile.target_roles, str) else profile.target_roles
            except (json.JSONDecodeError, TypeError):
                existing_roles = []
        if not existing_roles or existing_roles in ([], [""], [None]):
            profile.target_roles = json.dumps([r.strip() for r in ai_target_roles if isinstance(r, str) and r.strip()][:12])
        else:
            # Merge: existing roles take priority, then add new AI roles
            seen_lower = {r.strip().lower() for r in existing_roles if isinstance(r, str)}
            merged = list(existing_roles)
            for role in ai_target_roles:
                if isinstance(role, str) and role.strip() and role.strip().lower() not in seen_lower:
                    seen_lower.add(role.strip().lower())
                    merged.append(role.strip())
            profile.target_roles = json.dumps(merged[:12])

    if not profile.experience_years and data.get("experience_years") is not None:
        try:
            profile.experience_years = int(data["experience_years"])
        except (ValueError, TypeError):
            pass

    if not profile.location and data.get("location"):
        loc = clean(data["location"])
        if loc:
            profile.location = loc

    # ── Soft / narrative fields ──
    if not profile.profile_summary:
        profile.profile_summary = clean(data.get("profile_summary"))
    if not profile.career_trajectory:
        profile.career_trajectory = clean(data.get("career_trajectory"))
    if not profile.leadership_style:
        profile.leadership_style = clean(data.get("leadership_style"))
    if not profile.industry_preferences or profile.industry_preferences in ("[]", "null", ""):
        profile.industry_preferences = json.dumps(data.get("industry_preferences", []))
    if not profile.values or profile.values in ("[]", "null", ""):
        profile.values = json.dumps(data.get("values", []))
    if not profile.deal_breakers or profile.deal_breakers in ("[]", "null", ""):
        profile.deal_breakers = json.dumps(data.get("deal_breakers", []))
    if not profile.strengths or profile.strengths in ("[]", "null", ""):
        profile.strengths = json.dumps(data.get("strengths", []))
    if not profile.growth_areas or profile.growth_areas in ("[]", "null", ""):
        profile.growth_areas = json.dumps(data.get("growth_areas", []))
    if not profile.ideal_culture:
        profile.ideal_culture = clean(data.get("ideal_culture"))
    if not profile.seniority_level and data.get("seniority_level"):
        profile.seniority_level = clean(data.get("seniority_level"))
    profile.profile_analyzed = True

    db.commit()
    return profile


def _build_data_sources(c: Company) -> list:
    """Build a list of data source attributions for transparency."""
    sources = []
    if c.glassdoor_rating or c.glassdoor_reviews_count or c.glassdoor_url:
        sources.append({
            "name": "Glassdoor",
            "type": "scraped",
            "fields": [f for f in [
                "rating" if c.glassdoor_rating else None,
                f"{c.glassdoor_reviews_count} reviews" if c.glassdoor_reviews_count else None,
                "CEO approval" if c.ceo_approval else None,
                "recommend %" if c.recommend_pct else None,
            ] if f],
            "url": c.glassdoor_url,
        })
    if c.indeed_rating:
        sources.append({
            "name": "Indeed",
            "type": "scraped",
            "fields": ["rating"],
        })
    # AI-generated fields
    ai_fields = [f for f in [
        "culture summary" if c.culture_summary else None,
        "pros/cons" if c.pros and c.pros != "[]" else None,
        "scorecard" if c.score_overall else None,
        "sentiment" if c.sentiment else None,
        "website" if c.website and not c.glassdoor_url else None,
        "description" if c.description else None,
    ] if f]
    if ai_fields:
        sources.append({
            "name": "AI Analysis",
            "type": "ai",
            "fields": ai_fields,
        })
    return sources


def company_dict(c: Company) -> dict:
    """Serialize a Company to dict for API responses."""
    if not c:
        return None
    return {
        "id": c.id,
        "name": c.name,
        "industry": c.industry,
        "size": c.size,
        "headquarters": c.headquarters,
        "website": c.website,
        "description": c.description,
        "glassdoor_rating": c.glassdoor_rating,
        "glassdoor_reviews_count": c.glassdoor_reviews_count,
        "glassdoor_url": c.glassdoor_url,
        "indeed_rating": c.indeed_rating,
        "culture_summary": c.culture_summary,
        "pros": json.loads(c.pros or "[]"),
        "cons": json.loads(c.cons or "[]"),
        "ceo_approval": c.ceo_approval,
        "recommend_pct": c.recommend_pct,
        "scorecard": {
            "culture": c.score_culture,
            "compensation": c.score_compensation,
            "growth": c.score_growth,
            "wlb": c.score_wlb,
            "leadership": c.score_leadership,
            "diversity": c.score_diversity,
            "overall": c.score_overall,
        },
        "scorecard_summary": c.scorecard_summary,
        "sentiment": json.loads(c.sentiment or "{}") if c.sentiment else None,
        "enriched": c.enriched,
        "data_sources": _build_data_sources(c),
    }
