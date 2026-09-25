/* ==========================================================================
   Tab: Server - GPU devices and Omni model workers
   ========================================================================== */

var TabServer = {
    selectedModel: null,
    selectedDevice: null,
    selectedVariant: null,
    selectedLora: null,
    selectedPrecision: '',  // '' = auto (bf16 on CUDA, fp32 on CPU)
    lastSpawnInteractionAt: 0,
    apiKeys: [],
    apiKeysLoaded: false,

    init: function() {
        this.render();
        this.renderDevices();
        this.loadApiKeys();
    },

    loadApiKeys: async function() {
        try {
            var data = await App.api('GET', '/api/keys');
            this.apiKeys = (data && data.keys) || [];
            this.apiKeysLoaded = true;
            this.renderKeysList();
        } catch (e) {
            // Older builds may not expose /api/keys; just leave empty.
            this.apiKeys = [];
            this.apiKeysLoaded = true;
            this.renderKeysList();
        }
    },

    syncSelectionFromDom: function() {
        var modelSel = document.getElementById('spawn-model');
        var deviceSel = document.getElementById('spawn-device');
        var variantSel = document.getElementById('spawn-variant');
        var loraSel = document.getElementById('spawn-lora');
        var precSel = document.getElementById('spawn-precision');
        if (modelSel) this.selectedModel = modelSel.value;
        if (deviceSel) this.selectedDevice = deviceSel.value;
        if (variantSel) this.selectedVariant = variantSel.value;
        if (loraSel) this.selectedLora = loraSel.value;
        if (precSel) this.selectedPrecision = precSel.value;
    },

    markSpawnInteraction: function() {
        this.lastSpawnInteractionAt = Date.now();
    },

    isSpawnFormActive: function() {
        var active = document.activeElement;
        var ids = {
            'spawn-model': true,
            'spawn-device': true,
            'spawn-variant': true,
            'spawn-lora': true,
            'spawn-precision': true,
        };
        if (active && ids[active.id]) return true;
        return Date.now() - (this.lastSpawnInteractionAt || 0) < 2500;
    },

    /** Stage 1 — GPU/CPU devices with VRAM gauges. Rendered into the
     *  #device-panel inside the pipeline so it can refresh on the 15s device
     *  poll independently of the full tab re-render. */
    renderDevices: function() {
        var container = document.getElementById('device-panel');
        if (!container) return;
        var devices = App.state.devices;
        var E = App.esc;
        if (!devices.length) {
            container.innerHTML = '<div class="empty-state" style="padding:16px">No devices detected</div>';
            return;
        }
        var html = '';
        for (var i = 0; i < devices.length; i++) {
            var d = devices[i];
            var hasVram = d.vram_total_mb > 0;
            var pct = hasVram ? Math.round((d.vram_total_mb - d.vram_free_mb) / d.vram_total_mb * 100) : 0;
            var wc = (d.workers || []).length + (d.comfy_instances || []).length;
            html += '<div style="padding:8px 0;border-bottom:1px solid var(--border)">';
            html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">';
            // Raw device id kept out of the primary line — shown via title only.
            html += '<span style="flex:1;font-size:13px" title="device ' + E(d.id) + '">' + E(d.name) + '</span>';
            html += '<span class="badge badge-gray" title="workers + ComfyUI instances on this device">' + wc + ' proc' + (wc === 1 ? '' : 's') + '</span>';
            html += '</div>';
            if (hasVram) {
                html += '<div style="display:flex;align-items:center;gap:8px">';
                html += '<div class="progress-bar" style="flex:1"><div class="fill" style="width:' + pct + '%"></div></div>';
                html += '<span style="color:var(--text-muted);font-size:11px;white-space:nowrap">'
                    + (d.vram_total_mb - d.vram_free_mb) + ' / ' + d.vram_total_mb + ' MB · ' + pct + '%</span>';
                html += '</div>';
            } else {
                html += '<div style="color:var(--text-muted);font-size:11px">No dedicated VRAM (CPU)</div>';
            }
            html += '</div>';
        }
        container.innerHTML = html;
    },

    /** A single-line summary of what's usable right now, computed from live
     *  state, with one-click actions where useful. Re-binds its action
     *  buttons on every render (idempotent). */
    renderReadiness: function() {
        var E = App.esc;
        var mkeys = Object.keys(App.state.models);
        var anyInstalled = false;
        var firstInstalledModel = null;
        var nonChatModels = { audio_lab: true, ace_step: true, moss_tts: true, moss_sfx: true };
        for (var i = 0; i < mkeys.length; i++) {
            if (App.modelInstalled(mkeys[i])) {
                anyInstalled = true;
                if (!firstInstalledModel && !nonChatModels[mkeys[i]]) firstInstalledModel = mkeys[i];
            }
        }
        var chatReady = (App.state.workers || []).some(function(w) {
            return w.status === 'ready' && !nonChatModels[w.model];
        });
        var comfyReady = App.comfyReady();

        var html = '';
        if (!anyInstalled) {
            html += '<span>⚠ Install a model to begin — open the <strong>Models</strong> tab to download weights.</span>';
            return {
                html: html,
                bind: function() {
                    /* readiness bar is informational only when nothing is installed */
                }
            };
        }

        var parts = [];
        // Chat / language line
        if (chatReady) {
            parts.push('<span class="badge badge-green">Chat: ready</span>');
        } else {
            parts.push('<span class="badge badge-orange">Chat: no worker</span>');
        }
        // Image / Video line (ComfyUI-backed)
        if (comfyReady) {
            parts.push('<span class="badge badge-green">Image/Video: ready</span>');
        } else {
            parts.push('<span class="badge badge-orange">Image/Video: start ComfyUI</span>');
        }

        html += '<span style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">' + parts.join(' ') + '</span>';
        html += '<span style="flex:1"></span>';
        // One-click actions for whatever isn't ready yet.
        var actions = [];
        if (!chatReady && firstInstalledModel) {
            actions.push('<button class="btn btn-primary btn-sm" id="readiness-spawn"'
                + ' data-model="' + E(firstInstalledModel) + '"'
                + ' title="Spawn a worker for ' + E(App.modelDisplay(firstInstalledModel)) + ' with sensible defaults">'
                + 'Start ' + E(App.modelDisplay(firstInstalledModel)) + '</button>');
        }
        if (!comfyReady) {
            actions.push('<button class="btn btn-sm" id="readiness-comfy" title="Start a ComfyUI instance with auto device + normal VRAM">Start ComfyUI</button>');
        }
        html += actions.join(' ');

        return {
            html: html,
            bind: function() {
                var sp = document.getElementById('readiness-spawn');
                if (sp) sp.addEventListener('click', function() {
                    var model = this.dataset.model;
                    this.disabled = true;
                    this.textContent = 'Starting…';
                    App.spawnWorker(model).then(function() { TabServer.render(); })
                        .catch(function() { TabServer.render(); });
                });
                var cf = document.getElementById('readiness-comfy');
                if (cf) cf.addEventListener('click', function() {
                    this.disabled = true;
                    this.textContent = 'Starting…';
                    App.startComfy().then(function() { TabServer.render(); })
                        .catch(function() { TabServer.render(); });
                });
            }
        };
    },

    /** Friendly display name for a variant_id of a given model (falls back to
     *  the raw id). Used so the workers list never surfaces raw variant ids. */
    variantDisplay: function(model, variantId) {
        if (!variantId) return 'default';
        var vs = (App.state.variants && App.state.variants[model]) || [];
        for (var i = 0; i < vs.length; i++) {
            if (vs[i].variant_id === variantId) return vs[i].display || variantId;
        }
        return variantId;
    },

    /** Friendly device name (falls back to id). */
    deviceDisplay: function(deviceId) {
        var devs = App.state.devices || [];
        for (var i = 0; i < devs.length; i++) {
            if (devs[i].id === deviceId) return devs[i].name || deviceId;
        }
        return deviceId;
    },

    render: function() {
        this.syncSelectionFromDom();

        var el = document.getElementById('tab-server');
        if (!el) return;
        var workers = App.state.workers || [];
        var instances = App.state.comfyInstances || [];
        var E = App.esc;
        var mkeys = Object.keys(App.state.models);
        var devs = App.state.devices;

        // --- resolve the spawn-form selection against live state ---
        var selectedModel = this.selectedModel && mkeys.indexOf(this.selectedModel) !== -1
            ? this.selectedModel
            : (mkeys[0] || '');
        var selectedDevice = '';
        for (var d = 0; d < devs.length; d++) {
            if (devs[d].id === this.selectedDevice) {
                selectedDevice = this.selectedDevice;
                break;
            }
        }
        if (!selectedDevice && devs.length > 0) selectedDevice = devs[0].id;
        this.selectedModel = selectedModel;
        this.selectedDevice = selectedDevice;

        var readiness = this.renderReadiness();

        // ===== Readiness summary line =====
        var html = '<div class="pipeline-readiness">' + readiness.html + '</div>';

        // ===== Pipeline flow: Devices → Workers → ComfyUI =====
        html += '<div class="pipeline-flow">';

        // ---- Stage 1: Devices ----
        html += '<div class="pipeline-stage">';
        html += '<div class="pipeline-stage-title">1 · Devices ' + App.tip('Physical GPUs and CPU the gateway can place workers and ComfyUI on. Bars show VRAM in use.') + '</div>';
        html += '<div id="device-panel"></div>';
        html += '</div>';

        html += '<div class="pipeline-arrow">→</div>';

        // ---- Stage 2: Workers (grouped by model) ----
        html += '<div class="pipeline-stage">';
        html += '<div class="pipeline-stage-title">2 · Model workers ' + App.tip('Loaded Omni model processes that serve chat, vision, speech and transcription over the OpenAI-compatible API.') + '</div>';
        html += this.renderWorkersStage(workers);
        html += '</div>';

        html += '<div class="pipeline-arrow">→</div>';

        // ---- Stage 3: ComfyUI ----
        html += '<div class="pipeline-stage">';
        html += '<div class="pipeline-stage-title">3 · ComfyUI ' + App.tip('Image/video diffusion backend. Click a ready port to open its native web UI in a new tab.') + '</div>';
        html += this.renderComfyStage(instances);
        html += '</div>';

        html += '</div>'; // .pipeline-flow

        // ===== Spawn worker form =====
        html += '<div class="card"><h2>Spawn a model worker</h2>';
        html += this.renderSpawnForm(selectedModel, selectedDevice);
        html += '</div>';

        // ===== API access card (OpenAI-compat surface + keys) =====
        html += this.renderApiAccessCard();

        el.innerHTML = html;

        // ---- bind everything ----
        readiness.bind();
        this.bindWorkersStage();
        this.bindComfyStage();
        this.bindSpawnForm();
        this.bindApiAccessHandlers();
        this.renderKeysList();
        this.renderDevices();
    },

    /** Stage 2 body — workers grouped by model with friendly names, status
     *  dots, and inline spawn/kill. Raw worker_id/port live in title attrs. */
    renderWorkersStage: function(workers) {
        var E = App.esc;
        if (!workers.length) {
            return '<div class="empty-state" style="padding:16px">No workers running.<br>Spawn one below.</div>';
        }

        // Group workers by model so the friendly model name leads.
        var byModel = {};
        var order = [];
        for (var i = 0; i < workers.length; i++) {
            var w = workers[i];
            if (!byModel[w.model]) { byModel[w.model] = []; order.push(w.model); }
            byModel[w.model].push(w);
        }

        var html = '';
        for (var m = 0; m < order.length; m++) {
            var model = order[m];
            var group = byModel[model];
            html += '<div style="margin-bottom:10px">';
            html += '<div style="font-weight:600;font-size:13px;margin-bottom:4px">' + E(App.modelDisplay(model)) + '</div>';
            for (var k = 0; k < group.length; k++) {
                var wk = group[k];
                var statusCls = 'status-' + wk.status;
                // Title carries the raw id + port for power users; primary row
                // stays human-friendly.
                var detail = 'worker ' + wk.worker_id + ' · port ' + wk.port
                    + ' · ' + this.deviceDisplay(wk.device);
                var meta = [];
                meta.push(this.variantDisplay(model, wk.variant));
                if (wk.precision) meta.push(wk.precision); else meta.push('auto');
                if (wk.lora) meta.push('LoRA: ' + wk.lora);
                var vram = (wk.vram_used_mb || 0) + '/' + (wk.vram_total_mb || 0) + ' MB';

                html += '<div style="display:flex;align-items:center;gap:8px;padding:4px 0" title="' + E(detail) + '">';
                html += '<span class="status-dot ' + statusCls + '"></span>';
                html += '<span style="flex:1;min-width:0;font-size:12px">';
                html += '<span style="text-transform:capitalize">' + E(wk.status) + '</span>';
                html += ' <span style="color:var(--text-muted)">· ' + E(meta.join(' · ')) + '</span>';
                html += '</span>';
                html += '<span style="color:var(--text-muted);font-size:11px;white-space:nowrap">' + E(vram) + '</span>';
                html += '<div class="btn-group">';
                html += '<button class="btn btn-sm" data-wid="' + E(wk.worker_id) + '" data-act="worker-logs">Logs</button>';
                html += '<button class="btn btn-danger btn-sm" data-wid="' + E(wk.worker_id) + '" data-act="worker-kill">Kill</button>';
                html += '</div>';
                html += '</div>';
            }
            html += '</div>';
        }
        html += '<button class="btn btn-danger btn-sm" data-act="worker-kill-all" style="margin-top:4px">Kill all workers</button>';
        return html;
    },

    bindWorkersStage: function() {
        var stage = document.getElementById('tab-server');
        if (!stage) return;
        var logBtns = stage.querySelectorAll('[data-act="worker-logs"]');
        for (var i = 0; i < logBtns.length; i++) {
            logBtns[i].addEventListener('click', function() { TabServer.showLogs(this.dataset.wid); });
        }
        var killBtns = stage.querySelectorAll('[data-act="worker-kill"]');
        for (var j = 0; j < killBtns.length; j++) {
            killBtns[j].addEventListener('click', function() { TabServer.killWorker(this.dataset.wid); });
        }
        var killAll = stage.querySelector('[data-act="worker-kill-all"]');
        if (killAll) killAll.addEventListener('click', function() { TabServer.killAll(); });
    },

    /** Stage 3 body — ComfyUI instances with status dots and the clickable
     *  port link (kept, with an App.tip). Detailed config lives in title. */
    renderComfyStage: function(instances) {
        var E = App.esc;
        if (!instances.length) {
            return '<div class="empty-state" style="padding:16px">No ComfyUI running.<br>Use the readiness bar above or the ComfyUI tab to start one.</div>';
        }
        var comfyHost = window.location.hostname || 'localhost';
        var html = '';
        for (var k = 0; k < instances.length; k++) {
            var inst = instances[k];
            var statusCls = 'status-' + inst.status;
            // FRO-3: coerce the server-supplied port to a number before
            // interpolating it into the href/text.
            var instPort = Number(inst.port);
            var detail = 'instance ' + inst.instance_id + ' · ' + this.deviceDisplay(inst.device)
                + ' · VRAM mode ' + inst.vram_mode + (inst.precision ? ' · ' + inst.precision : ' · auto');
            var vram = (inst.vram_used_mb || 0) + '/' + (inst.vram_total_mb || 0) + ' MB';

            html += '<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)" title="' + E(detail) + '">';
            html += '<span class="status-dot ' + statusCls + '"></span>';
            html += '<span style="flex:1;min-width:0;font-size:12px">';
            html += '<span style="text-transform:capitalize">' + E(inst.status) + '</span>';
            html += ' <span style="color:var(--text-muted)">· ' + E(this.deviceDisplay(inst.device)) + ' · ' + E(inst.vram_mode) + ' VRAM</span>';
            html += '</span>';
            if (inst.status === 'ready') {
                html += '<a href="http://' + E(comfyHost) + ':' + instPort + '" target="_blank" rel="noopener" style="color:var(--accent-blue);font-size:12px">Open :' + instPort + ' ↗</a>';
            } else {
                html += '<span style="color:var(--text-muted);font-size:12px">:' + instPort + '</span>';
            }
            html += '<div class="btn-group">';
            html += '<button class="btn btn-sm" data-iid="' + E(inst.instance_id) + '" data-act="comfy-logs">Logs</button>';
            html += '<button class="btn btn-danger btn-sm" data-iid="' + E(inst.instance_id) + '" data-act="comfy-stop">Stop</button>';
            html += '</div>';
            html += '</div>';
        }
        html += '<button class="btn btn-danger btn-sm" data-act="comfy-stop-all" style="margin-top:8px">Stop all ComfyUI</button>';
        return html;
    },

    bindComfyStage: function() {
        var stage = document.getElementById('tab-server');
        if (!stage) return;
        var logBtns = stage.querySelectorAll('[data-act="comfy-logs"]');
        for (var i = 0; i < logBtns.length; i++) {
            logBtns[i].addEventListener('click', function() { TabServer.showComfyLogs(this.dataset.iid); });
        }
        var stopBtns = stage.querySelectorAll('[data-act="comfy-stop"]');
        for (var j = 0; j < stopBtns.length; j++) {
            stopBtns[j].addEventListener('click', function() { TabServer.stopComfy(this.dataset.iid); });
        }
        var stopAll = stage.querySelector('[data-act="comfy-stop-all"]');
        if (stopAll) stopAll.addEventListener('click', function() { TabServer.stopAllComfy(); });
    },

    /** The spawn form, built entirely from App.field selects with friendly
     *  display names. Disables submit (with a matching hint) when the chosen
     *  model isn't installed and offers a banner to jump to Setup. */
    renderSpawnForm: function(selectedModel, selectedDevice) {
        var E = App.esc;
        var mkeys = Object.keys(App.state.models);
        var devs = App.state.devices || [];

        // Model select — friendly display names, raw id as the value.
        var modelOpts = mkeys.map(function(id) {
            return { value: id, label: App.modelDisplay(id) };
        });
        // Device select — friendly names, raw id as the value.
        var deviceOpts = devs.map(function(dv) {
            return { value: dv.id, label: dv.name };
        });

        var installed = App.modelInstalled(selectedModel);
        var status = (App.state.setupStatus && App.state.setupStatus[selectedModel]) || {};
        var runtimeMissing = status.runtime_missing || [];
        var aceNativeInstalled = selectedModel === 'ace_step' && (status.native_models_installed || 0) > 0;
        var aceNeedsCore = aceNativeInstalled && status.core_ready === false;
        var aceNeedsLm = selectedModel === 'ace_step'
            && status.lm_required === true
            && (status.lms_installed || 0) <= 0;
        var spawnDisabled = mkeys.length && (!installed || aceNeedsCore || aceNeedsLm);
        var spawnDisabledTitle = !installed
            ? 'Install this model first'
            : aceNeedsCore
            ? 'Install the ACE-Step shared core first'
            : aceNeedsLm
            ? 'Install an ACE-Step LM first'
            : '';

        var html = '';
        // Prereq banner + disabled submit when the selected model has no weights.
        if (mkeys.length && !installed) {
            var missingText = App.modelDisplay(selectedModel) + ' has no weights installed yet. Download a variant on the Models tab before spawning a worker.';
            var actionLabel = 'Open Models';
            var actionId = 'spawn-goto-models';
            if (status.runtime_ready === false) {
                missingText = App.modelDisplay(selectedModel) + ' runtime dependencies are missing: '
                    + runtimeMissing.join(', ')
                    + '. Run the app setup/venv repair before spawning this worker.';
            } else if (selectedModel === 'ace_step') {
                missingText = 'ACE-Step has no DiT model weights installed yet. Download a base model on the Models or ACE Step tab before spawning a worker.';
            } else if (selectedModel === 'audio_lab') {
                missingText = 'Audio Lab has no Stable Audio weights installed yet. Download a model on the Models or Audio Lab tab before spawning a worker.';
            }
            html += App.prereqBanner({
                level: 'warn',
                text: missingText,
                actionId: actionId,
                actionLabel: actionLabel,
            });
        }
        if (selectedModel === 'ace_step' && installed) {
            var aceHint = aceNeedsCore
                ? 'ACE-Step has DiT weights installed, but its shared v1.5 core is missing: '
                    + (status.core_missing || []).join(', ')
                    + '. New ACE model downloads include it automatically; for this existing partial install, install the shared core once on the ACE Step tab.'
                : aceNeedsLm
                ? 'ACE-Step has a DiT model installed, but no text encoder (LM). Install or load an LM on the ACE Step tab before generating.'
                : 'ACE-Step generation loads the model stack from the ACE Step tab. This Server button only starts the worker process.';
            html += App.prereqBanner({
                level: 'info',
                text: aceHint,
                actionId: 'spawn-goto-ace-step',
                actionLabel: 'Open ACE Step',
            });
        }

        html += '<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end">';

        html += '<div style="flex:1;min-width:180px">' + App.field({
            type: 'select', id: 'spawn-model', label: 'Model',
            options: modelOpts, value: selectedModel,
            hint: 'Which Omni model to load.',
        }) + '</div>';

        html += '<div style="flex:1;min-width:160px">' + App.field({
            type: 'select', id: 'spawn-device', label: 'Device',
            options: deviceOpts, value: selectedDevice,
            tip: 'GPU or CPU to place this worker on. Pick a device with free VRAM.',
            hint: 'Where the model runs.',
        }) + '</div>';

        // Variant + LoRA + precision get populated/refined by the update* fns
        // right after render, but we seed sensible single-option selects so the
        // controls are present and labeled immediately.
        html += '<div style="flex:1;min-width:160px">' + App.field({
            type: 'select', id: 'spawn-variant', label: 'Variant',
            options: [{ value: '', label: 'default (auto)' }], value: '',
            tip: 'A specific build/size of the model (e.g. a smaller quantized weight set). Only installed variants are listed.',
            hint: 'Defaults to the recommended build.',
        }) + '</div>';

        html += '<div style="flex:1;min-width:160px">' + App.field({
            type: 'select', id: 'spawn-lora', label: 'LoRA',
            options: [{ value: '', label: 'No LoRA' }], value: '',
            tip: 'A small fine-tuning adapter layered on top of the base model. Only shown for compatible models.',
            hint: 'Optional fine-tune adapter.',
        }) + '</div>';

        html += '<div style="flex:1;min-width:160px">' + App.field({
            type: 'select', id: 'spawn-precision', label: 'Precision',
            options: [{ value: '', label: 'auto (bf16 on CUDA)' }], value: '',
            tip: 'Numeric format the weights run in. Lower precision (fp16/bf16) uses less VRAM; fp32 is most accurate.',
            hint: 'Weight dtype / VRAM trade-off.',
        }) + '</div>';

        html += '<div style="flex:0 0 auto">';
        html += '<button id="spawn-btn" class="btn btn-primary btn-sm"'
            + (spawnDisabled ? ' disabled title="' + E(spawnDisabledTitle) + '"' : '')
            + '>Spawn worker</button>';
        html += '</div>';

        html += '</div>'; // form row
        return html;
    },

    bindSpawnForm: function() {
        var modelSel = document.getElementById('spawn-model');
        var deviceSel = document.getElementById('spawn-device');
        var variantSel = document.getElementById('spawn-variant');
        var loraSel = document.getElementById('spawn-lora');
        var precSel = document.getElementById('spawn-precision');
        var spawnBtn = document.getElementById('spawn-btn');

        [modelSel, deviceSel, variantSel, loraSel, precSel].forEach(function(ctrl) {
            if (!ctrl) return;
            ['focus', 'pointerdown', 'keydown', 'change'].forEach(function(evt) {
                ctrl.addEventListener(evt, function() { TabServer.markSpawnInteraction(); });
            });
        });

        if (modelSel) {
            modelSel.addEventListener('change', function() {
                TabServer.selectedModel = modelSel.value;
                TabServer.updateVariantOptions();
                TabServer.updateLoraOptions();
                TabServer.updatePrecisionOptions();
                // Re-render so the prereq banner + submit-disabled state track
                // the newly-selected model.
                App.preserveFocus(function() { TabServer.render(); });
            });
        }
        if (deviceSel) {
            deviceSel.addEventListener('change', function() {
                TabServer.selectedDevice = deviceSel.value;
            });
        }
        if (variantSel) {
            variantSel.addEventListener('change', function() {
                TabServer.selectedVariant = variantSel.value;
            });
        }
        if (loraSel) {
            loraSel.addEventListener('change', function() {
                TabServer.selectedLora = loraSel.value;
            });
        }
        if (precSel) {
            precSel.addEventListener('change', function() {
                TabServer.selectedPrecision = precSel.value;
            });
        }
        if (spawnBtn) {
            spawnBtn.addEventListener('click', function() { TabServer.spawnWorker(); });
        }
        var gotoModels = document.getElementById('spawn-goto-models');
        if (gotoModels) {
            gotoModels.addEventListener('click', function() {
                var btn = document.querySelector('.tab-btn[data-tab="models"]');
                if (btn) btn.click();
            });
        }
        var gotoAceStep = document.getElementById('spawn-goto-ace-step');
        if (gotoAceStep) {
            gotoAceStep.addEventListener('click', function() {
                var btn = document.querySelector('.tab-btn[data-tab="ace-step"]');
                if (btn) btn.click();
            });
        }

        // Populate variant + LoRA + precision options for the selected model.
        this.updateVariantOptions();
        this.updateLoraOptions();
        this.updatePrecisionOptions();
    },

    updateVariantOptions: function() {
        var sel = document.getElementById('spawn-variant');
        if (!sel) return;
        var model = this.selectedModel;
        var variants = (App.state.variants && App.state.variants[model]) || [];
        var variantStatus = (App.state.setupStatus[model] && App.state.setupStatus[model].variants) || [];
        var E = App.esc;

        // Build a set of installed variant_ids
        var installed = {};
        for (var i = 0; i < variantStatus.length; i++) {
            if (variantStatus[i].installed) installed[variantStatus[i].variant_id] = true;
        }

        var html = '';
        var firstInstalled = null;
        var hasInstalled = false;
        for (var j = 0; j < variants.length; j++) {
            var v = variants[j];
            if (!installed[v.variant_id]) continue;
            hasInstalled = true;
            if (!firstInstalled) firstInstalled = v.variant_id;
        }

        if (!hasInstalled) {
            html = '<option value="">— download a variant on Models tab —</option>';
            this.selectedVariant = null;
        } else {
            var pick = this.selectedVariant;
            if (!pick || !installed[pick]) pick = firstInstalled;
            this.selectedVariant = pick;
            for (var k = 0; k < variants.length; k++) {
                var vv = variants[k];
                if (!installed[vv.variant_id]) continue;
                var selected = (vv.variant_id === pick) ? ' selected' : '';
                html += '<option value="' + E(vv.variant_id) + '"' + selected + '>' + E(vv.display) + '</option>';
            }
        }
        sel.innerHTML = html;
    },

    /** Re-render the precision dropdown for the currently-selected model.
     *  Moshi's loader (`_load_moshi`) doesn't honor a runtime precision
     *  override — its dtype is baked into the variant (moshiko-bf16 vs
     *  moshiko-int8) — so we collapse the dropdown to a single disabled
     *  hint when Moshi is picked. Re-enables full options for any other
     *  model. */
    updatePrecisionOptions: function() {
        var sel = document.getElementById('spawn-precision');
        if (!sel) return;
        var moshiSelected = this.selectedModel === 'moshi';
        var E = App.esc;
        var html;
        if (moshiSelected) {
            html = '<option value="" selected>baked into variant</option>';
            sel.title = 'Moshi precision is set by the variant (moshiko-bf16, moshiko-int8, etc.) — runtime override has no effect';
            sel.disabled = true;
        } else {
            var opts = [
                { v: '',     label: 'auto (bf16 on CUDA)' },
                { v: 'fp16', label: 'fp16 (half)' },
                { v: 'bf16', label: 'bf16' },
                { v: 'fp32', label: 'fp32 (full)' },
            ];
            html = '';
            for (var i = 0; i < opts.length; i++) {
                var o = opts[i];
                var s = (o.v === (this.selectedPrecision || '')) ? ' selected' : '';
                html += '<option value="' + E(o.v) + '"' + s + '>' + E(o.label) + '</option>';
            }
            sel.title = 'Model precision / dtype';
            sel.disabled = false;
        }
        sel.innerHTML = html;
    },

    updateLoraOptions: function() {
        var sel = document.getElementById('spawn-lora');
        if (!sel) return;
        var model = this.selectedModel;
        var isCompatible = App.state.loraCompatible.indexOf(model) !== -1;
        var loras = App.state.loras || [];
        var E = App.esc;

        if (!isCompatible) {
            sel.innerHTML = '<option value="">-- not supported --</option>';
            sel.disabled = true;
            return;
        }
        sel.disabled = false;

        var html = '<option value="">No LoRA</option>';
        for (var i = 0; i < loras.length; i++) {
            if (!loras[i].has_adapter) continue;
            var selected = (loras[i].name === this.selectedLora) ? ' selected' : '';
            html += '<option value="' + E(loras[i].name) + '"' + selected + '>' + E(loras[i].name) + '</option>';
        }
        sel.innerHTML = html;
    },

    spawnWorker: async function() {
        var model = document.getElementById('spawn-model').value;
        var device = document.getElementById('spawn-device').value;
        var variantSel = document.getElementById('spawn-variant');
        var loraSel = document.getElementById('spawn-lora');
        var precSel = document.getElementById('spawn-precision');
        var variant = variantSel ? (variantSel.value || null) : null;
        var lora = (loraSel && !loraSel.disabled) ? (loraSel.value || null) : null;
        // Strip precision for Moshi — the loader ignores it and including
        // it in the body would only mislead anyone reading the request log.
        var precision = (precSel && !precSel.disabled) ? (precSel.value || null) : null;

        this.selectedModel = model;
        this.selectedDevice = device;
        this.selectedVariant = variant;
        this.selectedLora = lora;
        this.selectedPrecision = precision || '';

        if (model === 'ace_step') {
            var status = (App.state.setupStatus && App.state.setupStatus.ace_step) || {};
            var aceNativeInstalled = (status.native_models_installed || 0) > 0;
            var aceNeedsCore = aceNativeInstalled && status.core_ready === false;
            var aceNeedsLm = status.lm_required === true && (status.lms_installed || 0) <= 0;
            if (aceNeedsCore) {
                App.toast('Install the ACE-Step shared core on the ACE Step tab before spawning this worker.', 'error');
                return;
            }
            if (aceNeedsLm) {
                App.toast('Install an ACE-Step LM on the ACE Step tab before spawning this worker.', 'error');
                return;
            }
        }

        var btn = document.getElementById('spawn-btn');
        btn.disabled = true;
        btn.textContent = 'Spawning...';
        var label = model
            + (variant ? ' (' + variant + ')' : '')
            + (precision ? ' [' + precision + ']' : '')
            + (lora ? ' +LoRA:' + lora : '');
        App.toast('Spawning ' + label + ' on ' + device + '...', 'info');
        try {
            var body = { model: model, device: device };
            if (variant) body.variant = variant;
            if (lora) body.lora = lora;
            if (precision) body.precision = precision;
            await App.api('POST', '/api/workers/spawn', body);
            App.toast('Worker spawned', 'success');
        } catch(e) {
            App.toast('Spawn failed: ' + e.message, 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = 'Spawn worker';
        }
    },

    killWorker: async function(id) {
        try {
            await App.api('DELETE', '/api/workers/' + encodeURIComponent(id));
            await App.refreshWorkers();
            this.render();
            App.toast('Worker killed', 'success');
        } catch(e) {
            App.toast('Kill failed: ' + e.message, 'error');
        }
    },

    showLogs: async function(id) {
        try {
            var data = await App.api('GET', '/api/workers/' + encodeURIComponent(id) + '/logs?lines=200');
            App.showTextModal('Worker logs: ' + id, (data.lines || []).join('\n'));
        } catch(e) {
            App.toast('Failed to load logs: ' + e.message, 'error');
        }
    },

    killAll: async function() {
        if (!confirm('Kill all workers? Running inference jobs will be lost.')) return;
        try {
            var data = await App.api('POST', '/api/workers/kill-all');
            await App.refreshWorkers();
            this.render();
            App.toast('Killed ' + data.killed + ' workers', 'success');
        } catch(e) {
            App.toast('Kill all failed: ' + e.message, 'error');
        }
    },

    // ----------------------------------------------------------------------
    // ComfyUI controls (Stage 3) — same endpoints the ComfyUI tab uses, kept
    // here so the pipeline view can stop/inspect instances inline.
    // ----------------------------------------------------------------------
    showComfyLogs: async function(id) {
        try {
            var data = await App.api('GET', '/api/comfy/' + encodeURIComponent(id) + '/logs?lines=200');
            App.showTextModal('ComfyUI logs: ' + id, (data.lines || []).join('\n'));
        } catch(e) {
            App.toast('Failed to load logs: ' + e.message, 'error');
        }
    },

    stopComfy: async function(id) {
        try {
            await App.api('POST', '/api/comfy/' + encodeURIComponent(id) + '/stop');
            await App.refreshWorkers();
            this.render();
            App.toast('ComfyUI instance stopped', 'success');
        } catch(e) {
            App.toast('Stop failed: ' + e.message, 'error');
        }
    },

    stopAllComfy: async function() {
        if (!confirm('Stop all ComfyUI instances? Running workflows will be interrupted.')) return;
        try {
            var data = await App.api('POST', '/api/comfy/stop-all');
            await App.refreshWorkers();
            this.render();
            App.toast('Stopped ' + data.stopped + ' instances', 'success');
        } catch(e) {
            App.toast('Stop all failed: ' + e.message, 'error');
        }
    },

    // ----------------------------------------------------------------------
    // API access card — OpenAI-compat surface + bearer keys
    // ----------------------------------------------------------------------
    renderApiAccessCard: function() {
        var E = App.esc;
        var info = App.state.sessionInfo || {};
        var baseUrl = window.location.origin;
        var v1Url = baseUrl + '/v1';
        var aliases = info.openai_aliases || {};
        var authMode = info.auth_mode || 'loopback-token';

        // Group aliases by native model so the display is compact:
        //   gpt-3.5-turbo, gpt-4o-mini → qwen_omni_3b
        var byNative = {};
        Object.keys(aliases).forEach(function(k) {
            var native = aliases[k];
            (byNative[native] = byNative[native] || []).push(k);
        });
        var aliasRows = '';
        Object.keys(byNative).sort().forEach(function(native) {
            aliasRows += '<div class="api-alias-row">'
                + '<code>' + byNative[native].map(E).join(', ') + '</code>'
                + ' <span class="api-arrow">→</span> '
                + '<code class="api-native">' + E(native) + '</code>'
                + '</div>';
        });
        if (!aliasRows) aliasRows = '<div class="empty-state" style="padding:8px;">No aliases configured.</div>';

        var credentialHint = authMode === 'bearer'
            ? '<your-created-key-or-session-token>'
            : '<loopback-session-token>';
        var snippet = ''
            + 'from openai import OpenAI\n\n'
            + 'client = OpenAI(\n'
            + '    api_key="' + credentialHint + '",\n'
            + '    base_url="' + v1Url + '",\n'
            + ')\n\n'
            + 'resp = client.chat.completions.create(\n'
            + '    model="gpt-4o",  # alias of qwen_omni_7b\n'
            + '    messages=[{"role": "user", "content": "Hello!"}],\n'
            + ')\n'
            + 'print(resp.choices[0].message.content)\n';

        var authBanner = '';
        if (authMode === 'loopback-token') {
            authBanner = '<div class="api-warn">'
                + 'Auth mode: <strong>loopback-token</strong>. Use the current session token for API calls. '
                + 'Created API keys do not authenticate until the gateway starts with '
                + '<code>OMNI_AUTH_MODE=bearer</code>.'
                + '</div>';
        } else {
            authBanner = '<div class="api-info">Auth mode: <strong>' + E(authMode) + '</strong>. Registered keys and the local session token can authenticate.</div>';
        }

        return ''
            + '<div class="card api-card">'
            +   '<h2>API access</h2>'
            +   '<div class="api-row">'
            +     '<div class="api-lbl">OpenAI-compatible base URL</div>'
            +     '<div class="api-url">'
            +       '<code id="api-base-url">' + E(v1Url) + '</code>'
            // FRO-1: pass v1Url via data-* + addEventListener instead of building
            // executable JS from a dynamic value (App.esc is not JS-string safe).
            +       '<button class="btn btn-sm" id="api-base-url-copy" data-copy="' + E(v1Url) + '">📋 Copy</button>'
            +     '</div>'
            +   '</div>'

            +   '<div class="api-row">'
            +     '<div class="api-lbl">Endpoints</div>'
            +     '<table class="api-endpoints">'
            +       '<tr><td><code>POST</code></td><td><code>/v1/chat/completions</code></td><td>Multi-turn chat (text + vision)</td></tr>'
            +       '<tr><td><code>POST</code></td><td><code>/v1/completions</code></td><td>Legacy text completion</td></tr>'
            +       '<tr><td><code>POST</code></td><td><code>/v1/audio/speech</code></td><td>TTS — text → audio</td></tr>'
            +       '<tr><td><code>POST</code></td><td><code>/v1/audio/transcriptions</code></td><td>Whisper-compat STT</td></tr>'
            +       '<tr><td><code>GET</code></td><td><code>/v1/models</code></td><td>List models + aliases</td></tr>'
            +     '</table>'
            +   '</div>'

            +   '<div class="api-row">'
            +     '<div class="api-lbl">Model aliases <span class="api-hint">(set via <code>OMNI_OPENAI_ALIASES</code> env)</span></div>'
            +     '<div class="api-aliases">' + aliasRows + '</div>'
            +   '</div>'

            +   '<details class="api-row api-snippet">'
            +     '<summary>Python snippet</summary>'
            +     '<pre class="api-code">' + E(snippet) + '</pre>'
            +     '<button class="btn btn-sm" onclick="TabServer.copyText(this.previousElementSibling.textContent, this)">📋 Copy snippet</button>'
            +   '</details>'

            +   authBanner

            +   '<div class="api-row api-keys-row">'
            +     '<div class="api-lbl">'
            +       '<span>API keys</span>'
            +       '<button class="btn btn-sm btn-primary" id="api-key-new">+ New key</button>'
            +     '</div>'
            +     '<div id="api-keys-list" class="api-keys-list"></div>'
            +   '</div>'
            + '</div>';
    },

    bindApiAccessHandlers: function() {
        var btn = document.getElementById('api-key-new');
        if (btn) btn.addEventListener('click', function() { TabServer.createKeyFlow(); });
        // FRO-1: attach copy handler so the dynamic base URL is read from data-* at
        // click time rather than interpolated into an inline JS-string handler.
        var copyBtn = document.getElementById('api-base-url-copy');
        if (copyBtn) copyBtn.addEventListener('click', function() {
            TabServer.copyText(this.dataset.copy, this);
        });
    },

    renderKeysList: function() {
        var el = document.getElementById('api-keys-list');
        if (!el) return;
        var E = App.esc;
        if (!this.apiKeysLoaded) {
            el.innerHTML = '<div class="empty-state" style="padding:8px">Loading…</div>';
            return;
        }
        if (!this.apiKeys.length) {
            el.innerHTML = '<div class="empty-state" style="padding:8px">No keys yet. Create one for external apps that hit <code>/v1/*</code>.</div>';
            return;
        }
        var html = '';
        for (var i = 0; i < this.apiKeys.length; i++) {
            var k = this.apiKeys[i];
            var lastUsed = k.last_used_at
                ? new Date(k.last_used_at * 1000).toLocaleString()
                : '<span class="api-muted">never</span>';
            var scopes = (k.scopes || []).map(function(s) {
                return '<span class="badge badge-blue">' + E(s) + '</span>';
            }).join(' ');
            html += '<div class="api-key-row">'
                + '<div class="api-key-meta">'
                +   '<code>' + E(k.id) + '</code>'
                +   ' · ' + (E(k.label || '(no label)'))
                +   ' · ' + scopes
                + '</div>'
                + '<div class="api-key-stats">last used: ' + lastUsed + '</div>'
                + '<button class="btn btn-danger btn-sm" data-kid="' + E(k.id)
                + '" onclick="TabServer.revokeKey(this.dataset.kid)">Revoke</button>'
                + '</div>';
        }
        el.innerHTML = html;
    },

    createKeyFlow: async function() {
        var label = prompt('Label for this key (e.g. "my-python-app"):');
        if (label === null) return;
        label = (label || '').trim() || 'unlabeled';
        var scopesRaw = prompt('Scopes (comma-separated): admin, read, generate, manage', 'admin');
        if (scopesRaw === null) return;
        var scopes = scopesRaw.split(',').map(function(s) { return s.trim(); }).filter(Boolean);
        if (!scopes.length) scopes = ['admin'];
        try {
            var data = await App.api('POST', '/api/keys', { label: label, scopes: scopes });
            var presentation = (data && (data.presentation || data.secret)) || '';
            if (!presentation) throw new Error('Server did not return a key');
            App.showCopyModal(
                'New API key',
                presentation,
                'Copy this now — it will not be shown again. Use it as a Bearer token: ' +
                '`Authorization: Bearer <key>` against ' + window.location.origin + '/v1.'
            );
            await this.loadApiKeys();
        } catch (e) {
            App.toast('Create key failed: ' + e.message, 'error');
        }
    },

    revokeKey: async function(id) {
        if (!confirm('Revoke key ' + id + '? External clients using it will start getting 401.')) return;
        try {
            await App.api('DELETE', '/api/keys/' + encodeURIComponent(id));
            App.toast('Key revoked', 'success');
            await this.loadApiKeys();
        } catch (e) {
            App.toast('Revoke failed: ' + e.message, 'error');
        }
    },

    copyText: function(text, btn) {
        try {
            navigator.clipboard.writeText(text);
            if (btn) {
                var orig = btn.textContent;
                btn.textContent = 'Copied ✓';
                setTimeout(function() { btn.textContent = orig; }, 1200);
            }
        } catch (e) {
            App.toast('Clipboard unavailable', 'error');
        }
    },
};
