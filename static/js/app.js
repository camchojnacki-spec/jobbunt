/* ── Jobbunt Frontend - Hinge-style Job Browser ── */

const API = '/api';

function formatSalary(num) {
    if (!num && num !== 0) return '';
    const n = typeof num === 'string' ? parseInt(num.replace(/[^0-9]/g, ''), 10) : num;
    if (isNaN(n) || n === 0) return '';
    return '$' + n.toLocaleString('en-US');
}

function timeAgo(dateStr) {
    if (!dateStr) return '';
    const d = new Date(dateStr);
    if (isNaN(d)) return '';
    const now = new Date();
    const diffMs = now - d;
    if (diffMs < 0) return 'just now';
    const mins = Math.floor(diffMs / 60000);
    if (mins < 60) return mins <= 1 ? 'just now' : `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    if (days < 7) return `${days}d ago`;
    const weeks = Math.floor(days / 7);
    if (weeks < 5) return `${weeks}w ago`;
    const months = Math.floor(days / 30);
    return `${months}mo ago`;
}

let state = {
    profileId: null,
    profile: null,
    swipeStack: [],
    currentCardIndex: 0,
    selectedJobId: null,
    browseMode: 'list', // 'list', 'grid', 'card'
    tags: { roles: [], locations: [], skills: [] },
    parsedProfile: null,
    profileMode: 'paste',
};

// ── Init ──────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
    setupNavigation();
    setupTagInputs();
    setupKeyboard();
    setupFormHandlers();
    setupActionButtons();
    await loadProfile();
});

// ── API helpers ──────────────────────────────────────────────────────────

async function api(path, opts = {}) {
    const url = `${API}${path}`;
    const config = { headers: { 'Content-Type': 'application/json' }, ...opts };
    if (config.body && typeof config.body === 'object' && !(config.body instanceof FormData)) {
        config.body = JSON.stringify(config.body);
    }
    if (config.body instanceof FormData) {
        delete config.headers['Content-Type'];
    }
    const res = await fetch(url, config);
    if (!res.ok) {
        const err = await res.text();
        throw new Error(err);
    }
    return res.json();
}

// ── Navigation ──────────────────────────────────────────────────────────

function setupNavigation() {
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => showView(btn.dataset.view));
    });
}

function showView(name) {
    // Backward compat aliases
    if (name === 'swipe') name = 'hunt';
    if (name === 'scouting') name = 'hunt';
    if (name === 'applications') name = 'pipeline';
    if (name === 'insights') name = 'intel';
    // settings is now a tab within profile, not a separate view
    if (name === 'settings') { name = 'profile'; setTimeout(() => switchProfileTab('settings'), 0); }

    // Exit animation on current active view
    const currentView = document.querySelector('.view.active');
    if (currentView) {
        currentView.classList.add('view-exit');
    }

    document.querySelectorAll('.view').forEach(v => {
        v.classList.remove('active', 'view-enter', 'view-exit');
    });
    document.querySelectorAll('.nav-btn, .nav-link').forEach(b => b.classList.remove('active'));

    const newView = document.getElementById(`view-${name}`);
    if (newView) {
        newView.classList.add('active', 'view-enter');
        setTimeout(() => newView.classList.remove('view-enter'), 200);
    }

    const navEl = document.querySelector(`.nav-link[data-view="${name}"]`) || document.querySelector(`.nav-btn[data-view="${name}"]`);
    if (navEl) navEl.classList.add('active');

    if (name === 'dugout') {
        if (typeof loadStats === 'function') loadStats();
        if (typeof loadDugoutReadiness === 'function') loadDugoutReadiness();
        if (typeof loadDugoutSeasonStats === 'function') loadDugoutSeasonStats();
        if (typeof loadScoutingReport === 'function') loadScoutingReport();
        if (typeof loadReporterCorner === 'function') loadReporterCorner();
        if (typeof loadDugoutCharts === 'function') loadDugoutCharts();
    }
    if (name === 'hunt') loadSwipeStack();
    if (name === 'pipeline') {
        if (typeof loadPipelineData === 'function') loadPipelineData();
    }
    if (name === 'intel') {
        if (typeof loadIntelData === 'function') loadIntelData();
    }
    if (name === 'profile') {
        if (state.profile) populateProfileForm(state.profile);
        if (state.profileId) {
            loadApplyReadiness();
            loadInterviewQuestions();
        }
        loadSettings();
    }
}

// ── Profile ──────────────────────────────────────────────────────────────

async function loadProfile() {
    try {
        const profiles = await api('/profiles');
        populateProfileDropdown(profiles);
        if (profiles.length > 0) {
            state.profile = profiles[0];
            state.profileId = profiles[0].id;
            state.tags.roles = profiles[0].target_roles || [];
            state.tags.locations = profiles[0].target_locations || [];
            state.tags.skills = profiles[0].skills || [];
            updateNavAvatar();
            try {
                if (state.profile.has_profile_doc) {
                    setProfileMode('paste');
                } else {
                    setProfileMode('manual');
                }
            } catch(e) { /* profile view not yet active */ }
            showView('dugout');
        } else {
            document.getElementById('no-profile-state').style.display = 'block';
            document.getElementById('action-bar').style.display = 'none';
            setProfileMode('paste');
        }
    } catch (e) {
        console.error('Failed to load profile:', e);
    }
}

function setProfileMode(mode) {
    state.profileMode = mode;
    const pasteMode = document.getElementById('profile-paste-mode');
    const manualMode = document.getElementById('profile-form');
    const btnPaste = document.getElementById('btn-mode-paste');
    const btnManual = document.getElementById('btn-mode-manual');

    if (mode === 'paste') {
        if (pasteMode) pasteMode.style.display = 'block';
        if (manualMode) manualMode.style.display = 'none';
        if (btnPaste) btnPaste.className = 'btn btn-primary';
        if (btnManual) btnManual.className = 'btn btn-secondary';
    } else {
        if (pasteMode) pasteMode.style.display = 'none';
        if (manualMode) manualMode.style.display = 'block';
        if (btnPaste) btnPaste.className = 'btn btn-secondary';
        if (btnManual) btnManual.className = 'btn btn-primary';
        if (state.profile) populateProfileForm(state.profile);
    }
}

async function parseAndSaveProfile() {
    const text = document.getElementById('f-paste-text').value.trim();
    if (!text) { toast('Please paste your profile document first', 'error'); return; }

    const btn = document.getElementById('btn-parse-profile');
    btn.textContent = 'Parsing...';
    btn.disabled = true;

    try {
        const parsed = await api('/profiles/parse', { method: 'POST', body: { text } });
        state.parsedProfile = parsed;

        const preview = document.getElementById('parsed-preview');
        const content = document.getElementById('parsed-preview-content');

        content.innerHTML = `
            <div style="display:grid; gap:8px">
                <div><strong style="color:var(--accent-light)">Name:</strong> ${esc(parsed.name || 'Not found')}</div>
                <div><strong style="color:var(--accent-light)">Email:</strong> ${esc(parsed.email || 'Not found')}</div>
                <div><strong style="color:var(--accent-light)">Phone:</strong> ${esc(parsed.phone || 'Not found')}</div>
                <div><strong style="color:var(--accent-light)">Location:</strong> ${esc(parsed.location || 'Not found')}</div>
                <div><strong style="color:var(--accent-light)">Target Roles:</strong> ${(parsed.target_roles || []).map(r => `<span class="reason-tag">${esc(r)}</span>`).join(' ') || 'None'}</div>
                <div><strong style="color:var(--accent-light)">Skills:</strong> ${(parsed.skills || []).map(s => `<span class="reason-tag">${esc(s)}</span>`).join(' ') || 'None'}</div>
                <div><strong style="color:var(--accent-light)">Salary:</strong> ${parsed.min_salary ? `$${parsed.min_salary.toLocaleString()} - $${(parsed.max_salary || parsed.min_salary).toLocaleString()}` : 'Not found'}</div>
                <div><strong style="color:var(--accent-light)">Experience:</strong> ${parsed.experience_years ? parsed.experience_years + ' years' : 'Not found'}</div>
            </div>
        `;

        preview.style.display = 'block';
        toast('Profile parsed! Review below and confirm.', 'success');
    } catch (e) {
        toast('Parse failed: ' + e.message, 'error');
    } finally {
        btn.textContent = 'Parse & Save Profile';
        btn.disabled = false;
    }
}

async function confirmParsedProfile() {
    const parsed = state.parsedProfile;
    if (!parsed) return;

    const data = {
        name: parsed.name || 'Unknown',
        email: parsed.email || null,
        phone: parsed.phone || null,
        location: parsed.location || null,
        target_roles: parsed.target_roles || [],
        target_locations: parsed.target_locations || [],
        min_salary: parsed.min_salary || null,
        max_salary: parsed.max_salary || null,
        remote_preference: parsed.remote_preference || 'any',
        experience_years: parsed.experience_years || null,
        skills: parsed.skills || [],
        cover_letter_template: parsed.cover_letter_template || null,
        raw_profile_doc: parsed.raw_profile_doc || null,
    };

    try {
        let profile;
        if (state.profileId) {
            profile = await api(`/profiles/${state.profileId}`, { method: 'PUT', body: data });
        } else {
            profile = await api('/profiles', { method: 'POST', body: data });
        }
        state.profile = profile;
        state.profileId = profile.id;
        state.tags.roles = profile.target_roles || [];
        state.tags.locations = profile.target_locations || [];
        state.tags.skills = profile.skills || [];
        toast('Profile saved!', 'success');

        const resumeInput = document.getElementById('f-resume-paste');
        if (resumeInput && resumeInput.files.length > 0) {
            const formData = new FormData();
            formData.append('file', resumeInput.files[0]);
            await api(`/profiles/${state.profileId}/resume`, { method: 'POST', body: formData });
            toast('Resume uploaded!', 'success');
        }

        document.getElementById('no-profile-state').style.display = 'none';
        document.getElementById('parsed-preview').style.display = 'none';
        updateNavAvatar();
        try { const ps = await api('/profiles'); populateProfileDropdown(ps); } catch(e) { /* ok */ }
    } catch (e) {
        toast('Failed to save profile: ' + e.message, 'error');
    }
}

window.setProfileMode = setProfileMode;
window.parseAndSaveProfile = parseAndSaveProfile;
window.confirmParsedProfile = confirmParsedProfile;

function populateProfileForm(p) {
    document.getElementById('f-name').value = p.name || '';
    document.getElementById('f-email').value = p.email || '';
    document.getElementById('f-phone').value = p.phone || '';
    document.getElementById('f-location').value = p.location || '';
    document.getElementById('f-min-salary').value = p.min_salary || '';
    document.getElementById('f-experience').value = p.experience_years || '';
    document.getElementById('f-remote').value = p.remote_preference || 'any';
    document.getElementById('f-cover-template').value = p.cover_letter_template || '';

    // Tier controls
    const tiersDown = document.getElementById('f-tiers-down');
    const tiersUp = document.getElementById('f-tiers-up');
    if (tiersDown) tiersDown.value = (p.search_tiers_down || 0).toString();
    if (tiersUp) tiersUp.value = (p.search_tiers_up || 0).toString();

    state.tags.roles = p.target_roles || [];
    state.tags.locations = p.target_locations || [];
    state.tags.skills = p.skills || [];
    renderTags('roles');
    renderTags('locations');
    renderTags('skills');
}

function setupFormHandlers() {
    document.getElementById('profile-form')?.addEventListener('submit', async (e) => {
        e.preventDefault();
        await saveProfile();
    });

    document.getElementById('btn-search-jobs')?.addEventListener('click', searchJobs);
    document.getElementById('btn-search-more')?.addEventListener('click', searchJobs);
    document.getElementById('btn-rescore')?.addEventListener('click', rescoreJobs);
}

async function saveProfile() {
    const data = {
        name: document.getElementById('f-name').value,
        email: document.getElementById('f-email').value,
        phone: document.getElementById('f-phone').value,
        location: document.getElementById('f-location').value,
        target_roles: state.tags.roles,
        target_locations: state.tags.locations,
        skills: state.tags.skills,
        min_salary: parseInt(document.getElementById('f-min-salary').value) || null,
        experience_years: parseInt(document.getElementById('f-experience').value) || null,
        remote_preference: document.getElementById('f-remote').value,
        cover_letter_template: document.getElementById('f-cover-template').value,
        raw_profile_doc: state.parsedProfile?.raw_profile_doc || null,
        search_tiers_down: parseInt(document.getElementById('f-tiers-down')?.value) || 0,
        search_tiers_up: parseInt(document.getElementById('f-tiers-up')?.value) || 0,
    };

    try {
        let profile;
        if (state.profileId) {
            profile = await api(`/profiles/${state.profileId}`, { method: 'PUT', body: data });
        } else {
            profile = await api('/profiles', { method: 'POST', body: data });
        }
        state.profile = profile;
        state.profileId = profile.id;
        toast('Profile saved!', 'success');

        const resumeInput = document.getElementById('f-resume');
        if (resumeInput.files.length > 0) {
            const formData = new FormData();
            formData.append('file', resumeInput.files[0]);
            await api(`/profiles/${state.profileId}/resume`, { method: 'POST', body: formData });
            toast('Resume uploaded!', 'success');
        }

        document.getElementById('no-profile-state').style.display = 'none';
        updateNavAvatar();
        try { const ps = await api('/profiles'); populateProfileDropdown(ps); } catch(e) { /* ok */ }
    } catch (e) {
        toast('Failed to save profile: ' + e.message, 'error');
    }
}

async function searchJobs() {
    if (!state.profileId) { toast('Create a profile first', 'error'); return; }
    if (state.searching) return; // Prevent double-click

    state.searching = true;

    // Disable all search buttons and show loading
    const searchBtns = document.querySelectorAll('#btn-search-jobs, #btn-search-more');
    searchBtns.forEach(btn => {
        btn.disabled = true;
        btn._origText = btn.textContent;
        btn.innerHTML = `<svg width="36" height="14" viewBox="0 0 120 40" fill="none" style="display:inline-block;vertical-align:middle;margin-right:4px">
            <style>@keyframes dp{0%,100%{opacity:.15;r:4}50%{opacity:.7;r:5.5}}</style>
            <circle cx="30" cy="20" r="4" fill="#C4962C" style="animation:dp 1.2s ease-in-out infinite"/>
            <circle cx="60" cy="20" r="4" fill="#C4962C" style="animation:dp 1.2s ease-in-out .2s infinite"/>
            <circle cx="90" cy="20" r="4" fill="#C4962C" style="animation:dp 1.2s ease-in-out .4s infinite"/>
        </svg>Searching...`;
    });

    try {
        // Get selected sources from checkboxes
        const selectedSources = Array.from(document.querySelectorAll('#source-selector input:checked'))
            .map(cb => cb.value);
        const sourceParams = selectedSources.length > 0
            ? '?' + selectedSources.map(s => `sources=${s}`).join('&')
            : '';
        const result = await api(`/profiles/${state.profileId}/search${sourceParams}`, { method: 'POST' });
        toast(`Found ${result.new_jobs} new jobs (${result.duplicates_skipped} duplicates skipped)`, 'success');

        // Update button to show verification phase
        searchBtns.forEach(btn => {
            btn.innerHTML = `<svg width="36" height="14" viewBox="0 0 120 40" fill="none" style="display:inline-block;vertical-align:middle;margin-right:4px">
            <style>@keyframes dp2{0%,100%{opacity:.15;r:4}50%{opacity:.7;r:5.5}}</style>
            <circle cx="30" cy="20" r="4" fill="#3DB87A" style="animation:dp2 1.2s ease-in-out infinite"/>
            <circle cx="60" cy="20" r="4" fill="#3DB87A" style="animation:dp2 1.2s ease-in-out .2s infinite"/>
            <circle cx="90" cy="20" r="4" fill="#3DB87A" style="animation:dp2 1.2s ease-in-out .4s infinite"/>
        </svg>Verifying...`;
        });

        // Verify pending jobs are still active
        try {
            showActivity('Verifying job links...');
            const verifyResult = await api(`/profiles/${state.profileId}/verify-pending`, { method: 'POST' });
            if (verifyResult.expired > 0) {
                toast(`${verifyResult.expired} expired jobs filtered out`, 'info');
            }
        } catch (e) {
            console.warn('Verification pass failed:', e);
        }

        // AI-powered duplicate reconciliation
        try {
            showActivity('AI dedup reconciliation...');
            const dedupResult = await api(`/profiles/${state.profileId}/reconcile-duplicates`, { method: 'POST' });
            if (dedupResult.merged > 0) {
                toast(`AI merged ${dedupResult.merged} duplicate listings`, 'info');
            }
        } catch (e) {
            console.warn('AI dedup reconciliation failed (non-fatal):', e);
        }

        await loadSwipeStack();
        loadStats();
        showView('swipe');
    } catch (e) {
        toast('Search failed: ' + e.message, 'error');
    } finally {
        state.searching = false;
        searchBtns.forEach(btn => {
            btn.disabled = false;
            btn.textContent = btn._origText || 'Search for Jobs';
        });
    }
}

async function rescoreJobs() {
    if (!state.profileId) { toast('Create a profile first', 'error'); return; }
    const btn = document.getElementById('btn-rescore');
    const settingsBtn = document.getElementById('btn-rescore-settings');
    const allBtns = [btn, settingsBtn].filter(Boolean);

    allBtns.forEach(b => { b.disabled = true; });

    // Show progress overlay
    showProgressOverlay('Rescoring Jobs', 'Preparing...');

    try {
        // Kick off background rescore (returns immediately)
        const start = await api(`/profiles/${state.profileId}/rescore`, { method: 'POST' });
        const total = start.total || 0;
        updateProgressOverlay(0, `0 of ${total} jobs...`);

        // Poll progress until done
        await new Promise((resolve, reject) => {
            const pollInterval = setInterval(async () => {
                try {
                    const progress = await api(`/profiles/${state.profileId}/rescore-progress`);
                    if (progress.status === 'running' && progress.total > 0) {
                        const pct = Math.round((progress.current / progress.total) * 100);
                        updateProgressOverlay(pct, `${progress.current} of ${progress.total} jobs scored`);
                    } else if (progress.status === 'done') {
                        clearInterval(pollInterval);
                        updateProgressOverlay(100, `Done! ${progress.current} jobs rescored`);
                        setTimeout(() => { hideProgressOverlay(); resolve(); }, 1200);
                    } else if (progress.status === 'error') {
                        clearInterval(pollInterval);
                        hideProgressOverlay();
                        reject(new Error('Rescore failed on server'));
                    }
                } catch (e) { /* ignore poll errors */ }
            }, 2000);

            // Safety timeout: 10 minutes max
            setTimeout(() => { clearInterval(pollInterval); resolve(); }, 600000);
        });

        toast(`Rescored ${total} jobs with updated engine`, 'success');
        await loadSwipeStack();
        loadStats();
    } catch (e) {
        hideProgressOverlay();
        toast('Rescore failed: ' + e.message, 'error');
    } finally {
        allBtns.forEach(b => { b.disabled = false; });
    }
}

function showProgressOverlay(title, subtitle) {
    let overlay = document.getElementById('progress-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'progress-overlay';
        overlay.className = 'progress-overlay';
        document.body.appendChild(overlay);
    }
    overlay.innerHTML = `
        <div class="progress-modal">
            <div class="progress-icon">
                <svg width="60" height="60" viewBox="0 0 120 120" fill="none">
                    <style>@keyframes orbit-spin{to{transform:rotate(360deg)}}@keyframes plate-pulse{0%,100%{opacity:.15}50%{opacity:.35}}</style>
                    <path d="M55 56 L60 52 L65 56 L63 62 L57 62 Z" fill="#C4962C" style="animation:plate-pulse 1.8s ease-in-out infinite"/>
                    <g style="animation:orbit-spin 1.8s linear infinite;transform-origin:60px 60px">
                        <circle cx="60" cy="28" r="5" fill="none" stroke="#C4962C" stroke-width="1" opacity="0.5"/>
                        <path d="M57.5 25.5 Q60 28 57.5 30.5" fill="none" stroke="#8B6914" stroke-width="0.4" opacity="0.4"/>
                        <path d="M62.5 25.5 Q60 28 62.5 30.5" fill="none" stroke="#8B6914" stroke-width="0.4" opacity="0.4"/>
                    </g>
                    <circle cx="60" cy="60" r="32" fill="none" stroke="#E8E6E1" stroke-width="0.3" opacity="0.04" stroke-dasharray="3 5"/>
                </svg>
            </div>
            <div class="progress-title">${esc(title)}</div>
            <div class="progress-bar-track"><div class="progress-bar-fill" id="progress-fill" style="width:0%"></div></div>
            <div class="progress-subtitle" id="progress-subtitle">${esc(subtitle)}</div>
        </div>
    `;
    overlay.classList.add('show');
}

function updateProgressOverlay(pct, text) {
    const fill = document.getElementById('progress-fill');
    const sub = document.getElementById('progress-subtitle');
    if (fill) fill.style.width = `${pct}%`;
    if (sub) sub.textContent = text;
}

function hideProgressOverlay() {
    const overlay = document.getElementById('progress-overlay');
    if (overlay) overlay.classList.remove('show');
    setTimeout(() => overlay?.remove(), 300);
}

// ── Stats ────────────────────────────────────────────────────────────────

async function loadStats() {
    if (!state.profileId) return;
    try {
        const stats = await api(`/profiles/${state.profileId}/stats`);
        const fmtPct = (v) => { const s = (v || 0).toFixed(3); return s.startsWith('0') ? s.substring(1) : s; };

        const ab = stats.at_bats || stats.total_jobs || 0;
        const h = stats.hits || 0;
        const bb = stats.walks || stats.shortlisted || 0;
        const k = stats.strikeouts || stats.passed || 0;
        const avg = stats.avg || 0;
        const obp = stats.obp || 0;
        const slg = stats.slg || 0;
        const ops = stats.ops || 0;

        document.getElementById('stats-bar').innerHTML = `
            <div class="stat-item" data-tip="Jobs waiting for review">
                <div class="stat-value">${stats.pending_swipe}</div><div class="stat-label">On Deck</div>
            </div>
            <div class="stat-item" data-tip="At Bats — total jobs you've seen">
                <div class="stat-value">${ab}</div><div class="stat-label">AB</div>
            </div>
            <div class="stat-item" data-tip="Hits — jobs you liked or applied to">
                <div class="stat-value" style="color:var(--green)">${h}</div><div class="stat-label">H</div>
            </div>
            <div class="stat-item" data-tip="Base on Balls — jobs shortlisted for later">
                <div class="stat-value" style="color:var(--bright)">${bb}</div><div class="stat-label">BB</div>
            </div>
            <div class="stat-item" data-tip="Strikeouts — jobs you passed on">
                <div class="stat-value" style="color:var(--text-dim)">${k}</div><div class="stat-label">K</div>
            </div>
            <div class="stat-item" data-tip="Batting Average — hit rate (H ÷ AB)">
                <div class="stat-value" style="color:var(--bright)">${fmtPct(avg)}</div><div class="stat-label">AVG</div>
            </div>
            <div class="stat-item" data-tip="On-Base Percentage — engagement rate ((H+BB) ÷ AB)">
                <div class="stat-value" style="color:var(--info)">${fmtPct(obp)}</div><div class="stat-label">OBP</div>
            </div>
            <div class="stat-item" data-tip="Slugging — weighted application impact">
                <div class="stat-value" style="color:var(--green)">${fmtPct(slg)}</div><div class="stat-label">SLG</div>
            </div>
            <div class="stat-item" data-tip="OPS — overall effectiveness (OBP + SLG). .800+ is All-Star level!">
                <div class="stat-value" style="color:${ops >= 0.800 ? 'var(--green)' : ops >= 0.500 ? 'var(--bright)' : 'var(--text-dim)'}">${fmtPct(ops)}</div><div class="stat-label">OPS</div>
            </div>
        `;

        // Baseball card blurb — AI-generated "back of the card" flavor text
        renderBaseballCardBlurb(stats);
    } catch (e) {
        console.error('Stats load failed:', e);
    }
}

function renderBaseballCardBlurb(stats) {
    const blurb = document.getElementById('baseball-card-blurb');
    if (!blurb) return;

    if (!state.profile) { blurb.style.display = 'none'; return; }

    const p = state.profile;
    const name = (p.name || 'Unknown').split(' ');
    const firstName = name[0];
    const lastName = name.slice(1).join(' ') || '';
    const ab = stats.at_bats || stats.total_jobs || 0;
    const ops = stats.ops || 0;
    const yrs = p.experience_years || '?';
    const roles = (p.target_roles || []).slice(0, 2);
    const loc = p.location || 'Unknown';

    // Scouting report based on OPS
    let scouting = '';
    if (ops >= 0.800) scouting = 'All-Star caliber — making contact on every swing.';
    else if (ops >= 0.600) scouting = 'Solid contact hitter with upside. Working counts well.';
    else if (ops >= 0.300) scouting = 'Finding the zone. Patience at the plate will pay off.';
    else if (ab > 0) scouting = 'Early in the count. Settling in at the plate.';
    else scouting = 'Stepping up to the plate for the first time.';

    const roleText = roles.length > 0 ? roles.join(' · ') : 'Free Agent';

    blurb.innerHTML = `
        <svg class="card-diamond-icon" width="36" height="36" viewBox="0 0 64 64" fill="none">
            <path d="M32 4 L58 32 L32 60 L6 32 Z" fill="rgba(74,144,217,0.06)" stroke="rgba(74,144,217,0.15)" stroke-width="0.8"/>
            <circle cx="32" cy="4" r="3" fill="#4A90D9" opacity="0.7"/>
            <circle cx="58" cy="32" r="3" fill="#4A90D9" opacity="0.4"/>
            <circle cx="32" cy="60" r="3" fill="#B8211A" opacity="0.8"/>
            <circle cx="6" cy="32" r="3" fill="#4A90D9" opacity="0.4"/>
            <line x1="32" y1="60" x2="58" y2="32" stroke="#4A90D9" stroke-width="0.4" opacity="0.2"/>
            <line x1="32" y1="60" x2="6" y2="32" stroke="#4A90D9" stroke-width="0.4" opacity="0.2"/>
        </svg>
        <div class="card-blurb-text">
            <div class="card-blurb-name">${esc(firstName)} ${esc(lastName)}</div>
            <div class="card-blurb-role">${esc(roleText)} · ${yrs} yrs · ${esc(loc)}</div>
            <div class="card-blurb-scouting">${scouting}</div>
        </div>
    `;
    blurb.style.display = 'flex';
}

// ── Browse Views ────────────────────────────────────────────────────────

async function loadSwipeStack() {
    if (!state.profileId) return;
    try {
        state.swipeStack = await api(`/profiles/${state.profileId}/swipe?limit=50`);
        state.currentCardIndex = 0;
        state.selectedJobId = state.swipeStack.length > 0 ? state.swipeStack[0].id : null;
        renderBrowseView();
    } catch (e) {
        console.error('Failed to load swipe stack:', e);
    }
}

function switchBrowseMode(mode) {
    state.browseMode = mode;
    // Update toggle buttons
    document.querySelectorAll('.view-toggle-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.browseMode === mode);
    });
    renderBrowseView();
}

function renderBrowseView() {
    const listView = document.getElementById('job-list-view');
    const gridView = document.getElementById('job-grid-view');
    const feed = document.getElementById('job-feed');
    const actionBar = document.getElementById('action-bar');
    const empty = document.getElementById('empty-state');
    const noProfile = document.getElementById('no-profile-state');
    const toolbar = document.getElementById('browse-toolbar');
    const countEl = document.getElementById('browse-count');

    noProfile.style.display = 'none';

    const jobs = state.swipeStack;
    if (!jobs || jobs.length === 0) {
        listView.style.display = 'none';
        gridView.style.display = 'none';
        feed.style.display = 'none';
        actionBar.style.display = 'none';
        toolbar.style.display = 'none';
        empty.style.display = 'block';
        return;
    }

    empty.style.display = 'none';
    toolbar.style.display = 'flex';
    actionBar.style.display = 'flex';
    countEl.textContent = `${jobs.length} jobs`;

    if (state.browseMode === 'list') {
        listView.style.display = 'block';
        gridView.style.display = 'none';
        feed.style.display = 'none';
        renderJobList(jobs);
    } else if (state.browseMode === 'grid') {
        listView.style.display = 'none';
        gridView.style.display = 'block';
        feed.style.display = 'none';
        renderJobGrid(jobs);
    } else {
        // Card mode — existing single-card swipe view
        listView.style.display = 'none';
        gridView.style.display = 'none';
        feed.style.display = 'block';
        renderCurrentCard();
    }
}

function renderCurrentCard() {
    const feed = document.getElementById('job-feed');
    feed.innerHTML = '';

    const remaining = state.swipeStack.slice(state.currentCardIndex);
    if (remaining.length === 0) {
        document.getElementById('action-bar').style.display = 'none';
        document.getElementById('empty-state').style.display = 'block';
        return;
    }

    const job = remaining[0];
    state.selectedJobId = job.id;
    const card = buildJobCard(job);
    feed.appendChild(card);

    // Counter
    const counter = document.createElement('div');
    counter.className = 'job-counter';
    counter.textContent = `${state.currentCardIndex + 1} of ${state.swipeStack.length}`;
    feed.appendChild(counter);

    window.scrollTo({ top: 0, behavior: 'smooth' });
}

function renderJobList(jobs) {
    const list = document.getElementById('job-list');
    list.innerHTML = jobs.map((job, i) => {
        const scoreColor = job.match_score >= 70 ? 'var(--green)' :
                           job.match_score >= 40 ? 'var(--orange)' : 'var(--red)';
        const sources = (job.sources_seen && job.sources_seen.length > 0 ? job.sources_seen : [job.source || 'unknown']);
        const sourceBadges = sources.map(s => `<span class="card-source source-${s.toLowerCase()}">${esc(s)}</span>`).join('');
        const selected = job.id === state.selectedJobId ? 'selected' : '';
        const salary = job.salary_text ? `<span class="list-salary">${esc(job.salary_text)}</span>` : '';

        return `<div class="job-list-row ${selected}" data-job-id="${job.id}" onclick="selectJobFromList(${job.id}, ${i})">
            <div class="list-score" style="color:${scoreColor}">${Math.round(job.match_score)}</div>
            <div class="list-main">
                <div class="list-title">${esc(job.title)}</div>
                <div class="list-meta">
                    <span class="list-company">${esc(job.company)}</span>
                    <span class="list-location">${esc(job.location || '')}</span>
                    ${salary}
                </div>
            </div>
            <div class="list-badges">${sourceBadges}</div>
            <div class="list-actions">
                <button class="list-action-btn pass" onclick="event.stopPropagation(); quickAction(${job.id}, ${i}, 'pass')" title="Pass">✕</button>
                <button class="list-action-btn shortlist" onclick="event.stopPropagation(); quickAction(${job.id}, ${i}, 'shortlist')" title="Shortlist">⭐</button>
                <button class="list-action-btn apply" onclick="event.stopPropagation(); quickAction(${job.id}, ${i}, 'like')" title="Apply">✓</button>
            </div>
        </div>`;
    }).join('');
}

function renderJobGrid(jobs) {
    const grid = document.getElementById('job-grid');
    grid.innerHTML = jobs.map((job, i) => {
        const scoreColor = job.match_score >= 70 ? 'var(--green)' :
                           job.match_score >= 40 ? 'var(--orange)' : 'var(--red)';
        const fitLabel = job.match_score >= 80 ? 'Excellent' :
                         job.match_score >= 65 ? 'Strong' :
                         job.match_score >= 50 ? 'Decent' :
                         job.match_score >= 35 ? 'Weak' : 'Poor';
        const sources = (job.sources_seen && job.sources_seen.length > 0 ? job.sources_seen : [job.source || 'unknown']);
        const sourceBadges = sources.map(s => `<span class="card-source source-${s.toLowerCase()}">${esc(s)}</span>`).join('');
        const selected = job.id === state.selectedJobId ? 'selected' : '';
        const reasons = (job.match_reasons || []).filter(r => !r.startsWith('⚠')).slice(0, 2);

        // Compact info row details
        const postedAgo = job.posted_date ? timeAgo(job.posted_date) : '';
        const addedAgo = job.created_at ? timeAgo(job.created_at) : (job.scraped_at ? timeAgo(job.scraped_at) : '');
        const timesSeen = sources.length;
        const remoteType = job.remote_type || '';
        const salaryBrief = job.salary_text ? esc(job.salary_text) :
                            (job.salary_min ? `${formatSalary(job.salary_min)}${job.salary_max ? '–' + formatSalary(job.salary_max) : ''}${job.salary_estimated ? ' (est)' : ''}` : '');
        const breakdownHint = job.match_breakdown ? Object.entries(job.match_breakdown).slice(0, 2).map(([k, v]) => `${k.replace(/_/g,' ')} ${Math.round(v)}`).join(' · ') : '';

        const gridListingStatus = job.url_valid === false
            ? '<span class="listing-pill listing-closed">Closed</span>'
            : '<span class="listing-pill listing-active">Active</span>';

        return `<div class="job-grid-card ${selected}${job.url_valid === false ? ' listing-is-closed' : ''}" data-job-id="${job.id}" onclick="selectJobFromList(${job.id}, ${i})">
            <div class="grid-header">
                <div class="grid-score" style="color:${scoreColor}">${Math.round(job.match_score)}</div>
                <div class="grid-sources">${gridListingStatus} ${sourceBadges}</div>
            </div>
            <div class="grid-title">${esc(job.title)}</div>
            <div class="grid-company">${esc(job.company)}</div>
            <div class="grid-location">${esc(job.location || '')}</div>
            <div class="grid-info-row">
                ${postedAgo ? `<span class="grid-info-item" title="Date posted">📅 ${postedAgo}</span>` : ''}
                ${addedAgo ? `<span class="grid-info-item" title="Added to Jobbunt">📥 ${addedAgo}</span>` : ''}
                ${timesSeen > 1 ? `<span class="grid-info-item" title="Seen on ${timesSeen} sources">👁 ${timesSeen} sources</span>` : ''}
                ${remoteType ? `<span class="grid-info-item grid-remote-badge grid-remote-${remoteType.toLowerCase().replace(/[^a-z]/g,'')}">🏠 ${esc(remoteType)}</span>` : ''}
            </div>
            ${salaryBrief ? `<div class="grid-salary">${salaryBrief}</div>` : ''}
            ${breakdownHint ? `<div class="grid-breakdown" title="Score breakdown">${breakdownHint}</div>` : ''}
            ${reasons.length > 0 ? `<div class="grid-reasons">${reasons.map(r => `<div class="grid-reason">${esc(r)}</div>`).join('')}</div>` : ''}
            <div class="grid-actions">
                <button class="list-action-btn pass" onclick="event.stopPropagation(); quickAction(${job.id}, ${i}, 'pass')" title="Pass">✕</button>
                <button class="list-action-btn shortlist" onclick="event.stopPropagation(); quickAction(${job.id}, ${i}, 'shortlist')" title="Shortlist">⭐</button>
                <button class="list-action-btn apply" onclick="event.stopPropagation(); quickAction(${job.id}, ${i}, 'like')" title="Apply">✓</button>
            </div>
        </div>`;
    }).join('');
}

function selectJobFromList(jobId, index) {
    state.selectedJobId = jobId;
    state.currentCardIndex = index;

    // Update selection highlight
    document.querySelectorAll('.job-list-row, .job-grid-card').forEach(el => {
        el.classList.toggle('selected', parseInt(el.dataset.jobId) === jobId);
    });

    // Show detail panel for this job
    const job = state.swipeStack.find(j => j.id === jobId);
    if (job) showJobDetailPanel(job);
}

function showJobDetailPanel(job) {
    // Remove existing panel
    document.getElementById('job-detail-panel')?.remove();

    const card = buildJobCard(job);
    const panel = document.createElement('div');
    panel.id = 'job-detail-panel';
    panel.className = 'job-detail-panel';
    panel.innerHTML = `<div class="detail-panel-header">
        <button class="btn btn-sm btn-ghost" onclick="closeJobDetail()">← Back to list</button>
        <span class="job-counter">${state.currentCardIndex + 1} of ${state.swipeStack.length}</span>
    </div>`;
    panel.appendChild(card);
    document.querySelector('.browse-container').appendChild(panel);

    // Hide list/grid while detail is showing
    const listView = document.getElementById('job-list-view');
    const gridView = document.getElementById('job-grid-view');
    if (state.browseMode === 'list') listView.style.display = 'none';
    if (state.browseMode === 'grid') gridView.style.display = 'none';
}

function closeJobDetail() {
    document.getElementById('job-detail-panel')?.remove();
    if (state.browseMode === 'list') document.getElementById('job-list-view').style.display = 'block';
    if (state.browseMode === 'grid') document.getElementById('job-grid-view').style.display = 'block';
}

async function quickAction(jobId, index, action) {
    const job = state.swipeStack.find(j => j.id === jobId);
    if (!job) return;

    // Remove from stack visually
    state.swipeStack = state.swipeStack.filter(j => j.id !== jobId);

    let loadingToastId = null;
    if (action === 'like') {
        loadingToastId = toast(`Preparing application for ${job.title}...`, 'loading');
        setActionButtonsEnabled(false);
    } else if (action === 'shortlist') {
        toast(`Shortlisted ${job.title}`, 'success');
    } else {
        toast('Passed', 'info');
    }

    // Re-render current view
    renderBrowseView();

    try {
        const result = await api(`/jobs/${job.id}/swipe`, {
            method: 'POST',
            body: { action },
        });

        if (action === 'like') {
            if (loadingToastId) dismissToast(loadingToastId);
            if (result.status === 'failed') {
                toast(`Cannot apply: ${result.agent_result?.error || 'Job may no longer be active'}`, 'error');
            } else if (result.agent_result?.status === 'needs_input') {
                toast(`Applying to ${job.title} - need some info first`, 'info');
                showQuestions(result.agent_result.questions, result.application.id);
            } else if (result.agent_result?.status === 'ready') {
                toast(`Application ready for ${job.title}!`, 'success');
            } else {
                toast(`Applied to ${job.title}`, 'success');
            }
        }

        loadStats();
    } catch (e) {
        if (loadingToastId) dismissToast(loadingToastId);
        toast('Action failed: ' + e.message, 'error');
    }

    if (action === 'like') setActionButtonsEnabled(true);
}

function buildJobCard(job) {
    const card = document.createElement('div');
    card.className = 'job-card';
    card.dataset.jobId = job.id;

    const scoreColor = job.match_score >= 70 ? 'var(--green)' :
                        job.match_score >= 40 ? 'var(--orange)' : 'var(--red)';
    const scoreClass = job.match_score >= 70 ? 'score-high' :
                        job.match_score >= 40 ? 'score-mid' : 'score-low';
    const fitLabel = job.match_score >= 80 ? 'Excellent Match' :
                     job.match_score >= 65 ? 'Strong Match' :
                     job.match_score >= 50 ? 'Decent Match' :
                     job.match_score >= 35 ? 'Weak Match' : 'Poor Match';
    const sourceClass = `source-${(job.source || 'unknown').toLowerCase()}`;
    const allReasons = job.match_reasons || [];
    const reasons = allReasons.filter(r => !r.startsWith('⚠')).slice(0, 4);
    const concerns = allReasons.filter(r => r.startsWith('⚠')).slice(0, 2);
    const co = job.company_data;

    // Company logo - multiple fallback sources
    const logoColors = ['#C4962C','#d4aa4f','#e17055','#4caf50','#8B6914','#ef5350','#0984e3','#ff9800','#d63031','#2196f3'];
    const logoColor = logoColors[Math.abs([...job.company].reduce((a,c) => a + c.charCodeAt(0), 0)) % logoColors.length];
    const logoInitials = job.company.split(/\s+/).filter(w => w.length > 0).slice(0,2).map(w => w[0]).join('').toUpperCase();
    const companyDomain = co?.website ? co.website.replace(/^https?:\/\//, '').replace(/\/.*$/, '') : null;

    // URL status
    let urlStatusHtml = '';
    if (job.url_valid === true) urlStatusHtml = '<span class="card-url-status url-valid">Active</span>';
    else if (job.url_valid === false) urlStatusHtml = '<span class="card-url-status url-invalid">May be expired</span>';
    else urlStatusHtml = '<span class="card-url-status url-unchecked">Unchecked</span>';

    let html = '';

    // Sources badges
    const sources = job.sources_seen && job.sources_seen.length > 0
        ? job.sources_seen
        : [job.source || 'unknown'];
    const sourceBadges = sources.map(s => {
        const cls = `source-${s.toLowerCase()}`;
        return `<span class="card-source ${cls}">${esc(s)}</span>`;
    }).join('');

    // Freshness label
    let freshnessHtml = '';
    if (job.scraped_at) {
        const scraped = new Date(job.scraped_at);
        const now = new Date();
        const hoursAgo = Math.floor((now - scraped) / (1000 * 60 * 60));
        if (hoursAgo < 1) freshnessHtml = '<span class="freshness fresh">Just found</span>';
        else if (hoursAgo < 24) freshnessHtml = `<span class="freshness fresh">Found ${hoursAgo}h ago</span>`;
        else {
            const daysAgo = Math.floor(hoursAgo / 24);
            freshnessHtml = `<span class="freshness ${daysAgo <= 3 ? 'recent' : 'stale'}">Found ${daysAgo}d ago</span>`;
        }
    }

    // ── Hero ──
    html += `<div class="card-hero">
        <div class="card-source-row">
            <div class="card-sources">${sourceBadges}</div>
            <div class="card-status-row">
                ${freshnessHtml}
                ${urlStatusHtml}
            </div>
        </div>
        <div class="card-title">${esc(job.title)}</div>
        <div class="card-company"><span class="company-logo" style="background:${logoColor}" id="logo-${job.id}">${companyDomain
            ? `<img src="https://logo.clearbit.com/${esc(companyDomain)}?size=64" alt="" onerror="this.onerror=function(){this.parentElement.textContent='${logoInitials}'};this.src='https://www.google.com/s2/favicons?domain=${esc(companyDomain)}&sz=64'">`
            : logoInitials}</span>${esc(job.company)}</div>
        <div class="card-meta">
            ${job.location ? `<span class="card-meta-item">${esc(job.location)}</span>` : ''}
            ${job.remote_type ? `<span class="card-meta-item">${esc(job.remote_type)}</span>` : ''}
            ${job.job_type ? `<span class="card-meta-item">${esc(job.job_type)}</span>` : ''}
            ${job.posted_date ? `<span class="card-meta-item">${esc(job.posted_date)}</span>` : ''}
            ${sources.length > 1 ? `<span class="card-meta-item multi-source">Found on ${sources.length} sites</span>` : ''}
        </div>
    </div>`;

    // ── Score with Ring + Breakdown ──
    const bd = job.match_breakdown;
    html += `<div class="card-score-section">
        <div class="score-header">
            <div class="score-ring ${scoreClass}">${Math.round(job.match_score)}</div>
            <div class="score-summary">
                <div class="score-fit-label">${fitLabel}</div>
                <div class="score-fit-sublabel">Overall fit score</div>
            </div>
        </div>
        ${reasons.length || concerns.length ? `<div class="card-reasons">
            ${reasons.map(r => `<span class="reason-tag">${esc(r)}</span>`).join('')}
            ${concerns.map(c => `<span class="reason-tag concern">${esc(c)}</span>`).join('')}
        </div>` : ''}
        ${bd ? `<div class="match-breakdown">
            ${renderBreakdownBar('Role Fit', bd.role_fit)}
            ${renderBreakdownBar('Skills', bd.skills)}
            ${renderBreakdownBar('Location', bd.location)}
            ${renderBreakdownBar('Compensation', bd.compensation)}
            ${renderBreakdownBar('Seniority', bd.seniority)}
            ${renderBreakdownBar('Culture Fit', bd.culture_fit)}
            ${bd.research_fit !== undefined ? renderBreakdownBar('Deep Research', bd.research_fit) : ''}
        </div>` : ''}
    </div>`;

    // ── Salary ──
    if (job.salary_text || job.salary_min) {
        const salaryText = job.salary_text || `$${(job.salary_min||0).toLocaleString()} - $${(job.salary_max||0).toLocaleString()}`;
        html += `<div class="card-section">
            <div class="card-section-title"><span class="section-icon">💰</span>Compensation</div>
            <div class="salary-display">
                <span class="salary-amount">${esc(salaryText)}</span>
                ${job.salary_estimated ? '<span class="salary-estimated">Estimated</span>' : ''}
            </div>
        </div>`;
    }

    // ── Role Details ──
    const hasLevel = job.seniority_level && job.seniority_level !== 'null';
    const hasReports = job.reports_to && job.reports_to !== 'null';
    const hasTeam = job.team_size && job.team_size !== 'null';
    if (hasLevel || hasReports || hasTeam) {
        html += `<div class="card-section">
            <div class="card-section-title"><span class="section-icon">🎯</span>Role Details</div>
            <div class="detail-chips">
                ${hasLevel ? `<span class="detail-chip"><span class="detail-chip-icon">📊</span><span class="detail-chip-label">Level</span>${esc(job.seniority_level)}</span>` : ''}
                ${hasReports ? `<span class="detail-chip"><span class="detail-chip-icon">👤</span><span class="detail-chip-label">Reports to</span>${esc(job.reports_to)}</span>` : ''}
                ${hasTeam ? `<span class="detail-chip"><span class="detail-chip-icon">👥</span><span class="detail-chip-label">Team</span>${esc(job.team_size)}</span>` : ''}
            </div>
        </div>`;
    }

    // ── Timeline ──
    const hasTimeline = job.posted_date || job.closing_date || job.scraped_at;
    if (hasTimeline) {
        html += `<div class="card-section">
            <div class="card-section-title"><span class="section-icon">📅</span>Timeline</div>
            <div class="timeline-row">`;
        if (job.posted_date) {
            html += `<div class="timeline-item">
                <span class="timeline-icon">📤</span>
                <div class="timeline-content">
                    <span class="timeline-label">Posted</span>
                    <span class="timeline-value">${esc(job.posted_date)}</span>
                </div>
            </div>`;
        }
        if (job.closing_date) {
            let urgencyClass = '';
            let urgencyIcon = '📋';
            try {
                const closeDate = new Date(job.closing_date);
                const now = new Date();
                const daysLeft = Math.ceil((closeDate - now) / (1000 * 60 * 60 * 24));
                if (!isNaN(daysLeft)) {
                    if (daysLeft <= 3) { urgencyClass = 'urgent'; urgencyIcon = '🔴'; }
                    else if (daysLeft <= 7) { urgencyClass = 'soon'; urgencyIcon = '🟡'; }
                    else { urgencyIcon = '🟢'; }
                }
            } catch(e) {}
            html += `<div class="timeline-item ${urgencyClass}">
                <span class="timeline-icon">${urgencyIcon}</span>
                <div class="timeline-content">
                    <span class="timeline-label">Closes</span>
                    <span class="timeline-value">${esc(job.closing_date)}</span>
                </div>
            </div>`;
        }
        if (job.scraped_at) {
            const d = new Date(job.scraped_at);
            html += `<div class="timeline-item">
                <span class="timeline-icon">🔍</span>
                <div class="timeline-content">
                    <span class="timeline-label">Found</span>
                    <span class="timeline-value">${d.toLocaleDateString()}</span>
                </div>
            </div>`;
        }
        html += `</div></div>`;
    }

    // ── Role Summary ──
    if (job.role_summary) {
        html += `<div class="card-section">
            <div class="card-section-title"><span class="section-icon">📝</span>What You'll Do</div>
            <div class="role-summary">${esc(job.role_summary)}</div>
        </div>`;
    }

    // ── Why Apply ──
    const whyApply = job.why_apply || [];
    if (whyApply.length > 0) {
        html += `<div class="card-section">
            <div class="card-section-title"><span class="section-icon">✨</span>Why Apply</div>
            <ul class="insight-list why-apply-list">
                ${whyApply.map(w => `<li>${esc(w)}</li>`).join('')}
            </ul>
        </div>`;
    }

    // ── Red Flags ──
    const redFlags = job.red_flags || [];
    if (redFlags.length > 0) {
        html += `<div class="card-section">
            <div class="card-section-title"><span class="section-icon">⚠️</span>Watch Out</div>
            <ul class="insight-list red-flag-list">
                ${redFlags.map(f => `<li>${esc(f)}</li>`).join('')}
            </ul>
        </div>`;
    }

    // ── Company Profile ──
    if (co) {
        html += `<div class="card-section">
            <div class="card-section-title"><span class="section-icon">🏢</span>About ${esc(co.name)}</div>
            <div class="company-profile">`;

        // Ratings row
        const hasRatings = co.glassdoor_rating || co.indeed_rating;
        if (hasRatings) {
            html += '<div class="company-ratings">';
            if (co.glassdoor_rating) {
                html += `<div class="rating-item">
                    <div class="rating-value">${co.glassdoor_rating.toFixed(1)}</div>
                    <div class="rating-stars">${renderStars(co.glassdoor_rating)}</div>
                    <div class="rating-label">Glassdoor</div>
                    ${co.glassdoor_reviews_count ? `<div class="rating-label">${co.glassdoor_reviews_count.toLocaleString()} reviews</div>` : ''}
                </div>`;
            }
            if (co.indeed_rating) {
                html += `<div class="rating-item">
                    <div class="rating-value">${co.indeed_rating.toFixed(1)}</div>
                    <div class="rating-stars">${renderStars(co.indeed_rating)}</div>
                    <div class="rating-label">Indeed</div>
                </div>`;
            }
            if (co.recommend_pct) {
                html += `<div class="rating-item">
                    <div class="rating-value">${Math.round(co.recommend_pct)}%</div>
                    <div class="rating-label">Recommend</div>
                </div>`;
            }
            if (co.ceo_approval) {
                html += `<div class="rating-item">
                    <div class="rating-value">${Math.round(co.ceo_approval)}%</div>
                    <div class="rating-label">CEO Approval</div>
                </div>`;
            }
            html += '</div>';
        }

        // Company Scorecard
        const sc = co.scorecard;
        if (sc && sc.overall) {
            const scOverallColor = sc.overall >= 70 ? 'var(--green)' : sc.overall >= 50 ? 'var(--orange)' : 'var(--red)';
            html += `<div class="company-scorecard">
                <div class="scorecard-header">
                    <span class="scorecard-title">Employer Scorecard</span>
                    <span class="scorecard-overall" style="color:${scOverallColor}">${Math.round(sc.overall)}/100</span>
                </div>
                <div class="scorecard-bars">
                    ${renderScorecardBar('Culture', sc.culture)}
                    ${renderScorecardBar('Compensation', sc.compensation)}
                    ${renderScorecardBar('Growth', sc.growth)}
                    ${renderScorecardBar('Work-Life Balance', sc.wlb)}
                    ${renderScorecardBar('Leadership', sc.leadership)}
                    ${renderScorecardBar('Diversity & Inclusion', sc.diversity)}
                </div>
                ${co.scorecard_summary ? `<div class="scorecard-summary">${esc(co.scorecard_summary)}</div>` : ''}
            </div>`;
        }

        // Info grid
        const hasInfo = co.industry || co.size || co.headquarters || co.website;
        if (hasInfo) {
            html += '<div class="company-info-grid">';
            if (co.industry) html += `<div class="company-info-item"><span>Industry</span>${esc(co.industry)}</div>`;
            if (co.size) html += `<div class="company-info-item"><span>Size</span>${esc(co.size)}</div>`;
            if (co.headquarters) html += `<div class="company-info-item"><span>HQ</span>${esc(co.headquarters)}</div>`;
            if (co.website) html += `<div class="company-info-item"><span>Website</span>${esc(co.website)}</div>`;
            html += '</div>';
        }

        // Culture
        if (co.culture_summary) {
            html += `<div class="company-culture">"${esc(co.culture_summary)}"</div>`;
        }

        // Sentiment
        if (co.sentiment && co.sentiment.positive) {
            const pos = co.sentiment.positive || 0;
            const neg = co.sentiment.negative || 0;
            const neu = co.sentiment.neutral || 0;
            html += `<div class="sentiment-bar">
                <div class="sentiment-pos" style="width:${pos}%" title="Positive ${pos}%"></div>
                <div class="sentiment-neu" style="width:${neu}%" title="Neutral ${neu}%"></div>
                <div class="sentiment-neg" style="width:${neg}%" title="Negative ${neg}%"></div>
            </div>
            <div class="sentiment-labels">
                <span style="color:var(--green)">Positive ${pos}%</span>
                <span style="color:var(--text-dim)">Neutral ${neu}%</span>
                <span style="color:var(--red)">Negative ${neg}%</span>
            </div>`;
        }

        // Pros & Cons
        const pros = co.pros || [];
        const cons = co.cons || [];
        if (pros.length > 0 || cons.length > 0) {
            html += '<div class="company-pros-cons">';
            if (pros.length > 0) html += `<ul class="pros-list">${pros.map(p => `<li>${esc(p)}</li>`).join('')}</ul>`;
            if (cons.length > 0) html += `<ul class="cons-list">${cons.map(c => `<li>${esc(c)}</li>`).join('')}</ul>`;
            html += '</div>';
        }

        // Data Sources
        const dataSources = co.data_sources || [];
        if (dataSources.length > 0) {
            html += '<div class="data-sources-row">';
            dataSources.forEach(src => {
                const badgeClass = src.name === 'Glassdoor' ? 'source-badge-glassdoor' :
                                   src.name === 'Indeed' ? 'source-badge-indeed' :
                                   src.type === 'ai' ? 'source-badge-ai' : 'source-badge-scraped';
                const fieldsText = src.fields.join(', ');
                html += `<span class="source-badge ${badgeClass}" title="${esc(fieldsText)}">${esc(src.name)}: ${esc(fieldsText)}</span>`;
            });
            html += '</div>';
            const aiOnly = dataSources.length === 1 && dataSources[0].type === 'ai';
            if (aiOnly) {
                html += '<div class="data-source-note">All company data is AI-inferred. Verify independently before relying on it.</div>';
            }
        }

        html += '</div></div>';
    }

    // ── Description ──
    if (job.description) {
        const descId = `desc-${job.id}`;
        const isLong = job.description.length > 500;
        html += `<div class="card-section">
            <div class="card-section-title"><span class="section-icon">📄</span>Full Description</div>
            <div class="card-description ${isLong ? 'description-collapsed' : ''}" id="${descId}">
                ${esc(job.description)}
            </div>
            ${isLong ? `<button class="desc-toggle" onclick="toggleDesc('${descId}', this)">Show more</button>` : ''}
        </div>`;
    }

    // ── Data Quality ──
    if (job.completeness !== undefined && job.completeness < 80) {
        html += `<div class="card-section">
            <div class="card-section-title"><span class="section-icon">📊</span>Data Quality</div>
            <div class="score-row">
                <div class="score-bar"><div class="score-fill" style="width:${job.completeness}%; background:${job.completeness >= 70 ? 'var(--green)' : job.completeness >= 50 ? 'var(--orange)' : 'var(--red)'}"></div></div>
                <span class="score-label" style="color:var(--text-dim)">${job.completeness}%</span>
            </div>
            <div style="font-size:12px; color:var(--text-dim); margin-top:4px">Some details may be estimated or missing</div>
        </div>`;
    }

    // ── Deep Research (simplified: AI synthesis + drill-down) ──
    if (job.deep_researched && job.ai_synthesis) {
        const researchId = `research-detail-${job.id}`;
        html += `<div class="card-section ai-synthesis-section">
            <div class="card-section-title"><span class="section-icon">🔬</span>AI Research Insight</div>
            <div class="ai-synthesis-text">${esc(job.ai_synthesis)}</div>
            <button class="btn btn-sm btn-secondary" onclick="toggleResearchDetail('${researchId}', this)" style="margin-top:8px">
                View Full Report
            </button>
            <div class="research-detail-panel" id="${researchId}" style="display:none">`;
        if (job.culture_insights) {
            html += `<div class="research-block">
                <div class="research-block-title">🏢 Culture & Environment</div>
                <div class="research-block-text">${esc(job.culture_insights)}</div>
            </div>`;
        }
        if (job.day_in_life) {
            html += `<div class="research-block">
                <div class="research-block-title">📅 A Day in the Life</div>
                <div class="research-block-text">${esc(job.day_in_life)}</div>
            </div>`;
        }
        if (job.interview_process) {
            html += `<div class="research-block">
                <div class="research-block-title">🎯 Interview Process</div>
                <div class="research-block-text">${esc(job.interview_process)}</div>
            </div>`;
        }
        if (job.growth_opportunities) {
            html += `<div class="research-block">
                <div class="research-block-title">📈 Growth Trajectory</div>
                <div class="research-block-text">${esc(job.growth_opportunities)}</div>
            </div>`;
        }
        if (job.hiring_sentiment) {
            html += `<div class="research-block">
                <div class="research-block-title">🌡️ Hiring Climate</div>
                <div class="research-block-text">${esc(job.hiring_sentiment)}</div>
            </div>`;
        }
        html += `</div></div>`;
    } else if (job.match_score >= 55) {
        // Show a button to trigger deep research for decent-scoring jobs
        html += `<div class="card-section" style="text-align:center;padding:14px 24px">
            <button class="btn btn-secondary btn-sm" onclick="triggerDeepResearch(${job.id})" id="deep-research-btn-${job.id}">
                🔬 Deep Research This Role
            </button>
            <div style="font-size:11px;color:var(--text-dim);margin-top:6px">Get AI-powered insights on culture, interviews, and growth</div>
        </div>`;
    }

    // ── Links ──
    if (job.url) {
        html += `<div class="card-links">
            <a href="${esc(job.url)}" target="_blank" rel="noopener">View Original Posting</a>
            ${co && co.glassdoor_url ? `<a href="${esc(co.glassdoor_url)}" target="_blank" rel="noopener">Glassdoor</a>` : ''}
        </div>`;
    }

    card.innerHTML = html;
    return card;
}

function renderBreakdownBar(label, value) {
    if (value === undefined || value === null) return '';
    const color = value >= 70 ? 'var(--green)' : value >= 40 ? 'var(--orange)' : 'var(--red)';
    return `<div class="breakdown-row">
        <span class="breakdown-label">${esc(label)}</span>
        <div class="breakdown-bar"><div class="breakdown-fill" style="width:${value}%; background:${color}"></div></div>
        <span class="breakdown-value" style="color:${color}">${Math.round(value)}</span>
    </div>`;
}

function renderScorecardBar(label, value) {
    if (value === undefined || value === null) return '';
    const color = value >= 70 ? 'var(--green)' : value >= 50 ? 'var(--orange)' : 'var(--red)';
    return `<div class="scorecard-row">
        <span class="scorecard-label">${esc(label)}</span>
        <div class="scorecard-bar"><div class="scorecard-fill" style="width:${value}%; background:${color}"></div></div>
        <span class="scorecard-value">${Math.round(value)}</span>
    </div>`;
}

function renderStars(rating) {
    const full = Math.floor(rating);
    const half = rating - full >= 0.5 ? 1 : 0;
    const empty = 5 - full - half;
    return '★'.repeat(full) + (half ? '½' : '') + '☆'.repeat(empty);
}

function toggleDesc(id, btn) {
    const el = document.getElementById(id);
    if (el.classList.contains('description-collapsed')) {
        el.classList.remove('description-collapsed');
        btn.textContent = 'Show less';
    } else {
        el.classList.add('description-collapsed');
        btn.textContent = 'Show more';
    }
}
window.toggleDesc = toggleDesc;

// ── Action Buttons ──────────────────────────────────────────────────────

function setupActionButtons() {
    document.getElementById('btn-pass').addEventListener('click', () => swipeAction('pass'));
    document.getElementById('btn-shortlist').addEventListener('click', () => swipeAction('shortlist'));
    document.getElementById('btn-like').addEventListener('click', () => swipeAction('like'));
    document.getElementById('btn-skip').addEventListener('click', skipJob);
}

function skipJob() {
    state.currentCardIndex++;
    renderCurrentCard();
}

async function swipeAction(action) {
    // In list/grid mode, act on selected job; in card mode, act on current card
    let job;
    if (state.browseMode === 'card') {
        job = state.swipeStack[state.currentCardIndex];
        if (!job) return;
        state.currentCardIndex++;
        renderCurrentCard();
    } else {
        job = state.swipeStack.find(j => j.id === state.selectedJobId);
        if (!job) { toast('Select a job first', 'info'); return; }
        // Remove from stack and close detail panel
        state.swipeStack = state.swipeStack.filter(j => j.id !== state.selectedJobId);
        closeJobDetail();
        // Select next job if available
        if (state.swipeStack.length > 0) {
            const nextIdx = Math.min(state.currentCardIndex, state.swipeStack.length - 1);
            state.selectedJobId = state.swipeStack[nextIdx]?.id || null;
        }
        renderBrowseView();
    }

    // Instant feedback before API call
    let loadingToastId = null;
    if (action === 'like') {
        loadingToastId = toast(`Preparing application for ${job.title}...`, 'loading');
        setActionButtonsEnabled(false);
    } else if (action === 'shortlist') {
        toast(`Shortlisted ${job.title}`, 'success');
    } else {
        toast('Passed', 'info');
    }

    try {
        const result = await api(`/jobs/${job.id}/swipe`, {
            method: 'POST',
            body: { action },
        });

        if (action === 'like') {
            if (loadingToastId) dismissToast(loadingToastId);
            if (result.status === 'failed') {
                toast(`Cannot apply: ${result.agent_result?.error || 'Job may no longer be active'}`, 'error');
            } else if (result.agent_result?.status === 'needs_input') {
                toast(`Applying to ${job.title} - need some info first`, 'info');
                showQuestions(result.agent_result.questions, result.application.id);
            } else if (result.agent_result?.status === 'ready') {
                toast(`Application ready for ${job.title}!`, 'success');
            } else {
                toast(`Applied to ${job.title}`, 'success');
            }
        }

        loadStats();
    } catch (e) {
        if (loadingToastId) dismissToast(loadingToastId);
        toast('Swipe failed: ' + e.message, 'error');
    }

    if (action === 'like') setActionButtonsEnabled(true);
}

// ── Keyboard Controls ───────────────────────────────────────────────────

function setupKeyboard() {
    document.addEventListener('keydown', (e) => {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;

        const activeView = document.querySelector('.view.active');
        if (!activeView || activeView.id !== 'view-swipe') return;

        if (e.key === 'ArrowLeft' || e.key === 'a') {
            e.preventDefault();
            swipeAction('pass');
        } else if (e.key === 'ArrowRight' || e.key === 'd') {
            e.preventDefault();
            swipeAction('like');
        } else if (e.key === 'ArrowDown' || e.key === 's') {
            e.preventDefault();
            swipeAction('shortlist');
        } else if (e.key === ' ') {
            e.preventDefault();
            skipJob();
        }
    });
}

// ── Questions Panel ─────────────────────────────────────────────────────

function showQuestions(questions, applicationId) {
    const panel = document.getElementById('questions-panel');
    const list = document.getElementById('questions-list');

    list.innerHTML = questions.map((q, i) => `
        <div class="question-item">
            <p>${esc(q)}</p>
            <input type="text" id="answer-${i}" data-question="${esc(q)}" placeholder="Your answer...">
        </div>
    `).join('');

    panel.dataset.applicationId = applicationId;
    panel.classList.add('show');
}

document.getElementById('btn-submit-answers').addEventListener('click', async () => {
    const panel = document.getElementById('questions-panel');
    const appId = panel.dataset.applicationId;

    try {
        const questions = await api(`/applications/${appId}/questions`);
        for (const q of questions) {
            const inputs = panel.querySelectorAll('input');
            for (const input of inputs) {
                if (input.dataset.question === q.question && input.value.trim()) {
                    await api(`/questions/${q.id}/answer`, {
                        method: 'POST',
                        body: { answer: input.value.trim() },
                    });
                }
            }
        }
        toast('Answers submitted! Agent resuming...', 'success');
        panel.classList.remove('show');
        loadStats();
    } catch (e) {
        toast('Failed to submit answers: ' + e.message, 'error');
    }
});

// ── Applications List ───────────────────────────────────────────────────

async function loadApplications() {
    if (!state.profileId) return;
    try {
        const apps = await api(`/profiles/${state.profileId}/applications`);
        const list = document.getElementById('applications-list');
        const pipeline = document.getElementById('diamond-pipeline');

        if (apps.length === 0) {
            if (pipeline) pipeline.innerHTML = '';
            list.innerHTML = `<div class="empty-state">
                <div class="empty-state-art">
                    <svg width="200" height="160" viewBox="0 0 200 160" fill="none">
                        <rect x="60" y="40" width="80" height="6" rx="3" fill="#E8E6E1" opacity="0.06"/>
                        <rect x="60" y="110" width="80" height="6" rx="3" fill="#E8E6E1" opacity="0.06"/>
                        <rect x="62" y="40" width="4" height="76" rx="2" fill="#E8E6E1" opacity="0.05"/>
                        <rect x="134" y="40" width="4" height="76" rx="2" fill="#E8E6E1" opacity="0.05"/>
                        <g transform="translate(88, 48) rotate(2)"><rect width="6" height="56" rx="3" fill="#C4962C" opacity="0.3"/><rect width="6" height="14" rx="3" fill="#E8E6E1" opacity="0.1"/></g>
                        <circle cx="100" cy="138" r="8" fill="none" stroke="#E8E6E1" stroke-width="0.8" opacity="0.12"/>
                        <path d="M96 134 Q100 138 96 142" fill="none" stroke="#C4962C" stroke-width="0.5" opacity="0.2"/>
                        <path d="M104 134 Q100 138 104 142" fill="none" stroke="#C4962C" stroke-width="0.5" opacity="0.2"/>
                    </svg>
                </div>
                <h2>No applications yet</h2><p>Apply to jobs to start tracking your progress</p>
            </div>`;
            return;
        }

        // Render diamond pipeline with counts
        if (pipeline) {
            const counts = { applied: 0, interview: 0, offer: 0, closed: 0 };
            apps.forEach(a => {
                if (a.status === 'completed') counts.applied++;
                else if (a.status === 'interview') counts.interview++;
                else if (a.status === 'offer') counts.offer++;
                else if (a.status === 'failed' || a.status === 'hidden') counts.closed++;
                else counts.applied++;
            });
            pipeline.innerHTML = `
                <svg width="220" height="140" viewBox="0 0 220 140" fill="none">
                    <path d="M110 10 L190 70 L110 130 L30 70 Z" fill="none" stroke="#C4962C" stroke-width="0.8" opacity="0.25"/>
                    <circle cx="110" cy="10" r="6" fill="#3DB87A" opacity="0.6"/>
                    <circle cx="190" cy="70" r="6" fill="#E5A030" opacity="0.6"/>
                    <circle cx="110" cy="130" r="6" fill="#5B9FD6" opacity="0.6"/>
                    <circle cx="30" cy="70" r="6" fill="#E05252" opacity="0.4"/>
                    <text x="110" y="-2" text-anchor="middle" font-family="'JetBrains Mono',monospace" font-size="8" fill="#3DB87A" opacity="0.7">OFFER</text>
                    <text x="110" y="6" text-anchor="middle" font-family="'DM Sans',sans-serif" font-size="11" font-weight="700" fill="#3DB87A" opacity="0.8">${counts.offer}</text>
                    <text x="205" y="66" text-anchor="start" font-family="'JetBrains Mono',monospace" font-size="8" fill="#E5A030" opacity="0.7">INTERVIEW</text>
                    <text x="205" y="78" text-anchor="start" font-family="'DM Sans',sans-serif" font-size="11" font-weight="700" fill="#E5A030" opacity="0.8">${counts.interview}</text>
                    <text x="110" y="148" text-anchor="middle" font-family="'JetBrains Mono',monospace" font-size="8" fill="#5B9FD6" opacity="0.7">APPLIED</text>
                    <text x="110" y="156" text-anchor="middle" font-family="'DM Sans',sans-serif" font-size="11" font-weight="700" fill="#5B9FD6" opacity="0.8">${counts.applied}</text>
                    <text x="15" y="66" text-anchor="end" font-family="'JetBrains Mono',monospace" font-size="8" fill="#E05252" opacity="0.5">CLOSED</text>
                    <text x="15" y="78" text-anchor="end" font-family="'DM Sans',sans-serif" font-size="11" font-weight="700" fill="#E05252" opacity="0.6">${counts.closed}</text>
                    <path d="M116 125 Q185 125 185 75" stroke="#5B9FD6" stroke-width="1.5" opacity="0.15" fill="none"/>
                    <path d="M185 64 Q185 15 116 12" stroke="#E5A030" stroke-width="1.5" opacity="0.15" fill="none"/>
                    <circle cx="110" cy="70" r="3" fill="#C4962C" opacity="0.3"/>
                </svg>`;
        }

        // Email check banner + application list
        let html = `<div class="email-check-bar">
            <button class="btn btn-sm btn-secondary" onclick="checkApplicationEmails()" id="btn-check-emails">
                📧 Check Email for Updates
            </button>
            <span id="email-check-status" style="font-size:12px;color:var(--text-dim)"></span>
        </div>`;
        html += apps.map(a => renderAppItem(a)).join('');
        html += `<div class="hidden-toggle" onclick="toggleHiddenApps()">
            <span id="hidden-toggle-label">Show Hidden Applications</span>
        </div>
        <div id="hidden-apps-list" style="display:none"></div>`;
        list.innerHTML = html;
    } catch (e) {
        console.error('Failed to load applications:', e);
    }
}

function renderAppItem(a, isHidden = false) {
    const statusLabels = {
        'queued': 'Queued', 'in_progress': 'In Progress', 'needs_input': 'Needs Input',
        'ready': 'Ready to Submit', 'completed': 'Submitted', 'failed': 'Failed', 'hidden': 'Hidden',
    };
    const statusLabel = statusLabels[a.status] || a.status.replace('_', ' ');
    return `
    <div class="app-item" onclick="showAppDetail(${a.id})">
        <div class="app-info">
            <h3>${esc(a.job_title)}</h3>
            <p>${esc(a.company)} ${a.applied_at ? '&middot; Applied ' + new Date(a.applied_at).toLocaleDateString() : ''}</p>
            ${a.status === 'ready' ? '<p style="color:var(--accent-light);font-size:12px">Application materials prepared - ready for submission</p>' : ''}
            ${a.status === 'failed' ? `<p style="color:var(--red);font-size:12px">${esc(a.error_message || 'Application failed')}</p>` : ''}
        </div>
        <span class="app-status status-${a.status}">${statusLabel}</span>
    </div>`;
}

async function toggleHiddenApps() {
    const container = document.getElementById('hidden-apps-list');
    const label = document.getElementById('hidden-toggle-label');
    if (container.style.display === 'none') {
        try {
            const hidden = await api(`/profiles/${state.profileId}/applications/hidden`);
            if (hidden.length === 0) {
                container.innerHTML = '<p style="color:var(--text-dim);padding:12px;font-size:13px">No hidden applications</p>';
            } else {
                container.innerHTML = hidden.map(a => renderAppItem(a, true)).join('');
            }
            container.style.display = 'block';
            label.textContent = 'Hide Hidden Applications';
        } catch (e) {
            toast('Failed to load hidden apps: ' + e.message, 'error');
        }
    } else {
        container.style.display = 'none';
        label.textContent = 'Show Hidden Applications';
    }
}

async function showAppDetail(appId) {
    try {
        const app = await api(`/applications/${appId}`);
        const questions = await api(`/applications/${appId}/questions`);

        const list = document.getElementById('applications-list');
        // Remove any existing detail panel
        const existing = document.getElementById('app-detail-panel');
        if (existing) existing.remove();

        const panel = document.createElement('div');
        panel.id = 'app-detail-panel';
        panel.className = 'app-detail-panel';

        const statusLabels = {
            'queued': 'Queued', 'in_progress': 'In Progress', 'needs_input': 'Needs Input',
            'ready': 'Ready to Submit', 'completed': 'Submitted', 'failed': 'Failed',
        };

        // Agent log timeline
        const agentLog = app.agent_log || [];
        const timelineHtml = agentLog.length > 0 ? `
            <div class="detail-section">
                <h4>Application Timeline</h4>
                <div class="agent-timeline">
                    ${agentLog.map(entry => {
                        const statusIcon = entry.status === 'completed' ? '✓' :
                                           entry.status === 'failed' ? '✕' :
                                           entry.status === 'waiting' ? '…' :
                                           entry.status === 'skipped' ? '—' : '▸';
                        const statusClass = entry.status === 'completed' ? 'step-done' :
                                            entry.status === 'failed' ? 'step-fail' :
                                            entry.status === 'waiting' ? 'step-wait' : 'step-active';
                        return `<div class="timeline-step ${statusClass}">
                            <span class="step-icon">${statusIcon}</span>
                            <div class="step-content">
                                <div class="step-name">${esc(entry.step)}</div>
                                <div class="step-detail">${esc(entry.details || '')}</div>
                            </div>
                        </div>`;
                    }).join('')}
                </div>
            </div>` : '';

        // Cover letter
        if (app.cover_letter) _coverLetters[app.id] = app.cover_letter;
        const coverLetterHtml = app.cover_letter ? `
            <div class="detail-section">
                <h4>Cover Letter</h4>
                <div class="cover-letter-preview">${esc(app.cover_letter).replace(/\n/g, '<br>')}</div>
                <div class="detail-actions">
                    <button class="btn btn-sm btn-secondary" onclick="copyCoverLetter(${app.id})">Copy</button>
                </div>
            </div>` : '';

        // Pending questions
        const questionsHtml = questions.length > 0 ? `
            <div class="detail-section">
                <h4>Pending Questions</h4>
                ${questions.map(q => `
                    <div class="detail-question">
                        <div class="question-text">${esc(q.question)}</div>
                        ${q.is_answered
                            ? `<div class="question-answer">Answer: ${esc(q.answer)}</div>`
                            : `<div class="question-input-row">
                                <input type="text" class="question-answer-input" id="qa-${q.id}" placeholder="Type your answer...">
                                <button class="btn btn-sm btn-primary" onclick="submitSingleAnswer(${q.id})">Submit</button>
                              </div>`
                        }
                    </div>
                `).join('')}
            </div>` : '';

        // Error
        const errorHtml = app.error_message ? `
            <div class="detail-section detail-error">
                <h4>Error</h4>
                <p>${esc(app.error_message)}</p>
            </div>` : '';

        // Action buttons based on status
        let actionsHtml = '<div class="detail-actions">';
        if (app.status === 'ready') {
            actionsHtml += `<button class="btn btn-success" onclick="autoApply(${appId})">🚀 Submit Application</button>`;
            actionsHtml += `<button class="btn btn-secondary" onclick="markSubmitted(${appId})">Mark as Submitted</button>`;
        }
        actionsHtml += `<button class="btn btn-secondary" onclick="returnToBrowse(${appId})">↩ Return to Browse</button>`;
        if (app.status !== 'hidden') {
            actionsHtml += `<button class="btn btn-secondary" onclick="hideApplication(${appId})">🙈 Hide</button>`;
        } else {
            actionsHtml += `<button class="btn btn-secondary" onclick="unhideApplication(${appId})">👁 Unhide</button>`;
        }
        actionsHtml += `<button class="btn btn-secondary" onclick="document.getElementById('app-detail-panel').remove()">✕ Close</button>`;
        actionsHtml += '</div>';

        panel.innerHTML = `
            <div class="detail-header">
                <div>
                    <h3>${esc(app.job_title)}</h3>
                    <p>${esc(app.company)}</p>
                </div>
                <span class="app-status status-${app.status}">${statusLabels[app.status] || app.status}</span>
            </div>
            ${timelineHtml}
            ${coverLetterHtml}
            ${questionsHtml}
            ${errorHtml}
            ${actionsHtml}
        `;

        list.appendChild(panel);
        panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (e) {
        toast('Failed to load application: ' + e.message, 'error');
    }
}

async function submitSingleAnswer(questionId) {
    const input = document.getElementById(`qa-${questionId}`);
    if (!input || !input.value.trim()) return;
    try {
        await api(`/questions/${questionId}/answer`, {
            method: 'POST',
            body: { answer: input.value.trim() },
        });
        toast('Answer submitted', 'success');
        loadApplications();
    } catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}

async function markSubmitted(appId) {
    try {
        await api(`/applications/${appId}/submit`, { method: 'POST' });
        toast('Application marked as submitted!', 'success');
        loadApplications();
        loadStats();
    } catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}

const _coverLetters = {};

function fallbackCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px';
    document.body.appendChild(ta);
    ta.select();
    try {
        document.execCommand('copy');
        toast('Copied to clipboard', 'success');
    } catch (e) {
        toast('Copy failed — please select and copy manually', 'error');
    }
    document.body.removeChild(ta);
}

function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text)
            .then(() => toast('Copied to clipboard', 'success'))
            .catch(() => fallbackCopy(text));
    } else {
        fallbackCopy(text);
    }
}

function copyCoverLetter(appId) {
    const text = _coverLetters[appId];
    if (text) {
        copyToClipboard(text);
    } else {
        toast('Cover letter not found', 'error');
    }
}

async function checkApplicationEmails() {
    const btn = document.getElementById('btn-check-emails');
    const status = document.getElementById('email-check-status');
    btn.disabled = true;
    btn.textContent = '📧 Checking...';
    status.textContent = '';

    try {
        // Fetch recent emails via backend proxy (which calls Gmail API if configured)
        // For now, use the manual approach — open a modal to paste email subjects
        const modal = document.createElement('div');
        modal.className = 'modal-overlay';
        modal.innerHTML = `
            <div class="modal-content" style="max-width:600px">
                <h3>📧 Check Application Emails</h3>
                <p style="color:var(--text-dim);font-size:13px;margin-bottom:12px">
                    Paste email subjects and senders to check for application updates.
                    One per line, format: <code>Subject | From</code>
                </p>
                <textarea id="email-paste-input" rows="8" placeholder="Thank you for applying at CIRA | CIRA &lt;notifications@app.bamboohr.com&gt;
We'd like to schedule an interview | Jane Smith &lt;hr@company.com&gt;" style="width:100%;font-size:13px"></textarea>
                <div class="detail-actions" style="margin-top:12px">
                    <button class="btn btn-primary" onclick="processEmailPaste()">Check Emails</button>
                    <button class="btn btn-secondary" onclick="this.closest('.modal-overlay').remove()">Cancel</button>
                </div>
            </div>`;
        document.body.appendChild(modal);
    } catch(e) {
        toast('Failed: ' + e.message, 'error');
    }
    btn.disabled = false;
    btn.textContent = '📧 Check Email for Updates';
}

async function processEmailPaste() {
    const input = document.getElementById('email-paste-input');
    const lines = input.value.trim().split('\n').filter(l => l.trim());

    if (lines.length === 0) {
        toast('No emails to check', 'error');
        return;
    }

    const emails = lines.map((line, i) => {
        const parts = line.split('|').map(s => s.trim());
        return {
            id: `manual-${i}`,
            subject: parts[0] || '',
            from: parts[1] || '',
            snippet: parts[0] || '',
        };
    });

    // Close modal
    document.querySelector('.modal-overlay')?.remove();

    const loadId = toast('Analyzing emails...', 'loading');
    try {
        const result = await api(`/profiles/${state.profileId}/check-emails`, {
            method: 'POST',
            body: { emails },
        });
        dismissToast(loadId);

        if (result.updates && result.updates.length > 0) {
            for (const u of result.updates) {
                const icon = u.classification === 'interview' ? '🎉' :
                             u.classification === 'rejected' ? '😔' :
                             u.classification === 'confirmed' ? '✅' : '📧';
                toast(`${icon} ${u.company}: ${u.changes?.action || u.summary}`,
                      u.classification === 'interview' ? 'success' : 'info');
            }
            loadApplications();
        } else {
            toast('No application updates found in these emails', 'info');
        }
    } catch(e) {
        dismissToast(loadId);
        toast('Email check failed: ' + e.message, 'error');
    }
}

async function returnToBrowse(appId) {
    try {
        await api(`/applications/${appId}/return-to-browse`, { method: 'POST' });
        toast('Returned to browse queue', 'success');
        document.getElementById('app-detail-panel')?.remove();
        loadApplications();
        loadStats();
    } catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}

async function hideApplication(appId) {
    try {
        await api(`/applications/${appId}/hide`, { method: 'POST' });
        toast('Application hidden', 'info');
        document.getElementById('app-detail-panel')?.remove();
        loadApplications();
    } catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}

async function unhideApplication(appId) {
    try {
        await api(`/applications/${appId}/unhide`, { method: 'POST' });
        toast('Application restored', 'success');
        document.getElementById('app-detail-panel')?.remove();
        loadApplications();
    } catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}

async function viewAutomationPlan(appId) {
    try {
        toast('Loading apply steps...', 'info');
        const plan = await api(`/applications/${appId}/automation-plan`);
        const panel = document.getElementById('app-detail-panel');
        if (!panel) return;

        if (plan.cover_letter) _coverLetters[appId] = plan.cover_letter;

        let planHtml = `<div class="detail-section">
            <h4>Apply Steps (${esc(plan.platform_name)})</h4>
            <div class="automation-steps">
                ${plan.steps.map((step, i) => `
                    <div class="auto-step">
                        <span class="auto-step-num">${i + 1}</span>
                        <span class="auto-step-text">${esc(step)}</span>
                    </div>
                `).join('')}
            </div>
            ${plan.requires_account ? '<p style="color:var(--orange);font-size:12px;margin-top:8px">This platform may require creating an account</p>' : ''}
            ${plan.notes ? `<p style="color:var(--text-dim);font-size:12px;margin-top:8px">${esc(plan.notes)}</p>` : ''}
            <div class="detail-actions" style="margin-top:12px">
                <a href="${esc(plan.url)}" target="_blank" rel="noopener" class="btn btn-primary">Open Job Posting</a>
                <button class="btn btn-secondary" onclick="copyCoverLetter(${appId})">Copy Cover Letter</button>
            </div>
        </div>`;

        // Insert before the close button area
        const actionsDiv = panel.querySelector('.detail-actions:last-child');
        const planDiv = document.createElement('div');
        planDiv.innerHTML = planHtml;
        if (actionsDiv) {
            actionsDiv.parentNode.insertBefore(planDiv, actionsDiv);
        } else {
            panel.appendChild(planDiv);
        }
    } catch (e) {
        toast('Failed to load plan: ' + e.message, 'error');
    }
}

// ── Tag Inputs ──────────────────────────────────────────────────────────

function setupTagInputs() {
    setupTagInput('roles', 'f-roles-input', 'roles-tags', 'roles-suggestions');
    setupTagInput('locations', 'f-locations-input', 'locations-tags', 'locations-suggestions');
    setupTagInput('skills', 'f-skills-input', 'skills-tags', 'skills-suggestions');
}

// Debounce helper
function debounce(fn, ms) {
    let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function setupTagInput(tagKey, inputId, containerId, suggestionsId) {
    const input = document.getElementById(inputId);
    const container = document.getElementById(containerId);
    const dropdown = document.getElementById(suggestionsId);
    const fieldMap = { roles: 'roles', locations: 'locations', skills: 'skills' };
    let activeIdx = -1;

    container.addEventListener('click', () => input.focus());

    function addTag(val) {
        val = val.trim();
        if (val && !state.tags[tagKey].includes(val)) {
            state.tags[tagKey].push(val);
            renderTags(tagKey);
        }
        input.value = '';
        closeSuggestions();
    }

    function closeSuggestions() {
        dropdown.classList.remove('open');
        dropdown.innerHTML = '';
        activeIdx = -1;
    }

    async function fetchSuggestions(q) {
        if (!state.profileId) return;
        try {
            const data = await api(`/profiles/${state.profileId}/suggestions?field=${fieldMap[tagKey]}&q=${encodeURIComponent(q)}`);
            if (!data.suggestions || data.suggestions.length === 0) { closeSuggestions(); return; }
            dropdown.innerHTML = '';
            activeIdx = -1;
            data.suggestions.forEach((s, i) => {
                const item = document.createElement('div');
                item.className = 'suggestion-item';
                item.innerHTML = `<span>${esc(s.value)}</span><span class="suggestion-count">${s.count} jobs</span>`;
                item.addEventListener('mousedown', (e) => { e.preventDefault(); addTag(s.value); });
                dropdown.appendChild(item);
            });
            dropdown.classList.add('open');
        } catch (e) { closeSuggestions(); }
    }

    const debouncedFetch = debounce(fetchSuggestions, 250);

    input.addEventListener('input', () => {
        const q = input.value.trim();
        if (q.length >= 1) { debouncedFetch(q); }
        else { closeSuggestions(); }
    });

    input.addEventListener('focus', () => {
        // On focus with empty input, show popular suggestions
        if (!input.value.trim() && state.profileId) { debouncedFetch(''); }
    });

    input.addEventListener('blur', () => {
        // Small delay to allow click on suggestion
        setTimeout(closeSuggestions, 200);
    });

    input.addEventListener('keydown', (e) => {
        const items = dropdown.querySelectorAll('.suggestion-item');

        if (e.key === 'ArrowDown' && items.length > 0) {
            e.preventDefault();
            activeIdx = Math.min(activeIdx + 1, items.length - 1);
            items.forEach((it, i) => it.classList.toggle('active', i === activeIdx));
            items[activeIdx]?.scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'ArrowUp' && items.length > 0) {
            e.preventDefault();
            activeIdx = Math.max(activeIdx - 1, 0);
            items.forEach((it, i) => it.classList.toggle('active', i === activeIdx));
            items[activeIdx]?.scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (activeIdx >= 0 && items[activeIdx]) {
                addTag(items[activeIdx].querySelector('span').textContent);
            } else {
                addTag(input.value);
            }
        } else if (e.key === 'Escape') {
            closeSuggestions();
        } else if (e.key === 'Backspace' && !input.value && state.tags[tagKey].length > 0) {
            state.tags[tagKey].pop();
            renderTags(tagKey);
        }
    });
}

function renderTags(tagKey) {
    const containerMap = { roles: 'roles-tags', locations: 'locations-tags', skills: 'skills-tags' };
    const inputMap = { roles: 'f-roles-input', locations: 'f-locations-input', skills: 'f-skills-input' };
    const container = document.getElementById(containerMap[tagKey]);
    const input = document.getElementById(inputMap[tagKey]);

    container.querySelectorAll('.tag').forEach(t => t.remove());

    state.tags[tagKey].forEach((tag, i) => {
        const el = document.createElement('span');
        el.className = 'tag';
        el.innerHTML = `${esc(tag)} <button type="button" data-key="${tagKey}" data-index="${i}">&times;</button>`;
        el.querySelector('button').addEventListener('click', () => {
            state.tags[tagKey].splice(i, 1);
            renderTags(tagKey);
        });
        container.insertBefore(el, input);
    });
}

// ── Skills Audit ─────────────────────────────────────────────────────────

async function runSkillsAudit() {
    if (!state.profileId) { toast('Create a profile first', 'error'); return; }
    const btn = document.getElementById('btn-skills-audit');
    const results = document.getElementById('skills-audit-results');

    btn.textContent = 'Auditing...';
    btn.disabled = true;
    results.style.display = 'block';
    results.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-dim)">Analyzing your skills against job market demand...</div>';

    try {
        const data = await api(`/profiles/${state.profileId}/skills-audit`);

        if (!data.skill_hits || Object.keys(data.skill_hits).length === 0) {
            results.innerHTML = '<div class="skills-audit"><p style="color:var(--text-dim)">Search for jobs first to audit your skills against the market.</p></div>';
            return;
        }

        let html = '<div class="skills-audit">';

        // Current skills hit rates
        html += '<h4>Your Skills vs. Job Market</h4>';
        const sorted = Object.entries(data.skill_hits).sort((a, b) => b[1].pct - a[1].pct);
        for (const [skill, info] of sorted) {
            const color = info.pct >= 20 ? 'var(--green)' : info.pct >= 5 ? 'var(--accent)' : 'var(--red)';
            html += `<div class="skill-audit-row">
                <span style="min-width:140px;color:${color}">${esc(skill)}</span>
                <div class="skill-bar"><div class="skill-bar-fill" style="width:${Math.min(info.pct, 100)}%;background:${color}"></div></div>
                <span style="min-width:60px;text-align:right;color:var(--text-dim)">${info.pct}%</span>
            </div>`;
        }

        // AI recommendations
        if (data.ai_audit) {
            const ai = data.ai_audit;

            if (ai.recommended_additions?.length) {
                html += '<h4 style="margin-top:16px;color:var(--green)">Recommended Additions</h4>';
                for (const skill of ai.recommended_additions) {
                    html += `<div class="skill-audit-row">
                        <span>${esc(skill)}</span>
                        <button class="skill-add-btn" onclick="addSkillFromAudit('${esc(skill).replace(/'/g, "\\'")}', this)">+ Add</button>
                    </div>`;
                }
            }

            if (ai.recommended_removals?.length) {
                html += '<h4 style="margin-top:16px;color:var(--red)">Consider Removing</h4>';
                for (const skill of ai.recommended_removals) {
                    html += `<div class="skill-audit-row">
                        <span>${esc(skill)}</span>
                        <button class="skill-remove-btn" onclick="removeSkillFromAudit('${esc(skill).replace(/'/g, "\\'")}', this)">Remove</button>
                    </div>`;
                }
            }

            if (ai.missing_high_demand?.length) {
                html += '<h4 style="margin-top:16px;color:var(--accent-light)">High Demand (Missing)</h4>';
                for (const skill of ai.missing_high_demand) {
                    html += `<div class="skill-audit-row">
                        <span>${esc(skill)}</span>
                        <button class="skill-add-btn" onclick="addSkillFromAudit('${esc(skill).replace(/'/g, "\\'")}', this)">+ Add</button>
                    </div>`;
                }
            }
        }

        html += '</div>';
        results.innerHTML = html;
    } catch (e) {
        results.innerHTML = `<div class="skills-audit"><p style="color:var(--red)">Audit failed: ${esc(e.message)}</p></div>`;
    } finally {
        btn.textContent = 'Audit Skills';
        btn.disabled = false;
    }
}

function addSkillFromAudit(skill, btnEl) {
    if (!state.tags.skills.includes(skill)) {
        state.tags.skills.push(skill);
        renderTags('skills');
        toast(`Added "${skill}" to skills`, 'success');
    }
    // Remove the row from the audit list
    const row = btnEl ? btnEl.closest('.skill-audit-row') : null;
    if (row) row.remove();
    // Auto-save with debounce
    _debouncedSkillSave();
}

function removeSkillFromAudit(skill, btnEl) {
    const idx = state.tags.skills.indexOf(skill);
    if (idx >= 0) {
        state.tags.skills.splice(idx, 1);
        renderTags('skills');
        toast(`Removed "${skill}" from skills`, 'info');
    }
    // Remove the row from the audit list
    const row = btnEl ? btnEl.closest('.skill-audit-row') : null;
    if (row) row.remove();
    // Auto-save with debounce
    _debouncedSkillSave();
}

// Debounced save so rapid clicks don't spam the API
let _skillSaveTimer = null;
function _debouncedSkillSave() {
    clearTimeout(_skillSaveTimer);
    _skillSaveTimer = setTimeout(async () => {
        try {
            if (!state.profileId) return;
            await api(`/profiles/${state.profileId}`, {
                method: 'PUT',
                body: { name: state.profile?.name || 'Unknown', skills: state.tags.skills }
            });
            toast('Profile saved!', 'success');
        } catch (e) {
            toast('Save failed: ' + e.message, 'error');
        }
    }, 800);
}

window.runSkillsAudit = runSkillsAudit;
window.addSkillFromAudit = addSkillFromAudit;
window.removeSkillFromAudit = removeSkillFromAudit;

// ── Utilities ───────────────────────────────────────────────────────────

function esc(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function toast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const el = document.createElement('div');
    const id = 'toast-' + Date.now();
    el.id = id;
    el.className = `toast toast-${type}`;
    if (type === 'loading') {
        el.innerHTML = `<span class="toast-spinner"></span>${message}`;
    } else {
        el.textContent = message;
    }
    container.appendChild(el);
    if (type !== 'loading') {
        setTimeout(() => el.remove(), 3000);
    }
    return id;
}

function dismissToast(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
}

function setActionButtonsEnabled(enabled) {
    const bar = document.getElementById('action-bar');
    if (enabled) {
        bar.classList.remove('action-bar--loading');
    } else {
        bar.classList.add('action-bar--loading');
    }
}

// ── Settings Page ───────────────────────────────────────────────────────

async function loadSettings() {
    // Show AI provider status
    const badge = document.getElementById('provider-badge');
    if (badge) {
        try {
            const providerData = await api('/ai-provider');
            const p = providerData.provider;
            badge.textContent = p === 'anthropic' ? 'Claude (Anthropic)' : p === 'gemini' ? 'Gemini (Google)' : 'No AI Provider';
            badge.className = `provider-badge ${p}`;
        } catch (e) {
            badge.textContent = 'Error';
            badge.className = 'provider-badge none';
        }
    }

    // Show rescore button state
    const rescoreSettingsBtn = document.getElementById('btn-rescore-settings');
    if (rescoreSettingsBtn) {
        rescoreSettingsBtn.textContent = 'Rescore All Jobs';
    }

    // Show weights (two sets: base + with deep research)
    const weightsEl = document.getElementById('weights-display');
    if (weightsEl) {
        const baseWeights = {
            'Role Fit': 25, 'Skills Match': 25, 'Location': 15,
            'Compensation': 15, 'Seniority': 10, 'Culture Fit': 10
        };
        const researchWeights = {
            'Role Fit': 22, 'Skills Match': 22, 'Location': 13,
            'Compensation': 13, 'Seniority': 8, 'Culture Fit': 8, 'Deep Research': 14
        };
        weightsEl.innerHTML = `
            <div style="font-size:12px;color:var(--text-dim);margin-bottom:8px">Base scoring (no deep research):</div>
            ${Object.entries(baseWeights).map(([label, value]) =>
                `<div class="weight-row">
                    <span class="weight-label">${label}</span>
                    <div class="weight-bar"><div class="weight-fill" style="width:${value}%"></div></div>
                    <span class="weight-value">${value}%</span>
                </div>`
            ).join('')}
            <div style="font-size:12px;color:var(--text-dim);margin:12px 0 8px">After deep research:</div>
            ${Object.entries(researchWeights).map(([label, value]) =>
                `<div class="weight-row">
                    <span class="weight-label">${label}</span>
                    <div class="weight-bar"><div class="weight-fill" style="width:${value}%;${label === 'Deep Research' ? 'background:var(--accent)' : ''}"></div></div>
                    <span class="weight-value">${value}%</span>
                </div>`
            ).join('')}
        `;
    }
}

async function reanalyzeProfile() {
    if (!state.profileId) { toast('No profile to analyze', 'error'); return; }
    const btn = document.getElementById('btn-reanalyze');
    if (btn) { btn.disabled = true; btn.textContent = 'Analyzing...'; }
    try {
        await api(`/profiles/${state.profileId}/analyze`, { method: 'POST' });
        toast('Profile re-analyzed with latest AI', 'success');
        await loadProfile();
    } catch (e) {
        toast('Analysis failed: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Re-analyze Profile'; }
    }
}

async function verifyAllJobs() {
    if (!state.profileId) return;
    toast('Verifying pending jobs...', 'info');
    try {
        const result = await api(`/profiles/${state.profileId}/verify-pending`, { method: 'POST' });
        toast(`Verified: ${result.active} active, ${result.expired} expired`, 'success');
        loadStats();
    } catch (e) {
        toast('Verification failed: ' + e.message, 'error');
    }
}

async function loadApplyReadiness() {
    if (!state.profileId) return;
    try {
        const data = await api(`/profiles/${state.profileId}/apply-readiness`);
        let container = document.getElementById('readiness-panel');
        if (!container) {
            container = document.createElement('div');
            container.id = 'readiness-panel';
            container.className = 'readiness-panel';
            const profileForm = document.querySelector('.profile-form');
            profileForm.insertBefore(container, profileForm.firstChild);
        }

        const scoreColor = data.score >= 80 ? 'var(--green)' : data.score >= 50 ? 'var(--orange)' : 'var(--red)';
        container.innerHTML = `
            <div class="readiness-header">
                <div class="readiness-score" style="color:${scoreColor}">${data.score}%</div>
                <div>
                    <div class="readiness-title">${data.ready ? 'Ready to Apply' : 'Not Quite Ready'}</div>
                    <div class="readiness-sub">${data.passed}/${data.total} checks passed</div>
                </div>
            </div>
            <div class="readiness-checks">
                ${data.checks.map(c => `
                    <div class="readiness-check ${c.passed ? 'check-pass' : 'check-fail'}">
                        <span class="check-icon">${c.passed ? '✓' : '✕'}</span>
                        <span class="check-name">${esc(c.name)}</span>
                        <span class="check-detail">${esc(c.detail)}</span>
                    </div>
                `).join('')}
            </div>
        `;
    } catch (e) {
        console.error('Failed to load readiness:', e);
    }
}

async function reenrichCompanies() {
    if (!state.profileId) return;
    toast('Re-enriching companies for logos...', 'info');
    try {
        const result = await api(`/profiles/${state.profileId}/reenrich-companies`, { method: 'POST' });
        toast(`Updated ${result.updated}/${result.total_missing} company websites`, 'success');
        if (result.updated > 0) loadSwipeStack();
    } catch (e) {
        toast('Re-enrich failed: ' + e.message, 'error');
    }
}

// ── Profile Interview Q&A ────────────────────────────────────────────────

async function loadInterviewQuestions() {
    if (!state.profileId) return;
    const container = document.getElementById('interview-container');
    if (!container) return;

    try {
        const questions = await api(`/profiles/${state.profileId}/interview-questions`);
        const allQuestions = await api(`/profiles/${state.profileId}/interview-questions?answered=true`);
        const answered = allQuestions.filter(q => q.is_answered);

        if (questions.length === 0 && answered.length === 0) {
            container.innerHTML = `
                <div class="interview-empty">
                    <p>No interview questions yet. Generate questions to build a richer profile for better cover letters and matching.</p>
                    <button class="btn btn-primary" onclick="generateInterviewQuestions()">
                        <span style="margin-right:6px">🎤</span>Generate Questions
                    </button>
                </div>`;
            return;
        }

        const categoryIcons = {
            experience: '💼', motivation: '🎯', preferences: '⚙️',
            culture: '🏢', leadership: '👑', technical: '🔧', general: '💡'
        };

        let html = `<div class="interview-header">
            <div class="interview-stats">
                <span class="interview-stat">${answered.length} answered</span>
                <span class="interview-stat pending">${questions.length} remaining</span>
            </div>
            <button class="btn btn-sm btn-secondary" onclick="generateInterviewQuestions()">+ More Questions</button>
        </div>`;

        if (questions.length > 0) {
            html += '<div class="interview-questions">';
            for (const q of questions) {
                const icon = categoryIcons[q.category] || '💡';
                html += `
                <div class="interview-question" data-id="${q.id}">
                    <div class="iq-header">
                        <span class="iq-icon">${icon}</span>
                        <span class="iq-category">${esc(q.category)}</span>
                    </div>
                    <div class="iq-text">${esc(q.question)}</div>
                    <div class="iq-answer-row">
                        <textarea class="iq-input" id="iq-${q.id}" rows="3" placeholder="Type your answer..."></textarea>
                        <button class="btn btn-sm btn-primary" onclick="submitInterviewAnswer(${q.id})">Submit</button>
                    </div>
                </div>`;
            }
            html += '</div>';
        }

        if (answered.length > 0) {
            html += `<div class="interview-answered-toggle" onclick="toggleAnswered()">
                <span id="answered-toggle-text">Show ${answered.length} answered questions</span>
            </div>
            <div id="answered-list" style="display:none" class="interview-questions">`;
            for (const q of answered) {
                const icon = categoryIcons[q.category] || '💡';
                html += `
                <div class="interview-question answered">
                    <div class="iq-header">
                        <span class="iq-icon">${icon}</span>
                        <span class="iq-category">${esc(q.category)}</span>
                        <span class="iq-done">✓</span>
                    </div>
                    <div class="iq-text">${esc(q.question)}</div>
                    <div class="iq-answer-text">${esc(q.answer)}</div>
                </div>`;
            }
            html += '</div>';
        }

        container.innerHTML = html;
    } catch (e) {
        console.error('Failed to load interview questions:', e);
    }
}

async function generateInterviewQuestions() {
    if (!state.profileId) return;
    toast('Generating interview questions...', 'info');
    try {
        const result = await api(`/profiles/${state.profileId}/generate-questions`, { method: 'POST' });
        toast(`Generated ${result.generated} new questions`, 'success');
        loadInterviewQuestions();
    } catch (e) {
        toast('Failed to generate questions: ' + e.message, 'error');
    }
}

async function submitInterviewAnswer(questionId) {
    const input = document.getElementById(`iq-${questionId}`);
    if (!input || !input.value.trim()) { toast('Please type an answer', 'error'); return; }
    try {
        await api(`/profiles/${state.profileId}/interview-questions/${questionId}/answer`, {
            method: 'POST',
            body: { answer: input.value.trim() },
        });
        toast('Answer saved!', 'success');
        loadInterviewQuestions();
    } catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}

function toggleAnswered() {
    const list = document.getElementById('answered-list');
    const text = document.getElementById('answered-toggle-text');
    if (list.style.display === 'none') {
        list.style.display = 'block';
        text.textContent = 'Hide answered questions';
    } else {
        list.style.display = 'none';
        text.textContent = text.textContent.replace('Hide', 'Show');
    }
}

// ── Dedup Cleanup ───────────────────────────────────────────────────────

async function dedupJobs() {
    if (!state.profileId) return;
    toast('Cleaning up duplicate jobs...', 'info');
    try {
        const result = await api(`/profiles/${state.profileId}/dedup-jobs`, { method: 'POST' });
        toast(`Removed ${result.removed} duplicates, merged ${result.merged_sources} sources`, 'success');
        loadSwipeStack();
        loadStats();
    } catch (e) {
        toast('Dedup failed: ' + e.message, 'error');
    }
}

async function resetDatabase() {
    if (!state.profileId) return;
    if (!confirm('⚠️ This will delete ALL jobs, applications, and company data.\n\nYour profile and preferences will be kept.\n\nAre you sure?')) return;
    if (!confirm('Really? This cannot be undone.')) return;
    toast('Clearing all job data...', 'info');
    try {
        const result = await api(`/profiles/${state.profileId}/reset-jobs`, { method: 'POST' });
        toast(`Reset complete — ${result.jobs_deleted} jobs and ${result.applications_deleted} applications removed`, 'success');
        loadSwipeStack();
        loadStats();
        loadApplications();
        loadShortlist();
    } catch (e) {
        toast('Reset failed: ' + e.message, 'error');
    }
}

// ── Auto-Apply via Agent ────────────────────────────────────────────────

async function autoApply(appId) {
    try {
        toast('Loading automation plan...', 'info');
        const plan = await api(`/applications/${appId}/automation-plan`);

        // Open the job URL in a new tab
        if (plan.url) {
            window.open(plan.url, '_blank');
        }

        // Copy cover letter to clipboard
        if (plan.cover_letter) {
            copyToClipboard(plan.cover_letter);
            toast('Cover letter copied to clipboard! Job page opening...', 'success');
        }

        // Show the steps in a modal
        showAutoApplyModal(appId, plan);
    } catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}

function showAutoApplyModal(appId, plan) {
    if (plan.cover_letter) _coverLetters[appId] = plan.cover_letter;
    let overlay = document.getElementById('auto-apply-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'auto-apply-overlay';
        overlay.className = 'modal-overlay';
        document.body.appendChild(overlay);
    }

    overlay.innerHTML = `
        <div class="modal auto-apply-modal">
            <button class="modal-close" onclick="document.getElementById('auto-apply-overlay').classList.remove('show')">&times;</button>
            <h2>🚀 Apply: ${esc(plan.platform_name || plan.platform)}</h2>
            <div class="auto-apply-steps">
                ${plan.steps.map((step, i) => `
                    <div class="auto-step">
                        <span class="auto-step-num">${i + 1}</span>
                        <span class="auto-step-text">${esc(step)}</span>
                    </div>
                `).join('')}
            </div>
            ${plan.requires_account ? '<p class="auto-apply-note warn">⚠️ This platform may require an account</p>' : ''}
            ${plan.notes ? `<p class="auto-apply-note">${esc(plan.notes)}</p>` : ''}
            <div class="auto-apply-actions">
                <a href="${esc(plan.url)}" target="_blank" class="btn btn-primary">Open Job Page</a>
                <button class="btn btn-secondary" onclick="copyCoverLetter(${appId})">📋 Copy Cover Letter</button>
                <button class="btn btn-success" onclick="markSubmitted(${appId});document.getElementById('auto-apply-overlay').classList.remove('show')">✅ Mark Submitted</button>
            </div>
        </div>
    `;
    overlay.classList.add('show');
}

// ── Deep Research Trigger ────────────────────────────────────────────────

async function triggerDeepResearch(jobId) {
    const btn = document.getElementById(`deep-research-btn-${jobId}`);
    if (btn) { btn.disabled = true; btn.textContent = '🔬 Researching...'; }
    toast('Running deep research...', 'info');
    try {
        await api(`/jobs/${jobId}/deep-research`, { method: 'POST' });
        toast('Deep research complete! Refreshing...', 'success');
        await loadSwipeStack();
    } catch (e) {
        toast('Deep research failed: ' + e.message, 'error');
        if (btn) { btn.disabled = false; btn.textContent = '🔬 Deep Research This Role'; }
    }
}

async function deepResearchShortlist() {
    if (!state.profileId) return;
    toast('Deep researching shortlisted jobs...', 'info');
    try {
        const result = await api(`/profiles/${state.profileId}/deep-research-shortlist`, { method: 'POST' });
        toast(`Deep researched ${result.researched} of ${result.total} jobs`, 'success');
    } catch (e) {
        toast('Deep research failed: ' + e.message, 'error');
    }
}

// ── Insights / Summary Tab ──────────────────────────────────────────────

async function loadInsights() {
    if (!state.profileId) return;
    const content = document.getElementById('insights-content');
    if (!content) return;

    content.innerHTML = `<div style="text-align:center;padding:40px">
        <svg width="48" height="48" viewBox="0 0 120 120" fill="none">
            <style>@keyframes o-spin{to{transform:rotate(360deg)}}@keyframes p-pulse{0%,100%{opacity:.15}50%{opacity:.35}}</style>
            <path d="M55 56 L60 52 L65 56 L63 62 L57 62 Z" fill="#C4962C" style="animation:p-pulse 1.8s ease-in-out infinite"/>
            <g style="animation:o-spin 1.8s linear infinite;transform-origin:60px 60px">
                <circle cx="60" cy="28" r="5" fill="none" stroke="#C4962C" stroke-width="1" opacity="0.5"/>
                <path d="M57.5 25.5 Q60 28 57.5 30.5" fill="none" stroke="#8B6914" stroke-width="0.4" opacity="0.4"/>
                <path d="M62.5 25.5 Q60 28 62.5 30.5" fill="none" stroke="#8B6914" stroke-width="0.4" opacity="0.4"/>
            </g>
            <circle cx="60" cy="60" r="32" fill="none" stroke="#E8E6E1" stroke-width="0.3" opacity="0.04" stroke-dasharray="3 5"/>
        </svg>
        <p style="color:var(--text-dim);margin-top:16px">Analyzing your job search data...</p>
    </div>`;

    try {
        const data = await api(`/profiles/${state.profileId}/insights`);
        renderInsights(data);
    } catch (e) {
        content.innerHTML = `<div class="empty-state"><h2>Failed to load insights</h2><p>${esc(e.message)}</p></div>`;
    }
}

function renderInsights(data) {
    const content = document.getElementById('insights-content');
    if (!content) return;
    if (data.total_jobs === 0) {
        content.innerHTML = `<div class="empty-state">
            <div class="empty-state-art">
                <svg width="200" height="160" viewBox="0 0 200 160" fill="none">
                    <g transform="translate(60, 30)">
                        <path d="M40 50 Q20 30 25 55 Q15 40 18 60 Q10 48 15 65 Q8 70 20 80 Q30 90 50 85 Q70 80 75 60 Q78 45 65 35 Q55 28 40 50Z" fill="#8B6914" opacity="0.12" stroke="#C4962C" stroke-width="0.8" opacity="0.2"/>
                        <circle cx="42" cy="62" r="11" fill="none" stroke="#E8E6E1" stroke-width="1" opacity="0.2"/>
                        <path d="M37 57 Q42 62 37 67" fill="none" stroke="#C4962C" stroke-width="0.6" opacity="0.25"/>
                        <path d="M47 57 Q42 62 47 67" fill="none" stroke="#C4962C" stroke-width="0.6" opacity="0.25"/>
                    </g>
                </svg>
            </div>
            <h2>No data yet</h2><p>Search for jobs first to generate insights</p>
        </div>`;
        return;
    }

    let html = '';

    // Stats overview cards
    const scoreStats = data.score_stats || {};
    const salaryStats = data.salary_stats || {};
    html += `<div class="insights-grid">
        <div class="insight-card">
            <div class="insight-card-title">📋 Total Jobs Found</div>
            <div class="insight-card-value">${data.total_jobs}</div>
            <div class="insight-stat-row">
                <span class="insight-stat-pill">Pending<span class="pill-count">${data.pending}</span></span>
                ${data.shortlisted ? `<span class="insight-stat-pill" style="border-color:var(--yellow)">Shortlisted<span class="pill-count">${data.shortlisted}</span></span>` : ''}
                <span class="insight-stat-pill">Liked<span class="pill-count">${data.liked}</span></span>
                <span class="insight-stat-pill">Passed<span class="pill-count">${data.passed}</span></span>
            </div>
        </div>
        <div class="insight-card">
            <div class="insight-card-title">🎯 Match Quality</div>
            <div class="insight-card-value">${scoreStats.avg || 0}<span style="font-size:16px;color:var(--text-dim)"> avg</span></div>
            <div class="insight-stat-row">
                <span class="insight-stat-pill" style="border-color:var(--green)">High (70+)<span class="pill-count">${scoreStats.high || 0}</span></span>
                <span class="insight-stat-pill" style="border-color:var(--orange)">Mid<span class="pill-count">${scoreStats.mid || 0}</span></span>
                <span class="insight-stat-pill" style="border-color:var(--red)">Low<span class="pill-count">${scoreStats.low || 0}</span></span>
            </div>
        </div>
        ${salaryStats.avg ? `<div class="insight-card">
            <div class="insight-card-title">💰 Salary Landscape</div>
            <div class="insight-card-value">$${(salaryStats.avg / 1000).toFixed(0)}K<span style="font-size:16px;color:var(--text-dim)"> avg</span></div>
            <div class="insight-card-sub">Range: $${(salaryStats.min / 1000).toFixed(0)}K - $${(salaryStats.max / 1000).toFixed(0)}K (${salaryStats.count} with salary data)</div>
        </div>` : ''}
        <div class="insight-card">
            <div class="insight-card-title">📨 Applications</div>
            <div class="insight-card-value">${data.applications}</div>
            <div class="insight-stat-row">
                ${Object.entries(data.app_status_distribution || {}).map(([status, count]) =>
                    `<span class="insight-stat-pill">${status}<span class="pill-count">${count}</span></span>`
                ).join('')}
            </div>
        </div>
    </div>`;

    // Distribution charts
    html += '<div class="insights-grid">';

    // Source distribution
    const sourceDist = data.source_distribution || {};
    const sourceTotal = Object.values(sourceDist).reduce((a, b) => a + b, 0) || 1;
    const sourceColors = { indeed: '#2164f3', linkedin: '#0a66c2', glassdoor: '#0caa41', usajobs: '#1a4480' };
    html += `<div class="insight-card">
        <div class="insight-card-title">🌐 Sources</div>
        <div class="distribution-bars">
            ${Object.entries(sourceDist).sort((a,b) => b[1]-a[1]).map(([name, count]) =>
                `<div class="dist-row">
                    <span class="dist-label">${esc(name)}</span>
                    <div class="dist-bar"><div class="dist-fill" style="width:${(count/sourceTotal)*100}%; background:${sourceColors[name.toLowerCase()] || 'var(--accent)'}"></div></div>
                    <span class="dist-value">${count}</span>
                </div>`
            ).join('')}
        </div>
    </div>`;

    // Seniority distribution
    const seniorityDist = data.seniority_distribution || {};
    const seniorityTotal = Object.values(seniorityDist).reduce((a, b) => a + b, 0) || 1;
    html += `<div class="insight-card">
        <div class="insight-card-title">📊 Seniority Mix</div>
        <div class="distribution-bars">
            ${Object.entries(seniorityDist).sort((a,b) => b[1]-a[1]).map(([name, count]) =>
                `<div class="dist-row">
                    <span class="dist-label">${esc(name)}</span>
                    <div class="dist-bar"><div class="dist-fill" style="width:${(count/seniorityTotal)*100}%; background:var(--accent)"></div></div>
                    <span class="dist-value">${count}</span>
                </div>`
            ).join('')}
        </div>
    </div>`;

    // Location distribution
    const locDist = data.location_distribution || {};
    const locTotal = Object.values(locDist).reduce((a, b) => a + b, 0) || 1;
    html += `<div class="insight-card">
        <div class="insight-card-title">📍 Top Locations</div>
        <div class="distribution-bars">
            ${Object.entries(locDist).sort((a,b) => b[1]-a[1]).slice(0, 8).map(([name, count]) =>
                `<div class="dist-row">
                    <span class="dist-label">${esc(name)}</span>
                    <div class="dist-bar"><div class="dist-fill" style="width:${(count/locTotal)*100}%; background:var(--green)"></div></div>
                    <span class="dist-value">${count}</span>
                </div>`
            ).join('')}
        </div>
    </div>`;

    // Remote distribution
    const remoteDist = data.remote_distribution || {};
    const remoteTotal = Object.values(remoteDist).reduce((a, b) => a + b, 0) || 1;
    const remoteColors = { remote: 'var(--green)', hybrid: 'var(--orange)', onsite: 'var(--accent)', unclear: 'var(--text-dim)' };
    html += `<div class="insight-card">
        <div class="insight-card-title">🏠 Remote Status</div>
        <div class="distribution-bars">
            ${Object.entries(remoteDist).sort((a,b) => b[1]-a[1]).map(([name, count]) =>
                `<div class="dist-row">
                    <span class="dist-label">${esc(name)}</span>
                    <div class="dist-bar"><div class="dist-fill" style="width:${(count/remoteTotal)*100}%; background:${remoteColors[name.toLowerCase()] || 'var(--accent)'}"></div></div>
                    <span class="dist-value">${count}</span>
                </div>`
            ).join('')}
        </div>
    </div>`;

    // Top companies
    const topCos = data.top_companies || [];
    if (topCos.length > 0) {
        html += `<div class="insight-card full-width">
            <div class="insight-card-title">🏢 Most Active Companies</div>
            <div class="insight-stat-row">
                ${topCos.map(c => `<span class="insight-stat-pill">${esc(c.name)}<span class="pill-count">${c.count}</span></span>`).join('')}
            </div>
        </div>`;
    }

    html += '</div>'; // end insights-grid

    // AI Insights
    const ai = data.ai_insights;
    if (ai) {
        html += '<div class="ai-insight-section">';
        html += '<h3>🤖 AI Market Intelligence</h3>';

        if (ai.market_summary) {
            html += `<div class="ai-market-summary">${esc(ai.market_summary)}</div>`;
        }

        if (ai.themes && ai.themes.length > 0) {
            html += '<div class="insight-card full-width" style="margin-bottom:16px"><div class="insight-card-title">📌 Key Themes</div>';
            ai.themes.forEach(t => { html += `<div class="ai-theme">${esc(t)}</div>`; });
            html += '</div>';
        }

        html += '<div class="insights-grid">';

        if (ai.opportunities && ai.opportunities.length > 0) {
            html += '<div class="insight-card"><div class="insight-card-title">✨ Opportunities</div>';
            ai.opportunities.forEach(o => { html += `<div class="ai-theme opportunity">${esc(o)}</div>`; });
            html += '</div>';
        }

        if (ai.risks && ai.risks.length > 0) {
            html += '<div class="insight-card"><div class="insight-card-title">⚠️ Risks & Concerns</div>';
            ai.risks.forEach(r => { html += `<div class="ai-theme risk">${esc(r)}</div>`; });
            html += '</div>';
        }

        if (ai.recommendations && ai.recommendations.length > 0) {
            html += '<div class="insight-card full-width"><div class="insight-card-title">💡 Strategic Recommendations</div>';
            ai.recommendations.forEach(r => { html += `<div class="ai-theme recommendation">${esc(r)}</div>`; });
            html += '</div>';
        }

        html += '</div>'; // end grid

        if (ai.salary_insight) {
            html += `<div class="insight-card full-width" style="margin-top:16px">
                <div class="insight-card-title">💵 Compensation Insight</div>
                <div class="research-block-text">${esc(ai.salary_insight)}</div>
            </div>`;
        }

        // Skill gaps and hot companies side by side
        html += '<div class="insights-grid" style="margin-top:16px">';
        if (ai.skill_gaps && ai.skill_gaps.length > 0) {
            html += `<div class="insight-card">
                <div class="insight-card-title">🎯 Skills to Highlight</div>
                <div class="skill-gap-tags">${ai.skill_gaps.map(s => `<span class="skill-gap-tag">${esc(s)}</span>`).join('')}</div>
            </div>`;
        }
        if (ai.hot_companies && ai.hot_companies.length > 0) {
            html += `<div class="insight-card">
                <div class="insight-card-title">🔥 Hot Companies</div>
                <div class="skill-gap-tags">${ai.hot_companies.map(c => `<span class="hot-company-tag">${esc(c)}</span>`).join('')}</div>
            </div>`;
        }
        if (ai.demand_signals && ai.demand_signals.length > 0) {
            html += `<div class="insight-card full-width">
                <div class="insight-card-title">📡 Demand Signals</div>
                ${ai.demand_signals.map(d => `<div class="ai-theme">${esc(d)}</div>`).join('')}
            </div>`;
        }
        html += '</div>';

        html += '</div>'; // end ai-insight-section
    }

    content.innerHTML = html;
}

// ── Research Detail Toggle ───────────────────────────────────────────────

function toggleResearchDetail(id, btn) {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.style.display === 'none') {
        el.style.display = 'block';
        btn.textContent = 'Hide Full Report';
    } else {
        el.style.display = 'none';
        btn.textContent = 'View Full Report';
    }
}

// ── Summary Sub-Tabs ────────────────────────────────────────────────────

function switchSummaryTab(tab) {
    document.querySelectorAll('.summary-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.summary-panel').forEach(p => p.classList.remove('active'));
    document.querySelector(`.summary-tab[data-subtab="${tab}"]`)?.classList.add('active');
    document.getElementById(`subtab-${tab}`)?.classList.add('active');

    if (tab === 'shortlist') loadShortlist();
    if (tab === 'advisor' && !document.getElementById('advisor-content')?.dataset.loaded) {
        // Don't auto-load - user clicks refresh
    }
    if (tab === 'overview') loadInsights();
}

// ── Shortlist View ──────────────────────────────────────────────────────

async function loadShortlist() {
    if (!state.profileId) return;
    const content = document.getElementById('shortlist-content');
    if (!content) return;

    content.innerHTML = `<div style="text-align:center;padding:40px">
        <svg width="40" height="40" viewBox="0 0 120 120" fill="none">
            <style>@keyframes o-sp{to{transform:rotate(360deg)}}@keyframes p-pl{0%,100%{opacity:.15}50%{opacity:.35}}</style>
            <path d="M55 56 L60 52 L65 56 L63 62 L57 62 Z" fill="#C4962C" style="animation:p-pl 1.8s ease-in-out infinite"/>
            <g style="animation:o-sp 1.8s linear infinite;transform-origin:60px 60px">
                <circle cx="60" cy="28" r="5" fill="none" stroke="#C4962C" stroke-width="1" opacity="0.5"/>
            </g>
            <circle cx="60" cy="60" r="32" fill="none" stroke="#E8E6E1" stroke-width="0.3" opacity="0.04" stroke-dasharray="3 5"/>
        </svg>
    </div>`;

    try {
        const jobs = await api(`/profiles/${state.profileId}/shortlist`);
        if (jobs.length === 0) {
            content.innerHTML = `<div class="empty-state">
                <div class="empty-state-art">
                    <svg width="200" height="160" viewBox="0 0 200 160" fill="none">
                        <rect x="40" y="100" width="120" height="8" rx="4" fill="#E8E6E1" opacity="0.06"/>
                        <rect x="48" y="100" width="3" height="24" rx="1" fill="#E8E6E1" opacity="0.04"/>
                        <rect x="149" y="100" width="3" height="24" rx="1" fill="#E8E6E1" opacity="0.04"/>
                        <circle cx="70" cy="80" r="14" fill="none" stroke="#E8E6E1" stroke-width="0.6" opacity="0.08" stroke-dasharray="3 3"/>
                        <circle cx="100" cy="80" r="14" fill="none" stroke="#E8E6E1" stroke-width="0.6" opacity="0.08" stroke-dasharray="3 3"/>
                        <circle cx="130" cy="80" r="14" fill="none" stroke="#E8E6E1" stroke-width="0.6" opacity="0.08" stroke-dasharray="3 3"/>
                        <path d="M96 74 Q92 68 88 72 Q84 76 90 82 Q94 86 100 84 Q106 82 108 76 Q110 70 104 68 Q98 66 96 74Z" fill="#C4962C" opacity="0.15" stroke="#C4962C" stroke-width="0.5"/>
                    </svg>
                </div>
                <h2>No shortlisted jobs yet</h2>
                <p>Use the ⭐ Shortlist button while browsing to save jobs you're interested in</p>
            </div>`;
            return;
        }

        let html = `<div class="shortlist-count">${jobs.length} shortlisted job${jobs.length !== 1 ? 's' : ''}</div>`;
        html += '<div class="shortlist-grid">';

        for (const job of jobs) {
            const scoreColor = job.match_score >= 70 ? 'var(--green)' :
                                job.match_score >= 40 ? 'var(--orange)' : 'var(--red)';
            const bd = job.match_breakdown;

            html += `<div class="shortlist-card" data-job-id="${job.id}">
                <div class="sl-header">
                    <div class="sl-score" style="color:${scoreColor}">${Math.round(job.match_score)}</div>
                    <div class="sl-info">
                        <div class="sl-title">${esc(job.title)}</div>
                        <div class="sl-company">${esc(job.company)}${job.location ? ` · ${esc(job.location)}` : ''}</div>
                    </div>
                </div>
                ${job.ai_synthesis ? `<div class="sl-synthesis">${esc(job.ai_synthesis)}</div>` : ''}
                ${job.salary_text ? `<div class="sl-salary">${esc(job.salary_text)}</div>` : ''}
                <div class="sl-actions">
                    <button class="btn btn-sm btn-primary" onclick="applyFromShortlist(${job.id})">Apply</button>
                    ${!job.deep_researched ? `<button class="btn btn-sm btn-secondary" onclick="triggerDeepResearch(${job.id})" id="deep-research-btn-${job.id}">🔬 Research</button>` : '<span class="sl-researched">🔬 Researched</span>'}
                    <button class="btn btn-sm btn-secondary" onclick="removeFromShortlist(${job.id})">Remove</button>
                    ${job.url ? `<a href="${esc(job.url)}" target="_blank" rel="noopener" class="btn btn-sm btn-secondary">View</a>` : ''}
                </div>
            </div>`;
        }

        html += '</div>';
        content.innerHTML = html;
    } catch (e) {
        content.innerHTML = `<div class="empty-state"><h2>Error</h2><p>${esc(e.message)}</p></div>`;
    }
}

async function applyFromShortlist(jobId) {
    try {
        const result = await api(`/jobs/${jobId}/swipe`, {
            method: 'POST',
            body: { action: 'like' },
        });
        if (result.status === 'failed') {
            toast(`Cannot apply: ${result.agent_result?.error || 'Job may no longer be active'}`, 'error');
        } else {
            toast('Application started!', 'success');
        }
        loadShortlist();
        loadStats();
    } catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}

async function removeFromShortlist(jobId) {
    try {
        await api(`/jobs/${jobId}/unshortlist`, { method: 'POST' });
        toast('Removed from shortlist', 'info');
        loadShortlist();
        loadStats();
    } catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}

// ── Search Advisor ──────────────────────────────────────────────────────

async function loadSearchAdvisor() {
    if (!state.profileId) return;
    const content = document.getElementById('advisor-content');
    if (!content) return;

    content.innerHTML = `<div style="text-align:center;padding:40px">
        <svg width="48" height="48" viewBox="0 0 120 120" fill="none">
            <style>@keyframes adv-spin{to{transform:rotate(360deg)}}@keyframes adv-pulse{0%,100%{opacity:.15}50%{opacity:.35}}</style>
            <path d="M55 56 L60 52 L65 56 L63 62 L57 62 Z" fill="#C4962C" style="animation:adv-pulse 1.8s ease-in-out infinite"/>
            <g style="animation:adv-spin 1.8s linear infinite;transform-origin:60px 60px">
                <circle cx="60" cy="28" r="5" fill="none" stroke="#C4962C" stroke-width="1" opacity="0.5"/>
                <path d="M57.5 25.5 Q60 28 57.5 30.5" fill="none" stroke="#8B6914" stroke-width="0.4" opacity="0.4"/>
                <path d="M62.5 25.5 Q60 28 62.5 30.5" fill="none" stroke="#8B6914" stroke-width="0.4" opacity="0.4"/>
            </g>
            <circle cx="60" cy="60" r="32" fill="none" stroke="#E8E6E1" stroke-width="0.3" opacity="0.04" stroke-dasharray="3 5"/>
        </svg>
        <p style="color:var(--text-dim);margin-top:16px">Analyzing your search strategy...</p>
    </div>`;
    content.dataset.loaded = 'true';

    try {
        const data = await api(`/profiles/${state.profileId}/search-advisor`);
        renderSearchAdvisor(data);
    } catch (e) {
        content.innerHTML = `<div class="empty-state">
            <div class="empty-state-art">
                <svg width="200" height="160" viewBox="0 0 200 160" fill="none">
                    <g transform="translate(60, 30)">
                        <line x1="20" y1="80" x2="70" y2="30" stroke="#C4962C" stroke-width="3" stroke-linecap="round" opacity="0.4"/>
                        <circle cx="20" cy="80" r="5" fill="#E8E6E1" opacity="0.08"/>
                        <path d="M75 25 Q85 20 80 10" stroke="#E8E6E1" stroke-width="1" opacity="0.1" fill="none"/>
                        <path d="M78 30 Q90 28 88 18" stroke="#E8E6E1" stroke-width="0.8" opacity="0.08" fill="none"/>
                        <path d="M72 22 Q78 12 72 6" stroke="#E8E6E1" stroke-width="0.8" opacity="0.06" fill="none"/>
                    </g>
                    <circle cx="80" cy="120" r="4" fill="#E8E6E1" opacity="0.04"/>
                    <circle cx="90" cy="116" r="6" fill="#E8E6E1" opacity="0.03"/>
                    <circle cx="100" cy="120" r="5" fill="#E8E6E1" opacity="0.03"/>
                </svg>
            </div>
            <h2>Swing and a miss</h2><p>${esc(e.message)} — try again in a moment</p>
        </div>`;
    }
}

function renderSearchAdvisor(data) {
    const content = document.getElementById('advisor-content');
    if (!content) return;

    if (!data.advisor) {
        content.innerHTML = `<div class="empty-state">
            <div class="empty-state-art">
                <svg width="120" height="120" viewBox="0 0 120 120" fill="none">
                    <g transform="translate(28, 28)">
                        <circle cx="32" cy="32" r="30" fill="none" stroke="#E8E6E1" stroke-width="0.5" opacity="0.08"/>
                        <circle cx="32" cy="32" r="22" fill="none" stroke="#E8E6E1" stroke-width="0.5" opacity="0.1"/>
                        <circle cx="32" cy="32" r="14" fill="none" stroke="#C4962C" stroke-width="0.6" opacity="0.15"/>
                        <circle cx="32" cy="32" r="6" fill="#C4962C" opacity="0.15"/>
                        <circle cx="32" cy="32" r="2" fill="#C4962C" opacity="0.5"/>
                    </g>
                </svg>
            </div>
            <h2>${data.reason || 'Unable to generate analysis'}</h2>
            <p>Search for more jobs and browse results to enable AI analysis</p>
        </div>`;
        return;
    }

    const a = data.advisor;
    let html = '';

    // Overall assessment
    if (a.overall_assessment) {
        html += `<div class="advisor-assessment">
            <div class="advisor-assessment-text">${esc(a.overall_assessment)}</div>
        </div>`;
    }

    // Top row: Market Fit + Ambition Assessment side by side
    html += '<div class="advisor-top-row">';

    // Market fit score
    if (a.market_fit_score !== undefined) {
        const fitColor = a.market_fit_score >= 70 ? 'var(--green)' :
                         a.market_fit_score >= 45 ? 'var(--orange)' : 'var(--red)';
        html += `<div class="advisor-market-fit">
            <div class="advisor-fit-score" style="color:${fitColor}">${a.market_fit_score}</div>
            <div class="advisor-fit-label">Market Fit Score</div>
        </div>`;
    }

    // Ambition assessment
    if (a.ambition_assessment) {
        const ambIcon = a.ambition_assessment.verdict === 'just_right' ? '✅' :
                        a.ambition_assessment.verdict === 'too_high' ? '🔺' :
                        a.ambition_assessment.verdict === 'too_low' ? '🔻' : '↔️';
        const ambLabel = a.ambition_assessment.verdict === 'just_right' ? 'Well Calibrated' :
                         a.ambition_assessment.verdict === 'too_high' ? 'Aiming High' :
                         a.ambition_assessment.verdict === 'too_low' ? 'Aiming Low' : 'Mixed Targeting';
        const ambColor = a.ambition_assessment.verdict === 'just_right' ? 'var(--green)' :
                         a.ambition_assessment.verdict === 'too_low' ? 'var(--orange)' : 'var(--accent)';
        html += `<div class="advisor-ambition">
            <div class="ambition-icon" style="color:${ambColor}">${ambIcon}</div>
            <div class="ambition-label" style="color:${ambColor}">${ambLabel}</div>
            <div class="ambition-confidence">${a.ambition_assessment.confidence || '?'}% confidence</div>
        </div>`;
    }

    html += '</div>'; // end top row

    // Career trajectory analysis - prominent section
    if (a.career_trajectory_analysis) {
        const ct = a.career_trajectory_analysis;
        const realismColor = ct.target_realism === 'realistic' ? 'var(--green)' :
                             ct.target_realism === 'stretch' ? 'var(--orange)' :
                             ct.target_realism === 'significant_stretch' ? '#ff6b6b' : 'var(--red)';
        const realismLabel = ct.target_realism === 'realistic' ? 'Realistic Target' :
                             ct.target_realism === 'stretch' ? 'Stretch Target' :
                             ct.target_realism === 'significant_stretch' ? 'Significant Stretch' : 'Unrealistic';
        html += `<div class="advisor-trajectory">
            <div class="trajectory-header">
                <h3>Career Trajectory Analysis</h3>
                <span class="trajectory-realism" style="color:${realismColor};border-color:${realismColor}">${realismLabel}</span>
            </div>
            <div class="trajectory-narrative">${esc(ct.trajectory_narrative || '')}</div>
            <div class="trajectory-details">
                <div class="trajectory-item">
                    <span class="trajectory-label">Assessed Level:</span>
                    <span class="trajectory-value">${esc(ct.current_level || '?')}</span>
                </div>
                <div class="trajectory-item">
                    <span class="trajectory-label">Recommended Target:</span>
                    <span class="trajectory-value">${esc(ct.recommended_level || '?')}</span>
                </div>
            </div>
            ${ct.gap_to_target ? `<div class="trajectory-gap"><strong>Gap to target:</strong> ${esc(ct.gap_to_target)}</div>` : ''}
        </div>`;
    }

    // Ambition explanation
    if (a.ambition_assessment && a.ambition_assessment.explanation) {
        html += `<div class="advisor-ambition-detail">
            <div class="ai-theme recommendation">${esc(a.ambition_assessment.explanation)}</div>
        </div>`;
    }

    // Search strategy verdict
    if (a.search_strategy) {
        const verdictIcon = a.search_strategy.verdict === 'on_track' ? '✅' :
                            a.search_strategy.verdict === 'needs_adjustment' ? '⚠️' : '🔴';
        const verdictLabel = a.search_strategy.verdict === 'on_track' ? 'On Track' :
                             a.search_strategy.verdict === 'needs_adjustment' ? 'Needs Adjustment' : 'Significantly Off';
        html += `<div class="advisor-verdict">
            <span class="verdict-icon">${verdictIcon}</span>
            <span class="verdict-label">${verdictLabel}</span>
            <div class="verdict-explain">${esc(a.search_strategy.explanation)}</div>
        </div>`;
    }

    // Profile suggestions - actionable cards with accept buttons
    if (a.profile_suggestions && a.profile_suggestions.length > 0) {
        window._advisorSuggestions = a.profile_suggestions;
        html += `<div class="advisor-suggestions">
            <h3>Suggested Profile Changes</h3>
            <p class="suggestions-subtitle">The AI advisor recommends these changes to improve your job matches</p>
            <div class="suggestion-cards">
                ${a.profile_suggestions.map((s, i) => {
                    const isArray = ['target_roles', 'skills', 'target_locations'].includes(s.field);
                    const suggestedArr = isArray ? (Array.isArray(s.suggested_value) ? s.suggested_value : [s.suggested_value]) : [];
                    const currentArr = isArray ? (Array.isArray(s.current_value) ? s.current_value : []) : [];

                    if (isArray) {
                        // Find items to add (in suggested but not in current)
                        const toAdd = suggestedArr.filter(v => !currentArr.some(c => c.toLowerCase() === v.toLowerCase()));
                        // Find items suggested for removal (in current but not in suggested)
                        const toRemove = currentArr.filter(v => !suggestedArr.some(c => c.toLowerCase() === v.toLowerCase()));
                        // Items kept in both
                        const kept = currentArr.filter(v => suggestedArr.some(c => c.toLowerCase() === v.toLowerCase()));

                        let itemsHtml = '';
                        if (toAdd.length > 0) {
                            itemsHtml += `<div class="suggestion-group-label" style="color:var(--green)">Add:</div>`;
                            itemsHtml += toAdd.map((v, j) => `<label class="suggestion-item-pick add">
                                <input type="checkbox" checked data-suggestion="${i}" data-action="add" data-value="${esc(v)}">
                                <span>+ ${esc(v)}</span>
                            </label>`).join('');
                        }
                        if (toRemove.length > 0) {
                            itemsHtml += `<div class="suggestion-group-label" style="color:var(--red);margin-top:6px">Remove:</div>`;
                            itemsHtml += toRemove.map((v, j) => `<label class="suggestion-item-pick remove">
                                <input type="checkbox" data-suggestion="${i}" data-action="remove" data-value="${esc(v)}">
                                <span>✕ ${esc(v)}</span>
                            </label>`).join('');
                        }
                        if (toAdd.length === 0 && toRemove.length === 0) {
                            itemsHtml = '<div style="color:var(--text-dim);font-size:12px">No changes needed — already matches recommendation</div>';
                        }

                        return `<div class="suggestion-card" id="suggestion-${i}">
                            <div class="suggestion-field">${esc(s.field.replace(/_/g, ' '))}</div>
                            <div class="suggestion-reason">${esc(s.reason)}</div>
                            <div class="suggestion-items">${itemsHtml}</div>
                            <div class="suggestion-actions">
                                <button class="btn btn-sm btn-success" onclick="applyArraySuggestion(${i})">Apply Selected</button>
                                <button class="btn btn-sm btn-ghost" onclick="document.getElementById('suggestion-${i}').classList.add('dismissed')">Dismiss</button>
                            </div>
                        </div>`;
                    } else {
                        // Scalar field — simple accept/dismiss
                        const displayVal = s.suggested_value;
                        const currentVal = s.current_value;
                        return `<div class="suggestion-card" id="suggestion-${i}">
                            <div class="suggestion-field">${esc(s.field.replace(/_/g, ' '))}</div>
                            <div class="suggestion-change">
                                <span class="suggestion-from">${esc(String(currentVal || 'not set'))}</span>
                                <span class="suggestion-arrow">→</span>
                                <span class="suggestion-to">${esc(String(displayVal || ''))}</span>
                            </div>
                            <div class="suggestion-reason">${esc(s.reason)}</div>
                            <div class="suggestion-actions">
                                <button class="btn btn-sm btn-success" onclick="acceptSuggestion(${i})">Accept</button>
                                <button class="btn btn-sm btn-ghost" onclick="document.getElementById('suggestion-${i}').classList.add('dismissed')">Dismiss</button>
                            </div>
                        </div>`;
                    }
                }).join('')}
            </div>
        </div>`;
    }

    // Roles to consider
    if (a.roles_to_consider && a.roles_to_consider.length > 0) {
        html += `<div class="advisor-roles">
            <h3>Roles to Consider</h3>
            <div class="skill-gap-tags">${a.roles_to_consider.map(r => `<span class="hot-company-tag">${esc(r)}</span>`).join('')}</div>
        </div>`;
    }

    html += '<div class="insights-grid">';

    // Quick wins
    if (a.quick_wins && a.quick_wins.length > 0) {
        html += `<div class="insight-card full-width advisor-card">
            <div class="insight-card-title">Quick Wins</div>
            ${a.quick_wins.map(w => `<div class="ai-theme recommendation">${esc(w)}</div>`).join('')}
        </div>`;
    }

    // Resume feedback
    if (a.resume_feedback && a.resume_feedback.length > 0) {
        html += `<div class="insight-card advisor-card">
            <div class="insight-card-title">Resume Feedback</div>
            ${a.resume_feedback.map(f => `<div class="ai-theme">${esc(f)}</div>`).join('')}
        </div>`;
    }

    // Positioning tips
    if (a.positioning_tips && a.positioning_tips.length > 0) {
        html += `<div class="insight-card full-width advisor-card">
            <div class="insight-card-title">Positioning Strategy</div>
            ${a.positioning_tips.map(t => `<div class="ai-theme recommendation">${esc(t)}</div>`).join('')}
        </div>`;
    }

    // Skills
    if (a.skills_to_highlight && a.skills_to_highlight.length > 0) {
        html += `<div class="insight-card advisor-card">
            <div class="insight-card-title">Skills to Highlight</div>
            <div class="skill-gap-tags">${a.skills_to_highlight.map(s => `<span class="hot-company-tag">${esc(s)}</span>`).join('')}</div>
        </div>`;
    }

    if (a.skills_to_develop && a.skills_to_develop.length > 0) {
        html += `<div class="insight-card advisor-card">
            <div class="insight-card-title">Skills to Develop</div>
            <div class="skill-gap-tags">${a.skills_to_develop.map(s => `<span class="skill-gap-tag">${esc(s)}</span>`).join('')}</div>
        </div>`;
    }

    html += '</div>'; // end grid

    // Questions the advisor wants answered
    if (a.questions_to_explore && a.questions_to_explore.length > 0) {
        html += `<div class="advisor-questions-hint">
            <p>The advisor generated <strong>${a.questions_to_explore.length} new profile questions</strong> to refine its assessment. Answer them in your Profile tab to get better advice next time.</p>
        </div>`;
    }

    content.innerHTML = html;
}

async function acceptSuggestion(index) {
    const suggestion = window._advisorSuggestions?.[index];
    if (!suggestion) { toast('Suggestion not found', 'error'); return; }

    const card = document.getElementById('suggestion-' + index);
    let value = suggestion.suggested_value;

    // Clean up the value — AI sometimes returns strings that should be arrays
    if (['target_roles', 'skills', 'target_locations'].includes(suggestion.field)) {
        if (typeof value === 'string') {
            // Try parsing as JSON array
            try { value = JSON.parse(value); } catch (e) {
                // Split comma-separated string into array
                value = value.split(',').map(s => s.trim().replace(/^["']+|["']+$/g, '')).filter(Boolean);
            }
        }
        if (!Array.isArray(value)) value = [value];
        // Filter out any narrative instructions the AI snuck in
        value = value.filter(v => typeof v === 'string' && v.length < 100 && !v.toLowerCase().startsWith('remove ') && !v.toLowerCase().startsWith('add '));
    }

    try {
        const loadId = toast('Applying suggestion...', 'loading');
        await api(`/profiles/${state.profileId}/apply-advisor-suggestion`, {
            method: 'POST',
            body: { field: suggestion.field, value: value },
        });
        dismissToast(loadId);

        // Sync local state for array fields so profile form updates immediately
        const tagMap = { target_roles: 'roles', skills: 'skills', target_locations: 'locations' };
        if (tagMap[suggestion.field] && Array.isArray(value)) {
            state.tags[tagMap[suggestion.field]] = value;
            if (state.profile) {
                state.profile[tagMap[suggestion.field]] = value;
            }
            // Re-render tags if profile form is visible
            try { renderTags(tagMap[suggestion.field]); } catch (e) {}
        }

        // Sync scalar fields to local state
        if (suggestion.field === 'min_salary' && state.profile) state.profile.min_salary = value;
        if (suggestion.field === 'max_salary' && state.profile) state.profile.max_salary = value;
        if (suggestion.field === 'seniority_level' && state.profile) state.profile.seniority_level = value;

        toast(`Updated ${suggestion.field.replace(/_/g, ' ')}`, 'success');
        if (card) {
            card.classList.add('applied');
            const displayVal = Array.isArray(value) ? value.join(', ') : value;
            card.querySelector('.suggestion-actions').innerHTML = `<span style="color:var(--green)">✓ Applied: ${esc(String(displayVal))}</span>`;
        }
    } catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}


window.applyArraySuggestion = applyArraySuggestion;


window.applyArraySuggestion = applyArraySuggestion;

async function applyArraySuggestion(index) {
    const suggestion = window._advisorSuggestions?.[index];
    if (!suggestion) { toast('Suggestion not found', 'error'); return; }

    const card = document.getElementById('suggestion-' + index);
    if (!card) return;

    const tagMap = { target_roles: 'roles', skills: 'skills', target_locations: 'locations' };
    const tagKey = tagMap[suggestion.field];
    if (!tagKey) { toast('Unknown array field', 'error'); return; }

    // Read checked items
    const checkboxes = card.querySelectorAll('input[type="checkbox"]');
    const toAdd = [];
    const toRemove = [];
    checkboxes.forEach(cb => {
        if (!cb.checked) return;
        const action = cb.dataset.action;
        const value = cb.dataset.value;
        if (action === 'add') toAdd.push(value);
        if (action === 'remove') toRemove.push(value);
    });

    if (toAdd.length === 0 && toRemove.length === 0) {
        toast('No changes selected', 'info');
        return;
    }

    // Apply changes to local state
    let current = [...(state.tags[tagKey] || [])];
    // Add new items (avoid duplicates)
    for (const item of toAdd) {
        if (!current.some(c => c.toLowerCase() === item.toLowerCase())) {
            current.push(item);
        }
    }
    // Remove selected items
    for (const item of toRemove) {
        current = current.filter(c => c.toLowerCase() !== item.toLowerCase());
    }

    try {
        const loadId = toast('Applying changes...', 'loading');
        await api(`/profiles/${state.profileId}`, {
            method: 'PUT',
            body: { [suggestion.field]: current }
        });
        dismissToast(loadId);

        // Sync local state
        state.tags[tagKey] = current;
        if (state.profile) state.profile[suggestion.field] = current;
        try { renderTags(tagKey); } catch (e) {}

        const changes = [];
        if (toAdd.length) changes.push(`+${toAdd.length} added`);
        if (toRemove.length) changes.push(`-${toRemove.length} removed`);
        toast(`Updated ${suggestion.field.replace(/_/g, ' ')}: ${changes.join(', ')}`, 'success');

        card.classList.add('applied');
        card.querySelector('.suggestion-actions').innerHTML = `<span style="color:var(--green)">✓ Applied: ${changes.join(', ')}</span>`;
    } catch (e) {
        toast('Failed: ' + e.message, 'error');
    }
}

window.applyArraySuggestion = applyArraySuggestion;

// Expose for inline onclick handlers
window.showView = showView;
window.showAppDetail = showAppDetail;
window.rescoreJobs = rescoreJobs;
window.reanalyzeProfile = reanalyzeProfile;
window.verifyAllJobs = verifyAllJobs;
window.reenrichCompanies = reenrichCompanies;
window.submitSingleAnswer = submitSingleAnswer;
window.markSubmitted = markSubmitted;
window.copyToClipboard = copyToClipboard;
window.returnToBrowse = returnToBrowse;
window.hideApplication = hideApplication;
window.unhideApplication = unhideApplication;
window.viewAutomationPlan = viewAutomationPlan;
window.toggleHiddenApps = toggleHiddenApps;
window.generateInterviewQuestions = generateInterviewQuestions;
window.submitInterviewAnswer = submitInterviewAnswer;
window.toggleAnswered = toggleAnswered;
window.dedupJobs = dedupJobs;
window.autoApply = autoApply;
window.triggerDeepResearch = triggerDeepResearch;
window.deepResearchShortlist = deepResearchShortlist;
window.loadInsights = loadInsights;
window.toggleResearchDetail = toggleResearchDetail;
window.switchSummaryTab = switchSummaryTab;
window.loadShortlist = loadShortlist;
window.applyFromShortlist = applyFromShortlist;
window.removeFromShortlist = removeFromShortlist;
window.loadSearchAdvisor = loadSearchAdvisor;
window.acceptSuggestion = acceptSuggestion;
window.checkApplicationEmails = checkApplicationEmails;
window.processEmailPaste = processEmailPaste;
window.switchBrowseMode = switchBrowseMode;
window.selectJobFromList = selectJobFromList;
window.quickAction = quickAction;
window.closeJobDetail = closeJobDetail;
window.searchJobs = searchJobs;
window.browserSearchJobs = browserSearchJobs;
window.startBrowserScrape = startBrowserScrape;
window.closeBrowserSearchModal = closeBrowserSearchModal;

// ── Browser Search Integration ──────────────────────────────────────────




window.openAllSiteUrls = function(siteKey) {
    const config = window._browserSearchConfig;
    if (!config) return;

    const urls = config.configs.filter(c => c.site === siteKey);
    for (const u of urls) {
        window.open(u.url, '_blank');
    }
    toast(`Opened ${urls.length} ${siteKey} search tabs`, 'info');
};


window.showExtractorScript = function(siteKey) {
    const config = window._browserSearchConfig;
    if (!config) return;

    const siteConfigs = config.configs.filter(c => c.site === siteKey);
    const script = siteConfigs[0]?.extractor || '';

    // Show a copyable script
    const modal = document.createElement('div');
    modal.className = 'modal-overlay';
    modal.id = 'extractor-script-modal';
    modal.innerHTML = `
        <div class="modal" style="max-width:600px;max-height:80vh">
            <div class="modal-header">
                <h3 style="margin:0">Extract Jobs from ${siteKey}</h3>
                <button class="btn-close" onclick="document.getElementById('extractor-script-modal')?.remove()">×</button>
            </div>
            <div style="padding:16px">
                <p style="color:var(--text-dim);font-size:13px;margin:0 0 12px">
                    1. Go to your ${siteKey} search results tab<br>
                    2. Open DevTools (F12) → Console<br>
                    3. Paste this script and press Enter<br>
                    4. Copy the JSON output<br>
                    5. Come back here and click "Paste from Console"
                </p>
                <textarea id="extractor-script-text" readonly style="width:100%;height:200px;font-family:var(--mono);
                    font-size:11px;background:var(--bg);color:var(--text);border:1px solid var(--border);
                    border-radius:var(--radius-sm);padding:8px;resize:vertical">${esc(script)}</textarea>
                <button class="btn btn-primary btn-sm" style="margin-top:8px" onclick="navigator.clipboard.writeText(document.getElementById('extractor-script-text').value);toast('Copied to clipboard','success')">
                    Copy Script
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) modal.remove();
    });
};

