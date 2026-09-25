'use strict';
// Execute UI modules against a small fake DOM; no browser/network is launched.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function load(name, exposed) {
    let source = fs.readFileSync(path.join(__dirname, '../server/static', name), 'utf8');
    if (exposed) {
        const at = source.lastIndexOf('    return {');
        assert(at >= 0);
        source = source.slice(0, at) + source.slice(at).replace('    return {', `    return {_test: {${exposed}},`);
    }
    const context = {
        console, setTimeout, clearTimeout, setInterval, clearInterval,
        confirm: () => true,
        window: {},
        document: {getElementById: () => null, querySelectorAll: () => [], querySelector: () => null},
        localStorage: {getItem: () => null, setItem: () => {}},
        App: {state: {models: {}, devices: [], comfyInstances: [], workflows: []},
              esc: s => String(s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/'/g, '&#39;'),
              toast: () => {}, modelDisplay: x => x, modelCapabilities: () => ({})},
    };
    vm.createContext(context);
    vm.runInContext(source, context, {filename: name});
    return context;
}

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return {promise, resolve};
}

async function main() {
    const chat = load('tab-chat.js', 'state, renderMessage, modelCaps');
    const html = chat.TabChat._test.renderMessage({role: 'user', content: '', image: 'bad" onerror="alert(1)', image_mime: 'image/png" onload="bad'});
    assert(!html.includes(' onerror="'));
    assert(!html.includes(' onload="'));
    assert(html.includes('&quot;'));
    assert.equal(chat.TabChat._test.modelCaps('qwen_omni_3b').audio, false);
    assert.equal(chat.TabChat._test.modelCaps('qwen_omni_7b').video, false);
    assert.equal(chat.TabChat._test.modelCaps('minicpm_o').audio, true);
    assert.equal(chat.TabChat._test.modelCaps('anygpt').image, false);

    const media = load('tab-media.js', 'state, refresh, bulkDelete');
    const oldReply = deferred(), newReply = deferred();
    media.App.api = async (method, url) => {
        if (url === '/api/outputs/collections') return {collections: []};
        return url.includes('kind=output') ? oldReply.promise : newReply.promise;
    };
    const m = media.TabMedia._test;
    const oldRequest = m.refresh();
    m.state.source = 'input';
    const newRequest = m.refresh();
    newReply.resolve({files: [{path: 'new.png'}], total: 1});
    await newRequest;
    oldReply.resolve({files: [{path: 'old.png'}], total: 1});
    await oldRequest;
    assert.equal(m.state.files[0].path, 'new.png');
    assert.equal(m.state.files[0].storage_kind, 'input');
    const deletion = deferred();
    const urls = [];
    media.App.api = async (method, url) => {
        if (method !== 'DELETE') return {files: [], total: 0, collections: []};
        urls.push(url);
        if (urls.length === 1) await deletion.promise;
        return {};
    };
    m.state.files = [{path: 'a.png', size: 1}, {path: 'b.png', size: 1}];
    m.state.selection = {'a.png': true, 'b.png': true};
    const deleting = m.bulkDelete();
    m.state.source = 'output';
    deletion.resolve();
    await deleting;
    assert.equal(urls.length, 2);
    assert(urls.every(url => url.endsWith('kind=input')));

    const results = {innerHTML: ''};
    media.document.getElementById = id => id === 'm-results' ? results : null;
    media.App.api = async () => ({files: [], total: 0, collections: []});
    m.state.search = 'no-match-';
    await m.refresh();
    assert(results.innerHTML.includes('Clear filters'));
    assert(!results.innerHTML.includes('Loading'));

    const workflows = load('tab-workflows.js');
    const w = workflows.TabWorkflows;
    let delegatedAdds = 0;
    const root = {addEventListener: () => { delegatedAdds++; }};
    workflows.document.getElementById = id => id === 'tab-workflows' ? root : null;
    w.bindEvents(); w.bindEvents();
    assert.equal(delegatedAdds, 0);
    assert.equal(typeof root.onclick, 'function');
    workflows.App.state.devices = [{id: 'cuda:0'}, {id: 'cuda:1'}];
    workflows.App.state.comfyInstances = [{instance_id: 'test', status: 'ready', gpu_pool: ['cuda:1', 'cuda:0']}];
    w.selectedInstance = 'test';
    assert.equal(w.placementDevices()[0].id, 'cuda:1');
    workflows.App.state.workflows = [{filename: 'saved.json', placement_policy: {mode: 'manual', reserve_mb: 2048, primary_device: 'GPU-X', eligible_devices: ['GPU-X']}}];
    assert.equal(w.placementPolicy('saved.json').reserve_mb, 2048);
    assert.equal(w.placementPrimary, 'GPU-X');
    const planning = deferred();
    let plannedContext;
    w.planWorkflow = async (filename, button, context) => { plannedContext = context; return planning.promise; };
    w.placementConfirmation = () => true;
    const posts = [];
    workflows.App.api = async (method, url, body) => { posts.push(body); return {}; };
    const running = w.queue('saved.json');
    await w.queue('saved.json'); // double click must not enqueue a second run
    w.selectedInstance = 'different';
    planning.resolve({valid: true});
    await running;
    assert.equal(posts.length, 1);
    assert.equal(posts[0].instance_id, 'test');
    assert.equal(posts[0].placement, plannedContext.placement);
    const music = load('tab-minimax-music3.js');
    const mm = music.TabMiniMaxMusic3;
    const fields = {'mm3-prompt': {value: 'music'}, 'mm3-lyrics': {value: 'lyrics'}, 'mm3-duration': {value: '60'}, 'mm3-seed': {value: '1'}};
    music.document.getElementById = id => fields[id] || null;
    mm.render = () => {};
    mm.refresh = async () => {};
    mm.workerId = 'chosen-worker';
    const generation = deferred();
    const calls = [];
    music.App.api = async (method, url, body) => {
        calls.push({url, body});
        if (url.endsWith('/generate')) return generation.promise;
        return {};
    };
    const producing = mm.generate();
    mm.workerId = 'different-worker';
    await mm.cancel();
    generation.resolve({results: [{url: '/audio'}], job_id: 'test'});
    await producing;
    assert.equal(calls[0].body.worker_id, 'chosen-worker');
    assert(calls[1].url.endsWith('worker_id=chosen-worker'));
    console.log('UI audit regressions passed, including exact Music 3 worker cancellation.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
