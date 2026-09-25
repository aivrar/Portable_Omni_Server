/* ==========================================================================
   Tab: Models — browse, download, and manage model weights
   ========================================================================== */

var TabModels = {
    _DEDICATED_IDS: ['audio_lab', 'ace_step'],
    audioLab: { models: [], vaes: [], claps: [] },
    aceStep: { models: [], lms: [], vaes: [], loras: [] },
    comfyTemplates: { models: [], blueprints: [], missing: [] },
    comfyTemplatesLoaded: false,
    comfyTemplatesLoading: false,
    comfyTemplateFilter: '',
    comfyTemplateStatus: 'missing',
    comfyTemplateVisibleLimit: 40,
    comfyTemplateBatchCategory: 'all',
    comfyTemplateBatchLimit: 50,
    comfyDirectCategory: 'checkpoints',
    viewMode: 'general',

    init: function() {
        try {
            this.viewMode = localStorage.getItem('omni.modelView') === 'comfy' ? 'comfy' : 'general';
        } catch (_) {}
        this.load();
    },

    onActivate: function() { this.load(); },

    load: async function() {
        try {
            var results = await Promise.all([
                App.api('GET', '/api/comfy/models').catch(function() { return { models: {} }; }),
                App.api('GET', '/api/audio_lab/status').catch(function() { return {}; }),
                App.api('GET', '/api/ace_step/status').catch(function() { return {}; }),
            ]);
            App.state.comfyModels = results[0].models || {};
            this.audioLab = {
                models: results[1].models || [],
                vaes: results[1].vaes || [],
                claps: results[1].claps || [],
            };
            this.aceStep = {
                models: results[2].models || [],
                lms: results[2].lms || [],
                vaes: results[2].vaes || [],
                loras: results[2].loras || [],
            };
        } catch (e) {
            /* keep last-known asset lists */
        }
        await App.loadSetupStatus();
        await App.loadInstallJobs();
        this.render();
    },

    openTab: function(tabName) {
        App.activateTab(tabName);
    },

    setViewMode: function(mode, renderNow) {
        this.viewMode = mode === 'comfy' ? 'comfy' : 'general';
        try { localStorage.setItem('omni.modelView', this.viewMode); } catch (_) {}
        if (renderNow !== false) this.render();
    },

    loadComfyTemplates: async function() {
        if (this.comfyTemplatesLoading) return;
        this.comfyTemplatesLoading = true;
        this.render();
        try {
            this.comfyTemplates = await App.api('GET', '/api/assets/comfy/templates/requirements');
            this.comfyTemplatesLoaded = true;
        } catch (e) {
            App.toast('Template catalog could not be loaded', 'error');
        } finally {
            this.comfyTemplatesLoading = false;
            this.render();
        }
    },

    setComfyTemplateFilter: function(value) {
        this.comfyTemplateFilter = value || '';
        this.comfyTemplateVisibleLimit = 40;
        if (App.preserveFocus) {
            App.preserveFocus(function() { TabModels.render(); });
        } else {
            this.render();
        }
    },

    setComfyTemplateStatus: function(status) {
        var allowed = ['missing', 'downloadable', 'installed', 'needs-link', 'all'];
        this.comfyTemplateStatus = allowed.indexOf(status) !== -1 ? status : 'missing';
        this.comfyTemplateVisibleLimit = 40;
        this.render();
    },

    showMoreComfyTemplateModels: function() {
        this.comfyTemplateVisibleLimit += 40;
        this.render();
    },

    comfyCategoryOptions: function(models, report) {
        var fallback = [
            'checkpoints', 'diffusion_models', 'vae', 'clip', 'text_encoders',
            'loras', 'controlnet', 'gguf', 'unet', 'embeddings',
            'upscale_models', 'latent_upscale_models', 'clip_vision',
            'model_patches', 'style_models', 'audio_encoders', 'diffusers',
            'configs', 'gligen', 'hypernetworks', 'vae_approx',
            'frame_interpolation', 'photomaker', 'background_removal',
            'detection', 'geometry_estimation', 'optical_flow',
        ];
        var seen = {};
        var out = [];
        function add(cat) {
            if (!cat || seen[cat]) return;
            seen[cat] = true;
            out.push(cat);
        }
        fallback.forEach(add);
        Object.keys(models || {}).forEach(add);
        var rows = (report && report.models) || [];
        for (var i = 0; i < rows.length; i++) add(rows[i].category);
        return out.sort();
    },

    render: function() {
        var el = document.getElementById('tab-models');
        if (!el) return;
        var models = App.state.comfyModels || {};
        var categories = Object.keys(models);
        var E = App.esc;
        var omniModels = App.state.models || {};
        var variants = App.state.variants || {};
        var setupStatus = App.state.setupStatus || {};

        var comfyOnly = this.viewMode === 'comfy';
        var html = '<div class="page-heading"><div class="page-heading-main">';
        html += '<span class="eyebrow">' + (comfyOnly ? 'ComfyUI workspace' : 'Models &amp; runtime') + '</span><h1>' + (comfyOnly ? 'ComfyUI models' : 'Model library') + '</h1>';
        html += '<p>' + (comfyOnly ? 'Browse installed ComfyUI weights, resolve workflow requirements, and monitor downloads without mixing in chat, audio, or music models.' : 'Install and manage non-Comfy model weights here. Nothing loads into memory until you start it from its workspace.') + '</p></div>';
        html += '<div class="page-heading-actions">';
        html += '<button class="btn btn-sm" onclick="TabModels.load()">Refresh all</button>';
        if (comfyOnly) {
            html += '<button class="btn btn-sm" onclick="TabModels.openTab(\'workflows\')">Workflows</button>';
            html += '<button class="btn btn-sm btn-primary" onclick="TabModels.openTab(\'comfy\')">Open ComfyUI</button>';
        } else {
            html += '<button class="btn btn-sm btn-primary" onclick="TabModels.openTab(\'server\')">Open Runtime</button>';
        }
        html += '</div>';
        html += '</div>';
        html += '<div class="model-view-switch" role="group" aria-label="Model library view">';
        html += '<button class="btn btn-sm' + (!comfyOnly ? ' btn-primary' : '') + '" type="button" aria-pressed="' + (!comfyOnly) + '" onclick="TabModels.setViewMode(\'general\')">General models</button>';
        html += '<button class="btn btn-sm' + (comfyOnly ? ' btn-primary' : '') + '" type="button" aria-pressed="' + comfyOnly + '" onclick="TabModels.setViewMode(\'comfy\')">ComfyUI models</button>';
        html += '</div>';

        if (!comfyOnly) {
        // Omni model variants
        html += '<div class="card mt-16"><h2>Omni Model Variants</h2>';
        html += '<p style="color:var(--text-secondary);font-size:12px;margin-bottom:8px">'
            + 'Install a model\'s base packages first, then download one or more weight variants. '
            + 'Gated HuggingFace repos need a <code>hf_</code> token on the Setup tab.'
            + '</p>';

        var omniIds = Object.keys(variants).filter(function(id) {
            return TabModels._DEDICATED_IDS.indexOf(id) === -1;
        });
        if (omniIds.length === 0) {
            html += '<div class="empty-state">No omni models configured.</div>';
        } else {
            for (var oi = 0; oi < omniIds.length; oi++) {
                var oid = omniIds[oi];
                var vs = variants[oid] || [];
                var modelDisplay = (omniModels[oid] && omniModels[oid].display) || oid;
                var st = setupStatus[oid] || {};
                var vStatus = st.variants || [];
                var installedMap = {};
                for (var si = 0; si < vStatus.length; si++) {
                    if (vStatus[si].installed) installedMap[vStatus[si].variant_id] = true;
                }

                html += '<details class="advanced-panel mt-16"' + (oi === 0 ? ' open' : '') + '>';
                html += '<summary>' + E(modelDisplay) + ' (' + vs.length + ' variant' + (vs.length === 1 ? '' : 's') + ')</summary>';
                html += '<div class="advanced-panel-body">';
                html += '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px">';
                if (st.installed) {
                    html += '<span class="badge badge-green">Base ready</span>';
                } else if (st.weights_installed) {
                    html += '<span class="badge badge-orange">Partial</span>';
                } else {
                    html += '<span class="badge badge-gray">Base not installed</span>';
                    html += '<button class="btn btn-primary btn-sm" data-model="' + E(oid) + '" '
                        + 'onclick="TabSetup.install(this)">Install base</button>';
                }
                html += '</div>';

                html += '<table><thead><tr><th>Variant</th><th>Repo</th><th>Size</th><th>VRAM</th><th>Status</th><th></th></tr></thead><tbody>';
                for (var vi = 0; vi < vs.length; vi++) {
                    var vr = vs[vi];
                    var isInstalled = !!installedMap[vr.variant_id];
                    html += '<tr>';
                    html += '<td>' + E(vr.display) + '</td>';
                    html += '<td style="font-family:var(--font-mono);font-size:11px">';
                    if (vr.repo) {
                        html += '<a href="https://huggingface.co/' + E(vr.repo) + '" target="_blank" rel="noopener" style="color:inherit">'
                            + E(vr.repo) + ' ↗</a>';
                    }
                    html += '</td>';
                    html += '<td>' + E(vr.size) + '</td>';
                    html += '<td>' + E(vr.vram) + '</td>';
                    html += '<td>' + (isInstalled
                        ? '<span class="badge badge-green">installed</span>'
                        : '<span class="badge badge-gray">not installed</span>') + '</td>';
                    html += '<td>';
                    if (isInstalled) {
                        html += '<span style="color:var(--text-muted);font-size:11px">ready to load</span>';
                    } else {
                        html += '<button class="btn btn-sm btn-primary" data-model="' + E(oid) + '" data-variant="' + E(vr.variant_id) + '" '
                            + 'onclick="TabSetup.installVariantById(this.dataset.model, this.dataset.variant, this)">Download</button>';
                    }
                    html += '</td></tr>';
                }
                html += '</tbody></table></div></details>';
            }
        }
        html += '</div>';

        // Audio Lab assets
        html += this._renderAssetSection({
            title: 'Audio Lab',
            subtitle: 'Stable Audio, CLAP, and VAE weights. Load installed picks on the Audio Lab tab.',
            tab: 'audio-lab',
            groups: [
                { label: 'Stable Audio models', kind: 'audio-model', list: this.audioLab.models },
                { label: 'CLAP models', kind: 'audio-clap', list: this.audioLab.claps },
                { label: 'VAE swaps', kind: 'audio-vae', list: (this.audioLab.vaes || []).filter(function(v) {
                    return v.variant_id !== 'default';
                }) },
            ],
        });

        // ACE Step assets
        html += this._renderAssetSection({
            title: 'ACE Step',
            subtitle: 'DiT song models, text encoders (LMs), and VAE swaps. Load on the ACE Step tab.',
            tab: 'ace-step',
            groups: [
                { label: 'DiT models', kind: 'ace-model', list: this.aceStep.models },
                { label: 'Text encoders (LM)', kind: 'ace-lm', list: this.aceStep.lms },
                { label: 'VAE swaps', kind: 'ace-vae', list: (this.aceStep.vaes || []).filter(function(v) {
                    return v.variant_id !== 'default';
                }) },
            ],
        });

        // LoRAs (installed overview)
        html += '<div class="card mt-16"><h2>Installed LoRAs</h2>';
        var loras = App.state.loras || [];
        if (loras.length === 0) {
            html += '<div class="empty-state">No LoRAs installed. Search and download on the Setup tab, then pick one when spawning a worker on Server.</div>';
        } else {
            html += '<table><thead><tr><th>Name</th><th>Size</th><th>Adapter File</th></tr></thead><tbody>';
            for (var li = 0; li < loras.length; li++) {
                var l = loras[li];
                var sizeStr = l.size_mb >= 1024 ? (l.size_mb / 1024).toFixed(1) + ' GB' : l.size_mb.toFixed(0) + ' MB';
                html += '<tr>';
                html += '<td style="font-family:var(--font-mono);font-size:12px">' + E(l.name) + '</td>';
                html += '<td>' + sizeStr + '</td>';
                html += '<td>' + (l.has_adapter ? '<span class="badge badge-green">OK</span>' : '<span class="badge badge-red">Missing</span>') + '</td>';
                html += '</tr>';
            }
            html += '</tbody></table>';
        }
        html += '</div>';

        }

        if (comfyOnly) {
        html += this._renderComfyTemplateSection(models);

        // Install jobs
        html += '<div class="card mt-16"><h2>Download Jobs</h2>';
        html += '<div id="models-install-jobs-list"><div class="empty-state">No download jobs</div></div>';
        html += '</div>';

        // ComfyUI models (filesystem scan)
        html += '<div class="card mt-16"><h2>ComfyUI Models</h2>';
        html += '<p style="color:var(--text-secondary);font-size:12px;margin-bottom:12px">';
        html += 'Filesystem scan of WSL-backed ComfyUI weight folders. Install missing native-template models above or paste a direct HuggingFace/Xet file link.';
        html += '</p>';

        var totalModels = 0;
        for (var i = 0; i < categories.length; i++) {
            totalModels += models[categories[i]].length;
        }

        if (totalModels === 0) {
            html += '<div class="empty-state">No ComfyUI models found yet.<br>';
            html += 'Use the ComfyUI Template Models section above to install native-template requirements.</div>';
        } else {
            for (var j = 0; j < categories.length; j++) {
                var cat = categories[j];
                var files = models[cat];
                if (files.length === 0) continue;

                html += '<div class="mt-16">';
                html += '<div class="section-title">' + E(cat) + ' (' + files.length + ')</div>';
                html += '<table><thead><tr><th>Filename</th><th style="width:100px;text-align:right">Size</th></tr></thead><tbody>';
                for (var k = 0; k < files.length; k++) {
                    var f = files[k];
                    var fname = typeof f === 'object' ? f.name : f;
                    var fsize = typeof f === 'object' ? f.size_mb : 0;
                    var fsizeStr = fsize >= 1024 ? (fsize / 1024).toFixed(1) + ' GB' : fsize.toFixed(0) + ' MB';
                    html += '<tr><td style="font-family:var(--font-mono);font-size:12px">' + E(fname) + '</td>';
                    html += '<td style="text-align:right;color:var(--text-muted);font-size:12px">' + fsizeStr + '</td></tr>';
                }
                html += '</tbody></table></div>';
            }
        }
        html += '</div>';
        }

        el.innerHTML = html;
        this.renderInstallJobs();
    },

    _renderComfyTemplateSection: function(models) {
        var E = App.esc;
        if (!this.comfyTemplatesLoaded) {
            var loading = this.comfyTemplatesLoading;
            return '<div class="card mt-16"><h2>ComfyUI template catalog</h2>'
                + '<p style="color:var(--text-secondary);font-size:12px;margin-bottom:10px">Load this large optional catalog only when you need native ComfyUI template requirements.</p>'
                + '<button class="btn btn-sm" type="button" onclick="TabModels.loadComfyTemplates()"'
                + (loading ? ' disabled' : '') + '>' + (loading ? 'Loading catalog…' : 'Load template catalog') + '</button></div>';
        }
        var report = this.comfyTemplates || {};
        var rows = report.models || [];
        var filter = (this.comfyTemplateFilter || '').toLowerCase();
        var filtered = rows.filter(function(m) {
            if (!filter) return true;
            var hay = [
                m.name || '',
                m.category || '',
                (m.templates || []).join(' '),
                m.url || '',
                m.source || '',
            ].join(' ').toLowerCase();
            return hay.indexOf(filter) !== -1;
        });
        var status = this.comfyTemplateStatus || 'missing';
        filtered = filtered.filter(function(m) {
            var installed = !!m.installed;
            var downloadable = !installed && !!m.downloadable && !!m.url;
            if (status === 'missing') return !installed;
            if (status === 'downloadable') return downloadable;
            if (status === 'installed') return installed;
            if (status === 'needs-link') return !installed && !downloadable;
            return true;
        });
        var installedCount = rows.filter(function(m) { return !!m.installed; }).length;
        var downloadableCount = rows.filter(function(m) {
            return !m.installed && !!m.downloadable && !!m.url;
        }).length;
        var needsLinkCount = rows.filter(function(m) {
            return !m.installed && !(m.downloadable && m.url);
        }).length;
        var categories = this.comfyCategoryOptions(models, report);
        if (categories.indexOf(this.comfyDirectCategory) === -1 && categories.length) {
            this.comfyDirectCategory = categories[0];
        }

        var html = '<div class="card mt-16 comfy-template-card"><h2>ComfyUI Template Models</h2>';
        html += '<p style="color:var(--text-secondary);font-size:12px;margin-bottom:8px">';
        html += 'Scans the native ComfyUI templates from the installed git checkout and installs model files into WSL-backed ComfyUI folders using HuggingFace Hub with hf_xet.';
        html += '</p>';
        html += '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">';
        html += '<button class="btn btn-sm" onclick="TabModels.loadComfyTemplates()">Refresh scan</button>';
        html += '<span class="badge badge-blue">' + E(String(report.total_templates || report.total_blueprints || 0)) + ' templates</span>';
        html += '<span class="badge badge-gray">' + E(String(report.total_models || rows.length)) + ' model refs</span>';
        html += '<span class="badge badge-orange">' + E(String(report.total_missing || 0)) + ' missing</span>';
        if (report.total_undownloadable) {
            html += '<span class="badge badge-gray" title="No HuggingFace/Xet URL was found in the template for these references">'
                + E(String(report.total_undownloadable)) + ' need a link</span>';
        }
        html += '</div>';

        html += '<div class="field-grid comfy-model-tools" style="margin-bottom:12px">';
        html += '<div class="field-row"><label for="comfy-template-filter">Search template models</label>';
        html += '<input id="comfy-template-filter" type="text" value="' + E(this.comfyTemplateFilter || '') + '" '
            + 'placeholder="filename, category, template, URL" oninput="TabModels.setComfyTemplateFilter(this.value)"></div>';
        html += '<div class="field-row"><label for="comfy-template-batch-category">Batch downloads (advanced)</label>';
        html += '<div style="display:flex;gap:8px;flex-wrap:wrap">';
        html += '<select id="comfy-template-batch-category" aria-label="Batch model folder" onchange="TabModels.comfyTemplateBatchCategory=this.value">';
        html += '<option value="all"' + (this.comfyTemplateBatchCategory === 'all' ? ' selected' : '') + '>all folders</option>';
        for (var bci = 0; bci < categories.length; bci++) {
            var batchCat = categories[bci];
            html += '<option value="' + E(batchCat) + '"' + (batchCat === this.comfyTemplateBatchCategory ? ' selected' : '') + '>' + E(batchCat) + '</option>';
        }
        html += '</select>';
        html += '<input id="comfy-template-batch-limit" aria-label="Maximum models in this batch" type="number" min="1" max="2000" value="' + E(this.comfyTemplateBatchLimit || 50) + '" style="width:100px" onchange="TabModels.comfyTemplateBatchLimit=this.value">';
        html += '<button class="btn btn-sm" onclick="TabModels.installComfyTemplateMissing(this)"'
            + ((report.total_downloadable_missing || 0) ? '' : ' disabled') + '>Download batch...</button>';
        html += '</div><span class="field-hint">Starts only the selected missing files. Up to 3 transfers run concurrently and download CPU work is capped to one-third of the CPUs available to this distro.</span></div>';
        html += '<div class="field-row comfy-direct-download-field"><label for="comfy-direct-url">Direct HF/Xet link</label>';
        html += '<div class="comfy-direct-download-controls">';
        html += '<select id="comfy-direct-category" aria-label="Destination model folder" onchange="TabModels.comfyDirectCategory=this.value">';
        for (var ci = 0; ci < categories.length; ci++) {
            var cat = categories[ci];
            html += '<option value="' + E(cat) + '"' + (cat === this.comfyDirectCategory ? ' selected' : '') + '>' + E(cat) + '</option>';
        }
        html += '</select>';
        html += '<input id="comfy-direct-url" type="url" placeholder="https://huggingface.co/org/repo/resolve/main/path/model.safetensors">';
        html += '<input id="comfy-direct-name" type="text" aria-label="Optional destination filename" placeholder="optional filename">';
        html += '<button class="btn btn-sm btn-primary" onclick="TabModels.downloadComfyUrl(this)">Download link</button>';
        html += '</div></div></div>';

        html += '<div class="model-view-switch comfy-model-status-switch" role="group" aria-label="Template model status">';
        var statusButtons = [
            { id: 'missing', label: 'Missing', count: report.total_missing || (downloadableCount + needsLinkCount) },
            { id: 'downloadable', label: 'Ready to download', count: downloadableCount },
            { id: 'installed', label: 'Installed', count: installedCount },
            { id: 'needs-link', label: 'Needs link', count: needsLinkCount },
            { id: 'all', label: 'All', count: rows.length },
        ];
        for (var si = 0; si < statusButtons.length; si++) {
            var sb = statusButtons[si];
            html += '<button type="button" class="btn btn-sm' + (status === sb.id ? ' btn-primary' : '')
                + '" onclick="TabModels.setComfyTemplateStatus(\'' + sb.id + '\')">'
                + E(sb.label + ' (' + sb.count + ')') + '</button>';
        }
        html += '</div>';

        if (!rows.length) {
            html += '<div class="empty-state">No ComfyUI template model references found in the installed checkout.</div>';
            html += '</div>';
            return html;
        }
        if (!filtered.length) {
            html += '<div class="empty-state">No template model rows match this search.</div>';
            html += '</div>';
            return html;
        }

        html += '<div class="table-wrap comfy-template-table"><table><thead><tr><th class="comfy-template-model">Model</th><th class="comfy-template-folder">Folder</th><th class="comfy-template-uses">Templates</th><th class="comfy-template-source">Source</th><th class="comfy-template-status">Status</th><th class="comfy-template-action"></th></tr></thead><tbody>';
        var maxRows = Math.min(filtered.length, this.comfyTemplateVisibleLimit || 40);
        for (var i = 0; i < maxRows; i++) {
            var m = filtered[i];
            var installed = !!m.installed;
            var downloadable = !!m.downloadable && !!m.url;
            var templates = (m.templates || [m.template || m.blueprint || '']).filter(Boolean);
            var title = templates.slice(0, 6).join(', ');
            if (templates.length > 6) title += ' +' + (templates.length - 6) + ' more';
            html += '<tr>';
            html += '<td class="comfy-template-model" style="font-family:var(--font-mono);font-size:12px">' + E(m.name || '') + '</td>';
            html += '<td class="comfy-template-folder"><span class="badge badge-blue">' + E(m.category || '') + '</span></td>';
            html += '<td class="comfy-template-uses" style="font-size:12px;color:var(--text-secondary)" title="' + E(templates.join(', ')) + '">' + E(title || '-') + '</td>';
            html += '<td class="comfy-template-source" style="font-size:12px">' + E((m.sources || [m.source || '']).filter(Boolean).join(', ') || '-') + '</td>';
            html += '<td class="comfy-template-status">' + (installed
                ? '<span class="badge badge-green">installed</span>'
                : '<span class="badge badge-orange">missing</span>') + '</td>';
            html += '<td class="comfy-template-action">';
            if (installed) {
                html += '<span style="color:var(--text-muted);font-size:11px">ready</span>';
            } else if (downloadable) {
                html += '<button class="btn btn-sm btn-primary" data-category="' + E(m.category || '') + '" data-url="' + E(m.url || '') + '" data-name="' + E(m.name || '') + '" onclick="TabModels.downloadComfyTemplateModel(this)">Download</button>';
            } else {
                html += '<span style="color:var(--text-muted);font-size:11px" title="' + E(m.reason || 'No HuggingFace/Xet URL found') + '">needs link</span>';
            }
            html += '</td></tr>';
        }
        html += '</tbody></table></div>';
        if (filtered.length > maxRows) {
            html += '<div class="flex-between" style="gap:12px;flex-wrap:wrap;margin-top:10px">';
            html += '<p style="color:var(--text-muted);font-size:12px;margin:0">Showing ' + maxRows + ' of ' + filtered.length + ' matches. Search by workflow, filename, or folder to narrow the list.</p>';
            html += '<button type="button" class="btn btn-sm" onclick="TabModels.showMoreComfyTemplateModels()">Show 40 more</button></div>';
        }
        html += '</div>';
        return html;
    },

    _renderAssetSection: function(cfg) {
        var E = App.esc;
        var html = '<details class="advanced-panel mt-16"><summary>' + E(cfg.title) + ' weights</summary><div class="advanced-panel-body">';
        html += '<p style="color:var(--text-secondary);font-size:12px;margin-bottom:8px">' + cfg.subtitle + '</p>';
        html += '<button class="btn btn-sm mb-8" onclick="TabModels.openTab(\'' + E(cfg.tab) + '\')">Open ' + E(cfg.title) + ' tab</button>';

        var anyRows = false;
        for (var g = 0; g < cfg.groups.length; g++) {
            var group = cfg.groups[g];
            if (!group.list || !group.list.length) continue;
            anyRows = true;
            html += '<div class="mt-16"><div class="section-title">' + E(group.label) + '</div>';
            html += '<table><thead><tr><th>Name</th><th>Repo</th><th>Size</th><th>Status</th><th></th></tr></thead><tbody>';
            for (var i = 0; i < group.list.length; i++) {
                var entry = group.list[i];
                var key = entry.variant_id || entry.name || '';
                var installed = !!entry.installed;
                var sizeStr = entry.size_gb ? ('~' + entry.size_gb + ' GB') : (entry.size || '');
                var repoCell = entry.repo
                    ? '<a href="https://huggingface.co/' + E(entry.repo) + '" target="_blank" rel="noopener" style="color:inherit;font-family:var(--font-mono);font-size:11px">'
                        + E(entry.repo) + ' ↗</a>'
                    : '';
                html += '<tr>';
                html += '<td>' + E(entry.display || key) + (entry.gated ? ' <span class="badge badge-orange" title="May require HuggingFace access if install fails">gated</span>' : '') + '</td>';
                html += '<td>' + repoCell + '</td>';
                html += '<td>' + E(sizeStr) + '</td>';
                html += '<td>' + (installed
                    ? '<span class="badge badge-green">installed</span>'
                    : '<span class="badge badge-gray">not installed</span>') + '</td>';
                html += '<td>';
                if (installed) {
                    html += '<span style="color:var(--text-muted);font-size:11px">ready to load</span>';
                } else {
                    html += '<button class="btn btn-sm btn-primary" data-kind="' + E(group.kind) + '" data-key="' + E(key) + '" '
                        + 'onclick="TabModels.downloadAsset(this)">Download</button>';
                }
                html += '</td></tr>';
            }
            html += '</tbody></table></div>';
        }
        if (!anyRows) {
            html += '<div class="empty-state">No assets in registry.</div>';
        }
        html += '</div></details>';
        return html;
    },

    downloadAsset: async function(btn) {
        var kind = btn.dataset.kind;
        var key = btn.dataset.key;
        var path, body;
        if (kind === 'audio-model') {
            path = '/api/audio_lab/install-model';
            body = { variant_id: key };
        } else if (kind === 'audio-clap') {
            path = '/api/audio_lab/install-clap';
            body = { variant_id: key };
        } else if (kind === 'audio-vae') {
            path = '/api/audio_lab/install-vae';
            body = { variant_id: key };
        } else if (kind === 'ace-model') {
            path = '/api/ace_step/install-model';
            body = { variant_id: key };
        } else if (kind === 'ace-lm') {
            path = '/api/ace_step/install-lm';
            body = { variant_id: key };
        } else if (kind === 'ace-vae') {
            path = '/api/ace_step/install-vae';
            body = { variant_id: key };
        } else {
            App.toast('Unknown asset kind: ' + kind, 'error');
            return;
        }

        btn.disabled = true;
        btn.textContent = 'Starting…';
        try {
            var data = await App.api('POST', path, body);
            await App.loadInstallJobs();
            var note = '';
            if (kind === 'ace-model') {
                var aceModels = this.aceStep.models || [];
                for (var i = 0; i < aceModels.length; i++) {
                    if (aceModels[i].variant_id === key && aceModels[i].format === 'native') {
                        note = ' plus shared core/LM if missing';
                        break;
                    }
                }
            }
            App.toast('Downloading ' + key + note + '…', 'info');
            btn.textContent = 'Downloading…';
            this._pollJob(data.job_id, btn, function() { TabModels.load(); });
        } catch (e) {
            App.toast('Download failed: ' + e.message, 'error');
            btn.disabled = false;
            btn.textContent = 'Download';
        }
    },

    installComfyTemplateMissing: async function(btn) {
        try {
            var limitEl = document.getElementById('comfy-template-batch-limit');
            var categoryEl = document.getElementById('comfy-template-batch-category');
            var limit = parseInt((limitEl && limitEl.value) || this.comfyTemplateBatchLimit || 50, 10);
            if (!isFinite(limit) || limit < 1) limit = 50;
            if (limit > 2000) limit = 2000;
            this.comfyTemplateBatchLimit = limit;
            var category = (categoryEl && categoryEl.value) || this.comfyTemplateBatchCategory || 'all';
            this.comfyTemplateBatchCategory = category;
            var scopeLabel = category === 'all' ? 'all model folders' : category;
            if (!confirm(
                'Download up to ' + limit + ' missing ComfyUI model file(s) from ' + scopeLabel + '?\n\n'
                + 'Model files can be very large. Up to 3 transfers will run concurrently.'
            )) return;
            btn.disabled = true;
            btn.textContent = 'Starting...';
            var body = {
                missing_only: true,
                limit: limit,
            };
            if (category && category !== 'all') body.categories = [category];
            var data = await App.api('POST', '/api/assets/comfy/templates/install', {
                missing_only: body.missing_only,
                limit: body.limit,
                categories: body.categories || [],
            });
            if (!data.job_id) {
                App.toast(data.message || 'No missing template models with HuggingFace/Xet links', 'info');
                btn.disabled = false;
                btn.textContent = 'Download batch...';
                await this.load();
                return;
            }
            await App.loadInstallJobs();
            App.toast('Installing ' + data.install_count + ' ComfyUI template models...', 'info');
            btn.textContent = 'Downloading...';
            this._pollJob(data.job_id, btn, function() { TabModels.load(); });
        } catch (e) {
            App.toast('Template install failed: ' + e.message, 'error');
            btn.disabled = false;
            btn.textContent = 'Download batch...';
        }
    },

    downloadComfyTemplateModel: async function(btn) {
        var category = btn.dataset.category || '';
        var url = btn.dataset.url || '';
        var name = btn.dataset.name || '';
        if (!category || !url) {
            App.toast('Missing template model download data', 'error');
            return;
        }
        btn.disabled = true;
        btn.textContent = 'Starting...';
        try {
            var data = await App.api(
                'POST',
                '/api/assets/comfy/' + encodeURIComponent(category) + '/install-url',
                { url: url, name: name }
            );
            await App.loadInstallJobs();
            App.toast('Downloading ' + (data.name || name || category) + '...', 'info');
            btn.textContent = 'Downloading...';
            this._pollJob(data.job_id, btn, function() { TabModels.load(); });
        } catch (e) {
            App.toast('Download failed: ' + e.message, 'error');
            btn.disabled = false;
            btn.textContent = 'Retry';
        }
    },

    downloadComfyUrl: async function(btn) {
        var categorySel = document.getElementById('comfy-direct-category');
        var urlInput = document.getElementById('comfy-direct-url');
        var nameInput = document.getElementById('comfy-direct-name');
        var category = categorySel ? categorySel.value : this.comfyDirectCategory;
        var url = urlInput ? urlInput.value.trim() : '';
        var name = nameInput ? nameInput.value.trim() : '';
        if (!category || !url) {
            App.toast('Paste a HuggingFace/Xet model file URL first', 'error');
            return;
        }
        this.comfyDirectCategory = category;
        btn.disabled = true;
        btn.textContent = 'Starting...';
        try {
            var body = { url: url };
            if (name) body.name = name;
            var data = await App.api(
                'POST',
                '/api/assets/comfy/' + encodeURIComponent(category) + '/install-url',
                body
            );
            await App.loadInstallJobs();
            App.toast('Downloading ' + (data.name || category) + '...', 'info');
            if (urlInput) urlInput.value = '';
            if (nameInput) nameInput.value = '';
            btn.textContent = 'Downloading...';
            this._pollJob(data.job_id, btn, function() { TabModels.load(); });
        } catch (e) {
            App.toast('Direct link failed: ' + e.message, 'error');
            btn.disabled = false;
            btn.textContent = 'Download link';
        }
    },

    _pollJob: function(jobId, btn, onDone) {
        var poll = setInterval(async function() {
            if (btn && !document.body.contains(btn)) {
                clearInterval(poll);
                return;
            }
            try {
                var job = await App.api('GET', '/api/setup/jobs/' + jobId);
                await App.loadInstallJobs();
                if (job.status === 'completed') {
                    clearInterval(poll);
                    App.toast('Download complete', 'success');
                    if (btn && document.body.contains(btn)) {
                        btn.disabled = false;
                        btn.textContent = 'Downloaded';
                    }
                    if (typeof onDone === 'function') await onDone();
                } else if (job.status === 'failed' || job.status === 'cancelled') {
                    clearInterval(poll);
                    if (job.status === 'failed' && window.TabSetup && TabSetup.showInstallFailure) {
                        TabSetup.showInstallFailure(job);
                    }
                    App.toast('Download ' + job.status + (job.error ? ': ' + job.error.slice(-200) : ''), 'error');
                    if (btn && document.body.contains(btn)) {
                        btn.disabled = false;
                        btn.textContent = 'Retry';
                    }
                }
            } catch (e) {
                clearInterval(poll);
                if (btn && document.body.contains(btn)) {
                    btn.disabled = false;
                    btn.textContent = 'Retry';
                }
            }
        }, 3000);
    },

    renderInstallJobs: function() {
        var panel = document.getElementById('models-install-jobs-list');
        if (!panel) return;
        var jobs = App.state.installJobs || [];
        var E = App.esc;
        if (jobs.length === 0) {
            panel.innerHTML = '<div class="empty-state">No download jobs yet. Downloads you start above appear here with live status.</div>';
            return;
        }
        var html = '<table><thead><tr><th>Job</th><th>Kind</th><th>Target</th><th>Status</th><th></th></tr></thead><tbody>';
        for (var i = 0; i < jobs.length; i++) {
            var j = jobs[i];
            var target = j.variant_display || j.model || j.name || j.repo || '';
            var canCancel = j.status === 'running' || j.status === 'cancelling';
            var helper = (window.TabSetup && TabSetup._jobHelperHtml) ? TabSetup._jobHelperHtml(j, true) : '';
            var progress = (window.TabSetup && TabSetup._jobProgressHtml) ? TabSetup._jobProgressHtml(j) : '';
            var warning = (window.TabSetup && TabSetup._jobWarningHtml) ? TabSetup._jobWarningHtml(j) : '';
            var attempt = (window.TabSetup && TabSetup._jobAttemptHtml) ? TabSetup._jobAttemptHtml(j) : '';
            html += '<tr>';
            var elapsed = typeof j.elapsed_seconds === 'number' ? this._formatElapsed(j.elapsed_seconds) : '';
            var details = [];
            if (j.pid) details.push('PID ' + j.pid);
            if (elapsed) details.push(elapsed);
            if (j.phase && (!j.progress || j.progress.message !== j.phase)) details.push(j.phase);
            html += '<td style="font-family:var(--font-mono);font-size:12px">' + E(j.job_id);
            if (details.length) html += '<div style="margin-top:4px;color:var(--text-secondary);font-family:var(--font-sans);font-size:11px">' + E(details.join(' / ')) + '</div>';
            html += '</td>';
            html += '<td>' + E(j.kind || '') + '</td>';
            html += '<td>' + E(target) + attempt + warning + (helper ? '<div style="margin-top:6px">' + helper + '</div>' : '') + '</td>';
            html += '<td><span class="badge badge-' + (j.status === 'completed' ? 'green' : (j.status === 'failed' ? 'red' : (j.status === 'cancelled' ? 'gray' : 'orange'))) + '">' + E(j.status || '') + '</span>' + progress + '</td>';
            html += '<td>';
            if (canCancel) {
                html += '<button class="btn btn-danger btn-sm" data-job="' + E(j.job_id) + '" onclick="TabModels.cancelJob(this.dataset.job)">Cancel</button>';
            }
            if ((j.log_available || j.output) && window.TabSetup && TabSetup.showJobOutput) {
                html += '<button class="btn btn-sm" style="margin-left:6px" data-job="' + E(j.job_id) + '" onclick="TabSetup.showJobOutput(this.dataset.job)">Log</button>';
            }
            html += '</td>';
            html += '</tr>';
        }
        html += '</tbody></table>';
        panel.innerHTML = html;
    },

    _formatElapsed: function(seconds) {
        var total = Math.max(0, Math.floor(seconds || 0));
        if (total < 60) return total + 's';
        var minutes = Math.floor(total / 60);
        var remainder = total % 60;
        if (minutes < 60) return minutes + 'm ' + remainder + 's';
        var hours = Math.floor(minutes / 60);
        return hours + 'h ' + (minutes % 60) + 'm';
    },

    cancelJob: async function(jobId) {
        if (!confirm('Cancel download job ' + jobId + '?')) return;
        try {
            await App.api('POST', '/api/setup/jobs/' + encodeURIComponent(jobId) + '/cancel');
            App.toast('Download job cancelling', 'info');
            await App.loadInstallJobs();
        } catch (e) {
            App.toast('Cancel failed: ' + e.message, 'error');
        }
    },
};
