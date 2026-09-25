# Documentation screenshots and bounded browser checks — 2026-09-25

## Delivered

Eighteen reviewed PNGs (2,227,248 bytes total) are stored in
[`manual/images/`](../manual/images/manifest.json). They cover all 14 main
navigation destinations, a 3200 × 1800 hero, the workflow editor, narrow Home,
and the Shutdown toolbar control. The README and corresponding manual pages
embed the images. The [gallery](../manual/screenshots.md) gives captions and
relative paths suitable for the future repository/wiki.

These are screenshots of the real local app. No API response mocks or
synthetic screen content were used for published images. Example text was
entered into ordinary unsent draft fields. Two browser-only response faults
were used separately for recovery verification and removed before capture.

## Tool and runtime provenance

- Read AgentRocko's `tests/browser/README.md` and its Python Playwright examples.
  Reused the already installed host Playwright/Chromium tooling rather than
  running AgentRocko's state-mutating qualification scripts.
- Python Playwright **1.58.0**, Chromium **145.0.7632.6**, headless Chromium on
  Windows. These capture the browser content, not native WebView2 window chrome.
- Desktop manual: 1440 × 1000 at scale 1. Hero: 1600 × 900 at scale 2.
  Narrow Home: 390 × 844. Editor and Shutdown: direct element screenshots.
- Short-lived scripts, profiles and raw captures:
  `%TEMP%\omni-wiki-capture-x5d7vvdl`.
- Asset dimensions, hashes and current static-source hashes:
  [`manual/images/manifest.json`](../manual/images/manifest.json).
- Live app: `http://127.0.0.1:9200`, distro `linbox-Omni_Studio`.
  The bridge's Windows loopback relay reported ready and served the browser.

The initial packaged launch failed because `server/setup.sh` contained CRLF
line endings. Its log reported `$'\r': command not found` and an invalid
`pipefail` option. Normalizing the file to LF fixed the source defect. The
existing `.gitattributes` already requested LF, but that did not normalize an
already present working-tree file. A raw-byte regression now covers the shell
entry points; text-mode reads had hidden the problem.

The distro contained older gateway files and lacked the `models`/`output`
runtime links and runtime cache. With ports 8200/9200 inactive, a source-only
refresh copied 72 changed gateway, bridge, CLI and guidance files and restored
the missing public links to `/var/lib/omni_studio`. Existing replaced files
were backed up to
`/opt/omni_studio/cache/tmp/wiki-capture-20260925/runtime-backup`; the sibling
`deployment.json` records the file list. The existing Python environment was
reused. Later static fixes were copied individually without a gateway restart.

The bridge reached gateway readiness after approximately 45 seconds. Home
reports zero installed standalone/audio model families, while retained
workflow/media data exists. Comfy installation status reports commit
`c67885b145` and eight template packages. No remote update check was requested.
This refresh does not qualify a complete installer run, dependency repair,
or deployment/execution of the separate Comfy custom-node changes.

## Verification

All 14 navigation destinations rendered with no uncaught JavaScript exceptions
or HTTP error responses during the initial capture sweep. Final screenshots
were visually inspected. Loading states in the first Comfy/workflow captures
were replaced with settled state; personal media thumbnails were replaced by
an ordinary filename-filtered empty view. No credentials or private account
panels appear in the delivered images.

The [machine-readable results](2026-09-25-browser-check-results.json) record
16 distinct passed checks:

