/* ==========================================================================
   Tab: Log — Real-time log viewer via SSE
   ========================================================================== */

var TabLog = {
    _autoScroll: true,

    init: function() {
        var el = document.getElementById('tab-log');
        el.innerHTML =
            '<div class="card" style="padding:8px">' +
            '<div class="flex-between mb-8">' +
            '<h2 style="margin:0">Live Log</h2>' +
            '<div class="btn-group">' +
            '<label style="font-size:12px;color:var(--text-secondary);display:flex;align-items:center;gap:4px">' +
            '<input type="checkbox" id="log-autoscroll" checked onchange="TabLog._autoScroll=this.checked"> Auto-scroll' +
            '</label>' +
            '<button class="btn btn-sm" onclick="TabLog.clear()">Clear</button>' +
            '</div></div>' +
            '<div id="log-viewer" class="log-viewer"></div>' +
            '</div>';

        // Render existing logs
        var viewer = document.getElementById('log-viewer');
        var logs = App.state.logs;
        for (var i = 0; i < logs.length; i++) {
            this._appendLine(viewer, logs[i]);
        }
    },

    appendEntry: function(entry) {
        var viewer = document.getElementById('log-viewer');
        if (!viewer) return;
        this._appendLine(viewer, entry);
        if (this._autoScroll) {
            viewer.scrollTop = viewer.scrollHeight;
        }
    },

    _appendLine: function(viewer, entry) {
        var line = document.createElement('div');
        var src = entry.source || '???';
        var msg = entry.message || '';
        line.textContent = '[' + src + '] ' + msg;  // textContent is XSS-safe

        // Color coding
        var lower = msg.toLowerCase();
        if (lower.indexOf('error') !== -1 || lower.indexOf('fail') !== -1) {
            line.style.color = 'var(--accent-red-text)';
        } else if (lower.indexOf('warn') !== -1) {
            line.style.color = 'var(--accent-orange-text)';
        } else if (lower.indexOf('ready') !== -1 || lower.indexOf('complete') !== -1 || lower.indexOf('success') !== -1) {
            line.style.color = 'var(--accent-green-text)';
        }
        viewer.appendChild(line);

        // Keep buffer bounded
        while (viewer.childNodes.length > App.MAX_LOGS) {
            viewer.removeChild(viewer.firstChild);
        }
    },

    clear: function() {
        var viewer = document.getElementById('log-viewer');
        if (viewer) viewer.innerHTML = '';
        App.state.logs = [];
    },
};
