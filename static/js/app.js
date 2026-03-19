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
    authUser: null,
    filters: {
        scoreMin: 0,
        remoteType: 'all',
        salaryMin: '',
        keyword: '',
        datePosted: 'all',
        sortBy: 'score',
        sortDir: 'desc',
        filtersOpen: false,
    },
};

// ── Init ──────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
    setupNavigation();
    setupTagInputs();
    setupKeyboard();
    setupFormHandlers();
    setupActionButtons();
    // Fetch auth user (Google profile picture, name)
    let authenticated = false;
    try {
        const authResp = await fetch('/auth/me').catch(() => null);
        if (authResp?.ok) {
            const authUser = await authResp.json();
            if (authUser && authUser.id) {
                state.authUser = authUser;
                authenticated = true;
                // Auto-claim unclaimed profiles on first login
                try { await fetch('/auth/claim-profiles', { method: 'POST' }); } catch(e) { /* ok */ }
            }
        }
    } catch(e) { /* auth not enabled or not logged in */ }

    // Check if auth is required and user isn't logged in
    if (!authenticated) {
        try {
            const configResp = await fetch('/auth/config');
            const config = await configResp.json();
            if (config.auth_enabled || config.local_auth_enabled) {
                const overlay = document.getElementById('login-overlay');
                if (overlay) overlay.style.display = 'flex';
                // Dev user skip link removed for security
                return; // Don't load profile until logged in
            }
        } catch(e) {}
    }

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
    document.querySelectorAll('.nav-btn, .nav-link, .bottom-tab').forEach(b => b.classList.remove('active'));

    const newView = document.getElementById(`view-${name}`);
    if (newView) {
        newView.classList.add('active', 'view-enter');
        setTimeout(() => newView.classList.remove('view-enter'), 200);
    }

    // Highlight bottom tab
    const bottomTab = document.querySelector(`.bottom-tab[data-view="${name}"]`);
    if (bottomTab) bottomTab.classList.add('active');

    const navEl = document.querySelector(`.nav-link[data-view="${name}"]`) || document.querySelector(`.nav-btn[data-view="${name}"]`);
    if (navEl) navEl.classList.add('active');

    if (name === 'dugout') {
        if (typeof loadCoachNote === 'function') loadCoachNote();
        if (typeof loadStats === 'function') loadStats();
        if (typeof loadDugoutReadiness === 'function') loadDugoutReadiness();
        if (typeof loadDugoutSeasonStats === 'function') loadDugoutSeasonStats();
        if (typeof loadSpringTraining === 'function') loadSpringTraining();
        if (typeof loadReporterCorner === 'function') loadReporterCorner();
        if (typeof loadDugoutCharts === 'function') loadDugoutCharts();
    }
    if (name === 'hunt') {
        loadSwipeStack();
        // Re-apply Spring Training gating to search buttons
        if (typeof getSpringTrainingLevel === 'function') {
            const st = getSpringTrainingLevel();
            applyFeatureGating(st.level, st.index);
        }
    }
    if (name === 'pipeline') {
        if (typeof loadPipelineData === 'function') loadPipelineData();
    }
    if (name === 'intel') {
        if (typeof loadIntelData === 'function') loadIntelData();
        // Re-apply Spring Training gating to Bullpen AI buttons
        if (typeof getSpringTrainingLevel === 'function') {
            const st = getSpringTrainingLevel();
            applyFeatureGating(st.level, st.index);
        }
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
            // New user — auto-create a profile from auth data, then go to Profile
            try {
                const name = state.authUser?.name || 'New User';
                const email = state.authUser?.email || '';
                const newProfile = await api('/profiles', {
                    method: 'POST',
                    body: { name, email }
                });
                state.profile = newProfile;
                state.profileId = newProfile.id;
                state.tags.roles = newProfile.target_roles || [];
                state.tags.locations = newProfile.target_locations || [];
                state.tags.skills = newProfile.skills || [];
                updateNavAvatar();
                populateProfileDropdown([newProfile]);
                toast('Welcome! Let\u2019s build your profile.', 'info');
            } catch(e) {
                console.warn('Auto-create profile failed:', e);
            }
            showView('profile');
        }
    } catch (e) {
        console.error('Failed to load profile:', e);
    }
}

function setProfileMode(mode) {
    state.profileMode = mode;
    const manualMode = document.getElementById('profile-form');
    // Always show the profile form — both modes need it visible
    if (manualMode) manualMode.style.display = 'block';
    if (state.profile) populateProfileForm(state.profile);
}

function handleResumeSelected(input) {
    const zone = document.getElementById('resume-drop-zone');
    const label = document.getElementById('resume-drop-label');
    if (input.files && input.files[0]) {
        const file = input.files[0];
        if (zone) zone.classList.add('has-file');
        if (label) label.textContent = file.name;
    }
}

async function parseAndSaveProfile() {
    const fileInput = document.getElementById('f-resume-paste');
    const text = document.getElementById('f-paste-text').value.trim();
    const hasFile = fileInput && fileInput.files && fileInput.files[0];

    if (!text && !hasFile) {
        toast('Upload a resume or paste your resume text to get started', 'error');
        return;
    }

    const btn = document.getElementById('btn-parse-profile');
    setButtonLoading(btn, true);

    try {
        let parseText = text;

        // If file uploaded, upload it first then use resume text for parsing
        if (hasFile) {
            // Create a temporary profile if none exists
            if (!state.profileId) {
                const tempName = state.authUser?.name || 'New Player';
                const newProfile = await api('/profiles', {
                    method: 'POST',
                    body: { name: tempName }
                });
                state.profile = newProfile;
                state.profileId = newProfile.id;
            }
            // Upload the resume file
            const formData = new FormData();
            formData.append('file', fileInput.files[0]);
            const uploadResp = await api(`/profiles/${state.profileId}/resume`, {
                method: 'POST',
                body: formData
            });
            // Use the parsed resume text for profile extraction
            parseText = uploadResp.resume_text || text || '';
        }

        if (!parseText) {
            toast('Could not extract text from resume. Try pasting it instead.', 'error');
            return;
        }

        const parsed = await api('/profiles/parse', { method: 'POST', body: { text: parseText } });
        state.parsedProfile = parsed;

        const preview = document.getElementById('parsed-preview');
        const content = document.getElementById('parsed-preview-content');

        // Build card-based results dashboard
        function _isEmptyParsed(v) {
            if (v === null || v === undefined) return true;
            if (typeof v === 'string') { const l = v.trim().toLowerCase(); return !l || ['not found','unknown','n/a','none','null'].includes(l); }
            if (Array.isArray(v)) return v.length === 0;
            return false;
        }
        const inferredSet = new Set((parsed.inferred_fields || []).map(f => f.toLowerCase()));

        // --- Card 1: Contact Info ---
        const contactFields = [
            { key: 'name', label: 'Name', icon: '👤' },
            { key: 'email', label: 'Email', icon: '✉' },
            { key: 'phone', label: 'Phone', icon: '📞' },
            { key: 'location', label: 'Location', icon: '📍' },
        ];
        let contactGrid = '<div class="parse-contact-grid">';
        for (const f of contactFields) {
            const val = _isEmptyParsed(parsed[f.key]) ? '' : String(parsed[f.key]);
            const display = val || '\u2014';
            contactGrid += `<div class="parse-contact-field">
                <div class="parse-contact-label">${f.icon} ${esc(f.label)}</div>
                <div class="parse-editable" data-field="${f.key}" data-editing="false" onclick="window._parseEditField(this)">
                    <span class="parse-editable-value">${esc(display)}</span>
                    <input type="text" class="parse-editable-input" value="${esc(val)}" style="display:none" />
                </div>
            </div>`;
        }
        contactGrid += '</div>';

        // --- Card 2: Career History ---
        const careerHistory = parsed.career_history || [];
        const expYears = parsed.experience_years;
        let careerContent = '';
        if (!_isEmptyParsed(expYears)) {
            careerContent += `<div class="experience-badge">${esc(String(expYears))} years experience</div>`;
        }
        if (careerHistory.length > 0) {
            careerContent += '<div class="career-timeline">';
            for (const job of careerHistory) {
                const company = job.company || job.organization || 'Unknown Company';
                const title = job.title || job.role || '';
                const startDate = job.start_date || '';
                const endDate = job.end_date || 'Present';
                const desc = job.description || job.summary || '';
                careerContent += `<div class="career-entry">
                    <div class="career-entry-company">${esc(company)}</div>
                    ${title ? `<div class="career-entry-title">${esc(title)}</div>` : ''}
                    <div class="career-entry-dates">${esc(startDate)}${startDate ? ' \u2014 ' : ''}${esc(endDate)}</div>
                    ${desc ? `<div class="career-entry-desc">${esc(desc)}</div>` : ''}
                </div>`;
            }
            careerContent += '</div>';
        } else {
            careerContent += '<p class="parse-empty-msg">No career history extracted</p>';
        }

        // --- Card 3: AI Insights ---
        function _renderPills(arr, fieldName) {
            if (!arr || arr.length === 0) return '<span class="parse-empty-msg">\u2014</span>';
            const isInferred = inferredSet.has(fieldName);
            return '<div class="insight-pills">' + arr.map(s =>
                `<span class="insight-pill${isInferred ? ' inferred' : ''}">${esc(s)}</span>`
            ).join('') + (isInferred ? '<span class="inferred-label">✨ AI inferred</span>' : '') + '</div>';
        }

        const targetRoles = parsed.target_roles || [];
        const skills = parsed.skills || [];
        const summary = parsed.profile_summary || parsed.summary || '';
        const seniority = parsed.seniority_level || '';
        const industryPrefs = parsed.industry_preferences || [];

        let insightsContent = '';
        // Target Roles
        insightsContent += `<div class="insight-section">
            <div class="insight-section-label">Target Roles</div>
            ${_renderPills(targetRoles, 'target_roles')}
        </div>`;
        // Skills
        insightsContent += `<div class="insight-section">
            <div class="insight-section-label">Skills</div>
            ${_renderPills(skills, 'skills')}
        </div>`;
        // Profile Summary
        if (!_isEmptyParsed(summary)) {
            const summaryInferred = inferredSet.has('profile_summary');
            insightsContent += `<div class="insight-section">
                <div class="insight-section-label">Profile Summary${summaryInferred ? ' <span class="inferred-label">✨ AI inferred</span>' : ''}</div>
                <div class="insight-summary-box">${esc(summary)}</div>
            </div>`;
        }
        // Seniority Level
        if (!_isEmptyParsed(seniority)) {
            const seniorityInferred = inferredSet.has('seniority_level');
            insightsContent += `<div class="insight-section">
                <div class="insight-section-label">Seniority Level</div>
                <span class="insight-pill seniority">${esc(seniority)}</span>
                ${seniorityInferred ? '<span class="inferred-label">✨ AI inferred</span>' : ''}
            </div>`;
        }
        // Industry Preferences
        if (industryPrefs.length > 0) {
            insightsContent += `<div class="insight-section">
                <div class="insight-section-label">Industry Preferences</div>
                ${_renderPills(industryPrefs, 'industry_preferences')}
            </div>`;
        }

        content.innerHTML = `
            <div class="parse-results-card">
                <div class="parse-card-title"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg> Contact Info</div>
                ${contactGrid}
            </div>
            <div class="parse-results-card">
                <div class="parse-card-title"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 8v4l3 3"/><circle cx="12" cy="12" r="10"/></svg> Career History</div>
                ${careerContent}
            </div>
            <div class="parse-results-card">
                <div class="parse-card-title"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg> AI Insights</div>
                ${insightsContent}
            </div>
        `;

        preview.style.display = 'block';
        toast('Resume scanned! Review and confirm below.', 'success');
    } catch (e) {
        toast('Scan failed: ' + e.message, 'error');
    } finally {
        setButtonLoading(btn, false);
    }
}

async function confirmParsedProfile() {
    const parsed = state.parsedProfile;
    if (!parsed) { toast('No parsed profile data \u2014 try scanning your resume again', 'warning'); return; }
    const btn = document.getElementById('btn-confirm-parsed');
    setButtonLoading(btn, true);

    // Helper: is value empty/placeholder?
    function _isEmpty(v) {
        if (v === null || v === undefined) return true;
        if (typeof v === 'string') { const l = v.trim().toLowerCase(); return !l || ['not found','unknown','n/a','none','null'].includes(l); }
        if (Array.isArray(v)) return v.length === 0;
        return false;
    }

    // Read edited values from inline editors in the contact card
    const editedContact = {};
    document.querySelectorAll('.parse-editable').forEach(el => {
        const field = el.dataset.field;
        const input = el.querySelector('.parse-editable-input');
        if (field && input) {
            editedContact[field] = input.value.trim();
        }
    });

    const existing = state.profile || {};

    // Build data: accept everything from parsed, override contact fields with edits
    const data = {};

    // Contact fields: prefer edited value, then parsed, then existing
    for (const key of ['name', 'email', 'phone', 'location']) {
        if (editedContact[key]) {
            data[key] = editedContact[key];
        } else if (!_isEmpty(parsed[key])) {
            data[key] = parsed[key];
        } else if (existing[key]) {
            data[key] = existing[key];
        } else {
            data[key] = key === 'name' ? 'Unknown' : null;
        }
    }

    // List fields: accept AI values, fall back to existing
    for (const key of ['target_roles', 'target_locations', 'skills', 'industry_preferences']) {
        data[key] = !_isEmpty(parsed[key]) ? parsed[key] : (existing[key] || []);
    }

    // Scalar fields: accept AI values, fall back to existing
    for (const key of ['min_salary', 'max_salary', 'experience_years']) {
        data[key] = !_isEmpty(parsed[key]) ? parsed[key] : (existing[key] || null);
    }

    // String fields
    data.remote_preference = !_isEmpty(parsed.remote_preference) ? parsed.remote_preference : (existing.remote_preference || 'any');
    data.seniority_level = !_isEmpty(parsed.seniority_level) ? parsed.seniority_level : (existing.seniority_level || null);
    data.cover_letter_template = existing.cover_letter_template || null;
    data.raw_profile_doc = parsed.raw_profile_doc || existing.raw_profile_doc || null;

    // New fields from AI parsing
    data.profile_summary = !_isEmpty(parsed.profile_summary || parsed.summary) ? (parsed.profile_summary || parsed.summary) : (existing.profile_summary || null);
    data.career_trajectory = !_isEmpty(parsed.career_trajectory) ? parsed.career_trajectory : (existing.career_trajectory || null);
    data.career_history = (parsed.career_history && parsed.career_history.length > 0) ? parsed.career_history : (existing.career_history || []);

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

        document.getElementById('parsed-preview').style.display = 'none';
        populateProfileForm(profile);
        updateNavAvatar();
        try { const ps = await api('/profiles'); populateProfileDropdown(ps); } catch(e) { /* ok */ }

        // Auto-fire deep analysis in background
        api('/profiles/' + state.profileId + '/analyze', { method: 'POST' }).catch(() => {});
        toast('Analyzing profile...', 'info');

        const step2 = document.querySelector('#profile-form .profile-section');
        if (step2) step2.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e) {
        toast('Failed to save profile: ' + e.message, 'error');
    }
}

