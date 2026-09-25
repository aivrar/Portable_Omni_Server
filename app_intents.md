# Omni Studio — App Intents

Intentional design decisions that may look wrong to auditors.
If you've reviewed an item, add your initials and date below it.

---

## 1. Module-level globals in omni_worker.py

`_model_name`, `_device`, `_model_obj`, `_loaded` etc. are module-level globals
rather than class members.

**Intent:** Each worker subprocess loads exactly one model. The process IS the
instance — there is no need for a class. Globals are set once at startup from
`__main__` args and never reassigned except by `/load` and `/unload`. This
matches the TTS server's proven `tts_worker.py` pattern.

Auditor: _____ Date: _____

---

## 2. Reconnect backoff uses bitwise shift in app.js

```js
var retryMs = Math.min(1000 * (1 << (this._backoff - 1)), 8000);
```

**Intent:** Binary exponential reconnect delay (1, 2, then 4 seconds with the
current `_backoff` clamp). A single reconnect timer prevents overlapping retry
loops. After the gateway returns, `recoverBackendState()` reacquires session
state and refreshes the active views instead of relying on skipped poll ticks.

Auditor: _____ Date: _____

---

## 3. Orphan cleanup uses app-owned pid files

`worker_manager.py` and `comfy_manager.py` write pid records under
`/opt/omni_studio/cache/runtime/pids`.

**Intent:** Cleanup must only target processes created by this app instance.
The pid records include the app instance path, pid, pgid, script, cwd, and
port. This avoids broad process-name sweeps that could kill unrelated
processes in the same WSL distro.

Auditor: _____ Date: _____

---

## 4. _LazyDevice class in config.py

`WORKER_DEFAULT_DEVICE` is a `str` subclass with overridden dunder methods that
defer to `_detect_default_device()`.

**Intent:** Avoids running `nvidia-smi` at import time, which would add 2-5s
to every `import config`. The lazy pattern is copied from the TTS server's
proven `config.py`. All string operations delegate to the cached result after
first access.

Auditor: _____ Date: _____

---

## 5. ComfyUI health check uses /system_stats not /health

ComfyUI's native API exposes `/system_stats` (returns JSON with device info)
but has no `/health` endpoint. This is not a mistake — it's what ComfyUI
provides.

Auditor: _____ Date: _____

---

## 6. Bridge proxy allows "/" path

`ProxyHandler._ALLOWED_PREFIXES` does not include `/`, but the code has an
explicit `if path_only != "/"` check that allows the root path through. This
is needed so the bridge can proxy the initial page load to the gateway's
`GET /` which serves `index.html`.

Auditor: _____ Date: _____

---

## 7. Moshi inference raises HTTPException (501)

Unlike other models that return results, `_infer_moshi()` raises a 501. This
is intentional — Moshi is a real-time full-duplex audio model that requires
WebSocket streaming. Batch POST inference is architecturally incompatible. A
streaming endpoint will be added when the Moshi integration matures.

The UI shows Moshi as installable because the weights are still needed for
future streaming support. The Testing tab disables Moshi for batch testing
until a streaming endpoint exists.

Auditor: _____ Date: _____

---

## 8. SSE log stream — per-client fan-out via broadcast

Each SSE client gets its own `asyncio.Queue`. The background tail task calls
`_broadcast()` which pushes to ALL registered client queues. On client
disconnect, the `finally` block in `event_generator()` removes the queue
from `_sse_clients`.

Per-client queues have a bounded size (500 entries). When full, oldest entries
are dropped for that client only — other clients are unaffected.

Auditor: _____ Date: _____

---

## 9. Runtime APP_DIR is /opt/omni_studio

```bash
APP_DIR="/opt/omni_studio"
SOURCE_APP_DIR="$(dirname "$SCRIPT_DIR")"
```

