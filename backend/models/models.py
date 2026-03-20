"""SQLAlchemy models for Jobbunt."""
import datetime
from sqlalchemy import Column, Integer, String, Text, Float, DateTime, Boolean, ForeignKey, Index
from sqlalchemy.orm import relationship
from backend.database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    google_id = Column(String(100), unique=True, nullable=True, index=True)
    email = Column(String(200), nullable=False)
    name = Column(String(200))
    picture_url = Column(String(500))
    password_hash = Column(String(200), nullable=True)  # For local auth (bcrypt)
    auth_provider = Column(String(20), default="google")  # "google" or "local"
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_login = Column(DateTime, default=datetime.datetime.utcnow)
    profiles = relationship("Profile", back_populates="user")


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(200), nullable=False)
    pin = Column(String(100), nullable=True)  # Simple PIN for profile switching
    email = Column(String(200))
    phone = Column(String(50))
    location = Column(String(200))
    target_roles = Column(Text)  # JSON list of target role titles
    target_locations = Column(Text)  # JSON list of preferred locations
    min_salary = Column(Integer, nullable=True)
    max_salary = Column(Integer, nullable=True)
    remote_preference = Column(String(50))  # remote, hybrid, onsite, any
    experience_years = Column(Integer)
    skills = Column(Text)  # JSON list of skills
    resume_text = Column(Text)  # parsed resume text
    resume_path = Column(String(500))  # path to uploaded resume file
    cover_letter_template = Column(Text)
    raw_profile_doc = Column(Text)  # full pasted profile document for agent reference
    additional_info = Column(Text)  # JSON dict of extra Q&A answers

    # AI-parsed deep profile insights (from raw_profile_doc)
    profile_summary = Column(Text)  # AI-generated executive summary of the candidate
    career_trajectory = Column(Text)  # career path narrative
    leadership_style = Column(Text)  # leadership/management approach
    industry_preferences = Column(Text)  # JSON list of preferred industries/sectors
    values = Column(Text)  # JSON list of work values (e.g. innovation, stability, impact)
    deal_breakers = Column(Text)  # JSON list of things candidate won't accept
    strengths = Column(Text)  # JSON list of key differentiators
    growth_areas = Column(Text)  # JSON list of areas candidate wants to develop
    ideal_culture = Column(Text)  # description of ideal work culture
    seniority_level = Column(String(100))  # entry, mid, senior, director, vp, c-suite
    availability = Column(String(100))  # how soon looking to start
    employment_type = Column(String(100))  # permanent, contract, consulting
    commute_tolerance = Column(String(100))  # under 30 min, 30-60, over 60, remote only
    relocation = Column(String(100))  # yes/no/within country/for right role
    company_size = Column(String(200))  # preferred company sizes (comma-separated)
    industry_preference = Column(Text)  # preferred industries (comma-separated)
    top_priority = Column(Text)  # what matters most in next role (comma-separated)
    security_clearance = Column(String(100))  # yes/no
    travel_willingness = Column(String(100))  # none, up to 10%, 25%, 50%, extensive
    additional_notes = Column(Text)  # free-text notes for recruiters
    search_tiers_down = Column(Integer, default=0)  # how many seniority tiers below to include in search
    search_tiers_up = Column(Integer, default=0)  # how many seniority tiers above to include in search
    profile_analyzed = Column(Boolean, default=False)  # whether deep analysis has been run
    advisor_data = Column(Text)  # JSON: cached advisor insights (roles_to_consider, keywords, etc.) for scorer
    career_history = Column(Text)  # JSON: structured work history [{company, role, start_year, end_year, years, stat_line}]
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    archived_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="profiles")
    jobs = relationship("Job", back_populates="profile")
    applications = relationship("Application", back_populates="profile")


