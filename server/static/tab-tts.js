/* ========================================================================== 
   Voice & TTS hub — reserved for the upcoming multi-engine TTS library
   ========================================================================== */

var TabTTS = {
    init: function() { this.render(); },

    render: function() {
        var el = document.getElementById('tab-tts');
        if (!el) return;
        el.innerHTML = ''
            + '<div class="page-heading">'
            +   '<div class="page-heading-copy">'
            +     '<div class="page-eyebrow">Audio workspace</div>'
            +     '<h1>Voice &amp; TTS</h1>'
            +     '<p>A dedicated home for speech engines, voices, comparison, and batch synthesis. This surface is ready for the upcoming TTS library migration.</p>'
            +   '</div>'
            + '</div>'
            + '<section class="tts-hero">'
            +   '<span class="tts-hero-status">Engine migration planned</span>'
            +   '<h2>One clear voice workflow, many engines underneath</h2>'
            +   '<p>The future library will bring roughly 25 TTS engines into one consistent experience: choose an engine and voice, tune delivery, preview alternatives, then save the result to the Media library.</p>'
            +   '<div class="next-step-actions">'
            +     '<button class="btn btn-primary" type="button" onclick="App.activateTab(\'moss\')">Open MOSS</button>'
            +     '<button class="btn" type="button" onclick="App.activateTab(\'audio-lab\')">Open Audio Lab</button>'
            +   '</div>'
            +   '<p>MOSS sound effects have a verified generation path. Fresh MOSS speech generation is currently blocked by a decoder failure on the tested build.</p>'
            + '</section>'
            + '<div class="tts-roadmap" aria-label="Planned TTS workflow">'
            +   '<article class="tts-roadmap-card"><span>01 · Discover</span><strong>Engine and voice library</strong><p>Searchable capabilities, languages, licenses, quality, speed, and hardware needs without a wall of technical controls.</p></article>'
            +   '<article class="tts-roadmap-card"><span>02 · Create</span><strong>Unified synthesis studio</strong><p>Text, voice, delivery, pronunciation, and output settings in a predictable form shared by every compatible engine.</p></article>'
            +   '<article class="tts-roadmap-card"><span>03 · Compare</span><strong>Side-by-side results</strong><p>Generate candidates, listen without losing context, pin favorites, and send finished audio directly to the Media library.</p></article>'
            + '</div>'
            + '<div class="home-section">'
            +   '<div class="home-section-heading"><div><h2>Design guardrails for the migration</h2><p>Keeping a large engine catalog understandable from day one.</p></div></div>'
            +   '<div class="readiness-grid">'
            +     '<div class="readiness-item"><div class="readiness-item-label">Default view</div><strong>Task-first</strong><small>Users choose what they want to make before choosing an engine.</small></div>'
            +     '<div class="readiness-item"><div class="readiness-item-label">Advanced controls</div><strong>Progressive</strong><small>Engine-specific settings stay collapsed until requested.</small></div>'
            +     '<div class="readiness-item"><div class="readiness-item-label">Runtime</div><strong>One engine at a time</strong><small>Explicit load and unload state prevents hidden resource pressure.</small></div>'
            +     '<div class="readiness-item"><div class="readiness-item-label">Output</div><strong>One library</strong><small>Every result uses the same preview, metadata, and collection flow.</small></div>'
            +   '</div>'
            + '</div>';
    },
};