window.pasteJobsFromClipboard = async function(siteKey) {
    try {
        const text = await navigator.clipboard.readText();
        let jobs;
        try {
            jobs = JSON.parse(text);
        } catch (e) {
            toast('Clipboard does not contain valid JSON. Run the extractor script first.', 'error');
            return;
        }

        if (!Array.isArray(jobs) || jobs.length === 0) {
            toast('No jobs found in clipboard data', 'error');
            return;
        }

        // Ensure source is set
        jobs = jobs.map(j => ({ ...j, source: j.source || siteKey || 'browser' }));

        await importBrowserJobs(jobs);
    } catch (e) {
        // Clipboard API might be blocked - offer a textarea paste instead
        showPasteArea(siteKey);
    }
};


window.importPastedJobs = async function(siteKey) {
    const textarea = document.getElementById('paste-jobs-area');
    if (!textarea) return;

    let jobs;
    try {
        jobs = JSON.parse(textarea.value);
    } catch (e) {
        toast('Invalid JSON. Make sure you copied the full output.', 'error');
        return;
    }

    if (!Array.isArray(jobs) || jobs.length === 0) {
        toast('No jobs found in pasted data', 'error');
        return;
    }

    jobs = jobs.map(j => ({ ...j, source: j.source || siteKey || 'browser' }));
    await importBrowserJobs(jobs);
};

