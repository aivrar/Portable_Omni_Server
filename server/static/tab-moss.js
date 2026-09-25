/* ==========================================================================
   Tab: MOSS - TTS and sound effects
   ========================================================================== */

var TabMoss = {
    ttsBusy: false,
    sfxBusy: false,
    ttsAudioUrl: null,
    sfxAudioUrl: null,

    init: function() {
        this.render();
    },

    onActivate: function() {
        App.loadSetupStatus();
        App.refreshWorkers().then(function() { TabMoss.render(); });
    },

    _workerFor: function(model) {
        var workers = App.state.workers || [];
        for (var i = 0; i < workers.length; i++) {
            if (workers[i].model === model && workers[i].status !== 'dead') return workers[i];
        }
        return null;
    },

    _headers: function(json) {
        var h = {};
        if (json) h['Content-Type'] = 'application/json';
        if (App.state.sessionToken) h['X-Omni-Token'] = App.state.sessionToken;
        return h;
    },

    _postAudio: async function(path, body) {
        var resp = await fetch(path, {
            method: 'POST',
            headers: this._headers(true),
            body: JSON.stringify(body || {}),
        });
        if (!resp.ok) {
            var txt = await resp.text();
            throw new Error(resp.status + ': ' + txt);
        }
        return resp.blob();
    },

    install: async function(model, btn) {
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Installing...';
        }
        try {
            await App.api('POST', '/api/setup/install/' + model);
            App.toast('MOSS install started. Watch Download Jobs on the Models tab.', 'info');
            await App.loadInstallJobs();
            this.render();
        } catch (e) {
            App.toast('Install failed to start: ' + e.message, 'error');
            this.render();
        }
    },

    startWorker: async function(model, btn) {
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Starting...';
        }
        try {
            await App.spawnWorker(model);
            await App.refreshWorkers();
            this.render();
        } catch (e) {
            this.render();
        }
    },

    openModels: function() {
        var btn = document.querySelector('.tab-btn[data-tab="models"]');
        if (btn) btn.click();
    },

    openServer: function() {
        var btn = document.querySelector('.tab-btn[data-tab="server"]');
        if (btn) btn.click();
    },

    _statusBlock: function(model, label) {
        var E = App.esc;
        var installed = App.modelInstalled(model);
        var worker = this._workerFor(model);
        var ready = worker && worker.status === 'ready';
        var html = '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">';
        html += '<strong>' + E(label) + '</strong>';
        html += installed
            ? '<span class="badge badge-green">installed</span>'
            : '<span class="badge badge-gray">not installed</span>';
        html += ready
            ? '<span class="badge badge-green">worker ready</span>'
            : (worker
                ? '<span class="badge badge-orange">worker ' + E(worker.status) + '</span>'
                : '<span class="badge badge-gray">no worker</span>');
        html += '<span style="flex:1"></span>';
        if (!installed) {
            html += '<button class="btn btn-primary btn-sm" data-install-model="' + E(model) + '">Install</button>';
        } else if (!ready) {
            html += '<button class="btn btn-primary btn-sm" data-start-model="' + E(model) + '">Start worker</button>';
        }
        html += '<button class="btn btn-sm" data-open-server>Server</button>';
        html += '</div>';
        return html;
    },

    render: function() {
        if (this.ttsBusy || this.sfxBusy) return;
        var el = document.getElementById('tab-moss');
        if (!el) return;
        var E = App.esc;
        var ttsReady = !!(this._workerFor('moss_tts') || {}).status && App.workerReadyFor('moss_tts');
        var sfxReady = !!(this._workerFor('moss_sfx') || {}).status && App.workerReadyFor('moss_sfx');

        var html = '';
        html += '<div class="card"><h2>MOSS Speech + SFX</h2>';
        html += '<div style="display:flex;gap:8px;flex-wrap:wrap">';
        html += '<button class="btn btn-sm" id="moss-refresh">Refresh</button>';
        html += '<button class="btn btn-sm" id="moss-open-models">Open Models</button>';
        html += '</div></div>';

        html += '<div class="card mt-16">';
        html += this._statusBlock('moss_tts', 'MOSS-TTS Local v1.5');
        if (!App.modelInstalled('moss_tts')) {
            html += App.prereqBanner({
                level: 'warn',
                text: 'Install MOSS-TTS to download the model, paired audio tokenizer, source checkout, and isolated runtime.',
            });
        } else if (!ttsReady) {
            html += App.prereqBanner({
                level: 'warn',
                text: 'Start a MOSS-TTS worker before generating speech.',
            });
        }
        html += App.field({
            type: 'textarea',
            id: 'moss-tts-text',
            label: 'Speech text',
            rows: 4,
            value: 'We stand on the threshold of the AI era, where intelligence becomes an extension of human creativity.',
        });
        html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px">';
        html += App.field({ type: 'text', id: 'moss-tts-language', label: 'Language', value: 'English' });
        html += App.field({ type: 'number', id: 'moss-tts-seed', label: 'Seed', value: 1234, step: 1 });
        html += App.field({ type: 'number', id: 'moss-tts-frames', label: 'Max frames', value: 2048, min: 1, max: 7500, step: 1 });
        html += App.field({ type: 'number', id: 'moss-tts-temp', label: 'Temperature', value: 1.7, min: 0.1, max: 3.0, step: 0.1 });
        html += '</div>';
        html += '<button class="btn btn-primary mt-16" id="moss-tts-generate"' + (!ttsReady ? ' disabled' : '') + '>Generate speech</button>';
        html += '<div id="moss-tts-result" class="mt-16">';
        if (this.ttsAudioUrl) html += '<audio controls src="' + E(this.ttsAudioUrl) + '" style="width:100%"></audio>';
        html += '</div></div>';

        html += '<div class="card mt-16">';
        html += this._statusBlock('moss_sfx', 'MOSS-SoundEffect v2.0');
        if (!App.modelInstalled('moss_sfx')) {
            html += App.prereqBanner({
                level: 'warn',
                text: 'Install MOSS-SoundEffect to download the SFX model, source checkout, and isolated runtime.',
            });
        } else if (!sfxReady) {
            html += App.prereqBanner({
                level: 'warn',
                text: 'Start a MOSS-SoundEffect worker before generating SFX.',
            });
        }
        html += App.field({
            type: 'textarea',
            id: 'moss-sfx-prompt',
            label: 'SFX prompt',
            rows: 4,
            value: 'The crisp rhythmic click-clack of fast typing on a mechanical keyboard.',
        });
        html += '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px">';
        html += App.field({ type: 'number', id: 'moss-sfx-seconds', label: 'Seconds', value: 10, min: 0.1, max: 30, step: 0.1 });
        html += App.field({ type: 'number', id: 'moss-sfx-steps', label: 'Steps', value: 100, min: 1, max: 200, step: 1 });
        html += App.field({ type: 'number', id: 'moss-sfx-cfg', label: 'CFG', value: 4.0, min: 0, max: 20, step: 0.1 });
        html += App.field({ type: 'number', id: 'moss-sfx-sigma', label: 'Sigma shift', value: 5.0, min: 0, max: 20, step: 0.1 });
        html += App.field({ type: 'number', id: 'moss-sfx-seed', label: 'Seed', value: 0, step: 1 });
        html += '</div>';
        html += '<button class="btn btn-primary mt-16" id="moss-sfx-generate"' + (!sfxReady ? ' disabled' : '') + '>Generate SFX</button>';
        html += '<div id="moss-sfx-result" class="mt-16">';
        if (this.sfxAudioUrl) html += '<audio controls src="' + E(this.sfxAudioUrl) + '" style="width:100%"></audio>';
        html += '</div></div>';

        el.innerHTML = html;
        this.bind();
    },

    bind: function() {
        var self = this;
        var refresh = document.getElementById('moss-refresh');
        if (refresh) refresh.addEventListener('click', function() {
            App.loadSetupStatus();
            App.refreshWorkers().then(function() { self.render(); });
        });
        var models = document.getElementById('moss-open-models');
        if (models) models.addEventListener('click', function() { self.openModels(); });
        document.querySelectorAll('[data-open-server]').forEach(function(btn) {
            btn.addEventListener('click', function() { self.openServer(); });
        });
        document.querySelectorAll('[data-install-model]').forEach(function(btn) {
            btn.addEventListener('click', function() { self.install(btn.dataset.installModel, btn); });
        });
        document.querySelectorAll('[data-start-model]').forEach(function(btn) {
            btn.addEventListener('click', function() { self.startWorker(btn.dataset.startModel, btn); });
        });
        var tts = document.getElementById('moss-tts-generate');
        if (tts) tts.addEventListener('click', function() { self.generateTts(this); });
        var sfx = document.getElementById('moss-sfx-generate');
        if (sfx) sfx.addEventListener('click', function() { self.generateSfx(this); });
    },

    generateTts: async function(btn) {
        this.ttsBusy = true;
        btn.disabled = true;
        btn.textContent = 'Generating...';
        try {
            var blob = await this._postAudio('/api/tts/moss_tts', {
                text: document.getElementById('moss-tts-text').value,
                response_format: 'wav',
                model_params: {
                    language: document.getElementById('moss-tts-language').value,
                    seed: Number(document.getElementById('moss-tts-seed').value),
                    max_new_frames: Number(document.getElementById('moss-tts-frames').value),
                    temperature: Number(document.getElementById('moss-tts-temp').value),
                },
            });
            if (this.ttsAudioUrl) URL.revokeObjectURL(this.ttsAudioUrl);
            this.ttsAudioUrl = URL.createObjectURL(blob);
        } catch (e) {
            App.toast('MOSS-TTS failed: ' + e.message, 'error');
        } finally {
            this.ttsBusy = false;
            this.render();
        }
    },

    generateSfx: async function(btn) {
        this.sfxBusy = true;
        btn.disabled = true;
        btn.textContent = 'Generating...';
        try {
            var blob = await this._postAudio('/api/moss/sfx', {
                prompt: document.getElementById('moss-sfx-prompt').value,
                seconds: Number(document.getElementById('moss-sfx-seconds').value),
                steps: Number(document.getElementById('moss-sfx-steps').value),
                cfg_scale: Number(document.getElementById('moss-sfx-cfg').value),
                sigma_shift: Number(document.getElementById('moss-sfx-sigma').value),
                seed: Number(document.getElementById('moss-sfx-seed').value),
            });
            if (this.sfxAudioUrl) URL.revokeObjectURL(this.sfxAudioUrl);
            this.sfxAudioUrl = URL.createObjectURL(blob);
        } catch (e) {
            App.toast('MOSS-SoundEffect failed: ' + e.message, 'error');
        } finally {
            this.sfxBusy = false;
            this.render();
        }
    },
};
