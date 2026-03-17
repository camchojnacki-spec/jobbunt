"""Browser-based job scraping via Chrome MCP integration.

Contains JavaScript extraction scripts for major job sites that can be
injected into the user's authenticated browser session. Each extractor
returns a JSON array of job objects ready for the import endpoint.

Usage flow:
  1. Navigate to site search results (via Chrome MCP navigate)
  2. Inject the appropriate extractor script (via Chrome MCP javascript_tool)
  3. Parse the returned JSON array
  4. POST to /api/profiles/{id}/import-browser-jobs
"""

import logging
import json
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


# ── Search URL builders ──────────────────────────────────────────────────

def build_search_urls(query: str, location: str, sites: list[str] = None) -> dict:
    """Build search URLs for each supported site.

    Returns dict of {site_key: search_url}.
    """
    q = quote_plus(query)
    loc = quote_plus(location)

    all_urls = {
        "indeed": f"https://www.indeed.com/jobs?q={q}&l={loc}&fromage=14&sort=date",
        "indeed_ca": f"https://ca.indeed.com/jobs?q={q}&l={loc}&fromage=14&sort=date",
        "glassdoor": f"https://www.glassdoor.com/Job/jobs.htm?sc.keyword={q}&locT=C&locKeyword={quote_plus(location)}",
        "linkedin": f"https://www.linkedin.com/jobs/search/?keywords={q}&location={loc}&f_TPR=r604800",
        "zip_recruiter": f"https://www.ziprecruiter.com/jobs-search?search={q}&location={loc}&days=14",
        "monster": f"https://www.monster.com/jobs/search?q={q}&where={loc}&page=1&so=m.h.s",
        "workday": None,  # Workday requires company-specific URLs
    }

    if sites:
        return {k: v for k, v in all_urls.items() if k in sites and v}
    return {k: v for k, v in all_urls.items() if v}


# ── JavaScript extraction scripts ────────────────────────────────────────
# Each script runs in the browser page context and returns JSON string

EXTRACTORS = {}

EXTRACTORS["indeed"] = """
(function() {
    const jobs = [];

    // Method 1: Try mosaic provider data (embedded JSON)
    try {
        const scripts = document.querySelectorAll('script[type="text/javascript"]');
        for (const script of scripts) {
            const text = script.textContent || '';
            if (text.includes('mosaic-provider-jobcards') || text.includes('jobKeysWithInfo')) {
                // Extract from window.mosaic.providerData
                const match = text.match(/window\\.mosaic\\.providerData\\["mosaic-provider-jobcards"\\]\\s*=\\s*({.*?});/s);
                if (match) {
                    const data = JSON.parse(match[1]);
                    const results = data?.metaData?.mosaicProviderJobCardsModel?.results || [];
                    for (const r of results) {
                        jobs.push({
                            title: r.title || '',
                            company: r.company || '',
                            location: r.formattedLocation || r.jobLocationCity || '',
                            description: r.snippet || r.jobSnippet || '',
                            url: r.link ? 'https://www.indeed.com' + r.link :
                                 r.jobkey ? 'https://www.indeed.com/viewjob?jk=' + r.jobkey : '',
                            source: 'indeed',
                            salary_text: r.formattedSalary || r.salarySnippet?.text || '',
                            posted_date: r.formattedRelativeTime || '',
                        });
                    }
                }
            }
        }
    } catch(e) { console.log('Mosaic extraction failed:', e); }

    // Method 2: DOM scraping fallback
    if (jobs.length === 0) {
        const cards = document.querySelectorAll('div.job_seen_beacon');

        for (const card of cards) {
            const titleEl = card.querySelector(
                'h2.jobTitle a, h2 a, a.jcs-JobTitle, span[id^="jobTitle"]'
            );
            const companyEl = card.querySelector(
                'span[data-testid="company-name"], span.companyName'
            );
            const locationEl = card.querySelector(
                'div[data-testid="text-location"], div.companyLocation'
            );
            const salaryEl = card.querySelector(
                'div.salary-snippet-container, div[class*="salary"]'
            );
            const snippetEl = card.querySelector('div.job-snippet');

            if (!titleEl) continue;

            // Extract job key from ancestor or link href
            let jk = '';
            const jkEl = card.closest('[data-jk]') || card.querySelector('[data-jk]');
            if (jkEl) {
                jk = jkEl.getAttribute('data-jk');
            } else {
                const linkEl = card.querySelector('a[href*="jk="]');
                if (linkEl) {
                    const m = linkEl.getAttribute('href').match(/jk=([a-f0-9]+)/);
                    if (m) jk = m[1];
                }
            }

            jobs.push({
                title: titleEl.textContent?.trim() || '',
                company: companyEl?.textContent?.trim() || 'Unknown',
                location: locationEl?.textContent?.trim() || '',
                description: snippetEl?.textContent?.trim() || '',
                url: jk ? window.location.origin + '/viewjob?jk=' + jk : '',
                source: 'indeed',
                salary_text: salaryEl?.textContent?.trim() || '',
            });
        }
    }

    return JSON.stringify(jobs);
})();
"""