window.manualJobEntry = function() {
    const resultsDiv = document.getElementById('browser-scrape-results');
    if (!resultsDiv) return;

    resultsDiv.style.display = 'block';
    const logDiv = document.getElementById('browser-scrape-log');
    logDiv.innerHTML = `
        <div style="margin-bottom:8px">Add a job manually:</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
            <input id="manual-job-title" placeholder="Job Title" class="input" style="font-size:12px">
            <input id="manual-job-company" placeholder="Company" class="input" style="font-size:12px">
            <input id="manual-job-location" placeholder="Location" class="input" style="font-size:12px">
            <input id="manual-job-url" placeholder="URL (optional)" class="input" style="font-size:12px">
        </div>
        <button class="btn btn-primary btn-sm" onclick="submitManualJob()">Add Job</button>
    `;
};

window.submitManualJob = async function() {
    const title = document.getElementById('manual-job-title')?.value?.trim();
    const company = document.getElementById('manual-job-company')?.value?.trim();
    const location = document.getElementById('manual-job-location')?.value?.trim();
    const url = document.getElementById('manual-job-url')?.value?.trim();

    if (!title || !company) {
        toast('Title and company are required', 'error');
        return;
    }

    await importBrowserJobs([{
        title, company, location: location || '', url: url || '',
        source: 'manual', description: '',
    }]);
};

