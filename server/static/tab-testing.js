/* ==========================================================================
   Tab: Testing — load downloaded weights, then run single-shot inference
   --------------------------------------------------------------------------
   Download weights on the Models tab. Here you pick installed variants per
   model family (repo) and load them, then send a one-off prompt to inspect
   the raw reply. For multi-turn chat, use the Chat tab.
   ========================================================================== */

var TabTesting = {
    _model: '',
    _variant: '',
    _device: '',
    _precision: '',
    _temperature: 0.7,
    _topP: 0.9,
    _maxNewTokens: 512,
    _busy: false,
    _lastResponseText: null,
    _lastResponseColor: null,
    _draftText: '',

    _alSa: '',
    _alClap: '',
    _alVae: 'default',
    _asModel: '',
    _asLm: '',
    _asVae: 'default',

    audioLab: { models: [], vaes: [], claps: [] },
    aceStep: { models: [], lms: [], vaes: [], custom: { model: [], lm: [], vae: [], lora: [] } },

    init: function() {
        var self = this;
        this.refreshAssets().then(function() { self.render(); });
    },

    onActivate: function() {
        var self = this;
        this.refreshAssets().then(function() { self.render(); });
    },

    refreshAssets: async function() {
        await App.loadSetupStatus();
        try {
            var al = await App.api('GET', '/api/audio_lab/status');
            this.audioLab = {
                models: al.models || [],
                vaes: al.vaes || [],
                claps: al.claps || [],
            };
        } catch (e) { /* keep last-known */ }
        try {
            var as = await App.api('GET', '/api/ace_step/status');
            this.aceStep = {
                models: as.models || [],
                lms: as.lms || [],
                vaes: as.vaes || [],
                custom: as.custom || { model: [], lm: [], vae: [], lora: [] },
            };
        } catch (e) { /* keep last-known */ }
    },

    _batchModels: function() {
        var models = App.state.models || {};
        var hidden = { moshi: true, audio_lab: true, ace_step: true, moss_tts: true, moss_sfx: true };
        return Object.keys(models).filter(function(m) { return !hidden[m]; });
    },

    _installedVariantMap: function(model) {
        var variantStatus = (App.state.setupStatus[model] && App.state.setupStatus[model].variants) || [];
        var installed = {};
        for (var i = 0; i < variantStatus.length; i++) {
            if (variantStatus[i].installed) installed[variantStatus[i].variant_id] = true;
        }
        return installed;
    },

    _installedOmniVariants: function(model) {
        var variants = (App.state.variants && App.state.variants[model]) || [];
        var installed = this._installedVariantMap(model);
        var out = [];
        for (var i = 0; i < variants.length; i++) {
            if (installed[variants[i].variant_id]) out.push(variants[i]);
        }
        return out;
    },

    _resolveDevice: function() {
        var devs = App.state.devices || [];
        for (var i = 0; i < devs.length; i++) {
            if (devs[i].id === this._device) return this._device;
        }
        return devs.length ? devs[0].id : '';
    },

    _workerForModel: function(model) {
        var workers = App.state.workers || [];
        for (var i = 0; i < workers.length; i++) {
            var w = workers[i];
            if (w.model === model && w.status !== 'dead') return w;
        }
        return null;
    },

    _variantLabel: function(entry) {
        var label = entry.display || entry.variant_id || entry.name || '';
        if (entry.repo) label += ' · ' + entry.repo;
        return label;
    },

    _buildSelectOptions: function(entries, currentValue, emptyLabel) {
        var E = App.esc;
        if (!entries.length) {
            return '<option value="">' + E(emptyLabel || '— nothing downloaded —') + '</option>';
        }
        var html = '';
        var pick = currentValue;
        if (!pick || !entries.some(function(e) { return (e.variant_id || e.name) === pick; })) {
            pick = entries[0].variant_id || entries[0].name || '';
        }
        for (var i = 0; i < entries.length; i++) {
            var e = entries[i];
            var val = e.variant_id || e.name || '';
            var sel = (val === pick) ? ' selected' : '';
            html += '<option value="' + E(val) + '"' + sel + '>' + E(this._variantLabel(e)) + '</option>';
        }
        return { html: html, pick: pick };
    },

    _installedEntries: function(list, skipDefault) {
        var out = [];
        for (var i = 0; i < (list || []).length; i++) {
            var e = list[i];
            if (skipDefault && e.variant_id === 'default') continue;
            if (e.installed) out.push(e);
        }
        return out;
    },

    _customAceEntries: function(kind, prefix) {
        var rows = (this.aceStep.custom && this.aceStep.custom[kind]) || [];
        return rows.map(function(row) {
            return {
                variant_id: 'custom:' + row.name,
                display: prefix + ': ' + row.name,
                repo: row.repo,
                installed: true,
            };
        });
    },

    render: function() {
        if (this._busy) return;
        var el = document.getElementById('tab-testing');
        if (!el) return;
        var models = App.state.models || {};
        var E = App.esc;
        var self = this;

        var batchModels = this._batchModels();
        if (batchModels.indexOf(this._model) === -1) {
            this._model = batchModels.length ? batchModels[0] : '';
        }
        var model = this._model;
        this._device = this._resolveDevice();

        var installedVariants = model ? this._installedOmniVariants(model) : [];
        var variantOpts = this._buildSelectOptions(
            installedVariants,
            this._variant,
            '— download variants on Models tab —'
        );
        this._variant = variantOpts.pick;

        var devs = App.state.devices || [];
        var deviceOpts = devs.map(function(d) {
            return { value: d.id, label: d.name || d.id };
        });

        var worker = model ? this._workerForModel(model) : null;
        var ready = model ? App.workerReadyFor(model) : false;
        var modelInstalled = model ? App.modelInstalled(model) : false;

        var html = '';

        // --- Load omni worker ------------------------------------------------
        html += '<div class="card"><h2>Load omni model worker</h2>';
        html += '<p style="margin:0 0 12px;font-size:12px;color:var(--text-secondary)">'
            + 'Pick a downloaded weight set (per HuggingFace repo) for the model family, then spawn a worker. '
            + 'Download new weights on the <strong>Models</strong> tab first.</p>';

        if (!modelInstalled && model) {
            html += App.prereqBanner({
                level: 'warn',
                text: App.modelDisplay(model) + ' has no downloaded weights yet.',
                actionId: 'test-goto-models',
                actionLabel: 'Open Models',
            });
        } else if (model && installedVariants.length === 0) {
            html += App.prereqBanner({
                level: 'warn',
                text: 'No variants downloaded for ' + App.modelDisplay(model) + ' yet.',
                actionId: 'test-goto-models',
                actionLabel: 'Open Models',
            });
        }

        var modelOptions = batchModels.map(function(m) {
            return { value: m, label: App.modelDisplay(m) };
        });

        html += '<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end">';
        html += '<div style="flex:1;min-width:160px">' + App.field({
            type: 'select', id: 'test-load-model', label: 'Model family',
            options: modelOptions, value: model,
            hint: 'Omni model family to load.',
        }) + '</div>';
        html += '<div style="flex:1;min-width:220px">' + App.field({
            type: 'select', id: 'test-load-variant', label: 'Downloaded variant (repo)',
            options: [{ value: '', label: '—' }],
            value: this._variant,
            hint: 'Only weights you have downloaded appear here.',
        }) + '</div>';
        html += '<div style="flex:1;min-width:160px">' + App.field({
            type: 'select', id: 'test-load-device', label: 'Device',
            options: deviceOpts, value: this._device,
            hint: 'GPU or CPU for this worker.',
        }) + '</div>';
        html += '<div style="flex:1;min-width:140px">' + App.field({
            type: 'select', id: 'test-load-precision', label: 'Precision',
            options: [
                { value: '', label: 'auto (bf16 on CUDA)' },
                { value: 'fp16', label: 'fp16' },
                { value: 'bf16', label: 'bf16' },
                { value: 'fp32', label: 'fp32' },
            ],
            value: this._precision,
            hint: 'Optional dtype override.',
        }) + '</div>';
        var spawnDisabled = !model || installedVariants.length === 0;
        html += '<div style="flex:0 0 auto"><button id="test-spawn-btn" class="btn btn-primary btn-sm"'
            + (spawnDisabled ? ' disabled' : '') + '>Spawn worker</button></div>';
        html += '</div>';

        if (worker) {
            var wVariant = worker.variant || 'default';
            var statusCls = worker.status === 'ready' ? 'badge-green' : 'badge-orange';
            html += '<div style="margin-top:10px;font-size:12px;color:var(--text-secondary)">'
                + 'Worker: <span class="badge ' + statusCls + '">' + E(worker.status) + '</span> '
                + '· variant <code>' + E(wVariant) + '</code> '
                + '· ' + E(worker.device || '') + '</div>';
        } else if (model) {
            html += '<div style="margin-top:10px;font-size:12px;color:var(--text-muted)">No worker running for this family.</div>';
        }
        html += '</div>';

        // --- Load Audio Lab --------------------------------------------------
        var alSa = this._installedEntries(this.audioLab.models);
        var alClap = this._installedEntries(this.audioLab.claps);
        var alVae = this._installedEntries(this.audioLab.vaes, true);
        var saOpts = this._buildSelectOptions(alSa, this._alSa, '— download on Models tab —');
        var clapOpts = this._buildSelectOptions(alClap, this._alClap, '— optional —');
        this._alSa = saOpts.pick;
        this._alClap = clapOpts.pick;

        html += '<div class="card mt-16"><h2>Load Audio Lab</h2>';
        html += '<p style="margin:0 0 12px;font-size:12px;color:var(--text-secondary)">'
            + 'Pick downloaded Stable Audio / CLAP / VAE weights, then load into the audio_lab worker.</p>';
        html += '<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end">';
        html += '<div style="flex:2;min-width:200px"><label class="section-title" style="display:block;margin-bottom:4px">Stable Audio</label>'
            + '<select id="test-al-sa" class="input">' + saOpts.html + '</select></div>';
        html += '<div style="flex:2;min-width:200px"><label class="section-title" style="display:block;margin-bottom:4px">CLAP</label>'
            + '<select id="test-al-clap" class="input">' + clapOpts.html + '</select></div>';
        html += '<div style="flex:1;min-width:140px"><label class="section-title" style="display:block;margin-bottom:4px">VAE</label>'
            + '<select id="test-al-vae" class="input"><option value="default"' + (this._alVae === 'default' ? ' selected' : '') + '>Default</option>'
            + alVae.map(function(v) {
                var sel = v.variant_id === self._alVae ? ' selected' : '';
                return '<option value="' + E(v.variant_id) + '"' + sel + '>' + E(self._variantLabel(v)) + '</option>';
            }).join('') + '</select></div>';
        html += '<div style="flex:0 0 auto"><button id="test-al-load-btn" class="btn btn-primary btn-sm"'
            + ((!saOpts.pick && !clapOpts.pick) ? ' disabled' : '') + '>Load</button></div>';
        html += '</div></div>';

        // --- Load ACE Step ---------------------------------------------------
        var asModels = this._installedEntries(this.aceStep.models)
            .concat(this._customAceEntries('model', 'Custom model'));
        var asLms = this._installedEntries(this.aceStep.lms)
            .concat(this._customAceEntries('lm', 'Custom LM'));
        var asVae = this._installedEntries(this.aceStep.vaes, true);
        var asModelOpts = this._buildSelectOptions(asModels, this._asModel, '— download on Models tab —');
        var asLmOpts = this._buildSelectOptions(asLms, this._asLm, '— optional (model default) —');
        this._asModel = asModelOpts.pick;
        this._asLm = asLmOpts.pick;

        html += '<div class="card mt-16"><h2>Load ACE Step</h2>';
        html += '<p style="margin:0 0 12px;font-size:12px;color:var(--text-secondary)">'
            + 'Pick downloaded DiT / LM / VAE weights per repo, then load for song generation tests on the ACE Step tab.</p>';
        html += '<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end">';
        html += '<div style="flex:2;min-width:200px"><label class="section-title" style="display:block;margin-bottom:4px">DiT model</label>'
            + '<select id="test-as-model" class="input">' + asModelOpts.html + '</select></div>';
        html += '<div style="flex:2;min-width:200px"><label class="section-title" style="display:block;margin-bottom:4px">Text encoder (LM)</label>'
            + '<select id="test-as-lm" class="input">' + asLmOpts.html + '</select></div>';
        html += '<div style="flex:1;min-width:140px"><label class="section-title" style="display:block;margin-bottom:4px">VAE</label>'
            + '<select id="test-as-vae" class="input"><option value="default"' + (this._asVae === 'default' ? ' selected' : '') + '>Default</option>'
            + asVae.map(function(v) {
                var sel = v.variant_id === self._asVae ? ' selected' : '';
                return '<option value="' + E(v.variant_id) + '"' + sel + '>' + E(self._variantLabel(v)) + '</option>';
            }).join('') + '</select></div>';
        html += '<div style="flex:0 0 auto"><button id="test-as-load-btn" class="btn btn-primary btn-sm"'
            + (!asModelOpts.pick ? ' disabled' : '') + '>Load</button></div>';
        html += '</div></div>';

        // --- Single-shot tester ----------------------------------------------
        html += '<div class="card mt-16"><h2>Single-Shot Tester</h2>';
        html += '<p style="margin:0 0 14px;font-size:12px;color:var(--text-secondary)">'
            + 'Send one prompt to a loaded omni worker and inspect the raw reply. '
            + 'For conversations with history, use the <strong>Chat</strong> tab.</p>';

        html += App.field({
            type: 'select',
            id: 'test-model',
            label: 'Test against worker',
            hint: 'Uses the worker spawned above for this model family.',
            value: model,
            options: modelOptions,
            disabled: batchModels.length === 0,
        });

        if (model && !ready) {
            html += App.prereqBanner({
                level: 'warn',
                text: 'No ready worker for ' + App.modelDisplay(model) + '. Spawn one above with a downloaded variant.',
                actionId: 'test-spawn-worker',
                actionLabel: 'Spawn worker',
                actionDisabled: spawnDisabled,
            });
        }

        html += App.field({
            type: 'textarea',
            id: 'test-input',
            label: 'Prompt',
            rows: 3,
            placeholder: 'Type your message…',
            hint: 'The text prompt sent to the worker. Add optional media below.',
        });

        html += App.field({
            type: 'slider', id: 'test-temp', label: 'Temperature',
            tip: 'Sampling randomness. Lower is more focused; higher is more creative.',
            min: 0, max: 2, step: 0.05, value: this._temperature,
            hint: 'Higher = more random. 0.7 is a balanced default.',
        });
        html += App.field({
            type: 'slider', id: 'test-topp', label: 'Top-p',
            tip: 'Nucleus sampling cutoff.',
            min: 0, max: 1, step: 0.05, value: this._topP,
            hint: 'Caps the sampling pool by cumulative probability.',
        });
        html += App.field({
            type: 'slider', id: 'test-maxtok', label: 'Max new tokens',
            tip: 'Upper bound on generated tokens.',
            min: 32, max: 4096, step: 32, value: this._maxNewTokens, unit: ' tok',
            hint: 'Maximum length of the generated reply.',
        });

        html += '<div style="display:flex;gap:14px;align-items:center;margin:4px 0 12px;flex-wrap:wrap">';
        html += '<label style="font-size:12px;color:var(--text-secondary);display:flex;gap:6px;align-items:center">Image <input type="file" id="test-image" accept="image/*" style="font-size:12px"></label>';
        html += '<label style="font-size:12px;color:var(--text-secondary);display:flex;gap:6px;align-items:center">Audio <input type="file" id="test-audio" accept="audio/*" style="font-size:12px"></label>';
        html += '<label style="font-size:12px;color:var(--text-secondary);display:flex;gap:6px;align-items:center">Video <input type="file" id="test-video" accept="video/*" style="font-size:12px"></label>';
        html += '</div>';

        var runDisabled = !ready;
        html += '<div style="display:flex;gap:10px;align-items:center;margin-bottom:12px">';
        html += '<button id="test-send-btn" class="btn btn-primary btn-sm"' + (runDisabled ? ' disabled' : '') + '>Run</button>';
        if (runDisabled) {
            html += '<span style="font-size:12px;color:var(--text-muted)">'
                + (model ? 'Spawn a worker with a downloaded variant to run.' : 'No batch-capable models available.')
                + '</span>';
        }
        html += '</div>';

        html += '<div id="test-response" style="min-height:100px;background:var(--bg-input);border:1px solid var(--border);border-radius:var(--radius);padding:12px;font-size:13px;white-space:pre-wrap;color:var(--text-secondary)">Response will appear here…</div>';
        html += '</div>';

        el.innerHTML = html;

        // Populate variant select (built separately so repo labels stay intact).
        var variantSel = document.getElementById('test-load-variant');
        if (variantSel) variantSel.innerHTML = variantOpts.html;

        this._bindHandlers();
    },

    _bindHandlers: function() {
        var self = this;

        var gotoModels = document.getElementById('test-goto-models');
        if (gotoModels) {
            gotoModels.addEventListener('click', function() {
                var btn = document.querySelector('.tab-btn[data-tab="models"]');
                if (btn) btn.click();
            });
        }

        var loadModelSel = document.getElementById('test-load-model');
        if (loadModelSel) {
            loadModelSel.addEventListener('change', function() {
                self._model = loadModelSel.value;
                self._variant = '';
                App.preserveFocus(function() { self.render(); });
            });
        }
        var testModelSel = document.getElementById('test-model');
        if (testModelSel) {
            testModelSel.addEventListener('change', function() {
                self._model = testModelSel.value;
                App.preserveFocus(function() { self.render(); });
            });
        }

        var variantSel = document.getElementById('test-load-variant');
        if (variantSel) {
            variantSel.addEventListener('change', function() {
                self._variant = variantSel.value;
            });
        }
        var deviceSel = document.getElementById('test-load-device');
        if (deviceSel) {
            deviceSel.addEventListener('change', function() {
                self._device = deviceSel.value;
            });
        }
        var precSel = document.getElementById('test-load-precision');
        if (precSel) {
            precSel.addEventListener('change', function() {
                self._precision = precSel.value;
            });
        }

        var spawnBtn = document.getElementById('test-spawn-btn');
        if (spawnBtn) spawnBtn.addEventListener('click', function() { self.spawnWorker(); });

        var spawnPrereq = document.getElementById('test-spawn-worker');
        if (spawnPrereq) spawnPrereq.addEventListener('click', function() { self.spawnWorker(); });

        var alLoad = document.getElementById('test-al-load-btn');
        if (alLoad) alLoad.addEventListener('click', function() { self.loadAudioLab(); });

        var asLoad = document.getElementById('test-as-load-btn');
        if (asLoad) asLoad.addEventListener('click', function() { self.loadAceStep(); });

        var bind = function(inputId, key, parser) {
            var inp = document.getElementById(inputId);
            if (!inp) return;
            inp.addEventListener('input', function() { self[key] = parser(inp.value); });
        };
        bind('test-temp', '_temperature', parseFloat);
        bind('test-topp', '_topP', parseFloat);
        bind('test-maxtok', '_maxNewTokens', function(v) { return parseInt(v, 10); });

        var sendBtn = document.getElementById('test-send-btn');
        if (sendBtn) sendBtn.addEventListener('click', function() { self.send(); });

        var ta = document.getElementById('test-input');
        if (ta) {
            if (this._draftText) ta.value = this._draftText;
            ta.addEventListener('input', function() { TabTesting._draftText = ta.value; });
        }

        if (this._lastResponseText !== null) {
            var resp = document.getElementById('test-response');
            if (resp) {
                resp.textContent = this._lastResponseText;
                if (this._lastResponseColor) resp.style.color = this._lastResponseColor;
            }
        }
    },

    spawnWorker: async function() {
        var model = this._model;
        var variant = this._variant;
        var device = this._device || this._resolveDevice();
        var precision = this._precision || null;
        if (!model) { App.toast('Select a model family', 'error'); return; }
        if (!variant) {
            App.toast('Select a downloaded variant — get weights on the Models tab first', 'error');
            return;
        }

        var btn = document.getElementById('test-spawn-btn') || document.getElementById('test-spawn-worker');
        if (btn) { btn.disabled = true; btn.textContent = 'Spawning…'; }
        try {
            var body = { model: model, device: device, variant: variant };
            if (precision) body.precision = precision;
            await App.api('POST', '/api/workers/spawn', body);
            App.toast('Worker spawned for ' + App.modelDisplay(model) + ' (' + variant + ')', 'success');
            await App.refreshWorkers();
            this.render();
        } catch (e) {
            App.toast('Spawn failed: ' + e.message, 'error');
            if (btn) { btn.disabled = false; btn.textContent = 'Spawn worker'; }
        }
    },

    loadAudioLab: async function() {
        var sa = (document.getElementById('test-al-sa') || {}).value || '';
        var clap = (document.getElementById('test-al-clap') || {}).value || '';
        var vae = (document.getElementById('test-al-vae') || {}).value || 'default';
        var body = {};
        if (sa) body.sa_variant = sa;
        if (clap) body.clap_variant = clap;
        if (vae && vae !== 'default') body.vae_variant = vae;
        if (!body.sa_variant && !body.clap_variant && !body.vae_variant) {
            App.toast('Pick at least one downloaded Audio Lab model', 'error');
            return;
        }
        var btn = document.getElementById('test-al-load-btn');
        if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
        try {
            await App.api('POST', '/api/audio_lab/load', body);
            App.toast('Audio Lab loaded', 'success');
        } catch (e) {
            App.toast('Load failed: ' + e.message, 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = 'Load'; }
        }
    },

    loadAceStep: async function() {
        var model = (document.getElementById('test-as-model') || {}).value || '';
        var lm = (document.getElementById('test-as-lm') || {}).value || '';
        var vae = (document.getElementById('test-as-vae') || {}).value || 'default';
        if (!model) {
            App.toast('Select a downloaded DiT model first', 'error');
            return;
        }
        var body = { model_variant: model, vae_variant: vae };
        if (lm) body.lm_variant = lm;
        var btn = document.getElementById('test-as-load-btn');
        if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
        try {
            await App.api('POST', '/api/ace_step/load', body);
            App.toast('ACE Step loaded', 'success');
        } catch (e) {
            App.toast('Load failed: ' + e.message, 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = 'Load'; }
        }
    },

    send: async function() {
        var model = document.getElementById('test-model').value;
        var text = document.getElementById('test-input').value.trim();
        var responseEl = document.getElementById('test-response');
        var btn = document.getElementById('test-send-btn');

        if (!text) { App.toast('Enter a message first', 'error'); return; }
        if (!model) { App.toast('Select a model', 'error'); return; }
        if (model === 'moshi') {
            App.toast('Moshi requires streaming audio and cannot use batch testing', 'error');
            return;
        }
        if (!App.workerReadyFor(model)) {
            App.toast('No ready worker for ' + App.modelDisplay(model) + ' — spawn one above', 'error');
            return;
        }

        this._busy = true;
        btn.disabled = true;
        btn.textContent = 'Running…';
        responseEl.textContent = 'Thinking…';
        responseEl.style.color = 'var(--text-muted)';

        var body = {
            model: model,
            text: text,
            temperature: this._temperature,
            top_p: this._topP,
            max_new_tokens: this._maxNewTokens,
        };

        var slots = [
            { inputId: 'test-image', key: 'image' },
            { inputId: 'test-audio', key: 'audio' },
            { inputId: 'test-video', key: 'video' },
        ];
        for (var si = 0; si < slots.length; si++) {
            var inp = document.getElementById(slots[si].inputId);
            if (inp && inp.files && inp.files[0]) {
                try {
                    body[slots[si].key] = await TabTesting._fileToBase64(inp.files[0]);
                } catch (e) {
                    App.toast('Failed to read ' + slots[si].key, 'error');
                }
            }
        }

        try {
            var data = await App.api('POST', '/api/chat/' + encodeURIComponent(model), body);
            this._lastResponseColor = 'var(--text-primary)';
            this._lastResponseText = data.text || data.error || JSON.stringify(data, null, 2);
        } catch (e) {
            this._lastResponseColor = 'var(--accent-red-text)';
            this._lastResponseText = 'Error: ' + e.message;
        } finally {
            var liveResp = document.getElementById('test-response');
            if (liveResp) {
                liveResp.style.color = this._lastResponseColor;
                liveResp.textContent = this._lastResponseText;
            }
            var liveBtn = document.getElementById('test-send-btn');
            if (liveBtn) {
                liveBtn.disabled = false;
                liveBtn.textContent = 'Run';
            }
            this._busy = false;
        }
    },

    _fileToBase64: function(file) {
        return new Promise(function(resolve, reject) {
            var reader = new FileReader();
            reader.onload = function() {
                var result = reader.result;
                var idx = result.indexOf(',');
                resolve(idx >= 0 ? result.substring(idx + 1) : result);
            };
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    },
};