// Inline edit handler for contact info fields
window._parseEditField = function(el) {
    if (el.dataset.editing === 'true') return;
    el.dataset.editing = 'true';
    const span = el.querySelector('.parse-editable-value');
    const input = el.querySelector('.parse-editable-input');
    span.style.display = 'none';
    input.style.display = 'block';
    input.focus();
    input.select();

    function commit() {
        const val = input.value.trim();
        span.textContent = val || '\u2014';
        span.style.display = '';
        input.style.display = 'none';
        el.dataset.editing = 'false';
    }
    input.addEventListener('blur', commit, { once: true });
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
        if (e.key === 'Escape') { input.value = span.textContent === '\u2014' ? '' : span.textContent; input.blur(); }
    });
};

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
        if (resumeInput && resumeInput.files.length > 0) {
            const formData = new FormData();
            formData.append('file', resumeInput.files[0]);
            await api(`/profiles/${state.profileId}/resume`, { method: 'POST', body: formData });
            toast('Resume uploaded!', 'success');
        }

        document.getElementById('no-profile-state').style.display = 'none';
        updateNavAvatar();
        // Refresh Spring Training after profile save
        if (typeof loadSpringTraining === 'function') loadSpringTraining();
        try { const ps = await api('/profiles'); populateProfileDropdown(ps); } catch(e) { /* ok */ }
    } catch (e) {
        toast('Failed to save profile: ' + e.message, 'error');
    }
}

async function searchJobs() {
    if (!state.profileId) { toast('Create a profile first', 'error'); return; }
    // Spring Training gate
    const stLevel = getSpringTrainingLevel();
    if (stLevel.level !== 'the_show') {
        toast('Complete The Climb to unlock search! Current level: ' + SPRING_TRAINING_LEVELS[stLevel.index].name, 'warning');
        showView('dugout');
        return;
    }
    if (state.searching) return; // Prevent double-click
    state.searching = true;

    // Disable all search buttons and show loading
    const searchBtns = document.querySelectorAll('#btn-search-jobs, #btn-search-more, #btn-search-empty');
    searchBtns.forEach(btn => {
        btn.disabled = true;
        btn._origText = btn.textContent;
    });

    // Show global search badge in top nav
    const badge = document.getElementById('search-status-badge');
    if (badge) {
        badge.style.display = 'flex';
        badge.classList.remove('search-done');
        document.getElementById('search-badge-text').textContent = 'Searching...';
        document.getElementById('search-badge-count').textContent = '0';
    }

    // Show progress as a top banner in the scouting view
    const browseToolbar = document.getElementById('browse-toolbar');
    let progressBanner = document.getElementById('search-progress-banner');
    if (!progressBanner && browseToolbar) {
        progressBanner = document.createElement('div');
        progressBanner.id = 'search-progress-banner';
        browseToolbar.parentNode.insertBefore(progressBanner, browseToolbar.nextSibling);
    }
    if (progressBanner) {
        progressBanner.style.display = 'block';
        progressBanner.innerHTML = `
            <div style="display:flex;align-items:center;gap:12px;padding:12px 16px;background:linear-gradient(135deg,rgba(61,184,122,.08),rgba(74,144,217,.08));border:1px solid rgba(61,184,122,.2);border-radius:8px;margin-bottom:12px">
                <div style="font-size:24px;animation:search-dot-pulse 1.2s ease-in-out infinite">⚾</div>
                <div style="flex:1">
                    <div style="display:flex;align-items:center;gap:8px">
                        <span style="font-weight:600;font-size:14px;color:var(--bright)">Scouting for Jobs...</span>
                        <span id="search-job-count" style="font-weight:700;font-size:16px;color:#3DB87A;display:none">0</span>
                        <span id="search-job-label" style="font-size:11px;color:var(--jb-text-dim);display:none">found</span>
                    </div>
                    <div id="search-status-detail" style="font-size:11px;color:var(--jb-text-dim);margin-top:2px">Starting search across job boards...</div>
                    <div style="width:100%;height:3px;background:var(--jb-surface-alt,#1a2744);border-radius:2px;margin-top:6px">
                        <div id="search-progress-bar" style="width:5%;height:100%;background:linear-gradient(90deg,#C4962C,#3DB87A);border-radius:2px;transition:width 0.5s ease"></div>
                    </div>
                </div>
                <div id="search-stage-text" style="font-size:10px;color:var(--jb-text-dim);text-align:right;min-width:80px">Initializing...</div>
            </div>`;
    }

    const stages = ['Expanding queries with AI...', 'Searching job boards...', 'Scoring results...', 'Finalizing...'];
    let stageIdx = 0;
    const stageInterval = setInterval(() => {
        stageIdx = Math.min(stageIdx + 1, stages.length - 1);
        const el = document.getElementById('search-stage-text');
        if (el) el.textContent = stages[stageIdx];
        const bar = document.getElementById('search-progress-bar');
        if (bar) bar.style.width = Math.min(10 + stageIdx * 25, 90) + '%';
    }, 15000);

    try {
        // Get selected sources from checkboxes
        const selectedSources = Array.from(document.querySelectorAll('#source-selector input:checked'))
            .map(cb => cb.value);
        const sourceParams = selectedSources.length > 0
            ? '?' + selectedSources.map(s => `sources=${s}`).join('&')
            : '';

        // Start search (returns immediately with task_id)
        const { task_id } = await api(`/profiles/${state.profileId}/search${sourceParams}`, { method: 'POST' });

        // Poll for new jobs every 5 seconds while search runs
        let jobPollCount = 0;
        const jobPoller = setInterval(async () => {
            try {
                const recent = await api(`/profiles/${state.profileId}/jobs/recent`);
                const newCount = recent.total_pending || 0;
                if (newCount > jobPollCount) {
                    jobPollCount = newCount;
                    const countEl = document.getElementById('search-job-count');
                    const labelEl = document.getElementById('search-job-label');
                    if (countEl) { countEl.textContent = newCount; countEl.style.display = 'block'; }
                    if (labelEl) labelEl.style.display = 'block';
                    const detailEl = document.getElementById('search-status-detail');
                    if (detailEl) detailEl.textContent = `Found ${newCount} jobs and counting...`;
                    // Update global badge
                    const badgeCount = document.getElementById('search-badge-count');
                    if (badgeCount) badgeCount.textContent = newCount;
                }
            } catch (e) { /* ignore poll errors */ }
        }, 5000);

        // Poll for task completion
        let taskDone = false;
        for (let i = 0; i < 120; i++) { // max 10 minutes (120 * 5s)
            await new Promise(r => setTimeout(r, 5000));
            try {
                const task = await api(`/tasks/${task_id}`);
                if (task.status === 'completed') {
                    taskDone = true;
                    const result = task.result || {};
                    toast(`Found ${result.new_jobs || 0} new jobs (${result.duplicates_skipped || 0} duplicates skipped)`, 'success');
                    break;
                }
                if (task.status === 'failed') {
                    throw new Error(task.error || 'Search failed');
                }
            } catch (e) {
                if (e.message && !e.message.includes('404')) {
                    clearInterval(jobPoller);
                    throw e;
                }
            }
        }

        clearInterval(jobPoller);

        if (!taskDone) {
            toast('Search is still running in the background. Refresh to see results.', 'info');
        }

        // Final load — remove progress banner
        const _progressBanner = document.getElementById('search-progress-banner');
        if (_progressBanner) _progressBanner.style.display = 'none';

        await loadSwipeStack();
        loadStats();
        showView('hunt');
    } catch (e) {
        toast('Search failed: ' + e.message, 'error');
    } finally {
        clearInterval(stageInterval);
        state.searching = false;
        searchBtns.forEach(btn => {
            btn.disabled = false;
            btn.textContent = btn._origText || 'Search for Jobs';
        });
        // Update global badge to "done" state, auto-hide after 10s
        const _badge = document.getElementById('search-status-badge');
        if (_badge) {
            _badge.classList.add('search-done');
            const _badgeText = document.getElementById('search-badge-text');
            if (_badgeText) _badgeText.textContent = 'Done!';
            setTimeout(() => { _badge.style.display = 'none'; }, 10000);
        }
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

        // Baseball card front — avatar, name, position
        renderBaseballCardFront();
        // Baseball card blurb — AI-generated "back of the card" flavor text
        renderBaseballCardBlurb(stats);
    } catch (e) {
        console.error('Stats load failed:', e);
    }
}

