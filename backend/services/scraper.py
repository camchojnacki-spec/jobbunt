"""Job scraping service - searches multiple job boards via web scraping."""
import hashlib
import json
import os
import re
import logging
from datetime import datetime
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from backend.models.models import Job, Profile
from backend.utils import safe_json
from backend.constants import SENIORITY_TIERS, TIER_TITLE_VARIANTS

logger = logging.getLogger(__name__)


import asyncio
import random
import time

# ── Rate limiting per domain ──────────────────────────────────────────────
_domain_last_request: dict[str, float] = {}  # domain -> timestamp of last request

async def _rate_limit_delay(domain: str):
    """Add a small random delay (0.3-0.8s) between requests to the same domain."""
    now = time.monotonic()
    last = _domain_last_request.get(domain, 0)
    elapsed = now - last
    min_delay = 0.3 + random.random() * 0.5  # 0.3-0.8s
    if elapsed < min_delay:
        await asyncio.sleep(min_delay - elapsed)
    _domain_last_request[domain] = time.monotonic()


_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0",
]

def _get_headers():
    """Get browser-like headers with rotated User-Agent."""
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }

# ── Per-domain rate limit configuration ───────────────────────────────────
# Sites with heavy anti-scraping need longer delays
_DOMAIN_RATE_LIMITS: dict[str, tuple[float, float]] = {
    # domain -> (min_delay, max_delay)
    "www.indeed.com": (2.5, 5.0),
    "ca.indeed.com": (2.5, 5.0),
    "www.linkedin.com": (1.0, 2.5),
    "www.glassdoor.com": (1.0, 2.5),
}

async def _rate_limit_delay_for_domain(domain: str):
    """Like _rate_limit_delay but with per-domain rate configs for aggressive sites."""
    min_d, max_d = _DOMAIN_RATE_LIMITS.get(domain, (0.3, 0.8))
    now = time.monotonic()
    last = _domain_last_request.get(domain, 0)
    elapsed = now - last
    target_delay = min_d + random.random() * (max_d - min_d)
    if elapsed < target_delay:
        await asyncio.sleep(target_delay - elapsed)
    _domain_last_request[domain] = time.monotonic()


# ── Shared connection pool ────────────────────────────────────────────────
_shared_client: httpx.AsyncClient | None = None

def _get_shared_client() -> httpx.AsyncClient:
    """Get or create a shared httpx.AsyncClient with connection pooling."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=25,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _shared_client


def _make_fresh_client(timeout: int = 25) -> httpx.AsyncClient:
    """Create a fresh httpx client with rotated headers and cookie support.

    Used for sites with strong anti-scraping (Indeed, Glassdoor) where
    the shared client's stale cookies/headers cause blocks.
    """
    return httpx.AsyncClient(
        headers=_get_headers(),
        follow_redirects=True,
        timeout=timeout,
        cookies=httpx.Cookies(),
    )


# Keep static HEADERS for backward compat in fetch_job_details etc.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Source health tracking ────────────────────────────────────────────────
_source_health: dict[str, dict] = {}  # source_key -> {"successes": int, "failures": int, "last_failure": float, "last_error": str}

def _record_source_success(source_key: str, result_count: int):
    """Record a successful scrape for a source."""
    if source_key not in _source_health:
        _source_health[source_key] = {"successes": 0, "failures": 0, "last_failure": 0, "last_error": ""}
    _source_health[source_key]["successes"] += 1
    logger.info(f"[{source_key}] SUCCESS: {result_count} results (lifetime: {_source_health[source_key]['successes']}S/{_source_health[source_key]['failures']}F)")

def _record_source_failure(source_key: str, error: str):
    """Record a failed scrape for a source."""
    if source_key not in _source_health:
        _source_health[source_key] = {"successes": 0, "failures": 0, "last_failure": 0, "last_error": ""}
    _source_health[source_key]["failures"] += 1
    _source_health[source_key]["last_failure"] = time.monotonic()
    _source_health[source_key]["last_error"] = error[:200]
    logger.warning(f"[{source_key}] FAILURE: {error[:100]} (lifetime: {_source_health[source_key]['successes']}S/{_source_health[source_key]['failures']}F)")

def _is_source_healthy(source_key: str) -> bool:
    """Check if a source should be attempted based on recent failure history.

    If a source has failed 5+ times in a row with no successes in between,
    and the last failure was within 5 minutes, skip it temporarily.
    """
    health = _source_health.get(source_key)
    if not health:
        return True
    recent_failure = (time.monotonic() - health["last_failure"]) < 300  # 5 min
    consecutive_fail_ratio = health["failures"] > 5 and health["successes"] == 0
    if consecutive_fail_ratio and recent_failure:
        logger.warning(f"[{source_key}] Temporarily skipped: {health['failures']} consecutive failures, last: {health['last_error'][:60]}")
        return False
    return True

def get_source_health() -> dict:
    """Return current source health stats (for debugging/monitoring)."""
    return dict(_source_health)


async def _retry_with_backoff(coro_factory, max_retries: int = 2, base_delay: float = 1.0):
    """Execute an async callable with exponential backoff retries.

    Args:
        coro_factory: A callable (no args) that returns a coroutine to await.
        max_retries: Number of retries after the first attempt.
        base_delay: Base delay in seconds (doubled each retry).

    Returns:
        The result of the coroutine, or raises the last exception.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt) + random.random()
                logger.debug(f"Retry {attempt + 1}/{max_retries} after {delay:.1f}s: {e}")
                await asyncio.sleep(delay)
    raise last_exc


def _normalize_location(loc: str) -> str:
    """Aggressively normalize location for dedup - strip state/province/country variations."""
    loc = loc.lower().strip()
    # Remove country suffixes
    for suffix in [", canada", ", ca", ", united states", ", usa", ", us"]:
        if loc.endswith(suffix):
            loc = loc[:-len(suffix)].strip()
    # Normalize province/state abbreviations
    province_map = {
        "ontario": "on", "british columbia": "bc", "alberta": "ab",
        "quebec": "qc", "manitoba": "mb", "saskatchewan": "sk",
        "nova scotia": "ns", "new brunswick": "nb",
    }
    for full, abbr in province_map.items():
        loc = loc.replace(f", {full}", f", {abbr}")
    # Strip extra whitespace and punctuation
    loc = re.sub(r"\s+", " ", loc).strip(", ")
    return loc


def normalize_title(title: str) -> str:
    """Normalize a job title for semantic dedup comparison.

    Strips seniority prefixes, level indicators, common fluff words,
    special characters, and collapses whitespace. Returns lowercase.
    """
    t = title.lower().strip()
    # Remove seniority / level prefixes
    seniority_prefixes = [
        r"senior\b", r"sr\.?\b", r"junior\b", r"jr\.?\b",
        r"lead\b", r"principal\b", r"staff\b", r"distinguished\b",
        r"entry[\s-]?level\b", r"mid[\s-]?level\b", r"intermediate\b",
        r"associate\b",
    ]
    for pfx in seniority_prefixes:
        t = re.sub(rf"^\s*{pfx}\s*", "", t)
        t = re.sub(rf"\s*,?\s*{pfx}\s*$", "", t)
    # Remove level indicators like "I", "II", "III", "IV", "V" at end
    t = re.sub(r"\s+(i{1,3}|iv|v)\s*$", "", t)
    # Remove level numbers at end like "1", "2", "3"
    t = re.sub(r"\s+[1-5]\s*$", "", t)
    # Remove parenthetical qualifiers like "(Remote)", "(Contract)", "(Canada)"
    t = re.sub(r"\s*\([^)]*\)\s*", " ", t)
    # Remove bracket qualifiers like "[Full-time]"
    t = re.sub(r"\s*\[[^\]]*\]\s*", " ", t)
    # Remove special characters, keep only alphanumeric and spaces
    t = re.sub(r"[^a-z0-9 ]", "", t)
    # Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t


def normalize_company(company: str) -> str:
    """Normalize a company name for semantic dedup comparison.

    Strips corporate suffixes (Inc., Ltd., Corp., etc.), special characters,
    and collapses whitespace. Returns lowercase.
    """
    c = company.lower().strip()
    # Remove common corporate suffixes (with or without period)
    suffixes = [
        r"incorporated", r"inc\.?", r"limited", r"ltd\.?",
        r"llc\.?", r"l\.l\.c\.?", r"llp\.?", r"l\.l\.p\.?",
        r"corporation", r"corp\.?", r"company", r"co\.?",
        r"group", r"holdings?", r"enterprises?",
        r"international", r"intl\.?",
        r"solutions?", r"services?", r"technologies", r"technology",
        r"tech\.?", r"systems?",
        r"plc\.?", r"gmbh", r"s\.?a\.?", r"pty\.?",
    ]
    for sfx in suffixes:
        c = re.sub(rf",?\s*\b{sfx}\s*$", "", c)
    # Remove "The " prefix
    c = re.sub(r"^the\s+", "", c)
    # Remove special characters, keep only alphanumeric and spaces
    c = re.sub(r"[^a-z0-9 ]", "", c)
    # Collapse whitespace
    c = re.sub(r"\s+", " ", c).strip()
    return c


def make_fingerprint(title: str, company: str, location: str = "") -> str:
    """Create a dedup fingerprint from job details.

    Uses only title + company for the hash to avoid location-text variations
    causing duplicates. Location is normalized but given low weight.
    """
    title_norm = re.sub(r"[^a-z0-9 ]", "", title.lower().strip())
    title_norm = re.sub(r"\s+", " ", title_norm)
    company_norm = re.sub(r"[^a-z0-9 ]", "", company.lower().strip())
    company_norm = re.sub(r"\s+", " ", company_norm)
    # Use city-only from location (first part before comma) for fingerprint
    loc_norm = _normalize_location(location).split(",")[0].strip()
    normalized = f"{title_norm}|{company_norm}|{loc_norm}"
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def job_exists(db: Session, profile_id: int, fingerprint: str) -> Optional[Job]:
    """Check if a job with this fingerprint already exists for this profile."""
    return db.query(Job).filter(
        Job.profile_id == profile_id,
        Job.fingerprint == fingerprint,
    ).first()


async def search_indeed(query: str, location: str, limit: int = 25) -> list[dict]:
    """Scrape Indeed search results — tries multiple approaches for reliability.

    Approach order:
    1. Indeed RSS feed with ca.indeed.com priority for Canadian searches
    2. Direct HTML scrape with fresh client + cookies
    3. Embedded JSON extraction from mosaic provider data

    Note: SerpAPI-based Indeed results come via the "serpapi" source (Google Jobs
    aggregator) which is handled separately to avoid double-dipping on quota.
    """
    # SerpAPI Indeed delegation removed — SerpAPI Google Jobs (which aggregates
    # Indeed listings) is handled by the "serpapi" source in AVAILABLE_SOURCES.
    # Calling it here too would double-dip on quota when both sources are active.

    jobs = []
    # Detect which Indeed domain to try
    loc_lower = location.lower()
    domains = []
    if any(s in loc_lower for s in ["canada", "ontario", "toronto", "vancouver", "ottawa", "calgary", "montreal", ", on", ", bc", ", ab"]):
        domains = ["ca.indeed.com", "www.indeed.com"]
    elif any(s in loc_lower for s in ["united states", "usa", ", us", "new york", "california"]):
        domains = ["www.indeed.com"]
    else:
        domains = ["www.indeed.com", "ca.indeed.com"]

    for domain in domains:
        if jobs:
            break

        # ── Method 1: RSS feed (most reliable, bypasses anti-scraping) ──
        try:
            rss_jobs = await _indeed_rss(domain, query, location, limit)
            if rss_jobs:
                jobs = rss_jobs
                logger.info(f"Indeed ({domain}) RSS: {len(jobs)} results")
                break
        except Exception as e:
            logger.debug(f"Indeed ({domain}) RSS failed: {e}")

        # ── Method 2: Direct scrape with fresh client ──
        try:
            await _rate_limit_delay_for_domain(domain)
            async with _make_fresh_client(timeout=25) as client:
                # First hit the Indeed homepage to collect cookies
                try:
                    await client.get(f"https://{domain}/", headers=_get_headers())
                    await asyncio.sleep(0.5 + random.random() * 0.5)
                except Exception:
                    pass  # Cookie prefetch is best-effort

                params = {"q": query, "l": location, "limit": str(limit), "fromage": "14"}
                headers = _get_headers()
                headers["Referer"] = f"https://{domain}/"

                resp = await client.get(f"https://{domain}/jobs", params=params, headers=headers)
                if resp.status_code == 403:
                    logger.warning(f"Indeed ({domain}) returned 403 (blocked), trying next approach")
                    continue
                if resp.status_code != 200:
                    logger.warning(f"Indeed ({domain}) returned status {resp.status_code}")
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")

                # Try to extract from embedded JSON (mosaic provider data)
                for script in soup.select("script[type='text/javascript']"):
                    text = script.string or ""
                    if "window.mosaic.providerData" in text or "jobKeysWithInfo" in text:
                        try:
                            matches = re.findall(r'"jk"\s*:\s*"([a-f0-9]+)"', text)
                            title_matches = re.findall(r'"title"\s*:\s*"([^"]+)"', text)
                            company_matches = re.findall(r'"company"\s*:\s*"([^"]+)"', text)
                            for idx in range(min(len(matches), len(title_matches), limit)):
                                jk = matches[idx] if idx < len(matches) else ""
                                jobs.append({
                                    "title": title_matches[idx] if idx < len(title_matches) else "",
                                    "company": company_matches[idx] if idx < len(company_matches) else "Unknown",
                                    "location": location,
                                    "description": "",
                                    "url": f"https://{domain}/viewjob?jk={jk}" if jk else "",
                                    "source": "indeed",
                                })
                            if jobs:
                                break
                        except Exception:
                            pass

                if not jobs:
                    # Fallback to HTML parsing
                    cards = soup.select("div.job_seen_beacon, div.jobsearch-ResultsList > div, div.result, div.slider_item, div.cardOutline")
                    for card in cards[:limit]:
                        title_el = card.select_one("h2.jobTitle a, h2 a, a.jcs-JobTitle, a[data-jk]")
                        company_el = card.select_one("span[data-testid='company-name'], span.companyName, span.company, [data-testid='company-name']")
                        location_el = card.select_one("div[data-testid='text-location'], div.companyLocation, span.location, [data-testid='text-location']")
                        snippet_el = card.select_one("div.job-snippet, td.snip, div[class*='job-snippet']")

                        if not title_el:
                            continue

                        href = title_el.get("href", "")
                        # Also try data-jk attribute for job key
                        jk = title_el.get("data-jk", "") or card.get("data-jk", "")
                        if jk and not href:
                            href = f"https://{domain}/viewjob?jk={jk}"
                        elif href and not href.startswith("http"):
                            href = f"https://{domain}{href}"

                        jobs.append({
                            "title": title_el.get_text(strip=True),
                            "company": company_el.get_text(strip=True) if company_el else "Unknown",
                            "location": location_el.get_text(strip=True) if location_el else "",
                            "description": snippet_el.get_text(strip=True) if snippet_el else "",
                            "url": href,
                            "source": "indeed",
                        })
        except Exception as e:
            logger.error(f"Indeed ({domain}) scrape failed: {e}")
    return jobs