**Intent:** The Windows-mounted child app directory is only a setup source.
Live server code, models, workflows, output, and caches are copied or created
under `/opt/omni_studio` inside WSL ext4 for speed and isolation.

Auditor: _____ Date: _____

---

## 10. ComfyRegistry.register() also discards port from pool

```python
self._port_pool.discard(instance.port)
```

This is a safety belt, not the primary allocation mechanism. `allocate_port()`
removes the port via `.remove()`. The `discard()` in `register()` guards
against a hypothetical caller that constructs a `ComfyInstance` without going
through `allocate_port()`.

Auditor: _____ Date: _____

---

## 11. app.json starts the bridge; the bridge starts the API server

`app.json:start` launches `/opt/omni_studio/bridge.py`. The bridge checks
`_check_api_alive()` and starts `omni_comfy_server.py` if the gateway is absent.

**Intent:** The bridge owns gateway supervision while exposing the Windows
loopback port. Its alive check also lets an already-running gateway be adopted
without launching a duplicate.

Auditor: _____ Date: _____

---

## 12. Bridge hardcodes /opt/omni_studio paths instead of importing config

`bridge.py` uses hardcoded paths like `/opt/omni_studio/venv/bin/python3`
instead of importing from `config.py`.

**Intent:** The bridge runs as a standalone script before the venv exists.
It cannot import config.py because that module may import packages only
available inside the venv (e.g., `pathlib` extensions). The bridge must
be runnable with bare system Python. The paths match what setup.sh writes.

Auditor: _____ Date: _____

---

## 13. Gateway and bridge origin policy

The gateway no longer advertises cross-origin access. The bridge only reflects
same-host loopback origins and no longer emits `Access-Control-Allow-Origin: *`.

**Intent:** Workers, ComfyUI, the gateway, and the bridge bind to `127.0.0.1`
by default. If a launcher needs the bridge on a wider interface, it must opt in
with `OMNI_BRIDGE_HOST`, and the API session token still protects `/api/*`.

**2026-05-29 reconciliation:** the *code* now matches this intent. Previously
`bridge.py` defaulted both binds to `0.0.0.0` and `app.json` launched the
gateway with `--host 0.0.0.0`, contradicting this text. Now `bridge.py`
defaults `BIND_ADDR`/`API_BIND_HOST` to `127.0.0.1`, `app.json` no longer pins
`--host` (so the gateway uses its `127.0.0.1` argparse default), and a single
explicit opt-in — `OMNI_API_ALLOW_REMOTE=1` (or setting `OMNI_BRIDGE_HOST` /
`OMNI_API_HOST`) — widens the bind. WSL2 localhost-forwarding dials `127.0.0.1`
inside the VM, so the loopback bind keeps the WebView reachable.

Auditor: Claude (Opus 4.8) 2026-05-29

---

## 14. Local API session token

The gateway requires a per-instance token on `/api/*` routes except
`/api/session`. The UI obtains the token at startup and sends it in
`X-Omni-Token`. Its log `EventSource` uses the HttpOnly session cookie
established by `/api/session`, since browsers cannot set a custom header on
an `EventSource` request.

**Intent:** This remains a single-user local app, but the token closes the
browser-origin and accidental-network-exposure gap without introducing user
accounts. A local process can still read `/api/session`, which is acceptable
inside the current local trust boundary.

**2026-05-29 hardening:** `/api/session` (the one token-dispensing route that is
exempt from the token gate) is now additionally restricted to **loopback peers**
in the gateway middleware (`is_loopback_peer`), unless `OMNI_API_ALLOW_REMOTE=1`.
A missing `Origin` header is no longer treated as trusted for this route. With
the default loopback bind this never blocks legitimate traffic — every peer that
reaches a `127.0.0.1` socket is itself loopback — but it stops token disclosure
if the gateway is ever exposed directly. `/metrics` is now token-gated too, and
the unauthenticated OpenAPI docs (`/docs`, `/redoc`, `/openapi.json`) are
disabled unless `OMNI_ENABLE_DOCS=1`.