function renderBaseballCardFront() {
    const el = document.getElementById('card-front');
    if (!el || !state.profile) return;

    const p = state.profile;
    const name = p.name || 'Unknown';
    const nameParts = name.split(' ');
    const firstName = nameParts[0];
    const lastName = nameParts.slice(1).join(' ').toUpperCase() || '';
    const loc = p.location || '';
    const pos = p.seniority_level ? p.seniority_level.substring(0, 2).toUpperCase() : 'SS';
    const pictureUrl = state.authUser?.picture_url || '';

    const avatarHTML = pictureUrl
        ? `<img src="${esc(pictureUrl)}" alt="${esc(name)}" referrerpolicy="no-referrer" style="width:100%;height:100%;border-radius:50%;object-fit:cover;" />`
        : `${(firstName[0] || '') + (lastName[0] || '')}`;

    el.innerHTML = `
        <div class="card-front-number">#${state.profileId || 0}</div>
        <div class="card-front-position">${esc(pos)}</div>
        <div class="card-front-avatar">${avatarHTML}</div>
        <div class="card-front-name">
            ${esc(firstName)}
            <span class="card-front-lastname">${esc(lastName)}</span>
        </div>
        <div class="card-front-team">${esc(loc)}</div>
        <svg class="card-front-diamond" width="32" height="32" viewBox="0 0 40 40" fill="none">
            <path d="M20 2 L36 18 L20 34 L4 18Z" fill="var(--jb-navy,#1D2D5C)" stroke="var(--jb-bright,#4A90D9)" stroke-width="0.8" opacity="0.5"/>
            <path d="M20 22 L28 18 L20 14 L12 18Z" fill="var(--jb-red,#E8291C)" opacity="0.35"/>
        </svg>
    `;
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
    const yrs = p.experience_years ? p.experience_years : null;
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
            <div class="card-blurb-role">${esc(roleText)}${yrs ? ' · ' + yrs + ' yrs' : ''} · ${esc(loc)}</div>
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

function getFilteredJobs(jobs) {
    if (!jobs || jobs.length === 0) return [];
    const f = state.filters;
    let result = jobs.filter(job => {
        // Score filter
        if (f.scoreMin > 0 && (job.match_score || 0) < f.scoreMin) return false;
        // Remote type filter
        if (f.remoteType !== 'all') {
            const rt = (job.remote_type || '').toLowerCase();
            if (f.remoteType === 'remote' && !rt.includes('remote')) return false;
            if (f.remoteType === 'hybrid' && !rt.includes('hybrid')) return false;
            if (f.remoteType === 'onsite' && !rt.includes('on-site') && !rt.includes('onsite') && !rt.includes('on site') && rt.includes('remote') === false && rt.includes('hybrid') === false && rt !== '') return false;
            if (f.remoteType === 'onsite' && rt === '') return false;
        }
        // Salary minimum filter
        if (f.salaryMin !== '' && f.salaryMin > 0) {
            const jobSalary = job.salary_max || job.salary_min || 0;
            if (jobSalary < f.salaryMin) return false;
        }
        // Keyword filter
        if (f.keyword.trim()) {
            const kw = f.keyword.toLowerCase();
            const haystack = [job.title, job.company, job.description, job.location].filter(Boolean).join(' ').toLowerCase();
            if (!haystack.includes(kw)) return false;
        }
        // Date posted filter
        if (f.datePosted !== 'all' && job.posted_date) {
            const posted = new Date(job.posted_date);
            const now = new Date();
            const diffDays = (now - posted) / (1000 * 60 * 60 * 24);
            if (f.datePosted === '24h' && diffDays > 1) return false;
            if (f.datePosted === '7d' && diffDays > 7) return false;
            if (f.datePosted === '30d' && diffDays > 30) return false;
        } else if (f.datePosted !== 'all' && !job.posted_date) {
            return false; // No date info, exclude from date-filtered results
        }
        return true;
    });
    // Sort
    result.sort((a, b) => {
        let cmp = 0;
        switch (f.sortBy) {
            case 'score':
                cmp = (a.match_score || 0) - (b.match_score || 0);
                break;
            case 'date':
                const da = a.posted_date ? new Date(a.posted_date).getTime() : 0;
                const db = b.posted_date ? new Date(b.posted_date).getTime() : 0;
                cmp = da - db;
                break;
            case 'salary':
                cmp = (a.salary_max || a.salary_min || 0) - (b.salary_max || b.salary_min || 0);
                break;
            case 'company':
                cmp = (a.company || '').localeCompare(b.company || '');
                break;
        }
        return f.sortDir === 'desc' ? -cmp : cmp;
    });
    return result;
}

function countActiveFilters() {
    const f = state.filters;
    let count = 0;
    if (f.scoreMin > 0) count++;
    if (f.remoteType !== 'all') count++;
    if (f.salaryMin !== '' && f.salaryMin > 0) count++;
    if (f.keyword.trim()) count++;
    if (f.datePosted !== 'all') count++;
    return count;
}

function clearFilters() {
    state.filters.scoreMin = 0;
    state.filters.remoteType = 'all';
    state.filters.salaryMin = '';
    state.filters.keyword = '';
    state.filters.datePosted = 'all';
    renderBrowseView();
}

let _filterDebounceTimer = null;
let _filterFocusField = null;
function updateFilter(key, value) {
    state.filters[key] = value;
    _filterFocusField = key === 'keyword' ? 'keyword' : (key === 'salaryMin' ? 'salary' : null);
    if (key === 'keyword') {
        clearTimeout(_filterDebounceTimer);
        _filterDebounceTimer = setTimeout(() => renderBrowseView(), 200);
    } else {
        renderBrowseView();
    }
}

function toggleSortDir() {
    state.filters.sortDir = state.filters.sortDir === 'desc' ? 'asc' : 'desc';
    renderBrowseView();
}

function toggleFiltersOpen() {
    state.filters.filtersOpen = !state.filters.filtersOpen;
    renderBrowseView();
}

function renderFilterBar(totalJobs, filteredCount) {
    const bar = document.getElementById('job-filter-bar');
    if (!bar) return;
    const f = state.filters;
    const activeCount = countActiveFilters();
    const toggleLabel = f.filtersOpen ? 'Hide Filters' : 'Filters';
    const badge = activeCount > 0 ? ` (${activeCount})` : '';

    bar.innerHTML = `
        <div class="filter-toggle-row">
            <button class="filter-toggle-btn" onclick="toggleFiltersOpen()">
                ${toggleLabel}${badge}
                <span class="filter-chevron ${f.filtersOpen ? 'open' : ''}">&rsaquo;</span>
            </button>
            <span class="filter-count">${filteredCount} of ${totalJobs} jobs</span>
            <div class="filter-sort-group">
                <button class="filter-sort-btn ${f.sortBy==='score'?'active':''}" onclick="updateFilter('sortBy','score')">Score</button>
                <button class="filter-sort-btn ${f.sortBy==='date'?'active':''}" onclick="updateFilter('sortBy','date')">Date</button>
                <button class="filter-sort-btn ${f.sortBy==='salary'?'active':''}" onclick="updateFilter('sortBy','salary')">Salary</button>
                <button class="filter-sort-btn ${f.sortBy==='company'?'active':''}" onclick="updateFilter('sortBy','company')">Company</button>
                <button class="filter-sort-dir-btn" onclick="toggleSortDir()" title="Sort direction: ${f.sortDir}">
                    ${f.sortDir === 'desc' ? '&#9660;' : '&#9650;'}
                </button>
            </div>
        </div>
        ${f.filtersOpen ? `
        <div class="filter-fields-row">
            <div class="filter-score-group">
                <label class="filter-label">Score &ge;${f.scoreMin}</label>
                <input type="range" class="filter-slider" min="0" max="100" value="${f.scoreMin}"
                    oninput="document.querySelector('.filter-label').textContent='Score \\u2265'+this.value"
                    onchange="updateFilter('scoreMin', parseInt(this.value))">
            </div>
            <select class="filter-select" onchange="updateFilter('remoteType', this.value)">
                <option value="all" ${f.remoteType==='all'?'selected':''}>All Types</option>
                <option value="remote" ${f.remoteType==='remote'?'selected':''}>Remote</option>
                <option value="hybrid" ${f.remoteType==='hybrid'?'selected':''}>Hybrid</option>
                <option value="onsite" ${f.remoteType==='onsite'?'selected':''}>On-site</option>
            </select>
            <input type="number" class="filter-search" placeholder="Min salary" value="${f.salaryMin}"
                onchange="updateFilter('salaryMin', this.value ? parseInt(this.value) : '')" style="max-width:120px">
            <input type="text" class="filter-search" placeholder="Keyword..." value="${f.keyword}"
                oninput="updateFilter('keyword', this.value)">
            <select class="filter-select" onchange="updateFilter('datePosted', this.value)">
                <option value="all" ${f.datePosted==='all'?'selected':''}>Any Date</option>
                <option value="24h" ${f.datePosted==='24h'?'selected':''}>Last 24h</option>
                <option value="7d" ${f.datePosted==='7d'?'selected':''}>Last 7 days</option>
                <option value="30d" ${f.datePosted==='30d'?'selected':''}>Last 30 days</option>
            </select>
            ${activeCount > 0 ? `<button class="filter-clear-btn" onclick="clearFilters()">Clear filters</button>` : ''}
        </div>` : ''}
    `;
    bar.style.display = 'block';
    // Restore focus to text inputs after re-render
    if (_filterFocusField === 'keyword') {
        const kwInput = bar.querySelector('input[type="text"]');
        if (kwInput) { kwInput.focus(); kwInput.selectionStart = kwInput.selectionEnd = kwInput.value.length; }
    } else if (_filterFocusField === 'salary') {
        const salInput = bar.querySelector('input[type="number"]');
        if (salInput) { salInput.focus(); }
    }
    _filterFocusField = null;
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

    const allJobs = state.swipeStack;
    const filterBar = document.getElementById('job-filter-bar');
    if (!allJobs || allJobs.length === 0) {
        listView.style.display = 'none';
        gridView.style.display = 'none';
        feed.style.display = 'none';
        actionBar.style.display = 'none';
        toolbar.style.display = 'none';
        if (filterBar) filterBar.style.display = 'none';
        // Show contextual empty state
        const stLevel = typeof getSpringTrainingLevel === 'function' ? getSpringTrainingLevel() : null;
        if (stLevel && stLevel.level !== 'the_show') {
            empty.innerHTML = `
                <div style="font-size:48px;margin-bottom:16px">&#x1F50D;</div>
                <h2>No jobs yet</h2>
                <p>Complete The Climb to unlock job search, then hit SEARCH JOBS to find opportunities.</p>
                <button class="btn btn-primary" onclick="showView('dugout')">Go to The Climb</button>`;
        } else {
            empty.innerHTML = `
                <div style="font-size:48px;margin-bottom:16px">&#x1F50D;</div>
                <h2>No jobs yet</h2>
                <p>Hit SEARCH JOBS to find opportunities.</p>
                <div class="btn-group">
                    <button class="cta-btn btn-search-trigger" id="btn-search-empty" onclick="searchJobs()">Search for Jobs</button>
                    <button class="btn btn-secondary" onclick="showView('profile')">Edit Profile</button>
                </div>`;
        }
        empty.style.display = 'block';
        return;
    }

    const jobs = getFilteredJobs(allJobs);

    empty.style.display = 'none';
    toolbar.style.display = 'flex';
    countEl.textContent = `${jobs.length} of ${allJobs.length} jobs`;
    renderFilterBar(allJobs.length, jobs.length);

    if (jobs.length === 0) {
        listView.style.display = 'none';
        gridView.style.display = 'none';
        feed.style.display = 'none';
        actionBar.style.display = 'none';
        empty.innerHTML = `
            <div style="font-size:48px;margin-bottom:16px">&#x1F50E;</div>
            <h2>No matches</h2>
            <p>No jobs match your current filters. Try adjusting or clearing them.</p>
            <button class="btn btn-secondary" onclick="clearFilters()">Clear Filters</button>`;
        empty.style.display = 'block';
        return;
    }

    if (state.browseMode === 'list') {
        listView.style.display = 'block';
        gridView.style.display = 'none';
        feed.style.display = 'none';
        actionBar.style.display = 'none';
        renderJobList(jobs);
    } else if (state.browseMode === 'grid') {
        listView.style.display = 'none';
        gridView.style.display = 'block';
        feed.style.display = 'none';
        actionBar.style.display = 'none';
        renderJobGrid(jobs);
    } else {
        // Card mode — existing single-card swipe view
        listView.style.display = 'none';
        gridView.style.display = 'none';
        feed.style.display = 'block';
        actionBar.style.display = 'flex';
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

const PIPELINE_STAGES = [
    { key: 'applied', label: 'Applied', color: '#5B9FD6' },
    { key: 'screening', label: 'Screening', color: '#E5A030' },
    { key: 'interview', label: 'Interview', color: '#C4962C' },
    { key: 'offer', label: 'Offer', color: '#3DB87A' },
    { key: 'accepted', label: 'Accepted', color: '#2ECC71' },
    { key: 'rejected', label: 'Rejected', color: '#E05252' },
    { key: 'no_response', label: 'No Response', color: '#888' },
];

async function updateAppPipelineStatus(appId, newStatus) {
    try {
        await api(`/applications/${appId}`, {
            method: 'PUT',
            body: { pipeline_status: newStatus },
        });
        toast(`Status updated to ${PIPELINE_STAGES.find(s => s.key === newStatus)?.label || newStatus}`, 'success');
        loadApplications();
    } catch (e) {
        toast('Failed to update status: ' + e.message, 'error');
    }
}

async function saveAppNotes(appId) {
    const textarea = document.getElementById(`app-notes-${appId}`);
    if (!textarea) return;
    try {
        await api(`/applications/${appId}`, {
            method: 'PUT',
            body: { notes: textarea.value },
        });
        toast('Notes saved', 'success');
    } catch (e) {
        toast('Failed to save notes: ' + e.message, 'error');
    }
}

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

        // Count pipeline stages and render stage bar
        const stageCounts = {};
        PIPELINE_STAGES.forEach(s => stageCounts[s.key] = 0);
        apps.forEach(a => {
            const ps = a.pipeline_status || 'applied';
            if (stageCounts[ps] !== undefined) stageCounts[ps]++;
            else stageCounts['applied']++;
        });

        if (pipeline) {
            pipeline.innerHTML = `
                <div style="display:flex;gap:4px;padding:12px 0;flex-wrap:wrap">
                    ${PIPELINE_STAGES.map(s => `
                        <div style="
                            display:flex;align-items:center;gap:6px;padding:6px 12px;
                            border-radius:20px;font-size:12px;font-weight:600;
                            background:${stageCounts[s.key] > 0 ? s.color + '22' : 'var(--jb-bg-tertiary)'};
                            color:${stageCounts[s.key] > 0 ? s.color : 'var(--jb-text-dim)'};
                            border:1px solid ${stageCounts[s.key] > 0 ? s.color + '44' : 'var(--jb-border)'};
                            ${stageCounts[s.key] > 0 ? '' : 'opacity:0.5;'}
                        ">
                            <span style="font-size:14px;font-weight:700">${stageCounts[s.key]}</span>
                            ${s.label}
                        </div>
                    `).join('')}
                </div>`;
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
    const pipelineStatus = a.pipeline_status || 'applied';
    const pipelineStage = PIPELINE_STAGES.find(s => s.key === pipelineStatus) || PIPELINE_STAGES[0];
    return `
    <div class="app-item" onclick="showAppDetail(${a.id})" style="position:relative">
        <div class="app-info">
            <h3>${esc(a.job_title)}</h3>
            <p>${esc(a.company)} ${a.applied_at ? '&middot; Applied ' + new Date(a.applied_at).toLocaleDateString() : ''}</p>
            ${a.status === 'ready' ? '<p style="color:var(--accent-light);font-size:12px">Application materials prepared - ready for submission</p>' : ''}
            ${a.status === 'failed' ? `<p style="color:var(--red);font-size:12px">${esc(a.error_message || 'Application failed')}</p>` : ''}
        </div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">
            <span class="app-status status-${a.status}">${statusLabel}</span>
            <span style="font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px;background:${pipelineStage.color}22;color:${pipelineStage.color};border:1px solid ${pipelineStage.color}33">${pipelineStage.label}</span>
        </div>
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

        // Pipeline status dropdown
        const currentPipeline = app.pipeline_status || 'applied';
        const pipelineHtml = `
            <div class="detail-section" style="padding:12px 0">
                <h4>Pipeline Stage</h4>
                <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
                    <select id="pipeline-select-${app.id}" onchange="updateAppPipelineStatus(${app.id}, this.value)" style="
                        padding:6px 12px;border-radius:6px;font-size:13px;font-weight:500;
                        background:var(--jb-bg-tertiary);color:var(--jb-text-1);border:1px solid var(--jb-border);
                        cursor:pointer;outline:none;
                    ">
                        ${PIPELINE_STAGES.map(s => `<option value="${s.key}" ${s.key === currentPipeline ? 'selected' : ''}>${s.label}</option>`).join('')}
                    </select>
                    <span style="font-size:11px;color:var(--jb-text-dim)">Update pipeline stage</span>
                </div>
            </div>`;

        // Notes section
        const notesHtml = `
            <div class="detail-section" style="padding:12px 0">
                <h4>Notes</h4>
                <textarea id="app-notes-${app.id}" placeholder="Add notes about this application..." rows="3" style="
                    width:100%;padding:8px 12px;border-radius:6px;font-size:13px;
                    background:var(--jb-bg-tertiary);color:var(--jb-text-1);border:1px solid var(--jb-border);
                    resize:vertical;font-family:inherit;margin-top:6px;
                ">${esc(app.notes || '')}</textarea>
                <button class="btn btn-sm btn-secondary" onclick="saveAppNotes(${app.id})" style="margin-top:6px">Save Notes</button>
            </div>`;

        panel.innerHTML = `
            <div class="detail-header">
                <div>
                    <h3>${esc(app.job_title)}</h3>
                    <p>${esc(app.company)}</p>
                </div>
                <span class="app-status status-${app.status}">${statusLabels[app.status] || app.status}</span>
            </div>
            ${pipelineHtml}
            ${timelineHtml}
            ${notesHtml}
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

function toggleAdminTools(show) {
    document.querySelectorAll('.admin-tool').forEach(el => {
        el.style.display = show ? '' : 'none';
    });
    if (show) { loadModelConfig(); loadPromptLab(); }
}

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

    // Load Prompt Lab and Model Config
    loadModelConfig();
    loadPromptLab();
}

// ── Prompt Lab ──────────────────────────────────────────────────────────

const TIER_LABELS = { flash: 'Flash', balanced: 'Balanced', deep: 'Deep' };
const TIER_COLORS = { flash: '#4ade80', balanced: '#60a5fa', deep: '#c084fc' };
const CAT_ICONS = { search: 'search', scoring: 'analytics', applications: 'description', profile: 'person', intelligence: 'psychology' };

async function loadModelConfig() {
    const container = document.getElementById('model-config-container');
    if (!container) return;
    try {
        const config = await api('/config/models');
        const p = config.provider;
        const providerLabel = p === 'anthropic' ? 'Claude (Anthropic)' : p === 'gemini' ? 'Gemini (Google)' : 'None';

        // Sanitize: show provider status (connected/not) without exposing internal model identifiers
        const providerStatus = p ? 'Connected' : 'Not configured';
        let html = `<div style="margin-bottom:16px">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
                <span style="font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:1px">AI Provider:</span>
                <span style="font-size:13px;font-weight:600;color:var(--text-bright)">${providerLabel} (${providerStatus})</span>
            </div>
            <div style="font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Model Tiers</div>
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">`;
        for (const tier of ['flash', 'balanced', 'deep']) {
            const info = config.tiers[tier];
            // Show tier status without exposing raw model identifiers
            const tierStatus = info.active ? 'Active' : 'N/A';
            html += `<div style="background:var(--card-bg);border:1px solid var(--border);border-radius:8px;padding:10px;text-align:center">
                <div style="font-size:11px;font-weight:600;color:${TIER_COLORS[tier]};text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px">${TIER_LABELS[tier]}</div>
                <div style="font-size:12px;color:var(--text-bright);word-break:break-all">${tierStatus}</div>
            </div>`;
        }
        html += `</div></div>`;

        // Per-feature tier overrides
        const overrides = config.feature_overrides;
        const overrideKeys = Object.keys(overrides);
        if (overrideKeys.length > 0) {
            const activeOverrides = overrideKeys.filter(k => overrides[k].override_tier);
            html += `<div style="font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:1px;margin:12px 0 8px">Feature Tier Overrides <span style="color:var(--text-muted)">(${activeOverrides.length} active)</span></div>`;
            if (activeOverrides.length > 0) {
                html += `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">`;
                for (const k of activeOverrides) {
                    const ov = overrides[k];
                    html += `<span style="font-size:11px;padding:3px 8px;background:${TIER_COLORS[ov.override_tier]}22;color:${TIER_COLORS[ov.override_tier]};border:1px solid ${TIER_COLORS[ov.override_tier]}44;border-radius:4px">${k}: ${ov.default_tier} &rarr; ${ov.override_tier} <a href="#" onclick="event.preventDefault();clearModelOverride('${k}')" style="color:var(--red);margin-left:4px" title="Clear override">&times;</a></span>`;
                }
                html += `</div>`;
            }
            html += `<p style="font-size:11px;color:var(--text-muted);margin-top:4px">Set per-feature overrides from the Prompt Lab below using the tier dropdown on each prompt.</p>`;
        }

        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = `<div style="color:var(--red);font-size:13px">Failed to load model config: ${e.message}</div>`;
    }
}

async function loadPromptLab() {
    const container = document.getElementById('prompt-lab-container');
    if (!container) return;
    try {
        const grouped = await api('/config/prompts');
        let html = '';

        for (const [catKey, catData] of Object.entries(grouped)) {
            if (!catData.prompts || catData.prompts.length === 0) continue;
            html += `<div class="prompt-lab-category" style="margin-bottom:16px">
                <div onclick="this.parentElement.classList.toggle('collapsed')" style="cursor:pointer;display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--border)">
                    <span style="font-size:14px;color:var(--text-bright);font-weight:600;flex:1">${catData.label}</span>
                    <span style="font-size:11px;color:var(--text-muted)">${catData.prompts.length} prompts</span>
                    <span style="color:var(--text-dim);font-size:16px;transition:transform 0.2s" class="prompt-cat-arrow">&#9662;</span>
                </div>
                <div class="prompt-cat-items" style="margin-top:8px">`;

            for (const p of catData.prompts) {
                const tierColor = TIER_COLORS[p.model_tier] || '#888';
                const modifiedBadge = p.is_modified ? '<span style="font-size:9px;padding:1px 6px;background:var(--orange,#f59e0b);color:#000;border-radius:4px;font-weight:600;margin-left:6px">MODIFIED</span>' : '';
                const overrideBadge = p.model_tier_override ? `<span style="font-size:9px;padding:1px 6px;background:${TIER_COLORS[p.model_tier_override]}33;color:${TIER_COLORS[p.model_tier_override]};border-radius:4px;margin-left:4px">OVERRIDE: ${p.model_tier_override}</span>` : '';

                html += `<div class="prompt-lab-item" id="prompt-item-${p.key}" style="background:var(--card-bg);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:8px">
                    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                        <span style="font-size:13px;font-weight:600;color:var(--text-bright);flex:1;min-width:200px">${p.name}${modifiedBadge}</span>
                        <span style="font-size:10px;padding:2px 8px;background:${tierColor}22;color:${tierColor};border-radius:4px;font-weight:600">${TIER_LABELS[p.model_tier] || p.model_tier}</span>
                        ${overrideBadge}
                        <span style="font-size:10px;color:var(--text-muted)">${p.file}:${p.function}</span>
                    </div>
                    <div style="font-size:12px;color:var(--text-dim);margin-top:4px">${p.description}</div>
                    <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
                        <button class="btn btn-secondary btn-sm" onclick="togglePromptEdit('${p.key}')" style="font-size:11px;padding:3px 10px">View / Edit</button>
                        <button class="btn btn-secondary btn-sm" onclick="enhancePrompt('${p.key}')" style="font-size:11px;padding:3px 10px">Enhance with AI</button>
                        ${p.is_modified ? `<button class="btn btn-secondary btn-sm" onclick="resetPrompt('${p.key}')" style="font-size:11px;padding:3px 10px;color:var(--red)">Reset</button>` : ''}
                        <select onchange="saveModelOverride('${p.key}', this.value)" style="font-size:11px;padding:3px 8px;background:var(--card-bg);color:var(--text-dim);border:1px solid var(--border);border-radius:4px;cursor:pointer" title="Override model tier">
                            <option value="" ${!p.model_tier_override ? 'selected' : ''}>Tier: Default (${TIER_LABELS[p.model_tier]})</option>
                            <option value="flash" ${p.model_tier_override === 'flash' ? 'selected' : ''}>Tier: Flash</option>
                            <option value="balanced" ${p.model_tier_override === 'balanced' ? 'selected' : ''}>Tier: Balanced</option>
                            <option value="deep" ${p.model_tier_override === 'deep' ? 'selected' : ''}>Tier: Deep</option>
                        </select>
                    </div>
                    <div id="prompt-edit-${p.key}" style="display:none;margin-top:12px">
                        <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">Variables: ${(p.variables || []).map(v => '<code style="background:var(--surface);padding:1px 4px;border-radius:3px;font-size:10px">{' + v + '}</code>').join(' ')}</div>
                        <textarea id="prompt-textarea-${p.key}" style="width:100%;min-height:200px;max-height:500px;background:var(--surface);color:var(--text-bright);border:1px solid var(--border);border-radius:6px;padding:10px;font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.5;resize:vertical">${_escapeHtml(p.prompt_template)}</textarea>
                        <div style="display:flex;gap:6px;margin-top:8px">
                            <button class="btn btn-primary btn-sm" onclick="savePrompt('${p.key}')" style="font-size:11px;padding:4px 14px">Save Changes</button>
                            <button class="btn btn-secondary btn-sm" onclick="togglePromptEdit('${p.key}')" style="font-size:11px;padding:4px 14px">Cancel</button>
                        </div>
                        <div id="prompt-enhance-${p.key}" style="display:none;margin-top:12px"></div>
                    </div>
                </div>`;
            }
            html += `</div></div>`;
        }

        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = `<div style="color:var(--red);font-size:13px">Failed to load prompts: ${e.message}</div>`;
    }
}

function _escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function togglePromptEdit(key) {
    const editDiv = document.getElementById(`prompt-edit-${key}`);
    if (editDiv) {
        editDiv.style.display = editDiv.style.display === 'none' ? 'block' : 'none';
    }
}

async function savePrompt(key) {
    const textarea = document.getElementById(`prompt-textarea-${key}`);
    if (!textarea) return;
    try {
        await api(`/config/prompts/${key}`, {
            method: 'PUT',
            body: { prompt_template: textarea.value },
        });
        toast('Prompt saved', 'success');
        loadPromptLab();
    } catch (e) {
        toast('Save failed: ' + e.message, 'error');
    }
}

async function resetPrompt(key) {
    try {
        const result = await api(`/config/prompts/${key}/reset`, { method: 'POST' });
        toast('Prompt reset to default', 'success');
        loadPromptLab();
    } catch (e) {
        toast('Reset failed: ' + e.message, 'error');
    }
}

async function enhancePrompt(key) {
    // Open edit pane if not already open
    const editDiv = document.getElementById(`prompt-edit-${key}`);
    if (editDiv) editDiv.style.display = 'block';

    const enhanceDiv = document.getElementById(`prompt-enhance-${key}`);
    if (!enhanceDiv) return;

    enhanceDiv.style.display = 'block';
    enhanceDiv.innerHTML = '<div style="color:var(--text-dim);font-size:12px;padding:8px"><span class="loading-dots">Analyzing prompt with AI</span></div>';

    try {
        const result = await api(`/config/prompts/${key}/enhance`, { method: 'POST' });
        const score = parseInt(result.quality_score) || 0;
        let html = `<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
                <span style="font-size:12px;font-weight:600;color:var(--text-bright)">AI Enhancement Analysis</span>
                <span style="font-size:11px;padding:2px 8px;background:${score >= 70 ? '#4ade8022' : score >= 40 ? '#f59e0b22' : '#ef444422'};color:${score >= 70 ? '#4ade80' : score >= 40 ? '#f59e0b' : '#ef4444'};border-radius:4px;font-weight:600">Score: ${score}/100</span>
            </div>
            <div style="font-size:12px;color:var(--text-dim);margin-bottom:8px">${result.analysis || ''}</div>`;

        if (result.suggestions && result.suggestions.length > 0) {
            html += `<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:0.5px">Suggestions</div>
                <ul style="margin:0 0 8px;padding-left:16px;font-size:12px;color:var(--text-dim)">`;
            for (const s of result.suggestions) {
                html += `<li style="margin-bottom:4px">${_escapeHtml(s)}</li>`;
            }
            html += `</ul>`;
        }

        if (result.improved_template) {
            html += `<button class="btn btn-primary btn-sm" onclick="applyEnhancedPrompt('${key}')" style="font-size:11px;padding:4px 14px;margin-top:4px">Apply Improved Version</button>
                <textarea id="prompt-enhanced-${key}" style="display:none">${_escapeHtml(result.improved_template)}</textarea>`;
        }
        html += `</div>`;
        enhanceDiv.innerHTML = html;
    } catch (e) {
        enhanceDiv.innerHTML = `<div style="color:var(--red);font-size:12px;padding:8px">Enhancement failed: ${e.message}</div>`;
    }
}

function applyEnhancedPrompt(key) {
    const enhanced = document.getElementById(`prompt-enhanced-${key}`);
    const textarea = document.getElementById(`prompt-textarea-${key}`);
    if (enhanced && textarea) {
        textarea.value = enhanced.value;
        toast('Enhanced prompt applied to editor - click Save to persist', 'success');
    }
}

async function saveModelOverride(featureKey, tier) {
    try {
        await api('/config/models/override', {
            method: 'PUT',
            body: { feature_key: featureKey, model_tier: tier },
        });
        toast(tier ? `Model override: ${featureKey} -> ${tier}` : `Model override cleared for ${featureKey}`, 'success');
        loadModelConfig();
    } catch (e) {
        toast('Override failed: ' + e.message, 'error');
    }
}

async function clearModelOverride(featureKey) {
    await saveModelOverride(featureKey, '');
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

async function runSpringTrainingAnalysis(btn) {
    if (!state.profileId) { toast('No profile to analyze', 'error'); return; }
    if (btn) { btn.disabled = true; btn.textContent = 'Analyzing...'; }
    try {
        await api(`/profiles/${state.profileId}/analyze`, { method: 'POST' });
        toast('Profile analyzed! Level updated.', 'success');
        await loadProfile();
        if (typeof loadSpringTraining === 'function') loadSpringTraining();
    } catch (e) {
        toast('Analysis failed: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Analyze Profile'; }
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

        const pScore = data.profile_score != null ? data.profile_score : data.score;
        const scoreColor = pScore >= 80 ? '#4CAF50' : pScore >= 50 ? '#FFA726' : '#EF5350';
        const scoreTitle = pScore >= 80 ? 'Profile Ready' : 'Building Profile';
        const totalChecks = (data.checks || []).length;
        const passedChecks = (data.checks || []).filter(c => c.passed).length;

        // Split checks into profile vs search tiers
        const profileChecks = (data.checks || []).filter(c => c.tier !== 'search');
        const searchChecks = (data.checks || []).filter(c => c.tier === 'search');
        const profilePassed = profileChecks.filter(c => c.passed).length;
        const searchPassed = searchChecks.filter(c => c.passed).length;
        const profileTierScore = profileChecks.length > 0 ? Math.round(profilePassed / profileChecks.length * 100) : 0;
        const searchTierScore = searchChecks.length > 0 ? Math.round(searchPassed / searchChecks.length * 100) : 0;

        function renderCheck(c) {
            const actionAttr = (!c.passed && c.action) ? ` onclick="handleReadinessAction('${esc(c.action)}')"` : '';
            const cursorStyle = (!c.passed && c.action) ? ' cursor:pointer;' : '';
            return `<div class="readiness-check ${c.passed ? 'check-pass' : 'check-fail'}"${actionAttr} style="${cursorStyle}">
                <span class="check-icon">${c.passed ? '✓' : '✕'}</span>
                <span class="check-name">${esc(c.name)}</span>
                <span class="check-detail">${esc(c.detail)}</span>
            </div>`;
        }

        // Profile tier
        const profileTierHtml = `<div class="readiness-tier">
            <div class="readiness-tier-header" onclick="toggleReadinessTier('profile')">
                <span class="readiness-tier-title">Profile Readiness</span>
                <span class="readiness-tier-score" style="color:${profileTierScore >= 80 ? '#4CAF50' : profileTierScore >= 50 ? '#FFA726' : '#EF5350'}">${profileTierScore}%</span>
                <span class="readiness-tier-chevron" id="readiness-profile-chevron">▼</span>
            </div>
            <div class="readiness-tier-body" id="readiness-profile-body">
                ${profileChecks.map(renderCheck).join('')}
            </div>
        </div>`;

        // Search tier
        let searchTierHtml = '';
        if (data.has_searched) {
            searchTierHtml = `<div class="readiness-tier">
                <div class="readiness-tier-header" onclick="toggleReadinessTier('search')">
                    <span class="readiness-tier-title">Search Performance</span>
                    <span class="readiness-tier-score" style="color:${searchTierScore >= 80 ? '#4CAF50' : searchTierScore >= 50 ? '#FFA726' : '#EF5350'}">${searchTierScore}%</span>
                    <span class="readiness-tier-chevron" id="readiness-search-chevron">▼</span>
                </div>
                <div class="readiness-tier-body" id="readiness-search-body">
                    ${searchChecks.map(renderCheck).join('')}
                </div>
            </div>`;
        } else {
            searchTierHtml = `<div class="readiness-tier readiness-tier-locked">
                <div class="readiness-tier-header readiness-locked-header">
                    <span class="readiness-tier-title">Search Performance</span>
                    <span class="readiness-tier-badge">LOCKED</span>
                </div>
                <div class="readiness-locked-msg">Complete your first search to unlock performance metrics.</div>
            </div>`;
        }

        container.innerHTML = `
            <div class="readiness-header">
                <div class="readiness-score" style="color:${scoreColor}">${pScore}%</div>
                <div>
                    <div class="readiness-title">${scoreTitle}</div>
                    <div class="readiness-sub">${passedChecks}/${totalChecks} profile checks passed</div>
                </div>
            </div>
            ${profileTierHtml}
            ${searchTierHtml}
        `;
    } catch (e) {
        console.error('Failed to load readiness:', e);
    }
}

function handleReadinessAction(action) {
    if (action.startsWith('scroll:')) {
        const elId = action.replace('scroll:', '');
        showView('profile'); // switch to profile tab first
        setTimeout(() => {
            const el = document.getElementById(elId);
            if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                el.classList.add('readiness-highlight');
                setTimeout(() => el.classList.remove('readiness-highlight'), 2000);
                if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') el.focus();
            }
        }, 300);
    } else if (action === 'run:analyzeProfile') {
        runSpringTrainingAnalysis(null);
    } else if (action === 'run:searchJobs') {
        showView('hunt');
    } else if (action.startsWith('view:')) {
        showView(action.replace('view:', ''));
    }
}

function toggleReadinessTier(tier) {
    const body = document.getElementById('readiness-' + tier + '-body');
    const chevron = document.getElementById('readiness-' + tier + '-chevron');
    if (body) body.classList.toggle('collapsed');
    if (chevron) chevron.classList.toggle('rotated');
}

function loadClubhouseCard(container, taEl, saveBtn) {
    if (taEl) taEl.style.display = 'none';
    if (saveBtn) saveBtn.style.display = 'none';

    // Update card labels
    const labelEl = document.querySelector('#dugout-reporter-corner .dugout-card-label');
    const titleEl = document.querySelector('#dugout-reporter-corner .dugout-card-title');
    const hintEl = document.getElementById('reporter-triple-a-hint');
    if (labelEl) labelEl.textContent = 'THE CLUBHOUSE';
    if (titleEl) titleEl.textContent = 'Home Base';
    if (hintEl) { hintEl.textContent = "You've made The Majors"; hintEl.classList.add('completed'); }

    const jobCount = (state.swipeStack || []).length;
    const shortlisted = (state.swipeStack || []).filter(j => j.status === 'shortlisted').length;

    // Market Pulse
    let marketHtml = '';
    if (jobCount > 0) {
        const topMatch = [...(state.swipeStack || [])].sort((a, b) => (b.match_score || 0) - (a.match_score || 0))[0];
        marketHtml = `<div class="clubhouse-section">
            <div class="clubhouse-section-label">MARKET PULSE</div>
            <div class="clubhouse-pulse-stat">${jobCount} jobs in pipeline</div>
            ${topMatch ? `<div class="clubhouse-pulse-top">Top match: <strong>${esc(topMatch.title)}</strong> at ${esc(topMatch.company)} <span class="clubhouse-score">${Math.round(topMatch.match_score || 0)}</span></div>` : ''}
        </div>`;
    } else {
        marketHtml = `<div class="clubhouse-section">
            <div class="clubhouse-section-label">MARKET PULSE</div>
            <div class="clubhouse-empty">Run your first search to see market data.</div>
        </div>`;
    }

    // Profile Tune-up
    const tuneUp = getProfileTuneUp();
    const tuneUpHtml = tuneUp ? `<div class="clubhouse-section">
        <div class="clubhouse-section-label">PROFILE TUNE-UP</div>
        <div class="clubhouse-tuneup">${tuneUp}</div>
    </div>` : '';

    container.innerHTML = `
        <div class="clubhouse-actions">
            <button class="btn btn-primary btn-sm" onclick="showView('hunt')">Search Jobs</button>
            <button class="btn btn-outline btn-sm" onclick="showView('prospects')">View Shortlist</button>
            ${jobCount > 0 ? `<span class="clubhouse-badge">${jobCount} jobs</span>` : ''}
            ${shortlisted > 0 ? `<span class="clubhouse-badge clubhouse-badge-green">${shortlisted} shortlisted</span>` : ''}
        </div>
        ${marketHtml}
        ${tuneUpHtml}
        <div class="clubhouse-footer">
            <a href="#" class="clubhouse-reanswer" onclick="resetReporterCorner();return false;">Update your answers</a>
        </div>
    `;
}

function getProfileTuneUp() {
    const p = state.profile;
    if (!p) return null;
    if (!p.cover_letter_template) return 'Add cover letter style notes to generate better cover letters.';
    if (!p.profile_analyzed) return 'Run AI analysis on your profile for smarter matching.';
    if ((p.skills || []).length < 5) return 'Add more skills — aim for at least 5 to improve match accuracy.';
    if (!p.phone) return 'Add your phone number — many applications require it.';
    if (state.swipeStack && state.swipeStack.length > 0) {
        const skills = (p.skills || []).map(s => s.toLowerCase());
        const jobTexts = state.swipeStack.slice(0, 20).map(j => ((j.requirements || '') + ' ' + (j.description || '')).toLowerCase()).join(' ');
        const keywords = ['python', 'javascript', 'aws', 'sql', 'react', 'docker', 'kubernetes', 'azure', 'agile', 'terraform', 'ci/cd'];
        for (const kw of keywords) {
            if (!skills.some(s => s.includes(kw)) && (jobTexts.match(new RegExp(kw, 'gi')) || []).length >= 3) {
                return `Consider adding "${kw}" to your skills — it appears frequently in your matched jobs.`;
            }
        }
    }
    return null;
}

function resetReporterCorner() {
    _reporterQuestionIndex = 0;
    REPORTER_QUESTIONS.forEach(rq => {
        if (rq.profileField && state.profile) state.profile[rq.profileField] = null;
    });
    if (state.profileId) {
        const clearBody = {};
        REPORTER_QUESTIONS.forEach(rq => { if (rq.profileField) clearBody[rq.profileField] = null; });
        api('/profiles/' + state.profileId, { method: 'PUT', body: clearBody }).catch(() => {});
    }
    loadReporterCorner();
    if (typeof loadSpringTraining === 'function') loadSpringTraining();
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
    // Spring Training gate: require Double-A
    const stLevel = getSpringTrainingLevel();
    if (stLevel.index < 2) {
        toast('Reach Double-A in Spring Training to unlock Deep Research', 'warning');
        return;
    }
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

    // Set button loading state
    const btn = document.querySelector('#subtab-overview .btn-primary');
    setButtonLoading(btn, true);

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
    } finally {
        setButtonLoading(btn, false);
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

            const hasNotes = job.user_notes && job.user_notes.trim();
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
                <div class="sl-notes-toggle" onclick="event.stopPropagation(); toggleJobNotes(${job.id})" style="
                    font-size:12px;color:var(--jb-text-dim);cursor:pointer;padding:4px 0;
                    display:flex;align-items:center;gap:4px;
                ">
                    <span id="job-notes-arrow-${job.id}" style="font-size:10px;transition:transform 0.2s;${hasNotes ? 'transform:rotate(90deg)' : ''}">${hasNotes ? '&#9654;' : '&#9654;'}</span>
                    ${hasNotes ? 'Notes' : 'Add Notes'}
                    ${hasNotes ? '<span style="width:6px;height:6px;border-radius:50%;background:var(--jb-bright);display:inline-block"></span>' : ''}
                </div>
                <div id="job-notes-section-${job.id}" style="display:${hasNotes ? 'block' : 'none'}">
                    <textarea id="job-notes-${job.id}" placeholder="Your notes about this job..." rows="2" onclick="event.stopPropagation()" style="
                        width:100%;padding:8px 10px;border-radius:6px;font-size:12px;
                        background:var(--jb-bg-tertiary);color:var(--jb-text-1);border:1px solid var(--jb-border);
                        resize:vertical;font-family:inherit;margin-top:4px;
                    ">${esc(job.user_notes || '')}</textarea>
                    <button class="btn btn-sm btn-secondary" onclick="event.stopPropagation(); saveJobNotes(${job.id})" style="margin-top:4px;font-size:11px">Save</button>
                </div>
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

function toggleJobNotes(jobId) {
    const section = document.getElementById(`job-notes-section-${jobId}`);
    const arrow = document.getElementById(`job-notes-arrow-${jobId}`);
    if (!section) return;
    if (section.style.display === 'none') {
        section.style.display = 'block';
        if (arrow) arrow.style.transform = 'rotate(90deg)';
    } else {
        section.style.display = 'none';
        if (arrow) arrow.style.transform = '';
    }
}

async function saveJobNotes(jobId) {
    const textarea = document.getElementById(`job-notes-${jobId}`);
    if (!textarea) return;
    try {
        await api(`/jobs/${jobId}/notes`, {
            method: 'PUT',
            body: { user_notes: textarea.value },
        });
        toast('Notes saved', 'success');
    } catch (e) {
        toast('Failed to save notes: ' + e.message, 'error');
    }
}

// ── Search Advisor ──────────────────────────────────────────────────────

async function loadSearchAdvisor() {
    if (!state.profileId) return;
    const content = document.getElementById('advisor-content');
    if (!content) return;

    // Set button loading state
    const btn = document.querySelector('#subtab-advisor .btn-primary');
    setButtonLoading(btn, true);

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
    } finally {
        setButtonLoading(btn, false);
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
window.updateFilter = updateFilter;
window.toggleSortDir = toggleSortDir;
window.toggleFiltersOpen = toggleFiltersOpen;
window.clearFilters = clearFilters;
window.showAppDetail = showAppDetail;
window.rescoreJobs = rescoreJobs;
window.reanalyzeProfile = reanalyzeProfile;
window.runSpringTrainingAnalysis = runSpringTrainingAnalysis;
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
window.updateAppPipelineStatus = updateAppPipelineStatus;
window.saveAppNotes = saveAppNotes;
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
window.toggleJobNotes = toggleJobNotes;
window.saveJobNotes = saveJobNotes;
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

// skipLogin removed for security

// ── Local Auth ──────────────────────────────────────────────────────────

let _localAuthMode = 'login'; // 'login' or 'register'

function toggleLocalAuthMode() {
    _localAuthMode = _localAuthMode === 'login' ? 'register' : 'login';
    const nameField = document.getElementById('local-auth-name');
    const submitBtn = document.getElementById('local-auth-submit');
    const toggleText = document.getElementById('local-auth-toggle-text');
    const toggleLink = document.getElementById('local-auth-toggle-link');
    const errorDiv = document.getElementById('local-auth-error');
    const passField = document.getElementById('local-auth-password');

    const confirmField = document.getElementById('local-auth-confirm-password');
    const dividerSpan = document.querySelector('.local-auth-divider span');

    if (_localAuthMode === 'register') {
        if (nameField) nameField.style.display = '';
        if (confirmField) confirmField.style.display = '';
        if (submitBtn) submitBtn.textContent = 'Create Account';
        if (toggleText) toggleText.textContent = 'Already have an account?';
        if (toggleLink) toggleLink.textContent = 'Sign in';
        if (passField) passField.autocomplete = 'new-password';
        if (dividerSpan) dividerSpan.textContent = 'or create account with email';
    } else {
        if (nameField) { nameField.style.display = 'none'; nameField.value = ''; }
        if (confirmField) { confirmField.style.display = 'none'; confirmField.value = ''; }
        if (submitBtn) submitBtn.textContent = 'Sign In';
        if (toggleText) toggleText.textContent = "Don't have an account?";
        if (toggleLink) toggleLink.textContent = 'Create one';
        if (passField) passField.autocomplete = 'current-password';
        if (dividerSpan) dividerSpan.textContent = 'or sign in with email';
    }
    if (errorDiv) errorDiv.style.display = 'none';
}

async function handleLocalAuth() {
    const email = document.getElementById('local-auth-email')?.value?.trim();
    const password = document.getElementById('local-auth-password')?.value;
    const name = document.getElementById('local-auth-name')?.value?.trim();
    const errorDiv = document.getElementById('local-auth-error');
    const submitBtn = document.getElementById('local-auth-submit');

    if (!email || !password || (_localAuthMode === 'register' && !name)) {
        if (errorDiv) { errorDiv.textContent = _localAuthMode === 'register' ? 'Name, email, and password are required' : 'Email and password are required'; errorDiv.style.display = ''; }
        return;
    }
    if (_localAuthMode === 'register') {
        const confirmPassword = document.getElementById('local-auth-confirm-password')?.value;
        if (password !== confirmPassword) {
            if (errorDiv) { errorDiv.textContent = 'Passwords do not match'; errorDiv.style.display = ''; }
            return;
        }
    }

    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = 'Loading...'; }
    if (errorDiv) errorDiv.style.display = 'none';

    try {
        const endpoint = _localAuthMode === 'register' ? '/auth/local/register' : '/auth/local/login';
        const body = _localAuthMode === 'register'
            ? { name, email, password }
            : { email, password };

        const resp = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!resp.ok) {
            const errText = await resp.text();
            let msg = 'Login failed';
            try { msg = JSON.parse(errText).detail || msg; } catch(e) {}
            throw new Error(msg);
        }

        const user = await resp.json();
        state.authUser = user;
        const overlay = document.getElementById('login-overlay');
        if (overlay) overlay.style.display = 'none';
        toast(`Welcome, ${user.name}!`, 'success');
        // Reload to set up profile
        location.reload();

    } catch(e) {
        if (errorDiv) { errorDiv.textContent = e.message; errorDiv.style.display = ''; }
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = _localAuthMode === 'register' ? 'Create Account' : 'Sign In';
        }
    }
}

// Enter key in password field triggers submit
document.addEventListener('DOMContentLoaded', () => {
    const passField = document.getElementById('local-auth-password');
    if (passField) passField.addEventListener('keydown', (e) => { if (e.key === 'Enter') handleLocalAuth(); });
});

async function logout() {
    localStorage.removeItem('jb_profile_id');
    localStorage.removeItem('jb_token');
    state.profileId = null;
    state.profile = null;
    state.authUser = null;
    // Clear server session
    try { await fetch('/auth/logout'); } catch(e) {}
    // Show login overlay instead of reloading (allows switching users)
    const overlay = document.getElementById('login-overlay');
    if (overlay) {
        overlay.style.display = 'flex';
        // Reset form state
        const errorDiv = document.getElementById('local-auth-error');
        if (errorDiv) errorDiv.style.display = 'none';
    } else {
        location.reload();
    }
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
    const avatarImg = document.getElementById('avatar-img');
    const nameEl = document.getElementById('dropdown-profile-name');

    // Use Google picture if available
    if (state.authUser?.picture_url) {
        if (el) el.style.display = 'none';
        if (!avatarImg) {
            // Create img element inside profile-avatar
            const container = document.getElementById('profile-avatar');
            if (container) {
                const img = document.createElement('img');
                img.id = 'avatar-img';
                img.src = state.authUser.picture_url;
                img.alt = state.authUser.name || '';
                img.referrerPolicy = 'no-referrer';
                img.style.cssText = 'width:100%;height:100%;border-radius:50%;object-fit:cover;';
                img.onerror = () => { img.remove(); if (el) { el.style.display = ''; el.textContent = getProfileInitials(state.profile); } };
                container.appendChild(img);
            }
        } else {
            avatarImg.src = state.authUser.picture_url;
        }
    } else {
        if (el) { el.style.display = ''; el.textContent = getProfileInitials(state.profile); }
        if (avatarImg) avatarImg.remove();
    }

    if (nameEl) nameEl.textContent = state.authUser?.name || state.profile?.name || 'No Profile';
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

// ── Button Loading Helper ────────────────────────────────────────────────

function setButtonLoading(btn, loading) {
    if (!btn) return;
    if (loading) {
        btn.dataset.originalText = btn.innerHTML;
        btn.innerHTML = '<span class="spinner-inline"></span> Running...';
        btn.disabled = true;
        btn.classList.add('btn-loading');
    } else {
        btn.innerHTML = btn.dataset.originalText || btn.innerHTML;
        btn.disabled = false;
        btn.classList.remove('btn-loading');
    }
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
    setButtonLoading(btn, true);
    const widget = btn.closest('.pregame-hub-widget');
    if (widget) widget.classList.add('pregame-widget-running');

    // Switch to tab and run, then restore button when done
    switchIntelTab(tabId, false);
    const runner = _getIntelRunner(tabId);
    if (runner) {
        runner().finally(() => {
            setButtonLoading(btn, false);
            if (widget) {
                widget.classList.remove('pregame-widget-running');
                widget.classList.add('pregame-widget-done');
            }
        });
    } else {
        setButtonLoading(btn, false);
    }
}

function _getIntelRunner(tab) {
    switch (tab) {
        case 'overview': return loadInsights;
        case 'advisor': return loadSearchAdvisor;
        case 'skills-audit': return runSkillsAuditIntel;
        case 'resume': return improveResumeIntel;
        case 'pregame': return loadPregameReport;
        default: return null;
    }
}

async function generateAll() {
    if (!state.profileId) return;
    // Spring Training gate: require Double-A
    const stLevel = getSpringTrainingLevel();
    if (stLevel.index < 2) {
        toast('Reach Double-A in Spring Training to unlock AI features', 'warning');
        showView('dugout');
        return;
    }
    const genAllBtn = document.getElementById('btn-generate-all');
    setButtonLoading(genAllBtn, true);

    // Find all hub card buttons
    const hubBtns = document.querySelectorAll('.pregame-hub-btn');
    const tabs = ['overview', 'advisor', 'skills-audit', 'resume'];

    // Mark all widgets as running
    hubBtns.forEach(btn => {
        setButtonLoading(btn, true);
        const widget = btn.closest('.pregame-hub-widget');
        if (widget) widget.classList.add('pregame-widget-running');
    });

    // Run all analyses in parallel (min 1s loading state so user sees feedback)
    const runners = tabs.map((tab, i) => {
        const runner = _getIntelRunner(tab);
        if (!runner) return Promise.resolve();
        const minDelay = new Promise(r => setTimeout(r, 1000));
        return Promise.allSettled([runner(), minDelay]).then(([result]) => {
            const btn = hubBtns[i];
            if (btn) {
                setButtonLoading(btn, false);
                const widget = btn.closest('.pregame-hub-widget');
                if (widget) {
                    widget.classList.remove('pregame-widget-running');
                    if (result.status === 'fulfilled') widget.classList.add('pregame-widget-done');
                }
            }
        });
    });

    await Promise.allSettled(runners);
    setButtonLoading(genAllBtn, false);
}

// ── Pregame Report ──────────────────────────────────────────────────────

async function loadPregameReport(triggerBtn) {
    if (!state.profileId) return;
    const area = document.getElementById('pregame-summary-area');
    if (!area) return;

    // Find the Generate Report button if not passed
    if (!triggerBtn) {
        triggerBtn = document.querySelector('#subtab-pregame .btn-primary');
    }
    setButtonLoading(triggerBtn, true);
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
    } finally {
        setButtonLoading(triggerBtn, false);
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

    // Set button loading state
    const btn = document.querySelector('#subtab-skills-audit .btn-primary');
    setButtonLoading(btn, true);

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
    } finally {
        setButtonLoading(btn, false);
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
    const btn = document.querySelector('#subtab-resume .btn-primary');
    setButtonLoading(btn, true);
    if (el) {
        el.innerHTML = '<div class="loading-shimmer" style="height:300px;border-radius:8px"></div>';
    }
    try {
        await _doImproveResume(el);
    } finally {
        setButtonLoading(btn, false);
    }
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

// Question types: 'single' (pick one), 'multi' (pick many), 'boolean' (yes/no), 'text' (free-form)
// level: which Spring Training level this question advances (single_a, double_a, triple_a)
const REPORTER_QUESTIONS = [
    // ── ROOKIE BALL: Essential profile fields ────────────────────────────────
    { q: "What job titles are you targeting?", type: "text", level: "single_a",
      profileField: "target_roles", fillsProfile: true,
      placeholder: "E.g., Software Developer, Project Manager, Data Analyst — separate with commas",
      saveAs: "csv_to_json" },
    { q: "Where do you want to work?", type: "text", level: "single_a",
      profileField: "target_locations", fillsProfile: true,
      placeholder: "E.g., Toronto, ON, Remote, Vancouver, BC — separate with commas",
      saveAs: "csv_to_json" },

    // ── SINGLE-A: Core job search parameters ───────────────────────────────
    { q: "What level of role are you targeting?", type: "single", level: "single_a",
      choices: ["Entry-level / Junior", "Mid-level / Intermediate", "Senior / Lead", "Manager / Senior Manager", "Director / VP", "Executive (C-Suite)"],
      profileField: "seniority_level", fillsProfile: true,
      mapTo: { "Entry-level / Junior": "entry", "Mid-level / Intermediate": "mid", "Senior / Lead": "senior", "Manager / Senior Manager": "manager", "Director / VP": "director", "Executive (C-Suite)": "c-suite" } },
    { q: "What's your target salary range?", type: "single", level: "single_a",
      choices: ["Under $50K", "$50K - $80K", "$80K - $120K", "$120K - $160K", "$160K - $200K", "$200K+"],
      profileField: "min_salary", fillsProfile: true,
      mapTo: { "Under $50K": "40000", "$50K - $80K": "50000", "$80K - $120K": "80000", "$120K - $160K": "120000", "$160K - $200K": "160000", "$200K+": "200000" } },
    { q: "How soon are you looking to start?", type: "single", level: "single_a",
      choices: ["Immediately", "Within 1 month", "2 - 3 months", "Just exploring"],
      profileField: "availability", fillsProfile: true },

    // ── DOUBLE-A: Work preferences & logistics ─────────────────────────────
    { q: "What's your preferred work setup?", type: "single", level: "double_a",
      choices: ["Fully remote", "Hybrid (2-3 days in office)", "On-site", "No preference"],
      profileField: "remote_preference", fillsProfile: true,
      mapTo: { "Fully remote": "remote", "Hybrid (2-3 days in office)": "hybrid", "On-site": "onsite", "No preference": "any" } },
    { q: "What type of employment are you looking for?", type: "multi", level: "double_a",
      choices: ["Full-time permanent", "Contract / Temp", "Part-time", "Freelance / Consulting", "Internship / Co-op"],
      profileField: "employment_type", fillsProfile: true },
    { q: "How far are you willing to commute?", type: "single", level: "double_a",
      choices: ["Under 30 min", "30 - 60 min", "Over 60 min if needed", "Remote only"],
      profileField: "commute_tolerance", fillsProfile: true },
    { q: "Would you relocate for the right opportunity?", type: "single", level: "double_a",
      choices: ["Yes, anywhere", "Yes, within my country", "Only for the right role", "No, staying put"],
      profileField: "relocation", fillsProfile: true },
    { q: "What size company appeals to you?", type: "multi", level: "double_a",
      choices: ["Startup (< 50)", "Small business (50 - 200)", "Mid-size (200 - 1000)", "Large enterprise (1000+)", "No preference"],
      profileField: "company_size", fillsProfile: true },

    // ── TRIPLE-A: Industry, priorities & deal-breakers ──────────────────────
    { q: "What industries interest you?", type: "multi", level: "triple_a",
      choices: ["Tech / SaaS", "Financial Services", "Government / Public Sector", "Healthcare", "Consulting", "Energy / Utilities", "Retail / E-commerce", "Manufacturing", "Education", "Non-profit"],
      profileField: "industry_preference", fillsProfile: true },
    { q: "What matters most in your next role?", type: "multi", level: "triple_a",
      choices: ["Compensation & benefits", "Career growth", "Mission & impact", "Work-life balance", "Team & culture", "Learning & development", "Job stability", "Flexibility"],
      profileField: "top_priority", fillsProfile: true },
    { q: "What are your deal-breakers?", type: "multi", level: "triple_a",
      choices: ["Micromanagement", "No remote option", "Below-market pay", "Toxic culture", "No growth path", "Excessive travel", "On-call / after-hours", "Outdated tech stack", "Long hiring process"],
      profileField: "deal_breakers", fillsProfile: true },

    // ── THE MAJORS: Fine-tuning your profile ────────────────────────────────
    { q: "What kind of team environment do you thrive in?", type: "single", level: "the_show",
      choices: ["Small, scrappy team", "Collaborative mid-size team", "Large structured organization", "Independent / solo contributor"],
      profileField: "ideal_culture" },
    { q: "What drives you at work?", type: "multi", level: "the_show",
      choices: ["Solving hard problems", "Building things", "Helping people", "Learning new skills", "Leading & mentoring", "Creative expression", "Financial reward", "Making an impact"],
      profileField: "values" },
    { q: "What are your biggest strengths?", type: "multi", level: "the_show",
      choices: ["Technical expertise", "Problem solving", "Communication", "Teamwork", "Leadership", "Attention to detail", "Adaptability", "Project management", "Creativity", "Data analysis"],
      profileField: "strengths" },
    { q: "How do you prefer to grow professionally?", type: "multi", level: "the_show",
      choices: ["Hands-on projects", "Mentorship", "Formal training / certifications", "Stretch assignments", "Conferences & networking", "Side projects / open source"],
      profileField: "growth_areas" },
    { q: "Do you have or need a security clearance?", type: "single", level: "the_show",
      choices: ["Yes, I have an active clearance", "No, but open to obtaining one", "No, and not interested", "Not applicable to my field"],
      profileField: "security_clearance" },
    { q: "How much travel works for you?", type: "single", level: "the_show",
      choices: ["No travel", "Occasional (up to 10%)", "Some (up to 25%)", "Frequent (up to 50%)", "Extensive (50%+)"],
      profileField: "travel_willingness" },
    { q: "Anything else we should know?", type: "text", level: "the_show",
      profileField: "additional_notes", placeholder: "E.g., visa requirements, notice period, target companies, certifications in progress, schedule constraints..." },
];

let _reporterQuestionIndex = 0;
let _reporterMultiSelections = new Set();  // tracks multi-select picks

const LEVEL_THEME = {
    single_a:  { icon: '🥉', label: 'Single-A',  color: '#CD7F32', bg: 'rgba(205,127,50,.08)' },
    double_a:  { icon: '🥈', label: 'Double-A',  color: '#C0C0C0', bg: 'rgba(192,192,192,.06)' },
    triple_a:  { icon: '🥇', label: 'Triple-A',  color: '#FFD700', bg: 'rgba(255,215,0,.06)' },
    the_show:  { icon: '🏟️', label: 'The Majors', color: '#4A90D9', bg: 'rgba(74,144,217,.06)' },
};

function loadReporterCorner() {
    const container = document.getElementById('reporter-question');
    const taEl = document.getElementById('reporter-textarea');
    const saveBtn = document.getElementById('reporter-save-btn');

    // Gate behind profile completion
    if (!state.profileId || !state.profile) {
        if (container) container.innerHTML = `<div style="font-size:13px;color:var(--jb-text-dim);padding:8px 0">Complete your profile to unlock the pre-game interview.</div>`;
        if (taEl) taEl.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'none';
        return;
    }

    // Count answered questions and update title
    const answeredCount = REPORTER_QUESTIONS.filter(rq => rq.profileField && state.profile?.[rq.profileField]).length;
    const titleEl = document.querySelector('#dugout-reporter-corner .dugout-card-title');
    if (titleEl) titleEl.textContent = `Pre-game Interview (${answeredCount}/${REPORTER_QUESTIONS.length})`;

    // Skip past already-answered questions to find the next unanswered one
    let attempts = 0;
    while (attempts < REPORTER_QUESTIONS.length) {
        const candidate = REPORTER_QUESTIONS[_reporterQuestionIndex % REPORTER_QUESTIONS.length];
        if (candidate.profileField && state.profile?.[candidate.profileField]) {
            _reporterQuestionIndex++;
            attempts++;
        } else {
            break;
        }
    }

    // All answered?
    if (attempts >= REPORTER_QUESTIONS.length) {
        loadClubhouseCard(container, taEl, saveBtn);
        return;
    }

    const q = REPORTER_QUESTIONS[_reporterQuestionIndex % REPORTER_QUESTIONS.length];
    const theme = LEVEL_THEME[q.level] || LEVEL_THEME.single_a;
    _reporterMultiSelections = new Set();

    // Update Triple-A progress hint
    const hintEl = document.getElementById('reporter-triple-a-hint');
    if (hintEl) {
        const { index } = getSpringTrainingLevel();
        if (index >= 4) {
            hintEl.textContent = '✓ The Majors — keep fine-tuning your profile';
            hintEl.classList.add('completed');
        } else {
            // Count how many questions at the CURRENT level are answered
            const currentLevelKey = SPRING_TRAINING_LEVELS[index]?.key || 'single_a';
            const levelQs = REPORTER_QUESTIONS.filter(rq => rq.level === currentLevelKey);
            const levelAnswered = levelQs.filter(rq => rq.profileField && state.profile?.[rq.profileField]).length;
            hintEl.textContent = `${theme.icon} ${theme.label} questions (${levelAnswered}/${levelQs.length} answered)`;
            hintEl.classList.remove('completed');
        }
    }

    // Build question UI based on type
    let inputHtml = '';
    const qType = q.type || 'single';

    if (qType === 'boolean') {
        inputHtml = `
            <div class="reporter-bool-row">
                <button class="reporter-bool-btn" data-val="${q.choices?.[0] || 'Yes'}" onclick="selectReporterBool(this)">${q.choices?.[0] || 'Yes'}</button>
                <button class="reporter-bool-btn" data-val="${q.choices?.[1] || 'No'}" onclick="selectReporterBool(this)">${q.choices?.[1] || 'No'}</button>
                ${(q.choices || []).slice(2).map(c => `<button class="reporter-bool-btn" data-val="${esc(c)}" onclick="selectReporterBool(this)">${esc(c)}</button>`).join('')}
            </div>`;
        if (taEl) taEl.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'none';

    } else if (qType === 'single') {
        inputHtml = `
            <div class="reporter-choices-grid">
                ${q.choices.map(c => `<button class="reporter-choice-btn" data-val="${esc(c)}" onclick="selectReporterSingle(this)">${esc(c)}</button>`).join('')}
            </div>`;
        if (taEl) taEl.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'none';

    } else if (qType === 'multi') {
        inputHtml = `
            <div class="reporter-choices-grid multi">
                ${q.choices.map(c => `<button class="reporter-multi-btn" data-val="${esc(c)}" onclick="toggleReporterMulti(this)">${esc(c)}</button>`).join('')}
            </div>
            <button class="reporter-confirm-btn" id="reporter-multi-confirm" onclick="confirmReporterMulti()">Confirm Selection</button>`;
        if (taEl) taEl.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'none';

    } else { // text
        inputHtml = '';
        if (taEl) { taEl.style.display = ''; taEl.value = ''; taEl.placeholder = q.placeholder || 'Type your answer...'; }
        if (saveBtn) saveBtn.style.display = '';
    }

    if (container) {
        // Question number within its level
        const levelQs = REPORTER_QUESTIONS.filter(rq => rq.level === q.level);
        const qNumInLevel = levelQs.indexOf(q) + 1;

        // Build "Previous" link if there is a previous answered question to go back to
        let prevHtml = '';
        if (_reporterQuestionIndex > 0) {
            prevHtml = `<a href="#" class="reporter-prev-link" onclick="reporterGoBack();return false;" style="font-size:12px;color:var(--jb-text-dim,#8A9BB5);text-decoration:none;display:inline-block;margin-bottom:6px;cursor:pointer">&#8592; Previous</a>`;
        }

        container.innerHTML = `
            ${prevHtml}
            <div class="reporter-level-badge" style="background:${theme.bg};border-color:${theme.color}">
                ${theme.icon} ${theme.label} — Question ${qNumInLevel} of ${levelQs.length}
            </div>
            <div class="reporter-q-text">${esc(q.q)}</div>
            ${inputHtml}
        `;
    }
}

function reporterGoBack() {
    if (_reporterQuestionIndex <= 0) return;
    // Walk backwards to find the previous answered question
    let idx = _reporterQuestionIndex - 1;
    while (idx > 0) {
        const candidate = REPORTER_QUESTIONS[idx % REPORTER_QUESTIONS.length];
        if (candidate.profileField && state.profile?.[candidate.profileField]) break;
        idx--;
    }
    _reporterQuestionIndex = idx;
    loadReporterCorner();
}

// ── Reporter answer handlers ──

function selectReporterBool(btn) {
    // Highlight selected, save immediately
    btn.closest('.reporter-bool-row').querySelectorAll('.reporter-bool-btn').forEach(b => b.classList.remove('selected'));
    btn.classList.add('selected');
    _saveReporterValue(btn.dataset.val);
}

function selectReporterSingle(btn) {
    btn.closest('.reporter-choices-grid').querySelectorAll('.reporter-choice-btn').forEach(b => b.classList.remove('selected'));
    btn.classList.add('selected');
    _saveReporterValue(btn.dataset.val);
}

function toggleReporterMulti(btn) {
    const val = btn.dataset.val;
    if (_reporterMultiSelections.has(val)) {
        _reporterMultiSelections.delete(val);
        btn.classList.remove('selected');
    } else {
        _reporterMultiSelections.add(val);
        btn.classList.add('selected');
    }
    // Update confirm button state
    const confirmBtn = document.getElementById('reporter-multi-confirm');
    if (confirmBtn) {
        confirmBtn.disabled = _reporterMultiSelections.size === 0;
        confirmBtn.textContent = _reporterMultiSelections.size > 0
            ? `Confirm ${_reporterMultiSelections.size} selected`
            : 'Confirm Selection';
    }
}

function confirmReporterMulti() {
    if (_reporterMultiSelections.size === 0) { toast('Select at least one option', 'warning'); return; }
    _saveReporterValue([..._reporterMultiSelections].join(', '));
}

async function _saveReporterValue(rawAnswer) {
    const q = REPORTER_QUESTIONS[_reporterQuestionIndex % REPORTER_QUESTIONS.length];
    let mappedValue = q.mapTo ? (q.mapTo[rawAnswer] || rawAnswer) : rawAnswer;

    // Convert comma-separated text to JSON array for list fields
    if (q.saveAs === 'csv_to_json' && typeof mappedValue === 'string') {
        mappedValue = mappedValue.split(',').map(s => s.trim()).filter(Boolean);
    }

    try {
        if (state.profileId && q.profileField) {
            const updateBody = {};
            updateBody[q.profileField] = mappedValue;
            try {
                await api(`/profiles/${state.profileId}`, { method: 'PUT', body: updateBody });
            } catch(e) {
                // Direct PUT failed (field may not exist as a DB column).
                // Try the advisor-suggestion fallback, but don't block advancement if it also fails.
                try {
                    await api(`/profiles/${state.profileId}/apply-advisor-suggestion`, {
                        method: 'POST',
                        body: { field: q.profileField, value: mappedValue }
                    });
                } catch(e2) {
                    console.warn(`Reporter Corner: could not persist "${q.profileField}" to server, stored locally only.`, e2);
                }
            }
        }

        // Always store locally so the question counts as answered
        if (q.profileField && state.profile) {
            state.profile[q.profileField] = mappedValue;
        }

        // Brief "saved" flash then advance
        toast('Saved!', 'success');
        _reporterQuestionIndex++;
        // Small delay so user sees the selection highlight before it moves on
        setTimeout(() => {
            loadReporterCorner();
            if (typeof loadSpringTraining === 'function') loadSpringTraining();
        }, 400);
    } catch (e) {
        toast('Failed to save answer', 'error');
    }
}

async function saveReporterAnswer() {
    // Used for free-text questions only
    const taEl = document.getElementById('reporter-textarea');
    const answer = taEl ? taEl.value.trim() : '';
    const q = REPORTER_QUESTIONS[_reporterQuestionIndex % REPORTER_QUESTIONS.length];
    // For csv_to_json fields, require at least one entry
    if (q.saveAs === 'csv_to_json' && !answer) {
        toast('Please enter at least one item', 'warning');
        return;
    }
    await _saveReporterValue(answer || 'Skipped');
}

// ── Coach's Note ────────────────────────────────────────────────────────

function loadCoachNote() {
    const el = document.getElementById('dugout-coach-note');
    if (!el) return;

    const p = state.profile;
    let message = '';

    if (!p) {
        message = 'Create a profile to get started with your job search journey.';
    } else if (!p.has_resume_text && !p.resume_uploaded) {
        message = 'Upload your resume to unlock AI-powered job matching and scoring.';
    } else {
        // Calculate profile completeness
        const fields = [p.name, p.email, p.location, p.seniority_level, p.min_salary,
            p.remote_preference, p.industry_preference];
        const targetFields = [(p.target_roles || []).length > 0, (p.target_locations || []).length > 0];
        const filledCount = [...fields, ...targetFields].filter(Boolean).length;
        const totalFields = fields.length + targetFields.length;
        const completeness = Math.round((filledCount / totalFields) * 100);

        if (completeness < 50) {
            message = 'Answering Reporter Corner questions will sharpen your job matches. Your profile is ' + completeness + '% complete.';
        } else if (!state.jobs || state.jobs.length === 0) {
            message = 'Your profile is looking good! Hit Search Jobs in Scouting to find opportunities.';
        } else {
            const shortlisted = (state.jobs || []).filter(j => j.status === 'shortlisted').length;
            const applied = (state.jobs || []).filter(j => j.status === 'liked' || j.status === 'applied').length;
            if (shortlisted === 0 && applied === 0) {
                message = 'Review your search results and shortlist jobs that interest you.';
            } else if (shortlisted > 0 && applied === 0) {
                message = 'You have ' + shortlisted + ' shortlisted job' + (shortlisted !== 1 ? 's' : '') + '. Ready to start applying?';
            } else if (applied > 0) {
                message = 'You\'ve applied to ' + applied + ' job' + (applied !== 1 ? 's' : '') + '. Keep the momentum going!';
            } else {
                message = 'Keep building your profile and searching for opportunities.';
            }
        }
    }

    el.style.display = 'block';
    el.innerHTML = `
        <div style="
            display:flex;align-items:flex-start;gap:12px;padding:16px;
            background:linear-gradient(135deg, #1a2744 0%, #1d2d5c 100%);
            border-radius:10px;border:1px solid rgba(74,144,217,0.2);
            margin-bottom:8px;
        ">
            <div style="font-size:28px;flex-shrink:0;line-height:1">&#x1F9E2;</div>
            <div>
                <div style="font-size:11px;font-weight:700;color:#4A90D9;letter-spacing:1px;margin-bottom:4px">COACH'S NOTE</div>
                <div style="font-size:14px;color:var(--jb-text-1);line-height:1.5">${esc(message)}</div>
            </div>
        </div>`;
}

// ── Dugout Helpers (called from showView) ───────────────────────────────

async function loadDugoutReadiness() {
    // Now handled by loadSpringTraining / updateDugoutReadinessBadge
    if (!state.profileId) return;
    // Spring Training already updates the readiness badge, but
    // keep backward compat: if Spring Training is complete, show the real readiness score
    const stLevel = getSpringTrainingLevel();
    if (stLevel.level === 'the_show') {
        try {
            const r = await api(`/profiles/${state.profileId}/apply-readiness`);
            const el = document.getElementById('dugout-readiness');
            if (!el) return;
            const rScore = r.profile_score != null ? r.profile_score : r.score;
            const emoji = rScore >= 70 ? '🟢' : rScore >= 40 ? '🟡' : '🔴';
            el.innerHTML = `<div style="display:flex;align-items:center;gap:12px;padding:12px;background:var(--jb-bg-secondary);border-radius:8px;border:1px solid var(--jb-border)">
                <span style="font-size:24px">${emoji}</span>
                <div><div style="font-size:16px;font-weight:600">${rScore}% Ready</div><div style="font-size:12px;color:var(--jb-text-2)">${r.passed}/${r.total} checks passed &mdash; Spring Training Complete!</div></div>
            </div>`;
        } catch {}
    }
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

// ── Spring Training System ───────────────────────────────────────────────

const SPRING_TRAINING_LEVELS = [
    { key: 'rookie', name: 'Rookie Ball', icon: '⚾', hint: 'Upload your resume' },
    { key: 'single_a', name: 'Single-A', icon: '🥉', hint: 'Fill in name, email, location, target roles & locations' },
    { key: 'double_a', name: 'Double-A', icon: '🥈', hint: 'AI profile analysis, seniority level & min salary set' },
    { key: 'triple_a', name: 'Triple-A', icon: '🥇', hint: 'Answer Reporter Corner: remote pref, industry & deal-breakers' },
    { key: 'the_show', name: 'The Majors', icon: '🏟️', hint: 'Ready to search! All critical fields filled' },
];

function getSpringTrainingLevel() {
    if (!state.profile) return { level: 'rookie', index: 0, checks: {} };
    const p = state.profile;

    const hasResume = !!(p.resume_uploaded || p.has_resume_text);
    const hasBasicFields = !!(p.name && p.email && p.location
        && (p.target_roles || []).length > 0
        && (p.target_locations || []).length > 0);
    const hasDeepAnalysis = !!(p.seniority_level && (p.min_salary || p.availability));
    const hasReporterAnswers = !!(p.remote_preference
        && p.industry_preference
        && p.deal_breakers);
    // The Majors = everything above is done
    const allComplete = hasResume && hasBasicFields && hasDeepAnalysis && hasReporterAnswers;

    const checks = { hasResume, hasBasicFields, hasDeepAnalysis, hasReporterAnswers, allComplete };

    let level = 'rookie';
    let index = 0;
    if (hasResume) { level = 'single_a'; index = 1; }
    if (hasResume && hasBasicFields) { level = 'double_a'; index = 2; }
    if (hasResume && hasBasicFields && hasDeepAnalysis) { level = 'triple_a'; index = 3; }
    if (allComplete) { level = 'the_show'; index = 4; }

    return { level, index, checks };
}

function loadSpringTraining() {
    const el = document.getElementById('spring-training-levels');
    const card = document.getElementById('dugout-spring-training');
    const titleEl = document.getElementById('spring-training-title');

    if (!state.profileId || !state.profile) {
        if (el) el.innerHTML = `
            <div style="padding:12px 0;text-align:center">
                <div style="font-size:13px;color:var(--jb-text-dim);margin-bottom:12px">Create a profile to begin Spring Training.</div>
                <button class="btn btn-primary btn-sm" onclick="showView('profile')">Get Started</button>
            </div>`;
        const pctEl = document.getElementById('spring-training-pct');
        if (pctEl) pctEl.innerHTML = '<span class="st-level-badge level-rookie">Rookie Ball</span>';
        return;
    }

    const { level, index, checks } = getSpringTrainingLevel();
    const pct = Math.round((index / (SPRING_TRAINING_LEVELS.length - 1)) * 100);

    // Update progress bar
    const bar = document.getElementById('spring-training-progress-bar');
    if (bar) bar.style.width = pct + '%';

    // Update level badge
    const pctEl = document.getElementById('spring-training-pct');
    if (pctEl) {
        const lvl = SPRING_TRAINING_LEVELS[index];
        pctEl.innerHTML = `<span class="st-level-badge level-${level}">${lvl.icon} ${lvl.name}</span> <span style="color:var(--jb-text-muted)">${pct}% complete</span>`;
    }

    // Update title
    if (titleEl) {
        titleEl.textContent = level === 'the_show' ? 'The Climb — Complete!' : 'The Climb';
    }

    // Complete state
    if (card) {
        card.classList.toggle('spring-complete', level === 'the_show');
    }

    // Build level rows
    const checkMap = [checks.hasResume, checks.hasBasicFields, checks.hasDeepAnalysis, checks.hasReporterAnswers, checks.allComplete];
    const actions = [
        `<button class="btn btn-primary btn-sm" onclick="showView('profile')">Upload Resume</button>`,
        `<button class="btn btn-primary btn-sm" onclick="showView('profile')">Edit Profile</button>`,
        `<button class="btn btn-primary btn-sm" onclick="runSpringTrainingAnalysis(this)">Analyze Profile</button>`,
        `<button class="btn btn-primary btn-sm" onclick="showView('dugout');setTimeout(()=>document.getElementById('dugout-reporter-corner')?.scrollIntoView({behavior:'smooth',block:'center'}),200)">Answer Questions</button>`,
        null,
    ];

    if (el) {
        el.innerHTML = SPRING_TRAINING_LEVELS.map((lvl, i) => {
            const done = i < index || (i === index && level === 'the_show');
            const current = i === index && level !== 'the_show';
            const locked = i > index;
            return `
                <div class="st-level-row ${done ? 'st-done' : ''} ${current ? 'st-current' : ''}">
                    <div class="st-level-icon">${done ? '✓' : lvl.icon}</div>
                    <div class="st-level-info">
                        <div class="st-level-name">${esc(lvl.name)}</div>
                        ${current ? `<div class="st-level-hint">${esc(lvl.hint)}</div>` : ''}
                        ${current && actions[i] ? `<div class="st-level-action">${actions[i]}</div>` : ''}
                        ${locked ? `<div class="st-level-hint" style="font-style:italic">${esc(lvl.hint)}</div>` : ''}
                    </div>
                </div>
            `;
        }).join('');
    }

    // Update readiness badge
    updateDugoutReadinessBadge(level, index);

    // Apply feature gating
    applyFeatureGating(level, index);
}

function updateDugoutReadinessBadge(level, index) {
    const el = document.getElementById('dugout-readiness');
    if (!el) return;
    const pct = Math.round((index / (SPRING_TRAINING_LEVELS.length - 1)) * 100);
    const lvl = SPRING_TRAINING_LEVELS[index];
    const emoji = level === 'the_show' ? '🟢' : index >= 2 ? '🟡' : '🔴';
    el.innerHTML = `<div style="display:flex;align-items:center;gap:12px;padding:12px;background:var(--jb-bg-secondary);border-radius:8px;border:1px solid var(--jb-border)">
        <span style="font-size:24px">${emoji}</span>
        <div>
            <div style="font-size:16px;font-weight:600">${pct}% Ready</div>
            <div style="font-size:12px;color:var(--jb-text-2)">Spring Training: ${lvl.name}</div>
        </div>
    </div>`;
}

function _addGatedClickHandler(btn, message) {
    // Remove any previous gated handler to avoid duplicates
    if (btn._gatedClickHandler) {
        btn.removeEventListener('click', btn._gatedClickHandler, true);
        btn._gatedClickHandler = null;
    }
    const handler = (e) => {
        if (btn._springGated) {
            e.stopImmediatePropagation();
            e.preventDefault();
            toast(message, 'warning');
        }
    };
    btn._gatedClickHandler = handler;
    btn.addEventListener('click', handler, true);
}

function applyFeatureGating(level, index) {
    const levelName = SPRING_TRAINING_LEVELS[index]?.name || 'Rookie Ball';

    // Gate search buttons: require "the_show" level
    const searchBtns = document.querySelectorAll('#btn-search-jobs, #btn-search-more, #btn-search-empty');
    searchBtns.forEach(btn => {
        if (index < 4) {
            btn.classList.add('btn-gated');
            btn.title = 'Complete The Climb to unlock search';
            btn._springGated = true;
            _addGatedClickHandler(btn, 'Complete The Climb to unlock this feature. Current level: ' + levelName);
        } else {
            btn.classList.remove('btn-gated');
            btn.title = '';
            btn._springGated = false;
        }
    });

    // Gate Bullpen AI features: require "double_a" or higher
    const genAllBtn = document.getElementById('btn-generate-all');
    if (genAllBtn) {
        if (index < 2) {
            genAllBtn.classList.add('btn-gated');
            genAllBtn.title = 'Reach Double-A in Spring Training to unlock';
            genAllBtn._springGated = true;
            _addGatedClickHandler(genAllBtn, 'Complete The Climb to unlock this feature. Current level: ' + levelName);
        } else {
            genAllBtn.classList.remove('btn-gated');
            genAllBtn.title = '';
            genAllBtn._springGated = false;
        }
    }

    // Gate deep research shortlist button
    const deepResBtn = document.getElementById('btn-deep-research-shortlist');
    if (deepResBtn) {
        if (index < 2) {
            deepResBtn.classList.add('btn-gated');
            deepResBtn.title = 'Reach Double-A in Spring Training to unlock';
            deepResBtn._springGated = true;
            _addGatedClickHandler(deepResBtn, 'Complete The Climb to unlock this feature. Current level: ' + levelName);
        } else {
            deepResBtn.classList.remove('btn-gated');
            deepResBtn.title = '';
            deepResBtn._springGated = false;
        }
    }
}

// Alias for backward compat
async function loadScoutingReport() {
    loadSpringTraining();
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
// window.skipLogin removed for security
window.toggleLocalAuthMode = toggleLocalAuthMode;
window.handleLocalAuth = handleLocalAuth;
window.logout = logout;
window.toggleProfileDropdown = toggleProfileDropdown;
window.closeProfileDropdown = closeProfileDropdown;
window.switchProfile = switchProfile;
window.createNewProfile = createNewProfile;
window.toggleImportSection = toggleImportSection;
window.switchPipelineTab = switchPipelineTab;
window.setButtonLoading = setButtonLoading;
window.switchIntelTab = switchIntelTab;
window.runFromHub = runFromHub;
window.generateAll = generateAll;
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
window.reporterGoBack = reporterGoBack;
window.getSpringTrainingLevel = getSpringTrainingLevel;
window.loadPromptLab = loadPromptLab;
window.loadModelConfig = loadModelConfig;
window.togglePromptEdit = togglePromptEdit;
window.savePrompt = savePrompt;
window.resetPrompt = resetPrompt;
window.enhancePrompt = enhancePrompt;
window.applyEnhancedPrompt = applyEnhancedPrompt;
window.saveModelOverride = saveModelOverride;
window.clearModelOverride = clearModelOverride;
window.loadSpringTraining = loadSpringTraining;
window.handleReadinessAction = handleReadinessAction;
window.toggleReadinessTier = toggleReadinessTier;
window.resetReporterCorner = resetReporterCorner;