async def _indeed_rss(domain: str, query: str, location: str, limit: int) -> list[dict]:
    """Fetch Indeed results via RSS feed — more reliable than HTML scraping.

    Indeed provides RSS feeds at /rss endpoint that are less likely to be blocked.
    Strips negative boolean operators from the query since RSS doesn't support them.
    """
    jobs = []
    # Strip negative keyword operators — RSS feeds don't support boolean -"keyword"
    clean_query = re.sub(r'\s*-"[^"]*"', '', query).strip()
    params = {"q": clean_query, "l": location, "limit": str(min(limit, 25)), "fromage": "14", "sort": "date"}
    # Use realistic RSS reader headers (not browser headers) — RSS endpoints
    # are less suspicious when the request looks like an actual RSS reader
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Feedfetcher-Google; +http://www.google.com/feedfetcher.html)",
        "Accept": "application/rss+xml, application/xml, text/xml, application/atom+xml, */*;q=0.1",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }

    # Try multiple RSS URL patterns — path-based URL is less likely to be blocked
    # e.g., ca.indeed.com/rss/q-QUERY-l-LOCATION-jobs
    encoded_q = clean_query.replace(" ", "+")
    encoded_l = location.replace(" ", "+").replace(",", "%2C")
    rss_paths = [
        (f"https://{domain}/rss/q-{encoded_q}-l-{encoded_l}-jobs", {}),
        (f"https://{domain}/rss", params),
        (f"https://{domain}/jobs/rss", params),
    ]

    for rss_url, rss_params in rss_paths:
        if jobs:
            break
        await _rate_limit_delay_for_domain(domain)
        # Use a longer delay for Indeed RSS to avoid 429
        await asyncio.sleep(1.0 + random.random() * 1.5)
        resp_text = None
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
                resp = await client.get(rss_url, params=rss_params, headers=headers)
                if resp.status_code == 429:
                    logger.warning(f"Indeed RSS ({rss_url}) returned 429 — rate limited, trying next pattern")
                    await asyncio.sleep(3.0 + random.random() * 2.0)
                    continue
                if resp.status_code != 200:
                    logger.debug(f"Indeed RSS ({rss_url}) returned {resp.status_code}")
                    continue
                resp_text = resp.text
        except Exception as e:
            logger.debug(f"Indeed RSS ({rss_url}) request failed: {e}")
            continue

        if not resp_text:
            continue

        soup = BeautifulSoup(resp_text, "xml")
        items = soup.find_all("item")

        for item in items[:limit]:
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            source_el = item.find("source")

            if not title_el:
                continue

            # Parse company from source element or title
            company = "Unknown"
            if source_el:
                company = source_el.get_text(strip=True)

            # Clean HTML from description
            desc_text = ""
            if desc_el:
                desc_soup = BeautifulSoup(desc_el.get_text(), "html.parser")
                desc_text = desc_soup.get_text(strip=True)[:500]

            url = link_el.get_text(strip=True) if link_el else ""
            # Indeed RSS sometimes wraps URLs; also check next sibling
            if not url and link_el and link_el.next_sibling:
                url = str(link_el.next_sibling).strip()
            # B16 fix: normalize French Canadian Indeed URLs to English
            if url:
                url = re.sub(r'emplois\.ca\.indeed\.com', 'ca.indeed.com', url)
                url = re.sub(r'emplois\.indeed\.com', 'www.indeed.com', url)

            jobs.append({
                "title": title_el.get_text(strip=True),
                "company": company,
                "location": location,
                "description": desc_text,
                "url": url,
                "source": "indeed",
            })

    return jobs


async def search_linkedin_jobs(query: str, location: str, limit: int = 25) -> list[dict]:
    """Scrape LinkedIn public job search (no auth required).

    Uses LinkedIn's public guest API which returns server-rendered HTML.
    Uses a fresh client per request to avoid cookie/session staleness.
    """
    jobs = []
    # Use past 30 days for executive/senior searches, past week for others
    # CISO/VP/Director roles are rare — weekly filter misses most postings
    query_lower = query.lower()
    is_executive = any(kw in query_lower for kw in [
        "ciso", "cio", "cto", "chief", "vp ", "vice president",
        "svp", "avp", "director", "head of"
    ])
    time_filter = "r2592000" if is_executive else "r604800"  # 30 days vs 7 days
    params = {"keywords": query, "location": location, "trk": "public_jobs_jobs-search-bar_search-submit",
              "position": "1", "pageNum": "0", "f_TPR": time_filter}
    # For executive roles, add seniority filter for Director+ level
    if is_executive:
        params["f_E"] = "5,6"  # 5=Director, 6=Executive
    try:
        await _rate_limit_delay_for_domain("www.linkedin.com")
        async with _make_fresh_client(timeout=25) as client:
            headers = _get_headers()
            headers["Referer"] = "https://www.linkedin.com/jobs/search/"

            resp = await client.get(
                "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                params=params, headers=headers,
            )
            if resp.status_code == 200 and resp.text.strip():
                soup = BeautifulSoup(resp.text, "html.parser")
                cards = soup.select("li")
                for card in cards[:limit]:
                    title_el = card.select_one("h3.base-search-card__title, h3")
                    company_el = card.select_one("h4.base-search-card__subtitle, a.hidden-nested-link")
                    location_el = card.select_one("span.job-search-card__location")
                    link_el = card.select_one("a.base-card__full-link, a[href*='/jobs/']")
                    if not title_el:
                        continue
                    url = link_el.get("href", "").split("?")[0] if link_el else ""
                    jobs.append({
                        "title": title_el.get_text(strip=True),
                        "company": company_el.get_text(strip=True) if company_el else "Unknown",
                        "location": location_el.get_text(strip=True) if location_el else "",
                        "description": "",
                        "url": url,
                        "source": "linkedin",
                    })
                if jobs:
                    return jobs

            # Fallback: standard page
            await _rate_limit_delay_for_domain("www.linkedin.com")
            headers = _get_headers()
            resp = await client.get("https://www.linkedin.com/jobs/search/", params=params, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"LinkedIn returned status {resp.status_code}")
                return jobs

            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.select("div.base-card, li.result-card, div.job-search-card")

            for card in cards[:limit]:
                title_el = card.select_one("h3.base-search-card__title, h3, span.sr-only")
                company_el = card.select_one("h4.base-search-card__subtitle, a.hidden-nested-link")
                location_el = card.select_one("span.job-search-card__location")
                link_el = card.select_one("a.base-card__full-link, a")
                if not title_el:
                    continue
                url = link_el.get("href", "").split("?")[0] if link_el else ""
                jobs.append({
                    "title": title_el.get_text(strip=True),
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": location_el.get_text(strip=True) if location_el else "",
                    "description": "",
                    "url": url,
                    "source": "linkedin",
                })
    except Exception as e:
        logger.error(f"LinkedIn scrape failed: {e}")
    return jobs


async def search_glassdoor(query: str, location: str, limit: int = 25) -> list[dict]:
    """Glassdoor search — SKIPPED (direct scraping blocked).

    Glassdoor has aggressive anti-bot protection (Cloudflare + fingerprinting)
    that blocks all direct scraping attempts with 403 Forbidden. Their public
    pages require JavaScript rendering which httpx cannot handle.

    Glassdoor results are already covered by the 'serpapi' source (Google Jobs
    aggregator) — do NOT add SerpAPI calls here to avoid double-dipping on quota.
    Browser fallback via Playwright may also surface Glassdoor listings.
    """
    logger.info(
        "[glassdoor] SKIPPED: direct scraping blocked (403 Forbidden / Cloudflare). "
        "Glassdoor results are covered by the serpapi source (Google Jobs aggregator)."
    )
    return []


async def search_gcjobs(query: str, location: str, limit: int = 25) -> list[dict]:
    """Search Government of Canada jobs via GC Jobs portal."""
    jobs = []
    try:
        await _rate_limit_delay("emplois-jobs.gc.ca")
        client = _get_shared_client()
        # GC Jobs search endpoint
        resp = await client.get(
            "https://emplois-jobs.gc.ca/gcjobs/fp-pf/resultatrecherche-searchresult.htm",
            params={
                "kw": query,
                "clty": location.split(",")[0].strip() if location else "",
                "tmpl": "search",
                "action": "doSearch",
                "ln": "en",
                "mrp": str(limit),
            },
        )
        if resp.status_code != 200:
            logger.warning(f"GC Jobs returned status {resp.status_code}, trying fallback")
            return await _gcjobs_google_fallback(query, location, limit)

        soup = BeautifulSoup(resp.text, "html.parser")
        # GC Jobs uses table rows or div cards for results
        rows = soup.select("tr.resultRow, div.searchResult, div[class*='result']")

        for row in rows[:limit]:
            title_el = row.select_one("a[id*='title'], a[class*='title'], td a, h3 a, a")
            dept_el = row.select_one("td:nth-of-type(2), span[class*='dept'], div[class*='org']")
            loc_el = row.select_one("td:nth-of-type(3), span[class*='loc'], div[class*='location']")
            date_el = row.select_one("td:nth-of-type(4), span[class*='date'], div[class*='date']")

            if not title_el:
                continue

            href = title_el.get("href", "")
            if href and not href.startswith("http"):
                href = f"https://emplois-jobs.gc.ca{href}"

            jobs.append({
                "title": title_el.get_text(strip=True),
                "company": dept_el.get_text(strip=True) if dept_el else "Government of Canada",
                "location": loc_el.get_text(strip=True) if loc_el else "",
                "description": "",
                "url": href,
                "source": "gcjobs",
                "closing_date": date_el.get_text(strip=True) if date_el else "",
            })

        if not jobs:
            logger.info("GC Jobs scrape returned no results, trying Google fallback")
            return await _gcjobs_google_fallback(query, location, limit)

    except Exception as e:
        logger.error(f"GC Jobs scrape failed: {e}, trying Google fallback")
        return await _gcjobs_google_fallback(query, location, limit)
    return jobs


async def _gcjobs_google_fallback(query: str, location: str, limit: int = 10) -> list[dict]:
    """Fallback: search Google for GC Jobs postings."""
    return await _google_site_search("canada.ca/en/services/jobs", query, location, limit, "gcjobs")


_google_blocked_until = 0.0  # Timestamp — if Google 429s, skip for 5 minutes