Auditor: Claude (Opus 4.8) 2026-05-29

---

## 15. InferRequest schema has audio/video/top_p fields

`omni_worker.py:InferRequest` includes `audio`, `video`, and `top_p` fields
that most model inference handlers currently ignore.

**Intent:** The fields remain in the typed compatibility schema, but their
presence does not claim that every worker accepts those modalities. Handlers
must reject unsupported combinations explicitly; the capability matrix and
model metadata are authoritative. Removing the fields would break existing
typed clients even where a current handler returns a clear unsupported error.

Auditor: _____ Date: _____

---

## 16. Full DOM re-render every 3s poll cycle

Tabs like Server and ComfyUI rebuild their entire innerHTML every poll tick.
This destroys scroll position and focus state.

**Intent:** Acceptable tradeoff for a local admin dashboard with simple tables.
Differential DOM updates (virtual DOM, morphdom) would add complexity and a
dependency for marginal benefit. The app now preserves focused control values
across poll-driven re-renders; if table size grows, the next step is to diff
only changed rows.

Auditor: _____ Date: _____

---

## 17. No system-wide drop_caches on process cleanup

Worker and ComfyUI cleanup does not write to `/proc/sys/vm/drop_caches`.
After the final workload exits, Omni may release page cache charged to its
empty workload cgroup through scoped `memory.force_empty`.

**Intent:** Dropping kernel caches affects the whole WSL distro, not just Omni
Studio. That violates the app's non-interference goal. WSL/VHDX compaction or
memory reclamation should be an explicit operator action, not a routine cleanup
side effect.

Auditor: _____ Date: _____

---

## 18. HF_TOKEN exported to subprocess environment in install_model.sh

`export HF_TOKEN="$(cat ...)"` makes the token visible in `/proc/$PID/environ`.

**Intent:** This is required by `huggingface_hub.snapshot_download()` which
reads the token from the `HF_TOKEN` environment variable. There is no
alternative mechanism. The subprocess is short-lived (install duration only)
and runs inside an isolated WSL2 distro. The token file is created atomically
with 0600 permissions before the token bytes are written.

Auditor: _____ Date: _____

---

## 19. app.json setup command uses /mnt/* glob

```json
"setup": "for f in /mnt/*/linux/template/...; do ..."
```

**Intent:** The template C launcher mounts Windows drives at `/mnt/c`, `/mnt/d`,
etc. The glob discovers which drive letter the app lives on. First match wins.
This is the same pattern used by all sibling apps (TTS, Psychedelia, TQ_Server).
If the app exists on multiple drives, the glob finds the first one alphabetically
— this is deterministic and matches user expectation (C: before D:).

Auditor: _____ Date: _____

---

## 20. EventSource auto-reconnects (browser spec)

`app.js` creates an `EventSource` and the `onerror` handler only updates the
badge. It does not manually reconnect.

**Intent:** Per the W3C EventSource specification, the browser automatically
reconnects after a connection drop. Manual reconnection code would interfere
with the built-in retry logic. The `onerror` handler updates the UI badge;
the `onopen` handler restores it on successful reconnect.

Auditor: _____ Date: _____

---

## 21. ComfyUI and ComfyUI Manager are pinned by default

`setup.sh` checks out fixed ComfyUI and ComfyUI Manager commits by default:

```bash
OMNI_COMFYUI_REF=1ac60da2c9c8f83654204b2a1db13908cf7614f7
OMNI_COMFYUI_MANAGER_REF=7955e638db7d4a4b8bf7a614e724a2013b83dfd7
```

**Intent:** Portable installs should not silently drift when upstream branches
move. Operators can override the refs with environment variables when they
intentionally want to test newer upstream code. `server/python_constraints.txt`
also constrains major-version drift for Python packages, but it is not a full
hash-locked dependency file.