EXTRACTORS["glassdoor"] = """
(function() {
    const jobs = [];

    // Try React state / Apollo cache
    try {
        const stateEl = document.querySelector('#__NEXT_DATA__');
        if (stateEl) {
            const data = JSON.parse(stateEl.textContent);
            const results = data?.props?.pageProps?.jobListings?.jobListings ||
                          data?.props?.pageProps?.data?.jobListings || [];
            for (const item of results) {
                const j = item.jobview?.job || item.job || item;
                const employer = item.jobview?.header?.employerNameFromSearch ||
                               item.employer?.name || j.employer?.name || '';
                jobs.push({
                    title: j.jobTitleText || j.title || item.jobview?.header?.jobTitleText || '',
                    company: employer,
                    location: j.locationName || item.jobview?.header?.locationName || '',
                    description: j.descriptionFragment || j.description || '',
                    url: j.seoJobLink ? 'https://www.glassdoor.com' + j.seoJobLink :
                         j.listingId ? 'https://www.glassdoor.com/job-listing/j?jl=' + j.listingId : '',
                    source: 'glassdoor',
                    salary_text: item.jobview?.header?.payPercentile90 ?
                        '$' + (item.jobview.header.payPercentile10||'') + ' - $' + item.jobview.header.payPercentile90 : '',
                });
            }
        }
    } catch(e) { console.log('Glassdoor __NEXT_DATA__ failed:', e); }

    // DOM fallback
    if (jobs.length === 0) {
        const cards = document.querySelectorAll(
            'li.react-job-listing, li[data-test="jobListing"], ' +
            'li.JobsList_jobListItem__wjTHv, div.JobCard_jobCardContainer__arQlW, ' +
            'a[data-test="job-link"]'
        );

        for (const card of cards) {
            const titleEl = card.querySelector(
                'a[data-test="job-link"], a.jobLink, ' +
                'a.JobCard_jobTitle__GLyJ1, div.job-title'
            );
            const companyEl = card.querySelector(
                'span.EmployerProfile_employerName__qHaEF, ' +
                'a[data-test="employer-short-name"], ' +
                'div.jobCard_company, span.job-search-key-l2wjgv'
            );
            const locationEl = card.querySelector(
                'span[data-test="emp-location"], div.location, ' +
                'span.JobCard_location__rCz3x'
            );
            const salaryEl = card.querySelector(
                'span.JobCard_salaryEstimate__QpbTW, ' +
                'span[data-test="detailSalary"], div.salary-estimate'
            );

            if (!titleEl) continue;

            let href = titleEl.getAttribute('href') || '';
            if (href && !href.startsWith('http')) {
                href = 'https://www.glassdoor.com' + href;
            }

            jobs.push({
                title: titleEl.textContent?.trim() || '',
                company: companyEl?.textContent?.trim() || 'Unknown',
                location: locationEl?.textContent?.trim() || '',
                description: '',
                url: href,
                source: 'glassdoor',
                salary_text: salaryEl?.textContent?.trim() || '',
            });
        }
    }

    return JSON.stringify(jobs);
})();
"""

