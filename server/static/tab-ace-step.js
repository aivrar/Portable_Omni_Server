// ACE-Step tab — DiT-based song generation (1.5 family).
//
// Sub-tabs: Generate / Simple / Planner / A2A / Repaint / Edit / Extend /
//           Cover / Extract / Lego / Complete / Vocal→BGM / Lyric→Vocal /
//           Text→Samples / Jobs / Outputs / Install.
//
// Cross-worker CLAP: generate-ranked calls the audio_lab worker for scoring.
// If audio_lab isn't running with CLAP loaded, generate-ranked falls back to
// unranked output and the UI shows a notice.

var TabAceStep = {
    state: {
        mode: 'generate',       // active sub-tab
        models: [], lms: [], vaes: [], loras: [],
        custom: { model: [], lm: [], vae: [], lora: [] },
        runtime: { runtime_ready: true, runtime_missing: [] },
        core: [], core_ready: true, core_missing: [],
        live: null,             // worker /state response, or null if not running
        jobsInFlight: {},       // jobId → setInterval handle for install polls
        progressHandle: null,   // setInterval handle for inference progress
        lastJob: null,          // most recent inference job (results array)
        jobs: [],               // /api/ace_step/jobs response
        pendingInitUrl: null,   // queued init-audio URL to populate on next form render
        pendingRefUrl: null,    // queued style-reference URL for generate/cover
        pendingSeed: null,      // queued seed to populate on next form render
        plannerResult: null,
    },

    MODES: [
        ['generate',     'Generate'],
        ['simple',       'Simple'],
        ['planner',      'Planner'],
        ['a2a',          'A2A'],
        ['repaint',      'Repaint'],
        ['edit',         'Edit'],
        ['extend',       'Extend'],
        ['cover',        'Cover'],
        ['extract',      'Extract'],
        ['lego',         'Lego'],
        ['complete',     'Complete'],
        ['vocal2bgm',    'Vocal→BGM'],
        ['lyric2vocal',  'Lyric→Vocal'],
        ['text2samples', 'Text→Samples'],
        ['jobs',         'Jobs'],
        ['outputs',      'Outputs'],
        ['install',      'Install'],
    ],

    TRACK_NAMES: [
        'woodwinds', 'brass', 'fx', 'synth', 'strings', 'percussion',
        'keyboard', 'guitar', 'bass', 'drums', 'backing_vocals', 'vocals',
    ],

    LANGUAGES: [
        ['en', 'English'], ['zh', 'Chinese'], ['yue', 'Cantonese'],
        ['ja', 'Japanese'], ['ko', 'Korean'], ['es', 'Spanish'],
        ['fr', 'French'], ['de', 'German'], ['it', 'Italian'],
        ['pt', 'Portuguese'], ['ru', 'Russian'], ['ar', 'Arabic'],
        ['hi', 'Hindi'], ['th', 'Thai'], ['vi', 'Vietnamese'],
        ['id', 'Indonesian'], ['tr', 'Turkish'], ['pl', 'Polish'],
        ['nl', 'Dutch'], ['sv', 'Swedish'], ['no', 'Norwegian'],
        ['da', 'Danish'], ['fi', 'Finnish'], ['cs', 'Czech'],
        ['sk', 'Slovak'], ['hu', 'Hungarian'], ['ro', 'Romanian'],
        ['bg', 'Bulgarian'], ['uk', 'Ukrainian'], ['el', 'Greek'],
        ['he', 'Hebrew'], ['fa', 'Persian'], ['ur', 'Urdu'],
        ['bn', 'Bengali'], ['ta', 'Tamil'], ['te', 'Telugu'],
        ['pa', 'Punjabi'], ['ms', 'Malay'], ['tl', 'Tagalog'],
        ['sw', 'Swahili'], ['hr', 'Croatian'], ['sr', 'Serbian'],
        ['lt', 'Lithuanian'], ['ca', 'Catalan'], ['az', 'Azerbaijani'],
        ['is', 'Icelandic'], ['ne', 'Nepali'], ['sa', 'Sanskrit'],
        ['la', 'Latin'], ['ht', 'Haitian Creole'],
        ['unknown', 'Instrumental / auto'],
    ],

    init: function() {
        var el = document.getElementById('tab-ace-step');
        if (!el) return;
        el.innerHTML = this._baseHtml();
        // Load registry status + live worker state in parallel.
        this.refreshStatus();
        this.refreshLiveState();
        // Re-fetch on tab activation.
        var self = this;
        document.querySelectorAll('.tab-btn').forEach(function(btn) {
            if (btn.dataset.tab === 'ace-step') {
                btn.addEventListener('click', function() {
                    self.refreshStatus(); self.refreshLiveState();
                });
            }
        });
        this.setMode('generate');
    },

    _baseHtml: function() {
        var nav = this.MODES.map(function(m) {
            return '<button class="btn btn-sm" data-mode="' + m[0] +
                   '" onclick="TabAceStep.setMode(\'' + m[0] + '\')">' + m[1] + '</button>';
        }).join(' ');
        return '' +
            '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">' +
              '<div>' +
                '<h1 style="margin:0">ACE Step</h1>' +
                '<div class="section-title">DiT-based 48 kHz stereo song generation (ACE-Step 1.5)</div>' +
              '</div>' +
              '<div>' +
                '<button class="btn btn-sm" onclick="TabAceStep.refreshAll()">Refresh</button>' +
              '</div>' +
            '</div>' +

            '<div class="card" id="ace-step-live-card">' +
              '<div id="ace-step-live" class="empty-state">Loading worker state…</div>' +
            '</div>' +

            '<div style="margin:12px 0;display:flex;gap:6px;flex-wrap:wrap" id="ace-step-modes">' + nav + '</div>' +

            '<div id="ace-step-view"></div>';
    },

    refreshAll: function() { this.refreshStatus(); this.refreshLiveState(); },

    refreshStatus: async function() {
        try {
            var data = await App.api('GET', '/api/ace_step/status');
            this.state.models = data.models || [];
            this.state.lms    = data.lms    || [];
            this.state.vaes   = data.vaes   || [];
            this.state.loras  = data.loras  || [];
            this.state.custom = data.custom || { model: [], lm: [], vae: [], lora: [] };
            this.state.runtime = data.runtime || { runtime_ready: true, runtime_missing: [] };
            this.state.core = data.core || [];
            this.state.core_ready = data.core_ready !== false;
            this.state.core_missing = data.core_missing || [];
        } catch (err) {
            App.toast('Failed to load ACE-Step status: ' + (err && err.message ? err.message : err), 'error');
            return;
        }
        this._renderLive();
        if (this.state.mode === 'install') this._renderInstall();
    },

    refreshLiveState: async function() {
        var prevLoaded = this._modelLoaded();
        try {
            var s = await App.api('GET', '/api/ace_step/state');
            this.state.live = s;
        } catch (err) {
            this.state.live = { running: false, error: err && err.message };
        }
        this._renderLive();
        // If model-loaded readiness flipped while a generation form is showing,
        // re-render that form so the prereq banner / disabled-submit state and
        // the model-default placeholders update. We avoid re-rendering on every
        // poll (that would wipe user input) — only when readiness changed.
        var nowLoaded = this._modelLoaded();
        if (nowLoaded !== prevLoaded &&
            this._isGenerationMode(this.state.mode)) {
            var view = document.getElementById('ace-step-view');
            if (view) {
                this.setMode(this.state.mode);
            }
        }
    },

    // True if the worker is up AND a base model is loaded (the prerequisite for
    // any generation call). Anything else means "Load a model first".
    _modelLoaded: function() {
        var s = this.state.live || {};
        return !!(s.running && s.model_loaded);
    },

    _isGenerationMode: function(mode) {
        return ['generate', 'simple', 'planner', 'a2a', 'repaint', 'edit', 'extend',
                'cover', 'extract', 'lego', 'complete', 'vocal2bgm',
                'lyric2vocal', 'text2samples'].indexOf(mode) >= 0;
    },

    _runtimeReady: function() {
        return !this.state.runtime || this.state.runtime.runtime_ready !== false;
    },

    _coreReady: function() {
        return this.state.core_ready !== false;
    },

    _hasInstalledNativeModel: function() {
        return (this.state.models || []).some(function(m) {
            return m.installed && m.format === 'native';
        });
    },

    _renderLive: function() {
        var el = document.getElementById('ace-step-live');
        if (!el) return;
        if (!this._runtimeReady()) {
            var missing = (this.state.runtime.runtime_missing || []).join(', ');
            el.innerHTML = App.prereqBanner({
                level: 'warn',
                text: 'ACE-Step runtime dependencies are missing: ' + missing + '. Run the app setup/venv repair before loading.',
                actionId: '',
                actionLabel: '',
            });
            return;
        }
        var coreWarn = '';
        if (!this._coreReady() && this._hasInstalledNativeModel()) {
            var coreMissing = (this.state.core_missing || []).join(', ');
            coreWarn = App.prereqBanner({
                level: 'warn',
                text: 'ACE-Step shared v1.5 core is missing: ' + coreMissing + '. New native model installs include it automatically; install it now for already-downloaded models.',
                actionId: 'ace-step-open-install-core',
                actionLabel: 'Install shared core',
            });
        }
        var s = this.state.live || {};
        if (!s.running) {
            el.innerHTML =
                coreWarn +
                '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
                  '<span class="badge badge-gray">Worker: not running</span>' +
                  '<span class="section-title">Click "Load…" below to start generating.</span>' +
                  '<button class="btn btn-sm btn-primary" onclick="TabAceStep.openLoadModal()">Load…</button>' +
                '</div>';
            this._bindCoreAction();
            return;
        }
        var loras = (s.loras || []).map(function(l) {
            return '<span class="badge badge-purple" title="multiplier: ' + l.multiplier + '">' +
                   App.esc(l.name) + ' (' + l.multiplier + ')</span>';
        }).join(' ');
        var clapBadge = s.clap_available
            ? '<span class="badge badge-green" title="audio_lab worker is up with CLAP loaded — generate-ranked will use it">CLAP via audio_lab: ' + App.esc(s.clap_variant || 'loaded') + '</span>'
            : '<span class="badge badge-orange" title="generate-ranked will fall back to unranked output">CLAP via audio_lab: unavailable</span>';
        el.innerHTML =
            coreWarn +
            '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
              '<span class="badge badge-green">Worker: ready</span>' +
              (s.model_loaded
                ? '<span class="badge badge-green">model: ' + App.esc(this._modelDisplay(s.model_variant)) + '</span>'
                : '<span class="badge badge-gray">no model loaded</span>') +
              (s.lm_loaded
                ? '<span class="badge badge-green">LM: ' + App.esc(this._lmDisplay(s.lm_variant)) + '</span>'
                : '<span class="badge badge-gray">no LM loaded</span>') +
              (s.vae_swap
                ? '<span class="badge badge-purple">VAE swap: ' + App.esc(s.vae_swap) + '</span>'
                : '') +
              clapBadge +
              '<button class="btn btn-sm" onclick="TabAceStep.openLoadModal()">Change…</button>' +
              '<button class="btn btn-sm btn-danger" onclick="TabAceStep.unloadAll()">Unload all</button>' +
            '</div>' +
            (loras ? '<div style="margin-top:6px"><strong class="section-title">Attached LoRAs:</strong> ' + loras + '</div>' : '') +
            this._loraAttachHtml();
        this._bindCoreAction();
        this._bindLoraAdapterOptions();
    },

    // Friendly display-name lookups so the primary view never shows raw IDs.
    _modelDisplay: function(variantId) {
        if (!variantId) return variantId;
        var hit = (this.state.models || []).filter(function(m) { return m.variant_id === variantId; })[0];
        if (!hit && variantId.indexOf('custom:') === 0) {
            return 'Custom model: ' + variantId.split(':', 2)[1];
        }
        return hit && hit.display ? hit.display : variantId;
    },
    _lmDisplay: function(variantId) {
        if (!variantId) return variantId;
        var hit = (this.state.lms || []).filter(function(l) { return l.variant_id === variantId; })[0];
        if (!hit && variantId.indexOf('custom:') === 0) {
            return 'Custom LM: ' + variantId.split(':', 2)[1];
        }
        return hit && hit.display ? hit.display : variantId;
    },
    _modelStatus: function(variantId) {
        return (this.state.models || []).filter(function(m) { return m.variant_id === variantId; })[0] || null;
    },
    _installedLoraOptions: function() {
        var rows = (this.state.loras || []).filter(function(l) {
            return l.installed && l.available !== false;
        }).map(function(l) {
            return {
                name: l.name,
                display: l.display || l.name,
                adapter_files: l.adapter_files || [],
            };
        });
        ((this.state.custom && this.state.custom.lora) || []).forEach(function(row) {
            if (!rows.some(function(r) { return r.name === row.name; })) {
                rows.push({ name: row.name, display: 'Custom: ' + row.name, adapter_files: [] });
            }
        });
        return rows;
    },

    _loraAttachHtml: function() {
        if (!this._modelLoaded()) return '';
        var packs = this._installedLoraOptions();
        if (!packs.length) {
            return '<div class="section-title" style="margin-top:8px">No installed LoRAs to attach. Use the Install tab.</div>';
        }
        var nameOpts = packs.map(function(p) {
            return { value: p.name, label: p.display };
        });
        var first = packs[0];
        var fileOpts = [{ value: '', label: 'Default adapter file' }].concat(
            (first.adapter_files || []).map(function(f) {
                return { value: f.filename, label: f.display || f.filename };
            })
        );
        var attached = ((this.state.live || {}).loras || []);
        var detachOpts = attached.length
            ? attached.map(function(l) { return { value: l.name, label: l.name + ' (' + l.multiplier + ')' }; })
            : [{ value: '', label: 'None attached' }];
        return '' +
            this._section('LoRA attach') +
            '<div class="field-grid">' +
              App.field({
                  type: 'select', id: 'ace-step-lora-name',
                  label: 'Installed LoRA',
                  options: nameOpts, value: first.name,
                  hint: 'PEFT adapters already on disk. Techno XL is ryanontheinside/techno-acestep1.5-xl-v1 on Install.',
              }) +
              App.field({
                  type: 'select', id: 'ace-step-lora-file',
                  label: 'Adapter file',
                  options: fileOpts, value: '',
                  hint: 'Required when the pack uses a non-standard filename such as techno-xl-v1.safetensors.',
              }) +
              App.field({
                  type: 'slider', id: 'ace-step-lora-mult',
                  label: 'Multiplier',
                  min: 0, max: 2, step: 0.05, value: 1,
                  hint: 'Official ACE scale. 1.0 is full strength. 0.4–0.7 is safer on vocal packs.',
              }) +
              App.field({
                  type: 'select', id: 'ace-step-lora-detach-name',
                  label: 'Detach',
                  options: detachOpts, value: attached.length ? attached[0].name : '',
              }) +
            '</div>' +
            '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">' +
              '<button class="btn btn-sm btn-primary" onclick="TabAceStep.attachLora()">Attach</button>' +
              '<button class="btn btn-sm" onclick="TabAceStep.detachLora()">Detach selected</button>' +
            '</div></div>';
    },

    _bindLoraAdapterOptions: function() {
        var sel = document.getElementById('ace-step-lora-name');
        var fileSel = document.getElementById('ace-step-lora-file');
        if (!sel || !fileSel) return;
        var self = this;
        sel.addEventListener('change', function() {
            var pack = self._installedLoraOptions().filter(function(p) { return p.name === sel.value; })[0];
            var files = (pack && pack.adapter_files) || [];
            fileSel.innerHTML = '<option value="">Default adapter file</option>' + files.map(function(f) {
                return '<option value="' + App.esc(f.filename) + '">' + App.esc(f.display || f.filename) + '</option>';
            }).join('');
        });
    },

    attachLora: async function() {
        var nameEl = document.getElementById('ace-step-lora-name');
        if (!nameEl || !nameEl.value) { App.toast('Pick a LoRA first', 'error'); return; }
        var body = { name: nameEl.value, multiplier: 1 };
        var mult = document.getElementById('ace-step-lora-mult');
        if (mult && mult.value !== '') body.multiplier = parseFloat(mult.value);
        var fileEl = document.getElementById('ace-step-lora-file');
        if (fileEl && fileEl.value) body.adapter_file = fileEl.value;
        try {
            await App.api('POST', '/api/ace_step/lora/attach', body);
            App.toast('LoRA attached.', 'success');
            this.refreshLiveState();
        } catch (err) {
            App.toast('Attach failed: ' + (err && err.message ? err.message : err), 'error');
        }
    },

    detachLora: async function() {
        var nameEl = document.getElementById('ace-step-lora-detach-name');
        if (!nameEl || !nameEl.value) { App.toast('Nothing to detach', 'error'); return; }
        try {
            await App.api('POST', '/api/ace_step/lora/detach', { name: nameEl.value });
            App.toast('LoRA detached.', 'success');
            this.refreshLiveState();
        } catch (err) {
            App.toast('Detach failed: ' + (err && err.message ? err.message : err), 'error');
        }
    },

    _customLoadOptions: function(kind, prefix) {
        var custom = (this.state.custom && this.state.custom[kind]) || [];
        return custom.map(function(row) {
            return {
                variant_id: 'custom:' + row.name,
                display: prefix + ': ' + row.name,
                repo: row.repo,
                installed: true,
            };
        });
    },

    setMode: function(mode) {
        // Stop any in-flight progress polling — the new view's DOM IDs may not
        // exist and we don't want orphan setInterval handles writing nowhere.
        this._stopProgressPolling();
        this.state.mode = mode;
        document.querySelectorAll('#ace-step-modes .btn').forEach(function(b) {
            if (b.dataset.mode === mode) b.classList.add('btn-primary');
            else b.classList.remove('btn-primary');
        });
        var view = document.getElementById('ace-step-view');
        if (!view) return;
        switch (mode) {
            case 'generate':     view.innerHTML = this._renderGenerateView(); break;
            case 'simple':       view.innerHTML = this._renderSimpleView(); break;
            case 'planner':      view.innerHTML = this._renderPlannerView(); break;
            case 'a2a':          view.innerHTML = this._renderInitAudioForm('a2a'); break;
            case 'extract':      view.innerHTML = this._renderInitAudioForm('extract'); break;
            case 'lego':         view.innerHTML = this._renderInitAudioForm('lego'); break;
            case 'complete':     view.innerHTML = this._renderInitAudioForm('complete'); break;
            case 'repaint':      view.innerHTML = this._renderInitAudioForm('repaint'); break;
            case 'edit':         view.innerHTML = this._renderInitAudioForm('edit'); break;
            case 'extend':       view.innerHTML = this._renderInitAudioForm('extend'); break;
            case 'cover':        view.innerHTML = this._renderInitAudioForm('cover'); break;
            case 'vocal2bgm':    view.innerHTML = this._renderInitAudioForm('vocal2bgm'); break;
            case 'lyric2vocal':  view.innerHTML = this._renderLoraModeForm('lyric2vocal'); break;
            case 'text2samples': view.innerHTML = this._renderLoraModeForm('text2samples'); break;
            case 'jobs':         view.innerHTML = this._renderJobsView(); this.refreshJobs(); break;
            case 'outputs':      view.innerHTML = this._renderOutputsView(); break;
            case 'install':      view.innerHTML = this._renderInstallView(); this._renderInstall(); break;
            default:
                // Unknown/absent mode (e.g. loading an older or unsupported job):
                // fall back to the generate view so the container is never blank,
                // and keep state.mode consistent with what's actually rendered.
                this.state.mode = 'generate';
                view.innerHTML = this._renderGenerateView();
                break;
        }
        // Wire up the prereq banner's one-click [Load model] action, if present.
        this._bindPrereqAction();
        // Apply any pending init-audio URL / seed to the newly rendered form.
        this._applyPendingAfterRender(mode);
    },

    // -----------------------------------------------------------------------
    // Load / unload modal
    // -----------------------------------------------------------------------
    openLoadModal: async function() {
        if (!this._runtimeReady()) {
            App.toast('ACE-Step runtime dependencies are missing. Run setup/venv repair first.', 'error');
            return;
        }
        if (App.loadDevices && !(App.state.devices && App.state.devices.length)) {
            try { await App.loadDevices(); } catch (err) { /* device list is optional */ }
        }
        var installedModels = this.state.models
            .filter(function(m){ return m.installed; })
            .concat(this._customLoadOptions('model', 'Custom model'));
        if (!installedModels.length) {
            App.toast('No models installed yet. Download weights on the Models tab.', 'error');
            this.setMode('install');
            return;
        }
        var loaded = (this.state.live && this.state.live.model_variant) || null;
        var loadedLm = (this.state.live && this.state.live.lm_variant) || null;
        var loadedVae = (this.state.live && this.state.live.vae_swap) || '';

        // Auto-select: keep the currently-loaded model if any, else first installed.
        var modelDefault = loaded && installedModels.some(function(m){ return m.variant_id === loaded; })
            ? loaded : installedModels[0].variant_id;
        var modelOptions = installedModels.map(function(m) {
            return { value: m.variant_id, label: m.display + (m.variant_id === loaded ? '  (loaded)' : '') };
        });

        var lmInstalled = this.state.lms
            .filter(function(l){ return l.installed; })
            .concat(this._customLoadOptions('lm', 'Custom LM'));
        var lmOptions = [{ value: '', label: '(use the model\'s recommended LM)' }].concat(
            lmInstalled.map(function(l) {
                return { value: l.variant_id, label: l.display + (l.variant_id === loadedLm ? '  (loaded)' : '') };
            }));

        var vaeOptions = this.state.vaes
            .filter(function(v){ return v.variant_id === 'default' || v.installed; })
            .map(function(v) {
                return { value: v.variant_id, label: v.display + (v.variant_id === loadedVae ? '  (active)' : '') };
            });

        var coreNotice = (this._coreReady() || !this._hasInstalledNativeModel()) ? '' : App.prereqBanner({
            level: 'warn',
            text: 'Native ACE-Step v1.5 models need the shared core bundle: ' + (this.state.core_missing || []).join(', ') + '. New model installs include it automatically; install the shared core now for this existing model.',
            actionId: 'ace-step-load-install-core',
            actionLabel: 'Install shared core',
        });

        var body = '' +
            '<div style="display:flex;flex-direction:column;gap:4px">' +
              coreNotice +
              App.field({
                  type: 'select', id: 'ace-step-load-model', label: 'Base model',
                  options: modelOptions, value: modelDefault,
                  hint: 'The DiT checkpoint that generates audio. Friendly names; the loaded one is marked.',
                  tip: 'ACE-Step 1.5 is a diffusion-transformer (DiT) song model. Turbo variants need far fewer steps.',
              }) +
              App.field({
                  type: 'select', id: 'ace-step-load-lm', label: 'Text encoder (LM)',
                  options: lmOptions, value: loadedLm || '',
                  hint: 'Qwen3-based condition encoder. Leave on the recommended pairing unless you know you want another.',
                  tip: 'The LM turns your style prompt + lyrics into the conditioning the DiT samples from.',
              }) +
              App.field({
                  type: 'select', id: 'ace-step-load-vae', label: 'VAE swap',
                  options: vaeOptions, value: loadedVae || 'default',
                  hint: 'Optional 1-D autoencoder replacement applied at load time. "Default" keeps the model\'s own VAE.',
                  tip: 'VAE = variational autoencoder: it decodes the diffusion latent back into a waveform.',
              }) +
              App.field({
                  type: 'toggle', id: 'ace-step-load-cpu-offload', label: 'CPU offload',
                  toggleLabel: 'CPU offload',
                  hint: 'Trade-off: keeps idle weights in system RAM → lower VRAM use, but each step is slower.',
                  tip: 'VRAM = GPU memory. Offloading lets a big model fit on a smaller card at a speed cost.',
              }) +
              App.field({
                  type: 'toggle', id: 'ace-step-load-int8', label: 'Dynamic INT8 quantization',
                  toggleLabel: 'Dynamic INT8 quant',
                  hint: 'Trade-off: ~half the VRAM, slightly lower fidelity. Good for fitting XL on a 12 GB card.',
                  tip: 'INT8 stores weights as 8-bit integers instead of 16-bit floats — smaller, marginally less precise.',
              }) +
              App.field({
                  type: 'toggle', id: 'ace-step-load-compile', label: 'torch.compile',
                  toggleLabel: 'torch.compile',
                  hint: 'Trade-off: faster every run after the first; the first run pays a one-time compile cost.',
                  tip: 'torch.compile fuses the model graph for speed. The initial compile can take a minute or two.',
              }) +
              App.field({
                  type: 'toggle', id: 'ace-step-load-bf16', label: 'BF16 weights',
                  toggleLabel: 'Load in BF16', value: true,
                  hint: 'Official default. Off falls back to FP32 and uses much more VRAM.',
              }) +
              App.field({
                  type: 'select', id: 'ace-step-load-device',
                  label: 'DiT GPU',
                  options: this._deviceOptions(false),
                  value: (this.state.live && this.state.live.device) ||
                         ((App.state.devices && App.state.devices[0] && (App.state.devices[0].id || App.state.devices[0].device)) || 'cuda:0'),
                  hint: 'Where the DiT lives.',
              }) +
              App.field({
                  type: 'select', id: 'ace-step-load-lm-device',
                  label: 'LM GPU',
                  options: this._deviceOptions(true),
                  value: (this.state.live && this.state.live.lm_device) || '',
                  hint: 'Leave on “Same GPU as the DiT” to keep the planner next to the renderer.',
              }) +
              App.field({
                  type: 'select', id: 'ace-step-load-lm-backend',
                  label: 'LM backend',
                  options: [
                      { value: 'pt', label: 'PyTorch (official default)' },
                      { value: 'vllm', label: 'vLLM (faster, extra install)' },
                  ],
                  value: (this.state.live && this.state.live.lm_backend) || 'pt',
                  hint: 'PyTorch is the verified path. vLLM needs that extra runtime.',
              }) +
              '<div style="margin-top:8px;display:flex;gap:8px">' +
                '<button class="btn btn-primary" onclick="TabAceStep.submitLoad()">Load</button>' +
                '<button class="btn" onclick="App.closeModal()">Cancel</button>' +
              '</div>' +
            '</div>';
        App.showTextModal && App.showTextModal('Load ACE-Step', body, { html: true });
        this._bindCoreAction();
    },

    submitLoad: async function() {
        var model = document.getElementById('ace-step-load-model').value;
        var lm    = document.getElementById('ace-step-load-lm').value;
        var vae   = document.getElementById('ace-step-load-vae').value;
        var modelStatus = this._modelStatus(model);
        if (modelStatus && modelStatus.format === 'native' && !this._coreReady()) {
            App.toast('Install the ACE-Step shared core first. New model downloads include it automatically.', 'error');
            return;
        }
        var body = {
            model_variant: model,
            vae_variant: vae,
            cpu_offload: document.getElementById('ace-step-load-cpu-offload').checked,
            int8: document.getElementById('ace-step-load-int8').checked,
            torch_compile: document.getElementById('ace-step-load-compile').checked,
            bf16: document.getElementById('ace-step-load-bf16').checked,
            device: document.getElementById('ace-step-load-device').value,
            lm_backend: document.getElementById('ace-step-load-lm-backend').value || 'pt',
        };
        if (lm) body.lm_variant = lm;
        var lmDevice = document.getElementById('ace-step-load-lm-device').value;
        if (lmDevice) body.lm_device = lmDevice;
        App.toast('Loading ACE-Step…', 'info');
        try {
            await App.api('POST', '/api/ace_step/load', body);
            App.toast('Loaded.', 'success');
            App.closeModal && App.closeModal();
            this.refreshLiveState();
        } catch (err) {
            App.toast('Load failed: ' + (err && err.message ? err.message : err), 'error');
        }
    },

    unloadAll: async function() {
        if (!confirm('Unload everything from VRAM?')) return;
        try {
            await App.api('POST', '/api/ace_step/unload', { component: 'all' });
            App.toast('Unloaded.', 'success');
            this.refreshLiveState();
        } catch (err) {
            App.toast('Unload failed: ' + (err && err.message ? err.message : err), 'error');
        }
    },

    // -----------------------------------------------------------------------
    // Prerequisite: a base model must be loaded before any generation call.
    // -----------------------------------------------------------------------
    // HTML for the "no model loaded" banner + the disabled-submit hint, shared
    // by every generation form. Returns '' when a model is loaded.
    _prereqBannerHtml: function() {
        if (this._modelLoaded()) return '';
        var s = this.state.live || {};
        var text = s.running
            ? 'The ACE-Step worker is up but no base model is loaded. Load one to start generating.'
            : 'No ACE-Step model is loaded yet. Load a model to start generating.';
        return App.prereqBanner({
            level: 'warn',
            text: text,
            actionId: 'ace-step-prereq-load',
            actionLabel: 'Load model',
        });
    },

    // After a generation form renders, wire its prereq banner button (if any).
    _bindPrereqAction: function() {
        var btn = document.getElementById('ace-step-prereq-load');
        if (!btn) return;
        var self = this;
        btn.addEventListener('click', function() { self.openLoadModal(); });
    },

    _bindCoreAction: function() {
        var self = this;
        ['ace-step-open-install-core', 'ace-step-load-install-core'].forEach(function(id) {
            var btn = document.getElementById(id);
            if (!btn || btn.dataset.bound === '1') return;
            btn.dataset.bound = '1';
            btn.addEventListener('click', function() { self.installCoreBundle(btn); });
        });
    },

    // Attributes for the primary submit button so it's disabled (with a hint)
    // whenever the prerequisite is unmet. Returns ' disabled title="…"' or ''.
    _submitGateAttrs: function() {
        if (this._modelLoaded()) return '';
        return ' disabled title="Load an ACE-Step model first (use the banner above)."';
    },

    _modeTask: function(mode) {
        if (mode === 'repaint') return 'repaint';
        if (mode === 'a2a' || mode === 'cover' || mode === 'edit') return 'cover';
        if (mode === 'extract') return 'extract';
        if (mode === 'lego') return 'lego';
        if (mode === 'extend' || mode === 'vocal2bgm' || mode === 'complete') return 'complete';
        return 'text2music';
    },

    _modeSupported: function(mode) {
        if (!this._modelLoaded()) return true;
        var tasks = (this.state.live && this.state.live.model_supported_tasks) || [];
        if (!tasks.length) return true;
        return tasks.indexOf(this._modeTask(mode)) >= 0;
    },

    _modeUnsupportedHtml: function(mode) {
        if (this._modeSupported(mode)) return '';
        var task = this._modeTask(mode);
        return App.prereqBanner({
            level: 'warn',
            text: 'The loaded ACE-Step model does not support this mode. Load a base checkpoint for upstream task "' + task + '".',
            actionId: 'ace-step-prereq-load',
            actionLabel: 'Load model',
        });
    },

    _section: function(title) {
        return '<div class="field-section"><div class="field-section-title">' +
            App.esc(title) + '</div>';
    },

    _sel: function(pairs) {
        return pairs.map(function(pair) {
            return { value: pair[0], label: pair[1] };
        });
    },

    _isTurboLive: function() {
        var live = this.state.live || {};
        var variant = String(live.model_variant || '');
        if (variant.indexOf('turbo') >= 0 || variant === 'ace-1.5') return true;
        if (live.model_default_cfg != null && Number(live.model_default_cfg) <= 1) return true;
        return false;
    },

    _bpmOptions: function() {
        var opts = [{ value: '', label: 'Auto (LM estimates)' }];
        [60, 70, 80, 84, 88, 90, 96, 100, 105, 110, 112, 116, 118, 120, 122,
         124, 126, 128, 130, 132, 136, 138, 140, 144, 148, 150, 160, 168,
         174, 180, 190, 200].forEach(function(n) {
            opts.push({ value: String(n), label: String(n) + ' BPM' });
        });
        return opts;
    },

    _keyOptions: function() {
        var notes = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B'];
        var opts = [{ value: '', label: 'Auto (LM estimates)' }];
        notes.forEach(function(note) {
            opts.push({ value: note + ' major', label: note + ' major' });
            opts.push({ value: note + ' minor', label: note + ' minor' });
        });
        return opts;
    },

    _deviceOptions: function(includeAuto) {
        var devices = (App.state && App.state.devices) || [];
        var opts = includeAuto ? [{ value: '', label: 'Same GPU as the DiT' }] : [];
        devices.forEach(function(d) {
            var id = d.id || d.device;
            if (!id) return;
            var name = d.name ? id + ' — ' + d.name : id;
            if (d.vram_total_mb) name += ' (' + Math.round(d.vram_total_mb / 1024) + ' GB)';
            opts.push({ value: id, label: name });
        });
        if (!devices.length) {
            opts.push({ value: 'cuda:0', label: 'cuda:0' });
            opts.push({ value: 'cuda:1', label: 'cuda:1' });
            opts.push({ value: 'cpu', label: 'cpu' });
        }
        return opts;
    },

    _audioPickerHtml: function(idPrefix, opts) {
        opts = opts || {};
        return '' +
            '<div class="field-row">' +
              '<label class="field-label">' + App.esc(opts.label || 'Audio') +
                (opts.tip ? App.tip(opts.tip) : '') +
              '</label>' +
              (opts.hint ? '<div class="field-hint">' + App.esc(opts.hint) + '</div>' : '') +
              '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">' +
                '<input type="file" id="' + idPrefix + '-file" accept="audio/*" ' +
                'onchange="TabAceStep.handleAudioUpload(this, \'' + idPrefix + '-status\')" ' +
                'class="field-control" style="flex:1;min-width:220px">' +
                '<span id="' + idPrefix + '-status" class="section-title">No file</span>' +
              '</div>' +
              '<input type="hidden" id="' + idPrefix + '-base64">' +
              '<input type="hidden" id="' + idPrefix + '-url">' +
            '</div>';
    },

    _collectAudioInto: function(body, idPrefix, b64Key, urlKey) {
        var b64El = document.getElementById(idPrefix + '-base64');
        var urlEl = document.getElementById(idPrefix + '-url');
        var b64 = b64El ? b64El.value : '';
        var url = urlEl ? urlEl.value : '';
        if (b64) { body[b64Key] = b64; return true; }
        if (url) { body[urlKey] = url; return true; }
        return false;
    },

    _styleRefFieldsHtml: function(idPrefix, extra) {
        extra = extra || {};
        var coverDefault = extra.coverDefault != null ? extra.coverDefault : 1.0;
        return this._section('Style reference') +
            this._audioPickerHtml(idPrefix + '-ref', {
                label: 'Reference audio (style)',
                hint: 'Official ACE style-transfer slot. Makes a new song colored by this track. Optional.',
                tip: 'This is reference_audio, not the source you rewrite. Cover/A2A use a separate source file.',
            }) +
            '<div class="field-grid">' +
              App.field({
                  type: 'slider', id: idPrefix + '-cover-strength',
                  label: 'Reference / cover strength',
                  min: 0, max: 1, step: 0.05, value: coverDefault,
                  hint: '1 keeps more of the reference. 0 follows the prompt more. Official generate default is 1.0; Cover default is 0.8.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-cover-noise',
                  label: 'Cover noise',
                  min: 0, max: 1, step: 0.05, value: 0,
                  hint: 'Official default 0. Raise it to loosen the reference.',
              }) +
            '</div></div>';
    },

    _collectStyleRef: function(body, idPrefix) {
        this._collectAudioInto(body, idPrefix + '-ref', 'reference_audio_base64', 'reference_audio_url');
        var strengthEl = document.getElementById(idPrefix + '-cover-strength');
        var noiseEl = document.getElementById(idPrefix + '-cover-noise');
        if (strengthEl && strengthEl.value !== '') body.audio_cover_strength = parseFloat(strengthEl.value);
        if (noiseEl && noiseEl.value !== '') body.cover_noise_strength = parseFloat(noiseEl.value);
    },

    // -----------------------------------------------------------------------
    // Shared form HTML (prompt + lyrics + filled sliders / selectors)
    // -----------------------------------------------------------------------
    _commonFieldsHtml: function(idPrefix, opts) {
        opts = opts || {};
        var hidePrompt = opts.hidePrompt;
        var hideLyrics = opts.hideLyrics;
        var live = this.state.live || {};
        var turbo = this._isTurboLive();
        var maxDuration = opts.maxDuration || (live.model_max_duration_s || 600);
        var minDuration = opts.minDuration || 1;
        var defaultDuration = opts.defaultDuration || 60;
        var stepsValue = (live.model_default_steps != null)
            ? live.model_default_steps : (turbo ? 8 : 50);
        var cfgValue = (live.model_default_cfg != null)
            ? live.model_default_cfg : (turbo ? 1 : 7);
        var shiftValue = turbo ? 3 : 1;
        var thinkingOn = !!(live.lm_loaded);

        return '' +
            (hidePrompt ? '' :
              App.field({
                  type: 'textarea', id: idPrefix + '-prompt', rows: 2,
                  label: 'Style prompt',
                  placeholder: 'lofi hip-hop, female vocal, vinyl crackle',
                  hint: 'Describe genre, mood, instrumentation, and vocal style in plain words.',
              })) +
            (hideLyrics ? '' :
              App.field({
                  type: 'textarea', id: idPrefix + '-lyrics', rows: 6,
                  label: 'Lyrics',
                  placeholder: '[verse]\nLine 1\nLine 2\n\n[chorus]\nLine 1\nLine 2',
                  hint: 'Use [verse] / [chorus] / [bridge] section tags. Leave blank for an instrumental.',
                  tip: 'Section tags tell the model where verses, choruses and bridges begin.',
              })) +
            App.field({
                type: 'text', id: idPrefix + '-negative',
                label: 'Negative prompt',
                value: 'NO USER INPUT',
                hint: 'Official ACE default. Change this to steer away from qualities you do not want.',
            }) +

            this._section('Song') +
            '<div class="field-grid">' +
              App.field({
                  type: 'slider', id: idPrefix + '-duration',
                  label: 'Duration', unit: 's',
                  min: minDuration, max: maxDuration, step: 1, value: defaultDuration,
                  hint: 'Length of the generated track in seconds.',
              }) +
              App.field({
                  type: 'select', id: idPrefix + '-bpm',
                  label: 'BPM',
                  options: this._bpmOptions(), value: '',
                  hint: 'Typed tempo. Auto lets the planner estimate it.',
              }) +
              App.field({
                  type: 'select', id: idPrefix + '-keyscale',
                  label: 'Key',
                  options: this._keyOptions(), value: '',
                  hint: 'Musical key. Auto lets the planner estimate it.',
              }) +
              App.field({
                  type: 'select', id: idPrefix + '-timesignature',
                  label: 'Time signature',
                  options: [
                      { value: '', label: 'Auto (LM estimates)' },
                      { value: '2', label: '2/4' },
                      { value: '3', label: '3/4' },
                      { value: '4', label: '4/4' },
                      { value: '6', label: '6/8' },
                      { value: 'N/A', label: 'N/A' },
                  ],
                  value: '4',
                  hint: 'Official ACE dropdown. 4 is 4/4.',
              }) +
              App.field({
                  type: 'select', id: idPrefix + '-vocal-language',
                  label: 'Vocal language',
                  options: this._sel(this.LANGUAGES), value: 'en',
                  hint: 'ISO language tag written into the lyric header. English is the working SFT default.',
              }) +
            '</div></div>' +

            this._section('Sampling') +
            '<div class="field-grid">' +
              App.field({
                  type: 'slider', id: idPrefix + '-steps',
                  label: 'Sampling steps',
                  min: 2, max: 200, step: 1, value: stepsValue,
                  hint: 'More steps = more refinement, slower. SFT official is 50; Turbo official is 8.',
                  tip: 'Each step is one denoising pass.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-cfg',
                  label: 'CFG scale',
                  min: 0, max: 20, step: 0.1, value: cfgValue,
                  hint: 'How strongly to follow the prompt. SFT official is 7; Turbo official is 1.',
                  tip: 'CFG = classifier-free guidance.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-shift',
                  label: 'Timestep shift',
                  min: 1, max: 5, step: 0.1, value: shiftValue,
                  hint: 'Official turbo recipes use 3.0. SFT / base use 1.0.',
                  tip: 'Shift warps the flow-matching timestep schedule.',
              }) +
              App.field({
                  type: 'select', id: idPrefix + '-scheduler',
                  label: 'Sampler',
                  options: [
                      { value: 'euler', label: 'Euler (official default)' },
                      { value: 'heun',  label: 'Heun (slower)' },
                      { value: 'dpmpp', label: 'DPM++ (detailed, slower)' },
                  ],
                  value: 'euler',
                  hint: 'Official Gradio exposes Euler and Heun. Omni also forwards DPM++.',
              }) +
              App.field({
                  type: 'select', id: idPrefix + '-infer-method',
                  label: 'Infer method',
                  options: [
                      { value: 'ode', label: 'ODE (official, use this for SFT)' },
                      { value: 'sde', label: 'SDE (can garble SFT)' },
                  ],
                  value: 'ode',
                  hint: 'ODE is the official SFT path. SDE is the stochastic solver.',
              }) +
              App.field({
                  type: 'toggle', id: idPrefix + '-use-adg',
                  label: 'Adaptive Dual Guidance',
                  toggleLabel: 'Use ADG', value: false,
                  hint: 'Official default is off. Adaptive Dual Guidance is an alternate CFG path.',
              }) +
              App.field({
                  type: 'toggle', id: idPrefix + '-thinking',
                  label: 'Thinking (5 Hz LM)',
                  toggleLabel: 'Thinking / plan codes', value: thinkingOn,
                  hint: 'When on, the loaded LM writes semantic codes before the DiT renders. Needs an LM loaded.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-vel-norm',
                  label: 'Velocity norm threshold',
                  min: 0, max: 5, step: 0.1, value: 0,
                  hint: 'Official default 0. Raise to clamp extreme DiT velocities.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-vel-ema',
                  label: 'Velocity EMA factor',
                  min: 0, max: 0.5, step: 0.01, value: 0,
                  hint: 'Official default 0. Smooths the velocity field across steps.',
              }) +
            '</div></div>' +

            this._section('Guidance interval') +
            '<div class="field-grid">' +
              App.field({
                  type: 'slider', id: idPrefix + '-gi-start',
                  label: 'CFG interval start',
                  min: 0, max: 1, step: 0.01, value: 0,
                  hint: 'Official default 0 — guidance is active from the first step.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-gi-end',
                  label: 'CFG interval end',
                  min: 0, max: 1, step: 0.01, value: 1,
                  hint: 'Official default 1 — guidance stays on through the last step.',
              }) +
            '</div></div>' +

            this._section('DCW') +
            '<div class="field-grid">' +
              App.field({
                  type: 'toggle', id: idPrefix + '-dcw-enabled',
                  label: 'DCW',
                  toggleLabel: 'Enable DCW', value: turbo,
                  hint: 'Official Gradio: on for Turbo, off for SFT/base. Leaving this on for SFT garbled earlier takes.',
                  tip: 'DCW = dual-channel wavelet conditioning on the DiT.',
              }) +
              App.field({
                  type: 'select', id: idPrefix + '-dcw-mode',
                  label: 'DCW mode',
                  options: [
                      { value: 'low', label: 'low' },
                      { value: 'high', label: 'high' },
                      { value: 'double', label: 'double (official default)' },
                      { value: 'pix', label: 'pix' },
                  ],
                  value: 'double',
              }) +
              App.field({
                  type: 'select', id: idPrefix + '-dcw-wavelet',
                  label: 'DCW wavelet',
                  options: [
                      { value: 'haar', label: 'haar (official default)' },
                      { value: 'db2', label: 'db2' },
                      { value: 'db4', label: 'db4' },
                      { value: 'sym4', label: 'sym4' },
                      { value: 'sym8', label: 'sym8' },
                      { value: 'coif2', label: 'coif2' },
                  ],
                  value: 'haar',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-dcw-scaler',
                  label: 'DCW scaler',
                  min: 0, max: 0.1, step: 0.005, value: 0.05,
                  hint: 'Official default 0.05.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-dcw-high-scaler',
                  label: 'DCW high scaler',
                  min: 0, max: 0.1, step: 0.005, value: 0.02,
                  hint: 'Official default 0.02.',
              }) +
            '</div></div>' +

            this._section('Language model') +
            '<div class="field-grid">' +
              App.field({
                  type: 'slider', id: idPrefix + '-lm-temperature',
                  label: 'LM temperature',
                  min: 0, max: 2, step: 0.1, value: 0.85,
                  hint: 'Official default 0.85.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-lm-cfg',
                  label: 'LM CFG',
                  min: 1, max: 5, step: 0.1, value: 2.0,
                  hint: 'Official default 2.0.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-lm-top-k',
                  label: 'LM top-k',
                  min: 0, max: 100, step: 1, value: 0,
                  hint: 'Official default 0 (disabled).',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-lm-top-p',
                  label: 'LM top-p',
                  min: 0, max: 1, step: 0.01, value: 0.9,
                  hint: 'Official default 0.9.',
              }) +
              App.field({
                  type: 'toggle', id: idPrefix + '-constrained',
                  label: 'Constrained decoding',
                  toggleLabel: 'Constrained decoding', value: true,
                  hint: 'Official default on. Restricts LM tokens to the ACE code grammar.',
              }) +
              App.field({
                  type: 'toggle', id: idPrefix + '-cot-caption',
                  label: 'CoT caption rewrite',
                  toggleLabel: 'Rewrite caption (CoT)', value: false,
                  hint: 'Official Gradio default is off. On can rewrite your prompt; that garbled earlier SFT takes.',
              }) +
              App.field({
                  type: 'toggle', id: idPrefix + '-cot-metas',
                  label: 'CoT metadata rewrite',
                  toggleLabel: 'Rewrite BPM / key / meter (CoT)', value: false,
                  hint: 'Official Gradio default is on. Left off here so typed metadata stays yours.',
              }) +
              App.field({
                  type: 'toggle', id: idPrefix + '-cot-language',
                  label: 'CoT language rewrite',
                  toggleLabel: 'Rewrite vocal language (CoT)', value: false,
                  hint: 'Official Gradio default is on. Left off so the language selector stays in charge.',
              }) +
              App.field({
                  type: 'toggle', id: idPrefix + '-cot-lyrics',
                  label: 'CoT lyric rewrite',
                  toggleLabel: 'Rewrite lyrics (CoT)', value: false,
                  hint: 'Official default is off.',
              }) +
            '</div></div>' +

            this._section('Output') +
            '<div class="field-grid">' +
              App.field({
                  type: 'toggle', id: idPrefix + '-normalize',
                  label: 'Normalize',
                  toggleLabel: 'Enable loudness normalization', value: true,
                  hint: 'Official default on.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-normalize-db',
                  label: 'Normalization target', unit: 'dB',
                  min: -12, max: 0, step: 0.5, value: -1,
                  hint: 'Official default −1 dB.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-fade-in',
                  label: 'Fade in', unit: 's',
                  min: 0, max: 30, step: 0.1, value: 0,
                  hint: 'Official default 0.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-fade-out',
                  label: 'Fade out', unit: 's',
                  min: 0, max: 30, step: 0.1, value: 0,
                  hint: 'Official default 0.',
              }) +
              App.field({
                  type: 'slider', id: idPrefix + '-batch',
                  label: 'Batch size',
                  min: 1, max: 8, step: 1, value: 1,
                  hint: 'Official ACE batch. 1 is the safe default on a 24 GB card.',
              }) +
              App.field({
                  type: 'toggle', id: idPrefix + '-seed-random',
                  label: 'Random seed',
                  toggleLabel: 'Random seed', value: true,
                  hint: 'On = a new seed every take. Off = use the seed spinner.',
              }) +
              App.field({
                  type: 'number', id: idPrefix + '-seed',
                  label: 'Seed',
                  min: 0, step: 1, value: 42,
                  hint: 'Used only when Random seed is off. Same seed + settings reproduces the take.',
              }) +
              App.field({
                  type: 'toggle', id: idPrefix + '-bf16',
                  label: 'BF16',
                  toggleLabel: 'BF16 inference', value: true,
                  hint: 'Official default on.',
              }) +
              App.field({
                  type: 'toggle', id: idPrefix + '-overlapped',
                  label: 'Overlapped decode',
                  toggleLabel: 'Overlapped VAE decode', value: true,
                  hint: 'Official default on. Reduces decode seams on long tracks.',
              }) +
              App.field({
                  type: 'select', id: idPrefix + '-timesteps',
                  label: 'Custom timesteps',
                  options: [
                      { value: '', label: 'Model schedule (default)' },
                      { value: '0.97,0.76,0.615,0.5,0.395,0.28,0.18,0.085,0',
                        label: 'Official 8-step turbo schedule' },
                  ],
                  value: '',
                  hint: 'Leave on the model schedule unless you are matching a published Turbo recipe.',
              }) +
              App.field({
                  type: 'select', id: idPrefix + '-output-name',
                  label: 'Output name',
                  options: [
                      { value: '', label: 'Auto (model + duration + seed)' },
                  ],
                  value: '',
                  hint: 'Auto is the Omni naming scheme. Type a custom stem only if you need a fixed filename.',
              }) +
              App.field({
                  type: 'textarea', id: idPrefix + '-audio-codes', rows: 2,
                  label: 'Audio codes (optional)',
                  value: '',
                  placeholder: 'Paste 5Hz codes from Planner → Understand, or leave empty',
                  hint: 'Expert. Empty = let Thinking write codes. Paste to lock a known plan.',
              }) +
            '</div></div>';
    },

    _gatherCommonFields: function(idPrefix) {
        var get = function(id) {
            var el = document.getElementById(idPrefix + '-' + id);
            return el ? el.value : '';
        };
        var checked = function(id, fallback) {
            var el = document.getElementById(idPrefix + '-' + id);
            return el ? !!el.checked : fallback;
        };
        var num = function(id, parser) {
            var raw = get(id);
            if (raw === '' || raw == null) return null;
            var value = parser(raw);
            return isNaN(value) ? null : value;
        };
        var body = {
            prompt: (get('prompt') || '').trim(),
            lyrics: (get('lyrics') || '').trim() || null,
            negative_prompt: (get('negative') || '').trim() || null,
            duration_s: num('duration', parseFloat) || 60.0,
            scheduler: get('scheduler') || 'euler',
            infer_method: get('infer-method') || 'ode',
            use_adg: checked('use-adg', false),
            thinking: checked('thinking', false),
            dcw_enabled: checked('dcw-enabled', false),
            dcw_mode: get('dcw-mode') || 'double',
            dcw_wavelet: get('dcw-wavelet') || 'haar',
            vocal_language: get('vocal-language') || 'en',
            use_constrained_decoding: checked('constrained', true),
            use_cot_caption: checked('cot-caption', false),
            use_cot_metas: checked('cot-metas', false),
            use_cot_language: checked('cot-language', false),
            use_cot_lyrics: checked('cot-lyrics', false),
            enable_normalization: checked('normalize', true),
            bf16: checked('bf16', true),
            overlapped_decode: checked('overlapped', true),
        };
        var steps = num('steps', function(v) { return parseInt(v, 10); });
        if (steps != null) body.steps = steps;
        var cfg = num('cfg', parseFloat);
        if (cfg != null) body.cfg_scale = cfg;
        var shift = num('shift', parseFloat);
        if (shift != null) body.shift = shift;
        var bpm = num('bpm', function(v) { return parseInt(v, 10); });
        if (bpm != null) body.bpm = bpm;
        var keyscale = (get('keyscale') || '').trim();
        if (keyscale) body.keyscale = keyscale;
        var timesignature = (get('timesignature') || '').trim();
        if (timesignature) body.timesignature = timesignature;
        var dcwScaler = num('dcw-scaler', parseFloat);
        if (dcwScaler != null) body.dcw_scaler = dcwScaler;
        var dcwHigh = num('dcw-high-scaler', parseFloat);
        if (dcwHigh != null) body.dcw_high_scaler = dcwHigh;
        var lmTemp = num('lm-temperature', parseFloat);
        if (lmTemp != null) body.lm_temperature = lmTemp;
        var lmCfg = num('lm-cfg', parseFloat);
        if (lmCfg != null) body.lm_cfg_scale = lmCfg;
        var lmTopK = num('lm-top-k', function(v) { return parseInt(v, 10); });
        if (lmTopK != null) body.lm_top_k = lmTopK;
        var lmTopP = num('lm-top-p', parseFloat);
        if (lmTopP != null) body.lm_top_p = lmTopP;
        var normDb = num('normalize-db', parseFloat);
        if (normDb != null) body.normalization_db = normDb;
        var fadeIn = num('fade-in', parseFloat);
        if (fadeIn != null) body.fade_in_s = fadeIn;
        var fadeOut = num('fade-out', parseFloat);
        if (fadeOut != null) body.fade_out_s = fadeOut;
        var batch = num('batch', function(v) { return parseInt(v, 10); });
        if (batch != null) body.batch_size = batch;
        if (!checked('seed-random', true)) {
            var seed = num('seed', function(v) { return parseInt(v, 10); });
            if (seed != null) body.seed = seed;
        }
        var giStart = num('gi-start', parseFloat);
        var giEnd = num('gi-end', parseFloat);
        if (giStart == null) giStart = 0;
        if (giEnd == null) giEnd = 1;
        if (!(giStart >= 0 && giEnd <= 1 && giStart < giEnd)) {
            throw new Error('Guidance interval must satisfy 0 ≤ start < end ≤ 1.');
        }
        body.guidance_interval = [giStart, giEnd];
        var timesteps = (get('timesteps') || '').trim();
        if (timesteps) {
            body.timesteps = timesteps.split(',').map(function(item) {
                return parseFloat(item.trim());
            }).filter(function(item) { return !isNaN(item); });
        }
        var velNorm = num('vel-norm', parseFloat);
        if (velNorm != null) body.velocity_norm_threshold = velNorm;
        var velEma = num('vel-ema', parseFloat);
        if (velEma != null) body.velocity_ema_factor = velEma;
        var codes = (get('audio-codes') || '').trim();
        if (codes) body.audio_codes = codes;
        var outName = (get('output-name') || '').trim();
        if (outName) body.output_name = outName;
        return body;
    },

    _renderPlannerView: function() {
        var lmReady = !!(this.state.live && this.state.live.lm_loaded);
        var gate = lmReady ? '' : ' disabled title="Load an ACE-Step LM first."';
        var result = this.state.plannerResult;
        var resultHtml = result
            ? '<pre class="field-control" style="white-space:pre-wrap;max-height:240px;overflow:auto">' +
              App.esc(JSON.stringify(result, null, 2)) + '</pre>' +
              '<div style="margin-top:8px"><button class="btn btn-sm" onclick="TabAceStep.sendPlannerToGenerate()">Send caption/lyrics to Generate</button></div>'
            : '<div class="empty-state">No planner result yet.</div>';
        return '' +
            '<div class="card">' +
              '<h2>Create sample</h2>' +
              '<p class="section-title">Official ACE planner: a style query becomes caption, lyrics, BPM, and key. Does not render audio.</p>' +
              (lmReady ? '' : App.prereqBanner({ level: 'warn', text: 'Load a 5 Hz LM before using planner helpers.', actionId: 'ace-step-prereq-load', actionLabel: 'Load model' })) +
              App.field({
                  type: 'textarea', id: 'ace-step-create-query', rows: 3,
                  label: 'Style query',
                  placeholder: 'hypnotic minimal techno, dark warehouse, 128 BPM',
                  hint: 'Short request. The LM expands it.',
              }) +
              App.field({
                  type: 'toggle', id: 'ace-step-create-instrumental',
                  label: 'Instrumental',
                  toggleLabel: 'Instrumental (no vocals)', value: false,
              }) +
              '<div class="field-grid">' +
                App.field({
                    type: 'select', id: 'ace-step-create-lang',
                    label: 'Vocal language',
                    options: this._sel(this.LANGUAGES), value: 'en',
                }) +
                App.field({
                    type: 'slider', id: 'ace-step-create-temp',
                    label: 'LM temperature', min: 0, max: 2, step: 0.1, value: 0.85,
                }) +
                App.field({
                    type: 'slider', id: 'ace-step-create-topk',
                    label: 'LM top-k', min: 0, max: 100, step: 1, value: 0,
                }) +
                App.field({
                    type: 'slider', id: 'ace-step-create-topp',
                    label: 'LM top-p', min: 0, max: 1, step: 0.01, value: 0.9,
                }) +
              '</div>' +
              '<div style="margin-top:12px"><button class="btn btn-primary" onclick="TabAceStep.submitCreateSample(this)"' + gate + '>Create sample</button></div>' +
            '</div>' +
            '<div class="card">' +
              '<h2>Format sample</h2>' +
              '<p class="section-title">Official Format: expand a caption + lyrics pair into structured metadata.</p>' +
              App.field({
                  type: 'textarea', id: 'ace-step-format-prompt', rows: 2,
                  label: 'Caption',
                  placeholder: 'dark minimal techno, four on the floor, hypnotic pulse',
              }) +
              App.field({
                  type: 'textarea', id: 'ace-step-format-lyrics', rows: 4,
                  label: 'Lyrics',
                  placeholder: '[verse]\n…',
              }) +
              '<div class="field-grid">' +
                App.field({ type: 'select', id: 'ace-step-format-bpm', label: 'BPM', options: this._bpmOptions(), value: '128' }) +
                App.field({ type: 'select', id: 'ace-step-format-key', label: 'Key', options: this._keyOptions(), value: '' }) +
                App.field({
                    type: 'select', id: 'ace-step-format-timesig', label: 'Time signature',
                    options: [
                        { value: '', label: 'Auto (LM estimates)' },
                        { value: '2', label: '2/4' }, { value: '3', label: '3/4' },
                        { value: '4', label: '4/4' }, { value: '6', label: '6/8' },
                    ],
                    value: '4',
                }) +
                App.field({ type: 'select', id: 'ace-step-format-lang', label: 'Vocal language', options: this._sel(this.LANGUAGES), value: 'en' }) +
                App.field({ type: 'slider', id: 'ace-step-format-duration', label: 'Duration', unit: 's', min: 1, max: 600, step: 1, value: 120 }) +
                App.field({ type: 'slider', id: 'ace-step-format-temp', label: 'LM temperature', min: 0, max: 2, step: 0.1, value: 0.85 }) +
              '</div>' +
              '<div style="margin-top:12px"><button class="btn btn-primary" onclick="TabAceStep.submitFormatSample(this)"' + gate + '>Format sample</button></div>' +
            '</div>' +
            '<div class="card">' +
              '<h2>Understand codes</h2>' +
              '<p class="section-title">Official understand_music: 5Hz codes → caption, lyrics, and metadata.</p>' +
              App.field({
                  type: 'textarea', id: 'ace-step-understand-codes', rows: 4,
                  label: 'Audio codes',
                  placeholder: 'Paste 5Hz codes here',
              }) +
              App.field({
                  type: 'slider', id: 'ace-step-understand-temp',
                  label: 'LM temperature', min: 0, max: 2, step: 0.1, value: 0.85,
              }) +
              '<div style="margin-top:12px"><button class="btn btn-primary" onclick="TabAceStep.submitUnderstand(this)"' + gate + '>Understand</button></div>' +
            '</div>' +
            '<div class="card"><h2>Planner result</h2>' + resultHtml + '</div>';
    },

    _renderSimpleView: function() {
        var gate = this._submitGateAttrs();
        return '' +
            '<div class="card">' +
              '<h2>Simple Mode</h2>' +
              this._prereqBannerHtml() +
              '<p class="section-title">Official ACE Simple Mode: the LM writes caption + lyrics from a short query, then the DiT generates the song.</p>' +
              App.field({
                  type: 'textarea', id: 'ace-step-simple-query', rows: 3,
                  label: 'Style query',
                  placeholder: 'hypnotic minimal techno, 128 BPM, dark warehouse',
                  hint: 'A short style request. The planner expands this into a caption and lyrics.',
              }) +
              App.field({
                  type: 'toggle', id: 'ace-step-simple-instrumental',
                  label: 'Instrumental',
                  toggleLabel: 'Instrumental (no vocals)', value: false,
                  hint: 'Ask the planner for an instrumental instead of sung lyrics.',
              }) +
              this._styleRefFieldsHtml('ace-step-simple', { coverDefault: 1.0 }) +
              this._commonFieldsHtml('ace-step-simple', { hidePrompt: true, hideLyrics: true }) +
              '<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">' +
                '<button class="btn btn-primary" onclick="TabAceStep.submitSimple(this)"' + gate + '>Plan + generate</button>' +
                '<button class="btn btn-danger" onclick="TabAceStep.cancelInFlight()">Cancel</button>' +
              '</div>' +
            '</div>' +
            this._progressCardHtml() +
            '<div id="ace-step-results" style="margin-top:12px"></div>';
    },

    // -----------------------------------------------------------------------
    // Generate view
    // -----------------------------------------------------------------------
    _renderGenerateView: function() {
        var gate = this._submitGateAttrs();
        return '' +
            '<div class="card">' +
              '<h2>Generate</h2>' +
              this._prereqBannerHtml() +
              this._styleRefFieldsHtml('ace-step-gen', { coverDefault: 1.0 }) +
              this._commonFieldsHtml('ace-step-gen') +
              App.field({
                  type: 'slider', id: 'ace-step-gen-n',
                  label: 'Candidates (N)',
                  min: 1, max: 16, step: 1, value: 4,
                  hint: 'How many takes to generate, then rank. More candidates = better odds, more time.',
              }) +
              App.field({
                  type: 'toggle', id: 'ace-step-gen-clap',
                  label: 'CLAP-rank candidates',
                  toggleLabel: 'CLAP-rank candidates',
                  value: true,
                  hint: 'Scores each take against the prompt and highlights the best. Needs the audio_lab worker running with a CLAP model — otherwise results come back unranked.',
                  tip: 'CLAP scores how well audio matches a text prompt; here it picks the take closest to your prompt.',
              }) +
              App.field({
                  type: 'text', id: 'ace-step-gen-score-prompt',
                  label: 'Score prompt override (optional)',
                  placeholder: '(defaults to the style prompt above)',
                  hint: 'Rank against this text instead of the style prompt. Only used when CLAP-ranking is on.',
              }) +
              '<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">' +
                '<button class="btn btn-primary" onclick="TabAceStep.submitGenerateRanked(this)"' + gate + '>Generate (ranked)</button>' +
                '<button class="btn" onclick="TabAceStep.submitGenerate(this)"' + gate + '>Generate single</button>' +
                '<button class="btn btn-danger" onclick="TabAceStep.cancelInFlight()">Cancel</button>' +
              '</div>' +
            '</div>' +
            this._progressCardHtml() +
            '<div id="ace-step-results" style="margin-top:12px"></div>';
    },

    _progressCardHtml: function() {
        return '' +
            '<div class="card" id="ace-step-progress-card" style="display:none">' +
              '<h2>Progress</h2>' +
              '<div id="ace-step-progress-label" class="section-title">…</div>' +
              '<div style="background:var(--bg-soft,#222);height:8px;border-radius:4px;margin-top:6px;overflow:hidden">' +
                '<div id="ace-step-progress-bar" style="height:100%;width:0%;background:#3fb950;transition:width 0.3s"></div>' +
              '</div>' +
            '</div>';
    },

    _renderInitAudioForm: function(mode) {
        var gate = this._submitGateAttrs();
        if (!this._modeSupported(mode)) gate += ' disabled title="The loaded model does not support this mode."';
        var modeUnsupported = this._modeUnsupportedHtml(mode);
        var modeLabel = {
            a2a: 'Audio-to-Audio',
            repaint: 'Repaint masked region',
            edit: 'Edit lyrics or remix',
            extend: 'Extend (prepend/append)',
            cover: 'Cover (re-sing existing track)',
            extract: 'Extract one stem',
            lego: 'Lego (replace one stem)',
            complete: 'Complete missing stems',
            vocal2bgm: 'Vocal → BGM (instrumental from vocals)',
        }[mode] || mode;
        var p = 'ace-step-' + mode;
        var modeSpecific = '';
        var trackOptions = this.TRACK_NAMES.map(function(name) {
            return { value: name, label: name.replace(/_/g, ' ') };
        });
        if (mode === 'a2a') {
            modeSpecific = App.field({
                type: 'slider', id: p + '-noise',
                label: 'Init influence (Preserve original 0 ↔ Reinterpret 1)',
                min: 0, max: 1, step: 0.05, value: 0.6,
                hint: 'How much fresh noise to inject. Low keeps the input close; high reinterprets it freely.',
                tip: 'Init-noise level: 0 leaves the reference nearly untouched, 1 effectively regenerates from scratch.',
            });
        } else if (mode === 'repaint') {
            modeSpecific =
                App.field({
                    type: 'slider', id: p + '-mask-start',
                    label: 'Mask start', unit: 's',
                    min: 0, max: 600, step: 0.1, value: 0,
                    hint: 'Seconds into the reference where the rewrite begins.',
                }) +
                App.field({
                    type: 'slider', id: p + '-mask-end',
                    label: 'Mask end', unit: 's',
                    min: 0, max: 600, step: 0.1, value: 8,
                    hint: 'Seconds into the reference where the rewrite ends. Must be after start.',
                });
        } else if (mode === 'edit') {
            modeSpecific = App.field({
                type: 'select', id: p + '-edit-mode',
                label: 'Edit mode',
                options: [
                    { value: 'only_lyrics', label: 'Only lyrics — keep melody, change words' },
                    { value: 'remix',       label: 'Remix — change style/arrangement' },
                ],
                value: 'only_lyrics',
                hint: 'What to change: just the words, or the overall style.',
            }) +
            App.field({
                type: 'textarea', id: p + '-source-prompt', rows: 2,
                label: 'Source style prompt',
                placeholder: 'Describe the original track style',
                hint: 'What the reference currently sounds like. Helps the editor keep the parts you are not changing.',
            }) +
            App.field({
                type: 'textarea', id: p + '-source-lyrics', rows: 4,
                label: 'Source lyrics',
                placeholder: '[verse]\nOriginal words…',
                hint: 'Current lyrics of the reference, if you know them.',
            });
        } else if (mode === 'extract' || mode === 'lego') {
            modeSpecific = App.field({
                type: 'select', id: p + '-track',
                label: 'Track',
                options: trackOptions, value: 'drums',
                hint: 'Official ACE stem name. Extract pulls this stem; Lego rewrites it.',
            });
        } else if (mode === 'complete') {
            modeSpecific = '<label class="field-label">Stems to complete' +
                App.tip('Official ACE track classes. Checked stems are the ones the model should fill in.') +
                '</label><div class="field-grid" style="margin-bottom:12px">' +
                this.TRACK_NAMES.map(function(name) {
                    var checked = (name === 'drums' || name === 'bass' || name === 'synth') ? ' checked' : '';
                    return '<label class="field-toggle"><input type="checkbox" class="ace-step-complete-track" value="' +
                        App.esc(name) + '"' + checked + '><span>' + App.esc(name.replace(/_/g, ' ')) + '</span></label>';
                }).join('') + '</div>';
        } else if (mode === 'extend') {
            modeSpecific =
                App.field({
                    type: 'select', id: p + '-extend-mode',
                    label: 'Extend direction',
                    options: [
                        { value: 'append',  label: 'Append — add to the end' },
                        { value: 'prepend', label: 'Prepend — add before the start' },
                    ],
                    value: 'append',
                    hint: 'Where the new audio is attached relative to the reference.',
                }) +
                App.field({
                    type: 'number', id: p + '-extend-duration',
                    label: 'Seconds to add', unit: 's',
                    min: 1, max: 300, step: 1, value: 30,
                    hint: 'Length of the new section to generate.',
                });
        }
        var hidePrompt = mode === 'vocal2bgm';  // optional only
        var hideLyrics = mode === 'vocal2bgm';
        var promptBlock = hidePrompt
            ? App.field({
                  type: 'text', id: p + '-prompt',
                  label: 'Optional style hint',
                  placeholder: 'leave blank for auto',
                  hint: 'Vocal→BGM works without a prompt; add one to steer the instrumental.',
              })
            : this._commonFieldsHtml(p, { hidePrompt: hidePrompt, hideLyrics: hideLyrics });
        // When the prompt is hidden (vocal2bgm) we still need the rest of the
        // common controls (duration/steps/CFG/scheduler/seed/guidance).
        var restBlock = hidePrompt
            ? this._commonFieldsHtml(p, { hidePrompt: true, hideLyrics: true })
            : '';
        return '' +
            '<div class="card">' +
              '<h2>' + modeLabel + '</h2>' +
              this._prereqBannerHtml() +
              modeUnsupported +
              this._audioPickerHtml(p, {
                  label: 'Source audio',
                  hint: 'The track this mode rewrites, covers, or extends. Upload a file or use a previous output.',
                  tip: 'This is src_audio. Style reference is a separate optional file below.',
              }) +
              this._styleRefFieldsHtml(p, { coverDefault: mode === 'cover' ? 0.8 : 1.0 }) +
              promptBlock +
              restBlock +
              modeSpecific +
              '<div style="margin-top:12px;display:flex;gap:8px">' +
                '<button class="btn btn-primary" onclick="TabAceStep.submitInitAudioMode(\'' + mode + '\', this)"' + gate + '>Run ' + modeLabel + '</button>' +
                '<button class="btn btn-danger" onclick="TabAceStep.cancelInFlight()">Cancel</button>' +
              '</div>' +
            '</div>' +
            this._progressCardHtml() +
            '<div id="ace-step-results" style="margin-top:12px"></div>';
    },

    _renderLoraModeForm: function(mode) {
        var gate = this._submitGateAttrs();
        var modeLabel = mode === 'lyric2vocal' ? 'Lyric → Vocal' : 'Text → Samples';
        var loraName = mode;
        var loraInstalled = (this.state.loras || []).some(function(l) {
            return l.name === loraName && l.installed;
        });
        var loraNotice = loraInstalled
            ? '<div class="field-hint">The required <strong>' + App.esc(loraName) + '</strong> LoRA is installed — it will be auto-attached during this call.' +
              App.tip('LoRA = a small adapter that specializes the base model for this mode; attached automatically at run time.') + '</div>'
            : App.prereqBanner({
                  level: 'warn',
                  text: 'The "' + loraName + '" LoRA is not installed yet — install it on the Install tab to enable this mode.',
              });
        // Submit is gated by BOTH the model-loaded prereq and the LoRA install.
        var loraGate = loraInstalled ? '' : ' disabled title="Install the ' + loraName + ' LoRA first (Install tab)."';
        return '' +
            '<div class="card">' +
              '<h2>' + modeLabel + '</h2>' +
              this._prereqBannerHtml() +
              loraNotice +
              this._commonFieldsHtml('ace-step-' + mode, mode === 'text2samples'
                ? {minDuration: 1, maxDuration: 30, defaultDuration: 4}
                : {}) +
              '<div style="margin-top:12px;display:flex;gap:8px">' +
                '<button class="btn btn-primary"' + gate + loraGate +
                ' onclick="TabAceStep.submitLoraMode(\'' + mode + '\', this)">Run ' + modeLabel + '</button>' +
                '<button class="btn btn-danger" onclick="TabAceStep.cancelInFlight()">Cancel</button>' +
              '</div>' +
            '</div>' +
            this._progressCardHtml() +
            '<div id="ace-step-results" style="margin-top:12px"></div>';
    },

    // -----------------------------------------------------------------------
    // File upload → base64
    // -----------------------------------------------------------------------
    handleAudioUpload: function(input, statusId) {
        var file = input.files && input.files[0];
        var statusEl = document.getElementById(statusId);
        if (!file) { if (statusEl) statusEl.textContent = 'No file'; return; }
        if (file.size > 200 * 1024 * 1024) {
            App.toast('File too large (>200MB).', 'error');
            input.value = '';
            return;
        }
        if (statusEl) statusEl.textContent = 'Reading ' + (file.size/1024/1024).toFixed(2) + ' MB…';
        var reader = new FileReader();
        var mode = input.id.replace(/^ace-step-/, '').replace(/-file$/, '');
        var self = this;
        reader.onload = function() {
            // strip "data:audio/wav;base64," prefix
            var b64 = String(reader.result).split(',', 2)[1] || '';
            var b64El = document.getElementById('ace-step-' + mode + '-base64');
            var urlEl = document.getElementById('ace-step-' + mode + '-url');
            if (b64El) b64El.value = b64;
            if (urlEl) urlEl.value = '';
            // User uploaded a fresh file → clear the cross-form pending URL
            // so that navigating away+back doesn't overwrite this upload.
            if (input.id.indexOf('-ref-file') >= 0) self.state.pendingRefUrl = null;
            else self.state.pendingInitUrl = null;
            if (statusEl) statusEl.textContent = file.name + ' (' + (file.size/1024/1024).toFixed(2) + ' MB)';
        };
        reader.onerror = function() {
            if (statusEl) statusEl.textContent = 'Read failed';
            App.toast('Could not read audio file', 'error');
        };
        reader.readAsDataURL(file);
    },

    // -----------------------------------------------------------------------
    // Generate / generate-ranked
    // -----------------------------------------------------------------------
    submitGenerate: async function(btn) {
        var body;
        try { body = this._gatherCommonFields('ace-step-gen'); }
        catch (e) { App.toast(e.message, 'error'); return; }
        if (!body.prompt) { App.toast('Prompt is required', 'error'); return; }
        this._collectStyleRef(body, 'ace-step-gen');
        return this._runInference('/api/ace_step/generate', body, btn, 'Generating…');
    },

    submitSimple: async function(btn) {
        var body;
        try { body = this._gatherCommonFields('ace-step-simple'); }
        catch (e) { App.toast(e.message, 'error'); return; }
        var query = ((document.getElementById('ace-step-simple-query') || {}).value || '').trim();
        if (!query) { App.toast('Style query is required', 'error'); return; }
        body.query = query;
        body.prompt = query;
        body.instrumental = !!(document.getElementById('ace-step-simple-instrumental') || {}).checked;
        this._collectStyleRef(body, 'ace-step-simple');
        return this._runInference('/api/ace_step/simple', body, btn, 'Planning + generating…');
    },

    _runPlanner: async function(path, body, btn, msg) {
        var orig = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = msg; }
        try {
            var data = await App.api('POST', path, body, { timeout: 180000 });
            this.state.plannerResult = data;
            App.toast('Planner done.', 'success');
            this.setMode('planner');
        } catch (err) {
            App.toast('Planner failed: ' + (err && err.message ? err.message : err), 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = orig; }
        }
    },

    submitCreateSample: async function(btn) {
        var query = ((document.getElementById('ace-step-create-query') || {}).value || '').trim();
        if (!query) { App.toast('Style query is required', 'error'); return; }
        var body = {
            query: query,
            instrumental: !!(document.getElementById('ace-step-create-instrumental') || {}).checked,
            vocal_language: (document.getElementById('ace-step-create-lang') || {}).value || 'en',
            lm_temperature: parseFloat((document.getElementById('ace-step-create-temp') || {}).value),
            lm_top_k: parseInt((document.getElementById('ace-step-create-topk') || {}).value, 10),
            lm_top_p: parseFloat((document.getElementById('ace-step-create-topp') || {}).value),
        };
        return this._runPlanner('/api/ace_step/create-sample', body, btn, 'Planning…');
    },

    submitFormatSample: async function(btn) {
        var prompt = ((document.getElementById('ace-step-format-prompt') || {}).value || '').trim();
        if (!prompt) { App.toast('Caption is required', 'error'); return; }
        var body = {
            prompt: prompt,
            lyrics: ((document.getElementById('ace-step-format-lyrics') || {}).value || '').trim() || null,
            vocal_language: (document.getElementById('ace-step-format-lang') || {}).value || 'en',
            lm_temperature: parseFloat((document.getElementById('ace-step-format-temp') || {}).value),
            duration_s: parseFloat((document.getElementById('ace-step-format-duration') || {}).value),
        };
        var bpm = parseInt((document.getElementById('ace-step-format-bpm') || {}).value, 10);
        if (!isNaN(bpm)) body.bpm = bpm;
        var key = ((document.getElementById('ace-step-format-key') || {}).value || '').trim();
        if (key) body.keyscale = key;
        var ts = ((document.getElementById('ace-step-format-timesig') || {}).value || '').trim();
        if (ts) body.timesignature = ts;
        return this._runPlanner('/api/ace_step/format-sample', body, btn, 'Formatting…');
    },

    submitUnderstand: async function(btn) {
        var codes = ((document.getElementById('ace-step-understand-codes') || {}).value || '').trim();
        if (!codes) { App.toast('Audio codes are required', 'error'); return; }
        return this._runPlanner('/api/ace_step/understand', {
            audio_codes: codes,
            lm_temperature: parseFloat((document.getElementById('ace-step-understand-temp') || {}).value),
        }, btn, 'Understanding…');
    },

    sendPlannerToGenerate: function() {
        var sample = this.state.plannerResult || {};
        this.setMode('generate');
        var setVal = function(id, value) {
            var el = document.getElementById(id);
            if (el && value != null && value !== '') el.value = value;
        };
        setVal('ace-step-gen-prompt', sample.caption || sample.prompt || '');
        setVal('ace-step-gen-lyrics', sample.lyrics || '');
        if (sample.bpm) setVal('ace-step-gen-bpm', String(sample.bpm));
        if (sample.keyscale) setVal('ace-step-gen-keyscale', sample.keyscale);
        if (sample.timesignature) setVal('ace-step-gen-timesignature', sample.timesignature);
        if (sample.vocal_language) setVal('ace-step-gen-vocal-language', sample.vocal_language);
        if (sample.duration_s) setVal('ace-step-gen-duration', sample.duration_s);
        if (sample.audio_codes) setVal('ace-step-gen-audio-codes', sample.audio_codes);
        App.toast('Planner fields copied onto Generate.', 'success');
    },

    submitGenerateRanked: async function(btn) {
        var body;
        try { body = this._gatherCommonFields('ace-step-gen'); }
        catch (e) { App.toast(e.message, 'error'); return; }
        if (!body.prompt) { App.toast('Prompt is required', 'error'); return; }
        var n = parseInt(document.getElementById('ace-step-gen-n').value, 10);
        if (!isNaN(n)) body.n = n;
        var sp = (document.getElementById('ace-step-gen-score-prompt').value || '').trim();
        if (sp) body.score_prompt = sp;
        body.score_with_clap = document.getElementById('ace-step-gen-clap').checked;
        this._collectStyleRef(body, 'ace-step-gen');
        return this._runInference('/api/ace_step/generate-ranked', body, btn, 'Generating + ranking…');
    },

    submitInitAudioMode: async function(mode, btn) {
        var body;
        try { body = this._gatherCommonFields('ace-step-' + mode); }
        catch (e) { App.toast(e.message, 'error'); return; }
        // vocal2bgm allows a missing prompt (the worker fills a default).
        if (mode !== 'vocal2bgm' && !body.prompt) {
            App.toast('Prompt is required', 'error'); return;
        }
        var b64 = document.getElementById('ace-step-' + mode + '-base64').value;
        var url = document.getElementById('ace-step-' + mode + '-url').value;
        if (!b64 && !url) { App.toast('Upload an audio file first', 'error'); return; }
        if (b64) body.init_audio_base64 = b64;
        else body.init_audio_url = url;
        this._collectStyleRef(body, 'ace-step-' + mode);
        // Mode-specific fields.
        if (mode === 'a2a') {
            body.init_noise_level = parseFloat(document.getElementById('ace-step-a2a-noise').value);
        } else if (mode === 'repaint') {
            body.mask_start_s = parseFloat(document.getElementById('ace-step-repaint-mask-start').value);
            body.mask_end_s = parseFloat(document.getElementById('ace-step-repaint-mask-end').value);
            if (isNaN(body.mask_start_s) || isNaN(body.mask_end_s)) {
                App.toast('Mask start and end (in seconds) are required for Repaint', 'error');
                return;
            }
            if (!(body.mask_start_s < body.mask_end_s)) {
                App.toast('Mask start must be before mask end', 'error');
                return;
            }
        } else if (mode === 'edit') {
            body.edit_mode = document.getElementById('ace-step-edit-edit-mode').value;
            var srcPrompt = (document.getElementById('ace-step-edit-source-prompt').value || '').trim();
            var srcLyrics = (document.getElementById('ace-step-edit-source-lyrics').value || '').trim();
            if (srcPrompt) body.source_prompt = srcPrompt;
            if (srcLyrics) body.source_lyrics = srcLyrics;
        } else if (mode === 'extend') {
            body.extend_mode = document.getElementById('ace-step-extend-extend-mode').value;
            body.extend_duration_s = parseFloat(document.getElementById('ace-step-extend-extend-duration').value);
        } else if (mode === 'extract' || mode === 'lego') {
            body.track_name = document.getElementById('ace-step-' + mode + '-track').value;
        } else if (mode === 'complete') {
            var names = [];
            document.querySelectorAll('.ace-step-complete-track:checked').forEach(function(el) {
                names.push(el.value);
            });
            if (!names.length) { App.toast('Select at least one stem to complete', 'error'); return; }
            body.track_names = names;
        }
        return this._runInference('/api/ace_step/' + mode, body, btn, 'Running ' + mode + '…');
    },

    submitLoraMode: async function(mode, btn) {
        var body;
        try { body = this._gatherCommonFields('ace-step-' + mode); }
        catch (e) { App.toast(e.message, 'error'); return; }
        if (!body.prompt) { App.toast('Prompt is required', 'error'); return; }
        return this._runInference('/api/ace_step/' + mode, body, btn, 'Running ' + mode + '…');
    },

    _runInference: async function(path, body, btn, msg) {
        var origLabel = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = msg; }
        // Show progress card.
        var pc = document.getElementById('ace-step-progress-card');
        if (pc) pc.style.display = '';
        this._startProgressPolling();
        try {
            var data = await App.api('POST', path, body, { timeout: 1800000 });
            this.state.lastJob = data;
            this._renderResults(data);
            App.toast('Done.', 'success');
        } catch (err) {
            App.toast('Failed: ' + (err && err.message ? err.message : err), 'error');
        } finally {
            this._stopProgressPolling();
            if (pc) pc.style.display = 'none';
            if (btn) { btn.disabled = false; btn.textContent = origLabel; }
            this.refreshLiveState();
        }
    },

    cancelInFlight: async function() {
        try {
            await App.api('POST', '/api/ace_step/cancel');
            App.toast('Cancel requested. Mid-step diffusion can\'t be interrupted; current step will finish.', 'info');
        } catch (err) {
            App.toast('Cancel failed: ' + (err && err.message ? err.message : err), 'error');
        }
    },

    // -----------------------------------------------------------------------
    // Progress polling
    // -----------------------------------------------------------------------
    _startProgressPolling: function() {
        var self = this;
        if (this.state.progressHandle) clearInterval(this.state.progressHandle);
        this.state.progressHandle = setInterval(async function() {
            try {
                var p = await App.api('GET', '/api/ace_step/progress');
                if (!p.active) return;
                var lblEl = document.getElementById('ace-step-progress-label');
                var barEl = document.getElementById('ace-step-progress-bar');
                if (lblEl) lblEl.textContent = (p.label || 'Working…') +
                    (p.total ? ' (' + (p.current + 1) + '/' + p.total + ')' : '');
                if (barEl && p.total > 0) {
                    var pct = Math.max(0, Math.min(100, ((p.current + 0.5) / p.total) * 100));
                    barEl.style.width = pct.toFixed(1) + '%';
                }
            } catch (err) { /* transient */ }
        }, 1500);
    },

    _stopProgressPolling: function() {
        if (this.state.progressHandle) {
            clearInterval(this.state.progressHandle);
            this.state.progressHandle = null;
        }
    },

    // -----------------------------------------------------------------------
    // Results gallery (for the most recent inference)
    // -----------------------------------------------------------------------
    _renderResults: function(data) {
        var el = document.getElementById('ace-step-results');
        if (!el) return;
        var results = data.results || [];
        if (!results.length) {
            el.innerHTML = '<div class="empty-state">No results.</div>';
            return;
        }
        var clapAvail = data.clap_available;
        var html = '<div class="card"><h2>Results — job ' + App.esc(data.job_id) + '</h2>';
        if (data.cancelled) html += '<div class="badge badge-orange">Cancelled (partial)</div>';
        if (typeof clapAvail === 'boolean' && !clapAvail) {
            html += '<div class="badge badge-orange" style="display:inline-block;margin-bottom:8px">' +
                    'CLAP-ranking unavailable — load the audio_lab worker with a CLAP model to enable ranking.</div>';
        }
        for (var i = 0; i < results.length; i++) {
            var r = results[i];
            var url = App.urlWithToken ? App.urlWithToken(r.url) : r.url;
            var ribbon = r.best ? '<span class="badge badge-green">★ best</span>' : '';
            var scoreBadge = (typeof r.score === 'number')
                ? '<span class="badge badge-purple">CLAP ' + r.score.toFixed(4) + '</span>'
                : '<span class="badge badge-gray">unranked</span>';
            html += '' +
                '<div style="margin:8px 0;padding:8px;border:1px solid var(--border,#222);border-radius:6px">' +
                  '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">' +
                    '<strong>#' + r.rank + '</strong>' + ribbon + scoreBadge +
                    (r.seed !== null && r.seed !== undefined ? '<span class="section-title">seed: ' + r.seed + '</span>' : '') +
                    (r.duration_s ? '<span class="section-title">' + r.duration_s.toFixed(1) + 's</span>' : '') +
                  '</div>' +
                  '<audio controls src="' + App.esc(url) + '" style="width:100%;margin-top:4px"></audio>' +
                  '<div style="margin-top:4px;display:flex;gap:6px;flex-wrap:wrap">' +
                    '<a class="btn btn-sm" href="' + App.esc(url) + '" download>Download</a>' +
                    '<button class="btn btn-sm" data-init-url="' + App.esc(r.url) +
                      '" onclick="TabAceStep.useAsInit(this.dataset.initUrl)">Use as source</button>' +
                    '<button class="btn btn-sm" data-ref-url="' + App.esc(r.url) +
                      '" onclick="TabAceStep.useAsReference(this.dataset.refUrl)">Use as style reference</button>' +
                    (r.seed !== null && r.seed !== undefined
                      ? '<button class="btn btn-sm" data-seed="' + App.esc(r.seed) +
                        '" onclick="TabAceStep.useAsSeed(parseInt(this.dataset.seed, 10))">Re-use seed</button>'
                      : '') +
                  '</div>' +
                '</div>';
        }
        html += '</div>';
        el.innerHTML = html;
    },

    useAsReference: function(url) {
        this.state.pendingRefUrl = url;
        var applied = 0;
        ['gen', 'simple', 'a2a', 'repaint', 'edit', 'extend', 'cover',
         'extract', 'lego', 'complete', 'vocal2bgm'].forEach(function(m) {
            var urlEl = document.getElementById('ace-step-' + m + '-ref-url');
            var b64El = document.getElementById('ace-step-' + m + '-ref-base64');
            var status = document.getElementById('ace-step-' + m + '-ref-status');
            if (urlEl) {
                urlEl.value = url;
                if (b64El) b64El.value = '';
                if (status) status.textContent = 'Using ' + url;
                applied += 1;
            }
        });
        if (applied) App.toast('Style reference set on ' + applied + ' form(s).', 'success');
        else App.toast('Style reference queued. Open Generate, Simple, or Cover to use it.', 'success');
    },

    useAsInit: function(url) {
        // Queue the URL so it gets applied when the user navigates to an init-audio
        // mode (whose form DOM doesn't exist yet). Also apply to any currently
        // rendered init-audio form.
        this.state.pendingInitUrl = url;
        var applied = 0;
        ['a2a', 'repaint', 'edit', 'extend', 'cover', 'extract', 'lego', 'complete', 'vocal2bgm'].forEach(function(m) {
            var urlEl = document.getElementById('ace-step-' + m + '-url');
            var b64El = document.getElementById('ace-step-' + m + '-base64');
            var status = document.getElementById('ace-step-' + m + '-status');
            if (urlEl) {
                urlEl.value = url;
                if (b64El) b64El.value = '';
                if (status) status.textContent = 'Using ' + url;
                applied += 1;
            }
        });
        if (applied) {
            App.toast('Init audio set on ' + applied + ' rendered form(s); queued for the rest.', 'success');
        } else {
            App.toast('Init audio queued. Switch to A2A / Repaint / Edit / Extend / Cover / Extract / Lego / Complete / Vocal→BGM to use it.', 'success');
        }
    },

    useAsSeed: function(seed) {
        this.state.pendingSeed = seed;
        var applied = 0;
        ['gen', 'simple', 'a2a', 'repaint', 'edit', 'extend', 'cover',
         'extract', 'lego', 'complete', 'lyric2vocal', 'text2samples'].forEach(function(m) {
            var el = document.getElementById('ace-step-' + m + '-seed');
            var rnd = document.getElementById('ace-step-' + m + '-seed-random');
            if (el) { el.value = seed; applied += 1; }
            if (rnd) rnd.checked = false;
        });
        if (applied) App.toast('Seed ' + seed + ' set across ' + applied + ' form(s).', 'success');
        else App.toast('Seed ' + seed + ' queued.', 'success');
    },

    _applyPendingAfterRender: function(mode) {
        // Called from setMode after innerHTML swap, so the new form's elements
        // can pick up the queued init URL / seed.
        if (this.state.pendingInitUrl &&
            ['a2a', 'repaint', 'edit', 'extend', 'cover', 'extract', 'lego', 'complete', 'vocal2bgm'].indexOf(mode) >= 0) {
            var urlEl = document.getElementById('ace-step-' + mode + '-url');
            var b64El = document.getElementById('ace-step-' + mode + '-base64');
            var status = document.getElementById('ace-step-' + mode + '-status');
            if (urlEl) urlEl.value = this.state.pendingInitUrl;
            if (b64El) b64El.value = '';
            if (status) status.textContent = 'Using ' + this.state.pendingInitUrl;
        }
        if (this.state.pendingRefUrl) {
            var refPrefix = mode === 'generate' ? 'ace-step-gen-ref' : ('ace-step-' + mode + '-ref');
            var refUrlEl = document.getElementById(refPrefix + '-url');
            var refB64El = document.getElementById(refPrefix + '-base64');
            var refStatus = document.getElementById(refPrefix + '-status');
            if (refUrlEl) {
                refUrlEl.value = this.state.pendingRefUrl;
                if (refB64El) refB64El.value = '';
                if (refStatus) refStatus.textContent = 'Using ' + this.state.pendingRefUrl;
            }
        }
        if (this.state.pendingSeed !== null) {
            var seedPrefix = mode === 'generate' ? 'gen' : mode;
            var seedEl = document.getElementById('ace-step-' + seedPrefix + '-seed');
            var rndEl = document.getElementById('ace-step-' + seedPrefix + '-seed-random');
            if (seedEl) seedEl.value = this.state.pendingSeed;
            if (rndEl) rndEl.checked = false;
        }
    },

    // -----------------------------------------------------------------------
    // Jobs view
    // -----------------------------------------------------------------------
    _renderJobsView: function() {
        return '' +
            '<div class="card">' +
              '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
                '<h2 style="margin:0">Jobs</h2>' +
                '<select id="ace-step-jobs-filter" class="input" onchange="TabAceStep.refreshJobs()" style="width:auto">' +
                  '<option value="">All modes</option>' +
                  '<option value="generate">generate</option>' +
                  '<option value="generate-ranked">generate-ranked</option>' +
                  '<option value="simple">simple</option>' +
                  '<option value="create-sample">create-sample</option>' +
                  '<option value="format-sample">format-sample</option>' +
                  '<option value="understand">understand</option>' +
                  '<option value="a2a">a2a</option>' +
                  '<option value="repaint">repaint</option>' +
                  '<option value="edit">edit</option>' +
                  '<option value="extend">extend</option>' +
                  '<option value="cover">cover</option>' +
                  '<option value="extract">extract</option>' +
                  '<option value="lego">lego</option>' +
                  '<option value="complete">complete</option>' +
                  '<option value="vocal2bgm">vocal2bgm</option>' +
                  '<option value="lyric2vocal">lyric2vocal</option>' +
                  '<option value="text2samples">text2samples</option>' +
                '</select>' +
                '<button class="btn btn-sm" onclick="TabAceStep.refreshJobs()">Refresh</button>' +
              '</div>' +
              '<div id="ace-step-jobs-list" class="empty-state" style="margin-top:8px">Loading…</div>' +
            '</div>';
    },

    refreshJobs: async function() {
        var mode = (document.getElementById('ace-step-jobs-filter') || {}).value || '';
        var q = 'limit=100' + (mode ? '&mode=' + encodeURIComponent(mode) : '');
        try {
            var data = await App.api('GET', '/api/ace_step/jobs?' + q);
            this.state.jobs = data.jobs || [];
            this._renderJobsList();
        } catch (err) {
            App.toast('Failed to load jobs: ' + (err && err.message ? err.message : err), 'error');
        }
    },

    _renderJobsList: function() {
        var el = document.getElementById('ace-step-jobs-list');
        if (!el) return;
        var jobs = this.state.jobs || [];
        if (!jobs.length) { el.innerHTML = 'No jobs yet.'; return; }
        var html = '';
        for (var i = 0; i < jobs.length; i++) {
            var j = jobs[i];
            var bestUrl = j.best_url ? (App.urlWithToken ? App.urlWithToken(j.best_url) : j.best_url) : null;
            var zipUrl = j.zip_url ? (App.urlWithToken ? App.urlWithToken(j.zip_url) : j.zip_url) : null;
            html += '' +
                '<div style="padding:8px 0;border-bottom:1px solid var(--border,#222)">' +
                  '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">' +
                    '<strong>' + App.esc(j.job_id.slice(0, 12)) + '…</strong>' +
                    '<span class="badge badge-gray">' + App.esc(j.mode || '') + '</span>' +
                    (j.model_variant ? '<span class="badge badge-gray">' + App.esc(this._modelDisplay(j.model_variant)) + '</span>' : '') +
                    (j.clap_available ? '<span class="badge badge-green">CLAP-ranked</span>' :
                                         '<span class="badge badge-gray">unranked</span>') +
                    '<span class="section-title">' + App.esc(j.created_at || '') + '</span>' +
                  '</div>' +
                  '<div class="section-title" style="margin-top:2px">' + App.esc(j.prompt || '') + '</div>' +
                  '<div style="margin-top:4px;display:flex;gap:6px;flex-wrap:wrap">' +
                    (bestUrl ? '<a class="btn btn-sm" href="' + App.esc(bestUrl) + '" download>Best wav</a>' : '') +
                    (zipUrl ? '<a class="btn btn-sm" href="' + App.esc(zipUrl) + '">Zip all</a>' : '') +
                    '<button class="btn btn-sm" onclick="TabAceStep.loadJob(\'' + App.esc(j.job_id) + '\')">View</button>' +
                    '<button class="btn btn-sm btn-danger" onclick="TabAceStep.deleteJob(\'' + App.esc(j.job_id) + '\')">Delete</button>' +
                  '</div>' +
                '</div>';
        }
        el.innerHTML = html;
    },

    loadJob: async function(jobId) {
        try {
            var data = await App.api('GET', '/api/ace_step/outputs/' + encodeURIComponent(jobId));
            var manifest = data.manifest || {};
            var results = (manifest.results || []).map(function(r) {
                return Object.assign({}, r);
            });
            this.state.lastJob = {
                job_id: jobId, mode: manifest.mode,
                results: results,
                cancelled: manifest.cancelled,
                clap_available: manifest.clap_available,
            };
            this.setMode(manifest.mode === 'generate-ranked' || manifest.mode === 'generate'
                ? 'generate' : (manifest.mode || 'generate'));
            this._renderResults(this.state.lastJob);
        } catch (err) {
            App.toast('Load job failed: ' + (err && err.message ? err.message : err), 'error');
        }
    },

    deleteJob: async function(jobId) {
        if (!confirm('Delete job ' + jobId + '? This removes all wav files.')) return;
        try {
            await App.api('DELETE', '/api/ace_step/jobs/' + encodeURIComponent(jobId));
            App.toast('Deleted.', 'success');
            this.refreshJobs();
        } catch (err) {
            App.toast('Delete failed: ' + (err && err.message ? err.message : err), 'error');
        }
    },

    // -----------------------------------------------------------------------
    // Outputs view (alias to jobs for now)
    // -----------------------------------------------------------------------
    _renderOutputsView: function() {
        return this._renderJobsView();
    },

    // -----------------------------------------------------------------------
    // Install view (the original Phase 1 content, now a sub-tab)
    // -----------------------------------------------------------------------
    _renderInstallView: function() {
        return '' +
            '<div class="card">' +
              '<h2>Status</h2>' +
              '<div id="ace-step-status-summary" class="empty-state">Loading…</div>' +
              '<p class="section-title" style="margin-top:8px">' +
                'Use this tab to install, update, and remove ACE-Step assets. ' +
                'Once a model and LM are installed, switch to the Generate tab to start producing.' +
              '</p>' +
            '</div>' +

            '<div class="card" id="ace-step-models-card">' +
              '<h2>Base Models</h2>' +
              '<p class="section-title">' +
                'ACE-Step 1.5 checkpoints. XL Turbo is the recommended default ' +
                '(8-step inference, ~12 GB VRAM with INT8+offload). 2B family is the low-VRAM tier.' +
              '</p>' +
              '<div id="ace-step-models-list" class="empty-state">Loading…</div>' +
            '</div>' +

            '<div class="card" id="ace-step-lms-card">' +
              '<h2>5 Hz Text Encoders (LM)</h2>' +
              '<p class="section-title">' +
                'Qwen3-based condition encoders. Required by every native DiT.' +
              '</p>' +
              '<div id="ace-step-lms-list" class="empty-state">Loading…</div>' +
            '</div>' +

            '<div class="card" id="ace-step-vaes-card">' +
              '<h2>VAE Swaps</h2>' +
              '<p class="section-title">Optional 1D autoencoder replacement applied at load time.</p>' +
              '<div id="ace-step-vaes-list" class="empty-state">Loading…</div>' +
            '</div>' +

            '<div class="card" id="ace-step-loras-card">' +
              '<h2>LoRAs</h2>' +
              '<p class="section-title">' +
                'PEFT-style adapters. lyric2vocal and text2samples enable distinct ' +
                'inference modes (auto-attached at run time).' +
              '</p>' +
              '<div id="ace-step-loras-list" class="empty-state">Loading…</div>' +
            '</div>' +

            '<div class="card" id="ace-step-custom-card">' +
              '<h2>Custom HuggingFace Repo</h2>' +
              '<p class="section-title">' +
                'Install any HF repo as an ACE-Step asset. Sandboxed under ' +
                'custom/&lt;kind&gt;/&lt;name&gt;/.' +
              '</p>' +
              '<div class="form-row" style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">' +
                '<div style="flex:2;min-width:200px">' +
                  '<label class="section-title" style="display:block;margin-bottom:4px">Repo (org/name)</label>' +
                  '<input type="text" id="ace-step-custom-repo" class="input" placeholder="someuser/some-ace-step-tune" style="width:100%">' +
                '</div>' +
                '<div style="flex:1;min-width:140px">' +
                  '<label class="section-title" style="display:block;margin-bottom:4px">Local name (optional)</label>' +
                  '<input type="text" id="ace-step-custom-name" class="input" placeholder="auto-derived from repo" style="width:100%">' +
                '</div>' +
                '<div style="min-width:120px">' +
                  '<label class="section-title" style="display:block;margin-bottom:4px">Kind</label>' +
                  '<select id="ace-step-custom-kind" class="input">' +
                    '<option value="model">Model</option><option value="lm">LM</option>' +
                    '<option value="vae">VAE</option><option value="lora">LoRA</option>' +
                  '</select>' +
                '</div>' +
                '<button class="btn btn-primary" onclick="TabAceStep.installCustom(this)">Install</button>' +
              '</div>' +
              '<div id="ace-step-custom-list" style="margin-top:12px"></div>' +
            '</div>';
    },

    _renderInstall: function() {
        this.renderSummary();
        this.renderModels();
        this.renderLMs();
        this.renderVAEs();
        this.renderLoRAs();
        this.renderCustom();
    },

    renderSummary: function() {
        var el = document.getElementById('ace-step-status-summary');
        if (!el) return;
        var count = function(rows) {
            var n = 0;
            for (var i = 0; i < rows.length; i++) if (rows[i].installed) n++;
            return n;
        };
        var custom = this.state.custom || {};
        var customN =
            (custom.model || []).length + (custom.lm || []).length +
            (custom.vae || []).length + (custom.lora || []).length;
        el.innerHTML =
            '<div style="display:flex;gap:16px;flex-wrap:wrap">' +
              '<span><strong>' + count(this.state.models) + '/' + this.state.models.length + '</strong> models</span>' +
              '<span><strong>' + count(this.state.core)   + '/' + this.state.core.length   + '</strong> shared core</span>' +
              '<span><strong>' + count(this.state.lms)    + '/' + this.state.lms.length    + '</strong> LMs</span>' +
              '<span><strong>' + count(this.state.vaes)   + '/' + this.state.vaes.length   + '</strong> VAEs</span>' +
              '<span><strong>' + count(this.state.loras)  + '/' + this.state.loras.length  + '</strong> LoRAs</span>' +
              '<span><strong>' + customN + '</strong> custom</span>' +
            '</div>';
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

    _modeBadge: function(mode) {
        if (!mode) return '';
        return '<span class="badge badge-purple" title="Auto-attached when this mode is invoked">enables: ' + App.esc(mode) + '</span>';
    },

    _row: function(entry, kind) {
        var key = (kind === 'lora') ? entry.name : entry.variant_id;
        var installed = !!entry.installed;
        var btnAction = installed ? 'delete' : 'install';
        var btnClass = installed ? 'btn btn-danger btn-sm' : 'btn btn-primary btn-sm';
        var nativeModel = kind === 'model' && entry.format === 'native';
        var btnLabel = installed ? 'Delete' : (nativeModel ? 'Install all' : 'Install');
        var sizeStr = entry.size_gb ? ('~' + entry.size_gb + 'GB') : '';
        var repoLink = entry.repo
            ? '<a href="https://huggingface.co/' + App.esc(entry.repo) +
              '" target="_blank" rel="noopener noreferrer" style="color:inherit;text-decoration:none">' +
              App.esc(entry.repo) + ' ↗</a>' : '';
        var status = installed
            ? '<span class="badge badge-green">Installed</span>'
            : '<span class="badge badge-gray">Not installed</span>';
        var defaultLM = entry.default_lm
            ? '<span class="badge badge-gray" title="Recommended pairing">default LM: ' + App.esc(entry.default_lm) + '</span>' : '';
        var autoPrereqs = nativeModel
            ? '<span class="badge badge-blue" title="Installer also downloads the shared ACE 1.5 core and recommended LM if missing">auto core + LM</span>'
            : '';
        return (
            '<div class="ace-step-row" data-key="' + App.esc(key) + '" ' +
                 'style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--border,#222)">' +
              '<div style="flex:1;min-width:0">' +
                '<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">' +
                  '<strong>' + App.esc(entry.display || key) + '</strong>' +
                  this._tierBadge(entry.tier) +
                  (kind === 'model' ? this._formatBadge(entry.format) : '') +
                  (kind === 'lora'  ? this._modeBadge(entry.enables_mode) : '') +
                  (kind === 'model' ? defaultLM : '') +
                  autoPrereqs +
                  status +
                '</div>' +
                '<div class="section-title" style="margin-top:2px">' +
                  repoLink + (sizeStr ? ' · ' + sizeStr : '') +
                '</div>' +
              '</div>' +
              '<button class="' + btnClass + '" data-kind="' + kind + '" data-key="' + App.esc(key) + '" ' +
                 'data-action="' + btnAction + '" onclick="TabAceStep.handleClick(this)">' + btnLabel + '</button>' +
            '</div>'
        );
    },

    _groupedHtml: function(rows, kind) {
        var groups = { official: [], community: [], untested: [] };
        for (var i = 0; i < rows.length; i++) {
            var t = rows[i].tier || 'official';
            (groups[t] || groups.community).push(rows[i]);
        }
        var labels = {
            official: 'Official', community: 'Community',
            untested: 'Untested — may fail to load',
        };
        var html = '';
        ['official', 'community', 'untested'].forEach(function(tier) {
            if (!groups[tier].length) return;
            html += '<h3 class="section-title" style="margin-top:14px">' + labels[tier] + '</h3>';
            for (var i = 0; i < groups[tier].length; i++) html += TabAceStep._row(groups[tier][i], kind);
        });
        return html;
    },

    renderModels: function() {
        var el = document.getElementById('ace-step-models-list'); if (!el) return;
        if (!this.state.models.length) { el.innerHTML = '<div class="empty-state">No models in registry.</div>'; return; }
        el.innerHTML = this._groupedHtml(this.state.models, 'model');
    },

    renderLMs: function() {
        var el = document.getElementById('ace-step-lms-list'); if (!el) return;
        if (!this.state.lms.length) { el.innerHTML = '<div class="empty-state">No LMs in registry.</div>'; return; }
        var html = '';
        for (var i = 0; i < this.state.lms.length; i++) html += this._row(this.state.lms[i], 'lm');
        el.innerHTML = html;
    },

    renderVAEs: function() {
        var el = document.getElementById('ace-step-vaes-list'); if (!el) return;
        if (!this.state.vaes.length) { el.innerHTML = '<div class="empty-state">No VAEs in registry.</div>'; return; }
        var html = '';
        for (var i = 0; i < this.state.vaes.length; i++) {
            var v = this.state.vaes[i];
            if (v.variant_id === 'default') continue;
            html += this._row(v, 'vae');
        }
        if (!html) html = '<div class="empty-state">Only the default VAE is registered.</div>';
        el.innerHTML = html;
    },

    renderLoRAs: function() {
        var el = document.getElementById('ace-step-loras-list'); if (!el) return;
        if (!this.state.loras.length) { el.innerHTML = '<div class="empty-state">No LoRAs in registry.</div>'; return; }
        el.innerHTML = this._groupedHtml(this.state.loras, 'lora');
    },

    renderCustom: function() {
        var el = document.getElementById('ace-step-custom-list'); if (!el) return;
        var custom = this.state.custom || {};
        var kinds = ['model', 'lm', 'vae', 'lora'];
        var any = false;
        var html = '';
        for (var k = 0; k < kinds.length; k++) {
            var kind = kinds[k];
            var rows = custom[kind] || [];
            if (!rows.length) continue;
            any = true;
            html += '<h3 class="section-title" style="margin-top:10px">Custom ' + kind + 's</h3>';
            for (var i = 0; i < rows.length; i++) {
                var r = rows[i];
                var repoLink = r.repo
                    ? '<a href="https://huggingface.co/' + App.esc(r.repo) +
                      '" target="_blank" rel="noopener noreferrer" style="color:inherit;text-decoration:none">' +
                      App.esc(r.repo) + ' ↗</a>' : '';
                html += '' +
                    '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border,#222)">' +
                      '<div style="flex:1;min-width:0">' +
                        '<strong>' + App.esc(r.name) + '</strong> ' +
                        '<span class="badge badge-gray">' + kind + '</span> ' +
                        '<span class="badge badge-green">Installed</span>' +
                        '<div class="section-title" style="margin-top:2px">' + repoLink + '</div>' +
                      '</div>' +
                      '<button class="btn btn-danger btn-sm" data-custom-kind="' + kind + '" data-name="' + App.esc(r.name) + '" onclick="TabAceStep.deleteCustom(this)">Delete</button>' +
                    '</div>';
            }
        }
        if (!any) html = '<div class="empty-state section-title" style="margin-top:8px">No custom installs yet.</div>';
        el.innerHTML = html;
    },

    handleClick: function(btn) {
        var kind = btn.dataset.kind, key = btn.dataset.key, action = btn.dataset.action;
        if (action === 'install') this.installAsset(kind, key, btn);
        else if (action === 'delete') this.deleteAsset(kind, key, btn);
    },

    installAsset: async function(kind, key, btn) {
        var path, body;
        if (kind === 'model') { path = '/api/ace_step/install-model'; body = { variant_id: key }; }
        else if (kind === 'lm')  { path = '/api/ace_step/install-lm';   body = { variant_id: key }; }
        else if (kind === 'vae') { path = '/api/ace_step/install-vae';  body = { variant_id: key }; }
        else if (kind === 'lora'){ path = '/api/ace_step/install-lora'; body = { name: key }; }
        else { App.toast('Unknown kind: ' + kind, 'error'); return; }
        btn.disabled = true; var origLabel = btn.textContent; btn.textContent = 'Starting…';
        try {
            var data = await App.api('POST', path, body);
            var note = '';
            if (kind === 'model') {
                var row = this._modelStatus(key);
                if (row && row.format === 'native') note = ' plus shared core/LM if missing';
            }
            App.toast('Installing ' + key + note + ' (job ' + data.job_id + ')', 'info');
            btn.textContent = 'Downloading…';
            this._pollJob(data.job_id, btn, kind, key, origLabel);
            App.loadInstallJobs && App.loadInstallJobs();
        } catch (err) {
            App.toast('Install failed: ' + (err && err.message ? err.message : err), 'error');
            btn.disabled = false; btn.textContent = origLabel;
        }
    },

    installCoreBundle: async function(btn) {
        if (this._coreReady()) {
            App.toast('ACE-Step shared core is already installed', 'info');
            return;
        }
        btn.disabled = true;
        var origLabel = btn.textContent;
        btn.textContent = 'Starting...';
        try {
            var data = await App.api('POST', '/api/ace_step/install-model', { variant_id: 'ace-1.5' });
            App.toast('Installing ACE-Step shared core (job ' + data.job_id + ')', 'info');
            btn.textContent = 'Downloading...';
            this._pollJob(data.job_id, btn, 'model', 'ace-1.5', origLabel);
            App.loadInstallJobs && App.loadInstallJobs();
        } catch (err) {
            App.toast('Shared core install failed: ' + (err && err.message ? err.message : err), 'error');
            btn.disabled = false;
            btn.textContent = origLabel;
        }
    },

    deleteAsset: async function(kind, key, btn) {
        if (!confirm('Delete ' + kind + ' "' + key + '"?')) return;
        var path;
        if (kind === 'model') path = '/api/ace_step/install-model/';
        else if (kind === 'lm')  path = '/api/ace_step/install-lm/';
        else if (kind === 'vae') path = '/api/ace_step/install-vae/';
        else if (kind === 'lora')path = '/api/ace_step/install-lora/';
        else { App.toast('Unknown kind: ' + kind, 'error'); return; }
        btn.disabled = true; var origLabel = btn.textContent; btn.textContent = 'Deleting…';
        try {
            await App.api('DELETE', path + encodeURIComponent(key));
            App.toast(kind + ' "' + key + '" deleted', 'success');
            await this.refreshStatus();
        } catch (err) {
            App.toast('Delete failed: ' + (err && err.message ? err.message : err), 'error');
            btn.disabled = false; btn.textContent = origLabel;
        }
    },

    installCustom: async function(btn) {
        var repo = (document.getElementById('ace-step-custom-repo') || {}).value || '';
        var name = (document.getElementById('ace-step-custom-name') || {}).value || '';
        var kind = (document.getElementById('ace-step-custom-kind') || {}).value || 'model';
        repo = repo.trim(); name = name.trim();
        if (!repo) { App.toast('Enter a HuggingFace repo (org/name)', 'error'); return; }
        if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$/.test(repo)) {
            App.toast('Repo must look like "org/name"', 'error'); return;
        }
        var body = { repo: repo, kind: kind };
        if (name) body.name = name;
        btn.disabled = true; var origLabel = btn.textContent; btn.textContent = 'Starting…';
        try {
            var data = await App.api('POST', '/api/ace_step/install-custom', body);
            App.toast('Installing ' + (data.name || repo) + ' (job ' + data.job_id + ')', 'info');
            btn.textContent = 'Downloading…';
            this._pollCustomJob(data.job_id, btn, kind, data.name || name || repo.replace('/', '_'), origLabel);
            App.loadInstallJobs && App.loadInstallJobs();
        } catch (err) {
            App.toast('Custom install failed: ' + (err && err.message ? err.message : err), 'error');
            btn.disabled = false; btn.textContent = origLabel;
        }
    },

    deleteCustom: async function(btn) {
        var kind = btn.dataset.customKind, name = btn.dataset.name;
        if (!confirm('Delete custom ' + kind + ' "' + name + '"?')) return;
        btn.disabled = true; var origLabel = btn.textContent; btn.textContent = 'Deleting…';
        try {
            await App.api('DELETE', '/api/ace_step/install-custom/' + encodeURIComponent(kind) + '/' + encodeURIComponent(name));
            App.toast('Custom ' + kind + ' "' + name + '" deleted', 'success');
            await this.refreshStatus();
        } catch (err) {
            App.toast('Delete failed: ' + (err && err.message ? err.message : err), 'error');
            btn.disabled = false; btn.textContent = origLabel;
        }
    },

    _stopPoll: function(jobId) {
        var h = this.state.jobsInFlight[jobId];
        if (h) { clearInterval(h); delete this.state.jobsInFlight[jobId]; }
    },

    _pollJob: function(jobId, btn, kind, key, origLabel) {
        var self = this;
        this._stopPoll(jobId);
        var handle = setInterval(async function() {
            if (btn && !document.body.contains(btn)) { self._stopPoll(jobId); return; }
            try {
                var job = await App.api('GET', '/api/setup/jobs/' + jobId);
                if (job.status === 'completed') {
                    self._stopPoll(jobId);
                    App.toast(kind + ' "' + key + '" installed', 'success');
                    await self.refreshStatus();
                } else if (job.status === 'failed' || job.status === 'cancelled') {
                    self._stopPoll(jobId);
                    btn.disabled = false; btn.textContent = origLabel || 'Install';
                    App.toast('Install ' + job.status + ': ' + (job.error || 'see logs'), 'error');
                }
            } catch (err) {
                if (err && /404/.test(err.message || '')) {
                    self._stopPoll(jobId);
                    btn.disabled = false; btn.textContent = origLabel || 'Install';
                    App.toast('Install job ' + jobId + ' missing', 'error');
                }
            }
        }, 3000);
        this.state.jobsInFlight[jobId] = handle;
    },

    _pollCustomJob: function(jobId, btn, kind, name, origLabel) {
        var self = this;
        this._stopPoll(jobId);
        var handle = setInterval(async function() {
            if (btn && !document.body.contains(btn)) { self._stopPoll(jobId); return; }
            try {
                var job = await App.api('GET', '/api/setup/jobs/' + jobId);
                if (job.status === 'completed') {
                    self._stopPoll(jobId);
                    App.toast('Custom ' + kind + ' "' + name + '" installed', 'success');
                    btn.disabled = false; btn.textContent = origLabel || 'Install';
                    var repoEl = document.getElementById('ace-step-custom-repo');
                    var nameEl = document.getElementById('ace-step-custom-name');
                    if (repoEl) repoEl.value = ''; if (nameEl) nameEl.value = '';
                    await self.refreshStatus();
                } else if (job.status === 'failed' || job.status === 'cancelled') {
                    self._stopPoll(jobId);
                    btn.disabled = false; btn.textContent = origLabel || 'Install';
                    App.toast('Custom install ' + job.status + ': ' + (job.error || 'see logs'), 'error');
                }
            } catch (err) {
                if (err && /404/.test(err.message || '')) {
                    self._stopPoll(jobId);
                    btn.disabled = false; btn.textContent = origLabel || 'Install';
                    App.toast('Install job ' + jobId + ' missing', 'error');
                }
            }
        }, 3000);
        this.state.jobsInFlight[jobId] = handle;
    },
};