async def _google_site_search(site: str, query: str, location: str, limit: int, source: str) -> list[dict]:
    """Search Google with site: operator as a fallback for blocked scrapers.

    Tries DuckDuckGo as secondary fallback if Google blocks.
    Tracks Google 429 responses to avoid hammering when rate-limited.
    """
    global _google_blocked_until
    import time as _time
    jobs = []
    # Strip negative keywords from fallback queries — they make URLs too long
    # and Google doesn't always handle them well
    clean_query = re.sub(r'\s*-"[^"]*"', '', query).strip()
    search_query = f"site:{site} {clean_query} {location} jobs".strip()

    # Skip Google if recently rate-limited (429)
    if _time.time() < _google_blocked_until:
        logger.info(f"Google blocked (cooling down) — skipping site:{site} for [{source}], trying DDG directly")
        return await _ddg_site_search(site, clean_query, location, limit, source)

    # Try Google first
    try:
        await _rate_limit_delay("www.google.com")
        client = _get_shared_client()
        resp = await client.get(
            "https://www.google.com/search",
            params={"q": search_query, "num": str(min(limit, 20))},
        )
        if resp.status_code in (429, 302) and "sorry" in resp.text.lower():
            # Google is rate-limiting — mark blocked for 5 minutes
            _google_blocked_until = _time.time() + 300
            logger.warning(f"Google rate-limited (429/captcha) — blocking Google fallbacks for 5 min")
        elif resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for result in soup.select("div.g, div[data-sokoban-container]")[:limit]:
                title_el = result.select_one("h3")
                link_el = result.select_one("a")
                snippet_el = result.select_one("div.VwiC3b, span.aCOpRe, div[style*='line-clamp']")
                if not title_el or not link_el:
                    continue
                url = link_el.get("href", "")
                title_text = title_el.get_text(strip=True)
                parts = title_text.rsplit(" - ", 1)
                title = parts[0].strip()
                company = parts[1].strip() if len(parts) > 1 else "Unknown"
                if any(skip in url.lower() for skip in ["/about", "/help", "/faq", "/blog", "/salary"]):
                    continue
                jobs.append({
                    "title": title, "company": company, "location": location,
                    "description": snippet_el.get_text(strip=True) if snippet_el else "",
                    "url": url, "source": source,
                })
            if jobs:
                return jobs
    except Exception as e:
        logger.warning(f"Google site search failed for {site}: {e}")

    # Bing fallback (less aggressive anti-bot than Google)
    bing_results = await _bing_site_search(site, clean_query, location, limit, source)
    if bing_results:
        return bing_results

    # DuckDuckGo fallback
    return await _ddg_site_search(site, clean_query, location, limit, source)


async def _ddg_site_search(site: str, query: str, location: str, limit: int, source: str) -> list[dict]:
    """DuckDuckGo site: search fallback — no anti-bot blocking."""
    jobs = []
    search_query = f"site:{site} {query} {location} jobs".strip()
    try:
        await _rate_limit_delay("html.duckduckgo.com")
        async with _make_fresh_client(timeout=15) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": search_query},
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for result in soup.select("div.result, div.web-result")[:limit]:
                    title_el = result.select_one("a.result__a, h2 a")
                    snippet_el = result.select_one("a.result__snippet, div.result__snippet")
                    if not title_el:
                        continue
                    url = title_el.get("href", "")
                    title_text = title_el.get_text(strip=True)
                    parts = title_text.rsplit(" - ", 1)
                    title = parts[0].strip()
                    company = parts[1].strip() if len(parts) > 1 else "Unknown"
                    if any(skip in url.lower() for skip in ["/about", "/help", "/faq", "/blog", "/salary"]):
                        continue
                    jobs.append({
                        "title": title, "company": company, "location": location,
                        "description": snippet_el.get_text(strip=True) if snippet_el else "",
                        "url": url, "source": source,
                    })
    except Exception as e:
        logger.warning(f"DuckDuckGo fallback also failed for {site}: {e}")

    return jobs


async def _bing_site_search(site: str, query: str, location: str, limit: int, source: str) -> list[dict]:
    """Bing site: search — less aggressive anti-bot than Google."""
    jobs = []
    search_query = f"site:{site} {query} {location} jobs"
    try:
        await _rate_limit_delay("www.bing.com")
        async with _make_fresh_client(timeout=15) as client:
            resp = await client.get(
                "https://www.bing.com/search",
                params={"q": search_query, "count": str(min(limit, 20))},
                headers={**_get_headers(), "Referer": "https://www.bing.com/"},
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for result in soup.select("li.b_algo, div.b_algo")[:limit]:
                    title_el = result.select_one("h2 a, h2")
                    link_el = result.select_one("h2 a, a")
                    snippet_el = result.select_one("p, div.b_caption p")
                    if not title_el or not link_el:
                        continue
                    url = link_el.get("href", "")
                    if not url or "bing.com" in url:
                        continue
                    title_text = title_el.get_text(strip=True)
                    parts = title_text.rsplit(" - ", 1)
                    title = parts[0].strip()
                    company = parts[1].strip() if len(parts) > 1 else "Unknown"
                    if any(skip in url.lower() for skip in ["/about", "/help", "/faq", "/blog", "/salary"]):
                        continue
                    jobs.append({
                        "title": title, "company": company, "location": location,
                        "description": snippet_el.get_text(strip=True) if snippet_el else "",
                        "url": url, "source": source,
                    })
                if jobs:
                    logger.info(f"Bing site:{site} returned {len(jobs)} results for [{source}]")
            else:
                logger.warning(f"Bing returned status {resp.status_code}")
    except Exception as e:
        logger.warning(f"Bing site search failed for {site}: {e}")
    return jobs


async def search_usajobs(query: str, location: str, limit: int = 25) -> list[dict]:
    """Search USAJobs API (public, no auth required for basic search)."""
    jobs = []
    try:
        api_headers = {
            **HEADERS,
            "Host": "data.usajobs.gov",
            "User-Agent": "jobbunt-app/1.0",
        }
        async with httpx.AsyncClient(headers=api_headers, follow_redirects=True, timeout=30) as client:
            resp = await client.get(
                "https://data.usajobs.gov/api/Search",
                params={"Keyword": query, "LocationName": location, "ResultsPerPage": str(limit)},
            )
            if resp.status_code != 200:
                return jobs

            data = resp.json()
            for item in data.get("SearchResult", {}).get("SearchResultItems", [])[:limit]:
                pos = item.get("MatchedObjectDescriptor", {})
                locs = pos.get("PositionLocation", [{}])
                loc_str = locs[0].get("LocationName", "") if locs else ""
                salary = pos.get("PositionRemuneration", [{}])
                sal_str = ""
                if salary:
                    sal_str = f"${salary[0].get('MinimumRange', '')}-${salary[0].get('MaximumRange', '')}"

                jobs.append({
                    "title": pos.get("PositionTitle", ""),
                    "company": pos.get("OrganizationName", "US Government"),
                    "location": loc_str,
                    "description": pos.get("QualificationSummary", ""),
                    "url": pos.get("PositionURI", ""),
                    "source": "usajobs",
                    "salary_text": sal_str,
                })
    except Exception as e:
        logger.error(f"USAJobs search failed: {e}")
    return jobs


async def search_adzuna(query: str, location: str, limit: int = 25) -> list[dict]:
    """Search Adzuna API (free tier, covers US/CA/UK/AU and more)."""
    jobs = []
    adzuna_cfg = _get_source_config().get("adzuna", {})
    api_key = os.environ.get("ADZUNA_API_KEY") or adzuna_cfg.get("api_key", "")
    app_id = os.environ.get("ADZUNA_APP_ID") or adzuna_cfg.get("app_id", "")
    if not api_key or not app_id:
        logger.debug("Adzuna API key not configured, skipping")
        return jobs

    # Detect country from location
    loc_lower = location.lower()
    country = "ca"  # default to Canada
    if any(s in loc_lower for s in ["united states", "usa", ", us", "new york", "california"]):
        country = "us"
    elif any(s in loc_lower for s in ["united kingdom", "london", "uk"]):
        country = "gb"

    try:
        async with httpx.AsyncClient(headers=_get_headers(), follow_redirects=True, timeout=20) as client:
            resp = await client.get(
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                params={
                    "app_id": app_id,
                    "app_key": api_key,
                    "what": query,
                    "where": location.split(",")[0].strip(),
                    "results_per_page": str(min(limit, 50)),
                    "sort_by": "relevance",
                },
            )
            if resp.status_code != 200:
                logger.warning(f"Adzuna returned status {resp.status_code}")
                return jobs

            data = resp.json()
            for item in data.get("results", [])[:limit]:
                sal_min = item.get("salary_min")
                sal_max = item.get("salary_max")
                sal_str = ""
                if sal_min and sal_max:
                    sal_str = f"${int(sal_min):,} - ${int(sal_max):,}"
                elif sal_min:
                    sal_str = f"${int(sal_min):,}+"

                jobs.append({
                    "title": item.get("title", ""),
                    "company": item.get("company", {}).get("display_name", "Unknown"),
                    "location": item.get("location", {}).get("display_name", location),
                    "description": item.get("description", "")[:2000],
                    "url": item.get("redirect_url", ""),
                    "source": "adzuna",
                    "salary_text": sal_str,
                })
    except Exception as e:
        logger.error(f"Adzuna search failed: {e}")
    return jobs


async def search_jobbank(query: str, location: str, limit: int = 25) -> list[dict]:
    """Search Job Bank Canada (jobbank.gc.ca) — public government job board."""
    jobs = []
    try:
        async with httpx.AsyncClient(headers=_get_headers(), follow_redirects=True, timeout=20) as client:
            resp = await client.get(
                "https://www.jobbank.gc.ca/jobsearch/jobsearch",
                params={
                    "searchstring": query,
                    "locationstring": location.split(",")[0].strip() if location else "",
                    "sort": "M",  # Sort by relevance
                },
            )
            if resp.status_code != 200:
                logger.warning(f"Job Bank Canada returned status {resp.status_code}")
                return jobs

            soup = BeautifulSoup(resp.text, "html.parser")
            # Job Bank uses article elements or result-card divs
            cards = soup.select(
                "article.resultJobItem, div.resultJobItem, "
                "a[class*='resultJobItem']"
            )
            # Fallback: if no specific result cards found, try broader selector
            # but be more careful with filtering
            if not cards:
                cards = soup.select("div[class*='result']")

            # Known garbage titles from nav/UI elements that get scraped
            GARBAGE_TITLES = {
                "skip to filters", "browse", "best match", "date posted",
                "relevance", "search", "sign in", "menu", "close",
                "filter results", "sort by", "next", "previous",
                "show more", "load more", "back to top"
            }

            for card in cards[:limit]:
                title_el = card.select_one(
                    "span.noctitle, h3.jbTitle, a.resultJobItem, "
                    "span[class*='title'], h3"
                )
                # Don't use bare 'a' as title — picks up nav links
                if not title_el:
                    title_el = card.select_one("a[href*='/jobposting']")

                company_el = card.select_one(
                    "li.business, span[class*='business'], "
                    "span[class*='employer'], div[class*='company']"
                )
                location_el = card.select_one(
                    "li.location, span[class*='location'], "
                    "div[class*='location']"
                )
                salary_el = card.select_one(
                    "li.salary, span[class*='salary'], "
                    "div[class*='salary']"
                )

                if not title_el:
                    continue

                title_text = title_el.get_text(strip=True)
                # Skip garbage titles from nav/UI elements
                if title_text.lower() in GARBAGE_TITLES or len(title_text) < 4:
                    continue

                href = card.get("href", "") or (title_el.get("href", "") if title_el.name == "a" else "")
                if href and not href.startswith("http"):
                    href = f"https://www.jobbank.gc.ca{href}"

                # Skip entries without a real job URL
                if not href or "/jobposting" not in href.lower():
                    continue

                jobs.append({
                    "title": title_text,
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": location_el.get_text(strip=True) if location_el else "",
                    "description": "",
                    "url": href,
                    "source": "jobbank",
                    "salary_text": salary_el.get_text(strip=True) if salary_el else "",
                })

            if not jobs:
                return await _google_site_search("jobbank.gc.ca", query, location, limit, "jobbank")

    except Exception as e:
        logger.error(f"Job Bank Canada search failed: {e}")
        return await _google_site_search("jobbank.gc.ca", query, location, limit, "jobbank")
    return jobs


async def search_talent(query: str, location: str, limit: int = 25) -> list[dict]:
    """Search Talent.com (formerly Neuvoo) — Canadian-based global job aggregator.

    Talent.com uses React/Next.js rendering. The HTML returned by httpx is often
    minimal (skeleton), so we try multiple extraction strategies:
    1. __NEXT_DATA__ JSON (Next.js server-side data)
    2. JSON-LD structured data (schema.org/JobPosting)
    3. Embedded window.__data or similar JS state
    4. HTML card selectors as fallback
    5. DuckDuckGo site: search as final fallback

    Strips negative boolean operators from the query since Talent.com doesn't support them.
    """
    jobs = []
    # Strip negative keyword operators — Talent.com doesn't support boolean -"keyword"
    clean_query = re.sub(r'\s*-"[^"]*"', '', query).strip()
    # Detect country subdomain
    loc_lower = location.lower()
    if any(s in loc_lower for s in ["united states", "usa", ", us"]):
        domain = "www.talent.com"
    else:
        domain = "ca.talent.com"  # Default to Canada

    try:
        headers = _get_headers()
        headers["Referer"] = f"https://{domain}/"
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=25) as client:
            resp = await client.get(
                f"https://{domain}/jobs",
                params={
                    "q": clean_query,
                    "l": location.split(",")[0].strip() if location else "",
                },
            )
            if resp.status_code != 200:
                logger.warning(f"Talent.com returned status {resp.status_code}")
                return await _ddg_site_search("talent.com/jobs", clean_query, location, limit, "talent")

            soup = BeautifulSoup(resp.text, "html.parser")

            # ── Method 1: __NEXT_DATA__ JSON (Next.js app) ──
            next_data = soup.select_one("script#__NEXT_DATA__")
            if next_data and next_data.string:
                try:
                    data = json.loads(next_data.string)
                    props = data.get("props", {}).get("pageProps", {})
                    # Try multiple keys where job data might live
                    job_list = (
                        props.get("initialJobs")
                        or props.get("jobs")
                        or props.get("jobList")
                        or props.get("searchResults", {}).get("jobs")
                        or props.get("searchResults", {}).get("results")
                        or []
                    )
                    # If job_list is a dict with a nested list, extract it
                    if isinstance(job_list, dict):
                        job_list = job_list.get("jobs", job_list.get("results", []))
                    for item in job_list[:limit]:
                        if not isinstance(item, dict):
                            continue
                        sal_str = ""
                        if item.get("salary"):
                            sal_str = str(item["salary"])

                        job_url = item.get("url", item.get("jobUrl", item.get("href", "")))
                        if job_url and not job_url.startswith("http"):
                            job_url = f"https://{domain}{job_url}"

                        jobs.append({
                            "title": item.get("title", item.get("jobTitle", "")),
                            "company": item.get("company", item.get("companyName", "Unknown")),
                            "location": item.get("location", item.get("city", location)),
                            "description": (item.get("description", "") or "")[:2000],
                            "url": job_url,
                            "source": "talent",
                            "salary_text": sal_str,
                        })
                    if jobs:
                        logger.info(f"Talent.com __NEXT_DATA__: {len(jobs)} results")
                        return jobs
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    logger.debug(f"Talent.com __NEXT_DATA__ parse failed: {e}")

            # ── Method 2: JSON-LD structured data (schema.org/JobPosting) ──
            for script_tag in soup.select('script[type="application/ld+json"]'):
                try:
                    ld_data = json.loads(script_tag.string or "")
                    # Can be a single object or a list
                    items = ld_data if isinstance(ld_data, list) else [ld_data]
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        # Check for ItemList containing JobPostings
                        if item.get("@type") == "ItemList":
                            for elem in item.get("itemListElement", [])[:limit]:
                                job_item = elem.get("item", elem) if isinstance(elem, dict) else {}
                                if not isinstance(job_item, dict):
                                    continue
                                if job_item.get("@type") != "JobPosting":
                                    continue
                                org = job_item.get("hiringOrganization", {})
                                loc_data = job_item.get("jobLocation", {})
                                addr = loc_data.get("address", {}) if isinstance(loc_data, dict) else {}
                                jobs.append({
                                    "title": job_item.get("title", ""),
                                    "company": org.get("name", "Unknown") if isinstance(org, dict) else str(org),
                                    "location": addr.get("addressLocality", "") if isinstance(addr, dict) else "",
                                    "description": (job_item.get("description", "") or "")[:2000],
                                    "url": job_item.get("url", ""),
                                    "source": "talent",
                                })
                        elif item.get("@type") == "JobPosting":
                            org = item.get("hiringOrganization", {})
                            loc_data = item.get("jobLocation", {})
                            addr = loc_data.get("address", {}) if isinstance(loc_data, dict) else {}
                            jobs.append({
                                "title": item.get("title", ""),
                                "company": org.get("name", "Unknown") if isinstance(org, dict) else str(org),
                                "location": addr.get("addressLocality", "") if isinstance(addr, dict) else "",
                                "description": (item.get("description", "") or "")[:2000],
                                "url": item.get("url", ""),
                                "source": "talent",
                            })
                except (json.JSONDecodeError, TypeError):
                    continue
            if jobs:
                logger.info(f"Talent.com JSON-LD: {len(jobs)} results")
                return jobs

            # ── Method 3: Embedded JS state ──
            for script in soup.select("script"):
                text = script.string or ""
                # Look for window.__INITIAL_STATE__ or similar patterns
                for pattern_name, pattern in [
                    ("__INITIAL_STATE__", r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});'),
                    ("__data", r'window\.__data\s*=\s*(\{.+?\});'),
                    ("searchResults", r'"searchResults"\s*:\s*(\[.+?\])'),
                ]:
                    match = re.search(pattern, text, re.DOTALL)
                    if match:
                        try:
                            state = json.loads(match.group(1))
                            # Try to find job arrays in the state
                            job_list = []
                            if isinstance(state, list):
                                job_list = state
                            elif isinstance(state, dict):
                                for key in ["jobs", "results", "searchResults", "jobList"]:
                                    if key in state and isinstance(state[key], list):
                                        job_list = state[key]
                                        break
                            for item in job_list[:limit]:
                                if not isinstance(item, dict):
                                    continue
                                title = item.get("title", item.get("jobTitle", ""))
                                if not title:
                                    continue
                                job_url = item.get("url", item.get("jobUrl", item.get("href", "")))
                                if job_url and not job_url.startswith("http"):
                                    job_url = f"https://{domain}{job_url}"
                                jobs.append({
                                    "title": title,
                                    "company": item.get("company", item.get("companyName", "Unknown")),
                                    "location": item.get("location", item.get("city", location)),
                                    "description": (item.get("description", "") or "")[:2000],
                                    "url": job_url,
                                    "source": "talent",
                                })
                            if jobs:
                                logger.info(f"Talent.com JS state ({pattern_name}): {len(jobs)} results")
                                return jobs
                        except (json.JSONDecodeError, TypeError):
                            continue

            # ── Method 4: HTML card selectors ──
            cards = soup.select(
                "div[class*='card__job'], div[class*='jobCard'], "
                "a[class*='card__job'], div[data-testid='job-card'], "
                "div.card, div[class*='result'], li[class*='result'], "
                "div[class*='listing'], a[class*='listing']"
            )
            for card in cards[:limit]:
                title_el = card.select_one(
                    "h2, h3, a[class*='title'], span[class*='title'], "
                    "a[class*='link'], div[class*='title']"
                )
                company_el = card.select_one(
                    "span[class*='company'], div[class*='company'], "
                    "p[class*='company']"
                )
                location_el = card.select_one(
                    "span[class*='location'], div[class*='location'], "
                    "p[class*='location']"
                )
                salary_el = card.select_one(
                    "span[class*='salary'], div[class*='salary']"
                )

                if not title_el:
                    continue

                title_text = title_el.get_text(strip=True)
                if len(title_text) < 4 or len(title_text) > 200:
                    continue

                href = ""
                link = card if card.name == "a" else card.select_one("a")
                if link:
                    href = link.get("href", "")
                    if href and not href.startswith("http"):
                        href = f"https://{domain}{href}"

                jobs.append({
                    "title": title_text,
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": location_el.get_text(strip=True) if location_el else "",
                    "description": "",
                    "url": href,
                    "source": "talent",
                    "salary_text": salary_el.get_text(strip=True) if salary_el else "",
                })

            if jobs:
                logger.info(f"Talent.com HTML cards: {len(jobs)} results")
                return jobs

            # ── Method 5: DuckDuckGo fallback ──
            logger.info(f"Talent.com returned 200 but 0 results parsed — trying DDG fallback")
            return await _ddg_site_search("talent.com/jobs", clean_query, location, limit, "talent")

    except Exception as e:
        logger.error(f"Talent.com search failed: {e}")
        return await _ddg_site_search("talent.com/jobs", clean_query, location, limit, "talent")
    return jobs


