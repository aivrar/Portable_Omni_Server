# Omni Studio API review — 2026-08-11

## Result

The gateway exposes a broad, typed API for ComfyUI, standalone Omni workers,
ACE-Step, Stable Audio Lab, MOSS-TTS/SFX, model and asset installation,
workflow management, media outputs, maintenance, and OpenAI-compatible
clients. The complete durable route map is
[`docs/api-capability-inventory.md`](../docs/api-capability-inventory.md).

The source audit found 208 registered route entries, 198 OpenAPI paths, 211
HTTP operations, one WebSocket route, and 74 typed schemas. No endpoint was
added or changed during this review.

## What is dependable by API

- Device discovery, GPU UUID mapping, model-free placement analysis, exact
  worker lifecycle, Comfy instance lifecycle, workflow requirement analysis,
  model/node discovery, output metadata, and cleanup are strongly contracted.
- Comfy workflow execution is available through inline API graphs or saved
  workflows with parameter overrides. A plan must be valid before queueing and
  a Comfy prompt ID must be followed by output/history verification.
- ACE-Step, Stable Audio Lab, MOSS-TTS, and MOSS-SFX have explicit state/load,
  inference, output, and cleanup APIs. Long audio is assembled through the
  output composition job rather than one oversized generation call.
- Standalone model chat is available through native `/api/chat/{model}` and
  OpenAI-compatible `/v1/chat/completions`; placement must be analyzed before
  spawn.
- Installation APIs cover configured Omni variants, generic LoRAs, Hugging
  Face tokens/search, Comfy models, Xet/registry assets, Comfy Manager models,
  and catalog custom nodes.

## Important limits

- Current typed fields do not guarantee modality support. Qwen audio/video and
  native speech, MiniCPM video/TTS, Moshi transport, AnyGPT multimodal paths,
  and oversized Qwen3/Nemotron variants remain blocked or unqualified as
  recorded in `docs/capability-confidence.md`.
- Comfy placement is component placement. It is not a universal layer-sharder
  for arbitrary models.
- Background installation/update/zip/composition jobs are polled through
  `/api/jobs/{job_id}` only when the response reports a running operation.
  ACE-Step and Audio Lab inference output IDs are persisted artifacts, not
  automatically generic background jobs.
- Deleting models, outputs, workers, workflows, keys, or sessions is immediate
  at the target route and should follow exact-target resolution.

## Authentication and exposure

The normal client surface is the loopback Windows bridge on port 9200. The
gateway uses a loopback token by default, supports bearer API keys when
configured, token-gates `/api/*`, `/v1/*`, and `/metrics`, and keeps OpenAPI
docs disabled unless explicitly enabled. Browser media paths have a narrowly
scoped query-token exception. The full-app shutdown route is bridge-owned and
requires both the token and `confirm: true`.

## Findings

1. **Live bridge unavailable during this audit.** Port 9200 was not listening,
   and no `bridge.py` process was present. The route/schema inventory was
   generated from the current source and in-process FastAPI schema instead of
   pretending that a live capability response was available. Start the normal
   app before relying on installed assets, worker state, or readiness values.
2. **OpenAPI operation-ID warning.** FastAPI warns that the five-method
   `/api/comfy/{instance_id}/proxy/{subpath:path}` decorator produces a
   duplicate operation ID. This does not break routing, but generated clients
   that require unique operation IDs may need a future route-ID cleanup.
3. **Internal worker routes are intentionally not public contract.** The
   loopback worker endpoints under `/load`, `/infer`, `/ace_step/*`, and
   `/audio_lab/*` are implementation details. Agents should use the guarded
   gateway routes so placement, timeout retirement, token auth, and cleanup are
   preserved.

## Verification

- In-process route enumeration completed without loading model weights.
- FastAPI schema generation completed with the one duplicate-operation-ID
  warning above.
- The route-inventory regression test passed and confirms every registered
  gateway/WebSocket path appears in the durable inventory.
- The inventory was copied to `/opt/omni_studio/docs/` and future setup runs
  synchronize `AGENTS.md`, `docs/`, and `skills/` from this source tree.
