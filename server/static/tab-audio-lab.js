// Audio Lab tab — Stable Audio + CLAP.
//
// Phase 1 surface: install / delete registry assets (Stable Audio models,
// VAE swaps, CLAP models) and install arbitrary HuggingFace repos via
// the Custom HF Repo field. Inference (Generate / A2A / Inpaint / VAE /
// Score) is added in Phase 2+. The container <section id="tab-audio-lab">
// is rendered into by render() on init; refreshStatus() repopulates the
// installed/uninstalled state without rebuilding the form.

var TabAudioLab = {
    state: {
        models: [],
        vaes:   [],
        claps:  [],
        custom: { model: [], vae: [], clap: [] },
        runtime: { runtime_ready: true, runtime_missing: [], runtime_optional_missing: [] },
        statusLoaded: false,
        statusLoading: false,
        workers: [],
        selectedWorkerId: null,
        showLoadControls: false,
        live: null,           // /api/audio_lab/state response
        samplers: [],         // dropdown options for sampler
        lastJob: null,        // most recent inference job (any mode)
        generating: false,    // UI lock during in-flight inference
        jobsInFlight: {},
        currentMode: 'generate',  // generate | a2a | inpaint | uncond | vae | score
        initAudioB64: null,        // staged init audio for a2a/inpaint/vae
        initAudioName: null,
        progressHandle: null,      // setInterval handle for the progress poll
    },

    init: function() {
        this.render();
        this.refreshStatus();
        this.refreshLiveState();
        this.refreshSamplers();
        this.refreshJobs();
        // Highlight the default mode tab and render its form.
        this.setMode(this.state.currentMode || 'generate');
    },

    onActivate: function() {
        this.refreshStatus();
        this.refreshLiveState();
    },

    refreshJobs: async function() {
        var filterEl = document.getElementById('audio-lab-jobs-filter');
        var mode = filterEl ? filterEl.value : '';
        var params = 'limit=30';
        if (mode) params += '&mode=' + encodeURIComponent(mode);
        try {
            var data = await App.api('GET', '/api/audio_lab/jobs?' + params);
            this.state.jobs = data.jobs || [];
        } catch (err) {
            this.state.jobs = [];
        }
        this.renderJobs();
    },

    renderJobs: function() {
        var el = document.getElementById('audio-lab-jobs-list');
        if (!el) return;
        var jobs = this.state.jobs || [];
        if (!jobs.length) {
            el.innerHTML = '<div class="empty-state">No jobs yet.</div>';
            return;
        }
        var html = '';
        for (var i = 0; i < jobs.length; i++) {
            var j = jobs[i];
            var date = j.created_at ? j.created_at.replace('T', ' ').slice(0, 16) : '?';
            var scoreBadge = (typeof j.best_score === 'number' && isFinite(j.best_score))
                ? ' <span class="badge badge-blue">' + j.best_score.toFixed(3) + '</span>'
                : '';
            html += '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border,#222)">' +
              '<div style="flex:1;min-width:0">' +
                '<span class="badge badge-purple">' + App.esc(j.mode || '?') + '</span> ' +
                '<code>' + App.esc(j.job_id.slice(0, 12)) + '…</code>' + scoreBadge +
                '<div class="section-title" style="margin-top:2px">' + App.esc(date) +
                  ' · ' + (j.n || 1) + ' file' + ((j.n || 1) > 1 ? 's' : '') +
                  (j.sa_variant ? ' · ' + App.esc(this._displayName(this.state.models, j.sa_variant)) : '') +
                  (j.prompt ? ' · "' + App.esc(j.prompt.slice(0, 60)) +
                    (j.prompt.length > 60 ? '…' : '') + '"' : '') +
                '</div>' +
              '</div>' +
              // FRO media-1: pass dynamic values via data-* (HTML-attr context, where
              // App.esc is correct) and read them from this.dataset at click time,
              // instead of interpolating into inline JS-string handlers.
              (j.best_url
                ? '<button class="btn btn-sm" data-url="' + App.esc(j.best_url) +
                    '" data-name="' + App.esc(j.job_id) + '.wav"' +
                    ' onclick="TabAudioLab.useAsInitFromUrl(this.dataset.url, this.dataset.name)">Use as init</button>'
                : '') +
              '<a class="btn btn-sm" href="' + App.urlWithToken(j.zip_url) +
                '" download="' + App.esc(j.job_id) + '.zip">ZIP</a>' +
              '<button class="btn btn-sm" data-job="' + App.esc(j.job_id) +
                '" onclick="TabAudioLab.loadJob(this.dataset.job)">Open</button>' +
              '<button class="btn btn-danger btn-sm" data-job="' + App.esc(j.job_id) +
                '" onclick="TabAudioLab.deleteJob(this.dataset.job, this)">×</button>' +
            '</div>';
        }
        el.innerHTML = html;
    },

    loadJob: async function(job_id) {
        try {
            var data = await App.api('GET', '/api/audio_lab/outputs/' + encodeURIComponent(job_id));
            if (!data || !data.manifest) { App.toast('No manifest', 'error'); return; }
            // Reconstruct the job shape used by renderResults().
            this.state.lastJob = {
                job_id: job_id,
                mode: data.manifest.mode,
                results: data.manifest.results || [],
            };
            this.renderResults();
            window.scrollTo({ top: document.getElementById('audio-lab-results-card').offsetTop, behavior: 'smooth' });
        } catch (err) {
            App.toast('Failed to open job: ' + err.message, 'error');
        }
    },

    deleteJob: async function(job_id, btn) {
        if (!confirm('Delete job ' + job_id.slice(0, 12) + '… and all its files?')) return;
        btn.disabled = true;
        try {
            await App.api('DELETE', '/api/audio_lab/jobs/' + encodeURIComponent(job_id));
            App.toast('Job deleted', 'success');
            await this.refreshJobs();
            if (this.state.lastJob && this.state.lastJob.job_id === job_id) {
                this.state.lastJob = null;
                this.renderResults();
            }
        } catch (err) {
            App.toast('Delete failed: ' + err.message, 'error');
            btn.disabled = false;
        }
    },

    render: function() {
        var el = document.getElementById('tab-audio-lab');
        if (!el) return;
        el.innerHTML =
            '<div class="card">' +
              '<div class="flex-between">' +
                '<h2>Audio Lab — Stable Audio + CLAP</h2>' +
                '<button class="btn btn-sm" onclick="TabAudioLab.refreshStatus(); TabAudioLab.refreshLiveState();">Refresh</button>' +
              '</div>' +
              '<p class="section-title" style="margin-top:8px">' +
                'Text-to-audio with CLAP-ranked candidates. Start or select an Audio Lab worker, then generate.' +
              '</p>' +
            '</div>' +

            // ── Active Worker Models card ──
            '<div class="card" id="audio-lab-live-card">' +
              '<h2>Active Worker Models</h2>' +
              '<div id="audio-lab-live-status" class="empty-state">Loading…</div>' +
              '<div id="audio-lab-load-controls" style="margin-top:12px"></div>' +
            '</div>' +

            // ── Mode tabs + active form ──
            '<div class="card" id="audio-lab-generate-card">' +
              '<div id="audio-lab-mode-tabs" style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:10px">' +
                ['generate', 'a2a', 'inpaint', 'uncond', 'vae', 'score'].map(function(m) {
                    var label = ({generate:'Generate', a2a:'A2A', inpaint:'Inpaint',
                                   uncond:'Uncond', vae:'VAE Lab', score:'CLAP Score'})[m];
                    return '<button class="btn btn-sm" data-mode="' + m + '" ' +
                           'onclick="TabAudioLab.setMode(\'' + m + '\')">' + label + '</button>';
                }).join('') +
              '</div>' +
              '<div id="audio-lab-mode-form"></div>' +
            '</div>' +

            // ── Results gallery ──
            '<div class="card" id="audio-lab-results-card">' +
              '<div class="flex-between" style="margin-bottom:8px">' +
                '<h2 style="margin:0">Results</h2>' +
                '<div id="audio-lab-results-actions"></div>' +
              '</div>' +
              '<div id="audio-lab-results">' +
                '<div class="empty-state">No generations yet. Fill the form above and hit Generate.</div>' +
              '</div>' +
            '</div>' +

            // ── Job history (past inference runs) ──
            '<div class="card" id="audio-lab-jobs-card">' +
              '<div class="flex-between">' +
                '<h2>Job History</h2>' +
                '<div style="display:flex;gap:6px;align-items:center">' +
                  '<select id="audio-lab-jobs-filter" class="input" onchange="TabAudioLab.refreshJobs()">' +
                    '<option value="">All modes</option>' +
                    '<option value="generate">Generate</option>' +
                    '<option value="generate-ranked">Ranked</option>' +
                    '<option value="a2a">A2A</option>' +
                    '<option value="inpaint">Inpaint</option>' +
                    '<option value="uncond">Uncond</option>' +
                    '<option value="vae_decode">VAE decode</option>' +
                    '<option value="vae_reconstruct">VAE reconstruct</option>' +
                  '</select>' +
                  '<button class="btn btn-sm" onclick="TabAudioLab.refreshJobs()">Refresh</button>' +
                '</div>' +
              '</div>' +
              '<div id="audio-lab-jobs-list" style="margin-top:8px"><div class="empty-state">Loading…</div></div>' +
            '</div>' +

            '<div class="card" id="audio-lab-models-card">' +
              '<h2>Stable Audio Models</h2>' +
              '<p class="section-title">' +
                'Pick a base model. Tier 1 = official Stability, Tier 2 = vetted community, ' +
                'Tier 3 = untested community uploads (may fail to load). All downloads via HuggingFace Xet.' +
              '</p>' +
              '<div id="audio-lab-models-list" class="empty-state">Loading…</div>' +
            '</div>' +

            '<div class="card" id="audio-lab-vaes-card">' +
              '<h2>VAE Swaps</h2>' +
              '<p class="section-title">' +
                'Optional autoencoder replacement applied at load time. ' +
                'Default keeps each model\'s own VAE.' +
              '</p>' +
              '<div id="audio-lab-vaes-list" class="empty-state">Loading…</div>' +
            '</div>' +

            '<div class="card" id="audio-lab-claps-card">' +
              '<h2>CLAP Scoring Models</h2>' +
              '<p class="section-title">' +
                'Used to score and rank generated audio against the text prompt. ' +
                'larger-clap-general is the default.' +
              '</p>' +
              '<div id="audio-lab-claps-list" class="empty-state">Loading…</div>' +
            '</div>' +

            '<div class="card" id="audio-lab-custom-card">' +
              '<h2>Custom HuggingFace Repo</h2>' +
              '<p class="section-title">' +
                'Install any HF repo as a Stable Audio model, VAE, or CLAP. ' +
                'Loader auto-detects diffusers vs native format at load time.' +
              '</p>' +
              '<div class="form-row" style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">' +
                '<div style="flex:2;min-width:200px">' +
                  '<label class="section-title" style="display:block;margin-bottom:4px">Repo (org/name)</label>' +
                  '<input type="text" id="audio-lab-custom-repo" class="input" ' +
                  '  placeholder="someuser/some-stable-audio-finetune" style="width:100%">' +
                '</div>' +
                '<div style="flex:1;min-width:140px">' +
                  '<label class="section-title" style="display:block;margin-bottom:4px">Local name (optional)</label>' +
                  '<input type="text" id="audio-lab-custom-name" class="input" ' +
                  '  placeholder="auto-derived from repo" style="width:100%">' +
                '</div>' +
                '<div style="min-width:120px">' +
                  '<label class="section-title" style="display:block;margin-bottom:4px">Kind</label>' +
                  '<select id="audio-lab-custom-kind" class="input">' +
                    '<option value="model">Model</option>' +
                    '<option value="vae">VAE</option>' +
                    '<option value="clap">CLAP</option>' +
                  '</select>' +
                '</div>' +
                '<button class="btn btn-primary" onclick="TabAudioLab.installCustom(this)">Install</button>' +
              '</div>' +
              '<div id="audio-lab-custom-list" style="margin-top:12px"></div>' +
            '</div>';
    },

    refreshStatus: async function() {
        this.state.statusLoading = true;
        try {
            var data = await App.api('GET', '/api/audio_lab/status');
            this.state.models = data.models || [];
            this.state.vaes   = data.vaes   || [];
            this.state.claps  = data.claps  || [];
            this.state.custom = data.custom || { model: [], vae: [], clap: [] };
            this.state.runtime = data.runtime || { runtime_ready: true, runtime_missing: [], runtime_optional_missing: [] };
            this.state.statusLoaded = true;
        } catch (err) {
            this.state.statusLoaded = false;
            App.toast('Failed to load Audio Lab status: ' + err.message, 'error');
            this.renderLiveState();
            return;
        } finally {
            this.state.statusLoading = false;
        }
        this.renderModels();
        this.renderVAEs();
        this.renderCLAPs();
        this.renderCustom();
        // Live state's load-controls dropdowns depend on installed-state of
        // models/vaes/claps; re-render them after install state lands.
        this.renderLiveState();
    },

    _runtimeReady: function() {
        return !this.state.runtime || this.state.runtime.runtime_ready !== false;
    },

    _tierBadge: function(tier) {
        var color = 'gray';
        var label = tier || 'unknown';
        if (tier === 'official')  { color = 'green';  label = 'Official';  }
        if (tier === 'community') { color = 'blue';   label = 'Community'; }
        if (tier === 'untested')  { color = 'orange'; label = 'Untested';  }
        return '<span class="badge badge-' + color + '">' + label + '</span>';
    },

    _formatBadge: function(fmt) {
        if (!fmt) return '';
        var color = (fmt === 'diffusers') ? 'purple' : 'gray';
        return '<span class="badge badge-' + color + '">' + App.esc(fmt) + '</span>';
    },

    _row: function(entry, kind) {
        // kind: 'model' | 'vae' | 'clap'
        var installed = !!entry.installed;
        var btnAction = installed ? 'delete' : 'install';
        var btnClass  = installed ? 'btn btn-danger btn-sm' : 'btn btn-primary btn-sm';
        var btnLabel  = installed ? 'Delete' : 'Install';
        var sizeStr   = entry.size_gb ? ('~' + entry.size_gb + 'GB') : '';
        var repoLink  = entry.repo
            ? '<a href="https://huggingface.co/' + App.esc(entry.repo) +
              '" target="_blank" rel="noopener noreferrer" style="color:inherit;text-decoration:none">' +
              App.esc(entry.repo) + ' ↗</a>'
            : '';
        var tags = (entry.tags || []).map(function(t){
            return '<span class="badge badge-gray">' + App.esc(t) + '</span>';
        }).join(' ');
        var gated = entry.gated
            ? '<span class="badge badge-orange" title="If access fails, the install error will show the exact HuggingFace page">gated</span>'
            : '';
        var status = installed
            ? '<span class="badge badge-green">Installed</span>'
            : '<span class="badge badge-gray">Not installed</span>';
        return (
            '<div class="audio-lab-row" data-variant="' + App.esc(entry.variant_id) + '" ' +
                 'style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--border,#222)">' +
              '<div style="flex:1;min-width:0">' +
                '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">' +
                  '<strong>' + App.esc(entry.display || entry.variant_id) + '</strong>' +
                  this._tierBadge(entry.tier) +
                  (kind === 'model' ? this._formatBadge(entry.format) : '') +
                  status +
                  gated +
                '</div>' +
                '<div class="section-title" style="margin-top:2px">' +
                  repoLink + (sizeStr ? ' · ' + sizeStr : '') + (tags ? ' · ' + tags : '') +
                '</div>' +
              '</div>' +
              '<button class="' + btnClass + '" ' +
                 'data-kind="' + kind + '" ' +
                 'data-variant="' + App.esc(entry.variant_id) + '" ' +
                 'data-action="' + btnAction + '" ' +
                 'onclick="TabAudioLab.handleClick(this)">' + btnLabel + '</button>' +
            '</div>'
        );
    },

    _groupedModelHtml: function(modelList) {
        var groups = { official: [], community: [], untested: [] };
        for (var i = 0; i < modelList.length; i++) {
            var t = modelList[i].tier || 'community';
            (groups[t] || groups.community).push(modelList[i]);
        }
        var html = '';
        var sectionLabels = {
            official:  'Tier 1 — Official Stability',
            community: 'Tier 2 — Established Community',
            untested:  'Tier 3 — Untested Community',
        };
        ['official', 'community', 'untested'].forEach(function(tier) {
            if (!groups[tier].length) return;
            html += '<h3 class="section-title" style="margin-top:14px">' +
                sectionLabels[tier] + '</h3>';
            for (var i = 0; i < groups[tier].length; i++) {
                html += TabAudioLab._row(groups[tier][i], 'model');
            }
        });
        return html;
    },

    renderModels: function() {
        var el = document.getElementById('audio-lab-models-list');
        if (!el) return;
        if (!this.state.models.length) {
            el.innerHTML = '<div class="empty-state">No models in registry.</div>';
            return;
        }
        el.innerHTML = this._groupedModelHtml(this.state.models);
    },

    renderVAEs: function() {
        var el = document.getElementById('audio-lab-vaes-list');
        if (!el) return;
        if (!this.state.vaes.length) {
            el.innerHTML = '<div class="empty-state">No VAEs in registry.</div>';
            return;
        }
        var html = '';
        for (var i = 0; i < this.state.vaes.length; i++) {
            // The "default" VAE entry has no install action — skip the row for it.
            if (this.state.vaes[i].variant_id === 'default') continue;
            html += this._row(this.state.vaes[i], 'vae');
        }
        if (!html) html = '<div class="empty-state">Only the model-default VAE is available; no swaps registered.</div>';
        el.innerHTML = html;
    },

    renderCLAPs: function() {
        var el = document.getElementById('audio-lab-claps-list');
        if (!el) return;
        if (!this.state.claps.length) {
            el.innerHTML = '<div class="empty-state">No CLAP models in registry.</div>';
            return;
        }
        var html = '';
        for (var i = 0; i < this.state.claps.length; i++) {
            html += this._row(this.state.claps[i], 'clap');
        }
        el.innerHTML = html;
    },

    renderCustom: function() {
        var el = document.getElementById('audio-lab-custom-list');
        if (!el) return;
        var kinds = ['model', 'vae', 'clap'];
        var anyShown = false;
        var html = '';
        for (var k = 0; k < kinds.length; k++) {
            var entries = this.state.custom[kinds[k]] || [];
            if (!entries.length) continue;
            anyShown = true;
            html += '<h3 class="section-title" style="margin-top:8px">Custom ' + kinds[k] + 's</h3>';
            for (var i = 0; i < entries.length; i++) {
                var entry = entries[i];
                html +=
                    '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border,#222)">' +
                      '<div style="flex:1;min-width:0">' +
                        '<strong>' + App.esc(entry.name) + '</strong> ' +
                        '<span class="badge badge-purple">Custom</span> ' +
                        '<span class="badge badge-green">Installed</span>' +
                        (entry.repo
                          ? '<div class="section-title">' +
                              '<a href="https://huggingface.co/' + App.esc(entry.repo) + '" target="_blank" ' +
                              'rel="noopener noreferrer" style="color:inherit;text-decoration:none">' +
                              App.esc(entry.repo) + ' ↗</a></div>'
                          : '') +
                      '</div>' +
                      '<button class="btn btn-danger btn-sm" ' +
                         'data-kind="custom" ' +
                         'data-custom-kind="' + kinds[k] + '" ' +
                         'data-name="' + App.esc(entry.name) + '" ' +
                         'onclick="TabAudioLab.deleteCustom(this)">Delete</button>' +
                    '</div>';
            }
        }
        if (!anyShown) {
            html = '<div class="section-title">No custom assets installed yet.</div>';
        }
        el.innerHTML = html;
    },

    // Click delegator for install/delete buttons inside the registry rows.
    handleClick: function(btn) {
        var kind = btn.dataset.kind;
        var variant = btn.dataset.variant;
        var action = btn.dataset.action;
        if (action === 'install') {
            this.installAsset(kind, variant, btn);
        } else {
            this.deleteAsset(kind, variant, btn);
        }
    },

    installAsset: async function(kind, variantId, btn) {
        var path;
        if (kind === 'model') path = '/api/audio_lab/install-model';
        else if (kind === 'vae')   path = '/api/audio_lab/install-vae';
        else if (kind === 'clap')  path = '/api/audio_lab/install-clap';
        else { App.toast('Unknown kind: ' + kind, 'error'); return; }

        btn.disabled = true;
        btn.textContent = 'Starting…';
        try {
            var data = await App.api('POST', path, { variant_id: variantId });
            App.toast('Installing ' + variantId + ' (job ' + data.job_id + ')', 'info');
            btn.textContent = 'Downloading…';
            this._pollJob(data.job_id, btn, kind, variantId, 'install');
            App.loadInstallJobs && App.loadInstallJobs();
        } catch (err) {
            App.toast('Install failed: ' + err.message, 'error');
            btn.disabled = false;
            btn.textContent = 'Install';
        }
    },

    deleteAsset: async function(kind, variantId, btn) {
        if (!confirm('Delete ' + kind + ' "' + variantId + '"? This removes downloaded weights from disk.')) {
            return;
        }
        var path;
        if (kind === 'model') path = '/api/audio_lab/install-model/';
        else if (kind === 'vae')   path = '/api/audio_lab/install-vae/';
        else if (kind === 'clap')  path = '/api/audio_lab/install-clap/';
        else { App.toast('Unknown kind: ' + kind, 'error'); return; }
        btn.disabled = true;
        btn.textContent = 'Deleting…';
        try {
            await App.api('DELETE', path + encodeURIComponent(variantId));
            App.toast(kind + ' "' + variantId + '" deleted', 'success');
            await this.refreshStatus();
        } catch (err) {
            App.toast('Delete failed: ' + err.message, 'error');
            btn.disabled = false;
            btn.textContent = 'Delete';
        }
    },

    installCustom: async function(btn) {
        var repo = (document.getElementById('audio-lab-custom-repo') || {}).value || '';
        var name = (document.getElementById('audio-lab-custom-name') || {}).value || '';
        var kind = (document.getElementById('audio-lab-custom-kind') || {}).value || 'model';
        repo = repo.trim();
        name = name.trim();
        if (!repo) { App.toast('Enter a HuggingFace repo (org/name)', 'error'); return; }
        if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$/.test(repo)) {
            App.toast('Repo must look like "org/name"', 'error');
            return;
        }
        var body = { repo: repo, kind: kind };
        if (name) body.name = name;
        btn.disabled = true;
        var origLabel = btn.textContent;
        btn.textContent = 'Starting…';
        try {
            var data = await App.api('POST', '/api/audio_lab/install-custom', body);
            App.toast('Installing ' + (data.name || repo) + ' (job ' + data.job_id + ')', 'info');
            btn.textContent = 'Downloading…';
            // Custom-install jobs use job_id polling identical to registry installs.
            this._pollCustomJob(data.job_id, btn, kind, data.name || name || repo.replace('/', '_'), origLabel);
            App.loadInstallJobs && App.loadInstallJobs();
        } catch (err) {
            App.toast('Custom install failed: ' + err.message, 'error');
            btn.disabled = false;
            btn.textContent = origLabel;
        }
    },

    deleteCustom: async function(btn) {
        var kind = btn.dataset.customKind;
        var name = btn.dataset.name;
        if (!confirm('Delete custom ' + kind + ' "' + name + '"?')) return;
        btn.disabled = true;
        btn.textContent = 'Deleting…';
        try {
            await App.api('DELETE', '/api/audio_lab/install-custom/' + encodeURIComponent(kind) + '/' + encodeURIComponent(name));
            App.toast('Custom ' + kind + ' "' + name + '" deleted', 'success');
            await this.refreshStatus();
        } catch (err) {
            App.toast('Delete failed: ' + err.message, 'error');
            btn.disabled = false;
            btn.textContent = 'Delete';
        }
    },

    _stopPoll: function(jobId) {
        var handle = this.state.jobsInFlight[jobId];
        if (handle) {
            clearInterval(handle);
            delete this.state.jobsInFlight[jobId];
        }
    },

    _pollJob: function(jobId, btn, kind, variantId, originalAction) {
        var self = this;
        this._stopPoll(jobId);  // replace any prior handle for the same job
        var handle = setInterval(async function() {
            // Stop if the originating button has been removed (tab nav, refresh).
            if (btn && !document.body.contains(btn)) {
                self._stopPoll(jobId);
                return;
            }
            try {
                var job = await App.api('GET', '/api/setup/jobs/' + jobId);
                if (job.status === 'completed') {
                    self._stopPoll(jobId);
                    App.toast(kind + ' "' + variantId + '" installed', 'success');
                    await self.refreshStatus();
                    App.loadInstallJobs && App.loadInstallJobs();
                } else if (job.status === 'failed') {
                    self._stopPoll(jobId);
                    if (window.TabSetup && TabSetup.showInstallFailure) {
                        TabSetup.showInstallFailure(job);
                    }
                    App.toast('Install failed for ' + variantId + ' (see Setup tab → jobs for log)', 'error');
                    if (btn && document.body.contains(btn)) {
                        btn.disabled = false;
                        btn.textContent = originalAction === 'install' ? 'Retry' : 'Install';
                    }
                } else if (job.status === 'cancelled') {
                    self._stopPoll(jobId);
                    App.toast('Install cancelled for ' + variantId, 'info');
                    if (btn && document.body.contains(btn)) {
                        btn.disabled = false;
                        btn.textContent = 'Install';
                    }
                }
            } catch (err) {
                self._stopPoll(jobId);
                if (btn && document.body.contains(btn)) {
                    btn.disabled = false;
                    btn.textContent = 'Install';
                }
            }
        }, 3000);
        this.state.jobsInFlight[jobId] = handle;
    },

    _pollCustomJob: function(jobId, btn, kind, name, origLabel) {
        var self = this;
        this._stopPoll(jobId);
        var handle = setInterval(async function() {
            if (btn && !document.body.contains(btn)) {
                self._stopPoll(jobId);
                return;
            }
            try {
                var job = await App.api('GET', '/api/setup/jobs/' + jobId);
                if (job.status === 'completed') {
                    self._stopPoll(jobId);
                    App.toast('Custom ' + kind + ' "' + name + '" installed', 'success');
                    if (btn && document.body.contains(btn)) {
                        btn.disabled = false;
                        btn.textContent = origLabel;
                    }
                    var r = document.getElementById('audio-lab-custom-repo');
                    var n = document.getElementById('audio-lab-custom-name');
                    if (r) r.value = '';
                    if (n) n.value = '';
                    await self.refreshStatus();
                    App.loadInstallJobs && App.loadInstallJobs();
                } else if (job.status === 'failed') {
                    self._stopPoll(jobId);
                    if (window.TabSetup && TabSetup.showInstallFailure) {
                        TabSetup.showInstallFailure(job);
                    }
                    App.toast('Custom install failed for ' + name + ' (see Setup tab → jobs for log)', 'error');
                    if (btn && document.body.contains(btn)) {
                        btn.disabled = false;
                        btn.textContent = origLabel;
                    }
                } else if (job.status === 'cancelled') {
                    self._stopPoll(jobId);
                    App.toast('Custom install cancelled', 'info');
                    if (btn && document.body.contains(btn)) {
                        btn.disabled = false;
                        btn.textContent = origLabel;
                    }
                }
            } catch (err) {
                self._stopPoll(jobId);
                if (btn && document.body.contains(btn)) {
                    btn.disabled = false;
                    btn.textContent = origLabel;
                }
            }
        }, 3000);
        this.state.jobsInFlight[jobId] = handle;
    },

    // ── Phase 2: live worker state + load/unload controls ──
    refreshLiveState: async function() {
        var prevReady = this._readinessSig();
        try {
            var data = await App.api('GET', '/api/audio_lab/workers');
            this.state.workers = data.workers || [];
            var selected = this._selectedWorkerRecord();
            this.state.selectedWorkerId = selected ? selected.worker_id : null;
            this.state.live = selected
                ? Object.assign({
                    running: selected.running,
                    worker_id: selected.worker_id,
                    port: selected.port,
                    worker_status: selected.status,
                    registry_variant: selected.variant,
                    worker: selected,
                }, selected.state || {})
                : { running: false };
        } catch (err) {
            this.state.workers = [];
            this.state.live = { running: false, _error: err.message };
        }
        // Re-render the live status header + load controls only. Do NOT
        // re-render the form below — that would wipe user input.
        this.renderLiveState();
        // BUT if SA/CLAP readiness actually flipped (e.g. the model finished
        // loading after the tab first painted, or it was loaded/unloaded in
        // another session), re-render the active mode form so its prereq
        // banner and submit-enabled state stay correct. Skip when readiness is
        // unchanged so a routine Refresh never wipes in-progress form input.
        if (this._readinessSig() !== prevReady) {
            App.preserveFocus(function() { TabAudioLab.renderModeForm(); });
        }
    },

    _selectedWorkerRecord: function() {
        var list = this.state.workers || [];
        var selectedId = this.state.selectedWorkerId;
        if (selectedId) {
            for (var i = 0; i < list.length; i++) {
                if (list[i] && list[i].worker_id === selectedId) return list[i];
            }
        }
        for (var j = 0; j < list.length; j++) {
            var st = list[j] && list[j].state;
            if (st && st.sa && st.clap) return list[j];
        }
        for (var k = 0; k < list.length; k++) {
            if (list[k] && list[k].status === 'ready') return list[k];
        }
        return list.length ? list[0] : null;
    },

    _activeWorkerId: function() {
        return this.state.selectedWorkerId || (this.state.live && this.state.live.worker_id) || '';
    },

    _withWorkerId: function(body) {
        var sa = (this.state.live || {}).sa;
        if (sa && sa.format !== 'native') {
            delete body.sampler;
            delete body.sigma_min;
            delete body.sigma_max;
            delete body.init_noise_level;
        }
        var out = Object.assign({}, body || {});
        var workerId = this._activeWorkerId();
        if (workerId) out.worker_id = workerId;
        return out;
    },

    selectWorker: function(workerId) {
        this.state.selectedWorkerId = workerId || null;
        this.state.showLoadControls = false;
        this.refreshLiveState();
    },

    showModelLoadControls: function() {
        this.state.showLoadControls = true;
        this.renderLiveState();
    },

    openServerTab: function() {
        var btn = document.querySelector('.tab-btn[data-tab="server"]');
        if (btn) btn.click();
    },

    startAudioLabWorker: async function(btn) {
        if (btn) {
            btn.disabled = true;
            btn.dataset.origText = btn.textContent;
            btn.textContent = 'Starting...';
        }
        try {
            await App.spawnWorker('audio_lab');
            await this.refreshLiveState();
            this.renderModeForm();
        } catch (err) {
            App.toast('Audio Lab worker start failed: ' + err.message, 'error');
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = btn.dataset.origText || 'Start Audio Lab worker';
            }
        }
    },

    // Compact signature of "what's loaded" — used to detect readiness changes.
    _readinessSig: function() {
        return (this._saLoaded() ? '1' : '0') + (this._clapLoaded() ? '1' : '0');
    },

    // Friendly display names for the sampler dropdown. Keys are the raw
    // sampler ids the backend expects; the VALUE sent in the payload is always
    // the raw id (we only prettify the visible label). Unknown ids fall back
    // to their raw string so new backend samplers still appear.
    SAMPLER_LABELS: {
        'dpmpp-3m-sde': 'DPM++ 3M SDE (best quality, default)',
        'dpmpp-2m-sde': 'DPM++ 2M SDE (faster)',
        'k-heun':       'Heun (high accuracy, slow)',
        'k-lms':        'LMS (linear multistep)',
        'k-dpmpp-2s-ancestral': 'DPM++ 2S Ancestral',
        'k-dpm-2':      'DPM-2',
        'k-dpm-fast':   'DPM Fast',
        'k-dpm-adaptive': 'DPM Adaptive',
        'k-euler':      'Euler',
        'k-euler-ancestral': 'Euler Ancestral',
    },

    // Build [{value,label}] sampler options (friendly labels, raw values).
    _samplerOptions: function() {
        var labels = this.SAMPLER_LABELS;
        var list = this.state.samplers && this.state.samplers.length
            ? this.state.samplers : ['dpmpp-3m-sde'];
        return list.map(function(s) {
            return { value: s, label: labels[s] || s };
        });
    },

    refreshSamplers: async function() {
        try {
            var data = await App.api('GET', '/api/audio_lab/samplers');
            this.state.samplers = data.samplers || [];
        } catch (err) {
            this.state.samplers = ['dpmpp-3m-sde', 'dpmpp-2m-sde', 'k-heun', 'k-lms'];
        }
        // Update the sampler dropdown in place if it's already in the DOM;
        // avoid re-rendering the whole form so we don't wipe user input.
        var sel = document.getElementById('al-sampler');
        if (sel) {
            var current = sel.value;
            var labels = this.SAMPLER_LABELS;
            sel.innerHTML = this._samplerOptions().map(function(op) {
                return '<option value="' + App.esc(op.value) + '">' + App.esc(op.label) + '</option>';
            }).join('');
            if (current) sel.value = current;
        }
    },

    _updateInitAudioStatus: function(idPrefix) {
        var disp = document.getElementById(idPrefix + '-status');
        if (!disp) return;
        var name = this.state.initAudioName;
        disp.innerHTML = name
            ? '<span class="badge badge-green">Loaded:</span> ' + App.esc(name)
            : '<span class="badge badge-gray">No audio loaded</span>';
    },

    /** Resolve a variant_id to its friendly display name from a registry list
     *  (this.state.models/claps/vaes). Falls back gracefully; keeps raw ids
     *  out of the primary view. */
    _displayName: function(list, vid) {
        if (!vid) return '';
        if (String(vid).indexOf('custom:') === 0) return 'Custom: ' + String(vid).slice(7);
        var arr = list || [];
        for (var i = 0; i < arr.length; i++) {
            if (arr[i] && arr[i].variant_id === vid) return arr[i].display || vid;
        }
        return vid;
    },

    _workerLabel: function(worker) {
        var st = (worker && worker.state) || {};
        var parts = [];
        parts.push(worker.status || 'worker');
        if (worker.device) parts.push(worker.device);
        if (st.sa && st.sa.variant_id) {
            parts.push(this._displayName(this.state.models, st.sa.variant_id));
        } else if (worker.variant) {
            parts.push(this._displayName(this.state.models, worker.variant));
        } else {
            parts.push('empty');
        }
        if (st.clap && st.clap.variant_id) {
            parts.push('CLAP');
        }
        return parts.join(' · ');
    },

    _workerSelectorHtml: function() {
        var workers = this.state.workers || [];
        if (!workers.length) return '';
        var current = this._activeWorkerId();
        var html = '<div style="flex:2;min-width:260px">' +
            '<label class="section-title" style="display:block;margin-bottom:4px">Audio Lab worker</label>' +
            '<select id="audio-lab-worker-select" class="input" onchange="TabAudioLab.selectWorker(this.value)">';
        for (var i = 0; i < workers.length; i++) {
            var w = workers[i];
            var sel = (w.worker_id === current) ? ' selected' : '';
            html += '<option value="' + App.esc(w.worker_id) + '"' + sel + '>' +
                App.esc(this._workerLabel(w)) + '</option>';
        }
        html += '</select></div>';
        return html;
    },

    renderLiveState: function() {
        var statusEl   = document.getElementById('audio-lab-live-status');
        var controlsEl = document.getElementById('audio-lab-load-controls');
        if (!statusEl || !controlsEl) return;

        if (!this._runtimeReady()) {
            var missing = (this.state.runtime.runtime_missing || []).join(', ');
            statusEl.innerHTML = App.prereqBanner({
                level: 'warn',
                text: 'Audio Lab runtime dependencies are missing: ' + missing + '. Run the app setup/venv repair before loading.',
                actionId: '',
                actionLabel: '',
            });
            controlsEl.innerHTML = '';
            return;
        }
        var optionalMissing = (this.state.runtime && this.state.runtime.runtime_optional_missing) || [];

        var live = this.state.live || {};
        var sa   = live.sa   || null;
        var clap = live.clap || null;
        var selectedWorker = live.worker || null;

        var statusHtml;
        if (!live.running) {
            statusHtml = '<div class="empty-state">' +
                'No Audio Lab worker is running.' +
            '</div>';
        } else {
            statusHtml =
                '<div style="display:flex;gap:16px;flex-wrap:wrap">' +
                  '<div><span class="section-title">Stable Audio:</span> ' +
                    (sa
                       ? '<span class="badge badge-green" title="' + App.esc(sa.variant_id) + '">' +
                         App.esc(this._displayName(this.state.models, sa.variant_id)) + '</span>' +
                         ' <span class="section-title">(' + App.esc(sa.format || '') +
                         ', sr=' + (sa.sample_rate || '?') + ')</span>'
                       : '<span class="badge badge-gray">None</span>') +
                  '</div>' +
                  '<div><span class="section-title">CLAP:</span> ' +
                    (clap
                       ? '<span class="badge badge-green" title="' + App.esc(clap.variant_id) + '">' +
                         App.esc(this._displayName(this.state.claps, clap.variant_id)) + '</span>'
                       : '<span class="badge badge-gray">None</span>') +
                  '</div>' +
                  '<div><span class="section-title">VAE:</span> ' +
                    ((sa && sa.vae_swap)
                       ? '<span class="badge badge-purple" title="' + App.esc(sa.vae_swap) + '">' +
                         App.esc(this._displayName(this.state.vaes, sa.vae_swap)) + '</span>'
                       : '<span class="badge badge-gray">default</span>') +
                  '</div>' +
                  '<div><span class="section-title">Worker:</span> ' +
                    '<span class="badge ' + ((live.worker_status || 'ready') === 'ready' ? 'badge-green' : 'badge-gray') +
                    '" title="' + App.esc(live.worker_id || '') + '">' +
                    App.esc(live.worker_status || 'ready') + '</span>' +
                  '</div>' +
                '</div>';
        }
        if (optionalMissing.length) {
            statusHtml += App.prereqBanner({
                level: 'info',
                text: 'Native Stable Audio variants and VAE swaps need: ' +
                    optionalMissing.join(', ') + '. Run setup/venv repair before loading those assets.',
            });
        }
        statusEl.innerHTML = statusHtml;

        if (!live.running) {
            controlsEl.innerHTML =
                '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">' +
                  '<button class="btn btn-primary" onclick="TabAudioLab.startAudioLabWorker(this)">Start Audio Lab worker</button>' +
                  '<button class="btn" onclick="TabAudioLab.openServerTab()">Open Server</button>' +
                '</div>';
            return;
        }

        // Build load controls: 3 dropdowns + Load button + Unload buttons.
        // Surface custom installs alongside registry entries so users can
        // load any HF repo they've pulled in via the Custom HF Repo field.
        var customSAList = (this.state.custom && this.state.custom.model || []).map(function(c) {
            return {
                variant_id: 'custom:' + c.name,
                display: 'Custom: ' + c.name,
                installed: true,
                tier: 'custom',
            };
        });
        var customCLAPList = (this.state.custom && this.state.custom.clap || []).map(function(c) {
            return { variant_id: 'custom:' + c.name, display: 'Custom: ' + c.name, installed: true, tier: 'custom' };
        });
        var customVAEList = (this.state.custom && this.state.custom.vae || []).map(function(c) {
            return { variant_id: 'custom:' + c.name, display: 'Custom: ' + c.name, installed: true, tier: 'custom' };
        });
        var saList = this.state.models.concat(customSAList);
        var clapList = this.state.claps.concat(customCLAPList);
        var vaeList = this.state.vaes.concat(customVAEList);
        var loadedSA = sa && sa.variant_id;
        var loadedCLAP = clap && clap.variant_id;
        var loadedVAE = (sa && sa.vae_swap) || 'default';
        var selectedSA = this._preferredInstalledValue(saList, loadedSA || (selectedWorker && selectedWorker.variant));
        var selectedCLAP = this._preferredInstalledValue(clapList, loadedCLAP);

        var saOpts   = this._installedOptions(saList, selectedSA, false, loadedSA);
        var clapOpts = this._installedOptions(clapList, selectedCLAP, false, loadedCLAP);
        var vaeOpts  = this._installedOptions(vaeList, loadedVAE, true, loadedVAE);

        var workerSelector = this._workerSelectorHtml();
        var needsLoadControls = !live.running || !sa || !clap || this.state.showLoadControls;
        var showSASelect = !live.running || !sa || this.state.showLoadControls;
        var showCLAPSelect = !live.running || !clap || this.state.showLoadControls;
        var showVAESelect = !live.running || !sa || this.state.showLoadControls;
        var canLoadVisible =
            (showSASelect && saOpts.anyInstalled) ||
            (showCLAPSelect && clapOpts.anyInstalled) ||
            (showVAESelect && sa && vaeOpts.anyInstalled);
        var loadDisabled = canLoadVisible ? '' : 'disabled';
        var modelControlsHtml = '';
        if (showSASelect) {
            modelControlsHtml +=
              '<div style="flex:2;min-width:220px">' +
                '<label class="section-title" style="display:block;margin-bottom:4px">Stable Audio</label>' +
                '<select id="audio-lab-load-sa" class="input">' + saOpts.html + '</select>' +
              '</div>';
        }
        if (showCLAPSelect) {
            modelControlsHtml +=
              '<div style="flex:2;min-width:220px">' +
                '<label class="section-title" style="display:block;margin-bottom:4px">CLAP</label>' +
                '<select id="audio-lab-load-clap" class="input">' + clapOpts.html + '</select>' +
              '</div>';
        }
        if (showVAESelect) {
            modelControlsHtml +=
              '<div style="flex:1;min-width:160px">' +
                '<label class="section-title" style="display:block;margin-bottom:4px">VAE</label>' +
                '<select id="audio-lab-load-vae" class="input">' + vaeOpts.html + '</select>' +
              '</div>';
        }
        if (live.running && !needsLoadControls) {
            controlsEl.innerHTML =
                '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">' +
                  workerSelector +
                  '<button class="btn" onclick="TabAudioLab.showModelLoadControls()">Change models</button>' +
                  '<button class="btn btn-sm" onclick="TabAudioLab.unloadComponent(\'sa\', this)">Unload SA</button>' +
                  '<button class="btn btn-sm" onclick="TabAudioLab.unloadComponent(\'clap\', this)">Unload CLAP</button>' +
                  (sa && sa.vae_swap
                      ? '<button class="btn btn-sm" onclick="TabAudioLab.unloadComponent(\'vae\', this)">Reset VAE</button>'
                      : '') +
                  '<button class="btn btn-danger btn-sm" onclick="TabAudioLab.unloadComponent(\'all\', this)">Unload All</button>' +
                '</div>';
            return;
        }
        controlsEl.innerHTML =
            '<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">' +
              workerSelector +
              modelControlsHtml +
              '<button class="btn btn-primary" ' + loadDisabled + ' ' +
                ' onclick="TabAudioLab.loadModels(this)">Load selected</button>' +
            '</div>' +
            (live.running
                ? '<div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">' +
                    '<button class="btn btn-sm" onclick="TabAudioLab.unloadComponent(\'sa\', this)">Unload SA</button>' +
                    '<button class="btn btn-sm" onclick="TabAudioLab.unloadComponent(\'clap\', this)">Unload CLAP</button>' +
                    (sa && sa.vae_swap
                        ? '<button class="btn btn-sm" onclick="TabAudioLab.unloadComponent(\'vae\', this)">Reset VAE</button>'
                        : '') +
                    '<button class="btn btn-danger btn-sm" onclick="TabAudioLab.unloadComponent(\'all\', this)">Unload All</button>' +
                  '</div>'
                : '');
    },

    _preferredInstalledValue: function(list, currentValue) {
        if (currentValue) return currentValue;
        for (var j = 0; j < list.length; j++) {
            if (list[j] && list[j].installed && list[j].variant_id !== 'default') {
                return list[j].variant_id;
            }
        }
        return currentValue || '';
    },

    _installedOptions: function(list, currentValue, includeDefault, loadedValue) {
        // Build <option> HTML for installed-only entries (or +default for VAEs).
        var opts = [];
        var anyInstalled = false;
        if (includeDefault) {
            var selDefault = (currentValue === 'default' || !currentValue) ? ' selected' : '';
            opts.push('<option value="default"' + selDefault + '>Default (model VAE)</option>');
            anyInstalled = true;
        } else {
            opts.push('<option value="">— pick one —</option>');
        }
        for (var i = 0; i < list.length; i++) {
            var entry = list[i];
            if (entry.variant_id === 'default') continue;  // VAE default handled above
            if (!entry.installed) continue;
            anyInstalled = true;
            var sel = (entry.variant_id === currentValue) ? ' selected' : '';
            var label = entry.display || entry.variant_id;
            if (entry.variant_id === loadedValue) label += ' (loaded)';
            opts.push('<option value="' + App.esc(entry.variant_id) + '"' + sel + '>' +
                App.esc(label) + '</option>');
        }
        if (currentValue && currentValue !== 'default' && opts.join('').indexOf('value="' + App.esc(currentValue) + '"') === -1) {
            anyInstalled = true;
            opts.push('<option value="' + App.esc(currentValue) + '" selected>' +
                App.esc(currentValue + ' (loaded)') + '</option>');
        }
        if (!anyInstalled && !includeDefault) {
            var label = (this.state.statusLoading || !this.state.statusLoaded)
                ? '— checking installed models —'
                : '— nothing installed yet —';
            opts = ['<option value="">' + label + '</option>'];
        }
        return { html: opts.join(''), anyInstalled: anyInstalled };
    },

    loadModels: async function(btn) {
        if (!this._runtimeReady()) {
            App.toast('Audio Lab runtime dependencies are missing. Run setup/venv repair first.', 'error');
            return;
        }
        var sa   = (document.getElementById('audio-lab-load-sa')   || {}).value || '';
        var clap = (document.getElementById('audio-lab-load-clap') || {}).value || '';
        var vae  = (document.getElementById('audio-lab-load-vae')  || {}).value || 'default';
        var body = {};
        if (sa)   body.sa_variant   = sa;
        if (clap) body.clap_variant = clap;
        if (vae && vae !== 'default') body.vae_variant = vae;
        body = this._withWorkerId(body);
        if (!body.sa_variant && !body.clap_variant && !body.vae_variant) {
            App.toast('Pick at least one model to load', 'error');
            return;
        }
        btn.disabled = true;
        var orig = btn.textContent;
        btn.textContent = 'Loading…';
        try {
            await App.api('POST', '/api/audio_lab/load', body);
            App.toast('Loaded', 'success');
            this.state.showLoadControls = false;
            await this.refreshLiveState();
            // Reflect new readiness in the active mode form (clears prereq
            // banners, enables the submit button). This re-render replaces the
            // form, so it runs only after an explicit load action.
            this.renderModeForm();
        } catch (err) {
            App.toast('Load failed: ' + err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = orig;
        }
    },

    unloadComponent: async function(component, btn) {
        if (!confirm('Unload ' + component + '? This frees GPU memory.')) return;
        btn.disabled = true;
        var orig = btn.textContent;
        btn.textContent = 'Unloading…';
        try {
            await App.api('POST', '/api/audio_lab/unload',
                this._withWorkerId({ component: component }));
            App.toast('Unloaded ' + component, 'success');
            this.state.showLoadControls = true;
            await this.refreshLiveState();
            // Re-render the active mode form so prereq banners reappear and the
            // submit button disables now that a component is gone.
            this.renderModeForm();
        } catch (err) {
            App.toast('Unload failed: ' + err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = orig;
        }
    },

    // ── Mode dispatch ──
    setMode: function(mode) {
        this.state.currentMode = mode;
        // Toggle the tab button active state: active button is .btn.btn-primary,
        // inactive is just .btn.
        var tabsEl = document.getElementById('audio-lab-mode-tabs');
        if (tabsEl) {
            var btns = tabsEl.querySelectorAll('button[data-mode]');
            for (var i = 0; i < btns.length; i++) {
                btns[i].classList.add('btn');
                if (btns[i].dataset.mode === mode) {
                    btns[i].classList.add('btn-primary');
                } else {
                    btns[i].classList.remove('btn-primary');
                }
            }
        }
        this.renderModeForm();
    },

    renderModeForm: function() {
        switch (this.state.currentMode) {
            case 'a2a':     return this.renderA2AForm();
            case 'inpaint': return this.renderInpaintForm();
            case 'uncond':  return this.renderUncondForm();
            case 'vae':     return this.renderVAEForm();
            case 'score':   return this.renderScoreForm();
            case 'generate':
            default:        return this.renderGenerateForm();
        }
    },

    // ── Shared readiness helpers (drive the prereq banners + submit gating) ──
    _saLoaded: function() {
        var live = this.state.live || {};
        return !!(live.running && live.sa);
    },
    _clapLoaded: function() {
        var live = this.state.live || {};
        return !!(live.running && live.clap);
    },
    _durationMax: function(fallback) {
        var live = this.state.live || {};
        var sa = live.sa || {};
        var maxDur = parseFloat(sa.max_duration_s);
        if (!isFinite(maxDur) || maxDur <= 0) maxDur = fallback || 47;
        return Math.max(1, Math.min(120, maxDur));
    },
    _durationDefault: function(maxDur) {
        return Math.min(10, maxDur || 47);
    },

    // Plain-English explanation reused as the App.tip on both sigma controls.
    SIGMA_TIP: 'Sigma is the amount of noise the sampler removes each step: it ' +
        'starts at Sigma max (fully noisy) and ends near Sigma min (clean audio). ' +
        'Leave the defaults unless you know the schedule for your model.',

    // ── Generate form ──
    renderGenerateForm: function() {
        var el = document.getElementById('audio-lab-mode-form');
        if (!el) return;
        var disabled = this.state.generating || !this._saLoaded() || !this._clapLoaded();
        var maxDur = this._durationMax(47);
        var defaultDur = this._durationDefault(maxDur);

        // PREREQ: ranked generate needs BOTH a Stable Audio model and a CLAP
        // model loaded (CLAP ranks the candidates). Guide the operator with a
        // one-click jump to the Active Worker Models card instead of a post-hoc error.
        var banner = '';
        if (!((this.state.live || {}).running)) {
            banner = App.prereqBanner({
                level: 'warn',
                text: 'Generate needs an Audio Lab worker running. Start a worker, then generate.',
                actionId: 'al-gen-prereq-start',
                actionLabel: 'Start worker',
            });
        } else if (!this._saLoaded() || !this._clapLoaded()) {
            var missing = [];
            if (!this._saLoaded())   missing.push('a Stable Audio model');
            if (!this._clapLoaded()) missing.push('a CLAP scoring model');
            banner = App.prereqBanner({
                level: 'warn',
                text: 'Generate needs ' + missing.join(' and ') +
                    ' loaded on the selected worker.',
                actionId: 'al-gen-prereq-load',
                actionLabel: 'Configure worker',
            });
        }

        el.innerHTML =
            '<div style="display:flex;flex-direction:column;gap:10px">' +
              banner +
              App.field({
                  type: 'textarea', id: 'al-prompt', label: 'Prompt', rows: 2,
                  placeholder: 'lofi hip hop beat with vinyl crackle, 90bpm, mellow piano',
                  hint: 'Describe the sound you want. More detail (genre, tempo, instruments, mood) ranks higher.',
              }) +
              App.field({
                  type: 'textarea', id: 'al-neg-prompt', label: 'Negative prompt (optional)', rows: 1,
                  placeholder: 'distorted, low quality',
                  hint: 'Qualities to steer away from.',
              }) +
              '<div class="field-grid">' +
                App.field({ type: 'slider', id: 'al-duration', label: 'Duration', min: 1, max: maxDur, step: 0.5, value: defaultDur, unit: 's',
                    hint: 'Length of each clip in seconds.' }) +
                App.field({ type: 'slider', id: 'al-steps', label: 'Steps', min: 10, max: 500, step: 5, value: 100,
                    hint: 'Denoising steps. More = cleaner but slower.' }) +
                App.field({ type: 'slider', id: 'al-cfg', label: 'CFG scale', min: 0, max: 20, step: 0.1, value: 7,
                    tip: 'Classifier-Free Guidance: how strongly the audio follows the prompt. Higher sticks closer to the text but can sound forced.',
                    hint: 'Prompt adherence strength.' }) +
                App.field({ type: 'slider', id: 'al-sigma-min', label: 'Sigma min (native models)', min: 0, max: 10, step: 0.01, value: 0.3,
                    tip: this.SIGMA_TIP, hint: 'Lowest noise level (end of schedule).' }) +
                App.field({ type: 'slider', id: 'al-sigma-max', label: 'Sigma max (native models)', min: 50, max: 1000, step: 10, value: 500,
                    tip: this.SIGMA_TIP, hint: 'Highest noise level (start of schedule).' }) +
                App.field({ type: 'slider', id: 'al-n', label: 'Candidates (N)', min: 1, max: 16, step: 1, value: 4,
                    tip: 'How many clips to generate. CLAP scores all of them and highlights the best match for your prompt.',
                    hint: 'Clips to generate and rank.' }) +
              '</div>' +
              '<div id="al-gen-clap-note"></div>' +
              '<div class="field-grid">' +
                App.field({ type: 'select', id: 'al-sampler', label: 'Sampler (native models)', options: this._samplerOptions(),
                    value: 'dpmpp-3m-sde',
                    tip: 'The numerical solver that turns noise into audio. The default is a good all-rounder.',
                    hint: 'Solver algorithm.' }) +
                App.field({ type: 'number', id: 'al-seed', label: 'Seed', min: 0, step: 1, placeholder: 'random',
                    hint: 'Blank = random each run. Set a number to reproduce a result.' }) +
                App.field({ type: 'text', id: 'al-score-prompt', label: 'Score prompt override', placeholder: '(uses Prompt above)',
                    hint: 'Optional: rank candidates against different text than the generation prompt.' }) +
              '</div>' +
              '<div>' +
                '<button class="btn btn-primary" id="al-generate-btn"' +
                  (disabled ? ' disabled' : '') + '>Generate</button>' +
                (disabled && !this.state.generating
                    ? '<span class="field-hint" style="margin-left:10px">' +
                      (((this.state.live || {}).running)
                        ? 'Configure the selected worker first.'
                        : 'Start an Audio Lab worker first.') +
                      '</span>'
                    : '') +
              '</div>' +
            '</div>';

        // Wire handlers without inline JS (security posture: no dynamic on*).
        var genBtn = document.getElementById('al-generate-btn');
        if (genBtn) genBtn.addEventListener('click', function() { TabAudioLab.generateRanked(genBtn); });
        var prereqBtn = document.getElementById('al-gen-prereq-load');
        if (prereqBtn) prereqBtn.addEventListener('click', function() { TabAudioLab.focusLoadPanel(); });
        var startBtn = document.getElementById('al-gen-prereq-start');
        if (startBtn) startBtn.addEventListener('click', function() { TabAudioLab.startAudioLabWorker(startBtn); });
        // Live CLAP-readiness note tied to the Candidates slider.
        var nEl = document.getElementById('al-n');
        if (nEl) nEl.addEventListener('input', function() { TabAudioLab._updateGenClapNote(); });
        this._updateGenClapNote();
    },

    // Show a contextual hint when ranked generation (N>1) is requested but no
    // CLAP model is loaded to rank with.
    _updateGenClapNote: function() {
        var noteEl = document.getElementById('al-gen-clap-note');
        if (!noteEl) return;
        var n = this._intVal('al-n', 4);
        if (n > 1 && !this._clapLoaded()) {
            noteEl.innerHTML = App.prereqBanner({
                level: 'info',
                text: 'You asked for ' + n + ' candidates, but no CLAP model is loaded — ' +
                    'they cannot be ranked by prompt match. Load a CLAP model to rank, or set Candidates to 1.',
            });
        } else {
            noteEl.innerHTML = '';
        }
    },

    // Prereq action: start a worker if none exists; otherwise scroll the
    // selected worker's configuration into view.
    focusLoadPanel: function() {
        if (!((this.state.live || {}).running)) {
            this.startAudioLabWorker();
            return;
        }
        var card = document.getElementById('audio-lab-live-card');
        if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
        var sa = document.getElementById('audio-lab-load-sa');
        if (sa) { try { sa.focus(); } catch (e) {} }
    },

    _floatVal: function(id, fallback) {
        var el = document.getElementById(id);
        if (!el) return fallback;
        var v = parseFloat(el.value);
        return isNaN(v) ? fallback : v;
    },

    _intVal: function(id, fallback) {
        var el = document.getElementById(id);
        if (!el) return fallback;
        var v = parseInt(el.value, 10);
        return isNaN(v) ? fallback : v;
    },

    generateRanked: async function(btn) {
        // H8: disable as first line to guard against rapid double-clicks.
        if (this.state.generating) {
            App.toast('A generation is already running', 'info');
            return;
        }
        if (btn) btn.disabled = true;

        var prompt = (document.getElementById('al-prompt') || {}).value || '';
        prompt = prompt.trim();
        if (!prompt) { App.toast('Enter a prompt', 'error'); if (btn) btn.disabled = false; return; }

        var live = this.state.live || {};
        if (!live.running || !live.sa || !live.clap) {
            App.toast('Load Stable Audio + CLAP worker models first', 'error');
            if (btn) btn.disabled = false;
            return;
        }

        var seedStr = (document.getElementById('al-seed') || {}).value || '';
        var seed = seedStr === '' ? null : parseInt(seedStr, 10);
        if (seedStr !== '' && isNaN(seed)) seed = null;

        var scorePromptRaw = (document.getElementById('al-score-prompt') || {}).value || '';
        scorePromptRaw = scorePromptRaw.trim();
        var maxDur = this._durationMax(47);

        var body = {
            prompt: prompt,
            negative_prompt: ((document.getElementById('al-neg-prompt') || {}).value || '').trim() || null,
            duration_s: Math.min(this._floatVal('al-duration', this._durationDefault(maxDur)), maxDur),
            steps:      this._intVal('al-steps', 100),
            cfg_scale:  this._floatVal('al-cfg', 7.0),
            sigma_min:  this._floatVal('al-sigma-min', 0.3),
            sigma_max:  this._floatVal('al-sigma-max', 500),
            sampler:    (document.getElementById('al-sampler') || {}).value || null,
            n:          this._intVal('al-n', 4),
        };
        if (seed !== null) body.seed = seed;
        if (scorePromptRaw) body.score_prompt = scorePromptRaw;
        body = this._withWorkerId(body);

        this.state.generating = true;
        var orig = btn.textContent;
        btn.textContent = 'Generating…';
        var resultsEl = document.getElementById('audio-lab-results');
        if (resultsEl) {
            resultsEl.innerHTML =
                '<div class="empty-state" id="audio-lab-progress-box">' +
                '<div id="audio-lab-progress-label">Generating ' + body.n + ' candidate' +
                (body.n > 1 ? 's' : '') + '…</div>' +
                '<div id="audio-lab-progress-bar-wrap" style="margin-top:8px;height:6px;background:rgba(255,255,255,0.08);border-radius:3px;overflow:hidden">' +
                  '<div id="audio-lab-progress-bar" style="height:100%;width:0%;background:#3fb950;transition:width 0.3s"></div>' +
                '</div>' +
                '<button class="btn btn-danger btn-sm" style="margin-top:10px" ' +
                'onclick="TabAudioLab.cancelInFlight(this)">Cancel</button></div>';
        }
        this._startProgressPolling();

        try {
            var data = await App.api('POST', '/api/audio_lab/generate-ranked', body);
            this.state.lastJob = data;
            this.renderResults();
            var n = (data.results || []).length;
            var note = '';
            if (data.cancelled) note = ' (cancelled before all candidates finished)';
            else if (data.n_completed != null && data.n_requested != null
                     && data.n_completed < data.n_requested) {
                note = ' (' + data.n_completed + '/' + data.n_requested + ' completed)';
            }
            App.toast('Generated ' + n + ' candidate' + (n === 1 ? '' : 's') + note,
                      data.cancelled ? 'info' : 'success');
        } catch (err) {
            App.toast('Generation failed: ' + err.message, 'error');
            if (resultsEl) {
                resultsEl.innerHTML = '<div class="empty-state">Failed: ' + App.esc(err.message) + '</div>';
            }
        } finally {
            this.state.generating = false;
            this._stopProgressPolling();
            if (btn && document.body.contains(btn)) {
                btn.disabled = false;
                btn.textContent = orig;
            }
        }
    },

    renderResults: function() {
        var el = document.getElementById('audio-lab-results');
        var actionsEl = document.getElementById('audio-lab-results-actions');
        if (!el) return;
        var job = this.state.lastJob;
        if (!job || !job.results || !job.results.length) {
            el.innerHTML = '<div class="empty-state">No results yet.</div>';
            if (actionsEl) actionsEl.innerHTML = '';
            return;
        }
        if (actionsEl) {
            actionsEl.innerHTML =
                '<a class="btn btn-sm" href="' +
                  App.urlWithToken('/api/audio_lab/zip/' + job.job_id) +
                  '" download="' + App.esc(job.job_id) + '.zip">Download ZIP</a>';
        }
        var ranked = job.results.length > 1;
        var scoringFailed = !!job.scoring_failed ||
            (ranked && job.results.every(function(r) { return !!r.score_failed; }));
        var header = ranked
            ? 'Job <code>' + App.esc(job.job_id) + '</code> - ' +
              job.results.length + (scoringFailed
                ? ' waveform variants. CLAP scoring failed; candidates are unranked.'
                : ' waveform variants from one base seed, ranked by CLAP score. Best is highlighted.')
            : 'Job <code>' + App.esc(job.job_id) + '</code>' +
              (job.mode ? ' - mode <strong>' + App.esc(job.mode) + '</strong>' : '');
        var html = '<div class="section-title" style="margin-bottom:10px">' + header + '</div>';
        if (job.failures && job.failures.length) {
            html += App.prereqBanner({
                level: 'warn',
                text: 'CLAP detail: ' + String(job.failures[0].detail || job.failures[0].stage || 'scoring failed').slice(0, 280),
            });
        }
        for (var i = 0; i < job.results.length; i++) {
            var r = job.results[i];
            var url = App.urlWithToken(r.url);
            var bestRibbon = (ranked && r.best)
                ? '<div style="background:rgba(46,160,67,0.18);color:#3fb950;padding:2px 8px;' +
                  'border-radius:4px;font-size:0.85em;font-weight:600">★ BEST</div>'
                : '';
            var borderColor = (ranked && r.best) ? '#3fb950' : 'var(--border, #222)';
            html += '<div style="display:flex;align-items:center;gap:12px;padding:10px;' +
                'border:1px solid ' + borderColor + ';border-radius:6px;margin-bottom:8px">' +
              (ranked
                ? '<div style="flex:0 0 56px;text-align:center;font-size:1.4em;font-weight:600">#' + r.rank + '</div>'
                : '') +
              '<div style="flex:0 0 110px">' + bestRibbon +
                (r.score != null
                    ? '<div class="section-title">score ' + r.score.toFixed(4) + '</div>'
                    : (r.score_failed
                        ? '<div class="section-title" title="' + App.esc(r.score_error || '') + '">score failed</div>'
                        : '')) +
                (r.seed != null
                    ? '<div class="section-title">base seed ' + r.seed +
                      (r.waveform_index != null ? ' · waveform ' + r.waveform_index : '') +
                      '</div>'
                    : '') +
                (r.diff_rms != null
                    ? '<div class="section-title">RMS Δ ' + r.diff_rms.toFixed(5) + '</div>'
                    : '') +
                (r.method
                    ? '<div class="section-title">' + App.esc(r.method) + '</div>'
                    : '') +
              '</div>' +
              '<audio controls src="' + App.esc(url) + '" style="flex:1;min-width:200px"></audio>' +
              '<a class="btn btn-sm" href="' + App.esc(url) + '" download="' + App.esc(r.filename) + '">Download</a>' +
              // FRO media-1: dynamic values go through data-* (App.esc-correct
              // attribute context) and are read via this.dataset at click time.
              '<button class="btn btn-sm" data-url="' + App.esc(r.url) +
                '" data-name="' + App.esc(r.filename) + '"' +
                ' onclick="TabAudioLab.useAsInitFromUrl(this.dataset.url, this.dataset.name)">Use as init</button>' +
              (r.seed != null
                ? '<button class="btn btn-sm" data-seed="' + App.esc(r.seed) +
                    '" onclick="TabAudioLab.useAsSeed(this.dataset.seed)">Use seed</button>'
                : '') +
            '</div>';
        }
        el.innerHTML = html;
    },

    useAsSeed: function(seed) {
        var el = document.getElementById('al-seed');
        if (el) {
            el.value = seed;
            App.toast('Seed ' + seed + ' will be used for the next run', 'info');
        } else {
            App.toast('Seed input not present in this mode (only Generate/A2A/Inpaint/Uncond)', 'error');
        }
    },

    _startProgressPolling: function() {
        this._stopProgressPolling();
        var self = this;
        this.state.progressHandle = setInterval(async function() {
            try {
                var p = await App.api('GET', '/api/audio_lab/progress');
                if (!p || !p.active) return;
                var labelEl = document.getElementById('audio-lab-progress-label');
                var barEl   = document.getElementById('audio-lab-progress-bar');
                if (labelEl) labelEl.textContent = p.label || (p.mode || 'Working') + '…';
                if (barEl) {
                    var pct = (p.total > 0)
                        ? Math.max(2, Math.min(100, Math.round(((p.current + 0.5) / p.total) * 100)))
                        : 5;
                    barEl.style.width = pct + '%';
                }
            } catch (err) {
                self._stopProgressPolling();
            }
        }, 1500);
    },

    _stopProgressPolling: function() {
        if (this.state.progressHandle) {
            clearInterval(this.state.progressHandle);
            this.state.progressHandle = null;
        }
    },

    cancelInFlight: async function(btn) {
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Cancelling…';
        }
        try {
            var data = await App.api('POST', '/api/audio_lab/cancel' + (this._activeWorkerId() ? '?worker_id=' + encodeURIComponent(this._activeWorkerId()) : ''));
            if (data && data.cancelled) {
                App.toast('Cancel requested — finishing current step then stopping', 'info');
            } else {
                App.toast('No active inference to cancel', 'info');
            }
        } catch (err) {
            App.toast('Cancel failed: ' + err.message, 'error');
        }
    },

    // ── A2A / Inpaint / VAE share an init-audio uploader ──
    // M10: bound the in-browser audio buffer size. A 100MB+ wav OOMs the tab.
    AUDIO_UPLOAD_MAX_BYTES: 100 * 1024 * 1024,

    handleAudioUpload: function(inputEl, displayElId) {
        var file = inputEl.files && inputEl.files[0];
        if (!file) return;
        if (file.size > this.AUDIO_UPLOAD_MAX_BYTES) {
            App.toast('Audio too large (' + (file.size / 1024 / 1024).toFixed(0) +
                'MB). Max is ' + (this.AUDIO_UPLOAD_MAX_BYTES / 1024 / 1024) + 'MB.', 'error');
            inputEl.value = '';
            return;
        }
        var self = this;
        var reader = new FileReader();
        reader.onload = function(e) {
            var dataUrl = e.target.result || '';
            var comma = dataUrl.indexOf(',');
            self.state.initAudioB64 = comma >= 0 ? dataUrl.slice(comma + 1) : '';
            self.state.initAudioName = file.name;
            var disp = document.getElementById(displayElId);
            if (disp) disp.textContent = file.name + ' (' + (file.size / 1024).toFixed(0) + ' KB)';
        };
        reader.onerror = function() {
            App.toast('Failed to read audio file', 'error');
        };
        reader.readAsDataURL(file);
    },

    useAsInitFromUrl: async function(url, filename) {
        try {
            var resp = await fetch(App.urlWithToken(url));
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            var blob = await resp.blob();
            if (blob.size > this.AUDIO_UPLOAD_MAX_BYTES) {
                App.toast('Audio too large to load as init (' +
                    (blob.size / 1024 / 1024).toFixed(0) + 'MB)', 'error');
                return;
            }
            var reader = new FileReader();
            var self = this;
            reader.onload = function(e) {
                var dataUrl = e.target.result || '';
                var comma = dataUrl.indexOf(',');
                self.state.initAudioB64 = comma >= 0 ? dataUrl.slice(comma + 1) : '';
                self.state.initAudioName = filename;
                App.toast('Loaded ' + filename + ' as init. Switch to A2A / Inpaint / VAE / Score to use it.', 'info');
                // Update any currently-rendered init-audio badge in place
                // (doesn't wipe form input). Each mode has its own status ID.
                ['al-a2a', 'al-inpaint', 'al-vae', 'al-score'].forEach(function(prefix) {
                    self._updateInitAudioStatus(prefix);
                });
            };
            reader.onerror = function() {
                App.toast('Failed to read fetched audio blob', 'error');
            };
            reader.readAsDataURL(blob);
        } catch (err) {
            App.toast('Failed to load as init: ' + err.message, 'error');
        }
    },

    _initAudioStatusHtml: function(idPrefix) {
        var name = this.state.initAudioName;
        var line = name
            ? '<span class="badge badge-green">Loaded:</span> ' + App.esc(name)
            : '<span class="badge badge-gray">No audio loaded</span>';
        return '<div>' +
            '<label class="section-title" style="display:block;margin-bottom:4px">Init audio</label>' +
            '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">' +
                '<input type="file" id="' + idPrefix + '-file" accept="audio/*" ' +
                  'onchange="TabAudioLab.handleAudioUpload(this, \'' + idPrefix + '-status\')">' +
                '<span id="' + idPrefix + '-status">' + line + '</span>' +
            '</div></div>';
    },

    _sharedGenFieldsHtml: function() {
        var maxDur = this._durationMax(47);
        var defaultDur = this._durationDefault(maxDur);
        return (
            '<div class="field-grid">' +
                App.field({ type: 'slider', id: 'al-duration', label: 'Duration', min: 1, max: maxDur, step: 0.5, value: defaultDur, unit: 's',
                    hint: 'Length of the clip in seconds.' }) +
                App.field({ type: 'slider', id: 'al-steps', label: 'Steps', min: 10, max: 500, step: 5, value: 100,
                    hint: 'Denoising steps. More = cleaner but slower.' }) +
                App.field({ type: 'slider', id: 'al-cfg', label: 'CFG scale', min: 0, max: 20, step: 0.1, value: 7,
                    tip: 'Classifier-Free Guidance: how strongly the audio follows the prompt. Higher sticks closer to the text but can sound forced.',
                    hint: 'Prompt adherence strength.' }) +
                App.field({ type: 'slider', id: 'al-sigma-min', label: 'Sigma min (native models)', min: 0, max: 10, step: 0.01, value: 0.3,
                    tip: this.SIGMA_TIP, hint: 'Lowest noise level (end of schedule).' }) +
                App.field({ type: 'slider', id: 'al-sigma-max', label: 'Sigma max (native models)', min: 50, max: 1000, step: 10, value: 500,
                    tip: this.SIGMA_TIP, hint: 'Highest noise level (start of schedule).' }) +
            '</div>' +
            '<div class="field-grid">' +
                App.field({ type: 'select', id: 'al-sampler', label: 'Sampler (native models)', options: this._samplerOptions(),
                    value: 'dpmpp-3m-sde',
                    tip: 'The numerical solver that turns noise into audio. The default is a good all-rounder.',
                    hint: 'Solver algorithm.' }) +
                App.field({ type: 'number', id: 'al-seed', label: 'Seed', min: 0, step: 1, placeholder: 'random',
                    hint: 'Blank = random each run. Set a number to reproduce a result.' }) +
            '</div>'
        );
    },

    _readSharedGenBody: function() {
        var seedStr = (document.getElementById('al-seed') || {}).value || '';
        var seed = seedStr === '' ? null : parseInt(seedStr, 10);
        if (seedStr !== '' && isNaN(seed)) seed = null;
        var maxDur = this._durationMax(47);
        var body = {
            duration_s: Math.min(this._floatVal('al-duration', this._durationDefault(maxDur)), maxDur),
            steps:      this._intVal('al-steps', 100),
            cfg_scale:  this._floatVal('al-cfg', 7.0),
            sigma_min:  this._floatVal('al-sigma-min', 0.3),
            sigma_max:  this._floatVal('al-sigma-max', 500),
            sampler:    (document.getElementById('al-sampler') || {}).value || null,
        };
        if (seed !== null) body.seed = seed;
        return body;
    },

    _runInference: async function(btn, endpoint, body, modeLabel) {
        // H8: disable+latch immediately.
        if (this.state.generating) {
            App.toast('A run is already in progress', 'info');
            return;
        }
        if (btn) btn.disabled = true;
        if (!this.state.live || !this.state.live.running || !this.state.live.sa) {
            App.toast('Load a Stable Audio worker model first', 'error');
            if (btn) btn.disabled = false;
            return;
        }
        this.state.generating = true;
        var orig = btn ? btn.textContent : '';
        if (btn) btn.textContent = modeLabel + '…';
        var resultsEl = document.getElementById('audio-lab-results');
        if (resultsEl) {
            resultsEl.innerHTML =
                '<div class="empty-state" id="audio-lab-progress-box">' +
                '<div id="audio-lab-progress-label">Running ' + modeLabel + '…</div>' +
                '<div id="audio-lab-progress-bar-wrap" style="margin-top:8px;height:6px;background:rgba(255,255,255,0.08);border-radius:3px;overflow:hidden">' +
                  '<div id="audio-lab-progress-bar" style="height:100%;width:0%;background:#3fb950;transition:width 0.3s"></div>' +
                '</div>' +
                '<button class="btn btn-danger btn-sm" style="margin-top:10px" ' +
                'onclick="TabAudioLab.cancelInFlight(this)">Cancel</button></div>';
        }
        this._startProgressPolling();
        try {
            body = this._withWorkerId(body);
            var data = await App.api('POST', endpoint, body);
            this.state.lastJob = data;
            this.renderResults();
            App.toast(modeLabel + ' done', 'success');
        } catch (err) {
            App.toast(modeLabel + ' failed: ' + err.message, 'error');
            if (resultsEl) {
                resultsEl.innerHTML = '<div class="empty-state">Failed: ' + App.esc(err.message) + '</div>';
            }
        } finally {
            this.state.generating = false;
            this._stopProgressPolling();
            if (btn && document.body.contains(btn)) {
                btn.disabled = false;
                btn.textContent = orig;
            }
        }
    },

    // ── A2A form ──
    renderA2AForm: function() {
        var el = document.getElementById('audio-lab-mode-form');
        if (!el) return;
        var disabled = !this._saLoaded();
        el.innerHTML =
            '<div style="display:flex;flex-direction:column;gap:10px">' +
              '<div class="section-title">Generate a variation from an init clip + prompt.</div>' +
              this._modePrereqBanner('al-a2a-prereq-load') +
              this._initAudioStatusHtml('al-a2a') +
              App.field({
                  type: 'textarea', id: 'al-prompt', label: 'Prompt', rows: 2,
                  hint: 'Describe the variation you want from the init clip.',
              }) +
              App.field({
                  type: 'text', id: 'al-neg-prompt', label: 'Negative prompt (optional)',
                  hint: 'Qualities to steer away from.',
              }) +
              '<div class="field-grid">' +
                App.field({ type: 'slider', id: 'al-init-noise',
                    label: 'Preserve original 0 ↔ Reinterpret 1',
                    min: 0, max: 1, step: 0.01, value: 0.7,
                    tip: 'How far to move away from the init clip. Near 0 stays faithful to the original; near 1 mostly reinterprets it from the prompt.',
                    hint: 'Lower keeps the source; higher follows the prompt.' }) +
              '</div>' +
              this._sharedGenFieldsHtml() +
              '<div>' +
                '<button class="btn btn-primary" id="al-a2a-btn"' +
                  (disabled ? ' disabled' : '') + '>Run A2A</button>' +
                (disabled
                    ? '<span class="field-hint" style="margin-left:10px">Load a Stable Audio model first.</span>'
                    : '') +
              '</div>' +
            '</div>';
        var btn = document.getElementById('al-a2a-btn');
        if (btn) btn.addEventListener('click', function() { TabAudioLab.submitA2A(btn); });
        var prereqBtn = document.getElementById('al-a2a-prereq-load');
        if (prereqBtn) prereqBtn.addEventListener('click', function() { TabAudioLab.focusLoadPanel(); });
    },

    // Shared "load a Stable Audio model" prereq banner for the init-driven
    // modes (A2A / Inpaint / Uncond). Returns '' when a model is loaded.
    _modePrereqBanner: function(actionId) {
        if (this._saLoaded()) return '';
        var hasWorker = !!((this.state.live || {}).running);
        return App.prereqBanner({
            level: 'warn',
            text: hasWorker
                ? 'This mode needs Stable Audio loaded on the selected worker.'
                : 'This mode needs an Audio Lab worker running.',
            actionId: actionId,
            actionLabel: hasWorker ? 'Configure worker' : 'Start worker',
        });
    },

    submitA2A: function(btn) {
        var prompt = ((document.getElementById('al-prompt') || {}).value || '').trim();
        if (!prompt) { App.toast('Enter a prompt', 'error'); return; }
        if (!this.state.initAudioB64) { App.toast('Upload an init audio clip', 'error'); return; }
        var body = this._readSharedGenBody();
        body.prompt = prompt;
        var neg = ((document.getElementById('al-neg-prompt') || {}).value || '').trim();
        if (neg) body.negative_prompt = neg;
        body.init_audio_base64 = this.state.initAudioB64;
        body.init_noise_level = this._floatVal('al-init-noise', 0.7);
        this._runInference(btn, '/api/audio_lab/a2a', body, 'A2A');
    },

    // ── Inpaint form ──
    renderInpaintForm: function() {
        var el = document.getElementById('audio-lab-mode-form');
        if (!el) return;
        var disabled = !this._saLoaded();
        var maxDur = this._durationMax(47);
        el.innerHTML =
            '<div style="display:flex;flex-direction:column;gap:10px">' +
              '<div class="section-title">Regenerate a time range of an existing clip ' +
                '<em>(Phase 3 uses a crossfade-splice fallback; native latent inpaint arrives in Phase 4)</em>.</div>' +
              this._modePrereqBanner('al-inpaint-prereq-load') +
              this._initAudioStatusHtml('al-inpaint') +
              App.field({
                  type: 'textarea', id: 'al-prompt', label: 'Prompt', rows: 2,
                  hint: 'What the regenerated section should sound like.',
              }) +
              App.field({
                  type: 'text', id: 'al-neg-prompt', label: 'Negative prompt (optional)',
                  hint: 'Qualities to steer away from.',
              }) +
              '<div class="field-grid">' +
                App.field({ type: 'slider', id: 'al-mask-start', label: 'Mask start', min: 0, max: maxDur, step: 0.1, value: Math.min(2, maxDur), unit: 's',
                    hint: 'Where the regenerated region begins.' }) +
                App.field({ type: 'slider', id: 'al-mask-end', label: 'Mask end', min: 0, max: maxDur, step: 0.1, value: Math.min(5, maxDur), unit: 's',
                    hint: 'Where the regenerated region ends (must be after start).' }) +
              '</div>' +
              this._sharedGenFieldsHtml() +
              '<div>' +
                '<button class="btn btn-primary" id="al-inpaint-btn"' +
                  (disabled ? ' disabled' : '') + '>Run Inpaint</button>' +
                (disabled
                    ? '<span class="field-hint" style="margin-left:10px">Load a Stable Audio model first.</span>'
                    : '') +
              '</div>' +
            '</div>';
        var btn = document.getElementById('al-inpaint-btn');
        if (btn) btn.addEventListener('click', function() { TabAudioLab.submitInpaint(btn); });
        var prereqBtn = document.getElementById('al-inpaint-prereq-load');
        if (prereqBtn) prereqBtn.addEventListener('click', function() { TabAudioLab.focusLoadPanel(); });
    },

    submitInpaint: function(btn) {
        var prompt = ((document.getElementById('al-prompt') || {}).value || '').trim();
        if (!prompt) { App.toast('Enter a prompt', 'error'); return; }
        if (!this.state.initAudioB64) { App.toast('Upload an init audio clip', 'error'); return; }
        var mStart = this._floatVal('al-mask-start', 2.0);
        var mEnd   = this._floatVal('al-mask-end', 5.0);
        if (mEnd <= mStart) { App.toast('Mask end must be greater than mask start', 'error'); return; }
        var body = this._readSharedGenBody();
        body.prompt = prompt;
        var neg = ((document.getElementById('al-neg-prompt') || {}).value || '').trim();
        if (neg) body.negative_prompt = neg;
        body.init_audio_base64 = this.state.initAudioB64;
        body.mask_start_s = mStart;
        body.mask_end_s = mEnd;
        this._runInference(btn, '/api/audio_lab/inpaint', body, 'Inpaint');
    },

    // ── Unconditional form ──
    renderUncondForm: function() {
        var el = document.getElementById('audio-lab-mode-form');
        if (!el) return;
        var disabled = !this._saLoaded();
        el.innerHTML =
            '<div style="display:flex;flex-direction:column;gap:10px">' +
              '<div class="section-title">Generate with no text guidance (free-form ambient). ' +
                'CFG is not used in this mode.</div>' +
              this._modePrereqBanner('al-uncond-prereq-load') +
              this._sharedGenFieldsHtml() +
              '<div>' +
                '<button class="btn btn-primary" id="al-uncond-btn"' +
                  (disabled ? ' disabled' : '') + '>Run Unconditional</button>' +
                (disabled
                    ? '<span class="field-hint" style="margin-left:10px">Load a Stable Audio model first.</span>'
                    : '') +
              '</div>' +
            '</div>';
        var btn = document.getElementById('al-uncond-btn');
        if (btn) btn.addEventListener('click', function() { TabAudioLab.submitUncond(btn); });
        var prereqBtn = document.getElementById('al-uncond-prereq-load');
        if (prereqBtn) prereqBtn.addEventListener('click', function() { TabAudioLab.focusLoadPanel(); });
    },

    submitUncond: function(btn) {
        var body = this._readSharedGenBody();
        delete body.cfg_scale;  // uncond ignores cfg
        this._runInference(btn, '/api/audio_lab/uncond', body, 'Uncond');
    },

    // ── VAE Lab form ──
    renderVAEForm: function() {
        var el = document.getElementById('audio-lab-mode-form');
        if (!el) return;
        var disabled = !this._saLoaded() ? ' disabled' : '';
        el.innerHTML =
            '<div style="display:flex;flex-direction:column;gap:12px">' +
              '<div class="section-title">VAE-only ops. Reconstruct round-trips audio through the autoencoder; useful for testing VAE swaps.</div>' +
              this._modePrereqBanner('al-vae-prereq-load') +
              this._initAudioStatusHtml('al-vae') +
              '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
                '<button class="btn btn-primary" id="al-vae-reconstruct-btn"' + disabled + '>Reconstruct (audio→audio)</button>' +
                '<button class="btn" id="al-vae-encode-btn"' + disabled + '>Encode (download .pt)</button>' +
                '<label class="btn" style="cursor:pointer">' +
                  'Decode .pt → audio' +
                  '<input type="file" id="al-vae-decode-file" accept=".pt,.bin" style="display:none">' +
                '</label>' +
              '</div>' +
              (disabled
                  ? '<div class="field-hint">Load a Stable Audio model (its VAE) first.</div>'
                  : '') +
            '</div>';
        var recBtn = document.getElementById('al-vae-reconstruct-btn');
        if (recBtn) recBtn.addEventListener('click', function() { TabAudioLab.submitVAEReconstruct(recBtn); });
        var encBtn = document.getElementById('al-vae-encode-btn');
        if (encBtn) encBtn.addEventListener('click', function() { TabAudioLab.submitVAEEncode(encBtn); });
        var decFile = document.getElementById('al-vae-decode-file');
        if (decFile) decFile.addEventListener('change', function() { TabAudioLab.submitVAEDecode(decFile); });
        var prereqBtn = document.getElementById('al-vae-prereq-load');
        if (prereqBtn) prereqBtn.addEventListener('click', function() { TabAudioLab.focusLoadPanel(); });
    },

    submitVAEReconstruct: function(btn) {
        if (!this.state.initAudioB64) { App.toast('Upload an init audio clip', 'error'); return; }
        var body = { audio_base64: this.state.initAudioB64 };
        this._runInference(btn, '/api/audio_lab/vae/reconstruct', body, 'VAE reconstruct');
    },

    submitVAEEncode: async function(btn) {
        if (!this.state.initAudioB64) { App.toast('Upload an init audio clip', 'error'); return; }
        btn.disabled = true;
        var orig = btn.textContent;
        btn.textContent = 'Encoding…';
        try {
            var data = await App.api('POST', '/api/audio_lab/vae/encode',
                this._withWorkerId({ audio_base64: this.state.initAudioB64 }));
            // Trigger a browser download of the latent bytes.
            var raw = atob(data.latent_base64);
            var bytes = new Uint8Array(raw.length);
            for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
            var blob = new Blob([bytes], { type: 'application/octet-stream' });
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url; a.download = (this.state.initAudioName || 'audio') + '.latent.pt';
            document.body.appendChild(a); a.click(); document.body.removeChild(a);
            URL.revokeObjectURL(url);
            App.toast('Downloaded latent (shape ' + JSON.stringify(data.shape) + ')', 'success');
        } catch (err) {
            App.toast('VAE encode failed: ' + err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = orig;
        }
    },

    submitVAEDecode: async function(inputEl) {
        var file = inputEl.files && inputEl.files[0];
        if (!file) return;
        var reader = new FileReader();
        var self = this;
        reader.onload = async function(e) {
            var dataUrl = e.target.result || '';
            var comma = dataUrl.indexOf(',');
            var b64 = comma >= 0 ? dataUrl.slice(comma + 1) : '';
            try {
                var data = await App.api('POST', '/api/audio_lab/vae/decode',
                    self._withWorkerId({ latent_base64: b64 }));
                self.state.lastJob = data;
                self.renderResults();
                App.toast('VAE decode done', 'success');
            } catch (err) {
                App.toast('VAE decode failed: ' + err.message, 'error');
            }
        };
        reader.readAsDataURL(file);
    },

    // ── CLAP Score form ──
    renderScoreForm: function() {
        var el = document.getElementById('audio-lab-mode-form');
        if (!el) return;
        var clapMissing = !this._clapLoaded();
        var banner = clapMissing
            ? App.prereqBanner({
                  level: 'warn',
                  text: 'Scoring needs a CLAP worker model loaded. Use Active Worker Models to load one.',
                  actionId: 'al-score-prereq-load',
                  actionLabel: 'Load CLAP',
              })
            : '';
        el.innerHTML =
            '<div style="display:flex;flex-direction:column;gap:10px">' +
              '<div class="section-title">Score how well an audio clip matches a text prompt ' +
                '<span>' + App.tip('CLAP measures the cosine similarity between the audio and the text — higher means a closer match.') + '</span>.</div>' +
              banner +
              this._initAudioStatusHtml('al-score') +
              App.field({
                  type: 'text', id: 'al-score-prompt-field', label: 'Text prompt',
                  placeholder: 'lofi hip hop beat with vinyl crackle',
                  hint: 'The description to compare the loaded audio against.',
              }) +
              '<div id="al-score-result" class="empty-state">No score yet.</div>' +
              '<div>' +
                '<button class="btn btn-primary" id="al-score-btn"' +
                  (clapMissing ? ' disabled' : '') + '>Score</button>' +
                (clapMissing
                    ? '<span class="field-hint" style="margin-left:10px">Load a CLAP model first.</span>'
                    : '') +
              '</div>' +
            '</div>';
        var btn = document.getElementById('al-score-btn');
        if (btn) btn.addEventListener('click', function() { TabAudioLab.submitScore(btn); });
        var prereqBtn = document.getElementById('al-score-prereq-load');
        if (prereqBtn) prereqBtn.addEventListener('click', function() { TabAudioLab.focusLoadPanel(); });
    },

    submitScore: async function(btn) {
        if (!this.state.initAudioB64) { App.toast('Upload an audio clip', 'error'); return; }
        var text = ((document.getElementById('al-score-prompt-field') || {}).value || '').trim();
        if (!text) { App.toast('Enter a text prompt', 'error'); return; }
        if (!this.state.live || !this.state.live.clap) {
            App.toast('Load a CLAP model first', 'error'); return;
        }
        btn.disabled = true;
        var orig = btn.textContent;
        btn.textContent = 'Scoring…';
        try {
            var data = await App.api('POST', '/api/audio_lab/score',
                this._withWorkerId({ text: text, audio_base64: this.state.initAudioB64 }));
            var resultEl = document.getElementById('al-score-result');
            if (resultEl) {
                var scoreStr = (typeof data.score === 'number' && isFinite(data.score))
                    ? data.score.toFixed(4)
                    : '—';
                resultEl.innerHTML = '<div style="font-size:1.3em">' +
                    'CLAP cosine similarity: <strong>' + scoreStr + '</strong>' +
                    (data.windows ? ' <span class="section-title">(averaged over ' + data.windows + ' window' +
                      (data.windows > 1 ? 's' : '') + ')</span>' : '') +
                '</div>';
            }
        } catch (err) {
            App.toast('Score failed: ' + err.message, 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = orig;
        }
    },
};