Auditor: _____ Date: _____

---

## 22. Install timeout knob

`OMNI_INSTALL_TIMEOUT_SECONDS` controls the maximum runtime for a model,
variant, or LoRA install job. The default is 43200 seconds (12 hours).

**Intent:** Large model downloads need a long timeout, but stuck installs
should eventually fail and release `_install_active`. The UI now exposes a
cancel action for running jobs, so operators do not have to restart the gateway
just to stop a bad download.

Auditor: _____ Date: _____

---

## 23. Runtime artifacts and repository layout

The local child app contains opaque runtime artifacts such as
`wsl/ext4.vhdx`, along with the distro bootstrap archive and Windows launcher.
[`docs/repository-layout.md`](docs/repository-layout.md) identifies the durable
source, packaged artifacts, canonical distro paths, and generated material.

**Intent:** Apparent age or size does not make a runtime artifact disposable.
The source repository ignores these local artifacts; a packaged app needs them.
Before packaging, verify the launcher inputs and confirm the server and agent
guidance beneath `/opt/omni_studio` match this child source. One-off size
manifests are deliberately not kept because they become stale immediately.

Auditor: _____ Date: _____

---

## 24. Loopback-token auth stays the default

`OMNI_AUTH_MODE` defaults to `loopback-token`. `GET /api/session` returns
the per-instance token to a same-origin **loopback peer**; the bridge enforces
the loopback origin via `_LOOPBACK_ORIGIN_RE` (see `bridge.py`) and the gateway
middleware re-checks `origin_matches_host` and `is_loopback_peer`
(see `security.py`). (Symbol references are used here instead of line numbers,
which drift — this resolves the old `bridge.py:37` line-ref mismatch.)

**Intent:** The product surface is "an app on this machine that the WebView
talks to over loopback." We do not bind the bridge or gateway to anything other
than loopback unless the operator explicitly opts in (`OMNI_API_ALLOW_REMOTE=1`,
or a specific `OMNI_BRIDGE_HOST`/`OMNI_API_HOST`), and the session token is a
per-instance secret rotated on first launch via `security.get_or_create_token`.
Bearer mode is implemented and is selected with `OMNI_AUTH_MODE=bearer`;
loopback-token remains the default.

Auditor: Claude (Opus 4.8) 2026-05-29

---

## 25. JobStore unifies every long-running operation

`server/jobs.py` is the single home for model installs, asset installs,
custom-node clones, scheduled maintenance, venv repairs, and verify-model
runs. There are no longer four parallel `_install_*` globals or per-router
job dicts.

**Intent:** Cancellation, progress reporting, and eviction are tricky to
get right; one tested implementation is cheaper than four hand-maintained
variants. `cancel()` always escalates `SIGTERM` to `SIGKILL` after a 2 s
grace, and the active-key guard surfaces 409 to the caller before two
identical work items can run concurrently.

Auditor: _____ Date: _____

---

## 26. New env knobs (phase 1-12 overhaul)