window.browserSearchJobs = browserSearchJobs;
window.startBrowserScrape = startBrowserScrape;
window.closeBrowserSearchModal = closeBrowserSearchModal;

// ── Browser Search Integration ──────────────────────────────────────────




window.openAllSiteUrls = function(siteKey) {
    const config = window._browserSearchConfig;
    if (!config) return;

    const urls = config.configs.filter(c => c.site === siteKey);
    for (const u of urls) {
        window.open(u.url, '_blank');
    }
    toast(`Opened ${urls.length} ${siteKey} search tabs`, 'info');
};


window.showExtractorScript = function(siteKey) {
    const config = window._browserSearchConfig;
    if (!config) return;

    const siteConfigs = config.configs.filter(c => c.site === siteKey);
    const script = siteConfigs[0]?.extractor || '';

    // Show a copyable script
    const modal = document.createElement('div');
    modal.className = 'modal-overlay';
    modal.id = 'extractor-script-modal';
    modal.innerHTML = `
        <div class="modal" style="max-width:600px;max-height:80vh">
            <div class="modal-header">
                <h3 style="margin:0">Extract Jobs from ${siteKey}</h3>
                <button class="btn-close" onclick="document.getElementById('extractor-script-modal')?.remove()">×</button>
            </div>
            <div style="padding:16px">
                <p style="color:var(--text-dim);font-size:13px;margin:0 0 12px">
                    1. Go to your ${siteKey} search results tab<br>
                    2. Open DevTools (F12) → Console<br>
                    3. Paste this script and press Enter<br>
                    4. Copy the JSON output<br>
                    5. Come back here and click "Paste from Console"
                </p>
                <textarea id="extractor-script-text" readonly style="width:100%;height:200px;font-family:var(--mono);
                    font-size:11px;background:var(--bg);color:var(--text);border:1px solid var(--border);
                    border-radius:var(--radius-sm);padding:8px;resize:vertical">${esc(script)}</textarea>
                <button class="btn btn-primary btn-sm" style="margin-top:8px" onclick="navigator.clipboard.writeText(document.getElementById('extractor-script-text').value);toast('Copied to clipboard','success')">
                    Copy Script
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) modal.remove();
    });
};

window.pasteJobsFromClipboard = async function(siteKey) {
    try {
        const text = await navigator.clipboard.readText();
        let jobs;
        try {
            jobs = JSON.parse(text);
        } catch (e) {
            toast('Clipboard does not contain valid JSON. Run the extractor script first.', 'error');
            return;
        }

        if (!Array.isArray(jobs) || jobs.length === 0) {
            toast('No jobs found in clipboard data', 'error');
            return;
        }

        // Ensure source is set
        jobs = jobs.map(j => ({ ...j, source: j.source || siteKey || 'browser' }));

        await importBrowserJobs(jobs);
    } catch (e) {
        // Clipboard API might be blocked - offer a textarea paste instead
        showPasteArea(siteKey);
    }
};


window.importPastedJobs = async function(siteKey) {
    const textarea = document.getElementById('paste-jobs-area');
    if (!textarea) return;

    let jobs;
    try {
        jobs = JSON.parse(textarea.value);
    } catch (e) {
        toast('Invalid JSON. Make sure you copied the full output.', 'error');
        return;
    }

    if (!Array.isArray(jobs) || jobs.length === 0) {
        toast('No jobs found in pasted data', 'error');
        return;
    }

    jobs = jobs.map(j => ({ ...j, source: j.source || siteKey || 'browser' }));
    await importBrowserJobs(jobs);
};

window.manualJobEntry = function() {
    const resultsDiv = document.getElementById('browser-scrape-results');
    if (!resultsDiv) return;

    resultsDiv.style.display = 'block';
    const logDiv = document.getElementById('browser-scrape-log');
    logDiv.innerHTML = `
        <div style="margin-bottom:8px">Add a job manually:</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
            <input id="manual-job-title" placeholder="Job Title" class="input" style="font-size:12px">
            <input id="manual-job-company" placeholder="Company" class="input" style="font-size:12px">
            <input id="manual-job-location" placeholder="Location" class="input" style="font-size:12px">
            <input id="manual-job-url" placeholder="URL (optional)" class="input" style="font-size:12px">
        </div>
        <button class="btn btn-primary btn-sm" onclick="submitManualJob()">Add Job</button>
    `;
};

window.submitManualJob = async function() {
    const title = document.getElementById('manual-job-title')?.value?.trim();
    const company = document.getElementById('manual-job-company')?.value?.trim();
    const location = document.getElementById('manual-job-location')?.value?.trim();
    const url = document.getElementById('manual-job-url')?.value?.trim();

    if (!title || !company) {
        toast('Title and company are required', 'error');
        return;
    }

    await importBrowserJobs([{
        title, company, location: location || '', url: url || '',
        source: 'manual', description: '',
    }]);
};

window.browserSearchJobs = browserSearchJobs;
window.startBrowserScrape = startBrowserScrape;
window.closeBrowserSearchModal = closeBrowserSearchModal;

// ── Browser Search Integration ──────────────────────────────────────────

async function browserSearchJobs() {
    if (!state.profileId) { toast('Create a profile first', 'error'); return; }

    showActivity('Loading browser search...');
    try {
        const config = await api(`/profiles/${state.profileId}/browser-search-config`);
        showBrowserSearchModal(config);
    } catch (e) {
        toast('Failed to load browser search config: ' + e.message, 'error');
    } finally {
        hideActivity();
    }
}

function showBrowserSearchModal(config) {
    // Remove existing modal if any
    document.getElementById('browser-search-modal')?.remove();

    // Group configs by site
    const bySite = {};
    for (const c of config.configs) {
        const siteLabel = {
            'indeed': 'Indeed (US)', 'indeed_ca': 'Indeed (Canada)',
            'glassdoor': 'Glassdoor', 'linkedin': 'LinkedIn',
            'zip_recruiter': 'ZipRecruiter', 'monster': 'Monster',
        }[c.site] || c.site;
        if (!bySite[c.site]) bySite[c.site] = { label: siteLabel, urls: [] };
        bySite[c.site].urls.push(c);
    }

    const modal = document.createElement('div');
    modal.id = 'browser-search-modal';
    modal.className = 'modal-overlay';
    modal.innerHTML = `
        <div class="modal" style="max-width:680px;max-height:85vh;overflow-y:auto">
            <div class="modal-header">
                <h3 style="margin:0">🌐 Browser Search</h3>
                <button class="btn-close" onclick="closeBrowserSearchModal()">×</button>
            </div>
            <div style="padding:16px">
                <p style="color:var(--text-dim);margin:0 0 12px;font-size:13px">
                    Search job sites using your logged-in browser sessions. Click a site to open it,
                    then click "Scrape Results" to extract jobs from the page. Your authentication
                    cookies make this work where API scraping gets blocked.
                </p>

                <div class="browser-search-sites" style="display:flex;flex-direction:column;gap:8px">
                    ${Object.entries(bySite).map(([siteKey, site]) => `
                        <div class="browser-search-site" style="background:var(--bg-section);border:1px solid var(--border);border-radius:var(--radius);padding:12px">
                            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
                                <strong style="color:var(--text)">${site.label}</strong>
                                <div style="display:flex;gap:6px">
                                    <button class="btn btn-sm btn-secondary" onclick="openAllSiteUrls('${siteKey}')" title="Open all search URLs in new tabs">
                                        Open All (${site.urls.length})
                                    </button>
                                    <button class="btn btn-sm btn-primary" onclick="startBrowserScrape('${siteKey}')" id="scrape-btn-${siteKey}">
                                        Scrape Results
                                    </button>
                                </div>
                            </div>
                            <div style="display:flex;flex-wrap:wrap;gap:4px">
                                ${site.urls.map((u, i) => `
                                    <a href="${esc(u.url)}" target="_blank" rel="noopener"
                                       class="browser-search-link"
                                       style="font-size:11px;color:var(--accent-light);text-decoration:none;
                                              background:var(--bg-card);padding:3px 8px;border-radius:4px;
                                              border:1px solid var(--border);white-space:nowrap;max-width:280px;
                                              overflow:hidden;text-overflow:ellipsis;display:inline-block"
                                       title="${esc(u.query)} in ${esc(u.location)}">
                                        ${esc(u.query)} · ${esc(u.location)}
                                    </a>
                                `).join('')}
                            </div>
                            <div id="scrape-status-${siteKey}" style="display:none;margin-top:8px;font-size:12px;color:var(--text-dim)"></div>
                        </div>
                    `).join('')}
                </div>

                <div style="margin-top:16px;padding:12px;background:var(--bg-card);border-radius:var(--radius);border:1px solid var(--border)">
                    <strong style="color:var(--text);font-size:13px">💡 How it works</strong>
                    <ol style="color:var(--text-dim);font-size:12px;margin:8px 0 0;padding-left:20px;line-height:1.6">
                        <li>Click <strong>"Open All"</strong> to open search results in new tabs</li>
                        <li>Make sure the pages have loaded (log in if needed)</li>
                        <li>Come back here and click <strong>"Scrape Results"</strong></li>
                        <li>Jobs get imported, deduped, scored, and appear in your feed</li>
                    </ol>
                </div>

                <div id="browser-scrape-results" style="display:none;margin-top:12px;padding:12px;background:var(--bg-section);border-radius:var(--radius);border:1px solid var(--border)">
                    <strong style="color:var(--text)">Import Results</strong>
                    <div id="browser-scrape-log" style="font-size:12px;color:var(--text-dim);margin-top:6px"></div>
                </div>
            </div>
        </div>
    `;

    // Store config for scraping
    window._browserSearchConfig = config;

    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeBrowserSearchModal();
    });
}

function closeBrowserSearchModal() {
    document.getElementById('browser-search-modal')?.remove();
    window._browserSearchConfig = null;
}

window.openAllSiteUrls = function(siteKey) {
    const config = window._browserSearchConfig;
    if (!config) return;

    const urls = config.configs.filter(c => c.site === siteKey);
    for (const u of urls) {
        window.open(u.url, '_blank');
    }
    toast(`Opened ${urls.length} ${siteKey} search tabs`, 'info');
};

async function startBrowserScrape(siteKey) {
    const config = window._browserSearchConfig;
    if (!config) return;

    const btn = document.getElementById(`scrape-btn-${siteKey}`);
    const status = document.getElementById(`scrape-status-${siteKey}`);
    const resultsDiv = document.getElementById('browser-scrape-results');
    const logDiv = document.getElementById('browser-scrape-log');

    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Scraping...';
    }
    if (status) {
        status.style.display = 'block';
        status.textContent = 'Extracting jobs from open tabs...';
    }

    const siteConfigs = config.configs.filter(c => c.site === siteKey);
    const extractorScript = siteConfigs[0]?.extractor;

    if (!extractorScript) {
        toast('No extractor available for ' + siteKey, 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'Scrape Results'; }
        return;
    }

    // Try to extract from the current page via a hidden approach:
    // We'll use a fetch-based approach to get page content from open tabs
    // For now, provide a manual paste option + direct URL fetch approach
    showActivity('Scraping ' + siteKey + '...');

    let allJobs = [];

    // Approach: Open each URL in a hidden iframe and extract
    // Note: This may be blocked by X-Frame-Options on some sites
    // Fallback: Use the extractor script that users can paste into console

    for (const sc of siteConfigs) {
        try {
            // Try fetching the page HTML directly (with credentials)
            const resp = await fetch(sc.url, { credentials: 'include', mode: 'no-cors' }).catch(() => null);

            // Since no-cors won't give us the body, we'll use a proxy approach
            // For now, show the extractor script for manual use
            if (status) {
                status.innerHTML = `
                    <div style="margin-bottom:6px">Auto-extraction limited by browser security. Use one of these methods:</div>
                    <div style="display:flex;gap:6px;flex-wrap:wrap">
                        <button class="btn btn-sm btn-primary" onclick="pasteJobsFromClipboard('${siteKey}')">
                            📋 Paste from Console
                        </button>
                        <button class="btn btn-sm btn-secondary" onclick="showExtractorScript('${siteKey}')">
                            📝 Show Script
                        </button>
                        <button class="btn btn-sm btn-secondary" onclick="manualJobEntry()">
                            ✏️ Manual Entry
                        </button>
                    </div>
                `;
            }
        } catch (e) {
            console.warn('Fetch failed for', sc.url, e);
        }
    }

    hideActivity();
    if (btn) { btn.disabled = false; btn.textContent = 'Scrape Results'; }
}

window.showExtractorScript = function(siteKey) {
    const config = window._browserSearchConfig;
    if (!config) return;

    const siteConfigs = config.configs.filter(c => c.site === siteKey);
    const script = siteConfigs[0]?.extractor || '';

    // Show a copyable script
    const modal = document.createElement('div');
    modal.className = 'modal-overlay';
    modal.id = 'extractor-script-modal';
    modal.innerHTML = `
        <div class="modal" style="max-width:600px;max-height:80vh">
            <div class="modal-header">
                <h3 style="margin:0">Extract Jobs from ${siteKey}</h3>
                <button class="btn-close" onclick="document.getElementById('extractor-script-modal')?.remove()">×</button>
            </div>
            <div style="padding:16px">
                <p style="color:var(--text-dim);font-size:13px;margin:0 0 12px">
                    1. Go to your ${siteKey} search results tab<br>
                    2. Open DevTools (F12) → Console<br>
                    3. Paste this script and press Enter<br>
                    4. Copy the JSON output<br>
                    5. Come back here and click "Paste from Console"
                </p>
                <textarea id="extractor-script-text" readonly style="width:100%;height:200px;font-family:var(--mono);
                    font-size:11px;background:var(--bg);color:var(--text);border:1px solid var(--border);
                    border-radius:var(--radius-sm);padding:8px;resize:vertical">${esc(script)}</textarea>
                <button class="btn btn-primary btn-sm" style="margin-top:8px" onclick="navigator.clipboard.writeText(document.getElementById('extractor-script-text').value);toast('Copied to clipboard','success')">
                    Copy Script
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
        if (e.target === modal) modal.remove();
    });
};