async def search_remoteok(query: str, location: str, limit: int = 25) -> list[dict]:
    """Search RemoteOK — free JSON API, remote-focused jobs."""
    jobs = []
    try:
        headers = {"User-Agent": "jobbunt-app/1.0"}
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as client:
            resp = await client.get(
                "https://remoteok.com/api",
                params={"tag": query.lower().replace(" ", "-")},
            )
            if resp.status_code != 200:
                return jobs

            data = resp.json()
            # First item is usually a legal notice, skip it
            for item in data[1:limit + 1] if len(data) > 1 else []:
                if not isinstance(item, dict):
                    continue
                sal_str = ""
                if item.get("salary_min") and item.get("salary_max"):
                    sal_str = f"${int(item['salary_min']):,} - ${int(item['salary_max']):,}"

                jobs.append({
                    "title": item.get("position", ""),
                    "company": item.get("company", "Unknown"),
                    "location": item.get("location", "Remote"),
                    "description": (item.get("description", "") or "")[:2000],
                    "url": item.get("url", f"https://remoteok.com/remote-jobs/{item.get('id', '')}"),
                    "source": "remoteok",
                    "salary_text": sal_str,
                })
    except Exception as e:
        logger.error(f"RemoteOK search failed: {e}")
    return jobs


async def search_careerjet(query: str, location: str, limit: int = 25) -> list[dict]:
    """Search Careerjet — global job aggregator with public search pages.

    Careerjet redirects search URLs (301) to SEO-friendly paths. The client
    follows redirects, and we parse the final page which uses different
    selectors than the initial search URL.
    Strips negative boolean operators from the query since Careerjet doesn't support them.
    """
    jobs = []
    # Strip negative keyword operators — Careerjet doesn't support boolean -"keyword"
    clean_query = re.sub(r'\s*-"[^"]*"', '', query).strip()
    # Detect locale
    loc_lower = location.lower()
    if any(s in loc_lower for s in ["canada", "ontario", "toronto", ", on", ", bc", ", ab"]):
        domain = "www.careerjet.ca"
    else:
        domain = "www.careerjet.com"

    try:
        headers = _get_headers()
        headers["Referer"] = f"https://{domain}/"
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=25) as client:
            # Careerjet uses /jobs path for search (redirects from /search/jobs)
            resp = await client.get(
                f"https://{domain}/search/jobs",
                params={"s": clean_query, "l": location.split(",")[0].strip(), "radius": "50", "sort": "relevance"},
            )
            if resp.status_code != 200:
                logger.warning(f"Careerjet ({domain}) returned status {resp.status_code}")
                return jobs

            soup = BeautifulSoup(resp.text, "html.parser")

            # Careerjet uses multiple page formats after redirect:
            # Format 1: article.job with nested elements
            # Format 2: div.job-listing cards
            # Format 3: Direct job list items
            cards = soup.select(
                "article.job, div.job, li.job-listing, "
                "div[class*='job_result'], div[class*='job-result'], "
                "section.job_listing, div.cj-job"
            )

            # Broader fallback: look for any container with job-like links
            if not cards:
                # Careerjet often uses simple list format with job links
                cards = soup.select("div[data-url], a[class*='job'], div.listing")

            # Even broader fallback: find all links that look like job postings
            if not cards:
                all_links = soup.select("a[href*='/job/'], a[href*='/jobs/'], a[href*='jobposting']")
                for link in all_links[:limit]:
                    title_text = link.get_text(strip=True)
                    if len(title_text) < 5 or len(title_text) > 200:
                        continue
                    href = link.get("href", "")
                    if href and not href.startswith("http"):
                        href = f"https://{domain}{href}"
                    # Try to find company/location in parent or siblings
                    parent = link.parent
                    company_text = ""
                    loc_text = ""
                    if parent:
                        company_el = parent.select_one("p.company, span.company, div.company, span[class*='company']")
                        location_el = parent.select_one("span.location, div.location, span[class*='location'], ul.tags li")
                        company_text = company_el.get_text(strip=True) if company_el else ""
                        loc_text = location_el.get_text(strip=True) if location_el else ""
                    jobs.append({
                        "title": title_text,
                        "company": company_text or "Unknown",
                        "location": loc_text or "",
                        "description": "",
                        "url": href,
                        "source": "careerjet",
                    })
                if jobs:
                    logger.info(f"Careerjet ({domain}) link-based parse: {len(jobs)} results")
                    return jobs

            for card in cards[:limit]:
                title_el = card.select_one(
                    "h2 a, header a, a.job-title, a[class*='title'], "
                    "h3 a, a[class*='job_title'], span[class*='title'] a, "
                    "a[href*='/job/']"
                )
                company_el = card.select_one(
                    "p.company, span.company, div.company, "
                    "span[class*='company'], p[class*='company']"
                )
                location_el = card.select_one(
                    "ul.tags li, span.location, div.location, "
                    "span[class*='location'], p[class*='location']"
                )
                desc_el = card.select_one(
                    "div.desc, p.desc, div.description, "
                    "div[class*='description'], p[class*='desc']"
                )

                if not title_el:
                    # Try the card itself if it's an anchor
                    if card.name == "a":
                        title_el = card
                    else:
                        continue

                href = title_el.get("href", "") if title_el else ""
                if not href and card.get("data-url"):
                    href = card.get("data-url", "")
                if href and not href.startswith("http"):
                    href = f"https://{domain}{href}"

                title_text = title_el.get_text(strip=True)
                if len(title_text) < 3:
                    continue

                jobs.append({
                    "title": title_text,
                    "company": company_el.get_text(strip=True) if company_el else "Unknown",
                    "location": location_el.get_text(strip=True) if location_el else "",
                    "description": desc_el.get_text(strip=True)[:500] if desc_el else "",
                    "url": href,
                    "source": "careerjet",
                })

            if jobs:
                logger.info(f"Careerjet ({domain}): {len(jobs)} results")

    except Exception as e:
        logger.error(f"Careerjet search failed: {e}")
    return jobs


