/* Standalone MiniMax Music 3 workspace. */
var TabMiniMaxMusic3 = {
    status: null,
    state: { running: false },
    busy: false,
    workers: [],
    workerId: '',
    activeWorkerId: '',
    refreshGeneration: 0,

    init: function() {
        this.render();
        this.refresh();
        if (!App.state.devices || !App.state.devices.length) App.loadDevices().then(this.render.bind(this));
    },

    onActivate: function() { this.refresh(); },

    render: function() {
        var el = document.getElementById('tab-minimax-music3');
        if (!el) return;
        var draft = {};
        el.querySelectorAll('input, textarea, select').forEach(function(input) {
            if (input.id) draft[input.id] = {value: input.value, checked: input.checked};
        });
        var previousResult = document.getElementById('mm3-result');
        var resultHtml = previousResult ? previousResult.innerHTML : '';
        var variant = this.status && this.status.variants && this.status.variants[0];
        var installed = !!(variant && variant.installed);
        var runtimeReady = !!(this.status && this.status.runtime_ready);
        var readyToLoad = installed && runtimeReady;
        var loaded = !!(this.state && this.state.model_loaded);
        var devices = (App.state.devices || []).filter(function(d) {
            return String(d.id || d.device || '').indexOf('cuda') === 0;
        });
        var deviceOptions = devices.map(function(d) {
            var id = d.id || d.device;
            var label = d.name ? id + ' — ' + d.name : id;
            return '<option value="' + App.esc(id) + '">' + App.esc(label) + '</option>';
        }).join('');
        if (!deviceOptions) deviceOptions = '<option value="">No CUDA device available</option>';
        var workerOptions = '<option value="">No selected worker</option>' + this.workers.map(function(w) {
            return '<option value="' + App.esc(w.worker_id) + '">' + App.esc(w.worker_id + ' — ' + w.device) + '</option>';
        }).join('');

        el.innerHTML = ''
            + '<div class="page-heading"><div class="page-heading-copy">'
            + '<div class="page-eyebrow">Standalone local music engine</div>'
            + '<h1>MiniMax Music 3</h1>'
            + '<p>Generate structured songs from lyrics and a detailed music description. Native output is 44.1 kHz stereo; requested duration can be up to 360 seconds.</p>'
            + '</div><div class="next-step-actions">'
            + '<span class="status-badge ' + (loaded ? 'ok' : '') + '">' + (loaded ? 'Loaded' : readyToLoad ? 'Installed' : installed ? 'Runtime incomplete' : 'Not installed') + '</span>'
            + '<button class="btn" type="button" onclick="TabMiniMaxMusic3.refresh()">Refresh</button>'
            + '</div></div>'
            + '<div class="setup-grid">'
            + '<section class="setup-card"><h2>Engine</h2>'
            + '<div class="form-group"><label for="mm3-worker">Worker</label><select id="mm3-worker" ' + (this.busy ? 'disabled' : '') + ' onchange="TabMiniMaxMusic3.selectWorker(this.value)">' + workerOptions + '</select></div>'
            + '<p>' + (readyToLoad ? 'Official Diffusers subset and isolated runtime are installed.' : installed ? 'Weights are installed, but the isolated runtime needs installation or repair.' : 'The selective installer downloads the official Diffusers components only (about 28 GB), not the duplicate SGLang weights.') + '</p>'
            + '<div class="form-group"><label for="mm3-device">GPU</label><select id="mm3-device">' + deviceOptions + '</select></div>'
            + '<label class="check-row"><input id="mm3-offload" type="checkbox" checked> CPU offload (recommended for a 24 GB GPU)</label>'
            + '<div class="form-group"><label for="mm3-cpu-budget">CPU offload estimate (MB)</label><input id="mm3-cpu-budget" type="number" min="0" step="256" value="16000"><small>Used for load admission. This is not a per-worker memory ceiling; the shared workload limit applies at runtime.</small></div>'
            + '<div class="next-step-actions">'
            + (!readyToLoad ? '<button class="btn btn-primary" type="button" onclick="TabMiniMaxMusic3.install()">' + (installed ? 'Repair runtime' : 'Install official model') + '</button>' : '')
            + (readyToLoad && !loaded ? '<button class="btn btn-primary" type="button" onclick="TabMiniMaxMusic3.load()">Load engine</button>' : '')
            + (loaded ? '<button class="btn" type="button" onclick="TabMiniMaxMusic3.unload()">Unload model</button>' : '')
            + '</div></section>'
            + '<section class="setup-card"><h2>Generate</h2>'
            + '<div class="form-group"><label for="mm3-prompt">Music description</label><textarea id="mm3-prompt" rows="6" placeholder="Genre: minimal techno. BPM: 126. Key: D minor. Hypnotic, spacious and gradually intensifying. Vocals: ... Arrangement: ..."></textarea></div>'
            + '<div class="form-group"><label for="mm3-lyrics">Lyrics</label><textarea id="mm3-lyrics" rows="10" placeholder="[intro]\n(instrumental)\n\n[verse]\nLyrics here...\n\n[chorus]\n..."></textarea><small>Put each structure tag on its own line. For instrumental music, lyrics must still contain a structure such as [intro] followed by (instrumental).</small></div>'
            + '<div class="form-grid">'
            + '<div class="form-group"><label for="mm3-duration">Maximum duration (seconds)</label><input id="mm3-duration" type="number" min="1" max="360" step="1" value="60"></div>'
            + '<div class="form-group"><label for="mm3-seed">Seed</label><input id="mm3-seed" type="number" min="0" step="1" value="0"></div>'
            + '</div><div class="next-step-actions">'
            + '<button class="btn btn-primary" type="button" ' + (!loaded || this.busy ? 'disabled' : '') + ' onclick="TabMiniMaxMusic3.generate()">' + (this.busy ? 'Generating…' : 'Generate song') + '</button>'
            + '<button class="btn btn-danger" type="button" ' + (!this.busy ? 'disabled' : '') + ' onclick="TabMiniMaxMusic3.cancel()">Cancel and unload</button>'
            + '</div><div id="mm3-result"></div></section></div>'
            + '<section class="setup-card"><h2>Model terms and controls</h2>'
            + '<p>This product identifies the engine as <strong>MiniMax-Music3</strong>. The official checkpoint uses the MiniMax-Music3 Community License; commercial operators must review its attribution, acceptable-use, safeguard, and revenue authorization terms.</p>'
            + '<p>MiniMax Music 3 requires both lyrics and a structured caption. It does not expose reference audio, voice cloning, TTS voices, streaming, or a verified LoRA attachment contract in this integration.</p></section>';
        Object.keys(draft).forEach(function(id) {
            var input = document.getElementById(id);
            if (input && id !== 'mm3-worker') { input.value = draft[id].value; input.checked = draft[id].checked; }
        });
        document.getElementById('mm3-worker').value = this.workerId;
        document.getElementById('mm3-result').innerHTML = resultHtml;
    },

    selectWorker: function(id) {
        if (this.busy) return;
        this.workerId = id;
        this.state = {running: false};
        this.refresh();
    },

    workerQuery: function(id) { return id ? '?worker_id=' + encodeURIComponent(id) : ''; },

    refresh: async function() {
        var generation = ++this.refreshGeneration;
        try {
            var values = await Promise.all([
                App.api('GET', '/api/minimax_music3/status'),
                App.api('GET', '/api/workers')
            ]);
            if (generation !== this.refreshGeneration) return;
            this.status = values[0];
            this.workers = (values[1].workers || []).filter(function(w) { return w.model === 'minimax_music3' && w.status !== 'dead'; });
            var selected = this.workerId;
            if (!this.workers.some(function(w) { return w.worker_id === selected; })) this.workerId = this.workers.length ? this.workers[0].worker_id : '';
            var state = this.workerId ? await App.api('GET', '/api/minimax_music3/state?autospawn=false&worker_id=' + encodeURIComponent(this.workerId)) : {running: false};
            if (generation !== this.refreshGeneration) return;
            this.state = state;
            this.render();
        } catch (err) { App.toast('Music 3 status failed: ' + err.message, 'error'); }
    },

    install: async function() {
        try {
            var result = await App.api('POST', '/api/setup/install-variant', {
                model: 'minimax_music3', variant_id: 'official-diffusers'
            });
            App.toast('Music 3 install started: ' + result.job_id, 'success');
            App.loadInstallJobs();
        } catch (err) { App.toast('Install failed: ' + err.message, 'error'); }
    },

    load: async function() {
        var device = document.getElementById('mm3-device').value;
        var cpuOffload = document.getElementById('mm3-offload').checked;
        var cpuBudget = Number(document.getElementById('mm3-cpu-budget').value);
        if (!device) { App.toast('Select a detected CUDA device', 'error'); return; }
        var selectedId = this.workerId;
        var selected = this.workers.find(function(w) { return w.worker_id === selectedId && w.device === device; });
        try {
            App.toast('Loading MiniMax Music 3…', 'info');
            this.state = await App.api('POST', '/api/minimax_music3/load', {
                model_variant: 'official-diffusers', device: device,
                bf16: true, cpu_offload: cpuOffload,
                worker_id: selected ? selected.worker_id : null,
                cpu_memory_mb: cpuBudget, reserve_mb: 1024
            });
            this.workerId = this.state.worker_id || '';
            await this.refresh();
            this.render();
            App.toast('MiniMax Music 3 loaded', 'success');
        } catch (err) { App.toast('Load failed: ' + err.message, 'error'); }
    },

    unload: async function() {
        try {
            await App.api('POST', '/api/minimax_music3/unload' + this.workerQuery(this.workerId));
            await this.refresh();
            App.toast('Music 3 model unloaded', 'success');
        } catch (err) { App.toast('Unload failed: ' + err.message, 'error'); }
    },

    generate: async function() {
        var prompt = document.getElementById('mm3-prompt').value.trim();
        var lyrics = document.getElementById('mm3-lyrics').value.trim();
        var duration = Number(document.getElementById('mm3-duration').value);
        var seed = Number(document.getElementById('mm3-seed').value);
        if (!prompt || !lyrics) { App.toast('Music description and lyrics are required', 'error'); return; }
        this.busy = true;
        var workerId = this.workerId;
        this.activeWorkerId = workerId;
        this.render();
        try {
            var result = await App.api('POST', '/api/minimax_music3/generate', {
                prompt: prompt, lyrics: lyrics,
                duration_s: duration, seed: seed, autospawn: false, worker_id: workerId || null
            });
            this.busy = false;
            this.activeWorkerId = '';
            this.render();
            var box = document.getElementById('mm3-result');
            if (box) box.innerHTML = '<audio controls autoplay src="' + App.esc(result.results[0].url) + '"></audio><p>Saved to Media library as job ' + App.esc(result.job_id) + '.</p>';
        } catch (err) {
            this.busy = false;
            this.activeWorkerId = '';
            this.render();
            App.toast('Generation failed: ' + err.message, 'error');
        }
    },

    cancel: async function() {
        try {
            await App.api('POST', '/api/minimax_music3/cancel' + this.workerQuery(this.activeWorkerId || this.workerId));
            this.busy = false;
            await this.refresh();
            App.toast('Generation cancelled; worker and model were unloaded', 'success');
        } catch (err) { App.toast('Cancel failed: ' + err.message, 'error'); }
    }
};