window.pasteJobsFromClipboard = async function(siteKey) {
    try {
        const text = await navigator.clipboard.readText();
        let jobs;
        try {
            jobs = JSON.parse(text);
        } catch (e) {
            toast('Clipboard does not contain valid JSON. Run the extractor script first.', 'error');
            return;
        }

        if (!Array.isArray(jobs) || jobs.length === 0) {
            toast('No jobs found in clipboard data', 'error');
            return;
        }

        // Ensure source is set
        jobs = jobs.map(j => ({ ...j, source: j.source || siteKey || 'browser' }));

        await importBrowserJobs(jobs);
    } catch (e) {
        // Clipboard API might be blocked - offer a textarea paste instead
        showPasteArea(siteKey);
    }
};

function showPasteArea(siteKey) {
    const status = document.getElementById(`scrape-status-${siteKey}`);
    if (!status) return;

    status.innerHTML = `
        <textarea id="paste-jobs-area" placeholder="Paste the JSON output from the console here..."
            style="width:100%;height:120px;font-family:var(--mono);font-size:11px;
            background:var(--bg);color:var(--text);border:1px solid var(--border);
            border-radius:var(--radius-sm);padding:8px;resize:vertical;margin-bottom:8px"></textarea>
        <button class="btn btn-primary btn-sm" onclick="importPastedJobs('${siteKey}')">
            Import Jobs
        </button>
    `;
}