async def fetch_job_details(url: str) -> dict:
    """Fetch full job description from a job URL."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return {}
            soup = BeautifulSoup(resp.text, "html.parser")

            # Remove scripts and styles
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()

            # Try common job description containers
            desc_el = (
                soup.select_one("#jobDescriptionText") or  # Indeed
                soup.select_one("div.show-more-less-html__markup") or  # LinkedIn
                soup.select_one("div.jobDescriptionContent") or  # Glassdoor
                soup.select_one("div[id*='description']") or
                soup.select_one("div[class*='description']") or
                soup.select_one("article") or
                soup.select_one("main")
            )

            text = desc_el.get_text(separator="\n", strip=True) if desc_el else ""
            # Truncate to avoid huge payloads
            return {"full_description": text[:5000]}
    except Exception as e:
        logger.error(f"Failed to fetch job details from {url}: {e}")
        return {}



def _generate_tier_variants(base_roles: list[str], seniority_level: str, tiers_down: int, tiers_up: int) -> list[str]:
    """Generate additional role variants based on seniority tier range.

    For example, if the user is VP-level (index 4), tiers_down=1 means also search
    director-level variants. tiers_up=0 means don't look above.
    """
    if not seniority_level or seniority_level not in SENIORITY_TIERS:
        return base_roles

    current_idx = SENIORITY_TIERS.index(seniority_level)
    variant_roles = list(base_roles)  # Start with originals

    # Determine which tiers to add
    tiers_to_add = []
    for i in range(1, tiers_down + 1):
        idx = current_idx - i
        if 0 <= idx < len(SENIORITY_TIERS):
            tiers_to_add.append(SENIORITY_TIERS[idx])
    for i in range(1, tiers_up + 1):
        idx = current_idx + i
        if 0 <= idx < len(SENIORITY_TIERS):
            tiers_to_add.append(SENIORITY_TIERS[idx])

    if not tiers_to_add:
        return base_roles

    # Generate variants: for each base role, create tier-modified versions
    tier_variants = []
    for tier in tiers_to_add:
        keywords = TIER_TITLE_VARIANTS.get(tier, [])
        for base in base_roles[:4]:  # Use top 4 base roles (was 3)
            # Extract the core function from the role title
            # e.g., "Director, Information Security" -> "Information Security"
            core = base
            for prefix_list in TIER_TITLE_VARIANTS.values():
                for prefix in prefix_list:
                    if core.lower().startswith(prefix.lower()):
                        core = core[len(prefix):].strip(" ,of-")
                    # Also check "X of Y" patterns
                    pattern = re.compile(rf"^{re.escape(prefix)}\s+(?:of\s+)?", re.I)
                    core = pattern.sub("", core).strip()

            # Add top 2 keyword variants for this tier
            for kw in keywords[:2]:
                variant = f"{kw} {core}" if core else f"{kw} {base}"
                if variant.lower() not in [r.lower() for r in variant_roles] and variant.lower() not in [v.lower() for v in tier_variants]:
                    tier_variants.append(variant)

    # INTERLEAVE tier variants with originals so they don't all get cut off
    # Pattern: orig1, tier1, orig2, tier2, orig3, tier3, ...
    interleaved = []
    orig_iter = iter(base_roles)
    tier_iter = iter(tier_variants)
    seen = set()
    # Add first 3 originals, then alternate
    for _ in range(min(3, len(base_roles))):
        r = next(orig_iter, None)
        if r and r.lower() not in seen:
            interleaved.append(r)
            seen.add(r.lower())
    # Now interleave remaining
    while True:
        t = next(tier_iter, None)
        if t and t.lower() not in seen:
            interleaved.append(t)
            seen.add(t.lower())
        o = next(orig_iter, None)
        if o and o.lower() not in seen:
            interleaved.append(o)
            seen.add(o.lower())
        if t is None and o is None:
            break
    # Add any remaining tier variants
    for t in tier_iter:
        if t.lower() not in seen:
            interleaved.append(t)
            seen.add(t.lower())

    logger.info(f"Tier search: base {len(base_roles)} roles → {len(interleaved)} with tier variants (tiers_down={tiers_down}, tiers_up={tiers_up})")
    logger.info(f"Tier search order: {[r[:40] for r in interleaved[:10]]}")
    return interleaved



_google_jobs_call_count = 0  # Track calls per search session to limit volume
_serpapi_call_count = 0  # Track SerpAPI calls per session to avoid exhausting quota
_serpapi_max_calls = 20  # Dynamic limit: 20 local, 40 on Cloud Run

async def search_google_jobs(query: str, location: str, limit: int = 25) -> list[dict]:
    """Search Google Jobs — aggregates Indeed, LinkedIn, Glassdoor, ZipRecruiter, etc.

    Google Jobs returns structured data from multiple sources in a single query.
    Respects the global Google rate-limit cooldown.
    Limited to MAX 3 queries per search session to avoid 429 rate limiting.
    """
    import time as _time
    global _google_blocked_until, _google_jobs_call_count
    jobs = []
    # Strip negative keywords — Google Jobs doesn't support boolean operators well
    clean_query = re.sub(r'\s*-"[^"]*"', '', query).strip()
    search_query = f"{clean_query} jobs in {location}"

    # Limit Google Jobs to first 3 queries per session to avoid 429
    _google_jobs_call_count += 1
    if _google_jobs_call_count > 3:
        logger.info(f"Google Jobs: skipping query #{_google_jobs_call_count} (max 3 per session to avoid rate limiting)")
        return []

    # Skip if Google is rate-limited
    if _time.time() < _google_blocked_until:
        logger.info(f"Google blocked (cooling down) — skipping Google Jobs search")
        return []

    # Add longer delay between Google requests to reduce rate limiting
    await asyncio.sleep(2.0 + random.random() * 2.0)

    try:
        headers = _get_headers()
        # Add specific headers that help avoid blocks
        headers["Referer"] = "https://www.google.com/"

        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=25) as client:
            resp = await client.get(
                "https://www.google.com/search",
                params={
                    "q": search_query,
                    "ibp": "htl;jobs",
                    "hl": "en",
                    "gl": "ca",
                },
            )
            if resp.status_code != 200:
                logger.warning(f"Google Jobs returned status {resp.status_code}")
                return jobs

            text = resp.text

            # Method 1: Extract from embedded JSON (Google embeds job data as JS objects)
            # Look for the structured job data in the page
            import re as _re

            # Google Jobs embeds data in script blocks with job listings
            # Pattern: find job title, company, location triplets
            title_pattern = _re.compile(r'"([^"]{5,120})"\s*,\s*"([^"]{2,80})"\s*,\s*"([^"]{2,100})"')

            # Try to extract from the AF_initDataCallback blocks
            data_blocks = _re.findall(r'AF_initDataCallback\(\{[^}]*data:(.*?)\}\);', text, _re.DOTALL)

            for block in data_blocks:
                # Look for arrays that contain job data patterns
                job_arrays = _re.findall(
                    r'\["([^"]{5,150})","([^"]{2,80})","([^"]{2,120})"',
                    block
                )
                for title, company, loc in job_arrays[:limit]:
                    # Filter out non-job entries
                    if any(skip in title.lower() for skip in ["google", "sign in", "search", "filter", "javascript"]):
                        continue
                    if len(title) < 5 or len(company) < 2:
                        continue

                    jobs.append({
                        "title": title,
                        "company": company,
                        "location": loc or location,
                        "description": "",
                        "url": f"https://www.google.com/search?q={query}+{company}&ibp=htl;jobs",
                        "source": "google_jobs",
                    })
                    if len(jobs) >= limit:
                        break
                if jobs:
                    break

            # Method 2: Fallback - parse visible HTML for job cards
            if not jobs:
                soup = BeautifulSoup(text, "html.parser")

                # Google Jobs cards often use specific data attributes
                for card in soup.select("[data-ved] .BjJfJf, [data-ved] .PwjeAc, li.iFjolb")[:limit]:
                    title_el = card.select_one(".BjJfJf, .PwjeAc, h2, [role='heading']")
                    company_el = card.select_one(".vNEEBe, .nJlDiv, .company")
                    location_el = card.select_one(".Qk80Jf, .location, .pwTheAd")

                    if not title_el:
                        continue

                    title_text = title_el.get_text(strip=True)
                    if len(title_text) < 5:
                        continue

                    jobs.append({
                        "title": title_text,
                        "company": company_el.get_text(strip=True) if company_el else "Unknown",
                        "location": location_el.get_text(strip=True) if location_el else "",
                        "description": "",
                        "url": f"https://www.google.com/search?q={query}+jobs&ibp=htl;jobs",
                        "source": "google_jobs",
                    })

            if jobs:
                logger.info(f"Google Jobs: found {len(jobs)} results for '{query}' in '{location}'")

    except Exception as e:
        logger.error(f"Google Jobs search failed: {e}")

    return jobs


async def search_serpapi_google_jobs(query: str, location: str, limit: int = 25) -> list[dict]:
    """Search via SerpAPI Google Jobs endpoint (requires SERPAPI_KEY in .env).

    SerpAPI provides clean structured JSON from Google Jobs, which aggregates
    Indeed, LinkedIn, Glassdoor, ZipRecruiter, and many more sources.
    Rate limited to 5 calls per search session to avoid quota exhaustion.
    """
    global _serpapi_call_count, _serpapi_max_calls
    _serpapi_call_count += 1
    max_calls = getattr(search_serpapi_google_jobs, '_max', None) or globals().get('_serpapi_max_calls', 20)
    if _serpapi_call_count > max_calls:
        logger.info(f"SerpAPI: skipping call #{_serpapi_call_count} (max {max_calls} per session to preserve quota)")
        return []

    api_key = os.environ.get("SERPAPI_KEY", "") or _get_source_config().get("serpapi", {}).get("api_key", "")
    if not api_key:
        logger.warning("SerpAPI: no API key configured, skipping")
        return []

    # Normalize location for SerpAPI (needs "Province/State, Country" format)
    serpapi_loc = location
    # "Milton, ON" → "Ontario, Canada", "Toronto, Ontario, Canada" → "Ontario, Canada"
    ca_provinces = {"ON": "Ontario", "BC": "British Columbia", "AB": "Alberta", "QC": "Quebec",
                    "MB": "Manitoba", "SK": "Saskatchewan", "NS": "Nova Scotia", "NB": "New Brunswick",
                    "PE": "Prince Edward Island", "NL": "Newfoundland and Labrador"}
    for abbr, full in ca_provinces.items():
        if f", {abbr}" in location or location.endswith(f" {abbr}"):
            serpapi_loc = f"{full}, Canada"
            break
    if ", Canada" in location:
        # Already has province, extract it
        parts = [p.strip() for p in location.split(",")]
        for p in parts:
            if p in ca_provinces.values():
                serpapi_loc = f"{p}, Canada"
                break
    # Clean query: strip negative keywords for SerpAPI
    clean_query = re.sub(r'\s+-"[^"]*"', '', query).strip() if '-"' in query else query

    logger.info(f"SerpAPI call #{_serpapi_call_count}: q='{clean_query}' loc='{serpapi_loc}'")
    jobs = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # Paginate using next_page_token from response (Google discontinued `start` param)
            next_page_token = None
            for page_num in range(2):  # up to 2 pages
                if page_num > 0:
                    _serpapi_call_count += 1
                    max_calls = globals().get('_serpapi_max_calls', 20)
                    if _serpapi_call_count > max_calls:
                        logger.info("SerpAPI: skipping page 2 (quota limit)")
                        break
                    if not next_page_token:
                        break  # No more pages available
                params = {
                    "engine": "google_jobs",
                    "q": clean_query,
                    "location": serpapi_loc,
                    "hl": "en",
                    "api_key": api_key,
                }
                if next_page_token:
                    params["next_page_token"] = next_page_token
                resp = await client.get("https://serpapi.com/search.json", params=params)
                if resp.status_code != 200:
                    logger.warning(f"SerpAPI returned status {resp.status_code}: {resp.text[:200]}")
                    break

                data = resp.json()
                page_results = data.get("jobs_results", [])
                if not page_results:
                    break

                # Extract next_page_token for subsequent page
                serpapi_pagination = data.get("serpapi_pagination", {})
                next_page_token = serpapi_pagination.get("next_page_token")

                for item in page_results[:limit - len(jobs)]:
                    # Extract salary if available
                    salary = ""
                    detected = item.get("detected_extensions", {})
                    if detected.get("salary"):
                        salary = detected["salary"]

                    # Get ALL sources from apply options (multi-tagging)
                    apply_links = item.get("apply_options", [])
                    sources_seen = []
                    primary_source = "google_jobs"
                    url = ""
                    # Priority order for primary source
                    priority = {"indeed": 1, "linkedin": 2, "glassdoor": 3}
                    best_priority = 99
                    for link in apply_links:
                        link_title = link.get("title", "").lower()
                        link_url = link.get("link", "")
                        # Identify known sources
                        for known in ["indeed", "linkedin", "glassdoor", "ziprecruiter", "monster"]:
                            if known in link_title:
                                if known not in sources_seen:
                                    sources_seen.append(known)
                                p = priority.get(known, 50)
                                if p < best_priority:
                                    best_priority = p
                                    primary_source = known
                                    url = link_url
                                break
                        else:
                            # Unknown source — use as fallback
                            src_name = link_title.replace(".com", "").replace(".ca", "").strip() or "google_jobs"
                            if src_name not in sources_seen:
                                sources_seen.append(src_name)
                            if not url:
                                url = link_url
                                if primary_source == "google_jobs":
                                    primary_source = src_name

                    if not sources_seen:
                        sources_seen = [primary_source]

                    jobs.append({
                        "title": item.get("title", ""),
                        "company": item.get("company_name", "Unknown"),
                        "location": item.get("location", location),
                        "description": (item.get("description", "") or "")[:2000],
                        "url": url,
                        "source": primary_source,
                        "sources_seen": sources_seen,
                        "salary_text": salary,
                        "job_type": detected.get("schedule_type", ""),
                        "remote_type": "remote" if detected.get("work_from_home") else "",
                    })

                if not next_page_token or len(jobs) >= limit:
                    break  # No more pages or reached limit

            if jobs:
                logger.info(f"SerpAPI Google Jobs: found {len(jobs)} results for '{query}' in '{location}'")

    except Exception as e:
        logger.error(f"SerpAPI search failed: {e}")

    return jobs


# ── Source config file helper ─────────────────────────────────────────────
_source_config_cache: dict | None = None
_source_config_mtime: float = 0

SOURCE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "source_config.json")


def _get_source_config() -> dict:
    """Load and cache source config from data/source_config.json.

    Automatically reloads if the file has been modified since last read.
    Returns empty dict if file doesn't exist.
    """
    global _source_config_cache, _source_config_mtime
    try:
        mtime = os.path.getmtime(SOURCE_CONFIG_PATH)
        if _source_config_cache is not None and mtime == _source_config_mtime:
            return _source_config_cache
        with open(SOURCE_CONFIG_PATH, "r") as f:
            _source_config_cache = json.load(f)
            _source_config_mtime = mtime
            return _source_config_cache
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.debug(f"Could not load source config: {e}")
        return {}


def _save_source_config(config: dict):
    """Save source config to data/source_config.json."""
    global _source_config_cache, _source_config_mtime
    os.makedirs(os.path.dirname(SOURCE_CONFIG_PATH), exist_ok=True)
    with open(SOURCE_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    _source_config_cache = config
    _source_config_mtime = os.path.getmtime(SOURCE_CONFIG_PATH)


async def search_serpapi_indeed(query: str, location: str, limit: int = 25) -> list[dict]:
    """Search for Indeed results via SerpAPI Google Jobs engine.

    NOTE: SerpAPI discontinued the dedicated 'indeed' engine.
    This now delegates to Google Jobs which aggregates Indeed listings.
    """
    logger.info("SerpAPI Indeed engine discontinued — delegating to Google Jobs (aggregates Indeed)")
    return await search_serpapi_google_jobs(query, location, limit)


# ── Source Registry ───────────────────────────────────────────────────────

AVAILABLE_SOURCES = {
    "linkedin": {
        "name": "LinkedIn",
        "search_fn": "search_linkedin_jobs",
        "fallback_site": "linkedin.com/jobs",
        "region": "global",
    },
    "indeed": {
        "name": "Indeed",
        "search_fn": "search_indeed",
        "fallback_site": "indeed.com",
        "region": "global",
    },
    "glassdoor": {
        "name": "Glassdoor",
        "search_fn": "search_glassdoor",
        "fallback_site": "glassdoor.com/job",
        "region": "global",
    },
    "talent": {
        "name": "Talent.com",
        "search_fn": "search_talent",
        "fallback_site": "talent.com/jobs",
        "region": "global",
    },
    "adzuna": {
        "name": "Adzuna",
        "search_fn": "search_adzuna",
        "fallback_site": None,  # API-based
        "region": "global",
    },
    "jobbank": {
        "name": "Job Bank",
        "search_fn": "search_jobbank",
        "fallback_site": "jobbank.gc.ca",
        "region": "canada",
    },
    "gcjobs": {
        "name": "GC Jobs",
        "search_fn": "search_gcjobs",
        "fallback_site": None,
        "region": "canada",
    },
    "usajobs": {
        "name": "USAJobs",
        "search_fn": "search_usajobs",
        "fallback_site": None,
        "region": "usa",
    },
    "remoteok": {
        "name": "RemoteOK",
        "search_fn": "search_remoteok",
        "fallback_site": None,
        "region": "global",
    },
    "careerjet": {
        "name": "Careerjet",
        "search_fn": "search_careerjet",
        "fallback_site": "careerjet.ca/search/jobs",
        "region": "global",
    },
    "google_jobs": {
        "name": "Google Jobs",
        "search_fn": "search_google_jobs",
        "fallback_site": None,
        "region": "global",
    },
    "serpapi": {
        "name": "SerpAPI",
        "search_fn": "search_serpapi_google_jobs",
        "fallback_site": None,
        "region": "global",
    },
}

# Map region to which sources to use
# google_jobs is listed first as it aggregates Indeed/LinkedIn/Glassdoor
REGION_SOURCES = {
    # serpapi covers Indeed/Glassdoor/Google Jobs via API (rate-limited to 5 calls/session)
    # google_jobs direct scraping removed (JS-rendered, always fails)
    # indeed/glassdoor direct scraping removed (403/429 blocks)
    # serpapi paused — preserving quota while fixing coverage strategy
    "canada": ["linkedin", "careerjet", "talent", "adzuna", "jobbank", "gcjobs", "remoteok"],
    "usa": ["linkedin", "careerjet", "talent", "adzuna", "usajobs", "remoteok"],
    "global": ["linkedin", "careerjet", "talent", "adzuna", "remoteok"],
}


def _detect_region(locations: list[str]) -> str:
    """Detect which region based on target locations."""
    loc_text = " ".join(locations).lower()
    canada_signals = ["canada", ", on", ", bc", ", ab", ", qc", ", mb", ", sk", ", ns",
                       "ontario", "toronto", "ottawa", "vancouver", "montreal", "calgary",
                       "milton", "gta"]
    usa_signals = [", us", "united states", "new york", "california", "texas", "florida",
                   "chicago", "seattle", "san francisco", "washington"]

    is_canada = any(s in loc_text for s in canada_signals)
    is_usa = any(s in loc_text for s in usa_signals)

    if is_canada and not is_usa:
        return "canada"
    elif is_usa and not is_canada:
        return "usa"
    return "global"


async def _search_with_fallback(source_key: str, query: str, location: str, limit: int) -> list[dict]:
    """Search a source with retry logic, health tracking, and Google/DDG fallback.

    Each source gets up to 2 retries with exponential backoff before falling
    back to search engine site: queries. Source health is tracked so that
    consistently failing sources are temporarily skipped.
    """
    source_config = AVAILABLE_SOURCES[source_key]

    # Skip API-key-dependent sources if no key configured (check env + config file)
    cfg = _get_source_config()
    serpapi_key = os.environ.get("SERPAPI_KEY") or cfg.get("serpapi", {}).get("api_key", "")
    if source_key == "serpapi" and not serpapi_key:
        return []
    adzuna_key = os.environ.get("ADZUNA_APP_ID") or cfg.get("adzuna", {}).get("app_id", "")
    if source_key == "adzuna" and not adzuna_key:
        return []

    # SerpAPI paused — skip SerpAPI fallback for Indeed
    # if source_key == "indeed" and serpapi_key:
    #     try:
    #         results = await search_serpapi_indeed(query, location, limit)
    #         ...
    #     except Exception as e:
    #         ...

    # Check source health — skip temporarily broken sources
    if not _is_source_healthy(source_key):
        return []

    search_fn = globals()[source_config["search_fn"]]

    # Determine retry config per source
    # API-based sources (usajobs, adzuna, remoteok, serpapi) get fewer retries
    # Scrape-heavy sources (indeed, linkedin, glassdoor) get more retries
    api_sources = {"usajobs", "adzuna", "remoteok", "serpapi"}
    max_retries = 1 if source_key in api_sources else 2

    try:
        async def _attempt():
            return await search_fn(query, location, limit)

        results = await _retry_with_backoff(_attempt, max_retries=max_retries, base_delay=1.5)
        if results:
            _record_source_success(source_key, len(results))
            return results
        else:
            logger.info(f"[{source_key}] Primary returned 0 results (not an error)")
    except Exception as e:
        _record_source_failure(source_key, str(e))

    # Try Google/DDG fallback if configured (skip on Cloud Run — always 429)
    is_cloud = os.environ.get("ENV") == "production" or os.environ.get("K_SERVICE")
    fallback_site = source_config.get("fallback_site")
    if fallback_site and not is_cloud:
        logger.info(f"[{source_key}] Trying search engine fallback via site:{fallback_site}")
        try:
            results = await _google_site_search(fallback_site, query, location, limit, source_key)
            if results:
                logger.info(f"[{source_key}] Fallback returned {len(results)} results")
                _record_source_success(source_key, len(results))
                return results
            else:
                logger.info(f"[{source_key}] Fallback also returned 0 results")
        except Exception as e:
            logger.warning(f"[{source_key}] Fallback also failed: {e}")

    return []


async def _ai_expand_queries(profile: Profile, base_roles: list[str]) -> tuple[list[str], list[str]]:
    """Use AI to generate smarter, broader search queries based on the profile.

    Takes the profile context and base role titles, returns a tuple of:
    - Additional search queries (synonyms, adjacent roles, industry-specific terms)
    - Negative keywords to exclude from searches (wrong-domain terms)
    """
    try:
        from backend.services.ai import ai_generate_json

        profile_summary = getattr(profile, 'profile_summary', '') or ''
        career_trajectory = getattr(profile, 'career_trajectory', '') or ''
        skills = safe_json(profile.skills, [])
        industries = safe_json(getattr(profile, 'industry_preferences', None), [])
        seniority = getattr(profile, 'seniority_level', None) or 'senior'
        tiers_down = getattr(profile, 'search_tiers_down', 0) or 0
        tiers_up = getattr(profile, 'search_tiers_up', 0) or 0
        deal_breakers = safe_json(getattr(profile, 'deal_breakers', None), [])
        strengths = safe_json(getattr(profile, 'strengths', None), [])

        # Also get rule-based negative keywords as a starting point
        rule_based_negatives = _build_negative_keywords(profile)

        prompt = f"""You are an executive job search strategist. Given this candidate's FULL profile context, generate highly targeted search queries and exclusion keywords.

