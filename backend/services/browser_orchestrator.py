"""Playwright-based browser scraping orchestrator.

Uses a real headless Chromium browser to navigate job sites, inject the
JavaScript extractors from browser_scraper.py, and return structured job data.
This bypasses anti-bot protections that block raw HTTP requests.
"""

import asyncio
import json
import logging
import random
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy-loaded playwright to avoid import errors if not installed
_playwright = None
_browser = None


async def _get_browser():
    """Get or create a shared browser instance."""
    global _playwright, _browser
    if _browser and _browser.is_connected():
        return _browser
    try:
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        logger.info("Playwright browser launched")
        return _browser
    except Exception as e:
        logger.error(f"Failed to launch Playwright browser: {e}")
        raise


async def _scrape_site(browser, url: str, extractor_js: str, site_key: str, timeout: int = 20000) -> list[dict]:
    """Navigate to a URL, inject extractor JS, return parsed jobs."""
    context = None
    try:
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/Toronto",
        )
        # Block unnecessary resources for speed
        await context.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,eot}", lambda route: route.abort())
        await context.route("**/analytics**", lambda route: route.abort())
        await context.route("**/tracking**", lambda route: route.abort())

        page = await context.new_page()

        # Navigate with retry
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as e:
            logger.warning(f"[{site_key}] Navigation timeout/error for {url}: {e}")
            return []

        # Wait a bit for JS rendering
        await page.wait_for_timeout(random.randint(2000, 4000))

        # Scroll down to trigger lazy loading
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1000)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)

        # Inject extractor
        try:
            result = await page.evaluate(extractor_js)
            jobs = json.loads(result) if isinstance(result, str) else result
            if jobs:
                logger.info(f"[{site_key}] Extracted {len(jobs)} jobs from {url}")
            else:
                logger.info(f"[{site_key}] No jobs extracted from {url}")
            return jobs if isinstance(jobs, list) else []
        except Exception as e:
            logger.warning(f"[{site_key}] Extractor failed: {e}")
            return []
    except Exception as e:
        logger.error(f"[{site_key}] Browser scrape failed: {e}")
        return []
    finally:
        if context:
            await context.close()


async def scrape_jobs_browser(
    query: str,
    location: str,
    sites: Optional[list[str]] = None,
    max_per_site: int = 15,
) -> list[dict]:
    """Scrape jobs from multiple sites using Playwright.

    Args:
        query: Job title/role to search for
        location: Target location
        sites: Optional list of site keys (indeed, linkedin, glassdoor, etc.)
               Defaults to all available sites.
        max_per_site: Max results per site

    Returns:
        List of job dicts ready for DB insertion
    """
    from backend.services.browser_scraper import build_search_urls, get_extractor

    urls = build_search_urls(query, location, sites)
    if not urls:
        logger.warning("No search URLs generated")
        return []

    try:
        browser = await _get_browser()
    except Exception:
        return []

    all_jobs = []

    # Process sites sequentially to avoid overwhelming the browser
    for site_key, url in urls.items():
        extractor_js = get_extractor(site_key)
        jobs = await _scrape_site(browser, url, extractor_js, site_key)

        # Limit per site
        for job in jobs[:max_per_site]:
            # Ensure all required fields
            job.setdefault("title", "")
            job.setdefault("company", "Unknown")
            job.setdefault("location", location)
            job.setdefault("description", "")
            job.setdefault("url", "")
            job.setdefault("source", site_key)
            if job["title"] and job["url"]:
                all_jobs.append(job)

        # Random delay between sites
        if len(urls) > 1:
            await asyncio.sleep(random.uniform(1.0, 3.0))

    logger.info(f"Browser scraping complete: {len(all_jobs)} total jobs from {len(urls)} sites")
    return all_jobs


async def close_browser():
    """Clean up browser resources."""
    global _browser, _playwright
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
