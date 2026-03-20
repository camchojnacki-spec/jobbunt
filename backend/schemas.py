"""Pydantic schemas for Jobbunt API request/response validation."""

from pydantic import BaseModel, Field
from typing import Any, Optional


class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    target_roles: list[str] = []
    target_locations: list[str] = []
    min_salary: Optional[int] = None
    max_salary: Optional[int] = None
    remote_preference: str = "any"
    experience_years: Optional[int] = None
    skills: list[str] = []
    cover_letter_template: Optional[str] = None
    raw_profile_doc: Optional[str] = None
    search_tiers_down: Optional[int] = 0
    search_tiers_up: Optional[int] = 0
    pin: Optional[str] = None
    seniority_level: Optional[str] = None
    availability: Optional[str] = None
    employment_type: Optional[str] = None
    commute_tolerance: Optional[str] = None
    relocation: Optional[str] = None
    company_size: Optional[str] = None
    industry_preference: Optional[str] = None
    top_priority: Optional[str] = None
    security_clearance: Optional[str] = None
    travel_willingness: Optional[str] = None
    additional_notes: Optional[str] = None
    deal_breakers: Optional[str] = None
    ideal_culture: Optional[str] = None
    values: Optional[str] = None
    strengths: Optional[str] = None
    growth_areas: Optional[str] = None


class ProfileUpdate(BaseModel):
    """All fields optional for partial updates."""
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    target_roles: Optional[list[str]] = None
    target_locations: Optional[list[str]] = None
    min_salary: Optional[int] = None
    max_salary: Optional[int] = None
    remote_preference: Optional[str] = None
    experience_years: Optional[int] = None
    skills: Optional[list[str]] = None
    cover_letter_template: Optional[str] = None
    raw_profile_doc: Optional[str] = None
    search_tiers_down: Optional[int] = None
    search_tiers_up: Optional[int] = None
    pin: Optional[str] = None
    seniority_level: Optional[str] = None
    availability: Optional[str] = None
    employment_type: Optional[str] = None
    commute_tolerance: Optional[str] = None
    relocation: Optional[str] = None
    company_size: Optional[str] = None
    industry_preference: Optional[str] = None
    top_priority: Optional[str] = None
    security_clearance: Optional[str] = None
    travel_willingness: Optional[str] = None
    additional_notes: Optional[str] = None
    deal_breakers: Optional[str] = None
    ideal_culture: Optional[str] = None
    values: Optional[str] = None
    strengths: Optional[str] = None
    growth_areas: Optional[str] = None
    industry_preferences: Optional[Any] = None
    career_history: Optional[Any] = None
    profile_summary: Optional[str] = None
    career_trajectory: Optional[str] = None


class ProfilePasteInput(BaseModel):
    text: str


class SearchRequest(BaseModel):
    query: Optional[str] = None
    sources: Optional[list[str]] = None
    max_results: Optional[int] = Field(50, ge=1, le=500)


class SwipeAction(BaseModel):
    action: str = Field(..., pattern="^(liked|passed|shortlisted|applied)$")


class AnswerSubmit(BaseModel):
    answer: str


class ApplicationUpdate(BaseModel):
    pipeline_status: Optional[str] = None
    notes: Optional[str] = None


class PaginatedResponse(BaseModel):
    total: int
    page: int
    per_page: int
    items: list