**Candidate profile:**
- Target roles: {', '.join(base_roles[:7])}
- Current seniority: {seniority}
- Search tiers down: {tiers_down} (0 = only target level, 1 = one level below OK, etc.)
- Search tiers up: {tiers_up} (0 = only target level, 1 = one level above OK, etc.)
- Key skills: {', '.join(skills[:12]) if isinstance(skills, list) else str(skills)[:200]}
- Industries: {', '.join(industries[:5]) if isinstance(industries, list) else ''}
- Profile summary: {profile_summary[:400]}
- Career trajectory: {career_trajectory[:300]}
- Key strengths: {', '.join(strengths[:4]) if isinstance(strengths, list) else ''}
- Deal breakers: {', '.join(deal_breakers[:3]) if isinstance(deal_breakers, list) else ''}

**CRITICAL SEARCH RULES:**
1. This candidate's domain is VERY specific. If they target "Chief Information SECURITY Officer" (CISO), do NOT include "Chief Information Officer" (CIO) — those are DIFFERENT roles. CIO is an IT leadership role; CISO is a cybersecurity role. Only include CIO if their profile explicitly mentions IT operations leadership without security focus.
2. Queries must be EXACT JOB TITLES, not skills or buzzwords.
3. Be precise about the domain: cybersecurity ≠ general IT, security engineering ≠ physical security.
4. Respect the seniority tier preferences. If tiers_down=0, do NOT suggest junior roles.

