/* ==========================================================================
   Tab: Media — Output browser, multi-select, lightbox player
   ==========================================================================

   Lists media under OUTPUT_DIR/comfyui (and optionally comfyui_input or the
   comfyui_temp cache) via /api/outputs. Supports:

   * filter chips (image/video/audio/data), source dropdown, search by
     filename prefix, prompt_id filter, tag/collection/pinned filters,
     date-since, sort by date/size/name
   * grid + list views with multi-select (shift-range, ctrl-toggle, all/none)
   * bulk export (POST /api/outputs/zip), delete, pin, tag, collection
   * lightbox with custom transport for image (zoom/pan/rotate),
     video (rate, loop, PiP, fullscreen, frame step) and audio (rate, loop,
     mute, scrub). Reads embedded PNG metadata (prompt + workflow JSON).
   ========================================================================== */

var TabMedia = (function() {
    var LS_KEY = 'omniMediaPrefs.v1';

    var state = {
        source: 'output',          // output | input | temp
        mediaKind: '',             // '' | image | video | audio | data | other
        search: '',                // filename prefix
        promptId: '',              // ?prompt_id= filter
        tag: '',                   // single-tag filter
        collection: '',            // collection name filter
        pinned: '',                // '' | 'true' | 'false'
        sinceDays: 0,              // 0 = no filter; otherwise N days
        sortBy: 'mtime-desc',      // mtime-desc | mtime-asc | size-desc | name-asc
        view: 'grid',              // grid | list
        autoRefresh: false,
        autoplayBrowse: false,
        files: [],
        total: 0,
        collections: [],
        loading: false,
        requestGeneration: 0,
        nextOffset: null,
        listQuery: "",
        lastFetchAt: 0,
        selection: {},             // map of relpath -> true
        lastClickedIndex: -1,
        lightboxIndex: -1,         // index into the visible files array, -1 = closed
        bound: false,
    };

    var refreshTimer = null;

    // ----------------------------------------------------------------------
    // Persistence
    // ----------------------------------------------------------------------
    function loadPrefs() {
        try {
            var raw = localStorage.getItem(LS_KEY);
            if (!raw) return;
            var p = JSON.parse(raw);
            ['source', 'mediaKind', 'sortBy', 'view'].forEach(function(k) {
                if (p[k] != null) state[k] = p[k];
            });
            if (p.autoRefresh != null) state.autoRefresh = !!p.autoRefresh;
            if (p.autoplayBrowse != null) state.autoplayBrowse = !!p.autoplayBrowse;
        } catch (e) {}
    }
    function savePrefs() {
        try {
            localStorage.setItem(LS_KEY, JSON.stringify({
                source: state.source, mediaKind: state.mediaKind,
                sortBy: state.sortBy, view: state.view,
                autoRefresh: state.autoRefresh,
                autoplayBrowse: state.autoplayBrowse,
            }));
        } catch (e) {}
    }

    // ----------------------------------------------------------------------
    // Helpers
    // ----------------------------------------------------------------------
    function fmtBytes(n) {
        if (n == null) return '';
        if (n < 1024) return n + ' B';
        var u = ['KB', 'MB', 'GB', 'TB']; var v = n / 1024; var i = 0;
        while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
        return v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2) + ' ' + u[i];
    }
    function fmtRel(t) {
        if (!t) return '';
        var s = (Date.now() / 1000) - t;
        if (s < 60) return Math.round(s) + 's ago';
        if (s < 3600) return Math.round(s / 60) + 'm ago';
        if (s < 86400) return Math.round(s / 3600) + 'h ago';
        if (s < 30 * 86400) return Math.round(s / 86400) + 'd ago';
        return new Date(t * 1000).toLocaleDateString();
    }
    function fmtTime(s) {
        if (!isFinite(s) || s < 0) return '0:00';
        var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
        var mm = m < 10 && h > 0 ? '0' + m : '' + m;
        var ss = sec < 10 ? '0' + sec : '' + sec;
        return (h > 0 ? h + ':' + mm : m + '') + ':' + ss;
    }
    function basename(p) {
        var i = p.lastIndexOf('/'); return i >= 0 ? p.substring(i + 1) : p;
    }
    /** Percent-encode each path segment but keep slashes literal so the
     *  ``{relpath:path}`` route sees the segments correctly. Filenames can
     *  contain '?', '#', '+', '%', spaces — concatenating raw is broken. */
    function encPath(p) {
        return String(p || '').split('/').map(encodeURIComponent).join('/');
    }
    function kindIcon(k) {
        return k === 'image' ? '🖼️' : k === 'video' ? '🎬' :
               k === 'audio' ? '🔊' : k === 'data' ? '📄' : '📎';
    }
    function kindBadge(k) {
        return '<span class="badge badge-' + (
            k === 'image' ? 'blue' : k === 'video' ? 'purple' :
            k === 'audio' ? 'orange' : 'gray'
        ) + '">' + k + '</span>';
    }

    function mediaUrl(rec, opts) {
        opts = opts || {};
        var qs = 'kind=' + encodeURIComponent(rec.storage_kind || state.source);
        if (opts.thumb) qs += '&thumb=1&w=' + (opts.w || 240);
        if (opts.download) qs += '&download=1';
        return App.urlWithToken('/api/outputs/' + encPath(rec.path) + '?' + qs);
    }

    // ----------------------------------------------------------------------
    // Input modal (self-contained replacement for browser prompt(), reusing
    // the shared .modal-backdrop / .modal / .modal-header CSS). Resolves to the
    // entered string (untrimmed — callers trim) or null on cancel/dismiss.
    // ----------------------------------------------------------------------
    function inputModal(opts) {
        opts = opts || {};
        return new Promise(function(resolve) {
            // Tear down any stray instance from a previous (e.g. rapid) call.
            var prev = document.getElementById('media-input-modal');
            if (prev) prev.remove();

            var settled = false;
            var backdrop = document.createElement('div');
            backdrop.id = 'media-input-modal';
            backdrop.className = 'modal-backdrop';

            var modal = document.createElement('div');
            modal.className = 'modal';
            modal.style.width = 'min(440px, 96vw)';

            var header = document.createElement('div');
            header.className = 'modal-header';
            var h = document.createElement('h2');
            h.textContent = opts.title || 'Enter value';
            var x = document.createElement('button');
            x.className = 'btn btn-sm';
            x.textContent = '✕';
            header.appendChild(h);
            header.appendChild(x);

            var body = document.createElement('div');
            body.style.padding = '14px';
            body.style.display = 'flex';
            body.style.flexDirection = 'column';
            body.style.gap = '10px';

            if (opts.hint) {
                var hint = document.createElement('div');
                hint.style.color = 'var(--text-secondary)';
                hint.style.fontSize = '12px';
                hint.textContent = opts.hint;
                body.appendChild(hint);
            }

            var input = document.createElement('input');
            input.type = 'text';
            input.style.width = '100%';
            if (opts.placeholder) input.placeholder = opts.placeholder;
            // Default value set via .value (never interpolated into HTML).
            input.value = opts.value != null ? String(opts.value) : '';
            body.appendChild(input);

            var actions = document.createElement('div');
            actions.style.display = 'flex';
            actions.style.gap = '8px';
            actions.style.justifyContent = 'flex-end';
            var cancelBtn = document.createElement('button');
            cancelBtn.className = 'btn btn-sm';
            cancelBtn.textContent = 'Cancel';
            var saveBtn = document.createElement('button');
            saveBtn.className = 'btn btn-sm btn-primary';
            saveBtn.textContent = opts.saveLabel || 'Save';
            actions.appendChild(cancelBtn);
            actions.appendChild(saveBtn);
            body.appendChild(actions);

            modal.appendChild(header);
            modal.appendChild(body);
            backdrop.appendChild(modal);

            function done(val) {
                if (settled) return;
                settled = true;
                document.removeEventListener('keydown', onKey, true);
                backdrop.remove();
                resolve(val);
            }
            function onKey(ev) {
                if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); done(null); }
                else if (ev.key === 'Enter') { ev.preventDefault(); ev.stopPropagation(); done(input.value); }
            }

            x.addEventListener('click', function() { done(null); });
            cancelBtn.addEventListener('click', function() { done(null); });
            saveBtn.addEventListener('click', function() { done(input.value); });
            backdrop.addEventListener('click', function(ev) { if (ev.target === backdrop) done(null); });
            // Capture phase so Enter/Escape here don't also trigger the
            // lightbox key handler bound on document.
            document.addEventListener('keydown', onKey, true);

            document.body.appendChild(backdrop);
            input.focus();
            input.select();
        });
    }

    // ----------------------------------------------------------------------
    // API
    // ----------------------------------------------------------------------
    function buildListQuery() {
        var order = {'mtime-desc': 'newest', 'mtime-asc': 'oldest', 'size-desc': 'size', 'name-asc': 'name'};
        var p = ['kind=' + encodeURIComponent(state.source), 'limit=500', 'sort=' + (order[state.sortBy] || 'newest')];
        if (state.mediaKind) p.push('media_kind=' + encodeURIComponent(state.mediaKind));
        if (state.search) p.push('prefix=' + encodeURIComponent(state.search));
        if (state.promptId) p.push('prompt_id=' + encodeURIComponent(state.promptId));
        if (state.tag) p.push('tag=' + encodeURIComponent(state.tag));
        if (state.collection) p.push('collection=' + encodeURIComponent(state.collection));
        if (state.pinned === 'true') p.push('pinned=true');
        if (state.pinned === 'false') p.push('pinned=false');
        if (state.sinceDays > 0) {
            var since = (Date.now() / 1000) - (state.sinceDays * 86400);
            p.push('since=' + since.toFixed(0));
        }
        return p.join('&');
    }

    function sortFiles(files) {
        var arr = files.slice();
        if (state.sortBy === 'mtime-desc') arr.sort(function(a, b) { return b.mtime - a.mtime; });
        else if (state.sortBy === 'mtime-asc') arr.sort(function(a, b) { return a.mtime - b.mtime; });
        else if (state.sortBy === 'size-desc') arr.sort(function(a, b) { return b.size - a.size; });
        else if (state.sortBy === 'name-asc') arr.sort(function(a, b) {
            return basename(a.path).localeCompare(basename(b.path));
        });
        return arr;
    }

    async function refresh(append) {
        append = append === true;
        if (append && (state.loading || state.nextOffset == null)) return;
        var generation = ++state.requestGeneration;
        var source = state.source;
        var query = append ? state.listQuery : buildListQuery();
        var offset = append ? state.nextOffset : 0;
        if (!append && query !== state.listQuery) {
            state.files = []; state.selection = {}; state.total = 0; state.nextOffset = null;
            closeLightbox(); renderResults();
        }
        state.listQuery = query;
        state.loading = true;
        renderToolbar();
        try {
            var data = await App.api('GET', '/api/outputs?' + query + '&offset=' + offset);
            if (generation !== state.requestGeneration || source !== state.source) return;
            var rows = (data.files || []).map(function(f) { f.storage_kind = source; return f; });
            state.files = append ? state.files.concat(rows) : rows;
            state.nextOffset = data.next_offset == null ? null : data.next_offset;
            state.total = data.total || 0;
            state.lastFetchAt = Date.now();
            // prune selection of any files that vanished
            var keep = {};
            for (var i = 0; i < state.files.length; i++) {
                if (state.selection[state.files[i].path]) keep[state.files[i].path] = true;
            }
            state.selection = keep;
            try {
                var c = await App.api('GET', '/api/outputs/collections');
                if (generation === state.requestGeneration) state.collections = (c && c.collections) || [];
            } catch (e) {}
        } catch (err) {
            if (generation !== state.requestGeneration) return;
            App.toast('Failed to list outputs: ' + err.message, 'error');
        } finally {
            if (generation === state.requestGeneration) {
                state.loading = false;
                renderToolbar();
                renderResults();
            }
        }
    }

    // ----------------------------------------------------------------------
    // Render
    // ----------------------------------------------------------------------
    function renderTab() {
        var el = document.getElementById('tab-media');
        if (!el) return;
        if (state.bound && el.querySelector('.media-root')) {
            // Already rendered; just refresh dynamic parts.
            renderToolbar();
            renderResults();
            return;
        }
        el.innerHTML = ''
            + '<div class="media-root">'
            +   '<div class="media-filterbar card">'
            +     '<div class="media-filter-row">'
            +       '<label class="media-lbl">Source'
            +         '<select id="m-source">'
            +           '<option value="output">ComfyUI generated</option>'
            +           '<option value="input">ComfyUI inputs</option>'
            +           '<option value="temp">ComfyUI temp</option>'
            +           '<option value="omni">Omni (TTS / STT)</option>'
            +         '</select>'
            +       '</label>'
            +       '<div class="media-kind-chips" id="m-kind-chips">'
            +         '<button data-kind="" class="chip">All</button>'
            +         '<button data-kind="image" class="chip">🖼️ Images</button>'
            +         '<button data-kind="video" class="chip">🎬 Video</button>'
            +         '<button data-kind="audio" class="chip">🔊 Audio</button>'
            +         '<button data-kind="data"  class="chip">📄 Data</button>'
            +         '<button data-kind="other" class="chip">📎 Other</button>'
            +       '</div>'
            +       '<input id="m-search" type="text" placeholder="Filename starts with..." class="media-search">'
            +       '<select id="m-sort">'
            +         '<option value="mtime-desc">Newest first</option>'
            +         '<option value="mtime-asc">Oldest first</option>'
            +         '<option value="size-desc">Largest first</option>'
            +         '<option value="name-asc">Name A→Z</option>'
            +       '</select>'
            +       '<select id="m-since">'
            +         '<option value="0">Any time</option>'
            +         '<option value="1">Last 24h</option>'
            +         '<option value="7">Last 7 days</option>'
            +         '<option value="30">Last 30 days</option>'
            +       '</select>'
            +       '<select id="m-pinned">'
            +         '<option value="">Pinned: any</option>'
            +         '<option value="true">Pinned only</option>'
            +         '<option value="false">Unpinned only</option>'
            +       '</select>'
            +       '<select id="m-collection">'
            +         '<option value="">Collection: any</option>'
            +       '</select>'
            +       '<input id="m-tag" type="text" placeholder="Tag" class="media-search media-search-narrow">'
            +       '<span class="media-lbl">'
            +         '<input id="m-prompt" type="text" placeholder="Prompt ID" class="media-search media-search-narrow">'
            +         App.tip('The ComfyUI prompt id assigned when a graph is queued — shown in the Workflows tab and in PNG metadata. Filters to outputs from that one run.')
            +       '</span>'
            +       '<div class="media-view-toggle">'
            +         '<button id="m-view-grid" class="btn btn-sm">Grid</button>'
            +         '<button id="m-view-list" class="btn btn-sm">List</button>'
            +       '</div>'
            +       '<button id="m-refresh" class="btn btn-sm" title="Refresh (R)">↻ Refresh</button>'
            +       '<label class="media-checkbox" title="Auto-refresh every 10s"><input type="checkbox" id="m-autorefresh"> auto</label>'
            +       '<label class="media-checkbox" title="Play audio and video automatically when opening or browsing"><input type="checkbox" id="m-autoplay"> autoplay media</label>'
            +     '</div>'
            +   '</div>'
            +   '<div id="m-toolbar" class="media-toolbar"></div>'
            +   '<div id="m-results"></div>'
            + '</div>';

        // Set initial select/input values from state
        document.getElementById('m-source').value = state.source;
        document.getElementById('m-sort').value = state.sortBy;
        document.getElementById('m-pinned').value = state.pinned;
        document.getElementById('m-since').value = String(state.sinceDays);
        document.getElementById('m-search').value = state.search;
        document.getElementById('m-tag').value = state.tag;
        document.getElementById('m-prompt').value = state.promptId;
        document.getElementById('m-autorefresh').checked = state.autoRefresh;
        document.getElementById('m-autoplay').checked = state.autoplayBrowse;

        bindFilterEvents();
        applyKindChipState();
        applyViewToggleState();
        renderCollectionOptions();
        renderToolbar();
        renderResults();
        state.bound = true;
    }

    function applyKindChipState() {
        var chips = document.querySelectorAll('#m-kind-chips .chip');
        chips.forEach(function(c) {
            c.classList.toggle('chip-active', (c.dataset.kind || '') === state.mediaKind);
        });
    }
    function applyViewToggleState() {
        var g = document.getElementById('m-view-grid'), l = document.getElementById('m-view-list');
        if (g) g.classList.toggle('btn-primary', state.view === 'grid');
        if (l) l.classList.toggle('btn-primary', state.view === 'list');
    }
    function renderCollectionOptions() {
        var sel = document.getElementById('m-collection');
        if (!sel) return;
        var current = state.collection;
        sel.innerHTML = '<option value="">Collection: any</option>'
            + state.collections.map(function(c) {
                return '<option value="' + App.esc(c.name) + '"' +
                    (c.name === current ? ' selected' : '') + '>' +
                    App.esc(c.name) + ' (' + c.count + ')</option>';
            }).join('');
        sel.value = current;
    }

    function bindFilterEvents() {
        var bind = function(id, ev, fn) {
            var el = document.getElementById(id); if (el) el.addEventListener(ev, fn);
        };
        // Anything that reorders or refilters the visible list invalidates
        // the shift-range anchor. Reset before fetching/re-sorting so the
        // next shift-click range starts fresh.
        bind('m-source', 'change', function(e) {
            state.source = e.target.value; state.selection = {};
            state.lastClickedIndex = -1; savePrefs(); refresh();
        });
        bind('m-sort', 'change', function(e) {
            state.sortBy = e.target.value; state.lastClickedIndex = -1; savePrefs();
            refresh();
        });
        bind('m-pinned', 'change', function(e) {
            state.pinned = e.target.value; state.lastClickedIndex = -1; refresh();
        });
        bind('m-since', 'change', function(e) {
            state.sinceDays = parseInt(e.target.value, 10) || 0; state.lastClickedIndex = -1; refresh();
        });
        bind('m-collection', 'change', function(e) {
            state.collection = e.target.value; state.lastClickedIndex = -1; refresh();
        });

        var debounceTimer = null;
        var debouncedRefresh = function() {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(function() {
                state.lastClickedIndex = -1; refresh();
            }, 250);
        };
        bind('m-search', 'input', function(e) { state.search = e.target.value; debouncedRefresh(); });
        bind('m-tag', 'input', function(e) { state.tag = e.target.value; debouncedRefresh(); });
        bind('m-prompt', 'input', function(e) { state.promptId = e.target.value; debouncedRefresh(); });

        bind('m-refresh', 'click', refresh);
        bind('m-autorefresh', 'change', function(e) {
            state.autoRefresh = e.target.checked; savePrefs();
            scheduleAutoRefresh();
        });
        bind('m-autoplay', 'change', function(e) {
            state.autoplayBrowse = e.target.checked; savePrefs();
        });
        bind('m-view-grid', 'click', function() {
            state.view = 'grid'; savePrefs(); applyViewToggleState(); renderResults();
        });
        bind('m-view-list', 'click', function() {
            state.view = 'list'; savePrefs(); applyViewToggleState(); renderResults();
        });

        document.querySelectorAll('#m-kind-chips .chip').forEach(function(c) {
            c.addEventListener('click', function() {
                state.mediaKind = c.dataset.kind || '';
                state.lastClickedIndex = -1;
                applyKindChipState(); refresh();
            });
        });
    }

    function scheduleAutoRefresh() {
        if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
        if (state.autoRefresh) {
            refreshTimer = setInterval(function() {
                // Skip while the lightbox is open — a refetch could reorder
                // state.files and the user's lightboxIndex would land on a
                // different file mid-view.
                if (state.lightboxIndex >= 0) return;
                var tab = document.getElementById('tab-media');
                if (tab && tab.classList.contains('active')) refresh();
            }, 10000);
        }
    }

    function selectionList() {
        return Object.keys(state.selection);
    }
    function selectionCount() { return selectionList().length; }
    function selectionSize() {
        var bytes = 0; var sel = state.selection;
        for (var i = 0; i < state.files.length; i++) {
            if (sel[state.files[i].path]) bytes += state.files[i].size || 0;
        }
        return bytes;
    }

    function renderToolbar() {
        var el = document.getElementById('m-toolbar');
        if (!el) return;
        var sel = selectionCount();
        var total = state.total;
        var loading = state.loading ? '<span class="status-dot status-loading"></span> Loading...' : '';
        var stats = '<span class="media-stats">' + state.files.length + ' shown / ' + total + ' total</span>';

        if (sel === 0) {
            el.innerHTML = '<div class="media-toolbar-row">'
                + (loading || stats)
                + '<span class="media-stats-spacer"></span>'
                + '<button class="btn btn-sm" id="m-select-all">Select all</button>'
                + (state.nextOffset != null ? '<button class="btn btn-sm" id="m-load-more"' + (state.loading ? ' disabled' : '') + '>Load more</button>' : '')
                + '</div>';
            var sa = document.getElementById('m-select-all');
            if (sa) sa.addEventListener('click', selectAll);
            var more = document.getElementById('m-load-more');
            if (more) more.addEventListener('click', function() { refresh(true); });
            return;
        }

        var sizeStr = fmtBytes(selectionSize());
        el.innerHTML = '<div class="media-toolbar-row media-toolbar-active">'
            + '<span class="media-sel-count"><strong>' + sel + '</strong> selected · ' + sizeStr + '</span>'
            + '<button class="btn btn-sm" id="m-select-none">Clear</button>'
            + '<button class="btn btn-sm" id="m-select-all">Select all</button>'
            + '<span class="media-stats-spacer"></span>'
            + '<button class="btn btn-sm" id="m-pin">📌 Pin</button>'
            + '<button class="btn btn-sm" id="m-unpin">Unpin</button>'
            + '<button class="btn btn-sm" id="m-tag-add">Tag…</button>'
            + '<button class="btn btn-sm" id="m-coll-add">Add to collection…</button>'
            + '<button class="btn btn-sm btn-primary" id="m-export">⬇ Export ZIP</button>'
            + '<button class="btn btn-sm btn-danger" id="m-delete">🗑 Delete</button>'
            + '</div>';
        document.getElementById('m-select-none').addEventListener('click', clearSelection);
        document.getElementById('m-select-all').addEventListener('click', selectAll);
        document.getElementById('m-pin').addEventListener('click', function() { bulkSetPin(true); });
        document.getElementById('m-unpin').addEventListener('click', function() { bulkSetPin(false); });
        document.getElementById('m-tag-add').addEventListener('click', bulkAddTag);
        document.getElementById('m-coll-add').addEventListener('click', bulkAddToCollection);
        document.getElementById('m-export').addEventListener('click', bulkExportZip);
        document.getElementById('m-delete').addEventListener('click', bulkDelete);
    }

    function renderResults() {
        var root = document.getElementById('m-results');
        if (!root) return;
        if (state.files.length === 0) {
            if (state.loading) {
                root.innerHTML = '<div class="empty-state">Loading…</div>';
                return;
            }
            // Distinguish "filters hide everything" from "nothing here yet" so
            // the empty state can point users at where outputs come from.
            var filtered = !!(state.mediaKind || state.search || state.promptId ||
                state.tag || state.collection || state.pinned || state.sinceDays);
            if (filtered) {
                root.innerHTML = '<div class="empty-state">'
                    + 'No outputs match these filters. '
                    + '<button class="btn btn-sm" id="m-empty-clear">Clear filters</button>'
                    + '</div>';
                var clr = document.getElementById('m-empty-clear');
                if (clr) clr.addEventListener('click', clearFilters);
                return;
            }
            root.innerHTML = '<div class="empty-state">'
                + '<div style="font-size:15px;margin-bottom:6px;">Nothing here yet.</div>'
                + '<div style="color:var(--text-muted);max-width:520px;margin:0 auto;line-height:1.6;">'
                +   'Generations you create show up here automatically. Produce some from the '
                +   '<strong>Chat</strong> tab (images / video), <strong>ACE-Step</strong> or '
                +   '<strong>Audio Lab</strong> (music &amp; speech), or by running a graph in '
                +   '<strong>Workflows</strong>. '
                +   'You can also switch the <strong>Source</strong> dropdown above to browse '
                +   'ComfyUI inputs, temp files, or Omni TTS / STT output.'
                + '</div>'
                + '</div>';
            return;
        }
        if (state.view === 'grid') renderGrid(root);
        else renderList(root);
    }

    function renderGrid(root) {
        var html = '<div class="media-grid">';
        for (var i = 0; i < state.files.length; i++) {
            var f = state.files[i];
            var sel = !!state.selection[f.path];
            var name = basename(f.path);
            var thumb;
            if (f.kind === 'image') {
                thumb = '<img class="media-thumb" loading="lazy" src="' +
                    mediaUrl(f, { thumb: 1, w: 320 }) + '" alt="Preview of ' + App.esc(name) + '">';
            } else if (f.kind === 'video') {
                // preload="none" so we don't fire a metadata fetch for every
                // video tile at once (up to limit=500). Metadata (and the #t=0.1
                // poster frame) loads lazily on hover via bindTileEvents below.
                thumb = '<div class="media-thumb media-thumb-icon">'
                    + '<video preload="none" muted playsinline src="' + mediaUrl(f) + '#t=0.1"></video>'
                    + '<span class="media-thumb-badge">▶</span></div>';
            } else if (f.kind === 'audio') {
                thumb = '<div class="media-thumb media-thumb-icon"><span class="media-thumb-glyph">🔊</span></div>';
            } else {
                thumb = '<div class="media-thumb media-thumb-icon"><span class="media-thumb-glyph">' + kindIcon(f.kind) + '</span></div>';
            }
            html += '<div class="media-tile' + (sel ? ' media-selected' : '') + '" data-idx="' + i + '" role="button" tabindex="0" aria-label="Open ' + App.esc(name) + ' preview">'
                +    '<input type="checkbox" class="media-tile-check" aria-label="Select ' + App.esc(name) + '"' + (sel ? ' checked' : '') + ' data-idx="' + i + '">'
                +    (f.pinned ? '<span class="media-pin-badge" title="Pinned">📌</span>' : '')
                +    thumb
                +    '<div class="media-tile-meta">'
                +      '<div class="media-tile-name" title="' + App.esc(name) + '">' + App.esc(name) + '</div>'
                +      '<div class="media-tile-sub">' + kindBadge(f.kind) + ' · ' + fmtBytes(f.size) + ' · ' + fmtRel(f.mtime) + '</div>'
                +    '</div>'
                +  '</div>';
        }
        html += '</div>';
        root.innerHTML = html;
        bindTileEvents(root);
    }

    function renderList(root) {
        var rows = '';
        for (var i = 0; i < state.files.length; i++) {
            var f = state.files[i];
            var sel = !!state.selection[f.path];
            rows += '<tr class="' + (sel ? 'media-selected' : '') + '" data-idx="' + i + '">'
                +  '<td class="media-list-check"><input type="checkbox" class="media-tile-check" aria-label="Select ' + App.esc(basename(f.path)) + '"' + (sel ? ' checked' : '') + ' data-idx="' + i + '"></td>'
                +  '<td>' + kindIcon(f.kind) + '</td>'
                +  '<td class="media-list-name" role="button" tabindex="0" aria-label="Open ' + App.esc(basename(f.path)) + ' preview" data-open-idx="' + i + '">' + App.esc(basename(f.path)) +
                       (f.pinned ? ' <span title="Pinned">📌</span>' : '') + '</td>'
                +  '<td>' + kindBadge(f.kind) + '</td>'
                +  '<td>' + fmtBytes(f.size) + '</td>'
                +  '<td>' + new Date(f.mtime * 1000).toLocaleString() + '</td>'
                +  '<td>' + App.esc((f.tags || []).join(', ')) + '</td>'
                +  '</tr>';
        }
        root.innerHTML = '<table class="media-list-table"><thead><tr>'
            + '<th></th><th></th><th>Name</th><th>Kind</th><th>Size</th><th>Modified</th><th>Tags</th>'
            + '</tr></thead><tbody>' + rows + '</tbody></table>';
        bindTileEvents(root);
    }

    function bindTileEvents(root) {
        root.querySelectorAll('.media-tile-check').forEach(function(cb) {
            cb.addEventListener('click', function(ev) {
                ev.stopPropagation();
                var idx = parseInt(cb.dataset.idx, 10);
                handleSelect(idx, ev);
            });
        });
        root.querySelectorAll('.media-tile').forEach(function(t) {
            t.addEventListener('click', function(ev) {
                if (ev.target.classList.contains('media-tile-check')) return;
                var idx = parseInt(t.dataset.idx, 10);
                if (ev.shiftKey || ev.ctrlKey || ev.metaKey) {
                    handleSelect(idx, ev);
                } else {
                    openLightbox(idx);
                }
            });
            t.addEventListener('keydown', function(ev) {
                if (ev.key !== 'Enter' && ev.key !== ' ') return;
                ev.preventDefault();
                openLightbox(parseInt(t.dataset.idx, 10));
            });
        });
        // Video thumbs load with preload="none" to avoid a metadata fetch storm
        // on render. Flip to "metadata" on first hover so the #t=0.1 poster frame
        // appears, then mark as done so we only do it once per element.
        root.querySelectorAll('.media-thumb video[preload="none"]').forEach(function(v) {
            v.addEventListener('mouseenter', function() {
                if (v.dataset.metaLoaded) return;
                v.dataset.metaLoaded = '1';
                v.preload = 'metadata';
                if (typeof v.load === 'function') v.load();
            });
        });
        root.querySelectorAll('.media-list-name').forEach(function(td) {
            td.addEventListener('click', function() {
                openLightbox(parseInt(td.dataset.openIdx, 10));
            });
            td.addEventListener('keydown', function(ev) {
                if (ev.key !== 'Enter' && ev.key !== ' ') return;
                ev.preventDefault();
                openLightbox(parseInt(td.dataset.openIdx, 10));
            });
        });
        root.querySelectorAll('tbody tr').forEach(function(tr) {
            tr.addEventListener('click', function(ev) {
                if (ev.target.tagName === 'INPUT' || ev.target.classList.contains('media-list-name')) return;
                var idx = parseInt(tr.dataset.idx, 10);
                if (ev.shiftKey || ev.ctrlKey || ev.metaKey) handleSelect(idx, ev);
            });
        });
    }

    function handleSelect(idx, ev) {
        if (ev.shiftKey && state.lastClickedIndex >= 0) {
            var lo = Math.min(idx, state.lastClickedIndex);
            var hi = Math.max(idx, state.lastClickedIndex);
            for (var i = lo; i <= hi; i++) {
                state.selection[state.files[i].path] = true;
            }
        } else {
            var f = state.files[idx];
            if (state.selection[f.path]) delete state.selection[f.path];
            else state.selection[f.path] = true;
        }
        state.lastClickedIndex = idx;
        renderToolbar();
        renderResults();
    }
    function selectAll() {
        state.selection = {};
        for (var i = 0; i < state.files.length; i++) state.selection[state.files[i].path] = true;
        renderToolbar(); renderResults();
    }
    function clearSelection() {
        state.selection = {}; renderToolbar(); renderResults();
    }
    function clearFilters() {
        state.mediaKind = '';
        state.search = '';
        state.promptId = '';
        state.tag = '';
        state.collection = '';
        state.pinned = '';
        state.sinceDays = 0;
        state.lastClickedIndex = -1;
        // Reflect the reset in the filter controls.
        var set = function(id, v) { var el = document.getElementById(id); if (el) el.value = v; };
        set('m-search', '');
        set('m-tag', '');
        set('m-prompt', '');
        set('m-pinned', '');
        set('m-since', '0');
        set('m-collection', '');
        applyKindChipState();
        refresh();
    }

    // ----------------------------------------------------------------------
    // Bulk actions
    // ----------------------------------------------------------------------
    async function bulkSetPin(pin) {
        var source = state.source;
        var selectedRows = state.files.slice();
        var paths = selectionList();
        if (!paths.length) return;
        var ok = 0, fail = 0;
        for (var i = 0; i < paths.length; i++) {
            try {
                await App.api('PUT', '/api/outputs/meta/pinned/' + encPath(paths[i]) +
                    '?kind=' + encodeURIComponent(source) + '&pinned=' + pin);
                ok++;
            } catch (e) { fail++; }
        }
        App.toast((pin ? 'Pinned ' : 'Unpinned ') + ok + (fail ? ', ' + fail + ' failed' : ''),
            fail ? 'error' : 'success');
        refresh();
    }
    async function bulkAddTag() {
        var source = state.source;
        var selectedRows = state.files.slice();
        var paths = selectionList(); if (!paths.length) return;
        var raw = await inputModal({
            title: 'Add tag',
            hint: 'Adds this tag to the ' + paths.length + ' selected file(s).',
            placeholder: 'e.g. keepers, draft, client-a',
            saveLabel: 'Add tag',
        });
        if (raw === null) return;
        var tag = raw.trim();
        if (!tag) return;
        var ok = 0, fail = 0;
        for (var i = 0; i < paths.length; i++) {
            var f = selectedRows.find(function(row) { return row.path === paths[i]; });
            var current = (f && f.tags) || [];
            if (current.indexOf(tag) >= 0) { ok++; continue; }
            var next = current.concat([tag]);
            try {
                await App.api('PUT', '/api/outputs/meta/tags/' + encPath(paths[i]) +
                    '?kind=' + encodeURIComponent(source), { tags: next });
                ok++;
            } catch (e) { fail++; }
        }
        App.toast('Tagged ' + ok + (fail ? ', ' + fail + ' failed' : ''), fail ? 'error' : 'success');
        refresh();
    }
    async function bulkAddToCollection() {
        var source = state.source;
        var selectedRows = state.files.slice();
        var paths = selectionList(); if (!paths.length) return;
        var raw = await inputModal({
            title: 'Add to collection',
            hint: 'Adds the ' + paths.length + ' selected file(s) to this collection (created if new).',
            placeholder: 'Collection name',
            saveLabel: 'Add',
        });
        if (raw === null) return;
        var name = raw.trim();
        if (!name) return;
        var ok = 0, fail = 0;
        for (var i = 0; i < paths.length; i++) {
            var f = selectedRows.find(function(row) { return row.path === paths[i]; });
            var current = (f && f.collections) || [];
            if (current.indexOf(name) >= 0) { ok++; continue; }
            var next = current.concat([name]);
            try {
                await App.api('PUT', '/api/outputs/meta/collections/' + encPath(paths[i]) +
                    '?kind=' + encodeURIComponent(source), { collections: next });
                ok++;
            } catch (e) { fail++; }
        }
        App.toast('Added to "' + name + '": ' + ok + (fail ? ', ' + fail + ' failed' : ''),
            fail ? 'error' : 'success');
        refresh();
    }
    async function bulkExportZip() {
        var paths = selectionList(); if (!paths.length) return;
        var defName = 'omni-' + state.source + '-' + new Date().toISOString().replace(/[:.]/g, '-').substring(0, 19) + '.zip';
        var source = state.source;
        var writable = null;
        var streamingSave = typeof window.showSaveFilePicker === 'function';
        var maxBytes = streamingSave ? 5 * 1024 * 1024 * 1024 : 128 * 1024 * 1024;
        if (selectionSize() > maxBytes) {
            App.toast('This browser limits ZIP exports to 128 MiB. Use the CLI for larger bundles.', 'error');
            return;
        }
        var body = { paths: paths, kind: source, name: defName, max_size_bytes: maxBytes };
        App.toast('Building ZIP for ' + paths.length + ' file(s)…');
        try {
            if (streamingSave) {
                var handle = await window.showSaveFilePicker({suggestedName: defName});
                writable = await handle.createWritable();
            }
            var headers = { 'Content-Type': 'application/json' };
            if (App.state.sessionToken) headers['X-Omni-Token'] = App.state.sessionToken;
            var resp = await fetch('/api/outputs/zip', { method: 'POST', headers: headers, body: JSON.stringify(body) });
            if (!resp.ok) {
                var txt = await resp.text();
                throw new Error(resp.status + ': ' + txt);
            }
            if (writable) {
                await resp.body.pipeTo(writable);
                writable = null;
                App.toast('ZIP saved', 'success');
                return;
            }
            if (Number(resp.headers.get('Content-Length') || 0) > maxBytes + 1024 * 1024) {
                await resp.body.cancel();
                throw new Error('ZIP is too large for this browser; use the CLI');
            }
            var blob = await resp.blob();
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url; a.download = defName;
            document.body.appendChild(a); a.click(); document.body.removeChild(a);
            setTimeout(function() { URL.revokeObjectURL(url); }, 1000);
            App.toast('ZIP ready (' + fmtBytes(blob.size) + ')', 'success');
        } catch (err) {
            if (writable) { try { await writable.abort(); } catch (_) {} }
            if (err.name === 'AbortError') return;
            App.toast('Export failed: ' + err.message, 'error');
        }
    }
    async function bulkDelete() {
        var source = state.source;
        var selectedRows = state.files.slice();
        var paths = selectionList(); if (!paths.length) return;
        var size = fmtBytes(selectionSize());
        if (!confirm('Permanently delete ' + paths.length + ' file(s) (' + size + ')?\n\nThis cannot be undone.')) return;
        var ok = 0, fail = 0;
        for (var i = 0; i < paths.length; i++) {
            try {
                await App.api('DELETE', '/api/outputs/' + encPath(paths[i]) +
                    '?kind=' + encodeURIComponent(source));
                ok++;
            } catch (e) { fail++; }
        }
        App.toast('Deleted ' + ok + (fail ? ', ' + fail + ' failed' : ''), fail ? 'error' : 'success');
        if (state.source === source) state.selection = {};
        state.lastClickedIndex = -1;
        refresh();
    }
    function findByPath(p) {
        for (var i = 0; i < state.files.length; i++) {
            if (state.files[i].path === p) return state.files[i];
        }
        return null;
    }

    // ----------------------------------------------------------------------
    // Lightbox player
    // ----------------------------------------------------------------------
    var lb = {
        backdrop: null,
        zoom: 1, panX: 0, panY: 0, rotate: 0,
        dragging: false, dragStartX: 0, dragStartY: 0, panStartX: 0, panStartY: 0,
        rate: 1, loop: false,
        meta: null, // cached metadata for current file
        detailsRequest: 0,
        returnFocus: null,
    };

    function openLightbox(idx) {
        if (idx < 0 || idx >= state.files.length) return;
        state.lightboxIndex = idx;
        lb.returnFocus = document.activeElement;
        if (!lb.backdrop) buildLightboxFrame();
        lb.backdrop.style.display = 'flex';
        document.body.classList.add('media-lb-open');
        loadLightboxItem();
        var dialog = lb.backdrop.querySelector('.media-lightbox');
        if (dialog) dialog.focus();
    }
    function closeLightbox() {
        if (!lb.backdrop) return;
        var media = lb.backdrop.querySelector('video, audio');
        if (media) { try { media.pause(); } catch (e) {} }
        lb.backdrop.style.display = 'none';
        document.body.classList.remove('media-lb-open');
        state.lightboxIndex = -1;
        lb.meta = null;
        if (lb.returnFocus && typeof lb.returnFocus.focus === 'function') lb.returnFocus.focus();
        lb.returnFocus = null;
    }
    function lbNav(dir) {
        var n = state.files.length;
        if (n === 0) return;
        var i = (state.lightboxIndex + dir + n) % n;
        state.lightboxIndex = i;
        loadLightboxItem();
    }

    function buildLightboxFrame() {
        var bd = document.createElement('div');
        bd.className = 'media-lightbox-backdrop';
        bd.style.display = 'none';
        bd.innerHTML = ''
            + '<div class="media-lightbox" tabindex="-1" role="dialog" aria-modal="true" aria-labelledby="lb-name">'
            +   '<header class="media-lb-header">'
            +     '<div class="media-lb-title-row">'
            +       '<span class="media-lb-counter" id="lb-counter"></span>'
            +       '<span class="media-lb-name" id="lb-name"></span>'
            +     '</div>'
            +     '<div class="media-lb-header-actions">'
            +       '<button class="btn btn-sm" id="lb-pin" aria-label="Pin media" title="Pin (P)">📌</button>'
            +       '<button class="btn btn-sm" id="lb-tag" title="Edit tags (T)">Tags</button>'
            +       '<button class="btn btn-sm" id="lb-coll" title="Collection (C)">Coll</button>'
            +       '<button class="btn btn-sm" id="lb-meta" title="Metadata (I)">Info</button>'
            +       '<button class="btn btn-sm" id="lb-download" aria-label="Download media" title="Download (D)">⬇</button>'
            +       '<button class="btn btn-sm btn-danger" id="lb-delete" aria-label="Delete media" title="Delete (Del)">🗑</button>'
            +       '<button class="btn btn-sm" id="lb-close" aria-label="Close preview" title="Close (Esc)">✕</button>'
            +     '</div>'
            +   '</header>'
            +   '<div class="media-lb-body">'
            +     '<button class="media-lb-nav media-lb-prev" id="lb-prev" aria-label="Previous media" title="Previous (←)">‹</button>'
            +     '<div class="media-lb-stage" id="lb-stage"></div>'
            +     '<button class="media-lb-nav media-lb-next" id="lb-next" aria-label="Next media" title="Next (→)">›</button>'
            +     '<aside class="media-lb-side" id="lb-side"></aside>'
            +   '</div>'
            +   '<footer class="media-lb-footer" id="lb-footer"></footer>'
            + '</div>';
        document.body.appendChild(bd);
        lb.backdrop = bd;

        bd.addEventListener('click', function(ev) { if (ev.target === bd) closeLightbox(); });
        document.getElementById('lb-close').addEventListener('click', closeLightbox);
        document.getElementById('lb-prev').addEventListener('click', function() { lbNav(-1); });
        document.getElementById('lb-next').addEventListener('click', function() { lbNav(1); });
        document.getElementById('lb-pin').addEventListener('click', lbTogglePin);
        document.getElementById('lb-tag').addEventListener('click', lbEditTags);
        document.getElementById('lb-coll').addEventListener('click', lbEditCollections);
        document.getElementById('lb-meta').addEventListener('click', lbToggleMetadata);
        document.getElementById('lb-download').addEventListener('click', lbDownload);
        document.getElementById('lb-delete').addEventListener('click', lbDeleteCurrent);

        document.addEventListener('keydown', lbKeyHandler);

        // Singleton drag listeners — read state from `lb`, locate the live
        // image element fresh each move. Avoids the leak from re-attaching
        // on every openLightbox() call.
        window.addEventListener('mousemove', function(ev) {
            if (!lb.dragging) return;
            var img = lb.backdrop && lb.backdrop.querySelector('.media-lb-img');
            if (!img) return;
            lb.panX = lb.panStartX + (ev.clientX - lb.dragStartX);
            lb.panY = lb.panStartY + (ev.clientY - lb.dragStartY);
            applyImageTransform(img);
        });
        window.addEventListener('mouseup', function() {
            if (!lb.dragging) return;
            lb.dragging = false;
            var w = lb.backdrop && lb.backdrop.querySelector('.media-lb-imgwrap');
            if (w) w.style.cursor = '';
        });
    }

    function loadLightboxItem() {
        var f = state.files[state.lightboxIndex];
        if (!f) return;
        var oldMedia = lb.backdrop.querySelector('video, audio');
        if (oldMedia) { try { oldMedia.pause(); } catch (e) {} }
        document.getElementById('lb-counter').textContent = (state.lightboxIndex + 1) + ' / ' + state.files.length;
        document.getElementById('lb-name').textContent = basename(f.path);
        document.getElementById('lb-pin').classList.toggle('btn-primary', !!f.pinned);

        // PNG metadata (Info) is only meaningful for .png files — pre-disable
        // the action for everything else instead of erroring on click.
        var metaBtn = document.getElementById('lb-meta');
        if (metaBtn) {
            metaBtn.disabled = false;
            metaBtn.textContent = 'Reload info';
            metaBtn.title = 'Reload details (I)';
        }

        // Reset per-item view-state every time so previous file's state
        // (zoom, rotation, fetched PNG metadata, audio backdrop) doesn't
        // bleed into this one.
        lb.zoom = 1; lb.panX = 0; lb.panY = 0; lb.rotate = 0;
        lb.meta = null;

        var stage = document.getElementById('lb-stage');
        stage.classList.remove('media-lb-stage-audio');
        stage.innerHTML = '';
        var footer = document.getElementById('lb-footer');
        footer.innerHTML = '';

        if (f.kind === 'image') {
            stage.appendChild(buildImageStage(f));
            footer.appendChild(buildImageFooter(f));
        } else if (f.kind === 'video') {
            var v = buildVideo(f);
            stage.appendChild(v);
            footer.appendChild(buildMediaTransport(v, f, true));
            autoplayCurrent(v);
        } else if (f.kind === 'audio') {
            stage.classList.add('media-lb-stage-audio');
            var a = buildAudio(f);
            stage.appendChild(a.wrap);
            footer.appendChild(buildMediaTransport(a.audio, f, false));
            autoplayCurrent(a.audio);
        } else {
            stage.appendChild(buildGenericPreview(f));
            footer.innerHTML = '<div class="media-lb-meta-row">'
                + 'Kind: ' + f.kind + ' · ' + fmtBytes(f.size) + ' · ' + new Date(f.mtime * 1000).toLocaleString()
                + '</div>';
        }

        lbRefreshSidePanel(f, true);
        loadItemDetails(f);
    }

    function autoplayCurrent(media) {
        if (!state.autoplayBrowse) return;
        setTimeout(function() {
            if (state.lightboxIndex < 0 || !document.body.contains(media)) return;
            var attempt = media.play();
            if (attempt && typeof attempt.catch === 'function') {
                attempt.catch(function() {
                    App.toast('Autoplay was blocked; press Play once to allow it.', 'error');
                });
            }
        }, 0);
    }

    function buildImageStage(f) {
        var wrap = document.createElement('div');
        wrap.className = 'media-lb-imgwrap';
        var img = document.createElement('img');
        img.src = mediaUrl(f);
        img.className = 'media-lb-img';
        img.alt = basename(f.path);
        img.draggable = false;
        wrap.appendChild(img);
        applyImageTransform(img);
        wrap.addEventListener('wheel', function(ev) {
            ev.preventDefault();
            var delta = ev.deltaY < 0 ? 0.15 : -0.15;
            lb.zoom = Math.max(0.1, Math.min(10, lb.zoom * (1 + delta)));
            applyImageTransform(img);
        }, { passive: false });
        wrap.addEventListener('mousedown', function(ev) {
            if (ev.button !== 0) return;
            lb.dragging = true;
            lb.dragStartX = ev.clientX; lb.dragStartY = ev.clientY;
            lb.panStartX = lb.panX; lb.panStartY = lb.panY;
            wrap.style.cursor = 'grabbing';
        });
        // Window mousemove/mouseup are bound once in buildLightboxFrame.
        wrap.addEventListener('dblclick', function() {
            lb.zoom = 1; lb.panX = 0; lb.panY = 0; applyImageTransform(img);
        });
        return wrap;
    }
    function applyImageTransform(img) {
        img.style.transform = 'translate(' + lb.panX + 'px,' + lb.panY + 'px) ' +
            'rotate(' + lb.rotate + 'deg) scale(' + lb.zoom + ')';
    }
    function buildImageFooter(f) {
        var div = document.createElement('div');
        div.className = 'media-lb-transport';
        div.innerHTML = ''
            + '<button class="btn btn-sm" id="lb-zoom-out" title="Zoom out (-)">−</button>'
            + '<span class="media-lb-zoomlbl" id="lb-zoom-lbl">100%</span>'
            + '<button class="btn btn-sm" id="lb-zoom-in" title="Zoom in (+)">+</button>'
            + '<button class="btn btn-sm" id="lb-zoom-reset" title="Reset (0)">1:1</button>'
            + '<button class="btn btn-sm" id="lb-zoom-fit" title="Fit (F)">Fit</button>'
            + '<button class="btn btn-sm" id="lb-rotate" title="Rotate 90° (R)">⟳</button>'
            + '<span class="media-lb-meta-spacer"></span>'
            + '<span class="media-lb-meta-row">' + fmtBytes(f.size) + ' · ' + new Date(f.mtime * 1000).toLocaleString() + '</span>';
        setTimeout(function() {
            var img = document.querySelector('.media-lb-img');
            var lbl = document.getElementById('lb-zoom-lbl');
            var setLbl = function() { if (lbl) lbl.textContent = Math.round(lb.zoom * 100) + '%'; };
            div.querySelector('#lb-zoom-in').addEventListener('click', function() {
                lb.zoom = Math.min(10, lb.zoom * 1.2); applyImageTransform(img); setLbl();
            });
            div.querySelector('#lb-zoom-out').addEventListener('click', function() {
                lb.zoom = Math.max(0.1, lb.zoom / 1.2); applyImageTransform(img); setLbl();
            });
            div.querySelector('#lb-zoom-reset').addEventListener('click', function() {
                lb.zoom = 1; lb.panX = 0; lb.panY = 0; applyImageTransform(img); setLbl();
            });
            div.querySelector('#lb-zoom-fit').addEventListener('click', function() {
                lb.zoom = 1; lb.panX = 0; lb.panY = 0; lb.rotate = 0; applyImageTransform(img); setLbl();
            });
            div.querySelector('#lb-rotate').addEventListener('click', function() {
                lb.rotate = (lb.rotate + 90) % 360; applyImageTransform(img);
            });
        }, 0);
        return div;
    }

    function buildVideo(f) {
        var v = document.createElement('video');
        v.className = 'media-lb-video';
        v.src = mediaUrl(f);
        v.preload = 'auto';
        // Keep the browser's native transport available as the dependable
        // playback path inside WebView2. The richer Omni transport remains
        // below the stage for frame stepping, speed, PiP, and shortcuts.
        v.controls = true;
        v.playsInline = true;
        return v;
    }
    function buildAudio(f) {
        var wrap = document.createElement('div');
        wrap.className = 'media-lb-audio-wrap';
        wrap.innerHTML = '<div class="media-lb-audio-art">🔊<div class="media-lb-audio-name">'
            + App.esc(basename(f.path)) + '</div></div>';
        var a = document.createElement('audio');
        a.src = mediaUrl(f);
        a.preload = 'metadata';
        a.controls = false;
        wrap.appendChild(a);
        return { wrap: wrap, audio: a };
    }
    function buildGenericPreview(f) {
        var d = document.createElement('div');
        d.className = 'media-lb-generic';
        d.innerHTML = '<div class="media-lb-generic-icon">' + kindIcon(f.kind) + '</div>'
            + '<div>' + App.esc(basename(f.path)) + '</div>'
            + '<div class="media-lb-meta-row">' + fmtBytes(f.size) + '</div>'
            + '<button class="btn btn-sm" id="lb-generic-dl">Download</button>';
        setTimeout(function() {
            var b = document.getElementById('lb-generic-dl');
            if (b) b.addEventListener('click', lbDownload);
        }, 0);
        return d;
    }

    function toggleMediaPlayback(media) {
        if (!media.paused) {
            media.pause();
            return;
        }
        var attempt = media.play();
        if (attempt && typeof attempt.catch === 'function') {
            attempt.catch(function(err) {
                var detail = err && err.message ? ': ' + err.message : '';
                App.toast('Unable to play this media' + detail, 'error');
            });
        }
    }

    function buildMediaTransport(media, f, isVideo) {
        var div = document.createElement('div');
        div.className = 'media-lb-transport';
        div.innerHTML = ''
            + '<button class="btn btn-sm" id="lb-play" title="Play/Pause (Space)">▶</button>'
            + '<button class="btn btn-sm" id="lb-stop" title="Stop (S)">■</button>'
            + (isVideo ? '<button class="btn btn-sm" id="lb-fr-prev" title="Frame back ([)">⏮</button>'
                       + '<button class="btn btn-sm" id="lb-fr-next" title="Frame fwd (])">⏭</button>' : '')
            + '<span class="media-lb-time" id="lb-time">0:00 / 0:00</span>'
            + '<input type="range" class="media-lb-scrub" id="lb-scrub" min="0" max="1000" value="0" step="1">'
            + '<button class="btn btn-sm" id="lb-mute" title="Mute (M)">🔊</button>'
            + '<input type="range" class="media-lb-vol" id="lb-vol" min="0" max="1" step="0.01" value="1">'
            + '<select id="lb-rate" class="media-lb-rate" title="Playback speed">'
            +   '<option value="0.25">0.25×</option>'
            +   '<option value="0.5">0.5×</option>'
            +   '<option value="0.75">0.75×</option>'
            +   '<option value="1" selected>1×</option>'
            +   '<option value="1.25">1.25×</option>'
            +   '<option value="1.5">1.5×</option>'
            +   '<option value="2">2×</option>'
            + '</select>'
            + '<button class="btn btn-sm" id="lb-loop" title="Loop (L)">↻</button>'
            + (isVideo ? '<button class="btn btn-sm" id="lb-pip" title="Picture-in-picture">⧉</button>'
                       + '<button class="btn btn-sm" id="lb-fs" title="Fullscreen (F)">⛶</button>' : '');
        setTimeout(function() {
            var play = div.querySelector('#lb-play');
            var stop = div.querySelector('#lb-stop');
            var time = div.querySelector('#lb-time');
            var scrub = div.querySelector('#lb-scrub');
            var mute = div.querySelector('#lb-mute');
            var vol = div.querySelector('#lb-vol');
            var rate = div.querySelector('#lb-rate');
            var loop = div.querySelector('#lb-loop');
            var fs = div.querySelector('#lb-fs');
            var pip = div.querySelector('#lb-pip');

            media.loop = lb.loop;
            media.playbackRate = lb.rate;
            if (loop) loop.classList.toggle('btn-primary', media.loop);
            if (rate) rate.value = String(media.playbackRate);

            var updateScrub = function() {
                if (!media.duration || !isFinite(media.duration)) return;
                scrub.value = String((media.currentTime / media.duration) * 1000);
                time.textContent = fmtTime(media.currentTime) + ' / ' + fmtTime(media.duration);
            };
            media.addEventListener('timeupdate', updateScrub);
            media.addEventListener('loadedmetadata', updateScrub);
            media.addEventListener('play', function() { play.textContent = '❚❚'; });
            media.addEventListener('pause', function() { play.textContent = '▶'; });
            media.addEventListener('ended', function() { play.textContent = '▶'; });

            play.addEventListener('click', function() {
                toggleMediaPlayback(media);
            });
            stop.addEventListener('click', function() {
                media.pause(); media.currentTime = 0;
            });
            scrub.addEventListener('input', function() {
                if (!media.duration) return;
                media.currentTime = (parseFloat(scrub.value) / 1000) * media.duration;
            });
            mute.addEventListener('click', function() {
                media.muted = !media.muted;
                mute.textContent = media.muted ? '🔇' : '🔊';
            });
            vol.addEventListener('input', function() {
                media.volume = parseFloat(vol.value);
                if (media.volume > 0 && media.muted) { media.muted = false; mute.textContent = '🔊'; }
            });
            rate.addEventListener('change', function() {
                media.playbackRate = parseFloat(rate.value);
                lb.rate = media.playbackRate;
            });
            loop.addEventListener('click', function() {
                media.loop = !media.loop;
                lb.loop = media.loop;
                loop.classList.toggle('btn-primary', media.loop);
            });
            if (fs) fs.addEventListener('click', function() {
                if (document.fullscreenElement) document.exitFullscreen();
                else media.requestFullscreen && media.requestFullscreen();
            });
            if (pip) pip.addEventListener('click', function() {
                try {
                    if (document.pictureInPictureElement) document.exitPictureInPicture();
                    else if (media.requestPictureInPicture) media.requestPictureInPicture();
                } catch (e) { App.toast('PiP not supported', 'error'); }
            });
            if (isVideo) {
                var fp = div.querySelector('#lb-fr-prev'), fn = div.querySelector('#lb-fr-next');
                fp.addEventListener('click', function() { media.pause(); media.currentTime = Math.max(0, media.currentTime - (1 / 30)); });
                fn.addEventListener('click', function() { media.pause(); media.currentTime = media.currentTime + (1 / 30); });
            }
        }, 0);
        return div;
    }

    function prettyValue(value) {
        if (typeof value === 'string') return value;
        try { return JSON.stringify(value, null, 2); }
        catch (e) { return String(value); }
    }
    function detailBlock(title, value, open) {
        if (value == null || value === '' || (Array.isArray(value) && value.length === 0)) return '';
        var pretty = prettyValue(value);
        if (pretty.length > 64000) pretty = pretty.substring(0, 64000) + '\n... (display truncated)';
        return '<details' + (open ? ' open' : '') + '><summary>' + App.esc(title) + '</summary>'
            + '<pre class="media-lb-side-meta">' + App.esc(pretty) + '</pre></details>';
    }
    function lbRefreshSidePanel(f, loading) {
        var side = document.getElementById('lb-side');
        if (!side) return;
        var tags = (f.tags || []).map(function(t) { return '<span class="media-tag">' + App.esc(t) + '</span>'; }).join('');
        var colls = (f.collections || []).map(function(c) { return '<span class="media-tag media-tag-coll">' + App.esc(c) + '</span>'; }).join('');
        var notes = f.notes ? '<div class="media-lb-side-notes">' + App.esc(f.notes) + '</div>' : '';
        var details = lb.meta || {};
        var artifact = details.artifact || f.artifact || {};
        var provenance = artifact.provenance || {};
        var documentData = details.document;
        var companion = details.companion;
        var related = details.related || [];
        var facts = {};
        if (artifact.dimensions) facts.dimensions = artifact.dimensions.width + ' x ' + artifact.dimensions.height;
        if (artifact.duration != null) facts.duration_seconds = Math.round(artifact.duration * 1000) / 1000;
        if (artifact.metadata) facts.media = artifact.metadata;
        if (artifact.integrity) facts.integrity = artifact.integrity;
        var content = '';
        if (documentData) content += detailBlock('File contents', documentData.parsed != null ? documentData.parsed : documentData.text, true);
        if (companion) content += detailBlock('Related ' + basename(companion.path || 'manifest.json'), companion.parsed != null ? companion.parsed : companion.text, true);
        content += detailBlock('Model, parameters & provenance', provenance, true);
        content += detailBlock('Media details', facts, false);
        content += detailBlock('Embedded generation metadata', details.metadata, false);
        if (details.error) content += detailBlock('Read error', details.error, true);
        if (related.length) {
            content += '<div class="media-lb-related">' + related.map(function(r) {
                return '<button class="media-related-item" data-related-path="' + App.esc(r.path) + '">'
                    + kindIcon(r.kind) + ' <span>' + App.esc(basename(r.path)) + '</span>'
                    + '<small>' + fmtBytes(r.size) + '</small></button>';
            }).join('') + '</div>';
        }
        side.innerHTML = ''
            + '<div class="media-lb-side-row"><strong>Path</strong>'
            +   '<div class="media-lb-side-path">' + App.esc(f.path) + '</div></div>'
            + '<div class="media-lb-side-row"><strong>Tags</strong>'
            +   '<div>' + (tags || '<span class="media-lb-side-empty">None</span>') + '</div></div>'
            + '<div class="media-lb-side-row"><strong>Collections</strong>'
            +   '<div>' + (colls || '<span class="media-lb-side-empty">None</span>') + '</div></div>'
            + (notes ? '<div class="media-lb-side-row"><strong>Notes</strong>' + notes + '</div>' : '')
            + '<div class="media-lb-side-row"><strong>Generation info</strong>'
            + (loading ? '<div class="media-lb-side-empty">Loading file and related details...</div>' : (content || '<div class="media-lb-side-empty">No generation details found.</div>'))
            + '</div>';
        side.querySelectorAll('.media-related-item').forEach(function(button) {
            button.addEventListener('click', function() {
                var path = button.dataset.relatedPath;
                for (var i = 0; i < state.files.length; i++) {
                    if (state.files[i].path === path) {
                        state.lightboxIndex = i; loadLightboxItem(); return;
                    }
                }
                window.open('/api/outputs/' + encPath(path) + '?kind=' + encodeURIComponent(state.source), '_blank');
            });
        });
    }

    async function loadItemDetails(f) {
        var requestId = ++lb.detailsRequest;
        try {
            var data = await App.api('GET', '/api/outputs/metadata/' + encPath(f.path) +
                '?kind=' + encodeURIComponent(state.source));
            if (requestId !== lb.detailsRequest || state.files[state.lightboxIndex] !== f) return;
            lb.meta = data;
            lbRefreshSidePanel(f, false);
        } catch (e) {
            if (requestId !== lb.detailsRequest) return;
            lb.meta = { artifact: f.artifact || {}, error: e.message };
            lbRefreshSidePanel(f, false);
            App.toast('Info read failed: ' + e.message, 'error');
        }
    }

    async function lbToggleMetadata() {
        var f = state.files[state.lightboxIndex]; if (!f) return;
        lb.meta = null;
        lbRefreshSidePanel(f, true);
        await loadItemDetails(f);
    }

    async function lbTogglePin() {
        var f = state.files[state.lightboxIndex]; if (!f) return;
        var next = !f.pinned;
        try {
            await App.api('PUT', '/api/outputs/meta/pinned/' + encPath(f.path) +
                '?kind=' + encodeURIComponent(f.storage_kind || state.source) + '&pinned=' + next);
            f.pinned = next;
            document.getElementById('lb-pin').classList.toggle('btn-primary', next);
            App.toast(next ? 'Pinned' : 'Unpinned', 'success');
            renderResults();
        } catch (e) { App.toast('Pin failed: ' + e.message, 'error'); }
    }
    async function lbEditTags() {
        var f = state.files[state.lightboxIndex]; if (!f) return;
        var current = (f.tags || []).join(', ');
        var input = await inputModal({
            title: 'Edit tags',
            hint: 'Comma-separated. Replaces the current tags for this file.',
            placeholder: 'tag-a, tag-b',
            value: current,
        });
        if (input === null) return;
        var tags = input.split(',').map(function(t) { return t.trim(); }).filter(Boolean);
        try {
            await App.api('PUT', '/api/outputs/meta/tags/' + encPath(f.path) +
                '?kind=' + encodeURIComponent(f.storage_kind || state.source), { tags: tags });
            f.tags = tags;
            lbRefreshSidePanel(f); renderResults();
        } catch (e) { App.toast('Tag save failed: ' + e.message, 'error'); }
    }
    async function lbEditCollections() {
        var f = state.files[state.lightboxIndex]; if (!f) return;
        var current = (f.collections || []).join(', ');
        var input = await inputModal({
            title: 'Edit collections',
            hint: 'Comma-separated. Replaces the current collections for this file.',
            placeholder: 'collection-a, collection-b',
            value: current,
        });
        if (input === null) return;
        var colls = input.split(',').map(function(t) { return t.trim(); }).filter(Boolean);
        try {
            await App.api('PUT', '/api/outputs/meta/collections/' + encPath(f.path) +
                '?kind=' + encodeURIComponent(f.storage_kind || state.source), { collections: colls });
            f.collections = colls;
            lbRefreshSidePanel(f); renderResults();
        } catch (e) { App.toast('Collection save failed: ' + e.message, 'error'); }
    }
    function lbDownload() {
        var f = state.files[state.lightboxIndex]; if (!f) return;
        var a = document.createElement('a');
        a.href = mediaUrl(f, { download: 1 });
        a.download = basename(f.path);
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
    }
    async function lbDeleteCurrent() {
        var f = state.files[state.lightboxIndex]; if (!f) return;
        if (!confirm('Delete ' + basename(f.path) + '?')) return;
        try {
            await App.api('DELETE', '/api/outputs/' + encPath(f.path) +
                '?kind=' + encodeURIComponent(f.storage_kind || state.source));
            if (f.storage_kind && f.storage_kind !== state.source) return;
            var removedIndex = state.files.findIndex(function(row) { return row.path === f.path; });
            if (removedIndex < 0) return;
            state.files.splice(removedIndex, 1);
            if (removedIndex < state.lightboxIndex) state.lightboxIndex--;
            delete state.selection[f.path];
            state.lastClickedIndex = -1;
            if (state.total > 0) state.total--;
            App.toast('Deleted', 'success');
            if (state.files.length === 0) { closeLightbox(); }
            else {
                if (state.lightboxIndex >= state.files.length) state.lightboxIndex = state.files.length - 1;
                loadLightboxItem();
            }
            renderResults(); renderToolbar();
        } catch (e) { App.toast('Delete failed: ' + e.message, 'error'); }
    }

    function lbKeyHandler(ev) {
        // Only react when the lightbox is open
        if (state.lightboxIndex < 0) {
            // Tab-level shortcuts (Ctrl+A, Del when nothing else focused)
            var tabActive = document.getElementById('tab-media') && document.getElementById('tab-media').classList.contains('active');
            if (!tabActive) return;
            if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 'a' && !isTextField(ev.target)) {
                ev.preventDefault(); selectAll();
            } else if (ev.key === 'Delete' && selectionCount() > 0 && !isTextField(ev.target)) {
                ev.preventDefault(); bulkDelete();
            } else if (ev.key.toLowerCase() === 'r' && !isTextField(ev.target) && !ev.ctrlKey && !ev.metaKey) {
                refresh();
            }
            return;
        }
        if (isTextField(ev.target)) return;
        var media = lb.backdrop && lb.backdrop.querySelector('video, audio');
        switch (ev.key) {
            case 'Escape': closeLightbox(); ev.preventDefault(); break;
            case 'ArrowLeft': lbNav(-1); ev.preventDefault(); break;
            case 'ArrowRight': lbNav(1); ev.preventDefault(); break;
            case ' ':
                if (media) { toggleMediaPlayback(media); ev.preventDefault(); }
                break;
            case 'f': case 'F':
                if (media && media.requestFullscreen) media.requestFullscreen();
                else {
                    var img = lb.backdrop.querySelector('.media-lb-img');
                    if (img) {
                        lb.zoom = 1; lb.panX = 0; lb.panY = 0; lb.rotate = 0;
                        applyImageTransform(img);
                        var lbl = document.getElementById('lb-zoom-lbl');
                        if (lbl) lbl.textContent = '100%';
                    }
                }
                break;
            case 'l': case 'L':
                if (media) {
                    media.loop = !media.loop; lb.loop = media.loop;
                    var b = document.getElementById('lb-loop');
                    if (b) b.classList.toggle('btn-primary', media.loop);
                }
                break;
            case 'm': case 'M':
                if (media) {
                    media.muted = !media.muted;
                    var mb = document.getElementById('lb-mute');
                    if (mb) mb.textContent = media.muted ? '🔇' : '🔊';
                }
                break;
            case 's': case 'S':
                if (media) { media.pause(); media.currentTime = 0; }
                break;
            case '+': case '=':
                {
                    var img2 = lb.backdrop.querySelector('.media-lb-img');
                    if (img2) {
                        lb.zoom = Math.min(10, lb.zoom * 1.2); applyImageTransform(img2);
                        var l2 = document.getElementById('lb-zoom-lbl');
                        if (l2) l2.textContent = Math.round(lb.zoom * 100) + '%';
                    }
                }
                break;
            case '-':
                {
                    var img3 = lb.backdrop.querySelector('.media-lb-img');
                    if (img3) {
                        lb.zoom = Math.max(0.1, lb.zoom / 1.2); applyImageTransform(img3);
                        var l3 = document.getElementById('lb-zoom-lbl');
                        if (l3) l3.textContent = Math.round(lb.zoom * 100) + '%';
                    }
                }
                break;
            case '0':
                {
                    var img4 = lb.backdrop.querySelector('.media-lb-img');
                    if (img4) {
                        lb.zoom = 1; lb.panX = 0; lb.panY = 0; applyImageTransform(img4);
                        var l4 = document.getElementById('lb-zoom-lbl');
                        if (l4) l4.textContent = '100%';
                    }
                }
                break;
            case 'r': case 'R':
                {
                    var img5 = lb.backdrop.querySelector('.media-lb-img');
                    if (img5) { lb.rotate = (lb.rotate + 90) % 360; applyImageTransform(img5); }
                }
                break;
            case 'p': case 'P': lbTogglePin(); break;
            case 't': case 'T': lbEditTags(); break;
            case 'c': case 'C': lbEditCollections(); break;
            case 'i': case 'I': lbToggleMetadata(); break;
            case 'd': case 'D': lbDownload(); break;
            case 'Delete': lbDeleteCurrent(); ev.preventDefault(); break;
            case '[':
                if (media && media.tagName === 'VIDEO') { media.pause(); media.currentTime = Math.max(0, media.currentTime - (1 / 30)); }
                break;
            case ']':
                if (media && media.tagName === 'VIDEO') { media.pause(); media.currentTime = media.currentTime + (1 / 30); }
                break;
        }
    }
    function isTextField(t) {
        if (!t) return false;
        var tag = (t.tagName || '').toLowerCase();
        return tag === 'input' || tag === 'textarea' || tag === 'select' || t.isContentEditable;
    }

    // ----------------------------------------------------------------------
    // Public surface
    // ----------------------------------------------------------------------
    return {
        init: function() {
            loadPrefs();
            renderTab();
            scheduleAutoRefresh();
            // Tabs are initialized lazily, so init itself is the first activation.
            refresh();
        },
        onActivate: function() {
            // Re-render frame in case it was destroyed (it isn't, but defensively).
            renderTab();
            // Refresh if we haven't fetched in the last 15s.
            if (Date.now() - state.lastFetchAt > 15000) refresh();
        },
    };
})();