| Check | Result and boundary |
| --- | --- |
| Idle baseline | Zero model workers and zero Comfy instances. |
| Home draft retention | LoRA search text survives an actual backend poll/render. No search or install submitted. |
| Session recovery | A browser-only 401 on workers causes token reacquisition and reconnect. No gateway restart. |
| Transport recovery | A browser-only connection refusal sets disconnected state; removing it restores connected state. |
| Chat modality gates | Qwen image enabled/audio-video disabled; MiniCPM audio enabled/video disabled; AnyGPT image disabled. |
| Chat session lifecycle | Create/select/delete one new empty session. Only that exact new session was deleted. |
| Music 3 | Readable layout; GPU, duration and draft retained after real Refresh; unloaded generation disabled. |
| Runtime API address | Copyable URL is the browser's bridge origin, `http://127.0.0.1:9200/v1`. |
| Media filter and views | Empty filename result reaches Clear filters; Grid/List switching works. |
| Audio state | ACE-Step, Audio Lab and Music 3 state calls with `autospawn=false` return `running:false`. |
| Comfy installation | Local status settles without starting an instance. |
| Workflow draft | Invalid JSON disables Check draft; valid unsaved JSON and filename survive rerender. |
| Shutdown confirmation | Cancelling the confirmation leaves the gateway running. |
| Narrow navigation | At 800 px, hamburger, Escape and scrim behave correctly. |
| Narrow layout | Home has no document overflow at 390 px. |
| Final idle state | Zero workers, zero Comfy instances and no uncaught JavaScript exceptions. |

The Media check failed before its fix: the API had completed but an empty
result remained on Loading. It failed again with a 60-second observation,
then passed immediately after the render-order correction. This was an app
defect, not a request timeout. The longer wait is not retained as the fix.

Focused source verification:

- `python -m pytest -q tests/test_startup_layout_regression.py`: **7 passed**,
  including raw LF validation.
- `node tests/test_ui_audit_repairs.js`: passed, including added empty-result
  rendering and chat-modality checks.
- `node --check` passed for changed Chat, Runtime and Media JavaScript.
- Explicit Git Bash `-n server/setup.sh`: passed after normalization.
- `python -m unittest tests.test_manual_coverage`: **5 passed**.
- All **18 PNG signatures, dimensions and SHA-256 hashes** match the manifest;
  all **20 embedded image references** resolve to delivered files.
- Git ignore checks confirm the selected documentation assets and gallery are
  eligible for the source repository. Nothing was staged, committed or pushed.

## New findings fixed during capture

| ID | Finding | Correction |
| --- | --- | --- |
| UI-01 | CRLF prevented the packaged setup shell script from executing in WSL. | Normalize to LF and check raw entrypoint bytes in regression coverage. |
| UI-02 | Music 3 used undefined layout/form classes, producing tiny inline textareas and ungrouped controls. | Scoped responsive card/form CSS; verified in actual Chromium. |
| UI-03 | Chat advertised Qwen audio/video and AnyGPT image/audio despite unwired handlers. | Gate by implemented input modalities, use a conservative unknown-model fallback, and verify attachment buttons. |
| UI-04 | Runtime's copyable API address used the internal gateway port returned behind the proxy. | Both displayed API address locations use the actual browser origin. Session metadata's internal base is not rewritten by this UI fix. |
| UI-05 | Empty Media results rendered while `loading` was still true, then never rendered after completion. | Render results in the guarded completion block after clearing `loading`; regression and live filter pass. |
| UI-06 | The routed Comfy skill still claimed staged graphs bypass host admission and generic VAE decode unloads ordinary upstream models. | Reconcile both statements with the repaired planner/node contract and canonical API guide. |

These are appended to the [original audit and repair ledger](2026-09-25-full-read-only-code-audit.md).

## Remaining boundaries

No weights, workflows, audio generations, external downloads, updates or GPU
loads were started for these captures. Existing workflow requirement reads do
not load weights. No user media/workflows were deleted or overwritten. The
gateway's normal maintenance scheduler ran after startup under its existing
policy; the capture scripts did not request maintenance operations.

Browser transport fault injection verifies frontend recovery and real session
bootstrap, not an actual gateway crash/restart or model worker recovery.
Heavy inference, upstream model failures, multi-GPU execution, subjective
audio quality and complete packaged setup remain separate qualification work.
The historical model grades are not upgraded by these screenshots.

All Playwright-owned browser contexts were closed. The bridge/gateway remains
available and idle for the user's next task; no model worker or Comfy instance
was left resident. No force-kill or distro shutdown was used.
