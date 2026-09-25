/* ==========================================================================
   Tab: Chat — multimodal sessions over /api/chat/sessions
   ==========================================================================

   Three-pane layout: Sessions (left) | Thread (center) | Side knobs (right).

   Backend:
   * GET    /api/chat/sessions
   * POST   /api/chat/sessions               body: {model, system?, metadata?}
   * GET    /api/chat/sessions/{id}
   * DELETE /api/chat/sessions/{id}
   * POST   /api/chat/sessions/{id}/messages         (sync, used as fallback)
   * POST   /api/chat/sessions/{id}/messages/stream  (SSE: {job_id},{delta},{done},{error},{cancelled})
   * POST   /api/chat/{model}/cancel/{job_id}        (abort an in-flight stream)

   Sessions are in-memory with a 24h idle TTL and 100-message cap (the
   server trims oldest non-system messages on append).
   ========================================================================== */

var TabChat = (function() {
    var LS_KEY = 'omniChatPrefs.v1';

    var state = {
        sessions: [],            // list summaries from GET /api/chat/sessions
        currentId: null,         // active session id
        currentSession: null,    // full snapshot
        loadingSessions: false,
        loadingThread: false,
        streaming: false,
        streamAbort: null,
        streamingJobId: null,    // captured from first {job_id} SSE event for cancel
        streamGeneration: 0,
        threadGeneration: 0,
        spawning: false,         // true while a one-click Spawn worker is in flight
        attachments: {           // pending uploads for the next send
            image: null,         // {b64, mime, name}
            audio: null,
            video: null,
        },
        // Persisted prefs:
        model: '',
        system: '',
        temperature: 0.7,
        topP: 0.9,
        maxNewTokens: 512,
        bound: false,
    };

    function loadPrefs() {
        try {
            var raw = localStorage.getItem(LS_KEY);
            if (!raw) return;
            var p = JSON.parse(raw);
            ['model', 'system'].forEach(function(k) {
                if (typeof p[k] === 'string') state[k] = p[k];
            });
            ['temperature', 'topP', 'maxNewTokens'].forEach(function(k) {
                if (typeof p[k] === 'number') state[k] = p[k];
            });
        } catch (e) {}
    }
    function savePrefs() {
        try {
            localStorage.setItem(LS_KEY, JSON.stringify({
                model: state.model, system: state.system,
                temperature: state.temperature, topP: state.topP,
                maxNewTokens: state.maxNewTokens,
            }));
        } catch (e) {}
    }

    // ----------------------------------------------------------------------
    // Input modalities wired through Omni's chat handlers. Upstream model
    // descriptions do not establish attachment support in this app.
    // ----------------------------------------------------------------------
    function modelCaps(modelId) {
        var fallback = {
            qwen_omni_3b:        { image: true,  audio: false, video: false, streaming: true },
            qwen_omni_7b:        { image: true,  audio: false, video: false, streaming: true },
            minicpm_o:           { image: true,  audio: true,  video: false, streaming: true },
            anygpt:              { image: false, audio: false, video: false, streaming: true },
            moshi:               { image: false, audio: true,  video: false, streaming: false },
            qwen3_omni:          { image: true,  audio: false, video: false, streaming: true },
            nemotron_nano_omni:  { image: true,  audio: false, video: false, streaming: true },
        };
        if (fallback[modelId]) return fallback[modelId];
        return {
            image: false,
            audio: false,
            video: false,
            streaming: true,
        };
    }

    // A short, human capability summary for a model — e.g. "text · vision · audio".
    // Always leads with "text" since every chat model accepts text.
    function capabilityHint(modelId) {
        var c = modelCaps(modelId);
        var parts = ['text'];
        if (c.image) parts.push('vision');
        if (c.audio) parts.push('audio');
        if (c.video) parts.push('video');
        return parts.join(' · ');
    }

    function chatableModels() {
        var ids = Object.keys(App.state.models || {});
        // Hide moshi: it's streaming-only and the session API can't carry
        // its full-duplex protocol.
        var hidden = { moshi: true, audio_lab: true, ace_step: true, moss_tts: true, moss_sfx: true };
        return ids.filter(function(m) { return !hidden[m]; });
    }

    // The model that drives the active conversation: the loaded session's
    // model when one is open, otherwise the picked "next new session" model.
    function activeModel() {
        if (state.currentSession && state.currentSession.model) return state.currentSession.model;
        return state.model || chatableModels()[0] || '';
    }

    // ----------------------------------------------------------------------
    // Helpers
    // ----------------------------------------------------------------------
    function fmtRel(ts) {
        if (!ts) return '';
        var now = Date.now() / 1000;
        var s = now - ts;
        if (s < 60) return Math.round(s) + 's ago';
        if (s < 3600) return Math.round(s / 60) + 'm ago';
        if (s < 86400) return Math.round(s / 3600) + 'h ago';
        return Math.round(s / 86400) + 'd ago';
    }
    function readFileAsB64(file, max_bytes) {
        return new Promise(function(resolve, reject) {
            if (max_bytes && file.size > max_bytes) {
                reject(new Error('File too large (' + Math.round(file.size / (1024 * 1024)) +
                    ' MB; max ' + Math.round(max_bytes / (1024 * 1024)) + ' MB)'));
                return;
            }
            var fr = new FileReader();
            fr.onload = function() {
                var s = String(fr.result || '');
                var i = s.indexOf(',');
                resolve({ b64: i >= 0 ? s.substring(i + 1) : s, mime: file.type, name: file.name });
            };
            fr.onerror = function() { reject(new Error('Read failed')); };
            fr.readAsDataURL(file);
        });
    }
    function escMd(s) {
        // Light markdown for chat — preserve line breaks, escape HTML.
        return App.esc(s).replace(/\n/g, '<br>');
    }

    // ----------------------------------------------------------------------
    // Render
    // ----------------------------------------------------------------------
    function render() {
        var el = document.getElementById('tab-chat');
        if (!el) return;
        if (!state.bound) {
            el.innerHTML = ''
                + '<aside class="chat-pane chat-sessions-pane">'
                +   '<div class="chat-pane-header">'
                +     '<span>Sessions</span>'
                +     '<button class="btn btn-sm btn-primary" id="chat-new-btn">+ New</button>'
                +   '</div>'
                +   '<div class="chat-pane-body" id="chat-sessions-body"></div>'
                + '</aside>'
                + '<section class="chat-pane chat-thread-pane">'
                +   '<div class="chat-pane-header" id="chat-thread-header">'
                +     '<span id="chat-thread-title">Pick or create a session</span>'
                +     '<span id="chat-thread-meta" class="api-muted"></span>'
                +   '</div>'
                +   '<div class="chat-thread-prereq" id="chat-prereq" style="padding:0 12px"></div>'
                +   '<div class="chat-pane-body chat-thread" id="chat-thread-body"></div>'
                +   '<div class="chat-pane-footer chat-compose">'
                +     '<div class="chat-attach-row" id="chat-attach-row"></div>'
                +     '<textarea id="chat-input" placeholder="Message... (Shift+Enter for newline)"></textarea>'
                +     '<div class="chat-compose-row">'
                +       '<div style="display:flex;gap:6px">'
                +         '<button class="btn btn-sm" id="chat-attach-image" title="Attach image">🖼️ Image</button>'
                +         '<button class="btn btn-sm" id="chat-attach-audio" title="Attach audio">🔊 Audio</button>'
                +         '<button class="btn btn-sm" id="chat-attach-video" title="Attach video">🎬 Video</button>'
                +       '</div>'
                +       '<div style="display:flex;gap:6px">'
                +         '<button class="btn btn-sm btn-danger" id="chat-abort-btn" style="display:none">■ Abort</button>'
                +         '<button class="btn btn-sm btn-primary" id="chat-send-btn">▶ Send</button>'
                +       '</div>'
                +     '</div>'
                +     '<div class="field-hint" id="chat-attach-hint"></div>'
                +     '<input type="file" id="chat-file-image" accept="image/*" style="display:none">'
                +     '<input type="file" id="chat-file-audio" accept="audio/*" style="display:none">'
                +     '<input type="file" id="chat-file-video" accept="video/*" style="display:none">'
                +   '</div>'
                + '</section>'
                + '<aside class="chat-pane chat-side-pane">'
                +   '<div class="chat-pane-header"><span>Settings</span></div>'
                +   '<div class="chat-pane-body" id="chat-side-body"></div>'
                + '</aside>';
            bindStaticHandlers();
            state.bound = true;
        }
        renderSessionsPanel();
        renderThread();
        renderSidePanel();
        renderAttachments();
        renderPrereq();
    }

    function bindStaticHandlers() {
        document.getElementById('chat-new-btn').addEventListener('click', createSessionFlow);
        document.getElementById('chat-send-btn').addEventListener('click', sendCurrent);
        document.getElementById('chat-abort-btn').addEventListener('click', abortStream);

        var ta = document.getElementById('chat-input');
        ta.addEventListener('keydown', function(e) {
            // Guard against IME composition: CJK input methods fire Enter
            // to commit the current candidate, and we shouldn't treat that
            // as a send. Both `isComposing` and the legacy keyCode 229
            // need to be checked — Safari historically sets only the
            // latter.
            if (e.isComposing || e.keyCode === 229) return;
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendCurrent();
            }
        });

        var bind = function(btnId, fileId, slot) {
            var btn = document.getElementById(btnId);
            var file = document.getElementById(fileId);
            btn.addEventListener('click', function() {
                // Disabled-modality buttons explain themselves inline (the
                // compose hint + a disabled look); a click is simply inert
                // rather than firing a surprising alert.
                if (btn.disabled || btn.classList.contains('chat-attach-disabled')) return;
                file.click();
            });
            file.addEventListener('change', async function() {
                var f = file.files && file.files[0];
                if (!f) return;
                try {
                    // Server caps the base64 *string* at 20 MB chars
                    // (INFER_MAX_IMAGE_BASE64_CHARS). Base64 expands binary
                    // by ~4/3, so we cap binary at ~14 MB to stay under.
                    // Aligns with INFER_MAX_IMAGE_BYTES = 15 MB.
                    var max = 14 * 1024 * 1024;
                    var rec = await readFileAsB64(f, max);
                    state.attachments[slot] = rec;
                    renderAttachments();
                } catch (e) {
                    App.toast(slot + ' attach failed: ' + e.message, 'error');
                }
                file.value = '';
            });
        };
        bind('chat-attach-image', 'chat-file-image', 'image');
        bind('chat-attach-audio', 'chat-file-audio', 'audio');
        bind('chat-attach-video', 'chat-file-video', 'video');
    }

    function renderSessionsPanel() {
        var body = document.getElementById('chat-sessions-body');
        if (!body) return;
        if (state.loadingSessions && !state.sessions.length) {
            body.innerHTML = '<div class="chat-empty">Loading…</div>';
            return;
        }
        if (!state.sessions.length) {
            body.innerHTML = '<div class="chat-empty">No sessions yet. Click <strong>+ New</strong> to start.</div>';
            return;
        }
        var html = '';
        for (var i = 0; i < state.sessions.length; i++) {
            var s = state.sessions[i];
            var active = s.session_id === state.currentId ? ' active' : '';
            var display = App.modelDisplay(s.model);
            var title = s.metadata && s.metadata.title
                ? s.metadata.title
                : (display + ' · ' + (s.message_count || 0) + ' msg');
            html += '<div class="chat-session-row' + active + '" data-sid="' + App.esc(s.session_id) + '">'
                + '<div class="chat-session-title">' + App.esc(title) + '</div>'
                + '<div class="chat-session-meta">' + App.esc(display) + ' · ' + fmtRel(s.last_activity) + '</div>'
                + '<div class="chat-session-actions">'
                +   '<button class="btn btn-sm btn-danger chat-session-del" data-sid="' + App.esc(s.session_id) + '">Delete</button>'
                + '</div>'
                + '</div>';
        }
        body.innerHTML = html;
        body.querySelectorAll('.chat-session-row').forEach(function(row) {
            row.addEventListener('click', function() {
                selectSession(row.dataset.sid);
            });
        });
        // Delete buttons: stop the row-select click from also firing.
        body.querySelectorAll('.chat-session-del').forEach(function(btn) {
            btn.addEventListener('click', function(e) {
                e.stopPropagation();
                deleteSession(btn.dataset.sid);
            });
        });
    }

    function renderThread() {
        var body = document.getElementById('chat-thread-body');
        var title = document.getElementById('chat-thread-title');
        var meta = document.getElementById('chat-thread-meta');
        if (!body) return;
        if (!state.currentId || !state.currentSession) {
            title.textContent = 'Pick or create a session';
            meta.textContent = '';
            body.innerHTML = '<div class="chat-empty">Select a session on the left, or click <strong>+ New</strong>.</div>';
            return;
        }
        var s = state.currentSession;
        title.textContent = App.modelDisplay(s.model) || 'session';
        meta.textContent = (s.messages || []).length + ' / 100 messages';
        var msgs = s.messages || [];
        if (!msgs.length) {
            body.innerHTML = '<div class="chat-empty">No messages yet. Type below to start.</div>';
            return;
        }
        var html = '';
        for (var i = 0; i < msgs.length; i++) {
            html += renderMessage(msgs[i]);
        }
        body.innerHTML = html;
        // Scroll to bottom
        body.scrollTop = body.scrollHeight;
        // Wire up image-click to open in Media tab's lightbox style (or new tab fallback)
        body.querySelectorAll('.chat-msg-attachment img').forEach(function(img) {
            img.addEventListener('click', function() {
                window.open(img.src, '_blank');
            });
        });
    }

    function renderMessage(m, opts) {
        opts = opts || {};
        var role = m.role || 'unknown';
        var roleCls = 'role-' + role;
        var roleLabel = role.charAt(0).toUpperCase() + role.slice(1);
        var streamingCls = opts.streaming ? ' streaming' : '';
        var attachments = '';
        if (m.image) {
            var src = 'data:' + (m.image_mime || opts.imageMime || 'image/png') + ';base64,' + m.image;
            attachments += '<div class="chat-msg-attachment"><img src="' + App.esc(src) + '" alt=""></div>';
        }
        if (m.audio) {
            var asrc = 'data:' + (m.audio_mime || opts.audioMime || 'audio/wav') + ';base64,' + m.audio;
            attachments += '<div class="chat-msg-attachment"><audio controls src="' + App.esc(asrc) + '"></audio></div>';
        }
        if (m.video) {
            var vsrc = 'data:' + (m.video_mime || opts.videoMime || 'video/mp4') + ';base64,' + m.video;
            attachments += '<div class="chat-msg-attachment"><video controls src="' + App.esc(vsrc) + '"></video></div>';
        }
        var attachWrap = attachments ? '<div class="chat-msg-attachments">' + attachments + '</div>' : '';
        var idAttr = opts.idAttr ? ' id="' + App.esc(opts.idAttr) + '"' : '';
        return '<div class="chat-msg ' + App.esc(roleCls) + '"' + idAttr + '>'
            + '<div class="chat-msg-role">' + App.esc(roleLabel) + '</div>'
            + '<div class="chat-msg-text' + streamingCls + '">' + escMd(m.content || '') + '</div>'
            + attachWrap
            + '</div>';
    }

    function renderAttachments() {
        var row = document.getElementById('chat-attach-row');
        if (!row) return;
        var chips = [];
        ['image', 'audio', 'video'].forEach(function(slot) {
            var rec = state.attachments[slot];
            if (rec) {
                chips.push('<span class="chat-attach-chip">' + slot + ': '
                    + App.esc(rec.name || '(file)')
                    + ' <button data-slot="' + slot + '" title="Remove">✕</button></span>');
            }
        });
        row.innerHTML = chips.join('');
        row.querySelectorAll('button').forEach(function(b) {
            b.addEventListener('click', function() {
                state.attachments[b.dataset.slot] = null;
                renderAttachments();
            });
        });
        applyCapabilityGating();
    }

    // Disable attachment buttons whose modality the active model can't accept,
    // and explain WHY inline (a single compose-row hint) instead of waiting for
    // a click to fire an alert.
    function applyCapabilityGating() {
        var model = activeModel();
        var caps = model ? modelCaps(model) : null;
        var display = model ? App.modelDisplay(model) : 'this model';
        var unsupported = [];
        ['image', 'audio', 'video'].forEach(function(slot) {
            var btn = document.getElementById('chat-attach-' + slot);
            if (!btn) return;
            var allowed = !caps || caps[slot];
            btn.disabled = !allowed;
            btn.classList.toggle('chat-attach-disabled', !allowed);
            btn.title = allowed
                ? 'Attach ' + slot
                : App.modelDisplay(model) + ' does not accept ' + slot + ' input';
            if (!allowed) unsupported.push(slot);
        });
        var hint = document.getElementById('chat-attach-hint');
        if (hint) {
            if (unsupported.length) {
                hint.style.display = '';
                hint.textContent = display + ' is text'
                    + (caps && caps.image ? ' + vision' : '')
                    + (caps && caps.audio ? ' + audio' : '')
                    + (caps && caps.video ? ' + video' : '')
                    + ' only — ' + unsupported.join(', ') + ' attachment'
                    + (unsupported.length > 1 ? 's are' : ' is') + ' disabled for it.';
            } else {
                hint.style.display = 'none';
                hint.textContent = '';
            }
        }
    }

    // ----------------------------------------------------------------------
    // Prerequisite banner — when no worker is ready for the active session's
    // model, surface a one-click "Spawn worker" and block Send.
    // ----------------------------------------------------------------------
    function renderPrereq() {
        var host = document.getElementById('chat-prereq');
        var sendBtn = document.getElementById('chat-send-btn');
        if (!host) return;

        var model = state.currentSession && state.currentSession.model;
        if (!model) {
            host.innerHTML = '';
            if (sendBtn) {
                sendBtn.disabled = true;
                sendBtn.title = 'Select or create a chat session first';
            }
            return;
        }
        if (App.workerReadyFor(model)) {
            host.innerHTML = '';
            if (sendBtn) {
                sendBtn.disabled = state.streaming;
                sendBtn.title = '';
            }
            return;
        }

        var display = App.modelDisplay(model);
        var msg = state.spawning
            ? 'Starting worker for ' + display + ' — this can take a minute…'
            : 'No worker running for ' + display + ' — start one to send messages.';
        host.innerHTML = App.prereqBanner({
            level: 'warn',
            text: msg,
            actionId: 'chat-spawn-worker',
            actionLabel: state.spawning ? 'Starting…' : 'Spawn worker',
            actionDisabled: state.spawning,
        });
        var spawnBtn = document.getElementById('chat-spawn-worker');
        if (spawnBtn) {
            spawnBtn.addEventListener('click', async function() {
                if (state.spawning) return;
                state.spawning = true;
                renderPrereq();
                try {
                    await App.spawnWorker(model);
                } catch (e) {
                    // App.spawnWorker already toasts on failure.
                } finally {
                    state.spawning = false;
                    renderPrereq();
                }
            });
        }
        if (sendBtn) {
            sendBtn.disabled = true;
            sendBtn.title = 'Start a worker for ' + display + ' first';
        }
    }

    function renderSidePanel() {
        var body = document.getElementById('chat-side-body');
        if (!body) return;

        var models = chatableModels();
        var modelOptions = models.map(function(m) {
            return {
                value: m,
                label: App.modelDisplay(m) + '  (' + capabilityHint(m) + ')',
            };
        });

        var html = ''
            + App.field({
                type: 'select',
                id: 'chat-side-model',
                label: 'Model for next new session',
                hint: 'Used when you click + New. Each session is locked to one model. Moshi is voice-only and not chattable here.',
                value: state.model || (models.length ? models[0] : ''),
                options: modelOptions,
                disabled: !models.length,
            })
            + App.field({
                type: 'textarea',
                id: 'chat-side-system',
                label: 'System prompt',
                hint: 'Optional persona/instructions baked into new sessions. Applied at + New time only.',
                rows: 3,
                value: state.system,
                placeholder: 'e.g. You are a concise, helpful assistant.',
            })
            + App.field({
                type: 'slider',
                id: 'chat-side-temp',
                label: 'Temperature',
                tip: 'Randomness of sampling. Low = focused/deterministic, high = creative/varied.',
                hint: 'Higher is more creative; lower is more focused. Applies to your next send.',
                min: 0, max: 2, step: 0.05, value: state.temperature,
            })
            + App.field({
                type: 'slider',
                id: 'chat-side-topp',
                label: 'Top-p',
                tip: 'Nucleus sampling: only sample from the smallest set of tokens whose probabilities sum to p.',
                hint: 'Caps the sampling pool by cumulative probability. 1.0 = no cap.',
                min: 0, max: 1, step: 0.05, value: state.topP,
            })
            + App.field({
                type: 'slider',
                id: 'chat-side-maxtok',
                label: 'Max new tokens',
                tip: 'Upper bound on how many tokens the model may generate in one reply.',
                hint: 'Longest possible reply length. Higher = longer answers but slower.',
                min: 32, max: 4096, step: 32, unit: ' tok', value: state.maxNewTokens,
            })
            + '<div class="chat-side-note">'
            +   'Sessions are kept in-memory. They expire after 24h idle, or '
            +   'when the gateway restarts. History is capped at 100 messages '
            +   '(oldest non-system messages get trimmed).'
            + '</div>';
        body.innerHTML = html;

        // App.field sliders auto-update their value badge via app.js's global
        // listener; here we only persist the value back into state.
        var modelEl = document.getElementById('chat-side-model');
        if (modelEl) {
            modelEl.addEventListener('change', function(e) {
                state.model = e.target.value; savePrefs();
                // Capability gating + prereq follow the picked model when no
                // session is open yet.
                applyCapabilityGating();
                renderPrereq();
            });
        }
        document.getElementById('chat-side-system').addEventListener('input', function(e) {
            state.system = e.target.value; savePrefs();
        });
        document.getElementById('chat-side-temp').addEventListener('input', function(e) {
            state.temperature = parseFloat(e.target.value); savePrefs();
        });
        document.getElementById('chat-side-topp').addEventListener('input', function(e) {
            state.topP = parseFloat(e.target.value); savePrefs();
        });
        document.getElementById('chat-side-maxtok').addEventListener('input', function(e) {
            state.maxNewTokens = parseInt(e.target.value, 10); savePrefs();
        });
    }

    // ----------------------------------------------------------------------
    // Data ops
    // ----------------------------------------------------------------------
    async function loadSessions() {
        if (state.loadingSessions) return;
        state.loadingSessions = true;
        renderSessionsPanel();
        try {
            var data = await App.api('GET', '/api/chat/sessions');
            state.sessions = (data && data.sessions) || [];
        } catch (e) {
            App.toast('Failed to list sessions: ' + e.message, 'error');
        } finally {
            state.loadingSessions = false;
            renderSessionsPanel();
        }
    }

    async function selectSession(id) {
        if (state.streaming) {
            if (!confirm('A response is still streaming. Switch session anyway?')) return;
            abortStream();
        }
        var generation = ++state.threadGeneration;
        state.currentId = id;
        state.currentSession = null;
        // Pending attachments belong to the compose-state of the
        // previous session — clear so they don't get sent to the wrong one.
        state.attachments = { image: null, audio: null, video: null };
        renderSessionsPanel();
        renderThread();
        renderAttachments();
        renderPrereq();
        try {
            state.loadingThread = true;
            var snap = await App.api('GET', '/api/chat/sessions/' + encodeURIComponent(id));
            if (generation !== state.threadGeneration || id !== state.currentId) return;
            state.currentSession = snap;
        } catch (e) {
            if (generation !== state.threadGeneration || id !== state.currentId) return;
            App.toast('Failed to load session: ' + e.message, 'error');
            state.currentSession = null;
        } finally {
            if (generation !== state.threadGeneration || id !== state.currentId) return;
            state.loadingThread = false;
            renderThread();
            applyCapabilityGating();
            renderPrereq();
        }
    }

    async function createSessionFlow() {
        var model = state.model || chatableModels()[0];
        if (!model) {
            App.toast('No models available — install one in Setup first', 'error');
            return;
        }
        var system = (state.system || '').trim() || null;
        try {
            var data = await App.api('POST', '/api/chat/sessions', {
                model: model,
                system: system,
                metadata: { title: 'Chat ' + new Date().toLocaleString() },
            });
            await loadSessions();
            await selectSession(data.session_id);
        } catch (e) {
            App.toast('Create session failed: ' + e.message, 'error');
        }
    }

    async function deleteSession(id) {
        if (!confirm('Delete this session? History will be lost.')) return;
        try {
            await App.api('DELETE', '/api/chat/sessions/' + encodeURIComponent(id));
            if (id === state.currentId) {
                state.currentId = null;
                state.currentSession = null;
            }
            await loadSessions();
            renderThread();
            renderPrereq();
        } catch (e) {
            App.toast('Delete failed: ' + e.message, 'error');
        }
    }

    async function sendCurrent() {
        if (state.streaming) return;
        if (!state.currentId) {
            App.toast('Pick or create a session first', 'info');
            return;
        }
        // Prereq gate: never submit into a "no worker" backend error. The
        // banner + disabled Send already communicate this, but guard anyway.
        var sessionModel = state.currentSession && state.currentSession.model;
        if (sessionModel && !App.workerReadyFor(sessionModel)) {
            App.toast('No worker ready for ' + App.modelDisplay(sessionModel) + ' — spawn one first', 'info');
            renderPrereq();
            return;
        }
        var ta = document.getElementById('chat-input');
        var text = (ta.value || '').trim();
        var att = state.attachments;
        if (!text && !att.image && !att.audio && !att.video) return;

        var body = {
            content: text,
            image: att.image ? att.image.b64 : null,
            audio: att.audio ? att.audio.b64 : null,
            video: att.video ? att.video.b64 : null,
            max_new_tokens: state.maxNewTokens,
            temperature: state.temperature,
            top_p: state.topP,
        };

        // Optimistically render the user message and a streaming placeholder.
        if (!state.currentSession) state.currentSession = { messages: [] };
        var userMsg = {
            role: 'user', content: text,
            image: body.image, audio: body.audio, video: body.video,
            image_mime: att.image ? att.image.mime : null,
            audio_mime: att.audio ? att.audio.mime : null,
            video_mime: att.video ? att.video.mime : null,
            created_at: Date.now() / 1000,
        };
        state.currentSession.messages = (state.currentSession.messages || []).concat([userMsg]);

        var asstMsg = { role: 'assistant', content: '', created_at: Date.now() / 1000 };
        state.currentSession.messages.push(asstMsg);

        renderThread();
        var threadBody = document.getElementById('chat-thread-body');
        // Re-mark the last message with the streaming class via a fresh render
        // We'll mutate its text node directly during the stream for perf.
        var lastMsgEls = threadBody.querySelectorAll('.chat-msg');
        var streamEl = lastMsgEls[lastMsgEls.length - 1];
        var streamTextEl = streamEl ? streamEl.querySelector('.chat-msg-text') : null;
        if (streamTextEl) streamTextEl.classList.add('streaming');

        // Clear compose
        ta.value = '';
        state.attachments = { image: null, audio: null, video: null };
        renderAttachments();

        // Switch send → abort
        state.streaming = true;
        state.streamingJobId = null;
        document.getElementById('chat-send-btn').style.display = 'none';
        document.getElementById('chat-abort-btn').style.display = '';

        var accumulated = '';
        var streamSessionId = state.currentId;
        var streamModel = state.currentSession.model;
        var streamJobId = null;
        var streamGeneration = ++state.streamGeneration;
        state.streamAbort = App.streamSSE(
            '/api/chat/sessions/' + encodeURIComponent(streamSessionId) + '/messages/stream',
            body,
            {
                onEvent: function(evt) {
                    if (evt.job_id) {
                        streamJobId = evt.job_id;
                        if (streamGeneration === state.streamGeneration) state.streamingJobId = evt.job_id;
                        return;
                    }
                    if (evt.delta != null) {
                        accumulated += evt.delta;
                        if (streamTextEl) {
                            streamTextEl.innerHTML = escMd(accumulated);
                            threadBody.scrollTop = threadBody.scrollHeight;
                        }
                        asstMsg.content = accumulated;
                    } else if (evt.error) {
                        App.toast('Stream error: ' + evt.error, 'error');
                    } else if (evt.cancelled) {
                        App.toast('Stream cancelled', 'info');
                    }
                },
                onError: function(err) {
                    App.toast('Stream failed: ' + err.message, 'error');
                    finishStreaming(streamGeneration);
                },
                onAbort: function() {
                    // User-initiated abort; if we have a job_id, ask the
                    // gateway to cancel it server-side too so the worker
                    // doesn't keep generating.
                    if (streamJobId) {
                        App.api('POST', '/api/chat/' + encodeURIComponent(streamModel)
                            + '/cancel/' + encodeURIComponent(streamJobId)).catch(function() {});
                    }
                    finishStreaming(streamGeneration);
                },
                onDone: function() {
                    finishStreaming(streamGeneration);
                    // Skip refreshCurrentThread() here — the server's
                    // append_message runs in the stream's `finally` block
                    // AFTER it sends `done` to us, so a refresh GET can
                    // race in and overwrite our optimistic assistant
                    // bubble with a snapshot that doesn't include it.
                    // Our optimistic state already has the right content.
                    // Just bump the sidebar's last-activity.
                    loadSessions();
                },
            }
        );
    }

    function finishStreaming(generation) {
        if (generation != null && generation !== state.streamGeneration) return;
        state.streaming = false;
        state.streamAbort = null;
        state.streamingJobId = null;
        var sb = document.getElementById('chat-send-btn');
        var ab = document.getElementById('chat-abort-btn');
        if (sb) sb.style.display = '';
        if (ab) ab.style.display = 'none';
        var body = document.getElementById('chat-thread-body');
        if (body) {
            body.querySelectorAll('.chat-msg-text.streaming').forEach(function(t) {
                t.classList.remove('streaming');
            });
        }
        // Re-evaluate the Send button's enabled state / prereq banner now that
        // streaming has stopped.
        renderPrereq();
    }

    function abortStream() {
        if (!state.streaming || !state.streamAbort) return;
        state.streamAbort();
    }

    async function refreshCurrentThread() {
        if (!state.currentId) return;
        var id = state.currentId;
        var generation = ++state.threadGeneration;
        try {
            var snap = await App.api('GET', '/api/chat/sessions/' + encodeURIComponent(id));
            if (id !== state.currentId || generation !== state.threadGeneration) return;
            state.currentSession = snap;
            renderThread();
        } catch (e) {
            if (id !== state.currentId || generation !== state.threadGeneration) return;
            // Session may have been evicted (TTL) — clear.
            state.currentId = null;
            state.currentSession = null;
            renderThread();
        }
    }

    // ----------------------------------------------------------------------
    // Public surface
    // ----------------------------------------------------------------------
    return {
        init: function() {
            loadPrefs();
            render();
        },
        onActivate: function() {
            render();
            loadSessions();
        },
        deleteSession: deleteSession,
    };
})();