class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(500), nullable=False)
    name_normalized = Column(String(500), index=True)  # lowercase for dedup

    # Basic info
    industry = Column(String(200))
    size = Column(String(100))  # e.g. "1,000-5,000 employees"
    headquarters = Column(String(300))
    website = Column(String(500))
    description = Column(Text)

    # Ratings & reviews
    glassdoor_rating = Column(Float, nullable=True)  # 0-5
    glassdoor_reviews_count = Column(Integer, nullable=True)
    glassdoor_url = Column(String(500))
    indeed_rating = Column(Float, nullable=True)
    linkedin_url = Column(String(500))

    # AI-generated insights
    culture_summary = Column(Text)  # AI summary of culture/work environment
    pros = Column(Text)  # JSON list of pros from reviews
    cons = Column(Text)  # JSON list of cons from reviews
    ceo_approval = Column(Float, nullable=True)  # percentage
    recommend_pct = Column(Float, nullable=True)  # % who recommend

    # Company Scorecard (0-100 each)
    score_culture = Column(Float, nullable=True)
    score_compensation = Column(Float, nullable=True)
    score_growth = Column(Float, nullable=True)
    score_wlb = Column(Float, nullable=True)  # work-life balance
    score_leadership = Column(Float, nullable=True)
    score_diversity = Column(Float, nullable=True)
    score_overall = Column(Float, nullable=True)
    scorecard_summary = Column(Text)  # AI explanation of scores
    sentiment = Column(Text)  # JSON: {"positive": 0-100, "negative": 0-100, "neutral": 0-100}

    # Meta
    enriched = Column(Boolean, default=False)
    enriched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    jobs = relationship("Job", back_populates="company_rel")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)

    # Deduplication key: normalized (company + title + location) hash
    fingerprint = Column(String(64), nullable=False, index=True)

    # Job details
    title = Column(String(500), nullable=False)
    company = Column(String(500), nullable=False)
    location = Column(String(300))
    salary_min = Column(Integer, nullable=True)
    salary_max = Column(Integer, nullable=True)
    salary_text = Column(String(200))
    job_type = Column(String(100))  # full-time, part-time, contract
    remote_type = Column(String(100))  # remote, hybrid, onsite
    description = Column(Text)
    requirements = Column(Text)
    url = Column(String(1000))
    source = Column(String(100))  # first source found on
    sources_seen = Column(Text)  # JSON list of all sources where this job appeared
    posted_date = Column(String(100))  # when it was posted (from scraping or enrichment)
    closing_date = Column(String(100))  # application deadline if found
    scraped_at = Column(DateTime, default=datetime.datetime.utcnow)  # when we first scraped it

    # Enrichment fields
    seniority_level = Column(String(100))  # entry, mid, senior, director, vp, c-suite
    reports_to = Column(String(200))  # who the role reports to
    team_size = Column(String(100))  # team/org size
    salary_estimated = Column(Boolean, default=False)  # if salary was estimated vs listed
    enriched = Column(Boolean, default=False)
    enriched_at = Column(DateTime, nullable=True)
    url_valid = Column(Boolean, nullable=True)  # None = unchecked, True/False = checked
    url_checked_at = Column(DateTime, nullable=True)

    # AI insights
    role_summary = Column(Text)  # AI-generated brief summary of the role
    red_flags = Column(Text)  # JSON list of concerns
    why_apply = Column(Text)  # JSON list of reasons to apply

    # Deep research (phase 2)
    deep_researched = Column(Boolean, default=False)
    deep_research_at = Column(DateTime, nullable=True)
    culture_insights = Column(Text)  # JSON: deep research findings about culture
    interview_process = Column(Text)  # what the interview process looks like
    growth_opportunities = Column(Text)  # career growth at this company/role
    day_in_life = Column(Text)  # what a typical day looks like
    hiring_sentiment = Column(Text)  # AI analysis of hiring trends/sentiment
    research_sources = Column(Text)  # JSON list of sources used for deep research

    # Scoring
    match_score = Column(Float, default=0.0)  # 0-100 match score
    match_reasons = Column(Text)  # JSON list of match reasons
    match_breakdown = Column(Text)  # JSON dict of dimension scores

    # User notes
    user_notes = Column(Text)  # User's personal notes about this job

    # Swipe status
    status = Column(String(50), default="pending")  # pending, liked, passed, applied, rejected
    swiped_at = Column(DateTime, nullable=True)

    # Reposting detection
    first_seen = Column(DateTime, default=datetime.datetime.utcnow)
    last_seen = Column(DateTime, default=datetime.datetime.utcnow)
    is_repost = Column(Boolean, default=False)
    original_job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    archived_at = Column(DateTime, nullable=True)

    profile = relationship("Profile", back_populates="jobs")
    company_rel = relationship("Company", back_populates="jobs")
    applications = relationship("Application", back_populates="job")
    questions = relationship("AgentQuestion", back_populates="job")

    __table_args__ = (
        Index("ix_jobs_profile_fingerprint", "profile_id", "fingerprint"),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_profile_status_score", "profile_id", "status", "match_score"),
        Index("ix_jobs_created_at", "created_at"),
    )


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False)

    status = Column(String(50), default="queued")  # queued, in_progress, needs_input, completed, failed
    pipeline_status = Column(String(50), default="applied")  # applied, screening, interview, offer, accepted, rejected, no_response
    cover_letter = Column(Text)
    agent_log = Column(Text)  # JSON log of agent actions
    error_message = Column(Text)
    notes = Column(Text)  # User notes about this application
    applied_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    job = relationship("Job", back_populates="applications")
    profile = relationship("Profile", back_populates="applications")
    questions = relationship("AgentQuestion", back_populates="application")