| Knob | Default | Purpose |
|---|---|---|
| `OMNI_AUTH_MODE` | `loopback-token` | Select the optional bearer-key mode. |
| `OMNI_OUTPUT_MAX_GB` | 50 | Cap for `prune-outputs` maintenance task. |
| `OMNI_PIP_CACHE_MAX_GB` | 5 | Cap for `prune-pip` maintenance task. |
| `OMNI_MAX_JOBS` | 500 | Hard ceiling on stored Job records. |
| `OMNI_JOB_TTL_S` | 86400 | Eviction TTL for terminal jobs. |
| `OMNI_MAINTENANCE_TICK_S` | 600 | Scheduler tick. Clamped to ≥30. |
| `OMNI_OPENAI_ALIASES` | `gpt-3.5/4/4o → qwen-omni` | OpenAI shim alias map JSON. |
| `OMNI_ASSET_UPLOAD_MAX_GB` | 30 | Per-file upload cap. |
| `OMNI_BASE_URL` | `http://127.0.0.1:9200` | CLI default base URL. |
| `OMNI_CLI_TIMEOUT_S` | 60 | CLI per-request timeout. |
| `OMNI_WSL_DISTRO` | `linbox-Omni_Studio` | Windows shim distro target. |
| `OMNI_API_ALLOW_REMOTE` | unset (off) | Opt in to a `0.0.0.0` bind for bridge+gateway and lift the `/api/session` loopback-peer gate. Off = loopback-only (default). |
| `OMNI_ENABLE_DOCS` | unset (off) | Re-enable the FastAPI `/docs`, `/redoc`, `/openapi.json` endpoints (disabled by default; unauthenticated schema disclosure). |
| `OMNI_AUDIO_TRUST_REMOTE_CODE` | unset (off) | Allow `trust_remote_code=True` when loading native/custom audio models. Off = refuse remote code (safe default for user-installed repos). |

**Intent:** Operators tune these without redeploying. The scheduler tick is
clamped at runtime so a typo (`OMNI_MAINTENANCE_TICK_S=0`) cannot busy-loop
the gateway. The three 2026-05-29 knobs are all **fail-closed**: the secure
behavior is the default and each must be explicitly opted out of.

Auditor: Claude (Opus 4.8) 2026-05-29

---

## 27. ComfyUI HTTP proxy uses a strict subpath allowlist

The forwarded paths under `/api/comfy/{instance_id}/proxy/{subpath}` are
matched against `proxy._EXACT_PATHS` and `proxy._PREFIXED` before any
network call to ComfyUI happens.

**Intent:** Auth gates the route, but a future regression elsewhere (an
auth-middleware bypass, a reverse-proxy misconfig) must not expose
ComfyUI's entire admin surface. Adding a new ComfyUI subpath requires
editing `proxy.py` - that is the gate.

Auditor: _____ Date: _____

---

## 28. WebSocket auth via query param + subprotocol (no headers)

`/api/comfy/{instance_id}/ws` reads the API token from `?token=...` first,
then `Sec-WebSocket-Protocol: omni.token.<TOKEN>` second. Browsers cannot
attach `X-Omni-Token` to a WS handshake, so the HTTP middleware path is
bypassed; auth runs inline inside the WebSocket handler.

**Intent:** The URL-embedded token is acceptable here because the bridge is
loopback-only. For non-browser clients, the subprotocol form keeps the
token off the URL. The bridge tunnels the WS as a transparent TCP forward
once the upgrade is detected, so it does not parse or log the URL after
that point.

Auditor: _____ Date: _____

---

## 29. OpenAI shim is a lossy adapter

`/v1/chat/completions` flattens the `messages` array into a single text
prompt (`Role: content` per message) before forwarding to the worker.
The shim forwards the **first inline base64 image** (`data:...;base64,...`)
found in an `{"type":"image_url",...}` block to the worker; it deliberately
does **not** fetch remote `http(s)` image URLs (that would be an SSRF
vector). Audio and video attachments are still dropped by the shim.

**Intent:** Dropping into an OpenAI-shaped client (LangChain, LlamaIndex,
the `openai` Python package) should "just work" for text-only chat plus
single inline-image prompts. Real multi-modal callers (multiple images,
audio, video, or remote image URLs) should use the native
`/api/chat/{model}` endpoint which carries `image`, `audio`, `video`
separately.

Auditor: _____ Date: _____

---

## 30. Workflow imports atomic-write through `.tmp`

`/api/workflows/import` and PUT `/api/workflows/{filename}` write to a
temp file in the workflows dir, then `os.replace()` to the target name.

**Intent:** A crash mid-write must never leave a half-written workflow JSON
that ComfyUI would later crash on. The temp prefix `.<name>.` keeps these
files visible but distinguishable from real workflows.

Auditor: _____ Date: _____