Return a JSON object with:
{{
    "queries": ["5-8 alternative EXACT JOB TITLES that match this candidate's specific domain and seniority. Be precise — 'VP Cybersecurity' not 'VP Technology'. Include industry-specific title variations."],
    "negative_keywords": ["words/phrases to EXCLUDE — wrong-domain titles and terms. Include titles that look similar but are wrong (e.g., 'Chief Information Officer' if candidate is CISO, 'IT Director' if candidate is Security Director). Also include wrong-industry terms."]
}}"""

        result = await ai_generate_json(prompt, max_tokens=600, model_tier="fast")

        expanded_queries = []
        ai_negatives = []

        if isinstance(result, dict):
            queries_raw = result.get("queries", [])
            if isinstance(queries_raw, list) and len(queries_raw) > 0:
                existing_lower = {r.lower() for r in base_roles}
                expanded_queries = [q for q in queries_raw if isinstance(q, str) and q.lower() not in existing_lower][:8]

            neg_raw = result.get("negative_keywords", [])
            if isinstance(neg_raw, list):
                ai_negatives = [kw for kw in neg_raw if isinstance(kw, str)][:15]
        elif isinstance(result, list) and len(result) > 0:
            # Backward compat: if AI returns just an array, treat as queries
            existing_lower = {r.lower() for r in base_roles}
            expanded_queries = [q for q in result if isinstance(q, str) and q.lower() not in existing_lower][:8]

        # Merge AI negatives with rule-based negatives (deduplicated)
        all_negatives_lower = {kw.lower() for kw in rule_based_negatives}
        for kw in ai_negatives:
            if kw.lower() not in all_negatives_lower:
                rule_based_negatives.append(kw)
                all_negatives_lower.add(kw.lower())

        logger.info(f"AI query expansion: {len(expanded_queries)} extra queries, {len(rule_based_negatives)} negative keywords")
        return expanded_queries, rule_based_negatives
    except Exception as e:
        logger.warning(f"AI query expansion failed (non-fatal): {e}")

    # Fallback: return rule-based negatives even if AI fails
    return [], _build_negative_keywords(profile)


def _build_negative_keywords(profile: Profile) -> list[str]:
    """Infer negative keywords from the profile's domain to exclude irrelevant jobs.

    Uses profile skills and target roles to detect the candidate's domain,
    then returns keywords that commonly cause false-positive matches.
    """
    target_roles = safe_json(profile.target_roles, [])
    skills = safe_json(profile.skills, [])
    profile_text = " ".join(target_roles + skills).lower()

    negative_keywords = []

    # IT/Cyber security → exclude physical security
    it_sec_signals = ["cybersecurity", "information security", "it security", "infosec",
                      "cyber security", "ciso", "soc analyst", "penetration test",
                      "security engineer", "security architect", "devsecops"]
    if any(kw in profile_text for kw in it_sec_signals):
        negative_keywords.extend([
            "physical security", "security guard", "loss prevention",
            "armed", "patrol", "CCTV", "surveillance officer",
            "guard supervisor", "bodyguard", "fire watch",
            "asset protection", "door supervisor", "access control officer",
            "building security", "site security", "mobile patrol",
            "unarmed guard", "armed guard", "security officer",
        ])

    # Software engineering → exclude unrelated engineering
    sw_signals = ["software engineer", "software developer", "full stack",
                  "frontend developer", "backend developer", "web developer"]
    if any(kw in profile_text for kw in sw_signals):
        negative_keywords.extend([
            "mechanical engineer", "civil engineer", "chemical engineer",
            "electrical engineer", "hvac", "plant engineer",
        ])

    # Data science → exclude data entry and unrelated scientist roles
    data_signals = ["data scientist", "data analyst", "data engineer",
                    "machine learning", "business intelligence"]
    if any(kw in profile_text for kw in data_signals):
        negative_keywords.extend(["data entry", "data capture", "data entry clerk"])
    if "data scientist" in profile_text:
        negative_keywords.extend(["political scientist", "social scientist", "research scientist"])

    return negative_keywords


async def filter_irrelevant_jobs(profile: Profile, raw_jobs: list[dict]) -> list[dict]:
    """Post-search AI relevance gate: filters out jobs from wrong domains.

    Batches jobs into groups of 20 and asks the fast AI model to identify
    which ones are relevant to the candidate's target domain.
    Returns only the relevant jobs.
    """
    if not raw_jobs:
        return raw_jobs

    from backend.services.ai import ai_generate_json, get_provider
    if get_provider() == "none":
        logger.info("AI relevance filter skipped: no AI provider configured")
        return raw_jobs

    target_roles = safe_json(profile.target_roles, [])
    skills = safe_json(profile.skills, [])
    profile_summary = getattr(profile, 'profile_summary', '') or ''
    seniority = getattr(profile, 'seniority_level', None) or ''

    domain_context = (
        f"Target roles: {', '.join(target_roles[:5])}\n"
        f"Key skills: {', '.join(skills[:8]) if isinstance(skills, list) else ''}\n"
        f"Seniority: {seniority}\n"
        f"Summary: {profile_summary[:200]}"
    )

    BATCH_SIZE = 20
    kept_jobs = []
    total_rejected = 0

    for batch_start in range(0, len(raw_jobs), BATCH_SIZE):
        batch = raw_jobs[batch_start:batch_start + BATCH_SIZE]

        # Build job list for the prompt
        job_lines = []
        for idx, job in enumerate(batch):
            title = job.get("title", "Unknown")
            company = job.get("company", "Unknown")
            snippet = (job.get("description") or job.get("snippet") or "")[:120]
            job_lines.append(f"{idx}. {title} @ {company} — {snippet}")

        prompt = f"""You are a job relevance filter. Given a candidate's profile domain, determine which jobs are RELEVANT (same professional domain) vs IRRELEVANT (completely wrong field/domain).

CANDIDATE DOMAIN:
{domain_context}

JOBS TO EVALUATE:
{chr(10).join(job_lines)}

