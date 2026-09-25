/* ========================================================================== 
   Tab: Workflows - choose, verify, then run ComfyUI workflows
   ========================================================================== */

var TabWorkflows = {
    analyses: {},
    analysisLoading: false,
    selectedInstance: '',
    templateResults: [],
    templateSearching: false,
    inlineReport: null,
    placementMode: 'auto',
    placementReserveMB: 1024,
    placementEligible: [],
    placementEligibleInitialized: false,
    placementPrimary: '',
    placementRequireAll: false,
    placementOverrides: {},
    placementDirty: false,
    runPending: false,
    draftText: "",
    draftName: "",
    draftOpen: false,

    init: function() { this.load(); },
    onActivate: function() { this.load(); },

    openComfyModels: function() {
        if (typeof TabModels !== 'undefined' && TabModels.setViewMode) {
            TabModels.setViewMode('comfy', false);
        }
        App.activateTab('models');
    },

    load: async function() {
        this.analysisLoading = true;
        try {
            var list = await App.api('GET', '/api/workflows');
            App.state.workflows = list.workflows || [];
            this.render();
            var qs = this.selectedInstance
                ? '?instance_id=' + encodeURIComponent(this.selectedInstance) : '';
            var result = await App.api('GET', '/api/workflows/requirements' + qs);
            var reports = result.workflows || [];
            this.analyses = {};
            for (var i = 0; i < reports.length; i++) {
                this.analyses[reports[i].filename] = reports[i];
            }
        } catch (e) {
            App.toast('Workflows could not be refreshed: ' + e.message, 'error');
        } finally {
            this.analysisLoading = false;
            this.render();
        }
    },

    instanceLabel: function(inst) {
        return 'ComfyUI' + (inst.device ? ' on ' + inst.device : '');
    },

    readyInstances: function() {
        return (App.state.comfyInstances || []).filter(function(inst) {
            return inst.status === 'ready';
        });
    },

    targetInstance: function() {
        var el = document.getElementById('wf-instance');
        return el && el.value ? el.value : this.selectedInstance;
    },

    placementDevices: function() {
        var instanceId = this.targetInstance();
        var instance = this.readyInstances().filter(function(item) { return item.instance_id === instanceId; })[0];
        if (!instance) return [];
        var pool = Array.isArray(instance.gpu_pool) && instance.gpu_pool.length
            ? instance.gpu_pool : [instance.device];
        return pool.map(function(id) {
            return (App.state.devices || []).find(function(item) { return item.id === id; });
        }).filter(function(item) { return item && String(item.id || '').indexOf('cuda:') === 0; }).map(function(item, index) {
            return Object.assign({}, item, {
                stable_id: item.stable_id || item.uuid || item.id,
                placement_target: index === 0 ? 'primary' : 'auxiliary:' + index
            });
        });
    },

    syncPlacementFromDom: function() {
        var mode = document.getElementById('wf-placement-mode');
        var reserve = document.getElementById('wf-placement-reserve');
        var primary = document.getElementById('wf-placement-primary');
        var requireAll = document.getElementById('wf-placement-require-all');
        if (mode) this.placementMode = mode.value;
        if (reserve) this.placementReserveMB = Math.max(0, Number(reserve.value || 0));
        if (primary) this.placementPrimary = primary.value;
        if (requireAll) this.placementRequireAll = requireAll.checked;
        var deviceControls = document.querySelectorAll('[data-wf-placement-device]');
        var checked = document.querySelectorAll('[data-wf-placement-device]:checked');
        if (deviceControls.length) {
            this.placementEligible = [];
            for (var i = 0; i < checked.length; i++) this.placementEligible.push(checked[i].value);
        }
    },

    placementPolicy: function(filename) {
        var saved = (App.state.workflows || []).find(function(row) { return row.filename === filename; });
        if (filename && saved && saved.placement_policy && !this.placementDirty) {
            var policy = JSON.parse(JSON.stringify(saved.placement_policy));
            this.placementMode = policy.mode || 'auto';
            this.placementReserveMB = policy.reserve_mb == null ? 1024 : policy.reserve_mb;
            this.placementEligible = (policy.eligible_devices || []).slice();
            this.placementEligibleInitialized = true;
            this.placementPrimary = policy.primary_device || '';
            this.placementRequireAll = !!policy.require_all;
            this.placementOverrides[filename] = Object.assign({}, policy.overrides || {});
            return policy;
        }
        this.syncPlacementFromDom();
        var devices = this.placementDevices();
        var eligible = this.placementEligible.length
            ? this.placementEligible.slice()
            : (this.placementPrimary ? [this.placementPrimary] : []);
        return {
            mode: this.placementMode,
            eligible_devices: eligible,
            primary_device: this.placementPrimary || (devices[0] ? devices[0].stable_id : null),
            reserve_mb: Math.round(this.placementReserveMB),
            require_all: !!this.placementRequireAll,
            overrides: Object.assign({}, this.placementOverrides[filename || '__draft__'] || {})
        };
    },

    readinessCopy: function(report) {
        if (!report) return { cls: 'gray', label: 'Checking', detail: 'Inspecting requirements' };
        var models = report.models || {};
        var nodes = report.nodes || {};
        if (report.readiness === 'ready') return { cls: 'green', label: 'Ready', detail: 'Models and nodes verified' };
        if (report.readiness === 'needs-models') return {
            cls: 'orange', label: (models.missing_count || 0) + ' model' + ((models.missing_count || 0) === 1 ? '' : 's') + ' missing',
            detail: (models.downloadable_count || 0) + ' verified download' + ((models.downloadable_count || 0) === 1 ? '' : 's')
        };
        if (report.readiness === 'needs-nodes') return { cls: 'orange', label: (nodes.missing_count || 0) + ' node' + ((nodes.missing_count || 0) === 1 ? '' : 's') + ' missing', detail: 'Install custom nodes before running' };
        if (report.readiness === 'needs-api-export') return { cls: 'red', label: 'API export needed', detail: 'Open in ComfyUI and export API format' };
        if (report.readiness === 'needs-review') return { cls: 'orange', label: 'Review required', detail: 'Unknown custom model input' };
        return { cls: 'blue', label: 'Models checked', detail: 'Start ComfyUI to verify nodes' };
    },

    render: function() {
        var el = document.getElementById('tab-workflows');
        if (!el) return;
        var priorDraft = document.getElementById('wf-json-draft');
        var priorName = document.getElementById('wf-save-name');
        var priorOpen = document.getElementById('wf-advanced');
        if (priorDraft) this.draftText = priorDraft.value;
        if (priorName) this.draftName = priorName.value;
        if (priorOpen) this.draftOpen = priorOpen.open;
        var E = App.esc;
        var workflows = App.state.workflows || [];
        var readyInstances = this.readyInstances();
        var hasInstance = readyInstances.length > 0;
        if (hasInstance && !this.selectedInstance) this.selectedInstance = readyInstances[0].instance_id;

        var html = '<div class="page-heading"><div class="page-heading-copy">';
        html += '<div class="page-eyebrow">ComfyUI workspace</div><h1>Workflows</h1>';
        html += '<p>Choose a workflow, verify its models and custom nodes, then run it. Nothing is loaded while requirements are checked.</p>';
        html += '</div><div class="page-heading-actions">';
        html += '<a class="btn btn-sm" href="/static/comfy-api-guide.html" target="_blank" rel="noopener">GPU &amp; API guide</a>';
        html += '<button class="btn btn-sm" type="button" onclick="App.activateTab(\'comfy\')">ComfyUI</button>';
        html += '<button class="btn btn-sm" type="button" onclick="TabWorkflows.openComfyModels()">Comfy models</button>';
        html += '<button id="wf-refresh" class="btn btn-sm">Refresh checks</button>';
        html += '<label for="wf-file" class="btn btn-primary btn-sm">Import workflow</label>';
        html += '</div></div>';

        html += '<div class="workflow-steps" aria-label="Workflow process">';
        html += this.renderStep('1', 'Choose', workflows.length ? workflows.length + ' saved workflow' + (workflows.length === 1 ? '' : 's') : 'Import your first workflow', workflows.length ? 'done' : 'active');
        html += this.renderStep('2', 'Verify', 'Models, nodes, and API format', workflows.length ? 'active' : '');
        html += this.renderStep('3', 'Run', hasInstance ? 'ComfyUI is available' : 'Start ComfyUI when ready', hasInstance ? '' : '');
        html += '</div>';

        if (!hasInstance) {
            html += App.prereqBanner({
                level: 'warn',
                text: "ComfyUI is not running. Model checks still work; start it when you want to verify nodes and run.",
                actionId: 'wf-start-comfy',
                actionLabel: 'Start ComfyUI'
            });
        }

        html += '<section class="card workflow-import-card" aria-labelledby="wf-import-title">';
        html += '<div><h2 id="wf-import-title">Add a workflow</h2><p>Select a ComfyUI JSON file. It will be checked automatically after import.</p></div>';
        html += '<div class="workflow-import-actions">';
        html += '<input id="wf-file" type="file" accept=".json,application/json" aria-label="Workflow JSON file">';
        html += '<label class="wf-check"><input id="wf-overwrite" type="checkbox"> Replace a workflow with the same name</label>';
        html += '<button id="wf-import" class="btn btn-primary btn-sm">Import and check</button>';
        html += '</div></section>';

        html += '<section class="card" aria-labelledby="wf-library-title">';
        html += '<div class="workflow-library-head"><div><h2 id="wf-library-title">Your workflows</h2>';
        html += '<p>' + (this.analysisLoading ? 'Checking requirements...' : 'Requirement checks use local files first and do not load models.') + '</p></div>';
        if (hasInstance) {
            var opts = readyInstances.map(function(inst) {
                return { value: inst.instance_id, label: TabWorkflows.instanceLabel(inst) };
            });
            html += '<div class="workflow-target">' + App.field({
                type: 'select', id: 'wf-instance', label: 'Run on',
                hint: 'Also enables live custom-node verification.',
                value: this.selectedInstance, options: opts
            }) + '</div>';
        }
        html += '</div>';
        if (hasInstance) html += this.renderPlacementControls();
        html += '<div id="wf-status-live" class="sr-status" aria-live="polite">';
        html += this.analysisLoading ? 'Checking workflow requirements' : 'Workflow checks complete';
        html += '</div>';

        if (!workflows.length) {
            html += '<div class="empty-state">No saved workflows yet. Import a ComfyUI JSON file above.</div>';
        } else {
            html += '<div class="wf-grid">';
            for (var i = 0; i < workflows.length; i++) {
                html += this.renderWorkflowCard(workflows[i], this.analyses[workflows[i].filename], hasInstance);
            }
            html += '</div>';
        }
        html += '</section>';

        html += '<details id="wf-advanced" class="advanced-panel"><summary>Templates and JSON editor</summary><div class="advanced-panel-body">';
        html += '<div class="workflow-template-search"><div>';
        html += App.field({ type: 'text', id: 'wf-template-query', label: 'Find ComfyUI templates', hint: 'Search installed templates by purpose, node, or text.', placeholder: 'for example: image upscale' });
        html += '</div><button id="wf-template-search" class="btn btn-sm">' + (this.templateSearching ? 'Searching...' : 'Search templates') + '</button></div>';
        html += '<div id="wf-template-results">' + this.renderTemplateResults() + '</div>';
        html += '<div class="wf-editor">';
        html += '<div class="field-grid">';
        html += App.field({ type: 'text', id: 'wf-save-name', label: 'Filename', placeholder: 'workflow-name.json' });
        html += '<div class="field-row"><span class="field-label">Editor actions</span><div class="btn-group">';
        html += '<button id="wf-sample" class="btn btn-sm">Load safe sample</button>';
        html += '<button id="wf-analyze-draft" class="btn btn-sm">Check draft</button>';
        html += '<button id="wf-save" class="btn btn-primary btn-sm">Save JSON</button>';
        html += '<button id="wf-run" class="btn btn-sm"' + (hasInstance ? '' : ' disabled') + '>Run draft</button>';
        html += '</div></div></div>';
        html += '<label class="field-label" for="wf-json-draft">ComfyUI API workflow JSON</label>';
        html += '<textarea id="wf-json-draft" spellcheck="false" aria-describedby="wf-json-error" placeholder="Paste ComfyUI API workflow JSON here"></textarea>';
        html += '<div id="wf-json-error" class="workflow-inline-error" role="alert"></div>';
        html += '<div id="wf-draft-analysis" aria-live="polite">' + (this.inlineReport ? this.renderReport(this.inlineReport, '') : '') + '</div>';
        html += '</div></div></details>';

        el.innerHTML = html;
        document.getElementById('wf-json-draft').value = this.draftText;
        document.getElementById('wf-save-name').value = this.draftName;
        document.getElementById('wf-advanced').open = this.draftOpen;
        this.bindEvents();
    },

    renderStep: function(number, title, detail, state) {
        return '<div class="workflow-step ' + App.esc(state || '') + '"><span>' + App.esc(number) + '</span><div><strong>' + App.esc(title) + '</strong><small>' + App.esc(detail) + '</small></div></div>';
    },

    renderPlacementControls: function() {
        var E = App.esc;
        var devices = this.placementDevices();
        if (!devices.length) return '<div class="inline-alert warning">The selected Comfy instance has no visible CUDA GPU pool.</div>';
        if (!this.placementPrimary) this.placementPrimary = devices[0].stable_id;
        if (!this.placementEligibleInitialized) {
            this.placementEligible = devices.map(function(item) { return item.stable_id; });
            this.placementEligibleInitialized = true;
        }
        var selected = this.placementEligible;
        var html = '<details class="workflow-placement" open><summary>GPU placement</summary><div class="workflow-placement-body">';
        html += '<div class="workflow-placement-grid"><div class="field-row"><label class="field-label" for="wf-placement-mode">Mode</label>';
        html += '<select id="wf-placement-mode" class="field-control">';
        [['single', 'Single GPU'], ['auto', 'Automatic pool'], ['manual', 'Manual / hybrid']].forEach(function(option) {
            html += '<option value="' + option[0] + '"' + (TabWorkflows.placementMode === option[0] ? ' selected' : '') + '>' + option[1] + '</option>';
        });
        html += '</select><span class="field-hint">Manual locks selected components; unassigned components are still planned automatically.</span></div>';
        html += '<div class="field-row"><label class="field-label" for="wf-placement-primary">Preferred GPU</label><select id="wf-placement-primary" class="field-control">';
        for (var p = 0; p < devices.length; p++) {
            html += '<option value="' + E(devices[p].stable_id) + '"' + (this.placementPrimary === devices[p].stable_id ? ' selected' : '') + '>'
                + E(devices[p].name + ' - ' + devices[p].placement_target) + '</option>';
        }
        html += '</select><span class="field-hint">Single mode uses only this GPU. In pool modes it is preferred for the main diffusion model.</span></div>';
        html += '<div class="field-row"><label class="field-label" for="wf-placement-reserve">VRAM reserve (MB)</label><input id="wf-placement-reserve" class="field-control" type="number" min="0" max="262144" step="256" value="' + E(String(this.placementReserveMB)) + '"><span class="field-hint">Headroom kept free on every eligible GPU.</span></div>';
        html += '</div><div class="workflow-placement-devices">';
        for (var d = 0; d < devices.length; d++) {
            var device = devices[d];
            html += '<label><input type="checkbox" data-wf-placement-device value="' + E(device.stable_id) + '"'
                + (selected.indexOf(device.stable_id) !== -1 ? ' checked' : '') + '><span><strong>' + E(device.name) + '</strong><small>'
                + E(device.id + ' - ' + String(device.vram_free_mb || 0) + ' MB free - ' + device.placement_target)
                + '</small></span></label>';
        }
        html += '</div><label class="wf-check"><input id="wf-placement-require-all" type="checkbox"' + (this.placementRequireAll ? ' checked' : '') + '> Advanced: require every selected GPU to receive a component</label>';
        html += '<p class="field-hint">Plan checks do not load models. GPU UUIDs are saved in policies so selections survive CUDA index changes.</p>';
        html += '</div></details>';
        return html;
    },

    renderWorkflowCard: function(wf, report, hasInstance) {
        var E = App.esc;
        var state = this.readinessCopy(report);
        var canRun = !!(hasInstance && report && report.ready_to_run);
        var html = '<article class="wf-card" data-workflow-card="' + E(wf.filename) + '">';
        html += '<div class="flex-between"><div class="wf-name">' + E(wf.name) + '</div>';
        html += '<span class="badge badge-' + E(state.cls) + '">' + E(state.label) + '</span></div>';
        html += '<div class="wf-meta">' + E(String(wf.nodes)) + ' nodes - ' + E(String(wf.size_kb)) + ' KB</div>';
        html += '<p class="workflow-readiness-detail">' + E(state.detail) + '</p>';
        html += '<div class="workflow-primary-actions">';
        html += '<button class="btn btn-primary btn-sm" data-wf="' + E(wf.filename) + '" data-action="queue"' + (canRun ? '' : ' disabled title="Resolve the readiness items first"') + '>Run</button>';
        html += '<button class="btn btn-sm" data-wf="' + E(wf.filename) + '" data-action="plan">Plan GPUs</button>';
        html += '<button class="btn btn-sm" data-wf="' + E(wf.filename) + '" data-action="check">Check again</button>';
        if (report && (report.models || {}).downloadable_count) {
            html += '<button class="btn btn-sm" data-wf="' + E(wf.filename) + '" data-action="install-missing">Download verified (' + E(String(report.models.downloadable_count)) + ')</button>';
        }
        html += '</div>';
        html += '<details class="workflow-card-details"><summary>Requirements and options</summary>';
        html += this.renderReport(report, wf.filename);
        html += '<div class="btn-group mt-8">';
        html += '<button class="btn btn-sm" data-wf="' + E(wf.filename) + '" data-action="view">Open JSON</button>';
        html += '<button class="btn btn-danger btn-sm" data-wf="' + E(wf.filename) + '" data-action="delete">Delete</button>';
        html += '</div></details></article>';
        return html;
    },

    renderReport: function(report, filename) {
        if (!report) return '<div class="workflow-report-empty">Requirements have not been checked yet.</div>';
        var E = App.esc;
        var models = report.models || {};
        var nodes = report.nodes || {};
        var html = '<div class="workflow-report">';
        html += '<div class="workflow-report-counts">';
        html += '<span><strong>' + E(String(models.total || 0)) + '</strong> model refs</span>';
        html += '<span><strong>' + E(String(models.missing_count || 0)) + '</strong> missing</span>';
        html += '<span><strong>' + E(String(nodes.total || 0)) + '</strong> node types</span>';
        html += '</div>';
        var missing = models.missing || [];
        for (var i = 0; i < missing.length; i++) {
            var model = missing[i];
            html += '<div class="workflow-requirement-row"><div><strong>' + E(model.name) + '</strong><small>' + E(model.category) + (model.source ? ' - ' + E(model.source) : '') + '</small></div>';
            if (model.downloadable) {
                html += '<span class="badge badge-green">verified source</span>';
            } else {
                html += '<button class="btn btn-sm" data-wf="' + E(filename) + '" data-model-index="' + i + '" data-action="find-model">Find source</button>';
            }
            html += '</div>';
        }
        var unknown = models.unclassified_inputs || [];
        for (var u = 0; u < unknown.length; u++) {
            var exactCount = (unknown[u].installed_matches || []).length + (unknown[u].manager_candidates || []).length;
            html += '<div class="workflow-requirement-row warning"><div><strong>' + E(unknown[u].name) + '</strong><small>Unknown field ' + E(unknown[u].field) + ' in ' + E(unknown[u].node_type) + (exactCount ? ' - ' + E(String(exactCount)) + ' exact catalog match' + (exactCount === 1 ? '' : 'es') : '') + '</small></div>';
            html += '<button class="btn btn-sm" data-wf="' + E(filename) + '" data-unknown-index="' + u + '" data-action="find-unknown-model">Search all folders</button></div>';
        }
        var missingNodes = nodes.missing || [];
        for (var n = 0; n < missingNodes.length; n++) {
            html += '<div class="workflow-requirement-row warning"><div><strong>' + E(missingNodes[n].class_type) + '</strong><small>Custom node is not available in the running ComfyUI instance</small></div><span class="badge badge-orange">missing node</span></div>';
        }
        var warnings = report.warnings || [];
        for (var w = 0; w < warnings.length; w++) html += '<p class="workflow-report-warning">' + E(warnings[w]) + '</p>';
        if (!missing.length && !unknown.length && !missingNodes.length) {
            html += '<p class="workflow-report-ok">No known model or node blockers.</p>';
        }
        if (report.placement_plan) html += this.renderPlacementPlan(report.placement_plan, filename);
        html += '</div>';
        return html;
    },

    renderPlacementPlan: function(plan, filename) {
        var E = App.esc;
        var components = plan.components || [];
        var blockers = plan.blockers || [];
        var warnings = plan.warnings || [];
        var devices = this.placementDevices();
        var overrides = this.placementOverrides[filename || '__draft__'] || {};
        var html = '<div class="workflow-placement-plan ' + (plan.valid ? 'ready' : 'blocked') + '">';
        html += '<div class="flex-between"><strong>GPU plan</strong><span class="badge badge-' + (plan.valid ? 'green' : 'orange') + '">' + E(plan.status || '') + '</span></div>';
        html += '<p>' + E(String((plan.summary || {}).assigned_count || 0)) + ' component(s) across ' + E(String((plan.summary || {}).used_gpu_count || 0)) + ' GPU(s)'
            + ((plan.summary || {}).staged ? ' - staged unload enabled' : '') + '</p>';
        for (var i = 0; i < components.length; i++) {
            var item = components[i];
            var device = item.device || {};
            html += '<div class="workflow-placement-row"><span><strong>' + E(item.role) + '</strong><small>' + E(item.model_name || item.component_id) + ' - about ' + E(String(item.estimated_peak_mb || 0)) + ' MB</small></span>';
            html += '<select class="field-control" data-wf-placement-override data-wf="' + E(filename || '') + '" data-component="' + E(item.component_id) + '">';
            html += '<option value="">Automatic (' + E(device.name || device.target || '') + ')</option>';
            for (var d = 0; d < devices.length; d++) {
                var selected = overrides[item.component_id] === devices[d].stable_id;
                html += '<option value="' + E(devices[d].stable_id) + '"' + (selected ? ' selected' : '') + '>' + E(devices[d].name + ' - ' + devices[d].placement_target) + '</option>';
            }
            html += '</select></div>';
        }
        for (var b = 0; b < blockers.length; b++) html += '<p class="workflow-report-warning">' + E(blockers[b]) + '</p>';
        for (var w = 0; w < warnings.length; w++) html += '<p class="field-hint">' + E(warnings[w]) + '</p>';
        if (filename && plan.valid) html += '<button class="btn btn-sm mt-8" data-action="save-policy" data-wf="' + E(filename) + '">Save as workflow default</button>';
        html += '</div>';
        return html;
    },

    renderTemplateResults: function() {
        var E = App.esc;
        if (!this.templateResults.length) return '';
        var html = '<div class="workflow-template-results">';
        for (var i = 0; i < this.templateResults.length; i++) {
            var item = this.templateResults[i];
            html += '<div><span><strong>' + E(item.id) + '</strong><small>' + E(item.source) + (item.package ? ' - ' + E(item.package) : '') + '</small></span>';
            html += '<button class="btn btn-sm" data-action="load-template" data-template-id="' + E(item.id) + '" data-template-source="' + E(item.source || '') + '" data-template-package="' + E(item.package || '') + '">Open in editor</button></div>';
        }
        html += '</div>';
        return html;
    },

    bindEvents: function() {
        var self = this;
        var byId = function(id) { return document.getElementById(id); };
        if (byId('wf-start-comfy')) byId('wf-start-comfy').addEventListener('click', async function() {
            this.disabled = true;
            try { await App.startComfy(); } finally { await self.load(); }
        });
        if (byId('wf-refresh')) byId('wf-refresh').addEventListener('click', function() { self.load(); });
        if (byId('wf-import')) byId('wf-import').addEventListener('click', function() { self.importFile(); });
        if (byId('wf-instance')) byId('wf-instance').addEventListener('change', function() {
            self.selectedInstance = this.value;
            self.placementDirty = false;
            self.placementEligible = [];
            self.placementEligibleInitialized = false;
            self.placementPrimary = '';
            self.load();
        });
        ['wf-placement-mode', 'wf-placement-primary', 'wf-placement-reserve', 'wf-placement-require-all'].forEach(function(id) {
            if (byId(id)) byId(id).addEventListener('change', function() { self.placementDirty = true; self.syncPlacementFromDom(); });
        });
        var placementDevices = document.querySelectorAll('[data-wf-placement-device]');
        for (var pd = 0; pd < placementDevices.length; pd++) {
            placementDevices[pd].addEventListener('change', function() { self.placementDirty = true; self.syncPlacementFromDom(); });
        }
        var placementOverrides = document.querySelectorAll('[data-wf-placement-override]');
        for (var po = 0; po < placementOverrides.length; po++) {
            placementOverrides[po].addEventListener('change', function() {
                var key = this.dataset.wf || '__draft__';
                self.placementOverrides[key] = self.placementOverrides[key] || {};
                if (this.value) self.placementOverrides[key][this.dataset.component] = this.value;
                else delete self.placementOverrides[key][this.dataset.component];
                self.placementMode = 'manual';
                self.placementDirty = true;
                self.planWorkflow(this.dataset.wf || '', null);
            });
        }
        if (byId('wf-template-search')) byId('wf-template-search').addEventListener('click', function() { self.searchTemplates(); });
        if (byId('wf-template-query')) byId('wf-template-query').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') { e.preventDefault(); self.searchTemplates(); }
        });
        if (byId('wf-sample')) byId('wf-sample').addEventListener('click', function() { self.fillEmptyImageExample(); self.validateDraft(); });
        if (byId('wf-analyze-draft')) byId('wf-analyze-draft').addEventListener('click', function() { self.analyzeDraft(); });
        if (byId('wf-save')) byId('wf-save').addEventListener('click', function() { self.saveDraft(); });
        if (byId('wf-run')) byId('wf-run').addEventListener('click', function() { self.runDraft(); });
        if (byId('wf-json-draft')) byId('wf-json-draft').addEventListener('input', function() { self.inlineReport = null; self.validateDraft(); });

        var el = document.getElementById('tab-workflows');
        if (el) el.onclick = function(e) {
            var btn = e.target.closest ? e.target.closest('button[data-action]') : null;
            if (!btn) return;
            var action = btn.dataset.action;
            var filename = btn.dataset.wf || '';
            if (action === 'queue') self.queue(filename);
            else if (action === 'plan') self.planWorkflow(filename, btn);
            else if (action === 'save-policy') self.savePlacementPolicy(filename, btn);
            else if (action === 'check') self.check(filename, btn);
            else if (action === 'install-missing') self.installMissing(filename, btn);
            else if (action === 'find-model') self.findModel(filename, Number(btn.dataset.modelIndex || 0), btn);
            else if (action === 'find-unknown-model') self.findUnknownModel(filename, Number(btn.dataset.unknownIndex || 0), btn);
            else if (action === 'view') self.viewJson(filename);
            else if (action === 'delete') self.delete(filename);
            else if (action === 'load-template') self.loadTemplate(btn);
        };
        this.validateDraft();
    },

    validateDraft: function() {
        var draft = document.getElementById('wf-json-draft');
        var errEl = document.getElementById('wf-json-error');
        var raw = draft ? draft.value.trim() : '';
        var error = '';
        if (raw) {
            try {
                var parsed = JSON.parse(raw);
                if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) error = 'Workflow JSON must be an object.';
            } catch (e) { error = 'Invalid JSON: ' + e.message; }
        }
        if (errEl) { errEl.textContent = error; errEl.style.display = error ? '' : 'none'; }
        var invalid = !!error;
        ['wf-save', 'wf-analyze-draft'].forEach(function(id) {
            var button = document.getElementById(id);
            if (button) button.disabled = invalid;
        });
        var run = document.getElementById('wf-run');
        if (run) run.disabled = invalid || !this.readyInstances().length;
        return !invalid && !!raw;
    },

    parseDraft: function() {
        var raw = ((document.getElementById('wf-json-draft') || {}).value || '').trim();
        if (!raw) throw new Error('Workflow JSON is empty');
        var parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Workflow JSON must be an object');
        return parsed;
    },

    normalizeFilename: function(name) {
        name = String(name || '').trim();
        if (!name) throw new Error('Filename is required');
        if (!name.endsWith('.json')) name += '.json';
        return name;
    },

    fillEmptyImageExample: function() {
        var name = document.getElementById('wf-save-name');
        var draft = document.getElementById('wf-json-draft');
        if (name && !name.value) name.value = 'empty-image-sample.json';
        if (!draft) return;
        draft.value = JSON.stringify({
            "1": { "class_type": "EmptyImage", "inputs": { "width": 768, "height": 432, "batch_size": 1, "color": 2368548 }, "_meta": { "title": "Solid color source" } },
            "2": { "class_type": "SaveImage", "inputs": { "images": ["1", 0], "filename_prefix": "omni_empty_image" }, "_meta": { "title": "Save image" } }
        }, null, 2);
    },

    importFile: async function() {
        var input = document.getElementById('wf-file');
        var overwrite = document.getElementById('wf-overwrite');
        if (!input || !input.files || !input.files.length) return App.toast('Choose a workflow JSON file first', 'error');
        var form = new FormData();
        form.append('file', input.files[0]);
        var path = '/api/workflows/import?overwrite=' + encodeURIComponent(overwrite && overwrite.checked ? 'true' : 'false');
        var opts = { method: 'POST', body: form, headers: {} };
        if (App.state.sessionToken) opts.headers['X-Omni-Token'] = App.state.sessionToken;
        try {
            var resp = await fetch(path, opts);
            if (!resp.ok) throw new Error(resp.status + ': ' + await resp.text());
            var data = await resp.json();
            App.toast('Imported ' + (data.name || 'workflow') + '; checking requirements', 'success');
            await this.load();
        } catch (e) { App.toast('Import failed: ' + e.message, 'error'); }
    },

    check: async function(filename, btn) {
        if (btn) { btn.disabled = true; btn.textContent = 'Checking...'; }
        try {
            var qs = this.targetInstance() ? '?instance_id=' + encodeURIComponent(this.targetInstance()) : '';
            this.analyses[filename] = await App.api('GET', '/api/workflows/' + encodeURIComponent(filename) + '/requirements' + qs);
            this.render();
        } catch (e) { App.toast('Check failed: ' + e.message, 'error'); if (btn) btn.disabled = false; }
    },

    planWorkflow: async function(filename, btn, context) {
        var self = this;
        try {
        context = context || {instance_id: this.targetInstance(), placement: this.placementPolicy(filename), workflow: filename ? null : this.parseDraft()};
        if (!context.instance_id) {
            App.toast('Start ComfyUI before planning GPU placement', 'error');
            return null;
        }
        if (btn) { btn.disabled = true; btn.textContent = 'Planning...'; }
            var workflow = filename
                ? await App.api('GET', '/api/workflows/' + encodeURIComponent(filename))
                : context.workflow;
            var body = {
                workflow: workflow,
                filename: filename || 'inline-workflow.json',
                instance_id: context.instance_id,
                placement: context.placement
            };
            var report = await App.api('POST', '/api/workflows/analyze', body);
            if (filename) {
                this.analyses[filename] = report;
                this.render();
            } else {
                this.inlineReport = report;
                var target = document.getElementById('wf-draft-analysis');
                if (target) {
                    target.innerHTML = this.renderReport(report, '');
                    target.querySelectorAll('[data-wf-placement-override]').forEach(function(select) {
                        select.addEventListener('change', function() {
                            self.placementOverrides.__draft__ = self.placementOverrides.__draft__ || {};
                            if (this.value) self.placementOverrides.__draft__[this.dataset.component] = this.value;
                            else delete self.placementOverrides.__draft__[this.dataset.component];
                            self.placementMode = 'manual';
                self.placementDirty = true;
                            self.planWorkflow('', null);
                        });
                    });
                }
            }
            return report.placement_plan || null;
        } catch (e) {
            App.toast('GPU plan failed: ' + e.message, 'error');
            if (btn) { btn.disabled = false; btn.textContent = 'Plan GPUs'; }
            return null;
        }
    },

    placementConfirmation: function(plan) {
        if (!plan || !plan.valid) return false;
        var lines = ['Run with this GPU plan?'];
        (plan.components || []).forEach(function(item) {
            var device = item.device || {};
            lines.push(item.role + ': ' + (item.model_name || item.component_id) + ' -> ' + (device.name || device.target));
        });
        if (!(plan.components || []).length) lines.push('No independently placeable model components were found; the graph will run unchanged.');
        return confirm(lines.join('\n'));
    },

    savePlacementPolicy: async function(filename, btn) {
        var workflow = (App.state.workflows || []).filter(function(item) { return item.filename === filename; })[0] || {};
        if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
        try {
            await App.api('PUT', '/api/workflows/' + encodeURIComponent(filename) + '/metadata', {
                tags: workflow.tags || [],
                description: workflow.description || '',
                placement_policy: this.placementPolicy(filename)
            });
            App.toast('GPU policy saved separately from the Comfy workflow', 'success');
            await this.load();
        } catch (e) {
            App.toast('GPU policy could not be saved: ' + e.message, 'error');
            if (btn) { btn.disabled = false; btn.textContent = 'Save as workflow default'; }
        }
    },

    analyzeDraft: async function() {
        try {
            var body = { workflow: this.parseDraft(), filename: ((document.getElementById('wf-save-name') || {}).value || 'inline-workflow.json'), instance_id: this.targetInstance() || null };
            if (this.targetInstance()) body.placement = this.placementPolicy('');
            this.inlineReport = await App.api('POST', '/api/workflows/analyze', body);
            var target = document.getElementById('wf-draft-analysis');
            if (target) target.innerHTML = this.renderReport(this.inlineReport, '');
        } catch (e) { App.toast('Draft check failed: ' + e.message, 'error'); }
    },

    installMissing: async function(filename, btn) {
        var report = this.analyses[filename];
        var items = report && report.models ? report.models.downloadable || [] : [];
        if (!items.length) return App.toast('No verified model downloads are available', 'info');
        if (btn) { btn.disabled = true; btn.textContent = 'Queueing...'; }
        var queued = 0, failed = 0;
        for (var item of items) {
            try { await App.api('POST', item.install.path, item.install.body); queued++; }
            catch (_) { failed++; }
        }
        App.toast(queued + ' model download' + (queued === 1 ? '' : 's') + ' queued', queued ? 'success' : 'error');
        if (failed) App.toast(failed + ' download' + (failed === 1 ? '' : 's') + ' could not be queued', 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'Downloads queued'; }
        if (typeof TabModels !== 'undefined') { App.loadInstallJobs(); TabModels.renderInstallJobs(); }
    },

    findModel: async function(filename, index, btn) {
        var report = this.analyses[filename];
        var model = report && report.models && (report.models.missing || [])[index];
        if (!model) return;
        if (btn) { btn.disabled = true; btn.textContent = 'Searching...'; }
        try {
            var q = encodeURIComponent((model.search || {}).query || model.name);
            var cat = encodeURIComponent(model.category);
            var local = await App.api('GET', '/api/registry/comfy/manager-models/search?q=' + q + '&category=' + cat + '&limit=10');
            var candidates = (local.models || []).filter(function(item) {
                return /^https:\/\/huggingface\.co\/.+\/(resolve|blob)\//.test(item.url || '');
            });
            if (!candidates.length) {
                var remote = await App.api('GET', '/api/registry/comfy/models/search?q=' + q + '&category=' + cat + '&repo_limit=6&file_limit=12');
                candidates = remote.candidates || [];
            }
            this.showModelCandidates(model, candidates);
        } catch (e) { App.toast('Model search failed: ' + e.message, 'error'); }
        finally { if (btn) { btn.disabled = false; btn.textContent = 'Find source'; } }
    },

    findUnknownModel: async function(filename, index, btn) {
        var report = filename ? this.analyses[filename] : this.inlineReport;
        var item = report && report.models && (report.models.unclassified_inputs || [])[index];
        if (!item) return;
        if (btn) { btn.disabled = true; btn.textContent = 'Searching...'; }
        try {
            var q = encodeURIComponent((item.search || {}).query || item.name);
            var results = await Promise.all([
                App.api('GET', '/api/registry/comfy/installed-models/search?q=' + q + '&limit=20'),
                App.api('GET', '/api/registry/comfy/manager-models/search?q=' + q + '&limit=20'),
            ]);
            var installed = results[0].models || item.installed_matches || [];
            var candidates = results[1].models || item.manager_candidates || [];
            if (!installed.length && !candidates.length) {
                var remote = await App.api('GET', '/api/registry/comfy/models/search?q=' + q + '&repo_limit=6&file_limit=20');
                candidates = remote.candidates || [];
            }
            this.showUnknownModelCandidates(item, installed, candidates);
        } catch (e) { App.toast('Cross-folder model search failed: ' + e.message, 'error'); }
        finally { if (btn) { btn.disabled = false; btn.textContent = 'Search all folders'; } }
    },

    showUnknownModelCandidates: function(model, installed, candidates) {
        var E = App.esc;
        var allowed = {};
        ['checkpoints', 'diffusion_models', 'vae', 'clip', 'text_encoders', 'loras', 'controlnet', 'gguf', 'unet', 'embeddings', 'upscale_models', 'latent_upscale_models', 'clip_vision', 'model_patches', 'style_models', 'audio_encoders', 'diffusers', 'configs', 'gligen', 'hypernetworks', 'vae_approx', 'frame_interpolation', 'photomaker', 'background_removal', 'detection', 'geometry_estimation', 'optical_flow'].forEach(function(value) { allowed[value] = true; });
        var installedKeys = {};
        installed.forEach(function(item) {
            installedKeys[(item.category || '') + ':' + String(item.name || '').toLowerCase()] = true;
        });
        var usable = candidates.map(function(item) {
            var rawCategory = item.category_root || item.category || '';
            var category = String(rawCategory).replace(/\\/g, '/').split('/')[0];
            return Object.assign({}, item, { resolvedCategory: allowed[category] ? category : '' });
        }).filter(function(item) {
            var name = item.file || item.filename || item.name || '';
            var alreadyInstalled = !!item.installed || !!installedKeys[item.resolvedCategory + ':' + String(name).toLowerCase()];
            return !alreadyInstalled && item.url && /^https:\/\/huggingface\.co\//.test(item.url);
        });
        var html = '<div class="workflow-candidate-list">';
        for (var i = 0; i < installed.length; i++) {
            html += '<div><span><strong>' + E(installed[i].name || model.name) + '</strong><small>' + E(installed[i].category || '') + '</small></span><span class="badge badge-green">installed</span></div>';
        }
        if (!installed.length && !usable.length) html += '<p>No exact local, Manager, or Hugging Face weight files matched. Use the Comfy models page to paste a direct link and choose its folder.</p>';
        for (var c = 0; c < usable.length; c++) {
            var candidate = usable[c];
            html += '<div><span><strong>' + E(candidate.file || candidate.filename || candidate.name) + '</strong><small>' + E(candidate.resolvedCategory || 'folder needs review') + ' - ' + E(candidate.repo || candidate.reference || candidate.source || '') + '</small></span>';
            if (candidate.resolvedCategory) html += '<button class="btn btn-primary btn-sm" data-unknown-candidate-index="' + c + '">Queue download</button>';
            else html += '<span class="badge badge-orange">review folder</span>';
            html += '</div>';
        }
        html += '</div>';
        App.showTextModal('Find ' + model.name + ' across all Comfy folders', html, { html: true });
        document.querySelectorAll('#modal-backdrop [data-unknown-candidate-index]').forEach(function(button) {
            button.addEventListener('click', async function() {
                var item = usable[Number(button.dataset.unknownCandidateIndex)];
                button.disabled = true;
                try {
                    await App.api('POST', '/api/assets/comfy/' + encodeURIComponent(item.resolvedCategory) + '/install-url', { url: item.url, name: model.name.split('/').pop() });
                    button.textContent = 'Queued';
                    App.toast('Model download queued', 'success');
                } catch (e) { button.disabled = false; App.toast('Download could not be queued: ' + e.message, 'error'); }
            });
        });
    },

    showModelCandidates: function(model, candidates) {
        var E = App.esc;
        var usable = candidates.filter(function(item) {
            return item.url && /^https:\/\/huggingface\.co\//.test(item.url);
        });
        var html = '<div class="workflow-candidate-list">';
        if (!usable.length) html += '<p>No installable Hugging Face files matched. Try the model filename or repository on the Models page.</p>';
        for (var i = 0; i < usable.length; i++) {
            var item = usable[i];
            html += '<div><span><strong>' + E(item.file || item.filename || item.name) + '</strong><small>' + E(item.repo || item.reference || item.source || '') + '</small></span>';
            html += '<button class="btn btn-primary btn-sm" data-candidate-index="' + i + '">Queue download</button></div>';
        }
        html += '</div>';
        App.showTextModal('Find ' + model.name, html, { html: true });
        var buttons = document.querySelectorAll('#modal-backdrop [data-candidate-index]');
        buttons.forEach(function(button) {
            button.addEventListener('click', async function() {
                var item = usable[Number(button.dataset.candidateIndex)];
                button.disabled = true;
                try {
                    await App.api('POST', '/api/assets/comfy/' + encodeURIComponent(model.category) + '/install-url', { url: item.url, name: model.name.split('/').pop() });
                    button.textContent = 'Queued';
                    App.toast('Model download queued', 'success');
                } catch (e) { button.disabled = false; App.toast('Download could not be queued: ' + e.message, 'error'); }
            });
        });
    },

    searchTemplates: async function() {
        var input = document.getElementById('wf-template-query');
        var query = input ? input.value.trim() : '';
        if (!query) return App.toast('Enter what you want the workflow to do', 'error');
        this.templateSearching = true;
        var button = document.getElementById('wf-template-search');
        if (button) { button.disabled = true; button.textContent = 'Searching...'; }
        try {
            var data = await App.api('GET', '/api/workflows/search?q=' + encodeURIComponent(query) + '&scope=templates&limit=30');
            this.templateResults = data.results || [];
            var target = document.getElementById('wf-template-results');
            if (target) target.innerHTML = this.renderTemplateResults() || '<div class="empty-state">No matching installed templates.</div>';
        } catch (e) { App.toast('Template search failed: ' + e.message, 'error'); }
        finally { this.templateSearching = false; if (button) { button.disabled = false; button.textContent = 'Search templates'; } }
    },

    loadTemplate: async function(btn) {
        try {
            var path = '/api/workflow-templates/' + encodeURIComponent(btn.dataset.templateId);
            var params = [];
            if (btn.dataset.templateSource) params.push('source=' + encodeURIComponent(btn.dataset.templateSource));
            if (btn.dataset.templatePackage) params.push('package=' + encodeURIComponent(btn.dataset.templatePackage));
            if (params.length) path += '?' + params.join('&');
            var data = await App.api('GET', path);
            var draft = document.getElementById('wf-json-draft');
            var name = document.getElementById('wf-save-name');
            if (draft) draft.value = JSON.stringify(data.workflow, null, 2);
            if (name) name.value = (data.template.filename || data.template.id + '.json').replace(/[^A-Za-z0-9._-]+/g, '_');
            this.validateDraft();
            if (draft) draft.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } catch (e) { App.toast('Template could not be opened: ' + e.message, 'error'); }
    },

    saveDraft: async function() {
        try {
            var name = this.normalizeFilename((document.getElementById('wf-save-name') || {}).value);
            var workflow = this.parseDraft();
            await App.api('PUT', '/api/workflows/' + encodeURIComponent(name), {
                workflow: workflow,
                overwrite: !!((document.getElementById('wf-overwrite') || {}).checked),
                placement_policy: this.targetInstance() ? this.placementPolicy('') : null
            });
            App.toast('Saved ' + name + '; checking requirements', 'success');
            await this.load();
        } catch (e) { App.toast('Save failed: ' + e.message, 'error'); }
    },

    runDraft: async function() {
        if (!this.targetInstance()) return App.toast('Start ComfyUI first', 'error');
        if (this.runPending) return;
        this.runPending = true;
        try {
            var context = {instance_id: this.targetInstance(), placement: this.placementPolicy(''), workflow: this.parseDraft()};
            var plan = await this.planWorkflow('', null, context);
            if (!plan || !plan.valid) return App.toast('Run blocked by the GPU placement plan', 'error');
            if (!this.placementConfirmation(plan)) return;
            var data = await App.api('POST', '/api/workflows/run', {
                workflow: context.workflow, instance_id: context.instance_id, client_id: 'omni-ui',
                placement: context.placement
            });
            App.toast('Workflow queued (prompt ' + (data.prompt_id || 'accepted') + ')', 'success');
        } catch (e) { App.toast('Run blocked: check workflow requirements. ' + e.message, 'error'); }
        finally { this.runPending = false; }
    },

    queue: async function(filename) {
        if (this.runPending) return;
        this.runPending = true;
        try {
            var context = {instance_id: this.targetInstance(), placement: this.placementPolicy(filename), workflow: null};
            var plan = await this.planWorkflow(filename, null, context);
            if (!plan || !plan.valid) return App.toast('Run blocked by the GPU placement plan', 'error');
            if (!this.placementConfirmation(plan)) return;
            var data = await App.api('POST', '/api/workflows/' + encodeURIComponent(filename) + '/run', {
                params: {}, instance_id: context.instance_id, client_id: 'omni-ui',
                placement: context.placement
            });
            App.toast('Workflow queued (prompt ' + (data.prompt_id || 'accepted') + ')', 'success');
            App.toast('Results will appear in Media.', 'info');
        } catch (e) { App.toast('Run blocked: check workflow requirements. ' + e.message, 'error'); }
        finally { this.runPending = false; }
    },

    viewJson: async function(filename) {
        try {
            var data = await App.api('GET', '/api/workflows/' + encodeURIComponent(filename));
            var advanced = document.getElementById('wf-advanced');
            if (advanced) advanced.open = true;
            var draft = document.getElementById('wf-json-draft');
            var name = document.getElementById('wf-save-name');
            if (draft) draft.value = JSON.stringify(data, null, 2);
            if (name) name.value = filename;
            this.validateDraft();
            if (draft) { draft.scrollIntoView({ behavior: 'smooth', block: 'center' }); draft.focus(); }
        } catch (e) { App.toast('Workflow could not be opened: ' + e.message, 'error'); }
    },

    delete: async function(filename) {
        if (!confirm('Delete workflow "' + filename + '"? This cannot be undone.')) return;
        try { await App.api('DELETE', '/api/workflows/' + encodeURIComponent(filename)); App.toast('Workflow deleted', 'success'); await this.load(); }
        catch (e) { App.toast('Delete failed: ' + e.message, 'error'); }
    }
};