EXTRACTORS["linkedin"] = """
(function() {
    const jobs = [];
    const seen = {};

    // Authenticated LinkedIn job search (2025/2026 layout)
    const cards = document.querySelectorAll(
        'li.scaffold-layout__list-item, li.occludable-update'
    );

    for (const card of cards) {
        const linkEl = card.querySelector('a[href*="/jobs/view/"]');
        if (!linkEl) continue;

        let href = (linkEl.getAttribute('href') || '').split('?')[0];
        if (href && !href.startsWith('http')) href = 'https://www.linkedin.com' + href;
        if (seen[href]) continue;
        seen[href] = true;

        // Title: aria-hidden span or strong inside the link
        const titleEl = linkEl.querySelector('span[aria-hidden="true"]') || linkEl.querySelector('strong');
        const title = titleEl ? titleEl.textContent.trim() : '';

        // Company: logo alt text (ends with " logo")
        const logoImg = card.querySelector('img[alt$=" logo"]');
        let company = logoImg ? logoImg.alt.replace(/ logo$/i, '').trim() : '';

        // Location: text containing (On-site), (Hybrid), or (Remote)
        let location = '';
        card.querySelectorAll('li, span').forEach(el => {
            const t = el.textContent.trim();
            if (t.match(/\\(On-site\\)|\\(Hybrid\\)|\\(Remote\\)/)) location = t;
        });

        jobs.push({
            title: title,
            company: company || 'Unknown',
            location: location,
            description: '',
            url: href,
            source: 'linkedin',
        });
    }

    // Public/guest search layout fallback
    if (jobs.length === 0) {
        const publicCards = document.querySelectorAll(
            'div.base-card, li.result-card, div.base-search-card'
        );
        for (const card of publicCards) {
            const titleEl = card.querySelector('h3.base-search-card__title, h3');
            const companyEl = card.querySelector('h4.base-search-card__subtitle, a.hidden-nested-link');
            const locationEl = card.querySelector('span.job-search-card__location');
            const linkEl = card.querySelector('a.base-card__full-link, a[href*="/jobs/view/"]');
            if (!titleEl) continue;
            jobs.push({
                title: titleEl.textContent?.trim() || '',
                company: companyEl?.textContent?.trim() || 'Unknown',
                location: locationEl?.textContent?.trim() || '',
                description: '',
                url: linkEl?.getAttribute('href') || '',
                source: 'linkedin',
            });
        }
    }

    return JSON.stringify(jobs);
})();
"""

EXTRACTORS["zip_recruiter"] = """
(function() {
    const jobs = [];

    const cards = document.querySelectorAll(
        'article.job_result, div.job_result_two_pane, ' +
        'div[data-testid="job-card"], li.job-listing'
    );

    for (const card of cards) {
        const titleEl = card.querySelector(
            'h2.job_result_title a, a.job_link, ' +
            'h2[class*="JobTitle"], a[data-testid="job-title"]'
        );
        const companyEl = card.querySelector(
            'a.t_org_link, span.job_org, ' +
            'p[data-testid="job-company"]'
        );
        const locationEl = card.querySelector(
            'span.location, p[data-testid="job-location"]'
        );
        const salaryEl = card.querySelector(
            'span.job_salary, p[data-testid="job-salary"]'
        );

        if (!titleEl) continue;

        let href = titleEl.getAttribute('href') || '';
        if (href && !href.startsWith('http')) {
            href = 'https://www.ziprecruiter.com' + href;
        }

        jobs.push({
            title: titleEl.textContent?.trim() || '',
            company: companyEl?.textContent?.trim() || 'Unknown',
            location: locationEl?.textContent?.trim() || '',
            description: '',
            url: href,
            source: 'ziprecruiter',
            salary_text: salaryEl?.textContent?.trim() || '',
        });
    }

    return JSON.stringify(jobs);
})();
"""