window.importPastedJobs = async function(siteKey) {
    const textarea = document.getElementById('paste-jobs-area');
    if (!textarea) return;

    let jobs;
    try {
        jobs = JSON.parse(textarea.value);
    } catch (e) {
        toast('Invalid JSON. Make sure you copied the full output.', 'error');
        return;
    }

    if (!Array.isArray(jobs) || jobs.length === 0) {
        toast('No jobs found in pasted data', 'error');
        return;
    }

    jobs = jobs.map(j => ({ ...j, source: j.source || siteKey || 'browser' }));
    await importBrowserJobs(jobs);
};

window.manualJobEntry = function() {
    const resultsDiv = document.getElementById('browser-scrape-results');
    if (!resultsDiv) return;

    resultsDiv.style.display = 'block';
    const logDiv = document.getElementById('browser-scrape-log');
    logDiv.innerHTML = `
        <div style="margin-bottom:8px">Add a job manually:</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
            <input id="manual-job-title" placeholder="Job Title" class="input" style="font-size:12px">
            <input id="manual-job-company" placeholder="Company" class="input" style="font-size:12px">
            <input id="manual-job-location" placeholder="Location" class="input" style="font-size:12px">
            <input id="manual-job-url" placeholder="URL (optional)" class="input" style="font-size:12px">
        </div>
        <button class="btn btn-primary btn-sm" onclick="submitManualJob()">Add Job</button>
    `;
};

