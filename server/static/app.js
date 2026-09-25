/* ==========================================================================
   Omni Studio — Main Application Controller
   ========================================================================== */

const App = {
    state: {
        workers: [],
        comfyInstances: [],
        models: {},
        variants: {},        // from /api/config: {model_id: [variant, ...]}
        loraCompatible: [],  // from /api/config: array of model_ids
        loras: [],           // from /api/loras: installed LoRA adapters
        setupStatus: {},
        devices: [],
        workflows: [],
        comfyModels: {},
        logs: [],
        installJobs: [],
        dismissedInstallFailureJobId: null,
        sessionToken: null,
        sessionInfo: null,   // full /api/session blob: token, auth_mode, api_base_url, openai_aliases
        connected: false,
        configLoaded: false,
        shuttingDown: false,
        activeTab: 'setup',
    },

    MAX_LOGS: 500,
    POLL_INTERVAL: 8000,
    _initializedTabs: {},

    // HTML escape to prevent XSS when inserting API data into innerHTML
    esc(s) {
        if (s == null) return '';
        return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
    },

    async api(method, path, body) {
        const opts = { method, headers: {} };
        var pathOnly = path.split('?')[0];
        if (this.state.sessionToken && pathOnly !== '/api/session') {
            opts.headers['X-Omni-Token'] = this.state.sessionToken;
        }
        if (body) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        const resp = await fetch(path, opts);
        if (!resp.ok) {
            if (resp.status === 401 && pathOnly !== '/api/session') {
                // The gateway may have regenerated its local session after a
                // recovery. Force the next poll to reacquire it.
                this.state.sessionToken = null;
                this.state.sessionInfo = null;
            }
            const err = await resp.text();
            throw new Error(resp.status + ': ' + err);
        }
        const ct = resp.headers.get('content-type') || '';
        if (ct.includes('application/json')) return resp.json();
        return null;
    },

    async initSession(opts) {
        opts = opts || {};
        try {
            var data = await this.api('GET', '/api/session');
            this.state.sessionToken = data && data.token ? data.token : null;
            this.state.sessionInfo = data || null;
            return true;
        } catch(err) {
            console.error('Failed to initialize local session:', err);
            if (!opts.silent) this.toast('Failed to initialize local session', 'error');
            return false;
        }
    },

    toast(message, type) {
        type = type || 'info';
        var container = document.getElementById('toast-container');
        var el = document.createElement('div');
        el.className = 'toast toast-' + type;
        el.textContent = message;
        el.style.cursor = 'pointer';
        el.addEventListener('click', function() { el.remove(); });
        container.appendChild(el);
        var timeout = type === 'error' ? 12000 : 4000;
        setTimeout(function() { el.remove(); }, timeout);
    },

    tabModule(tab) {
        var modules = {
            setup: window.TabSetup,
            server: window.TabServer,
            comfy: window.TabComfy,
            workflows: window.TabWorkflows,
            models: window.TabModels,
            chat: window.TabChat,
            media: window.TabMedia,
            tts: window.TabTTS,
            moss: window.TabMoss,
            'audio-lab': window.TabAudioLab,
            'ace-step': window.TabAceStep,
            'minimax-music3': window.TabMiniMaxMusic3,
            testing: window.TabTesting,
            log: window.TabLog,
        };
        return modules[tab];
    },

    isTabInitialized(tab) {
        return !!this._initializedTabs[tab];
    },

    ensureTabInitialized(tab) {
        if (this.isTabInitialized(tab)) return false;
        var mod = this.tabModule(tab);
        this._initializedTabs[tab] = true;
        if (!mod || typeof mod.init !== 'function') return true;
        try {
            mod.init();
        } catch (err) {
            console.error('Tab ' + tab + ' init failed:', err);
            this.toast(tab + ' failed to initialize', 'error');
        }
        return true;
    },

    activateTab(tab, options) {
        options = options || {};
        var btn = document.querySelector('.tab-btn[data-tab="' + tab + '"]');
        var panel = document.getElementById('tab-' + tab);
        if (!btn || !panel) return false;
        var previous = this.state.activeTab;
        document.querySelectorAll('.tab-btn').forEach(function(b) {
            var active = b === btn;
            b.classList.toggle('active', active);
            b.setAttribute('aria-selected', active ? 'true' : 'false');
            b.tabIndex = active ? 0 : -1;
        });
        document.querySelectorAll('.tab-content').forEach(function(section) {
            var active = section === panel;
            section.classList.toggle('active', active);
            section.hidden = !active;
        });
        this.state.activeTab = tab;
        var title = btn.dataset.title || btn.textContent.trim();
        var section = btn.dataset.section || 'Omni Studio';
        var titleEl = document.getElementById('page-title');
        var sectionEl = document.getElementById('page-section');
        if (titleEl) titleEl.textContent = title;
        if (sectionEl) sectionEl.textContent = section;
        document.title = title === 'Home' ? 'Omni Studio' : title + ' — Omni Studio';
        var advanced = btn.closest('.nav-advanced');
        if (advanced) advanced.open = true;

        var firstActivation = this.ensureTabInitialized(tab);
        var mod = this.tabModule(tab);
        if (firstActivation && ['server', 'comfy', 'testing', 'minimax-music3'].includes(tab)) {
            this.loadDevices();
        }
        if (!firstActivation && mod && typeof mod.onActivate === 'function') {
            try { mod.onActivate(); } catch (err) { console.error('Tab activate failed:', err); }
        }
        if (options.remember !== false) {
            try { localStorage.setItem('omni.activeTab', tab); } catch (_) {}
        }
        if (options.updateHash !== false && location.hash !== '#' + tab) {
            try { history.replaceState(null, '', '#' + tab); } catch (_) {}
        }
        var main = document.getElementById('main-content');
        if (main && previous !== tab) main.scrollTop = 0;
        this.closeNavigation();
        if (options.focusMain && main) main.focus({ preventScroll: true });
        return true;
    },

    openNavigation() {
        document.body.classList.add('nav-open');
        var toggle = document.getElementById('nav-toggle');
        if (toggle) toggle.setAttribute('aria-expanded', 'true');
    },

    closeNavigation() {
        document.body.classList.remove('nav-open');
        var toggle = document.getElementById('nav-toggle');
        if (toggle) toggle.setAttribute('aria-expanded', 'false');
    },

    initTabs() {
        var buttons = Array.from(document.querySelectorAll('.tab-btn'));
        buttons.forEach(function(btn, index) {
            var tab = btn.dataset.tab;
            btn.id = 'nav-' + tab;
            btn.tabIndex = index === 0 ? 0 : -1;
            var panel = document.getElementById('tab-' + tab);
            if (panel) panel.setAttribute('aria-labelledby', btn.id);
            btn.addEventListener('click', function() {
                App.activateTab(tab, { focusMain: window.innerWidth <= 920 });
            });
            btn.addEventListener('keydown', function(event) {
                if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return;
                event.preventDefault();
                var current = buttons.indexOf(btn);
                var next = event.key === 'Home' ? 0
                    : event.key === 'End' ? buttons.length - 1
                    : event.key === 'ArrowDown' ? (current + 1) % buttons.length
                    : (current - 1 + buttons.length) % buttons.length;
                buttons[next].focus();
            });
        });
        var toggle = document.getElementById('nav-toggle');
        var scrim = document.getElementById('nav-scrim');
        if (toggle) toggle.addEventListener('click', function() {
            if (document.body.classList.contains('nav-open')) App.closeNavigation();
            else App.openNavigation();
        });
        if (scrim) scrim.addEventListener('click', function() { App.closeNavigation(); });
        document.addEventListener('keydown', function(event) {
            if (event.key === 'Escape' && document.body.classList.contains('nav-open')) App.closeNavigation();
        });
        window.addEventListener('hashchange', function() {
            var requested = location.hash.replace(/^#/, '');
            if (requested && requested !== App.state.activeTab) {
                App.activateTab(requested, { updateHash: false, remember: false });
            }
        });
        var shutdownBtn = document.getElementById('app-shutdown-btn');
        if (shutdownBtn) {
            shutdownBtn.addEventListener('click', function() {
                App.shutdownApplication();
            });
        }
    },

    async shutdownApplication() {
        if (this.state.shuttingDown) return;
        if (!confirm(
            'Shut down Omni Studio?\n\nThis unloads every model, stops ComfyUI, cancels downloads and jobs, kills app-owned processes, and closes the app.'
        )) return;

        this.state.shuttingDown = true;
        var btn = document.getElementById('app-shutdown-btn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Shutting down...';
        }
        if (this._sse) {
            this._sse.close();
            this._sse = null;
        }
        var overlay = document.createElement('div');
        overlay.className = 'shutdown-overlay';
        overlay.innerHTML = '<strong>Shutting down Omni Studio</strong>'
            + '<span>Unloading models and stopping all app processes...</span>';
        document.body.appendChild(overlay);

        try {
            await this.api('POST', '/api/app/shutdown', { confirm: true });
        } catch (err) {
            // The bridge can disappear immediately after acknowledging. A
            // dropped connection here normally means shutdown is underway.
            if (/^(400|401|403|413):/.test(err.message || '')) {
                overlay.remove();
                this.state.shuttingDown = false;
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = 'Shutdown';
                }
                this.toast('Shutdown refused: ' + err.message, 'error');
                return;
            }
        }

        setTimeout(function() {
            try { window.close(); } catch (e) {}
        }, 750);
    },

    /** Media and EventSource requests authenticate with the HttpOnly cookie
     *  established by /api/session. Keep credentials out of URLs. */
    urlWithToken(path) {
        return path;
    },

    updateGatewayBadge() {
        var badge = document.getElementById('gateway-badge');
        if (!badge) return;
        var copy = badge.querySelector('.status-copy');
        if (this.state.connected) {
            var wc = this.state.workers.length;
            var cc = this.state.comfyInstances.length;
            if (copy) copy.textContent = wc || cc ? wc + ' workers · ' + cc + ' ComfyUI' : 'Connected · idle';
            badge.className = 'status-pill status-online';
            badge.title = wc + ' workers and ' + cc + ' ComfyUI instances';
        } else {
            if (copy) copy.textContent = 'Disconnected';
            badge.className = 'status-pill status-offline';
            badge.title = 'Cannot reach the local gateway';
        }
    },

    preserveFocus(renderFn) {
        var active = document.activeElement;
        var activeId = active && active.id ? active.id : null;
        var activeValue = active && typeof active.value === 'string' ? active.value : null;
        renderFn();
        if (!activeId) return;
        var restored = document.getElementById(activeId);
        if (!restored) return;
        if (activeValue !== null && typeof restored.value === 'string') {
            restored.value = activeValue;
        }
        try { restored.focus(); } catch(err) {}
    },

    formControlActiveIn(containerId) {
        var root = document.getElementById(containerId);
        var active = document.activeElement;
        if (!root || !active || !root.contains(active) || !active.matches) return false;
        return active.matches('input,select,textarea,button,[contenteditable="true"]');
    },

    initLogStream() {
        // /api/session establishes an HttpOnly, same-site cookie. EventSource
        // sends that cookie automatically, keeping the token out of URLs,
        // browser history, DOM attributes, and access logs.
        this._sse = new EventSource('/api/logs/stream');
        this._sse.onmessage = function(e) {
            try {
                var entry = JSON.parse(e.data);
                App.state.logs.push(entry);
                if (App.state.logs.length > App.MAX_LOGS) App.state.logs.shift();
                if (App.isTabInitialized('log') && typeof TabLog !== 'undefined') TabLog.appendEntry(entry);
            } catch(err) {}
        };
        this._sse.onerror = function() {
            var badge = document.getElementById('gateway-badge');
            if (badge && App.state.connected) {
                var copy = badge.querySelector('.status-copy');
                if (copy) copy.textContent = 'Reconnecting...';
                badge.className = 'status-pill status-reconnecting';
            }
        };
        this._sse.onopen = function() { App.updateGatewayBadge(); };
        // Clean up SSE on page unload to prevent server-side leak
        window.addEventListener('beforeunload', function() {
            if (App._sse) { App._sse.close(); App._sse = null; }
        });
    },

    _backoff: 0,
    _pollTick: 0,
    _reconnectTimer: null,
    _recoveringBackend: false,

    async recoverBackendState() {
        if (this._recoveringBackend) return;
        this._recoveringBackend = true;
        try {
            if (!this.state.sessionToken) {
                var sessionReady = await this.initSession({ silent: true });
                if (!sessionReady) return;
            }
            if (!this.state.configLoaded) await this.loadConfig();
            await this.loadSetupStatus();
            await this.loadInstallJobs();
            await this.loadLoras();
            await this.loadDevices();
        } finally {
            this._recoveringBackend = false;
        }
    },

    async poll() {
        if (this.state.shuttingDown) return;
        if (this._polling) return;
        this._pollTick++;
        this._polling = true;
        var wasConnected = this.state.connected;
        try {
            if (!this.state.sessionToken) {
                var sessionReady = await this.initSession({ silent: true });
                if (!sessionReady) throw new Error('Local session is not ready');
            }
            var wdata = await this.api('GET', '/api/workers');
            var cdata = await this.api('GET', '/api/comfy/instances');
            this.state.connected = true;
            this._backoff = 0;
            if (this._reconnectTimer) {
                clearTimeout(this._reconnectTimer);
                this._reconnectTimer = null;
            }
            this.state.workers = wdata.workers || [];
            this.state.comfyInstances = cdata.instances || [];
            if (!wasConnected && (this._pollTick > 1 || !this.state.configLoaded)) {
                await this.recoverBackendState();
            }
            this.updateGatewayBadge();
            if (this.state.activeTab === 'server' && this.isTabInitialized('server') && typeof TabServer !== 'undefined') {
                var serverEditing = this.formControlActiveIn('tab-server')
                    || (typeof TabServer.isSpawnFormActive === 'function' && TabServer.isSpawnFormActive());
                if (serverEditing) {
                    if (typeof TabServer.renderDevices === 'function') TabServer.renderDevices();
                } else {
                    this.preserveFocus(function() { TabServer.render(); });
                }
            }
            if (this.state.activeTab === 'comfy' && this.isTabInitialized('comfy') && typeof TabComfy !== 'undefined' && !this.formControlActiveIn('tab-comfy')) {
                this.preserveFocus(function() { TabComfy.render(); });
            }
            if (this.state.activeTab === 'testing' && this.isTabInitialized('testing') && typeof TabTesting !== 'undefined' && !this.formControlActiveIn('tab-testing')) {
                this.preserveFocus(function() { TabTesting.render(); });
            }
            if (this.state.activeTab === 'moss' && this.isTabInitialized('moss') && typeof TabMoss !== 'undefined' && !this.formControlActiveIn('tab-moss')) {
                this.preserveFocus(function() { TabMoss.render(); });
            }
            if (this.state.activeTab === 'setup' && this.isTabInitialized('setup') && typeof TabSetup !== 'undefined') {
                this.preserveFocus(function() { TabSetup.render(); });
            }
        } catch(err) {
            this.state.connected = false;
            this._backoff = Math.min(this._backoff + 1, 3);
            this.updateGatewayBadge();
            if (!this._reconnectTimer) {
                var retryMs = Math.min(1000 * (1 << (this._backoff - 1)), 8000);
                this._reconnectTimer = setTimeout(function() {
                    App._reconnectTimer = null;
                    App.poll();
                }, retryMs);
            }
        } finally {
            this._polling = false;
        }
    },

    async loadConfig() {
        try {
            var data = await this.api('GET', '/api/config');
            this.state.models = data.models || {};
            this.state.variants = data.variants || {};
            this.state.loraCompatible = data.lora_compatible || [];
            this.state.configLoaded = true;
        } catch(err) {
            console.error('Failed to load config:', err);
            this.toast('Failed to load config -- server may be starting', 'error');
        }
    },

    async loadLoras() {
        try {
            var data = await this.api('GET', '/api/loras');
            this.state.loras = data.loras || [];
            if (this.isTabInitialized('setup') && typeof TabSetup !== 'undefined' && typeof TabSetup.renderInstalledLoras === 'function') {
                TabSetup.renderInstalledLoras();
            }
        } catch(err) {
            console.error('Failed to load LoRAs:', err);
        }
    },

    async loadSetupStatus() {
        try {
            var data = await this.api('GET', '/api/setup/status');
            this.state.setupStatus = data.status || {};
            if (this.state.activeTab === 'setup' && this.isTabInitialized('setup') && typeof TabSetup !== 'undefined') this.preserveFocus(function() { TabSetup.render(); });
            if (this.state.activeTab === 'models' && this.isTabInitialized('models') && typeof TabModels !== 'undefined') this.preserveFocus(function() { TabModels.render(); });
            if (this.state.activeTab === 'testing' && this.isTabInitialized('testing') && typeof TabTesting !== 'undefined') this.preserveFocus(function() { TabTesting.render(); });
            if (this.state.activeTab === 'moss' && this.isTabInitialized('moss') && typeof TabMoss !== 'undefined') this.preserveFocus(function() { TabMoss.render(); });
            if (this.isTabInitialized('server') && typeof TabServer !== 'undefined' && typeof TabServer.updateVariantOptions === 'function') {
                TabServer.updateVariantOptions();
            }
        } catch(err) {
            console.error('Failed to load setup status:', err);
        }
    },

    async loadInstallJobs() {
        try {
            var data = await this.api('GET', '/api/setup/jobs');
            this.state.installJobs = data.jobs || [];
            this.renderInstallFailureNotice();
            this.surfaceNewInstallFailure();
            if (this.isTabInitialized('setup') && typeof TabSetup !== 'undefined' && typeof TabSetup.renderInstallJobs === 'function') {
                TabSetup.renderInstallJobs();
            }
            if (this.isTabInitialized('models') && typeof TabModels !== 'undefined' && typeof TabModels.renderInstallJobs === 'function') {
                TabModels.renderInstallJobs();
            }
        } catch(err) {
            console.error('Failed to load install jobs:', err);
        }
    },

    installJobText(job) {
        return ((job && (job.output || job.error)) || '').trim();
    },

    installJobUrls(job) {
        var hits = [];
        var text = this.installJobText(job);
        String(text || '').replace(/https?:\/\/[^\s'"<>`)]+/g, function(url) {
            url = url.replace(/[.,;:]+$/, '');
            if (hits.indexOf(url) === -1) hits.push(url);
            return url;
        });
        if (job && job.accept_url && hits.indexOf(job.accept_url) === -1) hits.unshift(job.accept_url);
        if (job && job.repo) {
            var repoUrl = 'https://huggingface.co/' + job.repo;
            if (hits.indexOf(repoUrl) === -1) hits.push(repoUrl);
        }
        return hits;
    },

    latestInstallFailure() {
        var jobs = this.state.installJobs || [];
        for (var i = 0; i < jobs.length; i++) {
            var job = jobs[i];
            if (!job || job.status !== 'failed') continue;
            if (job.latest_for_target === false) continue;
            if (this.installJobUrls(job).length || this.installJobText(job)) return job;
        }
        return null;
    },

    renderInstallFailureNotice() {
        var existing = document.getElementById('install-failure-notice');
        var job = this.latestInstallFailure();
        if (!job || job.job_id === this.state.dismissedInstallFailureJobId) {
            if (existing) existing.remove();
            return;
        }
        var main = document.querySelector('main');
        if (!main) return;
        var E = this.esc;
        var urls = this.installJobUrls(job);
        var target = job.variant_display || job.model || job.name || job.repo || 'Install';
        var text = this.installJobText(job).toLowerCase();
        var gated = job.failure_kind === 'access' || !!(job.gated || job.accept_url || /gated|accept the model card|401|403|license/.test(text));
        var network = job.failure_kind === 'network' || /temporary failure|failed to resolve|name resolution|network|connectionpool|max retries|dns/.test(text);
        var repoLine = job.repo ? ' Model page: ' + job.repo + '.' : '';
        var note = gated
            ? 'Accept HuggingFace model access with the same account as your saved token, then retry.'
            : 'Open the model page, then retry after resolving the install issue.';
        note += repoLine;
        if (network) note += ' This attempt also had a network/DNS failure reaching HuggingFace.';
        if (!existing) {
            existing = document.createElement('div');
            existing.id = 'install-failure-notice';
            main.insertBefore(existing, main.firstChild);
        }
        var html = '<div style="border:1px solid rgba(218,54,51,0.55);background:rgba(218,54,51,0.12);border-radius:8px;padding:12px 14px;margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">';
        html += '<div style="min-width:260px;flex:1">';
        html += '<div style="font-weight:600;color:var(--text-primary)">Install failed: ' + E(target) + '</div>';
        html += '<div style="font-size:13px;color:var(--text-secondary);margin-top:3px">' + E(note) + '</div>';
        html += '</div>';
        html += '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">';
        if (urls.length) {
            html += '<button class="btn btn-sm btn-primary" data-open-external-url="' + E(urls[0]) + '">' + (gated ? 'Accept model access' : 'Open default') + '</button>';
            html += '<button class="btn btn-sm" data-open-external-browser="chrome" data-open-external-url="' + E(urls[0]) + '">Chrome</button>';
            html += '<button class="btn btn-sm" data-open-external-browser="edge" data-open-external-url="' + E(urls[0]) + '">Edge</button>';
            html += '<button class="btn btn-sm" data-open-external-browser="firefox" data-open-external-url="' + E(urls[0]) + '">Firefox</button>';
            html += '<button class="btn btn-sm" data-copy-url="' + E(urls[0]) + '">Copy link</button>';
        }
        html += '<button class="btn btn-sm" data-install-failure-details="' + E(job.job_id) + '">Details</button>';
        html += '<button class="btn btn-sm" data-install-failure-dismiss="' + E(job.job_id) + '">Dismiss</button>';
        html += '</div></div>';
        existing.innerHTML = html;
        var detailBtn = existing.querySelector('[data-install-failure-details]');
        if (detailBtn) {
            detailBtn.addEventListener('click', function() {
                if (window.TabSetup && TabSetup.showInstallFailure) {
                    TabSetup.showInstallFailure(job);
                } else {
                    App.showTextModal('Install failed', App.installJobText(job) || 'No output captured.');
                }
            });
        }
        var dismissBtn = existing.querySelector('[data-install-failure-dismiss]');
        if (dismissBtn) {
            dismissBtn.addEventListener('click', function() {
                App.state.dismissedInstallFailureJobId = job.job_id;
                App.renderInstallFailureNotice();
            });
        }
    },

    _surfacedInstallFailureJobs: {},

    surfaceNewInstallFailure() {
        var job = this.latestInstallFailure();
        if (!job || this._surfacedInstallFailureJobs[job.job_id]) return;
        this._surfacedInstallFailureJobs[job.job_id] = true;
        if (window.TabSetup && TabSetup.showInstallFailure) {
            TabSetup.showInstallFailure(job);
        }
    },

    async openExternalUrl(url, browser) {
        if (!url) return false;
        try {
            var body = { url: url };
            if (browser) body.browser = browser;
            var result = await this.api('POST', '/api/open-url', body);
            if (result && result.opened) return true;
        } catch (err) {
            console.warn('Default browser open failed:', err);
        }
        try {
            var w = window.open(url, '_blank', 'noopener,noreferrer');
            if (w) return true;
        } catch (err2) {
            console.warn('Window open fallback failed:', err2);
        }
        return false;
    },

    async copyExternalUrl(url) {
        if (!url) return false;
        try {
            await navigator.clipboard.writeText(url);
            this.toast('Link copied', 'success');
            return true;
        } catch (err) {
            this.showTextModal('Copy link', url);
            return false;
        }
    },

    bindExternalUrlButtons() {
        if (this._externalUrlHandlerBound) return;
        this._externalUrlHandlerBound = true;
        document.addEventListener('click', function(e) {
            var copyTarget = e.target && e.target.closest ? e.target.closest('[data-copy-url]') : null;
            if (copyTarget) {
                e.preventDefault();
                App.copyExternalUrl(copyTarget.getAttribute('data-copy-url') || '');
                return;
            }
            var target = e.target && e.target.closest
                ? e.target.closest('[data-open-external-url], a[href^="https://huggingface.co/"]')
                : null;
            if (!target) return;
            var url = target.getAttribute('data-open-external-url') || target.getAttribute('href') || '';
            var browser = target.getAttribute('data-open-external-browser') || '';
            if (!url) return;
            e.preventDefault();
            var oldText = target.textContent;
            target.disabled = true;
            target.textContent = 'Opening...';
            App.openExternalUrl(url, browser).then(function(opened) {
                if (!opened) App.toast('Could not open browser for link', 'error');
            }).finally(function() {
                target.disabled = false;
                target.textContent = oldText;
            });
        });
    },

    async loadDevices() {
        try {
            var data = await this.api('GET', '/api/devices');
            this.state.devices = data.devices || [];
            if (this.isTabInitialized('server') && typeof TabServer !== 'undefined') TabServer.renderDevices();
            if (this.isTabInitialized('comfy') && typeof TabComfy !== 'undefined') TabComfy.renderDevices();
        } catch(err) {
            console.error('Failed to load devices:', err);
        }
    },

    async init() {
        this.initTabs();
        this.bindExternalUrlButtons();
        this.bindGlobalFieldEvents();
        await this.initSession();
        await this.loadConfig();
        await this.loadSetupStatus();
        await this.loadInstallJobs();
        await this.loadLoras();

        var requested = location.hash.replace(/^#/, '');
        if (!document.querySelector('.tab-btn[data-tab="' + requested + '"]')) {
            try { requested = localStorage.getItem('omni.activeTab') || 'setup'; }
            catch (_) { requested = 'setup'; }
        }
        if (!document.querySelector('.tab-btn[data-tab="' + requested + '"]')) requested = 'setup';
        this.activateTab(requested, { updateHash: true, remember: false });

        this.initLogStream();
        await this.poll();
        setInterval(function() { App.poll(); }, this.POLL_INTERVAL);
        setInterval(function() {
            var activeJobs = (App.state.installJobs || []).some(function(job) {
                return job && ['running', 'queued', 'cancelling'].includes(job.status);
            });
            var setupAdvancedOpen = App.state.activeTab === 'setup'
                && document.querySelector('#tab-setup > .advanced-panel[open]');
            if (activeJobs || App.state.activeTab === 'models' || setupAdvancedOpen) App.loadInstallJobs();
        }, 5000);
        setInterval(function() {
            if (['server', 'comfy', 'testing'].includes(App.state.activeTab)) App.loadDevices();
        }, 15000);
    },

    showTextModal(title, text, opts) {
        var existing = document.getElementById('modal-backdrop');
        if (existing) {
            if (typeof existing._dismiss === 'function') existing._dismiss();
            else existing.remove();
        }
        var returnFocus = document.activeElement;
        opts = opts || {};
        var backdrop = document.createElement('div');
        backdrop.id = 'modal-backdrop';
        backdrop.className = 'modal-backdrop';
        var modal = document.createElement('div');
        modal.className = 'modal';
        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');
        modal.setAttribute('aria-labelledby', 'modal-title');
        var header = document.createElement('div');
        header.className = 'modal-header';
        var h = document.createElement('h2');
        h.id = 'modal-title';
        h.textContent = title;
        var close = document.createElement('button');
        close.className = 'btn btn-sm';
        close.textContent = 'Close';
        function dismiss() {
            document.removeEventListener('keydown', onKey);
            backdrop.remove();
            if (returnFocus && typeof returnFocus.focus === 'function') returnFocus.focus();
        }
        function onKey(event) {
            if (event.key === 'Escape') {
                event.preventDefault();
                dismiss();
            }
        }
        backdrop._dismiss = dismiss;
        close.addEventListener('click', dismiss);
        header.appendChild(h);
        header.appendChild(close);
        modal.appendChild(header);
        if (opts.html) {
            var body = document.createElement('div');
            body.style.padding = '16px';
            body.innerHTML = text || '';
            modal.appendChild(body);
        } else {
            var pre = document.createElement('pre');
            pre.className = 'modal-log';
            pre.textContent = text || '';
            modal.appendChild(pre);
        }
        backdrop.appendChild(modal);
        backdrop.addEventListener('click', function(e) {
            if (e.target === backdrop) dismiss();
        });
        document.body.appendChild(backdrop);
        document.addEventListener('keydown', onKey);
        close.focus();
    },

    /** A copy-once modal for secrets that won't be retrievable later (e.g.
     *  newly-minted bearer keys). Same chrome as showTextModal but with a
     *  Copy button and a dismissive "I've saved it" CTA. Returns the
     *  backdrop element so callers can append additional notes if they
     *  want, but in practice the helper is fire-and-forget. */
    showCopyModal(title, secret, note) {
        var existing = document.getElementById('modal-backdrop');
        if (existing) {
            if (typeof existing._dismiss === 'function') existing._dismiss();
            else existing.remove();
        }
        var returnFocus = document.activeElement;
        var backdrop = document.createElement('div');
        backdrop.id = 'modal-backdrop';
        backdrop.className = 'modal-backdrop';
        var modal = document.createElement('div');
        modal.className = 'modal';
        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');
        modal.setAttribute('aria-labelledby', 'modal-title');
        var header = document.createElement('div');
        header.className = 'modal-header';
        var h = document.createElement('h2');
        h.id = 'modal-title';
        h.textContent = title;
        header.appendChild(h);
        modal.appendChild(header);
        var body = document.createElement('div');
        body.style.padding = '16px';
        body.style.display = 'flex';
        body.style.flexDirection = 'column';
        body.style.gap = '12px';
        if (note) {
            var p = document.createElement('div');
            p.style.color = 'var(--text-secondary)';
            p.style.fontSize = '13px';
            p.textContent = note;
            body.appendChild(p);
        }
        var pre = document.createElement('pre');
        pre.className = 'modal-log';
        pre.style.userSelect = 'all';
        pre.textContent = secret || '';
        body.appendChild(pre);
        var actions = document.createElement('div');
        actions.style.display = 'flex';
        actions.style.gap = '8px';
        actions.style.justifyContent = 'flex-end';
        var copyBtn = document.createElement('button');
        copyBtn.className = 'btn btn-primary btn-sm';
        copyBtn.textContent = 'Copy';
        copyBtn.addEventListener('click', function() {
            try {
                navigator.clipboard.writeText(secret || '');
                copyBtn.textContent = 'Copied ✓';
                setTimeout(function() { copyBtn.textContent = 'Copy'; }, 1500);
            } catch (e) {
                App.toast('Clipboard unavailable; select the text manually', 'error');
            }
        });
        var doneBtn = document.createElement('button');
        doneBtn.className = 'btn btn-sm';
        doneBtn.textContent = "I've saved it";
        function dismiss() {
            document.removeEventListener('keydown', onKey);
            backdrop.remove();
            if (returnFocus && typeof returnFocus.focus === 'function') returnFocus.focus();
        }
        function onKey(event) {
            if (event.key === 'Escape') {
                event.preventDefault();
                dismiss();
            }
        }
        backdrop._dismiss = dismiss;
        doneBtn.addEventListener('click', dismiss);
        actions.appendChild(copyBtn);
        actions.appendChild(doneBtn);
        body.appendChild(actions);
        modal.appendChild(body);
        backdrop.appendChild(modal);
        backdrop.addEventListener('click', function(e) {
            if (e.target === backdrop) dismiss();
        });
        document.body.appendChild(backdrop);
        document.addEventListener('keydown', onKey);
        copyBtn.focus();
        return backdrop;
    },

    /** Stream an SSE-style POST response. Browsers can't EventSource a POST,
     *  so we use fetch + ReadableStream and parse 'data: {...}' lines.
     *  Calls onEvent({...parsed JSON...}) for each event, onError(err) on
     *  failure, onDone() when the stream completes. Returns an `abort`
     *  function the caller can invoke to cut the stream early. */
    streamSSE(path, body, callbacks) {
        callbacks = callbacks || {};
        var ctrl = new AbortController();
        var headers = { 'Content-Type': 'application/json' };
        if (this.state.sessionToken) headers['X-Omni-Token'] = this.state.sessionToken;
        fetch(path, {
            method: 'POST',
            headers: headers,
            body: body ? JSON.stringify(body) : undefined,
            signal: ctrl.signal,
        }).then(function(resp) {
            if (!resp.ok) {
                if (resp.status === 401) {
                    App.state.sessionToken = null;
                    App.state.sessionInfo = null;
                }
                return resp.text().then(function(t) {
                    throw new Error(resp.status + ': ' + t);
                });
            }
            var jobId = resp.headers.get('X-Job-ID');
            if (jobId && callbacks.onEvent) callbacks.onEvent({job_id: jobId});
            var reader = resp.body.getReader();
            var dec = new TextDecoder();
            var buf = '';
            function pump() {
                return reader.read().then(function(r) {
                    if (r.done) {
                        if (buf.trim()) {
                            App._dispatchSSEChunk(buf, callbacks);
                        }
                        if (callbacks.onDone) callbacks.onDone();
                        return;
                    }
                    buf += dec.decode(r.value, { stream: true });
                    var idx;
                    while ((idx = buf.indexOf('\n\n')) !== -1) {
                        var chunk = buf.substring(0, idx);
                        buf = buf.substring(idx + 2);
                        App._dispatchSSEChunk(chunk, callbacks);
                    }
                    return pump();
                });
            }
            return pump();
        }).catch(function(err) {
            if (err.name === 'AbortError') {
                if (callbacks.onAbort) callbacks.onAbort();
                return;
            }
            if (callbacks.onError) callbacks.onError(err);
        });
        return function abort() { ctrl.abort(); };
    },

    _dispatchSSEChunk(chunk, callbacks) {
        var lines = chunk.split('\n');
        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            if (!line.startsWith('data:')) continue;
            var payload = line.substring(5).trim();
            if (!payload) continue;
            try {
                var parsed = JSON.parse(payload);
                if (callbacks.onEvent) callbacks.onEvent(parsed);
            } catch (e) {
                // Non-JSON SSE line — ignore (heartbeats, comments)
            }
        }
    },

    /* ======================================================================
       Shared UI primitives (clarity overhaul)
       ----------------------------------------------------------------------
       App.field(opts) -> HTML string for a consistent labeled control row.
       Read values back with document.getElementById(opts.id).value etc.
       opts:
         type: 'select'|'number'|'slider'|'toggle'|'text'|'textarea'|'dualNumber'
         id, label, hint (one-line help), tip (glossary '?'),
         value, disabled, placeholder
         select:     options: [{value,label,disabled?}] (or plain strings)
         number:     min,max,step,unit
         slider:     min,max,step,unit  (shows a live value badge)
         toggle:     toggleLabel (defaults to label)
         textarea:   rows
         dualNumber: startValue,endValue,startPlaceholder,endPlaceholder,min,max,step
                     (ids become <id>-start and <id>-end)
       ====================================================================== */
    field(o) {
        o = o || {};
        var E = this.esc.bind(this);
        var id = o.id || ('f-' + Math.random().toString(36).slice(2));
        var t = o.type || 'text';
        var tipHtml = o.tip ? this.tip(o.tip) : '';

        // Toggle renders its own inline label, so handle it separately.
        if (t === 'toggle') {
            var ck = o.value ? ' checked' : '';
            var tg = '<label class="field-toggle"><input type="checkbox" id="' + E(id) + '"'
                + ck + (o.disabled ? ' disabled' : '') + '><span>'
                + E(o.toggleLabel != null ? o.toggleLabel : (o.label || '')) + '</span>'
                + tipHtml + '</label>';
            var th = o.hint ? '<div class="field-hint">' + E(o.hint) + '</div>' : '';
            return '<div class="field-row">' + tg + th + '</div>';
        }

        var ctl = '';
        if (t === 'select') {
            var opts = (o.options || []).map(function(op) {
                var isObj = op && typeof op === 'object';
                var v = isObj ? op.value : op;
                var lab = isObj ? (op.label != null ? op.label : op.value) : op;
                var sel = (String(v) === String(o.value)) ? ' selected' : '';
                var dis = (isObj && op.disabled) ? ' disabled' : '';
                return '<option value="' + E(v) + '"' + sel + dis + '>' + E(lab) + '</option>';
            }).join('');
            ctl = '<select class="field-control" id="' + E(id) + '"'
                + (o.disabled ? ' disabled' : '') + '>' + opts + '</select>';
        } else if (t === 'number') {
            var num = '<input class="field-control" type="number" id="' + E(id) + '"'
                + (o.min != null ? ' min="' + E(o.min) + '"' : '')
                + (o.max != null ? ' max="' + E(o.max) + '"' : '')
                + (o.step != null ? ' step="' + E(o.step) + '"' : '')
                + (o.value != null ? ' value="' + E(o.value) + '"' : '')
                + (o.placeholder ? ' placeholder="' + E(o.placeholder) + '"' : '')
                + (o.disabled ? ' disabled' : '') + '>';
            ctl = o.unit
                ? '<div class="field-num">' + num + '<span class="field-unit">' + E(o.unit) + '</span></div>'
                : num;
        } else if (t === 'slider') {
            var sval = (o.value != null ? o.value : o.min);
            ctl = '<div class="field-slider">'
                + '<input type="range" data-field-slider id="' + E(id) + '"'
                + ' min="' + E(o.min) + '" max="' + E(o.max) + '"'
                + ' step="' + E(o.step != null ? o.step : 1) + '"'
                + ' value="' + E(sval) + '"' + (o.disabled ? ' disabled' : '') + '>'
                + '<span class="field-val" id="' + E(id) + '-val" data-unit="' + E(o.unit || '') + '">'
                + E(sval) + E(o.unit || '') + '</span></div>';
        } else if (t === 'textarea') {
            ctl = '<textarea class="field-control" id="' + E(id) + '"'
                + (o.rows ? ' rows="' + E(o.rows) + '"' : '')
                + (o.placeholder ? ' placeholder="' + E(o.placeholder) + '"' : '')
                + '>' + E(o.value || '') + '</textarea>';
        } else if (t === 'dualNumber') {
            var attrs = (o.min != null ? ' min="' + E(o.min) + '"' : '')
                + (o.max != null ? ' max="' + E(o.max) + '"' : '')
                + (o.step != null ? ' step="' + E(o.step) + '"' : '');
            ctl = '<div class="field-dual">'
                + '<input class="field-control" type="number" id="' + E(id) + '-start"'
                + attrs + (o.startValue != null ? ' value="' + E(o.startValue) + '"' : '')
                + ' placeholder="' + E(o.startPlaceholder || 'start') + '">'
                + '<span class="field-dual-sep">→</span>'
                + '<input class="field-control" type="number" id="' + E(id) + '-end"'
                + attrs + (o.endValue != null ? ' value="' + E(o.endValue) + '"' : '')
                + ' placeholder="' + E(o.endPlaceholder || 'end') + '">'
                + '</div>';
        } else {
            ctl = '<input class="field-control" type="text" id="' + E(id) + '"'
                + (o.value != null ? ' value="' + E(o.value) + '"' : '')
                + (o.placeholder ? ' placeholder="' + E(o.placeholder) + '"' : '')
                + (o.disabled ? ' disabled' : '') + '>';
        }
        var label = o.label != null
            ? '<label class="field-label" for="' + E(id) + '">' + E(o.label) + tipHtml + '</label>'
            : '';
        var hint = o.hint ? '<div class="field-hint">' + E(o.hint) + '</div>' : '';
        return '<div class="field-row">' + label + ctl + hint + '</div>';
    },

    /** A small "?" glossary tooltip. Pure CSS hover/focus; no inline JS. */
    tip(text) {
        return '<span class="help-tip" tabindex="0" role="img" aria-label="help" data-tip="'
            + this.esc(text) + '">?</span>';
    },

    /** A prerequisite banner with an optional one-click action button.
     *  Caller binds `#<actionId>` to a handler (e.g. App.spawnWorker). */
    prereqBanner(o) {
        o = o || {};
        var E = this.esc.bind(this);
        var btn = (o.actionId && o.actionLabel)
            ? '<button class="btn btn-primary btn-sm" id="' + E(o.actionId) + '"'
              + (o.actionDisabled ? ' disabled' : '') + '>' + E(o.actionLabel) + '</button>'
            : '';
        return '<div class="prereq-banner ' + (o.level === 'info' ? 'prereq-info' : 'prereq-warn') + '">'
            + '<span class="prereq-text">' + E(o.text) + '</span>' + btn + '</div>';
    },

    // ---- Readiness helpers (read from already-polled App.state) ----
    workerReadyFor(model) {
        return (this.state.workers || []).some(function(w) {
            return w.model === model && w.status === 'ready';
        });
    },
    workerExistsFor(model) {
        return (this.state.workers || []).some(function(w) {
            return w.model === model && w.status !== 'dead';
        });
    },
    comfyReady() {
        return (this.state.comfyInstances || []).some(function(i) {
            return i.status === 'ready';
        });
    },
    modelInstalled(model) {
        var s = this.state.setupStatus && this.state.setupStatus[model];
        if (!s) return false;
        if (s.runtime_ready === false) return false;
        if (model === 'moss_tts' || model === 'moss_sfx') return !!s.installed;
        if (s.installed || s.weights_installed) return true;
        if ((model === 'ace_step' || model === 'audio_lab') && this.modelHasInstalledVariant(model)) return true;
        return false;
    },
    modelHasInstalledVariant(model) {
        var s = this.state.setupStatus && this.state.setupStatus[model];
        if (!s) return false;
        if ((s.models_installed || 0) > 0) return true;
        var variants = s.variants || [];
        for (var i = 0; i < variants.length; i++) {
            if (variants[i] && variants[i].installed) return true;
        }
        return false;
    },
    defaultVariantFor(model) {
        var vs = (this.state.variants && this.state.variants[model]) || [];
        for (var i = 0; i < vs.length; i++) {
            if (vs[i] && vs[i].default) return vs[i].variant_id;
        }
        return vs.length ? vs[0].variant_id : null;
    },

    /** Refresh worker + comfy state out-of-band (bypasses the poll guard). */
    async refreshWorkers() {
        try {
            var w = await this.api('GET', '/api/workers');
            this.state.workers = w.workers || [];
            var c = await this.api('GET', '/api/comfy/instances');
            this.state.comfyInstances = c.instances || [];
            this.updateGatewayBadge();
        } catch (err) { /* leave last-known state */ }
    },

    /** Poll a predicate to true (refreshing state each tick) or time out. */
    async waitUntil(pred, timeoutMs) {
        var deadline = Date.now() + (timeoutMs || 120000);
        while (Date.now() < deadline) {
            if (pred()) return true;
            await new Promise(function(r) { setTimeout(r, 2000); });
            await this.refreshWorkers();
        }
        return pred();
    },

    /** One-click "spawn a worker for this model" using sensible defaults.
     *  Resolves when the worker reaches ready (or rejects/times out). */
    async spawnWorker(model, opts) {
        opts = opts || {};
        var body = { model: model };
        var variant = opts.variant || this.defaultVariantFor(model);
        if (variant) body.variant = variant;
        if (opts.device) body.device = opts.device;       // omitted => backend auto-picks
        if (opts.precision) body.precision = opts.precision;
        if (opts.lora) body.lora = opts.lora;
        try {
            await this.api('POST', '/api/workers/spawn', body);
        } catch (err) {
            this.toast('Could not start worker: ' + err.message, 'error');
            throw err;
        }
        this.toast('Starting ' + (this.modelDisplay(model)) + ' — this can take a minute…', 'info');
        await this.refreshWorkers();
        var self = this;
        return this.waitUntil(function() { return self.workerReadyFor(model); }, 240000);
    },

    /** One-click "start ComfyUI" with auto device + normal VRAM mode. */
    async startComfy(opts) {
        opts = opts || {};
        var body = { vram_mode: opts.vram_mode || 'normal' };
        if (opts.device) body.device = opts.device;
        if (opts.precision) body.precision = opts.precision;
        try {
            await this.api('POST', '/api/comfy/start', body);
        } catch (err) {
            this.toast('Could not start ComfyUI: ' + err.message, 'error');
            throw err;
        }
        this.toast('Starting ComfyUI…', 'info');
        await this.refreshWorkers();
        var self = this;
        return this.waitUntil(function() { return self.comfyReady(); }, 180000);
    },

    /** Friendly model display name from /api/config metadata (fallback: id). */
    modelDisplay(model) {
        var m = this.state.models && this.state.models[model];
        return (m && m.display) ? m.display : model;
    },

    /** One global listener keeps every App.field slider's value badge live
     *  without inline handlers (matches the no-inline-JS security posture). */
    bindGlobalFieldEvents() {
        document.addEventListener('input', function(e) {
            var t = e.target;
            if (!t || !t.matches || !t.matches('input[type=range][data-field-slider]')) return;
            var val = document.getElementById(t.id + '-val');
            if (val) val.textContent = t.value + (val.dataset.unit || '');
        });
    },
};

document.addEventListener('DOMContentLoaded', function() { App.init(); });