EXTRACTORS["monster"] = """
(function() {
    const jobs = [];

    const cards = document.querySelectorAll(
        'div[data-testid="svx_jobCard"], article.job-cardstyle, ' +
        'div.job-search-resultsstyle__JobCardComponent'
    );

    for (const card of cards) {
        const titleEl = card.querySelector(
            'h2[data-testid="svx_jobCard-title"] a, ' +
            'a.job-cardstyle__applyButton, h2 a'
        );
        const companyEl = card.querySelector(
            'span[data-testid="svx_jobCard-company"], ' +
            'span.job-cardstyle__CompanyName'
        );
        const locationEl = card.querySelector(
            'span[data-testid="svx_jobCard-location"], ' +
            'span.job-cardstyle__Location'
        );

        if (!titleEl) continue;

        let href = titleEl.getAttribute('href') || '';
        if (href && !href.startsWith('http')) {
            href = 'https://www.monster.com' + href;
        }

        jobs.push({
            title: titleEl.textContent?.trim() || '',
            company: companyEl?.textContent?.trim() || 'Unknown',
            location: locationEl?.textContent?.trim() || '',
            description: '',
            url: href,
            source: 'monster',
        });
    }

    return JSON.stringify(jobs);
})();
"""

# Generic fallback extractor that uses structured data (JSON-LD, microdata)
EXTRACTORS["generic"] = """
(function() {
    const jobs = [];

    // Try JSON-LD structured data first (many job boards use this)
    const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const script of ldScripts) {
        try {
            let data = JSON.parse(script.textContent);
            if (!Array.isArray(data)) data = [data];

            for (const item of data) {
                if (item['@type'] === 'JobPosting' || item['@type'] === 'JobListing') {
                    jobs.push({
                        title: item.title || '',
                        company: item.hiringOrganization?.name || '',
                        location: item.jobLocation?.address?.addressLocality ||
                                item.jobLocation?.name || '',
                        description: (item.description || '').replace(/<[^>]*>/g, '').substring(0, 500),
                        url: item.url || window.location.href,
                        source: window.location.hostname.replace('www.', ''),
                        salary_text: item.baseSalary?.value ?
                            '$' + item.baseSalary.value.minValue + ' - $' + item.baseSalary.value.maxValue : '',
                    });
                }
                // Handle ItemList of JobPostings
                if (item['@type'] === 'ItemList' && item.itemListElement) {
                    for (const el of item.itemListElement) {
                        const posting = el.item || el;
                        if (posting['@type'] === 'JobPosting') {
                            jobs.push({
                                title: posting.title || '',
                                company: posting.hiringOrganization?.name || '',
                                location: posting.jobLocation?.address?.addressLocality || '',
                                description: (posting.description || '').replace(/<[^>]*>/g, '').substring(0, 500),
                                url: posting.url || '',
                                source: window.location.hostname.replace('www.', ''),
                            });
                        }
                    }
                }
            }
        } catch(e) {}
    }

    return JSON.stringify(jobs);
})();
"""


def get_extractor(site_key: str) -> str:
    """Get the JavaScript extractor for a given site."""
    return EXTRACTORS.get(site_key, EXTRACTORS["generic"])


def detect_site(url: str) -> str:
    """Detect which site key to use based on URL."""
    url_lower = url.lower()
    if "indeed.com" in url_lower:
        return "indeed"
    elif "glassdoor.com" in url_lower:
        return "glassdoor"
    elif "linkedin.com" in url_lower:
        return "linkedin"
    elif "ziprecruiter.com" in url_lower:
        return "zip_recruiter"
    elif "monster.com" in url_lower:
        return "monster"
    return "generic"


# ── Full-page job detail extractor ────────────────────────────────────────