window.submitManualJob = async function() {
    const title = document.getElementById('manual-job-title')?.value?.trim();
    const company = document.getElementById('manual-job-company')?.value?.trim();
    const location = document.getElementById('manual-job-location')?.value?.trim();
    const url = document.getElementById('manual-job-url')?.value?.trim();

    if (!title || !company) {
        toast('Title and company are required', 'error');
        return;
    }

    await importBrowserJobs([{
        title, company, location: location || '', url: url || '',
        source: 'manual', description: '',
    }]);
};

async function importBrowserJobs(jobs) {
    if (!state.profileId) return;

    showActivity('Importing ' + jobs.length + ' jobs...');
    const loadId = toast(`Importing ${jobs.length} jobs...`, 'loading');

    try {
        const result = await api(`/profiles/${state.profileId}/import-browser-jobs`, {
            method: 'POST',
            body: jobs,
        });

        dismissToast(loadId);
        toast(`Imported ${result.new_jobs} new jobs (${result.duplicates_skipped} duplicates)`, 'success');

        // Run AI dedup reconciliation after browser import
        if (result.new_jobs > 0) {
            try {
                showActivity('AI dedup reconciliation...');
                const dedupResult = await api(`/profiles/${state.profileId}/reconcile-duplicates`, { method: 'POST' });
                if (dedupResult.merged > 0) {
                    toast(`AI merged ${dedupResult.merged} duplicate listings`, 'info');
                }
            } catch (e) {
                console.warn('AI dedup failed (non-fatal):', e);
            }
        }

        // Update results display
        const resultsDiv = document.getElementById('browser-scrape-results');
        const logDiv = document.getElementById('browser-scrape-log');
        if (resultsDiv && logDiv) {
            resultsDiv.style.display = 'block';
            logDiv.innerHTML += `<div style="color:var(--green)">✓ ${result.new_jobs} new jobs imported from ${result.source || 'browser'} (${result.duplicates_skipped} duplicates skipped)</div>`;
        }

        // Refresh job feed
        await loadSwipeStack();
        loadStats();
    } catch (e) {
        dismissToast(loadId);
        toast('Import failed: ' + e.message, 'error');
    } finally {
        hideActivity();
    }
}

// ── Activity Indicator (safety wrappers) ────────────────────────────────
if (typeof window.showActivity === 'undefined') {
    window.showActivity = function(text) {
        const el = document.getElementById('activity-indicator');
        const textEl = document.getElementById('activity-text');
        if (el) el.style.display = 'flex';
        if (textEl) textEl.textContent = text || 'Working...';
    };
}
if (typeof window.hideActivity === 'undefined') {
    window.hideActivity = function() {
        const el = document.getElementById('activity-indicator');
        if (el) el.style.display = 'none';
    };
}

// ── Profile / Settings Tab Switching ────────────────────────────────────

function switchProfileTab(tabName) {
    document.querySelectorAll('.profile-tab').forEach(t => t.classList.remove('active'));
    const activeTab = document.getElementById(`profile-tab-${tabName}`);
    if (activeTab) activeTab.classList.add('active');

    document.querySelectorAll('.profile-tab-content').forEach(c => c.classList.remove('active'));
    const activeContent = document.getElementById(`profile-tab-content-${tabName}`);
    if (activeContent) activeContent.classList.add('active');

    if (tabName === 'settings') {
        loadSettingsValues();
        loadSettings();
    }
}

// ── Settings Persistence (localStorage) ─────────────────────────────────

const SETTINGS_KEY = 'jobbunt_settings';

function getSettings() {
    try {
        const raw = localStorage.getItem(SETTINGS_KEY);
        return raw ? JSON.parse(raw) : {};
    } catch (e) {
        return {};
    }
}

function saveSettings() {
    const settings = {
        autoSearchFreq: document.getElementById('setting-auto-search-freq')?.value || 'manual',
        maxResults: parseInt(document.getElementById('setting-max-results')?.value || '25'),
        minScore: parseInt(document.getElementById('setting-min-score')?.value || '0'),
        includeExpired: document.getElementById('setting-include-expired')?.checked || false,
        emailDigest: document.getElementById('setting-email-digest')?.value || 'none',
        defaultView: document.getElementById('setting-default-view')?.value || 'list',
        cardsPerPage: parseInt(document.getElementById('setting-cards-per-page')?.value || '25'),
        darkTheme: document.getElementById('setting-dark-theme')?.checked ?? true,
    };

    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));

    if (settings.defaultView && state) state.browseMode = settings.defaultView;
    if (settings.cardsPerPage && state) state.jobsPerPage = settings.cardsPerPage;
    if (settings.minScore !== undefined && state?.jobFilters) state.jobFilters.minScore = settings.minScore;

    toast('Settings saved', 'success');
}

function loadSettingsValues() {
    const s = getSettings();
    const setVal = (id, val) => { const el = document.getElementById(id); if (el && val !== undefined) el.value = val; };
    const setChecked = (id, val) => { const el = document.getElementById(id); if (el && val !== undefined) el.checked = val; };

    setVal('setting-auto-search-freq', s.autoSearchFreq || 'manual');
    setVal('setting-max-results', String(s.maxResults || 25));
    setVal('setting-min-score', String(s.minScore || 0));
    setChecked('setting-include-expired', s.includeExpired || false);
    setVal('setting-email-digest', s.emailDigest || 'none');
    setVal('setting-default-view', s.defaultView || 'list');
    setVal('setting-cards-per-page', String(s.cardsPerPage || 25));
    setChecked('setting-dark-theme', s.darkTheme ?? true);

    const expiredLabel = document.getElementById('setting-include-expired-label');
    if (expiredLabel) expiredLabel.textContent = s.includeExpired ? 'On' : 'Off';

    const keys = getApiKeys();
    if (document.getElementById('setting-serpapi-key') && keys.serpapi) document.getElementById('setting-serpapi-key').value = keys.serpapi;
    if (document.getElementById('setting-adzuna-app-id') && keys.adzunaAppId) document.getElementById('setting-adzuna-app-id').value = keys.adzunaAppId;
    if (document.getElementById('setting-adzuna-key') && keys.adzunaKey) document.getElementById('setting-adzuna-key').value = keys.adzunaKey;
}

function applyStoredSettings() {
    const s = getSettings();
    if (s.defaultView && state) state.browseMode = s.defaultView;
    if (s.cardsPerPage && state) state.jobsPerPage = s.cardsPerPage;
    if (s.minScore !== undefined && state?.jobFilters) state.jobFilters.minScore = s.minScore;
}

// ── API Keys ────────────────────────────────────────────────────────────

const API_KEYS_STORAGE = 'jobbunt_api_keys';

function getApiKeys() {
    try { return JSON.parse(localStorage.getItem(API_KEYS_STORAGE) || '{}'); }
    catch (e) { return {}; }
}

function saveApiKeys() {
    const keys = {
        serpapi: document.getElementById('setting-serpapi-key')?.value?.trim() || '',
        adzunaAppId: document.getElementById('setting-adzuna-app-id')?.value?.trim() || '',
        adzunaKey: document.getElementById('setting-adzuna-key')?.value?.trim() || '',
    };
    localStorage.setItem(API_KEYS_STORAGE, JSON.stringify(keys));
    toast('API keys saved securely in browser', 'success');
}

function clearApiKeys() {
    localStorage.removeItem(API_KEYS_STORAGE);
    ['setting-serpapi-key', 'setting-adzuna-app-id', 'setting-adzuna-key'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    toast('API keys cleared', 'info');
}

// ── Export Jobs to CSV ──────────────────────────────────────────────────

async function exportJobsCSV() {
    if (!state.profileId) { toast('No profile selected', 'error'); return; }
    toast('Exporting jobs...', 'info');
    try {
        const data = await api(`/profiles/${state.profileId}/jobs?limit=10000`);
        const jobs = data.jobs || data || [];
        if (!jobs.length) { toast('No jobs to export', 'error'); return; }

        const headers = ['Title','Company','Location','Score','Status','Source','URL','Salary Min','Salary Max','Remote','Date Seen'];
        const rows = jobs.map(j => [
            csvEscape(j.title || ''), csvEscape(j.company || ''), csvEscape(j.location || ''),
            j.match_score || j.score || '', j.status || 'pending', j.source || '',
            csvEscape(j.url || ''), j.salary_min || '', j.salary_max || '',
            j.remote_type || '', j.first_seen || j.created_at || ''
        ]);

        let csv = headers.join(',') + '\n' + rows.map(r => r.join(',')).join('\n');
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `jobbunt-jobs-${new Date().toISOString().slice(0,10)}.csv`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
        toast(`Exported ${jobs.length} jobs to CSV`, 'success');
    } catch (e) { toast('Export failed: ' + e.message, 'error'); }
}

function csvEscape(str) {
    if (!str) return '';
    str = String(str);
    return (str.includes(',') || str.includes('"') || str.includes('\n'))
        ? '"' + str.replace(/"/g, '""') + '"' : str;
}

// ── Reset Profile ───────────────────────────────────────────────────────

async function resetProfile() {
    if (!state.profileId) { toast('No profile to reset', 'error'); return; }
    if (!confirm('This will clear all profile fields.\nYour jobs and applications will be kept.\n\nAre you sure?')) return;
    try {
        await api(`/profiles/${state.profileId}`, {
            method: 'PUT',
            body: { name:'',email:'',phone:'',location:'',target_roles:[],target_locations:[],skills:[],
                    min_salary:null,experience_years:null,remote_preference:'any',cover_letter_style:'',tiers_down:0,tiers_up:0 }
        });
        state.profile = null;
        document.getElementById('profile-form')?.reset();
        if (state.tags) { state.tags.roles = []; state.tags.locations = []; state.tags.skills = []; }
        toast('Profile reset. Fill in your details again.', 'success');
    } catch (e) { toast('Reset failed: ' + e.message, 'error'); }
}

// ── Toggle listeners ────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function() {
    applyStoredSettings();
    const expiredToggle = document.getElementById('setting-include-expired');
    if (expiredToggle) {
        expiredToggle.addEventListener('change', function() {
            const label = document.getElementById('setting-include-expired-label');
            if (label) label.textContent = this.checked ? 'On' : 'Off';
        });
    }
    const dryRunToggle = document.getElementById('dry-run-toggle');
    if (dryRunToggle) {
        dryRunToggle.addEventListener('change', function() {
            const label = document.getElementById('dry-run-label');
            if (label) label.textContent = this.checked ? 'Dry Run Mode: ON (Safe)' : 'Dry Run Mode: OFF (Live)';
        });
    }
});

// ── Missing Handlers — Login / Profile Dropdown / Import ────────────────

function skipLogin() {
    const overlay = document.getElementById('login-overlay');
    if (overlay) overlay.style.display = 'none';
}

function logout() {
    localStorage.removeItem('jb_profile_id');
    localStorage.removeItem('jb_token');
    state.profileId = null;
    state.profile = null;
    location.reload();
}

function toggleProfileDropdown() {
    const dd = document.getElementById('profile-dropdown');
    if (dd) dd.classList.toggle('open');
}

function closeProfileDropdown() {
    const dd = document.getElementById('profile-dropdown');
    if (dd) dd.classList.remove('open');
}

// Close dropdown on outside click
document.addEventListener('click', (e) => {
    const menu = document.getElementById('profile-menu');
    if (menu && !menu.contains(e.target)) closeProfileDropdown();
});

