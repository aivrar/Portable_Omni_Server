/* ==========================================================================
   Tab: Setup — Installation status for ComfyUI + Omni models
   ========================================================================== */

var TabSetup = {
    _pollTimer: null,
    // These worker families use per-asset install routes on their own tabs —
    // not /api/setup/install/{model} (which correctly returns 400).
    _DEDICATED_IDS: ['audio_lab', 'ace_step'],

    init: function() { this.render(); },

    render: function() {
        var el = document.getElementById('tab-setup');
        if (!el) return;
        var drafts = {};
        el.querySelectorAll('input[id],textarea[id],select[id]').forEach(function(control) {
            drafts[control.id] = {value: control.value, checked: control.checked};
        });
        var opened = Array.from(el.querySelectorAll('details')).map(function(item) { return item.open; });
        var oldResults = el.querySelector('#lora-search-results');
        var resultsHtml = oldResults ? oldResults.innerHTML : '';
        var status = App.state.setupStatus || {};
        var models = App.state.models || {};
        var E = App.esc;
        var hfSt = status.huggingface || {};
        var diskSt = status.disk || {};
        var modelIds = Object.keys(models).filter(function(id) {
            return TabSetup._DEDICATED_IDS.indexOf(id) === -1;
        });
        var readyIds = modelIds.filter(function(id) { return !!(status[id] || {}).installed; });
        var runtimeCount = (App.state.workers || []).length + (App.state.comfyInstances || []).length;
        var modelsReady = readyIds.length > 0;
        var runtimeReady = runtimeCount > 0;
        var missingDefaults = Number(diskSt.missing_defaults_required_gb || 0);

        var nextTitle = 'Choose what you want to create';
        var nextText = 'Your local workspace is ready. Pick a destination below; Omni Studio will show the exact runtime requirement before anything heavy starts.';
        var nextAction = '<button class="btn btn-primary" type="button" onclick="App.activateTab(\'chat\')">Open Chat</button>';
        if (!modelsReady) {
            nextTitle = 'Install your first model';
            nextText = 'Start in the Model library. Choose one model that fits your hardware; you do not need to install everything.';
            nextAction = '<button class="btn btn-primary" type="button" onclick="App.activateTab(\'models\')">Browse models</button>';
        } else if (!runtimeReady) {
            nextTitle = 'Start a runtime when you are ready';
            nextText = readyIds.length + ' model' + (readyIds.length === 1 ? ' is' : 's are') + ' installed. Loading is explicit, so the app stays light until you choose a model and device.';
            nextAction = '<button class="btn btn-primary" type="button" onclick="App.activateTab(\'server\')">Open Runtime</button>';
        }

        var hfBadge = !hfSt.token_saved
            ? '<span class="badge badge-orange">Not connected</span>'
            : hfSt.token_valid === true
                ? '<span class="badge badge-green">Connected</span>'
                : hfSt.token_valid === false
                    ? '<span class="badge badge-red">Needs attention</span>'
                    : '<span class="badge badge-orange">Not verified</span>';
        var hfDetail = !hfSt.token_saved
            ? 'Only needed for gated HuggingFace downloads.'
            : hfSt.token_valid === true
                ? 'Signed in as ' + E(hfSt.token_user || 'HuggingFace user') + '.'
                : E(hfSt.token_error || 'Replace or verify the saved token before gated downloads.');

        var html = '';
        html += '<div class="page-heading"><div class="page-heading-copy">';
        html += '<div class="page-eyebrow">' + (modelsReady ? 'Welcome back' : 'Welcome to Omni Studio') + '</div><h1>Your local creative workspace</h1>';
        html += '<p>Start with a task, not a wall of settings. Models load only when you ask for them, and every result comes back to one Media library.</p>';
        html += '</div><div class="page-heading-actions">';
        html += '<button class="btn" type="button" onclick="App.activateTab(\'models\')">Model library</button>';
        html += '<button class="btn" type="button" onclick="App.activateTab(\'server\')">Runtime status</button>';
        html += '</div></div>';

        html += '<div class="home-hero">';
        html += '<section class="next-step-card"><div class="next-step-label">Recommended next step</div>';
        html += '<h2>' + E(nextTitle) + '</h2><p>' + E(nextText) + '</p><div class="next-step-actions">' + nextAction;
        if (modelsReady) html += '<button class="btn" type="button" onclick="App.activateTab(\'media\')">View recent media</button>';
        html += '</div></section>';
        html += '<section class="card home-progress" aria-label="Getting started progress">';
        html += '<div class="progress-step ' + (modelsReady ? 'done' : 'active') + '"><span class="progress-step-marker">' + (modelsReady ? '✓' : '1') + '</span><span><strong>Install one model</strong><small>' + readyIds.length + ' model families ready</small></span></div>';
        html += '<div class="progress-step ' + (runtimeReady ? 'done' : (modelsReady ? 'active' : '')) + '"><span class="progress-step-marker">' + (runtimeReady ? '✓' : '2') + '</span><span><strong>Start a runtime</strong><small>' + (runtimeReady ? runtimeCount + ' active' : 'Nothing heavy is loaded') + '</small></span></div>';
        html += '<div class="progress-step ' + (runtimeReady ? 'active' : '') + '"><span class="progress-step-marker">3</span><span><strong>Create and review</strong><small>Results appear in Media</small></span></div>';
        html += '</section></div>';

        html += '<section class="home-section"><div class="home-section-heading"><div><h2>What do you want to make?</h2><p>Each workspace explains its own model and runtime requirements.</p></div></div>';
        html += '<div class="destination-grid">';
        html += '<button class="destination-card" type="button" onclick="App.activateTab(\'chat\')"><span class="destination-card-icon">↗</span><strong>Chat with media</strong><small>Text, images, audio, and video in one conversation.</small></button>';
        html += '<button class="destination-card" type="button" onclick="App.activateTab(\'workflows\')"><span class="destination-card-icon">◇</span><strong>Images &amp; workflows</strong><small>Run saved ComfyUI workflows without leaving the app.</small></button>';
        html += '<button class="destination-card" type="button" onclick="App.activateTab(\'tts\')"><span class="destination-card-icon">◉</span><strong>Voice &amp; TTS</strong><small>Dedicated speech workspace ready for the upcoming engine library.</small><span class="nav-badge">Soon</span></button>';
        html += '<button class="destination-card" type="button" onclick="App.activateTab(\'ace-step\')"><span class="destination-card-icon">♫</span><strong>Music &amp; audio</strong><small>Generate songs, sound, and scored audio candidates.</small></button>';
        html += '</div></section>';

        html += '<section class="home-section"><div class="home-section-heading"><div><h2>System overview</h2><p>Only the essentials; detailed controls live in their own destinations.</p></div></div>';
        html += '<div class="readiness-grid">';
        html += '<div class="readiness-item"><div class="readiness-item-label">Models</div><strong>' + readyIds.length + ' ready</strong><small>' + modelIds.length + ' families available</small></div>';
        html += '<div class="readiness-item"><div class="readiness-item-label">Runtime</div><strong>' + (runtimeReady ? runtimeCount + ' active' : 'Idle') + '</strong><small>' + (runtimeReady ? 'Open Runtime for details' : 'No model memory in use') + '</small></div>';
        html += '<div class="readiness-item"><div class="readiness-item-label">ComfyUI</div><strong>' + ((status.comfyui || {}).installed ? 'Installed' : 'Not installed') + '</strong><small>' + ((status.comfyui || {}).manager_installed ? 'Manager available' : 'Workflow engine') + '</small></div>';
        html += '<div class="readiness-item"><div class="readiness-item-label">Storage</div><strong>' + (diskSt.available ? E(diskSt.free_gb) + ' GB free' : 'Checking…') + '</strong><small>Inside the distro</small></div>';
        html += '</div></section>';

        html += '<details class="advanced-panel"><summary>Downloads, access token, and advanced setup</summary><div class="advanced-panel-body">';
        html += '<div class="settings-grid">';
        html += '<section class="settings-block"><h3>Default downloads</h3><p>Install only the missing baseline assets. For individual models and variants, use the Model library.</p>';
        html += '<button class="btn btn-sm' + (missingDefaults > 0 ? ' btn-primary' : '') + '" type="button" onclick="TabSetup.installMissingDefaults(this)"' + (missingDefaults > 0 ? '' : ' disabled') + '>' + (missingDefaults > 0 ? 'Install missing defaults' : 'Defaults are installed') + '</button>';
        if (diskSt.available) html += ' <span class="badge badge-gray">' + E(missingDefaults) + ' GB missing</span>';
        html += '</section>';
        html += '<section class="settings-block"><h3>HuggingFace access ' + hfBadge + '</h3><p>' + hfDetail + '</p>';
        html += '<form onsubmit="event.preventDefault();TabSetup.saveToken()"><label class="sr-only" for="hf-token-input">HuggingFace access token</label><div style="display:flex;gap:8px"><input type="password" id="hf-token-input" aria-label="HuggingFace access token" placeholder="hf_…" style="flex:1" autocomplete="off"><button class="btn btn-sm" type="submit">Save token</button></div></form></section>';
        html += '</div>';
        html += '<section class="settings-block mt-16"><h3>Optional LoRA adapters</h3><p>Add lightweight style or behavior adapters. Most users can skip this until a workflow specifically asks for one.</p>';
        html += '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px"><label class="sr-only" for="lora-search-input">Search HuggingFace for LoRA adapters</label><input type="text" id="lora-search-input" aria-label="Search HuggingFace for LoRA adapters" placeholder="Search HuggingFace" style="flex:1;min-width:220px" onkeypress="if(event.key===\'Enter\')TabSetup.searchLoras()"><button class="btn btn-sm" type="button" onclick="TabSetup.searchLoras()">Search</button></div>';
        html += '<div id="lora-search-results"></div>';
        html += '<details class="advanced-panel"><summary>Install from a repository ID</summary><div class="advanced-panel-body"><div style="display:flex;gap:8px;flex-wrap:wrap"><label class="sr-only" for="lora-repo-input">LoRA repository ID</label><input type="text" id="lora-repo-input" aria-label="LoRA repository ID" placeholder="organization/repository" style="flex:1;min-width:220px"><label class="sr-only" for="lora-name-input">Optional local LoRA name</label><input type="text" id="lora-name-input" aria-label="Optional local LoRA name" placeholder="Local name (optional)" style="width:190px"><button class="btn btn-sm" type="button" onclick="TabSetup.installLoraByRepo()">Download</button></div></div></details>';
        html += '<div class="section-title">Installed adapters</div><div id="lora-installed-list"><div class="empty-state">No LoRA adapters installed</div></div></section>';
        html += '<section class="settings-block mt-16"><h3>Install activity</h3><div id="install-jobs-list"><div class="empty-state">No active downloads</div></div></section>';
        html += '</div></details>';

        el.innerHTML = html;
        Object.keys(drafts).forEach(function(id) {
            var control = document.getElementById(id);
            if (control) { control.value = drafts[id].value; control.checked = drafts[id].checked; }
        });
        el.querySelectorAll('details').forEach(function(item, index) { item.open = !!opened[index]; });
        el.querySelector('#lora-search-results').innerHTML = resultsHtml;
        this.renderInstallJobs();
        this.renderInstalledLoras();
    },

    renderInstallJobs: function() {
        var panel = document.getElementById('install-jobs-list');
        if (!panel) return;
        var jobs = App.state.installJobs || [];
        var E = App.esc;
        if (jobs.length === 0) {
            panel.innerHTML = '<div class="empty-state">No install jobs yet. Downloads you start above appear here with live status.</div>';
            return;
        }
        var html = '<table><thead><tr><th>Job</th><th>Kind</th><th>Target</th><th>Status</th><th></th></tr></thead><tbody>';
        for (var i = 0; i < jobs.length; i++) {
            var j = jobs[i];
            var target = j.variant_display || j.model || j.name || j.repo || '';
            var canCancel = j.status === 'running' || j.status === 'cancelling';
            var helper = this._jobHelperHtml(j, true);
            var progress = this._jobProgressHtml(j);
            var warning = this._jobWarningHtml(j);
            var attempt = this._jobAttemptHtml(j);
            html += '<tr>';
            html += '<td style="font-family:var(--font-mono);font-size:12px">' + E(j.job_id) + '</td>';
            html += '<td>' + E(j.kind || '') + '</td>';
            html += '<td>' + E(target) + attempt + warning + (helper ? '<div style="margin-top:6px">' + helper + '</div>' : '') + '</td>';
            html += '<td><span class="badge badge-' + (j.status === 'completed' ? 'green' : (j.status === 'failed' ? 'red' : (j.status === 'cancelled' ? 'gray' : 'orange'))) + '">' + E(j.status || '') + '</span>' + progress + '</td>';
            html += '<td>';
            if (canCancel) {
                html += '<button class="btn btn-danger btn-sm" data-job="' + E(j.job_id) + '" onclick="TabSetup.cancelJob(this.dataset.job)">Cancel</button>';
            }
            if (j.log_available || j.output) {
                html += '<button class="btn btn-sm" style="margin-left:6px" data-job="' + E(j.job_id) + '" onclick="TabSetup.showJobOutput(this.dataset.job)">Log</button>';
            }
            html += '</td></tr>';
        }
        html += '</tbody></table>';
        panel.innerHTML = html;
    },

    _jobProgressHtml: function(job) {
        if (!job || !job.progress) return '';
        var E = App.esc;
        var pct = job.progress_percent;
        var hasPct = typeof pct === 'number' && isFinite(pct);
        var width = hasPct ? Math.max(0, Math.min(100, pct)) : 0;
        var label = job.progress.message || (hasPct ? (pct.toFixed(1) + '%') : 'working');
        var html = '<div style="margin-top:7px;min-width:170px;max-width:260px">';
        html += '<div style="height:5px;background:rgba(255,255,255,0.10);border-radius:3px;overflow:hidden">';
        html += '<div style="height:100%;width:' + width + '%;background:#3fb950"></div>';
        html += '</div>';
        html += '<div style="margin-top:4px;color:var(--text-secondary);font-size:12px;white-space:normal">' + E(label) + '</div>';
        html += '</div>';
        return html;
    },

    _jobWarningHtml: function(job) {
        if (!job || !job.warning_summary) return '';
        return '<div style="margin-top:5px;color:#d29922;font-size:12px;white-space:normal">' + App.esc(job.warning_summary) + '</div>';
    },

    _jobAttemptHtml: function(job) {
        if (!job || !job.target_attempts || job.target_attempts <= 1) return '';
        var text = 'Attempt ' + job.attempt + ' of ' + job.target_attempts;
        if (job.superseded) {
            text += '; superseded by latest ' + (job.final_status_for_target || 'attempt');
        }
        return '<div style="margin-top:5px;color:var(--text-secondary);font-size:12px;white-space:normal">' + App.esc(text) + '</div>';
    },

    renderInstalledLoras: function() {
        var panel = document.getElementById('lora-installed-list');
        if (!panel) return;
        var E = App.esc;
        var loras = App.state.loras || [];
        if (loras.length === 0) {
            panel.innerHTML = '<div class="empty-state">No LoRAs installed yet. LoRAs are optional &mdash; search above or paste a repo ID to add one.</div>';
            return;
        }
        var html = '<table><thead><tr><th>Name</th><th>Size</th><th>Adapter</th><th></th></tr></thead><tbody>';
        for (var i = 0; i < loras.length; i++) {
            var l = loras[i];
            var sizeStr = l.size_mb >= 1024 ? (l.size_mb / 1024).toFixed(1) + ' GB' : l.size_mb.toFixed(0) + ' MB';
            html += '<tr>';
            html += '<td style="font-family:var(--font-mono);font-size:12px">' + E(l.name) + '</td>';
            html += '<td>' + sizeStr + '</td>';
            html += '<td>' + (l.has_adapter ? '<span class="badge badge-green">OK</span>' : '<span class="badge badge-red">No adapter</span>') + '</td>';
            html += '<td><button class="btn btn-danger btn-sm" data-name="' + E(l.name) + '" onclick="TabSetup.deleteLora(this.dataset.name)">Delete</button></td>';
            html += '</tr>';
        }
        html += '</tbody></table>';
        panel.innerHTML = html;
    },

    installVariant: async function(btn) {
        var model = btn.dataset.model;
        var sel = document.getElementById('variant-' + model);
        if (!sel) { App.toast('Variant selector not found', 'error'); return; }
        var variantId = sel.value;
        await this.installVariantById(model, variantId, btn);
    },

    installVariantById: async function(model, variantId, btn) {
        btn.disabled = true;
        btn.textContent = 'Starting...';
        try {
            var data = await App.api('POST', '/api/setup/install-variant', {
                model: model, variant_id: variantId
            });
            await App.loadInstallJobs();
            btn.textContent = 'Downloading...';
            App.toast('Downloading ' + variantId, 'info');
            this._pollVariantJob(data.job_id, btn, model, variantId);
        } catch(e) {
            App.toast('Variant install failed: ' + e.message, 'error');
            btn.disabled = false;
            btn.textContent = 'Download';
        }
    },

    _pollVariantJob: function(jobId, btn, model, variantId) {
        var poll = setInterval(async function() {
            try {
                var job = await App.api('GET', '/api/setup/jobs/' + jobId);
                if (job.status === 'completed') {
                    clearInterval(poll);
                    App.toast('Variant ' + variantId + ' downloaded', 'success');
                    btn.disabled = false;
                    btn.textContent = 'Download';
                    await App.loadSetupStatus();
                    await App.loadInstallJobs();
                } else if (job.status === 'failed') {
                    clearInterval(poll);
                    App.toast('Variant download failed', 'error');
                    btn.disabled = false;
                    btn.textContent = 'Retry';
                    await App.loadInstallJobs();
                } else if (job.status === 'cancelled') {
                    clearInterval(poll);
                    App.toast('Variant download cancelled', 'info');
                    btn.disabled = false;
                    btn.textContent = 'Download';
                    await App.loadInstallJobs();
                }
            } catch(e) {
                clearInterval(poll);
                btn.disabled = false;
                btn.textContent = 'Download';
            }
        }, 3000);
    },

    searchLoras: async function() {
        var q = document.getElementById('lora-search-input').value.trim();
        if (!q) { App.toast('Enter a search query', 'error'); return; }
        var panel = document.getElementById('lora-search-results');
        panel.innerHTML = '<div class="empty-state">Searching...</div>';
        try {
            var data = await App.api('GET', '/api/search/hf?q=' + encodeURIComponent(q) + '&kind=lora&limit=20');
            this.renderLoraSearchResults(data.results || []);
        } catch(e) {
            panel.innerHTML = '<div class="empty-state">Search failed: ' + App.esc(e.message) + '</div>';
        }
    },

    renderLoraSearchResults: function(results) {
        var panel = document.getElementById('lora-search-results');
        if (!panel) return;
        var E = App.esc;
        if (results.length === 0) {
            panel.innerHTML = '<div class="empty-state">No results</div>';
            return;
        }
        var html = '';
        for (var i = 0; i < results.length; i++) {
            var r = results[i];
            html += '<div class="search-result">';
            html += '<div><div class="repo-name">' + E(r.repo_id) + '</div>';
            html += '<div class="repo-meta">' + E(r.author || '') + ' - ' + (r.downloads || 0) + ' downloads - ' + (r.likes || 0) + ' likes</div></div>';
            html += '<button class="btn btn-sm" data-repo="' + E(r.repo_id) + '" onclick="TabSetup.installLoraFromSearch(this)">Download</button>';
            html += '</div>';
        }
        panel.innerHTML = html;
    },

    installLoraFromSearch: async function(btn) {
        var repo = btn.dataset.repo;
        btn.disabled = true;
        btn.textContent = 'Downloading...';
        try {
            var data = await App.api('POST', '/api/loras/install', { repo: repo });
            await App.loadInstallJobs();
            App.toast('LoRA download started (job ' + data.job_id + ')', 'info');
            this._pollLoraJob(data.job_id, btn);
        } catch(e) {
            App.toast('LoRA install failed: ' + e.message, 'error');
            btn.disabled = false;
            btn.textContent = 'Download';
        }
    },

    installLoraByRepo: async function() {
        var repo = document.getElementById('lora-repo-input').value.trim();
        var name = document.getElementById('lora-name-input').value.trim() || null;
        if (!repo || repo.indexOf('/') === -1) {
            App.toast('Enter repo as org/name', 'error');
            return;
        }
        try {
            var data = await App.api('POST', '/api/loras/install', { repo: repo, name: name });
            await App.loadInstallJobs();
            App.toast('LoRA download started (job ' + data.job_id + ')', 'info');
            this._pollLoraJob(data.job_id, null);
        } catch(e) {
            App.toast('LoRA install failed: ' + e.message, 'error');
        }
    },

    _pollLoraJob: function(jobId, btn) {
        var poll = setInterval(async function() {
            try {
                var job = await App.api('GET', '/api/setup/jobs/' + jobId);
                if (job.status === 'completed') {
                    clearInterval(poll);
                    App.toast('LoRA installed successfully', 'success');
                    if (btn) { btn.disabled = false; btn.textContent = 'Downloaded'; }
                    await App.loadLoras();
                    await App.loadInstallJobs();
                } else if (job.status === 'failed') {
                    clearInterval(poll);
                    App.toast('LoRA install failed', 'error');
                    if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
                    await App.loadInstallJobs();
                } else if (job.status === 'cancelled') {
                    clearInterval(poll);
                    App.toast('LoRA install cancelled', 'info');
                    if (btn) { btn.disabled = false; btn.textContent = 'Download'; }
                    await App.loadInstallJobs();
                }
            } catch(e) {
                clearInterval(poll);
                if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
            }
        }, 3000);
    },

    deleteLora: async function(name) {
        if (!confirm('Delete LoRA "' + name + '"? This cannot be undone.')) return;
        try {
            await App.api('DELETE', '/api/loras/' + encodeURIComponent(name));
            App.toast('LoRA deleted', 'success');
            await App.loadLoras();
        } catch(e) {
            App.toast('Delete failed: ' + e.message, 'error');
        }
    },

    openTab: function(tabName) {
        if (window.App && typeof App.activateTab === 'function') {
            App.activateTab(tabName);
        }
    },

    _jobErrorText: function(job) {
        if (!job) return 'unknown error';
        var text = (job.output || job.error || '').trim();
        if (text) return text.slice(-500);
        return 'install ' + (job.status || 'failed');
    },

    _jobText: function(job) {
        return ((job && (job.output || job.error)) || '').trim();
    },

    _extractUrls: function(text) {
        var hits = [];
        String(text || '').replace(/https?:\/\/[^\s'"<>`)]+/g, function(url) {
            url = url.replace(/[.,;:]+$/, '');
            if (hits.indexOf(url) === -1) hits.push(url);
            return url;
        });
        return hits;
    },

    _jobRepoUrl: function(job) {
        if (!job) return '';
        if (job.accept_url) return job.accept_url;
        if (job.repo) return 'https://huggingface.co/' + job.repo;
        return '';
    },

    _jobFailureKind: function(job) {
        if (job && job.failure_kind && job.failure_kind !== 'unknown') return job.failure_kind;
        var text = this._jobText(job).toLowerCase();
        if (/gated|accept the model card|requires authorization|access denied|401|403|license/.test(text)) {
            return 'access';
        }
        if (/temporary failure|failed to resolve|name resolution|network|connectionpool|max retries|dns/.test(text)) {
            return 'network';
        }
        return '';
    },

    _jobHelperLinks: function(job) {
        var urls = this._extractUrls(this._jobText(job));
        var repoUrl = this._jobRepoUrl(job);
        if (repoUrl && urls.indexOf(repoUrl) === -1) urls.unshift(repoUrl);
        return urls;
    },

    _jobHelperHtml: function(job, compact) {
        if (!job || job.status !== 'failed') return '';
        var E = App.esc;
        var links = this._jobHelperLinks(job);
        var kind = this._jobFailureKind(job);
        var gated = !!(job.gated || job.accept_url);
        var hint = '';
        if (kind === 'access' || gated) {
            hint = 'HuggingFace access is required for this model. Accept the model card with the same account as your saved token, then retry.';
            if (kind === 'network') {
                hint += ' This attempt also could not reach HuggingFace, so check DNS/network access before retrying.';
            }
        } else if (kind === 'network') {
            hint = 'The download could not reach HuggingFace. Check DNS/network access, then retry.';
        }
        if (job.repo) {
            hint += (hint ? ' ' : '') + 'Model page: ' + job.repo + '.';
        }
        var html = '';
        if (job && job.failure_summary && !compact) {
            html += '<div style="color:var(--text-secondary);font-size:13px;margin-bottom:10px">' + E(job.failure_summary) + '</div>';
        }
        if (hint && !compact) {
            html += '<div style="color:var(--text-secondary);font-size:13px;margin-bottom:10px">' + E(hint) + '</div>';
        }
        if (links.length) {
            html += '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">';
            for (var i = 0; i < links.length; i++) {
                var label = (kind === 'access' || gated) && i === 0 ? 'Accept model access' : 'Open default';
                html += '<button class="btn btn-sm" data-open-external-url="' + E(links[i]) + '">' + label + '</button>';
                if (i === 0) {
                    html += '<button class="btn btn-sm" data-open-external-browser="chrome" data-open-external-url="' + E(links[i]) + '">Chrome</button>';
                    html += '<button class="btn btn-sm" data-open-external-browser="edge" data-open-external-url="' + E(links[i]) + '">Edge</button>';
                    html += '<button class="btn btn-sm" data-open-external-browser="firefox" data-open-external-url="' + E(links[i]) + '">Firefox</button>';
                    html += '<button class="btn btn-sm" data-copy-url="' + E(links[i]) + '">Copy link</button>';
                }
            }
            html += '</div>';
        }
        return html;
    },

    showInstallFailure: function(job) {
        var E = App.esc;
        var helper = this._jobHelperHtml(job, false);
        var text = this._jobText(job);
        var html = '';
        html += helper || '<div style="color:var(--text-secondary);font-size:13px;margin-bottom:10px">No helper link was found in this job. The raw output is below.</div>';
        html += '<pre class="modal-log" style="margin-top:12px">' + E(text || 'No output captured.') + '</pre>';
        App.showTextModal('Install failed', html, { html: true });
    },

    _waitForJob: async function(jobId, onTick) {
        var deadline = Date.now() + (12 * 60 * 60 * 1000);
        while (Date.now() < deadline) {
            var job = await App.api('GET', '/api/setup/jobs/' + jobId);
            await App.loadInstallJobs();
            if (typeof onTick === 'function') onTick(job);
            if (job.status === 'completed') return job;
            if (job.status === 'failed' || job.status === 'cancelled') {
                var err = new Error(this._jobErrorText(job));
                err.job = job;
                throw err;
            }
            await new Promise(function(r) { setTimeout(r, 3000); });
        }
        throw new Error('Install timed out after 12 hours');
    },

    installAudioLabDefaults: async function(btn) {
        var st = App.state.setupStatus.audio_lab || {};
        var saVariant = st.quick_sa_variant || 'sao-open-small';
        var clapVariant = st.default_clap_variant || 'larger-clap-general';
        btn.disabled = true;
        btn.textContent = 'Starting SA model...';
        try {
            var sa = await App.api('POST', '/api/audio_lab/install-model', { variant_id: saVariant });
            await App.loadInstallJobs();
            App.toast('Installing Stable Audio (' + saVariant + ')…', 'info');
            await this._waitForJob(sa.job_id, function() {
                btn.textContent = 'Installing SA model…';
            });
            btn.textContent = 'Starting CLAP...';
            var clap = await App.api('POST', '/api/audio_lab/install-clap', { variant_id: clapVariant });
            await App.loadInstallJobs();
            App.toast('Installing CLAP (' + clapVariant + ')…', 'info');
            await this._waitForJob(clap.job_id, function() {
                btn.textContent = 'Installing CLAP…';
            });
            App.toast('Audio Lab defaults installed', 'success');
            btn.textContent = 'Install defaults';
            btn.disabled = false;
            await App.loadSetupStatus();
        } catch (e) {
            App.toast('Audio Lab install failed: ' + e.message, 'error');
            if (e.job) this.showInstallFailure(e.job);
            btn.disabled = false;
            btn.textContent = 'Retry defaults';
            await App.loadInstallJobs();
            await App.loadSetupStatus();
        }
    },

    installAceStepDefaults: async function(btn) {
        var st = App.state.setupStatus.ace_step || {};
        var modelVariant = st.default_model_variant || 'ace-xl-turbo';
        btn.disabled = true;
        btn.textContent = 'Starting defaults...';
        try {
            var model = await App.api('POST', '/api/ace_step/install-model', { variant_id: modelVariant });
            await App.loadInstallJobs();
            App.toast('Installing ACE-Step defaults (' + modelVariant + ' plus shared core/LM if missing)', 'info');
            await this._waitForJob(model.job_id, function() {
                btn.textContent = 'Installing defaults...';
            });
            App.toast('ACE Step defaults installed', 'success');
            btn.textContent = 'Install defaults';
            btn.disabled = false;
            await App.loadSetupStatus();
        } catch (e) {
            App.toast('ACE Step install failed: ' + e.message, 'error');
            btn.disabled = false;
            btn.textContent = 'Retry defaults';
            await App.loadInstallJobs();
            await App.loadSetupStatus();
        }
    },

    installMissingDefaults: async function(btn) {
        btn.disabled = true;
        btn.textContent = 'Starting defaults...';
        try {
            var data = await App.api('POST', '/api/setup/install-missing-defaults');
            await App.loadInstallJobs();
            App.toast('Installing missing defaults', 'info');
            await this._waitForJob(data.job_id, function(job) {
                var pct = job && typeof job.progress_percent === 'number' ? ' ' + job.progress_percent.toFixed(0) + '%' : '';
                btn.textContent = 'Installing defaults' + pct;
            });
            App.toast('Missing defaults installed', 'success');
            btn.textContent = 'Install missing defaults';
            btn.disabled = false;
            await App.loadSetupStatus();
            await App.loadInstallJobs();
        } catch (e) {
            App.toast('Default install failed: ' + e.message, 'error');
            if (e.job) this.showInstallFailure(e.job);
            btn.disabled = false;
            btn.textContent = 'Retry defaults';
            await App.loadInstallJobs();
            await App.loadSetupStatus();
        }
    },

    install: async function(btn) {
        var model = btn.dataset.model;
        if (this._DEDICATED_IDS.indexOf(model) !== -1) {
            App.toast(model + ' installs from its dedicated tab — see Audio Generation below', 'error');
            return;
        }
        btn.disabled = true;
        btn.textContent = 'Starting...';
        try {
            var data = await App.api('POST', '/api/setup/install/' + encodeURIComponent(model));
            var jobId = data.job_id;
            await App.loadInstallJobs();
            btn.textContent = 'Installing...';
            App.toast('Installing ' + model + ' (job ' + jobId + ')', 'info');
            // Poll for completion
            TabSetup._pollJob(jobId, btn, model);
        } catch (e) {
            App.toast('Install failed: ' + e.message, 'error');
            btn.disabled = false;
            btn.textContent = 'Install';
        }
    },

    _pollJob: function(jobId, btn, model) {
        var poll = setInterval(async function() {
            try {
                var job = await App.api('GET', '/api/setup/jobs/' + jobId);
                if (job.status === 'completed') {
                    clearInterval(poll);
                    App.toast(model + ' installed successfully', 'success');
                    await App.loadSetupStatus();
                    await App.loadInstallJobs();
                } else if (job.status === 'failed') {
                    clearInterval(poll);
                    App.toast(model + ' install failed', 'error');
                    btn.disabled = false;
                    btn.textContent = 'Retry';
                    await App.loadInstallJobs();
                } else if (job.status === 'cancelled') {
                    clearInterval(poll);
                    App.toast(model + ' install cancelled', 'info');
                    btn.disabled = false;
                    btn.textContent = 'Install';
                    await App.loadInstallJobs();
                }
                // else still running
            } catch(e) {
                clearInterval(poll);
                btn.disabled = false;
                btn.textContent = 'Install';
            }
        }, 3000);
    },

    saveToken: async function() {
        var token = document.getElementById('hf-token-input').value.trim();
        if (!token) { App.toast('Enter a token first', 'error'); return; }
        if (!token.startsWith('hf_')) {
            App.toast('Token should start with hf_ -- check your HuggingFace settings', 'error');
            return;
        }
        try {
            var data = await App.api('POST', '/api/setup/hf-token', { token: token });
            var hf = data.huggingface || {};
            App.toast(hf.token_valid === true ? 'Token saved and verified' : 'Token saved', 'success');
            await App.loadSetupStatus();
        } catch(e) {
            App.toast('Failed to save token: ' + e.message, 'error');
        }
    },

    cancelJob: async function(jobId) {
        if (!confirm('Cancel install job ' + jobId + '?')) return;
        try {
            await App.api('POST', '/api/setup/jobs/' + encodeURIComponent(jobId) + '/cancel');
            App.toast('Install job cancelling', 'info');
            await App.loadInstallJobs();
        } catch(e) {
            App.toast('Cancel failed: ' + e.message, 'error');
        }
    },

    showJobOutput: async function(jobId) {
        var jobs = App.state.installJobs || [];
        for (var i = 0; i < jobs.length; i++) {
            if (jobs[i].job_id === jobId) {
                var text = jobs[i].output || '';
                if (jobs[i].log_available) {
                    try {
                        var data = await App.api('GET', '/api/setup/jobs/' + encodeURIComponent(jobId) + '/log?lines=500');
                        text = (data.lines || []).join('\n');
                        if (data.truncated) text = '[showing tail of large log]\n' + text;
                    } catch (e) {
                        text = text || ('Could not load job log: ' + e.message);
                    }
                }
                if (jobs[i].status === 'failed') {
                    var helper = this._jobHelperHtml(jobs[i], false);
                    var html = (helper || '<div style="color:var(--text-secondary);font-size:13px;margin-bottom:10px">No helper link was found in this job. The raw output is below.</div>');
                    html += '<pre class="modal-log" style="margin-top:12px">' + App.esc(text || 'No output captured.') + '</pre>';
                    App.showTextModal('Install failed', html, { html: true });
                    return;
                }
                App.showTextModal('Install job ' + jobId, text || 'No output captured.');
                return;
            }
        }
    },
};