DETAIL_EXTRACTOR = """
(function() {
    // Extract full job details from a single job posting page
    const result = {
        title: '',
        company: '',
        location: '',
        description: '',
        salary_text: '',
        job_type: '',
        remote_type: '',
        url: window.location.href,
        source: window.location.hostname.replace('www.', ''),
    };

    // Try JSON-LD first
    const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const script of ldScripts) {
        try {
            let data = JSON.parse(script.textContent);
            if (!Array.isArray(data)) data = [data];
            for (const item of data) {
                if (item['@type'] === 'JobPosting') {
                    result.title = item.title || '';
                    result.company = item.hiringOrganization?.name || '';
                    result.location = item.jobLocation?.address?.addressLocality ||
                                    item.jobLocation?.name || '';
                    result.description = (item.description || '').replace(/<[^>]*>/g, '');
                    result.job_type = item.employmentType || '';
                    if (item.baseSalary?.value) {
                        result.salary_text = '$' + (item.baseSalary.value.minValue||'') +
                            ' - $' + (item.baseSalary.value.maxValue||'');
                    }
                    if (item.jobLocationType === 'TELECOMMUTE') {
                        result.remote_type = 'remote';
                    }
                }
            }
        } catch(e) {}
    }

    // Indeed-specific
    if (window.location.hostname.includes('indeed.com')) {
        result.title = result.title || document.querySelector('h1.jobsearch-JobInfoHeader-title, h1[class*="JobTitle"]')?.textContent?.trim() || '';
        result.company = result.company || document.querySelector('div[data-company-name] a, a[data-tn-element="companyName"]')?.textContent?.trim() || '';
        result.location = result.location || document.querySelector('div[data-testid="job-location"], div.jobsearch-InlineCompanyRating + div')?.textContent?.trim() || '';
        result.description = result.description || document.querySelector('#jobDescriptionText, div.jobsearch-JobComponent-description')?.innerText?.trim() || '';
        result.salary_text = result.salary_text || document.querySelector('#salaryInfoAndJobType span, div[id="salaryGuide"]')?.textContent?.trim() || '';
    }

    // LinkedIn-specific
    if (window.location.hostname.includes('linkedin.com')) {
        result.title = result.title || document.querySelector('h1.t-24, h1.jobs-unified-top-card__job-title, h2.t-24')?.textContent?.trim() || '';
        result.company = result.company || document.querySelector('a.jobs-unified-top-card__company-name, span.jobs-unified-top-card__company-name')?.textContent?.trim() || '';
        result.location = result.location || document.querySelector('span.jobs-unified-top-card__bullet, span.jobs-unified-top-card__workplace-type')?.textContent?.trim() || '';
        result.description = result.description || document.querySelector('div.jobs-description__content, div.jobs-box__html-content')?.innerText?.trim() || '';
    }

    // Glassdoor-specific
    if (window.location.hostname.includes('glassdoor.com')) {
        result.title = result.title || document.querySelector('h1[data-test="job-title"], div.css-17x2owp')?.textContent?.trim() || '';
        result.company = result.company || document.querySelector('span[data-test="employer-name"], div.css-16nw49e')?.textContent?.trim() || '';
        result.location = result.location || document.querySelector('span[data-test="location"], div.css-56kyx5')?.textContent?.trim() || '';
        result.description = result.description || document.querySelector('div.jobDescriptionContent, div[data-test="description"]')?.innerText?.trim() || '';
    }

    // Fallback: grab page title and main content
    if (!result.title) {
        result.title = document.querySelector('h1')?.textContent?.trim() || document.title || '';
    }
    if (!result.description) {
        const main = document.querySelector('main, article, [role="main"]');
        if (main) result.description = main.innerText?.substring(0, 3000) || '';
    }

    return JSON.stringify(result);
})();
"""


# ── Scroll / pagination helpers ───────────────────────────────────────────

SCROLL_AND_LOAD = """
(function() {
    // Scroll down to load more results (infinite scroll sites)
    return new Promise((resolve) => {
        let lastHeight = document.body.scrollHeight;
        let scrolls = 0;
        const maxScrolls = 3;

        function doScroll() {
            window.scrollTo(0, document.body.scrollHeight);
            scrolls++;

            setTimeout(() => {
                if (document.body.scrollHeight > lastHeight && scrolls < maxScrolls) {
                    lastHeight = document.body.scrollHeight;
                    doScroll();
                } else {
                    resolve('scrolled ' + scrolls + ' times');
                }
            }, 1500);
        }

        doScroll();
    });
})();
"""

PAGINATION_URLS = """
(function() {
    // Find pagination links for multi-page results
    const links = [];
    const pageLinks = document.querySelectorAll(
        'a[aria-label*="Page"], a[data-testid*="pagination"], ' +
        'nav[role="navigation"] a, ul.pagination a, ' +
        'a.np, a[aria-label="Next"]'
    );
    for (const link of pageLinks) {
        const href = link.getAttribute('href');
        if (href) {
            links.push({
                text: link.textContent?.trim(),
                url: href.startsWith('http') ? href : window.location.origin + href,
            });
        }
    }
    return JSON.stringify(links);
})();
"""
