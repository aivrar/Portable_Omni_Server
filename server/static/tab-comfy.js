/* ==========================================================================
   Tab: ComfyUI - Instance management
   ========================================================================== */

var TabComfy = {
    selectedDevice: null,
    selectedVram: 'normal',
    selectedPrecision: '',  // '' = auto, 'fp16', 'bf16', 'fp32'
    useGpuPool: true,
    selectedPoolDevices: [],  // empty means every detected CUDA GPU
    selectedPreviewMethod: 'auto',
    disablePinnedMemory: false,
    startupGroups: [],
    startupOptions: {},
    startupFlagsOpen: false,
    startupSectionsOpen: {},
    selectedExecutionInstance: null,
    executionJobs: [],
    executionQueue: { queue_running: [], queue_pending: [] },
    executionLoading: false,
    executionError: '',
    executionLastLoaded: 0,
    installationStatus: null,
    installationLoading: false,
    installationError: '',
    startPending: false,

    init: function() {
        this.render();
        this.loadStartOptions();
        this.loadInstallationStatus(false);
    },

    refresh: async function() {
        await Promise.all([
            App.refreshWorkers(),
            this.loadInstallationStatus(false),
        ]);
        this.render();
    },

    openWorkflows: function() {
        App.activateTab('workflows');
    },

    openComfyModels: function() {
        if (typeof TabModels !== 'undefined' && TabModels.setViewMode) {
            TabModels.setViewMode('comfy', false);
        }
        App.activateTab('models');
    },

    loadInstallationStatus: async function(checkRemote) {
        if (this.installationLoading) return;
        this.installationLoading = true;
        this.installationError = '';
        this.renderInstallationCard();
        try {
            var suffix = checkRemote ? '?check_remote=true' : '';
            this.installationStatus = await App.api('GET', '/api/comfy/installation/status' + suffix);
        } catch (e) {
            this.installationError = e.message;
        } finally {
            this.installationLoading = false;
            this.renderInstallationCard();
        }
    },

    renderInstallationCard: function() {
        var panel = document.getElementById('comfy-installation-panel');
        if (!panel) return;
        var E = App.esc;
        var status = this.installationStatus;
        if (!status && this.installationLoading) {
            panel.innerHTML = '<div class="empty-state">Checking the local ComfyUI installation...</div>';
            return;
        }
        if (!status) {
            panel.innerHTML = '<div class="empty-state">Installation status unavailable' + (this.installationError ? ': ' + E(this.installationError) : '') + '</div>';
            return;
        }
        var current = String(status.current_commit || '');
        var remote = String(status.remote_commit || '');
        var packages = Array.isArray(status.template_packages) ? status.template_packages : [];
        var html = '<div class="comfy-install-grid">';
        html += '<div><span class="field-label">Installation</span><strong>' + (status.installed ? 'Ready' : 'Missing') + '</strong><small>' + E(status.path || '') + '</small></div>';
        html += '<div><span class="field-label">Current version</span><strong><code>' + E(current ? current.slice(0, 10) : 'unknown') + '</code></strong><small>' + packages.length + ' workflow template package' + (packages.length === 1 ? '' : 's') + '</small></div>';
        html += '<div><span class="field-label">Updates</span>';
        if (status.update_status === 'available') {
            html += '<strong><span class="badge badge-orange">Update available</span></strong><small>Target <code>' + E(remote.slice(0, 10)) + '</code></small>';
        } else if (status.update_status === 'current') {
            html += '<strong><span class="badge badge-green">Current</span></strong><small>Remote repository checked</small>';
        } else if (status.update_status === 'unknown') {
            html += '<strong><span class="badge badge-orange">Check failed</span></strong><small>' + E((status.errors || [])[0] || 'Remote version could not be resolved') + '</small>';
        } else {
            html += '<strong><span class="badge">Not checked</span></strong><small>Local status only; this does not mean up to date.</small>';
        }
        html += '</div><div class="comfy-install-action"><button class="btn btn-sm" type="button" onclick="TabComfy.loadInstallationStatus(true)"' + (this.installationLoading ? ' disabled' : '') + '>' + (this.installationLoading ? 'Checking...' : 'Check for updates') + '</button></div></div>';
        if (this.installationError) html += '<div class="inline-alert warning" role="alert">' + E(this.installationError) + '</div>';
        panel.innerHTML = html;
    },

    loadStartOptions: async function() {
        try {
            var data = await App.api('GET', '/api/comfy/start-options');
            this.startupGroups = Array.isArray(data.groups) ? data.groups : [];
            this.render();
        } catch(e) {
            this.startupGroups = [];
        }
    },

    syncSelectionFromDom: function() {
        var deviceSel = document.getElementById('comfy-device');
        var vramSel = document.getElementById('comfy-vram');
        var precSel = document.getElementById('comfy-precision');
        var previewSel = document.getElementById('comfy-preview-method');
        var pinned = document.getElementById('comfy-disable-pinned-memory');
        var gpuPool = document.getElementById('comfy-use-gpu-pool');
        var advanced = document.getElementById('comfy-startup-flags');
        if (deviceSel) this.selectedDevice = deviceSel.value;
        if (vramSel) this.selectedVram = vramSel.value;
        if (precSel) this.selectedPrecision = precSel.value;
        if (previewSel) this.selectedPreviewMethod = previewSel.value;
        if (pinned) this.disablePinnedMemory = pinned.checked;
        if (gpuPool) this.useGpuPool = gpuPool.checked;
        var poolDevices = document.querySelectorAll('[data-comfy-pool-device]');
        if (poolDevices.length) {
            this.selectedPoolDevices = [];
            for (var pd = 0; pd < poolDevices.length; pd++) {
                if (poolDevices[pd].checked) this.selectedPoolDevices.push(poolDevices[pd].value);
            }
        }
        if (advanced) this.startupFlagsOpen = advanced.open;
        var sections = document.querySelectorAll('[data-comfy-startup-section]');
        if (sections.length) {
            this.startupSectionsOpen = {};
            for (var s = 0; s < sections.length; s++) {
                this.startupSectionsOpen[sections[s].dataset.comfyStartupSection] = sections[s].open;
            }
        }
        var controls = document.querySelectorAll('[data-comfy-startup-key]');
        for (var i = 0; i < controls.length; i++) {
            var control = controls[i];
            var key = control.dataset.comfyStartupKey;
            var kind = control.dataset.comfyStartupKind;
            if (kind === 'boolean') {
                this.startupOptions[key] = control.checked;
            } else if (kind === 'integer') {
                this.startupOptions[key] = parseInt(control.value, 10);
            } else if (kind === 'number') {
                this.startupOptions[key] = control.value === '' ? null : parseFloat(control.value);
            } else {
                this.startupOptions[key] = control.value;
            }
        }
    },

    bindSelectionHandlers: function() {
        var deviceSel = document.getElementById('comfy-device');
        var vramSel = document.getElementById('comfy-vram');
        var precSel = document.getElementById('comfy-precision');
        if (deviceSel) {
            deviceSel.addEventListener('change', function() {
                TabComfy.selectedDevice = deviceSel.value;
                TabComfy.render();
            });
        }
        if (vramSel) {
            vramSel.addEventListener('change', function() {
                TabComfy.selectedVram = vramSel.value;
            });
        }
        if (precSel) {
            precSel.addEventListener('change', function() {
                TabComfy.selectedPrecision = precSel.value;
            });
        }
        var previewSel = document.getElementById('comfy-preview-method');
        if (previewSel) previewSel.addEventListener('change', function() {
            TabComfy.selectedPreviewMethod = previewSel.value;
        });
        var pinned = document.getElementById('comfy-disable-pinned-memory');
        if (pinned) pinned.addEventListener('change', function() {
            TabComfy.disablePinnedMemory = pinned.checked;
        });
        var gpuPool = document.getElementById('comfy-use-gpu-pool');
        if (gpuPool) gpuPool.addEventListener('change', function() {
            TabComfy.useGpuPool = gpuPool.checked;
            TabComfy.render();
        });
        var poolDevices = document.querySelectorAll('[data-comfy-pool-device]');
        for (var pd = 0; pd < poolDevices.length; pd++) {
            poolDevices[pd].addEventListener('change', function() {
                TabComfy.syncSelectionFromDom();
            });
        }
        var advanced = document.getElementById('comfy-startup-flags');
        if (advanced) advanced.addEventListener('toggle', function() {
            TabComfy.startupFlagsOpen = advanced.open;
        });
        var sections = document.querySelectorAll('[data-comfy-startup-section]');
        for (var s = 0; s < sections.length; s++) {
            sections[s].addEventListener('toggle', function() {
                TabComfy.startupSectionsOpen[this.dataset.comfyStartupSection] = this.open;
            });
        }
        var controls = document.querySelectorAll('[data-comfy-startup-key]');
        for (var i = 0; i < controls.length; i++) {
            controls[i].addEventListener('change', function() {
                TabComfy.syncSelectionFromDom();
                TabComfy.updateStartupCount();
            });
        }
    },

    startupValue: function(option) {
        return Object.prototype.hasOwnProperty.call(this.startupOptions, option.key)
            ? this.startupOptions[option.key] : option.default;
    },

    selectedStartupCount: function() {
        var count = (this.selectedPreviewMethod || 'auto') !== 'auto' ? 1 : 0;
        if (this.disablePinnedMemory) count++;
        for (var g = 0; g < this.startupGroups.length; g++) {
            var options = this.startupGroups[g].options || [];
            for (var i = 0; i < options.length; i++) {
                var value = this.startupValue(options[i]);
                if (String(value) !== String(options[i].default)) count++;
            }
        }
        return count;
    },

    updateStartupCount: function() {
        var badge = document.getElementById('comfy-startup-count');
        if (badge) badge.textContent = this.selectedStartupCount() + ' selected';
    },

    startupGroupCount: function(options) {
        var count = 0;
        for (var i = 0; i < options.length; i++) {
            if (String(this.startupValue(options[i])) !== String(options[i].default)) count++;
        }
        return count;
    },

    renderStartupControl: function(option) {
        var E = App.esc;
        var value = this.startupValue(option);
        var attrs = ' data-comfy-startup-key="' + E(option.key) + '" data-comfy-startup-kind="' + E(option.kind) + '"';
        var html = '';
        if (option.kind === 'boolean') {
            html += '<label class="comfy-option comfy-option-check"><input type="checkbox"' + attrs + (value ? ' checked' : '') + '><span><strong>' + E(option.label) + '</strong><small>' + E(option.description || '') + '</small></span></label>';
        } else {
            html += '<label class="comfy-option"><span>' + E(option.label) + '</span>';
            if (option.kind === 'select') {
                html += '<select' + attrs + '>';
                var choices = option.choices || [];
                for (var c = 0; c < choices.length; c++) {
                    html += '<option value="' + E(choices[c].value) + '"' + (String(value) === String(choices[c].value) ? ' selected' : '') + '>' + E(choices[c].label) + '</option>';
                }
                html += '</select>';
            } else {
                html += '<input type="number"' + attrs + ' value="' + (value === null || value === undefined ? '' : E(value)) + '" min="' + E(option.min) + '" max="' + E(option.max) + '" step="' + E(option.step || 1) + '">';
            }
            html += '<small>' + E(option.description || '') + '</small></label>';
        }
        return html;
    },

    renderStartupOptions: function() {
        var E = App.esc;
        var count = this.selectedStartupCount();
        var html = '<details id="comfy-startup-flags" class="comfy-startup-flags"' + (this.startupFlagsOpen ? ' open' : '') + '>';
        html += '<summary>Advanced startup flags <span id="comfy-startup-count" class="badge badge-blue">' + count + ' selected</span></summary>';
        html += '<div class="comfy-startup-note">Common choices are immediately below. Open a category only when you need its less-common flags.</div>';
        html += '<div class="comfy-option-group comfy-quick-picks"><h3>Quick picks</h3><div class="comfy-option-grid">';
        html += '<label class="comfy-option"><span>Preview method</span><select id="comfy-preview-method">';
        var previewOptions = [['auto', 'Automatic'], ['none', 'None'], ['latent2rgb', 'Latent2RGB'], ['taesd', 'TAESD']];
        for (var p = 0; p < previewOptions.length; p++) {
            html += '<option value="' + previewOptions[p][0] + '"' + (this.selectedPreviewMethod === previewOptions[p][0] ? ' selected' : '') + '>' + previewOptions[p][1] + '</option>';
        }
        html += '</select><small>Sampler preview implementation.</small></label>';
        var featuredKeys = ['attention', 'cache_policy', 'dynamic_vram'];
        var featured = {};
        for (var fg = 0; fg < this.startupGroups.length; fg++) {
            var featureOptions = this.startupGroups[fg].options || [];
            for (var fi = 0; fi < featureOptions.length; fi++) featured[featureOptions[fi].key] = featureOptions[fi];
        }
        for (var fk = 0; fk < featuredKeys.length; fk++) {
            if (featured[featuredKeys[fk]]) html += this.renderStartupControl(featured[featuredKeys[fk]]);
        }
        html += '</div></div>';
        if (!this.startupGroups.length) {
            html += '<div class="empty-state">Loading startup flag catalog...</div>';
        }
        for (var g = 0; g < this.startupGroups.length; g++) {
            var group = this.startupGroups[g] || {};
            var options = group.options || [];
            var remaining = options.filter(function(option) { return featuredKeys.indexOf(option.key) === -1; });
            if (!remaining.length) continue;
            var groupCount = this.startupGroupCount(remaining);
            var sectionKey = 'group-' + g;
            html += '<details class="comfy-option-section" data-comfy-startup-section="' + sectionKey + '"' + (this.startupSectionsOpen[sectionKey] ? ' open' : '') + '><summary><span>' + E(group.name || 'Options') + '</span><span class="badge' + (groupCount ? ' badge-blue' : '') + '">' + groupCount + ' selected</span></summary>';
            html += '<div class="comfy-option-grid">';
            for (var i = 0; i < options.length; i++) {
                var option = options[i];
                if (featuredKeys.indexOf(option.key) === -1) html += this.renderStartupControl(option);
            }
            html += '</div></details>';
        }
        html += '<details class="comfy-option-section" data-comfy-startup-section="troubleshooting"' + (this.startupSectionsOpen.troubleshooting ? ' open' : '') + '><summary><span>Troubleshooting</span><span class="badge">' + (this.disablePinnedMemory ? '1 selected' : '0 selected') + '</span></summary>';
        html += '<div class="comfy-option-grid"><label class="comfy-option comfy-option-check"><input id="comfy-disable-pinned-memory" type="checkbox"' + (this.disablePinnedMemory ? ' checked' : '') + '><span><strong>Disable pinned memory</strong><small>Useful for low-memory or incompatible systems. Adds <code>--disable-pinned-memory</code>.</small></span></label></div></details>';
        html += '</details>';
        return html;
    },

    renderDevices: function() {
        var sel = document.getElementById('comfy-device');
        if (!sel) return;
        this.syncSelectionFromDom();
        var devs = App.state.devices;
        var selectedDevice = '';
        for (var i = 0; i < devs.length; i++) {
            if (devs[i].id === this.selectedDevice) {
                selectedDevice = this.selectedDevice;
                break;
            }
        }
        if (!selectedDevice && devs.length > 0) selectedDevice = devs[0].id;
        this.selectedDevice = selectedDevice;
        sel.innerHTML = '';
        for (var j = 0; j < devs.length; j++) {
            var opt = document.createElement('option');
            opt.value = devs[j].id;
            opt.textContent = devs[j].id + ' - ' + devs[j].name;
            opt.selected = devs[j].id === selectedDevice;
            sel.appendChild(opt);
        }
    },

    render: function() {
        this.syncSelectionFromDom();

        var el = document.getElementById('tab-comfy');
        var instances = App.state.comfyInstances;
        var E = App.esc;
        var devs = App.state.devices;
        var cudaDevices = devs.filter(function(item) {
            return String(item.id || '').indexOf('cuda:') === 0;
        });
        var selectedDevice = '';
        for (var i = 0; i < devs.length; i++) {
            if (devs[i].id === this.selectedDevice) {
                selectedDevice = this.selectedDevice;
                break;
            }
        }
        if (!selectedDevice && devs.length > 0) selectedDevice = devs[0].id;
        this.selectedDevice = selectedDevice;
        var selectedVram = this.selectedVram || 'normal';
        var readyInstances = instances.filter(function(item) { return item.status === 'ready'; });
        var selectedExecution = this.selectedExecutionInstance;
        if (!readyInstances.some(function(item) { return item.instance_id === selectedExecution; })) {
            selectedExecution = readyInstances.length ? readyInstances[0].instance_id : null;
            if (selectedExecution !== this.selectedExecutionInstance) {
                this.executionJobs = [];
                this.executionQueue = { queue_running: [], queue_pending: [] };
                this.executionLastLoaded = 0;
                this.executionError = '';
            }
            this.selectedExecutionInstance = selectedExecution;
        }

        var html = '<div class="page-heading"><div class="page-heading-copy">';
        html += '<div class="page-eyebrow">ComfyUI runtime</div><h1>ComfyUI</h1>';
        html += '<p>Start a visual-workflow engine, then manage its queue and custom nodes. Models stay unloaded until a workflow needs them.</p>';
        html += '</div><div class="page-heading-actions"><a class="btn btn-sm" href="/static/comfy-api-guide.html" target="_blank" rel="noopener">GPU &amp; API guide</a><button class="btn btn-sm" onclick="TabComfy.openWorkflows()">Workflows</button><button class="btn btn-sm" onclick="TabComfy.openComfyModels()">Comfy models</button><button class="btn btn-sm" onclick="TabComfy.refresh()">Refresh</button></div></div>';
        html += '<div class="card"><div class="flex-between" style="gap:12px;flex-wrap:wrap"><div><h2>ComfyUI installation</h2><p class="card-helper">Engine version and installed workflow-template support. Checking for updates does not install anything.</p></div></div><div id="comfy-installation-panel"></div></div>';
        html += '<div class="card"><h2>Start ComfyUI</h2>';
        html += '<p class="card-helper">The recommended defaults work for most workflows. Change VRAM or precision only when a workflow requires it.</p>';
        html += '<div class="comfy-start-controls mb-8">';
        html += '<div class="field-row"><label class="field-label" for="comfy-device">Device</label><select class="field-control" id="comfy-device">';
        for (var d = 0; d < devs.length; d++) {
            var deviceId = devs[d].id;
            html += '<option value="' + E(deviceId) + '"' + (deviceId === selectedDevice ? ' selected' : '') + '>' + E(devs[d].id) + ' - ' + E(devs[d].name) + '</option>';
        }
        html += '</select><span class="field-hint">GPU or CPU used by this instance.</span></div>';
        if (cudaDevices.length > 1) {
            html += '<div class="field-row"><span class="field-label">GPU pool</span><label class="comfy-option comfy-option-check">'
                + '<input id="comfy-use-gpu-pool" type="checkbox"' + (this.useGpuPool ? ' checked' : '') + '>'
                + '<span><strong>Enable component placement</strong><small>The selected device remains primary; checked GPUs become available without allocating VRAM until used.</small></span></label>';
            if (this.useGpuPool && String(selectedDevice).indexOf('cuda:') === 0) {
                html += '<div class="comfy-gpu-pool-list">';
                for (var gp = 0; gp < cudaDevices.length; gp++) {
                    var poolDev = cudaDevices[gp];
                    var isPrimary = poolDev.id === selectedDevice;
                    var poolChecked = isPrimary || !this.selectedPoolDevices.length || this.selectedPoolDevices.indexOf(poolDev.id) !== -1;
                    html += '<label class="comfy-pool-device"><input type="checkbox" data-comfy-pool-device value="' + E(poolDev.id) + '"'
                        + (poolChecked ? ' checked' : '') + (isPrimary ? ' disabled' : '') + '><span><strong>'
                        + E(poolDev.id + ' - ' + poolDev.name) + (isPrimary ? ' (primary)' : '') + '</strong><small>'
                        + E(String(poolDev.vram_free_mb || 0) + ' MB free / ' + String(poolDev.vram_total_mb || 0) + ' MB')
                        + '</small></span></label>';
                }
                html += '</div>';
            }
            html += '</div>';
        }
        html += '<div class="field-row"><label class="field-label" for="comfy-vram">Memory mode</label><select class="field-control" id="comfy-vram">';
        html += '<option value="normal"' + (selectedVram === 'normal' ? ' selected' : '') + '>Normal VRAM</option>';
        html += '<option value="force_normal"' + (selectedVram === 'force_normal' ? ' selected' : '') + '>Force Normal VRAM</option>';
        html += '<option value="high"' + (selectedVram === 'high' ? ' selected' : '') + '>High VRAM (keep models loaded)</option>';
        html += '<option value="gpu_only"' + (selectedVram === 'gpu_only' ? ' selected' : '') + '>GPU Only</option>';
        html += '<option value="low"' + (selectedVram === 'low' ? ' selected' : '') + '>Low VRAM</option>';
        html += '<option value="none"' + (selectedVram === 'none' ? ' selected' : '') + '>No VRAM (CPU offload)</option>';
        html += '<option value="cpu"' + (selectedVram === 'cpu' ? ' selected' : '') + '>CPU Only</option>';
        html += '</select><span class="field-hint">Normal VRAM is the safe starting point.</span></div>';
        // Precision dropdown — maps to ComfyUI's --force-fp16/--bf16-unet/--force-fp32 flags
        var selectedPrec = this.selectedPrecision || '';
        var precOpts = [
            { v: '',     label: 'auto precision' },
            { v: 'fp16', label: 'fp16 (force)' },
            { v: 'bf16', label: 'bf16' },
            { v: 'fp32', label: 'fp32 (force)' },
        ];
        html += '<div class="field-row"><label class="field-label" for="comfy-precision">Precision</label><select class="field-control" id="comfy-precision" title="ComfyUI model precision (UNet/VAE/text encoder)">';
        for (var pi = 0; pi < precOpts.length; pi++) {
            var po = precOpts[pi];
            html += '<option value="' + E(po.v) + '"' + (po.v === selectedPrec ? ' selected' : '') + '>' + E(po.label) + '</option>';
        }
        html += '</select><span class="field-hint">Auto selects a compatible precision.</span></div>';
        html += '<div class="field-row comfy-start-action"><span class="field-label">Action</span><button id="comfy-start-btn" class="btn btn-primary btn-sm" onclick="TabComfy.startInstance()"'
            + (this.startPending ? ' disabled' : '') + '>' + (this.startPending ? 'Starting...' : 'Start ComfyUI') + '</button><span class="field-hint">Starting the engine does not load a model.</span></div>';
        if (instances.length > 0) {
            html += '<div class="field-row comfy-start-action"><span class="field-label">Running instances</span><button class="btn btn-danger btn-sm" onclick="TabComfy.stopAll()">Stop all</button></div>';
        }
        html += '</div>';
        html += this.renderStartupOptions();

        if (instances.some(function(item) { return item.status === 'starting'; })) {
            html += '<div class="comfy-starting-note" role="status"><strong>Starting the engine...</strong>'
                + '<span>A first GPU start can take a few minutes while WSL initializes the shared CUDA driver. No model weights are being loaded.</span></div>';
        }

        if (instances.length === 0) {
            html += '<div class="empty-state">No ComfyUI instances running. Start one above.</div>';
        } else {
            html += '<h3>Running instances</h3>';
            html += '<table><thead><tr><th>Instance</th><th>Device</th><th>Port</th><th>VRAM Mode</th><th>Precision</th><th>Status</th><th>VRAM</th><th></th></tr></thead><tbody>';
            for (var k = 0; k < instances.length; k++) {
                var inst = instances[k];
                var statusCls = 'status-' + inst.status;
                html += '<tr>';
                html += '<td>' + E(inst.instance_id) + '</td>';
                var pool = Array.isArray(inst.gpu_pool) ? inst.gpu_pool : [];
                html += '<td><span class="badge badge-blue">' + E(pool.length > 1 ? pool.join(' + ') : inst.device) + '</span></td>';
                html += '<td>';
                // FRO-3: coerce the server-supplied port to a number before
                // interpolating it into the href/text (defensive; App.esc only
                // covers HTML, not this numeric value).
                var instPort = Number(inst.port);
                if (inst.status === 'ready') {
                    var comfyHost = window.location.hostname || 'localhost';
                    html += '<a href="http://' + E(comfyHost) + ':' + instPort + '" target="_blank" style="color:var(--accent-blue)">' + instPort + '</a>';
                } else {
                    html += instPort;
                }
                html += '</td>';
                html += '<td>' + E(inst.vram_mode) + '</td>';
                html += '<td>' + (inst.precision ? '<span class="badge badge-blue">' + E(inst.precision) + '</span>' : '<span style="color:var(--text-muted)">auto</span>') + '</td>';
                html += '<td><span class="status-dot ' + statusCls + '"></span>' + E(inst.status) + '</td>';
                html += '<td>' + (inst.vram_used_mb || 0) + '/' + (inst.vram_total_mb || 0) + ' MB</td>';
                html += '<td><div class="btn-group">';
                html += '<button class="btn btn-sm" data-iid="' + E(inst.instance_id) + '" onclick="TabComfy.showLogs(this.dataset.iid)">Logs</button>';
                html += '<button class="btn btn-danger btn-sm" data-iid="' + E(inst.instance_id) + '" onclick="TabComfy.stopInstance(this.dataset.iid)">Stop</button>';
                html += '</div></td>';
                html += '</tr>';
            }
            html += '</tbody></table>';
        }
        html += '</div>';

        html += '<div class="card"><h2>ComfyUI Execution</h2>';
        if (!readyInstances.length) {
            html += '<div class="empty-state">Start a ComfyUI instance to inspect jobs and queue state.</div>';
        } else {
            html += '<div class="flex-between mb-8" style="flex-wrap:wrap;gap:8px">';
            html += '<div style="display:flex;gap:8px;align-items:end;flex-wrap:wrap">';
            html += '<div class="field-row" style="margin:0"><label class="field-label" for="comfy-execution-instance">Instance</label><select class="field-control" id="comfy-execution-instance" onchange="TabComfy.selectExecutionInstance(this.value)">';
            for (var ri = 0; ri < readyInstances.length; ri++) {
                var readyId = readyInstances[ri].instance_id;
                html += '<option value="' + E(readyId) + '"' + (readyId === selectedExecution ? ' selected' : '') + '>' + E(readyId) + '</option>';
            }
            html += '</select></div>';
            html += '<button class="btn btn-sm" onclick="TabComfy.refreshExecution(true)">Refresh</button>';
            html += '</div><div class="btn-group">';
            html += '<button class="btn btn-sm" onclick="TabComfy.clearPending()">Clear Pending</button>';
            html += '<button class="btn btn-danger btn-sm" onclick="TabComfy.cancelAllJobs()">Cancel All</button>';
            html += '<button class="btn btn-sm" onclick="TabComfy.unloadModels()">Unload Models</button>';
            html += '</div></div>';
            html += '<div id="comfy-execution-panel"></div>';
        }
        html += '</div>';

        html += '<div class="card"><h2>Custom Nodes</h2>';
        html += '<div style="margin-bottom:8px;font-size:12px;color:var(--text-secondary)">'
            + '<span class="badge badge-purple">OmniBridge</span> '
            + 'auto-installed — adds <code>OmniChat</code>, <code>OmniDescribe</code>, '
            + '<code>OmniTranscribe</code>, <code>OmniTTS</code> nodes that call back to '
            + 'this gateway. Find them under the <strong>OmniBridge</strong> category in '
            + 'the ComfyUI node menu.'
            + '</div>';
        html += '<div id="comfy-nodes-panel"><div class="empty-state">Loading...</div></div></div>';

        el.innerHTML = html;
        this.bindSelectionHandlers();
        this.renderInstallationCard();
        this.renderExecutionPanel();
        this.pollExecution();
        this.loadNodes();
    },

    selectExecutionInstance: function(id) {
        this.selectedExecutionInstance = id || null;
        this.executionJobs = [];
        this.executionQueue = { queue_running: [], queue_pending: [] };
        this.executionError = '';
        this.executionLastLoaded = 0;
        this.renderExecutionPanel();
        this.refreshExecution(true);
    },

    executionPath: function(subpath) {
        return '/api/comfy/' + encodeURIComponent(this.selectedExecutionInstance)
            + '/proxy/' + subpath;
    },

    pollExecution: function() {
        var tab = document.getElementById('tab-comfy');
        if (!tab || !tab.classList.contains('active') || !this.selectedExecutionInstance) return;
        if (Date.now() - this.executionLastLoaded < 2500) return;
        this.refreshExecution(false);
    },

    refreshExecution: async function(showError) {
        if (!this.selectedExecutionInstance || this.executionLoading) return;
        this.executionLoading = true;
        this.renderExecutionPanel();
        try {
            var results = await Promise.all([
                App.api('GET', this.executionPath('api/jobs?limit=50&sort_by=created_at&sort_order=desc')),
                App.api('GET', this.executionPath('queue')),
            ]);
            this.executionJobs = (results[0] && results[0].jobs) || [];
            this.executionQueue = results[1] || { queue_running: [], queue_pending: [] };
            this.executionError = '';
            this.executionLastLoaded = Date.now();
        } catch(e) {
            this.executionError = e.message;
            if (showError) App.toast('Execution refresh failed: ' + e.message, 'error');
        } finally {
            this.executionLoading = false;
            this.renderExecutionPanel();
        }
    },

    renderExecutionPanel: function() {
        var panel = document.getElementById('comfy-execution-panel');
        if (!panel) return;
        var E = App.esc;
        if (this.executionLoading && !this.executionLastLoaded) {
            panel.innerHTML = '<div class="empty-state">Loading live ComfyUI jobs...</div>';
            return;
        }
        if (this.executionError) {
            panel.innerHTML = '<div class="empty-state">Execution state unavailable: ' + E(this.executionError) + '</div>';
            return;
        }
        var queue = this.executionQueue || {};
        var running = Array.isArray(queue.queue_running) ? queue.queue_running.length : 0;
        var pending = Array.isArray(queue.queue_pending) ? queue.queue_pending.length : 0;
        var jobs = Array.isArray(this.executionJobs) ? this.executionJobs : [];
        var html = '<div class="mb-8" style="font-size:12px;color:var(--text-secondary)">'
            + '<span class="badge badge-blue">Running ' + running + '</span> '
            + '<span class="badge badge-purple">Pending ' + pending + '</span> '
            + '<span>Showing ' + jobs.length + ' recent jobs</span></div>';
        if (!jobs.length) {
            html += '<div class="empty-state">No ComfyUI jobs are currently recorded.</div>';
            panel.innerHTML = html;
            return;
        }
        html += '<table><thead><tr><th>Job</th><th>Status</th><th>Workflow</th><th>Outputs</th><th></th></tr></thead><tbody>';
        for (var i = 0; i < jobs.length; i++) {
            var job = jobs[i] || {};
            var jobId = String(job.id || '');
            var active = job.status === 'pending' || job.status === 'in_progress';
            html += '<tr><td><code>' + E(jobId) + '</code></td>';
            html += '<td><span class="badge ' + (active ? 'badge-blue' : '') + '">' + E(job.status || 'unknown') + '</span></td>';
            html += '<td>' + E(job.workflow_id || '') + '</td>';
            html += '<td>' + Number(job.outputs_count || 0) + '</td><td>';
            if (active) {
                html += '<button class="btn btn-danger btn-sm" data-job-id="' + E(jobId) + '" onclick="TabComfy.cancelJob(this.dataset.jobId)">Cancel</button>';
            }
            html += '</td></tr>';
        }
        html += '</tbody></table>';
        panel.innerHTML = html;
    },

    cancelJob: async function(jobId) {
        if (!jobId || !confirm('Cancel ComfyUI job ' + jobId + '?')) return;
        try {
            var data = await App.api('POST', this.executionPath('api/jobs/' + encodeURIComponent(jobId) + '/cancel'), {});
            App.toast(data && data.cancelled ? 'Job cancelled' : 'Job was already finished or absent', data && data.cancelled ? 'success' : 'info');
        } catch(e) {
            App.toast('Cancel failed: ' + e.message, 'error');
        } finally {
            this.executionLastLoaded = 0;
            await this.refreshExecution(true);
        }
    },

    clearPending: async function() {
        if (!this.selectedExecutionInstance || !confirm('Clear pending jobs? The running job will continue.')) return;
        try {
            await App.api('POST', this.executionPath('queue'), { clear: true });
            App.toast('Pending ComfyUI queue cleared', 'success');
        } catch(e) {
            App.toast('Clear pending failed: ' + e.message, 'error');
        } finally {
            this.executionLastLoaded = 0;
            await this.refreshExecution(true);
        }
    },

    cancelAllJobs: async function() {
        if (!this.selectedExecutionInstance || !confirm('Cancel every running and pending ComfyUI job in the current queue?')) return;
        try {
            var snapshot = await App.api('GET', this.executionPath('api/jobs?status=pending%2Cin_progress&sort_by=created_at&sort_order=asc'));
            var jobs = (snapshot && snapshot.jobs) || [];
            var ids = jobs.map(function(job) { return job.id; }).filter(Boolean);
            if (!ids.length) {
                App.toast('ComfyUI queue is already empty', 'info');
            } else {
                await App.api('POST', this.executionPath('api/jobs/cancel'), { job_ids: ids });
                App.toast('Cancellation sent for ' + ids.length + ' ComfyUI job(s)', 'success');
            }
        } catch(e) {
            App.toast('Cancel all failed: ' + e.message, 'error');
        } finally {
            this.executionLastLoaded = 0;
            await this.refreshExecution(true);
        }
    },

    unloadModels: async function() {
        if (!this.selectedExecutionInstance || !confirm('Unload models and free cached ComfyUI memory? Running work may be interrupted or slowed.')) return;
        try {
            await App.api('POST', this.executionPath('free'), { unload_models: true, free_memory: true });
            App.toast('ComfyUI accepted the unload/free-memory request', 'success');
        } catch(e) {
            App.toast('Unload failed: ' + e.message, 'error');
        } finally {
            this.executionLastLoaded = 0;
            await this.refreshExecution(true);
        }
    },

    startInstance: async function() {
        if (this.startPending) return;
        var device = document.getElementById('comfy-device').value;
        var vram = document.getElementById('comfy-vram').value;
        var precSel = document.getElementById('comfy-precision');
        var precision = precSel ? (precSel.value || null) : null;
        this.syncSelectionFromDom();
        this.selectedDevice = device;
        this.selectedVram = vram;
        this.selectedPrecision = precision || '';
        this.startPending = true;
        this.render();
        var label = device + (precision ? ' [' + precision + ']' : '');
        App.toast('Starting ComfyUI on ' + label + '...', 'info');
        try {
            var body = { device: device, vram_mode: vram };
            if (precision) body.precision = precision;
            body.preview_method = this.selectedPreviewMethod || 'auto';
            body.disable_pinned_memory = !!this.disablePinnedMemory;
            body.startup_options = Object.assign({}, this.startupOptions);
            if (this.useGpuPool && String(device).indexOf('cuda:') === 0) {
                var selectedPool = this.selectedPoolDevices.slice();
                if (selectedPool.indexOf(device) === -1) selectedPool.unshift(device);
                body.gpu_pool = selectedPool;
            }
            var data = await App.api('POST', '/api/comfy/start', body);
            App.toast('ComfyUI started on port ' + data.port, 'success');
        } catch(e) {
            App.toast('Start failed: ' + e.message, 'error');
        } finally {
            this.startPending = false;
            await App.refreshWorkers();
            this.render();
        }
    },

    stopInstance: async function(id) {
        try {
            await App.api('POST', '/api/comfy/' + encodeURIComponent(id) + '/stop');
            await App.refreshWorkers();
            this.render();
            App.toast('Instance stopped', 'success');
        } catch(e) {
            App.toast('Stop failed: ' + e.message, 'error');
        }
    },

    showLogs: async function(id) {
        try {
            var data = await App.api('GET', '/api/comfy/' + encodeURIComponent(id) + '/logs?lines=200');
            App.showTextModal('ComfyUI logs: ' + id, (data.lines || []).join('\n'));
        } catch(e) {
            App.toast('Failed to load logs: ' + e.message, 'error');
        }
    },

    stopAll: async function() {
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

    loadNodes: async function() {
        var panel = document.getElementById('comfy-nodes-panel');
        if (!panel) return;
        var E = App.esc;
        try {
            var data = await App.api('GET', '/api/comfy/nodes');
            var nodes = data.nodes || [];
            if (nodes.length === 0) {
                panel.innerHTML = '<div class="empty-state">No custom nodes installed</div>';
                return;
            }
            var html = '<table><thead><tr><th>Node</th><th>Has Requirements</th></tr></thead><tbody>';
            for (var i = 0; i < nodes.length; i++) {
                html += '<tr><td>' + E(nodes[i].name) + '</td>';
                html += '<td>' + (nodes[i].has_requirements ? 'Yes' : 'No') + '</td></tr>';
            }
            html += '</tbody></table>';
            panel.innerHTML = html;
        } catch(e) {
            panel.innerHTML = '<div class="empty-state">Failed to load nodes</div>';
        }
    },
};