Return a JSON object with:
- "keep": [list of job indices (integers) that ARE relevant to this candidate's domain]
- "reject": [list of job indices (integers) that are clearly WRONG domain]

Rules:
- REJECT jobs from a completely different professional field (e.g., physical security guard for an IT security professional, mechanical engineering for a software engineer)
- KEEP anything that's even plausibly related — err on the side of keeping
- Only reject clear domain mismatches, not merely imperfect fits"""

        try:
            result = await ai_generate_json(prompt, max_tokens=400, model_tier="flash")
            if isinstance(result, dict) and "keep" in result:
                keep_indices = set(result["keep"])
                for idx, job in enumerate(batch):
                    if idx in keep_indices:
                        kept_jobs.append(job)
                    else:
                        total_rejected += 1
            else:
                # If AI response is malformed, keep all jobs in this batch
                kept_jobs.extend(batch)
        except Exception as e:
            logger.warning(f"AI relevance filter batch failed (keeping all): {e}")
            kept_jobs.extend(batch)

    logger.info(f"AI relevance filter: {len(raw_jobs)} input → {len(kept_jobs)} kept, {total_rejected} rejected")
    return kept_jobs


INDUSTRY_SOURCES = {
    "construction": [
        {"key": "constructconnect", "name": "ConstructConnect", "url": "https://www.constructconnect.com/careers"},
        {"key": "buildforce", "name": "BuildForce Canada", "url": "https://www.buildforce.ca/en/find-jobs"},
    ],
    "healthcare": [
        {"key": "healthcarejobsite", "name": "HealthcareJobSite", "url": "https://www.healthcarejobsite.com"},
    ],
    "finance": [
        {"key": "efinancialcareers", "name": "eFinancialCareers", "url": "https://www.efinancialcareers.com"},
    ],
    "technology": [
        {"key": "dice", "name": "Dice", "url": "https://www.dice.com"},
        {"key": "stackoverflow", "name": "Stack Overflow Jobs", "url": "https://stackoverflow.com/jobs"},
    ],
    "cybersecurity": [
        {"key": "cybersecjobs", "name": "CyberSecJobs", "url": "https://www.cybersecjobs.com"},
        {"key": "dice", "name": "Dice", "url": "https://www.dice.com"},
    ],
    "engineering": [
        {"key": "engineeringjobs", "name": "EngineeringJobs", "url": "https://www.engineeringjobs.net"},
    ],
    "education": [
        {"key": "higheredjobs", "name": "HigherEdJobs", "url": "https://www.higheredjobs.com"},
    ],
    "government": [
        {"key": "gcjobs", "name": "GC Jobs", "url": "https://emplois.gc.ca"},
        {"key": "usajobs", "name": "USAJobs", "url": "https://www.usajobs.gov"},
    ],
}

# Keywords used to detect industry from profile skills, roles, and preferences
_INDUSTRY_KEYWORDS = {
    "construction": ["construction", "building", "civil engineering", "architecture", "trades",
                      "project management construction", "site supervisor", "general contractor"],
    "healthcare": ["healthcare", "health care", "nursing", "medical", "clinical", "hospital",
                    "pharmaceutical", "pharmacy", "physiotherapy", "physician"],
    "finance": ["finance", "banking", "investment", "accounting", "actuarial", "fintech",
                "financial analyst", "portfolio", "wealth management", "cpa", "cfa"],
    "technology": ["software", "developer", "programming", "devops", "full stack",
                   "frontend", "backend", "cloud computing", "saas", "data science", "machine learning"],
    "cybersecurity": ["cybersecurity", "cyber security", "information security", "infosec",
                      "penetration testing", "soc analyst", "security grc", "nist", "ciso"],
    "engineering": ["mechanical engineer", "electrical engineer", "chemical engineer",
                    "manufacturing engineer", "process engineer"],
    "education": ["teacher", "professor", "education", "curriculum", "academic",
                  "instructional design", "university", "school administrator"],
    "government": ["public service", "government", "policy analyst", "public administration",
                   "municipal", "federal government"],
}


def get_industry_recommendations(profile) -> list[dict]:
    """Suggest industry-relevant job boards based on profile analysis.

    Examines the profile's industry_preference, target_roles, and skills to
    detect the user's industry and return matching specialty job boards.
    """
    from backend.utils import safe_json

    # Build a text blob from profile data to match against
    parts = []
    industry_pref = getattr(profile, 'industry_preference', None) or ''
    if industry_pref:
        parts.append(industry_pref.lower())

    industry_prefs = safe_json(getattr(profile, 'industry_preferences', None), [])
    if isinstance(industry_prefs, list):
        parts.extend([p.lower() for p in industry_prefs])

    roles = safe_json(getattr(profile, 'target_roles', None), [])
    if isinstance(roles, list):
        parts.extend([r.lower() for r in roles])

    skills = safe_json(getattr(profile, 'skills', None), [])
    if isinstance(skills, list):
        parts.extend([s.lower() for s in skills])

    text_blob = " ".join(parts)

    # Match industries
    matched_industries = set()
    for industry, keywords in _INDUSTRY_KEYWORDS.items():
        if any(kw in text_blob for kw in keywords):
            matched_industries.add(industry)

    # Collect recommended sources (deduplicated by key)
    seen_keys = set()
    recommendations = []
    for industry in matched_industries:
        for source in INDUSTRY_SOURCES.get(industry, []):
            if source["key"] not in seen_keys:
                seen_keys.add(source["key"])
                recommendations.append({
                    "key": source["key"],
                    "name": source["name"],
                    "url": source["url"],
                    "industry": industry,
                })

    return recommendations


async def search_all_sources(profile: Profile, sources: list[str] | None = None, limit_per_source: int = 15) -> dict:
    """Run search across all sources for a given profile.

    Returns a dict with:
        - "jobs": list of raw job dicts
        - "relevance_filtered": count of jobs removed by AI relevance filter

    Args:
        profile: User profile with target roles and locations
        sources: Optional list of source keys to search (e.g. ["linkedin", "indeed"]).
                 If None, auto-detects based on target locations.
        limit_per_source: Max results per source per query
    """
    target_roles = safe_json(profile.target_roles, [])
    target_locations = safe_json(profile.target_locations, [""])

    if not target_roles:
        target_roles = [""]

    if not target_locations:
        target_locations = [""]

    # Generate tier variants if configured
    tiers_down = getattr(profile, 'search_tiers_down', 0) or 0
    tiers_up = getattr(profile, 'search_tiers_up', 0) or 0
    seniority = getattr(profile, 'seniority_level', None)
    if (tiers_down > 0 or tiers_up > 0) and seniority:
        target_roles = _generate_tier_variants(target_roles, seniority, tiers_down, tiers_up)

    # Domain-aware executive title injection: for cybersecurity profiles that
    # include director tier+up to c-suite, ensure CISO is explicitly searched
    roles_lower = [r.lower() for r in target_roles]
    skills_json = safe_json(profile.skills, [])
    skills_lower = " ".join(s.lower() for s in skills_json) if isinstance(skills_json, list) else str(skills_json).lower()
    is_cyber = any(kw in skills_lower for kw in ["cybersecurity", "information security", "infosec", "cyber security", "security grc"])
    if is_cyber:
        # Inject key executive cyber titles if not already present
        cyber_exec_titles = ["CISO", "Chief Information Security Officer", "VP Information Security"]
        for title in cyber_exec_titles:
            if title.lower() not in roles_lower:
                target_roles.append(title)
                roles_lower.append(title.lower())
                logger.info(f"Auto-injected cybersecurity executive title: {title}")

    # AI-powered query expansion: generate smarter search queries + negative keywords
    negative_keywords = []
    try:
        ai_queries, negative_keywords = await _ai_expand_queries(profile, target_roles)
        if ai_queries:
            # Keep original target roles first, fill remaining slots with AI queries
            # Increased cap to 10 — executive roles need more query variants to get coverage
            max_ai = max(0, 10 - len(target_roles))
            target_roles = target_roles + ai_queries[:max_ai]
            logger.info(f"Search queries after AI expansion: {len(target_roles)} total ({len(target_roles) - max_ai} original + {min(max_ai, len(ai_queries))} AI)")
        if negative_keywords:
            logger.info(f"Negative keywords for filtering: {negative_keywords[:5]}...")
    except Exception as e:
        logger.warning(f"AI query expansion skipped: {e}")
        negative_keywords = _build_negative_keywords(profile)

    # Determine which sources to search
    is_cloud = os.environ.get("ENV") == "production" or os.environ.get("K_SERVICE")  # Cloud Run detection
    if sources:
        active_sources = [s for s in sources if s in AVAILABLE_SOURCES]
    else:
        region = _detect_region(target_locations)
        active_sources = REGION_SOURCES.get(region, REGION_SOURCES["global"])

    if is_cloud:
        # Cloud Run IPs get blocked by Google (429), Indeed (403), and most scrapers.
        # Only SerpAPI (API-based), Careerjet, Adzuna, and JobBank reliably work.
        # Remove sources that depend on Google site-search fallback from Cloud Run.
        cloud_blocked = {"gcjobs", "talent"}  # These rely on Google/DDG fallbacks which always 429
        active_sources = [s for s in active_sources if s not in cloud_blocked]
        logger.info(f"Cloud Run detected — removed unreliable sources, using: {active_sources}")

    logger.info(f"Multi-source search: sources={active_sources}, roles={len(target_roles)}, locations={len(target_locations[:2])}")

    # Build negative keyword suffix for search queries (Indeed supports -keyword syntax)
    neg_suffix = ""
    if negative_keywords:
        # Use up to 5 negative keywords to avoid making queries too long
        neg_terms = [f'-"{kw}"' for kw in negative_keywords[:5]]
        neg_suffix = " " + " ".join(neg_terms)

    # Sources that support boolean negative operators in query strings
    # Only Indeed and SerpAPI handle -"keyword" properly
    # Careerjet, Talent.com, and Google Jobs do NOT support boolean operators
    BOOLEAN_SOURCES = {"indeed", "serpapi"}

    # Reset per-session counters
    global _google_jobs_call_count, _serpapi_call_count
    _google_jobs_call_count = 0
    _serpapi_call_count = 0

    # On Cloud Run, SerpAPI is the primary reliable source — allow more calls
    global _serpapi_max_calls
    _serpapi_max_calls = 40 if is_cloud else 20

    all_jobs = []
    source_result_counts: dict[str, int] = {}  # Track per-source totals
    for role in target_roles[:10]:
        for loc in target_locations[:2]:
            # Run all selected sources concurrently with fallback
            # Only append negative keywords to sources that support boolean operators
            source_results = await asyncio.gather(
                *[_search_with_fallback(
                    src,
                    role + neg_suffix if (neg_suffix and src in BOOLEAN_SOURCES) else role,
                    loc,
                    limit_per_source
                ) for src in active_sources],
                return_exceptions=True,
            )
            for src_key, r in zip(active_sources, source_results):
                if isinstance(r, list):
                    all_jobs.extend(r)
                    source_result_counts[src_key] = source_result_counts.get(src_key, 0) + len(r)
                elif isinstance(r, Exception):
                    logger.error(f"[{src_key}] Unhandled exception in gather: {r}")
                    source_result_counts.setdefault(src_key, 0)

    # Log per-source summary
    summary_parts = [f"{src}={count}" for src, count in sorted(source_result_counts.items(), key=lambda x: -x[1])]
    logger.info(f"Multi-source search complete: {len(all_jobs)} total raw results. Per-source: {', '.join(summary_parts) or 'none'}")

    # ── Browser fallback: if regular scraping returned few/no results, use Playwright ──
    if len(all_jobs) < 5:
        logger.info(f"Only {len(all_jobs)} results from regular scraping — trying Playwright browser fallback")
        try:
            from backend.services.browser_orchestrator import scrape_jobs_browser
            # Use first role + first location for browser search
            browser_query = target_roles[0] if target_roles else ""
            browser_loc = target_locations[0] if target_locations else ""
            browser_sites = ["indeed_ca", "linkedin", "glassdoor"] if _detect_region(target_locations) == "canada" else ["indeed", "linkedin", "glassdoor"]
            browser_jobs = await scrape_jobs_browser(browser_query, browser_loc, browser_sites, max_per_site=15)
            if browser_jobs:
                all_jobs.extend(browser_jobs)
                logger.info(f"Browser fallback added {len(browser_jobs)} jobs")
                source_result_counts["browser"] = len(browser_jobs)
        except Exception as e:
            logger.warning(f"Browser fallback failed: {e}")

    # AI relevance gate: filter out jobs from wrong domains
    pre_filter_count = len(all_jobs)
    try:
        all_jobs = await filter_irrelevant_jobs(profile, all_jobs)
    except Exception as e:
        logger.warning(f"AI relevance filter failed (keeping all jobs): {e}")
    relevance_filtered = pre_filter_count - len(all_jobs)
    if relevance_filtered > 0:
        logger.info(f"AI relevance filter removed {relevance_filtered} irrelevant jobs")

    # Industry-specific source recommendations
    recommended_sources = get_industry_recommendations(profile)

    return {
        "jobs": all_jobs,
        "relevance_filtered": relevance_filtered,
        "recommended_sources": recommended_sources,
    }


def _fuzzy_match_title(title1: str, title2: str) -> bool:
    """Check if two job titles are likely the same role (fuzzy match).

    Uses normalize_title() to strip seniority prefixes and level indicators
    before comparing, so "Senior Developer" and "Jr. Developer" at the same
    company are recognized as the same base role posted at different levels.
    """
    # Quick exact check (lowercase + strip specials)
    t1_raw = re.sub(r"[^a-z0-9 ]", "", title1.lower()).strip()
    t2_raw = re.sub(r"[^a-z0-9 ]", "", title2.lower()).strip()
    if t1_raw == t2_raw:
        return True
    # Semantic check: normalize away seniority/level differences
    t1 = normalize_title(title1)
    t2 = normalize_title(title2)
    if t1 == t2:
        return True
    # Word-overlap check on normalized titles
    words1 = set(t1.split())
    words2 = set(t2.split())
    if not words1 or not words2:
        return False
    overlap = words1 & words2
    # If 70%+ word overlap, likely same role
    min_len = min(len(words1), len(words2))
    if min_len > 0 and len(overlap) / min_len >= 0.7:
        return True
    return False


def _fuzzy_match_company(comp1: str, comp2: str) -> bool:
    """Check if two company names likely refer to the same company.

    Uses normalize_company() to strip corporate suffixes before comparing.
    """
    c1 = normalize_company(comp1)
    c2 = normalize_company(comp2)
    if c1 == c2:
        return True
    # Check containment (e.g. "Royal Bank of Canada" vs "RBC" won't match, but
    # "Scotiabank" vs "Scotiabank Global" will)
    if c1 and c2 and (c1 in c2 or c2 in c1):
        return True
    return False


def _sanitize_title(title: str) -> str:
    """Strip scraping artifacts (source badges, metadata) from job titles."""
    import re as _re
    # Remove jobbank boilerplate
    title = _re.sub(r'Posted on Job Bank.*?Job Bank\.?Job Bank', '', title, flags=_re.IGNORECASE)
    title = _re.sub(r'Posted on Job Bank.*?Job Bank', '', title, flags=_re.IGNORECASE)
    # Remove common source-name prefixes that get concatenated
    prefixes = [
        r'CareerBeacon', r'Direct Apply', r'indeed\.com', r'talent\.com',
        r'civicjobs\.ca', r'outscal\.com', r'recruit\.net', r'jobrapido',
        r'expertini', r'whatjobs', r'glassdoor', r'neuvoo',
    ]
    for pfx in prefixes:
        title = _re.sub(r'^' + pfx + r'\s*', '', title, flags=_re.IGNORECASE)
    # Remove leading "New", "On site", "Hybrid", "Remote" status badges
    title = _re.sub(r'^(New\s*)?(On\s*site\s*)?(Hybrid\s*)?(Remote\s*)?', '', title, flags=_re.IGNORECASE)
    title = title.strip()
    # Capitalize first letter if lowercase
    if title and title[0].islower():
        title = title[0].upper() + title[1:]
    return title


def _merge_sources(target_job, raw: dict, source: str):
    """Merge source info from a raw job dict into an existing Job, avoiding duplicates."""
    try:
        sources = json.loads(target_job.sources_seen or "[]")
    except (json.JSONDecodeError, TypeError):
        sources = [target_job.source] if target_job.source else []
    raw_sources = raw.get("sources_seen", [source])
    for s in raw_sources:
        if s not in sources:
            sources.append(s)
    target_job.sources_seen = json.dumps(sources)
    # Keep the longer description
    new_desc = raw.get("description", "")
    if new_desc and len(new_desc) > len(target_job.description or ""):
        target_job.description = new_desc
    # Keep a URL if the existing one is empty
    if not target_job.url and raw.get("url"):
        target_job.url = raw["url"]


def save_scraped_jobs(db: Session, profile_id: int, raw_jobs: list[dict]) -> list[Job]:
    """Save scraped jobs to DB, deduplicating by fingerprint + semantic + fuzzy matching.

    Dedup layers (checked in order):
    1. Fingerprint — exact hash of lowercased title+company+city
    2. Semantic   — normalize_title()+normalize_company() key match (strips
                    seniority prefixes, corporate suffixes, etc.)
    3. Fuzzy      — word-overlap on normalized titles + company containment
    """
    new_jobs = []
    seen_fps = {}  # Track fingerprints within this batch

    # Build indexes of existing jobs for dedup
    existing_jobs = db.query(Job).filter(Job.profile_id == profile_id).all()
    existing_index = {}   # company_norm -> [Job, ...]  (for fuzzy dedup)
    semantic_index = {}   # (norm_title, norm_company) -> Job  (for semantic dedup)
    for ej in existing_jobs:
        comp_key = re.sub(r"[^a-z0-9]", "", (ej.company or "").lower())
        existing_index.setdefault(comp_key, []).append(ej)
        # Semantic index key
        sem_key = (normalize_title(ej.title or ""), normalize_company(ej.company or ""))
        if sem_key not in semantic_index:
            semantic_index[sem_key] = ej

    dupes_by_semantic = 0
    dupes_by_fuzzy = 0

    for raw in raw_jobs:
        title = _sanitize_title(raw.get("title", "").strip())
        company = raw.get("company", "").strip()
        if not title or not company:
            continue

        fp = make_fingerprint(title, company, raw.get("location", ""))
        source = raw.get("source", "unknown")

        # ── Layer 1: Fingerprint dedup (exact hash match) ──
        # Check in-memory batch first (catches dupes within same scrape run)
        if fp in seen_fps:
            _merge_sources(seen_fps[fp], raw, source)
            continue

        existing = job_exists(db, profile_id, fp)
        if existing:
            existing.last_seen = datetime.utcnow()
            _merge_sources(existing, raw, source)
            continue

        # ── Layer 2: Semantic dedup (normalized title + company) ──
        # Catches "Senior Developer" vs "Jr. Developer" at "Acme Inc." vs "Acme"
        norm_t = normalize_title(title)
        norm_c = normalize_company(company)
        sem_key = (norm_t, norm_c)

        sem_match = semantic_index.get(sem_key)
        if sem_match:
            dupes_by_semantic += 1
            sem_match.last_seen = datetime.utcnow()
            _merge_sources(sem_match, raw, source)
            logger.debug(f"Semantic dedup: '{title}' at '{company}' matched existing '{sem_match.title}' at '{sem_match.company}'")
            continue

        # ── Layer 3: Fuzzy dedup (word overlap on titles + company containment) ──
        # This catches cases that semantic normalization misses
        comp_key = re.sub(r"[^a-z0-9]", "", company.lower())
        fuzzy_match = None

        # Check against existing DB jobs with similar company name
        for ck, ej_list in existing_index.items():
            if _fuzzy_match_company(company, ej_list[0].company if ej_list else ""):
                for ej in ej_list:
                    if _fuzzy_match_title(title, ej.title or ""):
                        fuzzy_match = ej
                        break
            if fuzzy_match:
                break

        # Also check against batch jobs
        if not fuzzy_match:
            for batch_fp, batch_job in seen_fps.items():
                if _fuzzy_match_company(company, batch_job.company or "") and \
                   _fuzzy_match_title(title, batch_job.title or ""):
                    fuzzy_match = batch_job
                    break

        if fuzzy_match:
            dupes_by_fuzzy += 1
            fuzzy_match.last_seen = datetime.utcnow()
            _merge_sources(fuzzy_match, raw, source)
            logger.debug(f"Fuzzy dedup: '{title}' at '{company}' matched existing '{fuzzy_match.title}' at '{fuzzy_match.company}'")
            continue

        # ── No match found — create new job ──
        job = Job(
            profile_id=profile_id,
            fingerprint=fp,
            title=title,
            company=company,
            location=raw.get("location", ""),
            salary_text=raw.get("salary_text", ""),
            description=raw.get("description", ""),
            url=raw.get("url", ""),
            source=source,
            sources_seen=json.dumps(raw.get("sources_seen", [source])),
            status="pending",
            scraped_at=datetime.utcnow(),
        )
        db.add(job)
        new_jobs.append(job)
        seen_fps[fp] = job
        # Add to both dedup indexes
        existing_index.setdefault(comp_key, []).append(job)
        semantic_index.setdefault(sem_key, job)

    if dupes_by_semantic or dupes_by_fuzzy:
        logger.info(f"Dedup stats: {dupes_by_semantic} semantic + {dupes_by_fuzzy} fuzzy duplicates merged")

    db.commit()
    for j in new_jobs:
        db.refresh(j)
    return new_jobs
