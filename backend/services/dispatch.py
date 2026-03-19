"""Dispatch Scout — automated Indeed scraper using Playwright with stealth.

Uses the user's real Chrome profile for cookies/fingerprint to avoid detection.
Falls back to a stealth-configured browser if Chrome profile isn't available.
"""
import asyncio
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Chrome user data directory (Windows default)
CHROME_USER_DATA = os.environ.get(
    "CHROME_USER_DATA",
    str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"),
)


async def scrape_indeed(searches: list[dict], max_pages: int = 2) -> list[dict]:
    """Scrape Indeed using Playwright with stealth.

    Args:
        searches: List of {"query": ..., "location": ..., "indeed_ca_url": ..., "indeed_url": ...}
        max_pages: Max pages to scrape per search (default 2, ~30 jobs per search)

    Returns:
        List of raw job dicts ready for save_scraped_jobs()
    """
    try:
        from playwright.async_api import async_playwright
        from playwright_stealth import stealth_async
    except ImportError:
        logger.error("Playwright or playwright-stealth not installed")
        return []

    all_jobs = []
    seen_urls = set()

    async with async_playwright() as p:
        # Try to use existing Chrome profile for real cookies/fingerprint
        browser = None
        context = None
        use_profile = os.path.isdir(CHROME_USER_DATA)

        try:
            if use_profile:
                logger.info(f"Launching Chrome with user profile: {CHROME_USER_DATA}")
                # Launch persistent context with user's Chrome profile
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=CHROME_USER_DATA,
                    channel="chrome",
                    headless=False,  # Indeed detects headless
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                    viewport={"width": 1280, "height": 800},
                    user_agent=None,  # Use Chrome's real UA
                )
            else:
                logger.info("No Chrome profile found, using stealth browser")
                browser = await p.chromium.launch(
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                )

            page = await context.new_page()
            await stealth_async(page)

            for search in searches:
                try:
                    jobs = await _scrape_indeed_search(
                        page, search, max_pages, seen_urls
                    )
                    all_jobs.extend(jobs)
                    logger.info(
                        f"Indeed dispatch: {len(jobs)} jobs for "
                        f"'{search['query']}' in '{search['location']}'"
                    )
                except Exception as e:
                    logger.warning(f"Indeed search failed for '{search['query']}': {e}")
                    continue

                # Brief pause between searches to look human
                await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Dispatch browser setup failed: {e}")
        finally:
            try:
                if context and not use_profile:
                    await context.close()
                elif context and use_profile:
                    await context.close()
                if browser:
                    await browser.close()
            except Exception:
                pass

    logger.info(f"Indeed dispatch complete: {len(all_jobs)} total jobs from {len(searches)} searches")
    return all_jobs


async def _scrape_indeed_search(
    page, search: dict, max_pages: int, seen_urls: set
) -> list[dict]:
    """Scrape a single Indeed search (multiple pages)."""
    jobs = []

    # Use Canadian Indeed by default
    url = search.get("indeed_ca_url") or search.get("indeed_url", "")
    if not url:
        return jobs

    for page_num in range(max_pages):
        page_url = url if page_num == 0 else f"{url}&start={page_num * 10}"
        logger.debug(f"Indeed page {page_num + 1}: {page_url}")

        try:
            resp = await page.goto(page_url, wait_until="domcontentloaded", timeout=15000)
            if not resp or resp.status != 200:
                logger.warning(f"Indeed returned status {resp.status if resp else 'None'}")
                break
        except Exception as e:
            logger.warning(f"Indeed navigation failed: {e}")
            break

        # Wait for job cards to render
        try:
            await page.wait_for_selector(
                "div.job_seen_beacon, div.jobsearch-ResultsList div.result, "
                "td.resultContent, div[data-jk], a[data-jk]",
                timeout=8000,
            )
        except Exception:
            # Check for CAPTCHA or block
            content = await page.content()
            if "captcha" in content.lower() or "blocked" in content.lower():
                logger.warning("Indeed CAPTCHA/block detected, stopping")
                break
            logger.debug("No job cards found on page")
            break

        # Small delay to let lazy-loaded content render
        await asyncio.sleep(1.5)

        # Extract job cards
        page_jobs = await page.evaluate("""() => {
            const jobs = [];
            // Indeed uses multiple card formats — try all known selectors
            const cards = document.querySelectorAll(
                'div.job_seen_beacon, div[data-jk], td.resultContent'
            );

            for (const card of cards) {
                try {
                    // Title
                    const titleEl = card.querySelector(
                        'h2.jobTitle a, h2.jobTitle span[id^="jobTitle"], ' +
                        'a[data-jk] span[title], a.jcs-JobTitle'
                    );
                    const title = titleEl?.textContent?.trim() || '';
                    if (!title || title.length < 3) continue;

                    // URL
                    const linkEl = card.querySelector('a[data-jk], h2.jobTitle a, a.jcs-JobTitle');
                    let url = '';
                    const jk = linkEl?.getAttribute('data-jk') || card.getAttribute('data-jk');
                    if (jk) {
                        url = 'https://ca.indeed.com/viewjob?jk=' + jk;
                    } else if (linkEl?.href) {
                        url = linkEl.href;
                    }

                    // Company
                    const compEl = card.querySelector(
                        'span[data-testid="company-name"], span.css-1h7lukg, ' +
                        'span.companyName, a[data-tn-element="companyName"]'
                    );
                    const company = compEl?.textContent?.trim() || 'Unknown';

                    // Location
                    const locEl = card.querySelector(
                        'div[data-testid="text-location"], div.css-1restlb, ' +
                        'div.companyLocation, span.companyLocation'
                    );
                    const location = locEl?.textContent?.trim() || '';

                    // Salary
                    const salEl = card.querySelector(
                        'div[data-testid="attribute_snippet_testid"], ' +
                        'div.salary-snippet-container, span.salary-snippet, ' +
                        'div.metadata.salary-snippet-container'
                    );
                    const salary = salEl?.textContent?.trim() || '';

                    // Snippet/description
                    const descEl = card.querySelector(
                        'div.job-snippet, td.snHl, ul[style]'
                    );
                    const description = descEl?.textContent?.trim() || '';

                    jobs.push({ title, company, location, url, salary_text: salary, description });
                } catch (e) {
                    // Skip malformed cards
                }
            }
            return jobs;
        }""")

        for job in page_jobs:
            if job["url"] and job["url"] not in seen_urls:
                seen_urls.add(job["url"])
                job["source"] = "indeed"
                job["sources_seen"] = ["indeed"]
                jobs.append(job)

        # Check if there's a next page
        has_next = await page.evaluate("""() => {
            const next = document.querySelector('a[data-testid="pagination-page-next"], a[aria-label="Next Page"]');
            return !!next;
        }""")
        if not has_next:
            break

        await asyncio.sleep(1 + (page_num * 0.5))  # Gradual slowdown

    return jobs