class AgentQuestion(Base):
    __tablename__ = "agent_questions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)

    question = Column(Text, nullable=False)
    context = Column(Text)  # why the agent is asking
    answer = Column(Text, nullable=True)
    is_answered = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    answered_at = Column(DateTime, nullable=True)

    application = relationship("Application", back_populates="questions")
    job = relationship("Job", back_populates="questions")


class ProfileQuestion(Base):
    """LLM-generated interview questions to flesh out the candidate profile."""
    __tablename__ = "profile_questions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False)
    question = Column(Text, nullable=False)
    category = Column(String(100))  # experience, motivation, preferences, culture, leadership, technical
    priority = Column(Integer, default=0)  # higher = more important
    purpose = Column(Text, nullable=True)  # explains WHY this question matters
    answer = Column(Text, nullable=True)
    is_answered = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    answered_at = Column(DateTime, nullable=True)

    profile = relationship("Profile")


class Interview(Base):
    __tablename__ = "interviews"
    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False, index=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False, index=True)
    round_number = Column(Integer, default=1)
    interview_type = Column(String(100))  # phone_screen, technical, behavioral, panel, case_study, final
    scheduled_at = Column(DateTime, nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    interviewer_names = Column(Text)  # JSON list
    prep_notes = Column(Text)  # AI-generated or user-written
    questions_asked = Column(Text)  # JSON list after interview
    outcome = Column(String(50))  # pending, passed, failed, no_decision
    feedback = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class SavedSearch(Base):
    __tablename__ = "saved_searches"
    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False, index=True)
    name = Column(String(200))
    query_config = Column(Text)  # JSON: {roles, locations, sources, filters}
    min_score = Column(Integer, default=70)
    last_run_at = Column(DateTime, nullable=True)
    results_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class FollowUp(Base):
    __tablename__ = "follow_ups"
    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=True, index=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False, index=True)
    follow_up_type = Column(String(50))  # thank_you, status_check, post_interview, networking
    due_date = Column(DateTime, nullable=False)
    completed = Column(Boolean, default=False)
    completed_at = Column(DateTime, nullable=True)
    draft_content = Column(Text)  # AI-generated draft
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True, index=True)
    doc_type = Column(String(50))  # resume, cover_letter, reference_list, tailored_resume
    version = Column(Integer, default=1)
    title = Column(String(200))
    content = Column(Text)
    file_path = Column(String(500))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)
    name = Column(String(200), nullable=False)
    title = Column(String(200))
    email = Column(String(200))
    linkedin_url = Column(String(500))
    phone = Column(String(50))
    relationship_type = Column(String(100))  # recruiter, hiring_manager, referral, networking
    notes = Column(Text)
    last_contacted = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class AICache(Base):
    __tablename__ = "ai_cache"
    id = Column(Integer, primary_key=True, autoincrement=True)
    cache_key = Column(String(64), unique=True, index=True)  # hash of prompt
    response = Column(Text)
    model_tier = Column(String(20))
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    ttl_hours = Column(Integer, default=168)  # 7 days