// ── Profile Switcher ────────────────────────────────────────────────────

function getProfileInitials(profile) {
    if (!profile?.name) return '?';
    const parts = profile.name.trim().split(/\s+/);
    if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    return parts[0][0]?.toUpperCase() || '?';
}

function updateNavAvatar() {
    const el = document.getElementById('avatar-initials');
    if (el) el.textContent = getProfileInitials(state.profile);
    const nameEl = document.getElementById('dropdown-profile-name');
    if (nameEl) nameEl.textContent = state.profile?.name || 'No Profile';
}

function populateProfileDropdown(profiles) {
    const container = document.getElementById('dropdown-profiles');
    if (!container) return;
    container.innerHTML = '';
    if (!profiles || profiles.length === 0) return;

    profiles.forEach(p => {
        const item = document.createElement('div');
        item.className = 'dropdown-profile-item' + (p.id === state.profileId ? ' active' : '');
        const initials = getProfileInitials(p);
        item.innerHTML = `<span class="profile-item-avatar">${initials}</span><span>${p.name || 'Unnamed'}</span>`;
        item.addEventListener('click', () => switchProfile(p.id));
        container.appendChild(item);
    });
}

async function switchProfile(profileId) {
    if (profileId === state.profileId) {
        closeProfileDropdown();
        return;
    }
    try {
        const result = await api('/profiles/select', { method: 'POST', body: { profile_id: profileId } });
        state.profile = result;
        state.profileId = result.id;
        state.tags.roles = result.target_roles || [];
        state.tags.locations = result.target_locations || [];
        state.tags.skills = result.skills || [];
        updateNavAvatar();
        // Refresh dropdown to mark the new active profile
        const profiles = await api('/profiles');
        populateProfileDropdown(profiles);
        closeProfileDropdown();
        toast(`Switched to ${result.name || 'profile'}`, 'success');
        // Reload current view
        const activeView = document.querySelector('.view.active');
        if (activeView) {
            const viewName = activeView.id.replace('view-', '');
            showView(viewName);
        }
    } catch (e) {
        toast('Failed to switch profile: ' + e.message, 'error');
    }
}

function createNewProfile() {
    closeProfileDropdown();
    // Clear current profile state so saveProfile creates a new one
    state.profileId = null;
    state.profile = null;
    state.tags.roles = [];
    state.tags.locations = [];
    state.tags.skills = [];
    // Navigate to profile view with empty form
    showView('profile');
    switchProfileTab('profile');
    // Clear the form fields
    const fields = ['f-name', 'f-email', 'f-phone', 'f-location', 'f-min-salary', 'f-experience', 'f-cover-template'];
    fields.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    const remoteEl = document.getElementById('f-remote');
    if (remoteEl) remoteEl.value = 'any';
    renderTags('roles');
    renderTags('locations');
    renderTags('skills');
    updateNavAvatar();
    toast('Fill in your new profile details', 'info');
}

function toggleImportSection() {
    const el = document.getElementById('profile-paste-mode');
    if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}

// ── Pipeline Tab Switching ──────────────────────────────────────────────

function switchPipelineTab(tab) {
    document.querySelectorAll('.pipeline-tab').forEach(t => {
        t.classList.toggle('active', t.textContent.toLowerCase().includes(tab));
    });
    const shortlistPanel = document.getElementById('pipeline-shortlist');
    const appsPanel = document.getElementById('pipeline-applications');
    if (shortlistPanel) shortlistPanel.classList.toggle('active', tab === 'shortlist');
    if (appsPanel) appsPanel.classList.toggle('active', tab === 'applications');

    if (tab === 'shortlist') loadShortlist();
    else if (tab === 'applications') loadApplications();
}

// ── Intel / Bullpen Tab Switching ────────────────────────────────────────

function switchIntelTab(tab, autoRun) {
    // Use the existing switchSummaryTab for the visual switching
    document.querySelectorAll('.summary-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.summary-panel').forEach(p => {
        p.classList.remove('active');
        p.style.display = 'none';
    });
    const tabBtn = document.querySelector(`.summary-tab[data-subtab="${tab}"]`);
    if (tabBtn) tabBtn.classList.add('active');
    const panel = document.getElementById(`subtab-${tab}`);
    if (panel) {
        panel.classList.add('active');
        panel.style.display = '';
    }

    if (autoRun) {
        switch (tab) {
            case 'pregame': loadPregameReport(); break;
            case 'overview': loadInsights(); break;
            case 'advisor': loadSearchAdvisor(); break;
            case 'skills-audit': runSkillsAuditIntel(); break;
            case 'resume': improveResumeIntel(); break;
        }
    }
}

function runFromHub(btn, tabId) {
    switchIntelTab(tabId, true);
}

// ── Pregame Report ──────────────────────────────────────────────────────

async function loadPregameReport() {
    if (!state.profileId) return;
    const area = document.getElementById('pregame-summary-area');
    if (!area) return;
    area.innerHTML = '<div class="loading-shimmer" style="height:200px;border-radius:8px"></div>';

    try {
        // Fetch insights, advisor, and skills-audit in parallel
        const [insightsRes, advisorRes, auditRes] = await Promise.allSettled([
            api(`/profiles/${state.profileId}/insights`),
            api(`/profiles/${state.profileId}/search-advisor`),
            api(`/profiles/${state.profileId}/skills-audit`),
        ]);

        let html = '<div class="pregame-summary" style="display:grid;gap:16px">';

        // Insights card
        if (insightsRes.status === 'fulfilled') {
            const d = insightsRes.value;
            let aiInsights = d.ai_insights;
            if (!aiInsights && d.ai_insights_task_id) {
                try { aiInsights = (await _pollTask(d.ai_insights_task_id)).ai_insights; } catch {}
            }
            html += `<div class="pregame-card insights-card" style="background:var(--jb-bg-secondary);border:1px solid var(--jb-border);border-radius:8px;padding:16px">
                <h4 style="margin:0 0 8px">Scoreboard Summary</h4>
                <p style="margin:0;color:var(--jb-text-2)">${d.total_jobs || 0} jobs found &middot; ${d.liked || 0} liked &middot; ${d.shortlisted || 0} shortlisted &middot; ${d.passed || 0} passed</p>
                ${d.score_stats && d.score_stats.avg ? `<p style="margin:4px 0 0;color:var(--jb-text-2)">Avg match score: <strong>${d.score_stats.avg}</strong></p>` : ''}
                ${aiInsights && aiInsights.market_summary ? `<p style="margin:8px 0 0;color:var(--jb-text-1)">${esc(aiInsights.market_summary)}</p>` : ''}
            </div>`;
        }

        // Advisor card
        if (advisorRes.status === 'fulfilled') {
            let adv = advisorRes.value;
            if (adv.task_id) { try { adv = await _pollTask(adv.task_id); } catch {} }
            if (adv.advisor) {
                html += `<div class="pregame-card" style="background:var(--jb-bg-secondary);border:1px solid var(--jb-border);border-radius:8px;padding:16px">
                    <h4 style="margin:0 0 8px">Coaching Staff Summary</h4>
                    <p style="margin:0;color:var(--jb-text-1)">${esc(adv.advisor.overall_assessment || '')}</p>
                    ${adv.advisor.market_fit_score != null ? `<p style="margin:8px 0 0">Market Fit: <strong>${adv.advisor.market_fit_score}/100</strong></p>` : ''}
                </div>`;
            }
        }

        // Skills audit card
        if (auditRes.status === 'fulfilled') {
            const a = auditRes.value;
            if (a.ai_audit) {
                const recs = a.ai_audit.recommended_additions || [];
                html += `<div class="pregame-card" style="background:var(--jb-bg-secondary);border:1px solid var(--jb-border);border-radius:8px;padding:16px">
                    <h4 style="margin:0 0 8px">Batting Practice Summary</h4>
                    <p style="margin:0;color:var(--jb-text-2)">${a.total_jobs || 0} jobs analyzed &middot; ${(a.profile_skills || []).length} skills on profile</p>
                    ${recs.length ? `<p style="margin:8px 0 0;color:var(--jb-text-1)">Top skills to add: ${recs.slice(0, 5).map(esc).join(', ')}</p>` : ''}
                </div>`;
            }
        }

        html += '</div>';
        area.innerHTML = html;
    } catch (e) {
        area.innerHTML = `<div class="empty-state"><h2>Report generation failed</h2><p>${esc(e.message)}</p></div>`;
    }
}

// Simple task poller for pregame report
async function _pollTask(taskId) {
    for (let i = 0; i < 60; i++) {
        await new Promise(r => setTimeout(r, 2000));
        try {
            const task = await api(`/tasks/${taskId}`);
            if (task.status === 'done') return task.result || task;
            if (task.status === 'error') throw new Error(task.error || 'Task failed');
        } catch (e) {
            if (e.message.includes('404')) throw e;
            throw e;
        }
    }
    throw new Error('Task timed out');
}

// ── Skills Audit (Intel tab) ────────────────────────────────────────────

async function runSkillsAuditIntel() {
    if (!state.profileId) return;
    const el = document.getElementById('intel-skills-audit');
    if (!el) return;
    el.innerHTML = '<div class="loading-shimmer" style="height:300px;border-radius:8px"></div>';

    try {
        const data = await api(`/profiles/${state.profileId}/skills-audit`);
        if (!data.skill_hits || !data.total_jobs) {
            el.innerHTML = `<div class="empty-state"><h2>Not enough data</h2><p>${esc(data.reason || 'Search for jobs first')}</p></div>`;
            return;
        }
        let html = `<p style="color:var(--jb-text-2)">${data.total_jobs} jobs analyzed &middot; ${(data.profile_skills || []).length} skills on profile</p>`;
        html += '<h4 style="margin:16px 0 8px">Your Skills &mdash; Market Demand</h4>';
        const sorted = Object.entries(data.skill_hits).sort((a, b) => b[1].pct - a[1].pct);
        html += '<div style="display:grid;gap:6px">';
        for (const [skill, info] of sorted) {
            html += `<div style="display:grid;grid-template-columns:140px 1fr 50px;align-items:center;gap:8px">
                <span style="font-size:13px">${esc(skill)}</span>
                <div style="background:var(--jb-bg-tertiary);border-radius:4px;height:12px;overflow:hidden"><div style="width:${Math.max(info.pct, 3)}%;height:100%;background:${info.pct >= 70 ? 'var(--jb-green,#4caf50)' : info.pct >= 40 ? 'var(--jb-bright)' : info.pct >= 15 ? 'var(--jb-orange,#f5a623)' : 'var(--jb-text-dim)'};border-radius:4px"></div></div>
                <span style="font-size:12px;color:var(--jb-text-2)">${info.pct}%</span>
            </div>`;
        }
        html += '</div>';

        if (data.ai_audit) {
            const a = data.ai_audit;
            if (a.recommended_additions && a.recommended_additions.length) {
                html += `<h4 style="margin:16px 0 8px">Recommended Skills to Add</h4><div style="display:flex;flex-wrap:wrap;gap:6px">${a.recommended_additions.map(s => `<span class="tag-chip" style="cursor:pointer" onclick="addSkillFromAudit('${esc(s)}',this)">${esc(s)} +</span>`).join('')}</div>`;
            }
            if (a.recommended_removals && a.recommended_removals.length) {
                html += `<h4 style="margin:16px 0 8px">Consider Removing</h4><div style="display:flex;flex-wrap:wrap;gap:6px">${a.recommended_removals.map(s => `<span class="tag-chip">${esc(s)}</span>`).join('')}</div>`;
            }
            if (a.missing_high_demand && a.missing_high_demand.length) {
                html += `<h4 style="margin:16px 0 8px">High-Demand Missing Skills</h4><ul>${a.missing_high_demand.map(s => `<li>${esc(s)}</li>`).join('')}</ul>`;
            }
        }

        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = `<div class="empty-state"><h2>Audit failed</h2><p>${esc(e.message)}</p></div>`;
    }
}

// ── Resume Improver ─────────────────────────────────────────────────────

async function improveResume() {
    if (!state.profileId) return;
    const el = document.getElementById('resume-improver-container');
    if (el) {
        el.innerHTML = '<div class="loading-shimmer" style="height:200px;border-radius:8px"></div>';
    }
    await _doImproveResume(el);
}

async function improveResumeIntel() {
    if (!state.profileId) return;
    const el = document.getElementById('intel-resume-container');
    if (el) {
        el.innerHTML = '<div class="loading-shimmer" style="height:300px;border-radius:8px"></div>';
    }
    await _doImproveResume(el);
}

async function _doImproveResume(el) {
    if (!el) return;
    try {
        let data = await api(`/profiles/${state.profileId}/improve-resume`, { method: 'POST' });

        // Poll if background task
        if (data.task_id) {
            data = await _pollTask(data.task_id);
        }

        if (data.error) {
            el.innerHTML = `<div class="empty-state"><h2>Analysis failed</h2><p>${esc(data.error)}</p></div>`;
            return;
        }

        let html = '<div class="resume-improvement">';
        if (data.overall_score != null) {
            html += `<div style="margin-bottom:16px"><h4>Resume Score</h4><div style="font-size:32px;font-weight:600;color:var(--jb-bright)">${data.overall_score}/100</div></div>`;
        }

        if (data.suggestions && data.suggestions.length) {
            html += '<h4>Suggestions</h4>';
            for (const s of data.suggestions) {
                html += `<div style="background:var(--jb-bg-secondary);border:1px solid var(--jb-border);border-radius:8px;padding:12px;margin-bottom:8px">
                    <div style="display:flex;gap:8px;margin-bottom:6px">
                        <span style="font-size:11px;text-transform:uppercase;padding:2px 6px;border-radius:4px;background:var(--jb-bg-tertiary)">${esc(s.type)}</span>
                        <span style="font-size:12px;color:var(--jb-text-2)">${esc(s.section)}</span>
                    </div>
                    ${s.current && s.current !== 'N/A' ? `<div style="font-size:13px;color:var(--jb-text-dim);margin-bottom:4px"><strong>Current:</strong> ${esc(s.current)}</div>` : ''}
                    <div style="font-size:13px;color:var(--jb-text-1);margin-bottom:4px"><strong>Suggested:</strong> ${esc(s.suggested)}</div>
                    <div style="font-size:12px;color:var(--jb-text-2)">${esc(s.reason)}</div>
                </div>`;
            }
        }

        if (data.missing_keywords && data.missing_keywords.length) {
            html += `<h4>Missing Keywords</h4><div style="display:flex;flex-wrap:wrap;gap:6px">${data.missing_keywords.map(k => `<span class="tag-chip">${esc(k)}</span>`).join('')}</div>`;
        }

        if (data.ats_tips && data.ats_tips.length) {
            html += `<h4>ATS Tips</h4><ul>${data.ats_tips.map(t => `<li>${esc(t)}</li>`).join('')}</ul>`;
        }

        html += '</div>';
        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = `<div class="empty-state"><h2>Analysis failed</h2><p>${esc(e.message)}</p></div>`;
    }
}

// ── Reporter Corner ─────────────────────────────────────────────────────

const REPORTER_QUESTIONS = [
    { q: "What's your ideal work environment?", choices: ["Remote-first startup", "Hybrid corporate", "Small team, big impact", "Enterprise with clear structure"], profileField: "ideal_culture" },
    { q: "What motivates you most?", choices: ["Solving hard problems", "Building teams", "Making an impact", "Learning new tech"], profileField: "values" },
    { q: "What's your biggest strength?", choices: ["Technical depth", "Strategic thinking", "People leadership", "Cross-functional communication"], profileField: "strengths" },
    { q: "How do you prefer to grow?", choices: ["Hands-on projects", "Mentorship", "Formal training", "Stretch assignments"], profileField: "growth_areas" },
    { q: "What's a deal-breaker for you?", choices: ["Micromanagement", "No remote option", "Below-market pay", "Toxic culture"], profileField: "deal_breakers" },
];

let _reporterQuestionIndex = 0;

function loadReporterCorner() {
    const q = REPORTER_QUESTIONS[_reporterQuestionIndex % REPORTER_QUESTIONS.length];
    const qEl = document.getElementById('reporter-question');
    const taEl = document.getElementById('reporter-textarea');
    if (qEl) {
        qEl.innerHTML = `
            <div class="reporter-q-text" style="font-size:14px;font-weight:500;margin-bottom:8px">${esc(q.q)}</div>
            <div class="reporter-choices" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">
                ${q.choices.map(c => `<button class="btn btn-sm btn-secondary reporter-choice" onclick="document.getElementById('reporter-textarea').value='${esc(c)}'">${esc(c)}</button>`).join('')}
            </div>
        `;
    }
    if (taEl) taEl.value = '';
}

async function saveReporterAnswer() {
    const q = REPORTER_QUESTIONS[_reporterQuestionIndex % REPORTER_QUESTIONS.length];
    const taEl = document.getElementById('reporter-textarea');
    const answer = taEl ? taEl.value.trim() : '';
    if (!answer) { toast('Write an answer first', 'warning'); return; }

    try {
        await api(`/profiles/${state.profileId}/apply-advisor-suggestion`, {
            method: 'POST',
            body: { field: q.profileField, value: answer }
        });
        const msgEl = document.getElementById('reporter-saved-msg');
        if (msgEl) { msgEl.textContent = 'Saved!'; setTimeout(() => msgEl.textContent = '', 2000); }
        _reporterQuestionIndex++;
        loadReporterCorner();
    } catch (e) {
        toast('Failed to save answer', 'error');
    }
}

// ── Dugout Helpers (called from showView) ───────────────────────────────

async function loadDugoutReadiness() {
    if (!state.profileId) return;
    try {
        const r = await api(`/profiles/${state.profileId}/apply-readiness`);
        const el = document.getElementById('dugout-readiness');
        if (!el) return;
        const emoji = r.score >= 70 ? '🟢' : r.score >= 40 ? '🟡' : '🔴';
        el.innerHTML = `<div style="display:flex;align-items:center;gap:12px;padding:12px;background:var(--jb-bg-secondary);border-radius:8px;border:1px solid var(--jb-border)">
            <span style="font-size:24px">${emoji}</span>
            <div><div style="font-size:16px;font-weight:600">${r.score}% Ready</div><div style="font-size:12px;color:var(--jb-text-2)">${r.passed}/${r.total} checks passed</div></div>
        </div>`;
    } catch {}
}

async function loadDugoutSeasonStats() {
    if (!state.profileId) return;
    try {
        const s = await api(`/profiles/${state.profileId}/stats`);
        const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        set('stat-total-apps', s.at_bats || 0);
        set('stat-callbacks', s.hits || 0);
        const avgStr = s.avg ? ('.' + String(s.avg).replace('0.', '').padEnd(3, '0')) : '.000';
        set('stat-avg', avgStr);
        set('stat-shortlisted', s.shortlisted || 0);
        set('stat-interviews', s.applications || 0);
    } catch {}
}

async function loadScoutingReport() {
    if (!state.profileId || !state.profile) return;
    const p = state.profile;
    const checks = [
        { label: 'Profile Created', done: true },
        { label: 'Resume Uploaded', done: p.resume_uploaded || p.has_resume_text },
        { label: 'Target Roles Set', done: (p.target_roles || []).length > 0 },
        { label: 'Location Set', done: !!p.location },
        { label: 'First Search Done', done: false },
        { label: 'First Application', done: false },
    ];
    try {
        const s = await api(`/profiles/${state.profileId}/stats`);
        checks[4].done = s.total_jobs > 0;
        checks[5].done = s.applications > 0;
    } catch {}

    const el = document.getElementById('scouting-checklist');
    if (!el) return;
    const done = checks.filter(c => c.done).length;
    const pct = Math.round((done / checks.length) * 100);
    el.innerHTML = checks.map(c => `
        <div class="scouting-check ${c.done ? 'scouting-check-done' : ''}" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px">
            <span style="color:${c.done ? 'var(--jb-green,#4caf50)' : 'var(--jb-text-dim)'}">${c.done ? '✓' : '○'}</span>
            <span style="color:${c.done ? 'var(--jb-text-1)' : 'var(--jb-text-dim)'}">${esc(c.label)}</span>
        </div>
    `).join('');
    const bar = document.getElementById('scouting-progress-bar');
    if (bar) bar.style.width = pct + '%';
    const pctEl = document.getElementById('scouting-progress-pct');
    if (pctEl) pctEl.textContent = pct + '% complete';
}

async function loadDugoutCharts() {
    if (!state.profileId) return;
    try {
        const stats = await api(`/profiles/${state.profileId}/stats`);
        const section = document.getElementById('dugout-chart-section');
        const sourceCounts = stats.source_counts || {};
        const hasData = Object.keys(sourceCounts).length > 0;
        if (section) section.style.display = hasData ? '' : 'none';
        if (!hasData) return;

        // Source chart
        const sourceEl = document.getElementById('source-chart-bars');
        if (sourceEl) {
            const max = Math.max(...Object.values(sourceCounts), 1);
            sourceEl.innerHTML = Object.entries(sourceCounts).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([k, v]) => `
                <div style="display:grid;grid-template-columns:80px 1fr 30px;align-items:center;gap:8px;padding:3px 0">
                    <span style="font-size:12px;overflow:hidden;text-overflow:ellipsis">${esc(k)}</span>
                    <div style="background:var(--jb-bg-tertiary);border-radius:4px;height:10px;overflow:hidden"><div style="width:${(v / max) * 100}%;height:100%;background:var(--jb-bright);border-radius:4px"></div></div>
                    <span style="font-size:12px;color:var(--jb-text-2);text-align:right">${v}</span>
                </div>
            `).join('');
        }

        // Status chart
        const statusEl = document.getElementById('status-chart-bars');
        if (statusEl) {
            const statuses = {
                pending: stats.pending_swipe || 0,
                liked: stats.liked || 0,
                shortlisted: stats.shortlisted || 0,
                passed: stats.passed || 0,
            };
            const max = Math.max(...Object.values(statuses), 1);
            statusEl.innerHTML = Object.entries(statuses).map(([k, v]) => `
                <div style="display:grid;grid-template-columns:80px 1fr 30px;align-items:center;gap:8px;padding:3px 0">
                    <span style="font-size:12px">${esc(k)}</span>
                    <div style="background:var(--jb-bg-tertiary);border-radius:4px;height:10px;overflow:hidden"><div style="width:${(v / max) * 100}%;height:100%;background:var(--jb-bright);border-radius:4px"></div></div>
                    <span style="font-size:12px;color:var(--jb-text-2);text-align:right">${v}</span>
                </div>
            `).join('');
        }
    } catch {}
}

// ── Pipeline Data Loader ────────────────────────────────────────────────

async function loadPipelineData() {
    if (!state.profileId) return;
    // Load funnel
    try {
        const stats = await api(`/profiles/${state.profileId}/stats`);
        const el = document.getElementById('pipeline-funnel');
        if (el) {
            const stages = [
                { label: 'Scouted', value: stats.total_jobs || 0 },
                { label: 'Shortlisted', value: stats.shortlisted || 0 },
                { label: 'Applied', value: (stats.liked || 0) + (stats.applied || 0) },
                { label: 'Interviews', value: stats.applications || 0 },
            ];
            const max = Math.max(...stages.map(s => s.value), 1);
            el.innerHTML = stages.map(s => `
                <div style="display:flex;align-items:center;gap:12px;padding:6px 0">
                    <div style="width:${Math.max((s.value / max) * 100, 5)}%;height:24px;background:var(--jb-bright);border-radius:4px;min-width:30px;display:flex;align-items:center;justify-content:flex-end;padding:0 8px;font-size:12px;font-weight:600;color:#fff">${s.value}</div>
                    <span style="font-size:13px;color:var(--jb-text-2)">${s.label}</span>
                </div>
            `).join('');
        }
    } catch {}

    // Load default tab
    loadShortlist();
}

// ── Intel Data Loader ───────────────────────────────────────────────────

function loadIntelData() {
    // Default to pregame tab visible, don't auto-run
}

// ── Window exports ──────────────────────────────────────────────────────
window.showView = showView;
window.switchProfileTab = switchProfileTab;
window.saveSettings = saveSettings;
window.saveApiKeys = saveApiKeys;
window.clearApiKeys = clearApiKeys;
window.exportJobsCSV = exportJobsCSV;
window.resetProfile = resetProfile;
window.skipLogin = skipLogin;
window.logout = logout;
window.toggleProfileDropdown = toggleProfileDropdown;
window.closeProfileDropdown = closeProfileDropdown;
window.switchProfile = switchProfile;
window.createNewProfile = createNewProfile;
window.toggleImportSection = toggleImportSection;
window.switchPipelineTab = switchPipelineTab;
window.switchIntelTab = switchIntelTab;
window.runFromHub = runFromHub;
window.loadPregameReport = loadPregameReport;
window.loadInsights = loadInsights;
window.loadSearchAdvisor = loadSearchAdvisor;
window.runSkillsAudit = runSkillsAudit;
window.runSkillsAuditIntel = runSkillsAuditIntel;
window.improveResume = improveResume;
window.improveResumeIntel = improveResumeIntel;
window.saveReporterAnswer = saveReporterAnswer;
window.deepResearchShortlist = deepResearchShortlist;
window.searchJobs = searchJobs;
window.rescoreJobs = rescoreJobs;
window.switchBrowseMode = switchBrowseMode;
window.verifyAllJobs = verifyAllJobs;
window.reenrichCompanies = reenrichCompanies;
window.dedupJobs = dedupJobs;
window.reanalyzeProfile = reanalyzeProfile;
window.resetDatabase = resetDatabase;
window.parseAndSaveProfile = parseAndSaveProfile;
window.confirmParsedProfile = confirmParsedProfile;
window.loadReporterCorner = loadReporterCorner;
