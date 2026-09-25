# Lifecycle and updates

## Discover first

1. `GET /api/devices`
2. `GET /api/comfy/instances`
3. `GET /api/comfy/start-options` before advanced flags
4. `GET /api/comfy/installation/status`
5. `GET /api/comfy/extensions/status?instance_id=...`

Starting Comfy does not load model weights. A multi-GPU instance must expose
all potentially targeted cards in its primary-first `gpu_pool`.

## Start

`POST /api/comfy/start`:

```json
{
  "device": "cuda:1",
  "gpu_pool": ["cuda:1", "cuda:0"],
  "vram_mode": "normal",
  "precision": null,
  "preview_method": "auto",
  "disable_pinned_memory": false,
  "startup_options": {}
}
```

Use physical `cuda:N` values returned by the current device call for startup.
Persist UUIDs in user policies, then resolve them to current indices.

## Stop

- One: `POST /api/comfy/{instance_id}/stop`
- All: `POST /api/comfy/stop-all`

Stopping interrupts active workflows. Check the queue and tell the user first
unless interruption was explicitly requested.

Comfy runs inside `/omni_studio/workloads`, below the app-wide cgroup, so a
heavy model cannot consume the gateway's reserved memory. After the last Comfy
or standalone model worker exits, Omni uses only that empty cgroup's
`memory.force_empty` control to release charged checkpoint page cache. Never
replace this with a global `drop_caches` command.

For a final application teardown, unload/stop exact workers and Comfy first,
verify both registries are empty, sync persisted distro state, and close the
desktop app normally. If `linbox-Omni_Studio` remains running after that clean
boundary, terminate only that distro to release its remaining page cache;
never stop an unrelated distro.

## Gateway-only source refresh

A normal gateway `SIGTERM` stops Comfy and model workers. Forced gateway
termination does not preserve standalone workers: startup cleans orphan worker
records, while only Comfy processes support recovery. Stage source changes,
wait for gateway jobs to finish, and use a graceful restart at a time when
resident workloads may be stopped. Do not use SIGKILL as a refresh procedure.

## Update core

Core Comfy update is independent of custom-node update-all:

```json
POST /api/comfy/installation/update
{
  "ref": "latest",
  "dry_run": false,
  "restart_instances": true
}
```

Capture running-instance settings before update. Require a successful returned
`qualification`, not just a completed Git operation. With no restarted
instances the level is `checkout-and-templates` and verifies the exact commit,
clean checkout, and template discovery. With restarted instances the level is
`live-runtime` and additionally verifies readiness, `/system_stats`,
`/object_info`, and core node classes. Any qualification blocker makes the
update job fail.

## Update Manager/custom nodes

```json
POST /api/comfy/extensions/manage
{
  "action": "update_all",
  "instance_id": "comfy-cuda1-8188",
  "dry_run": false,
  "auto_restart": true,
  "timeout_s": 1800
}
```

Valid actions also include `install`, `update`, `enable`, `disable`,
`reinstall`, `fix`, and `uninstall`. Exact actions may include `node`,
`repo_url`, and `expected_nodes`.

A full “update all” operation means, sequentially:

1. Update Comfy core.
2. Wait for health/restart completion.
3. Update Manager/custom nodes.
4. Wait for health/restart completion.
5. Verify core commit, extension status, and expected node classes.

Do not run core and extension mutations concurrently. An alive process that is
still initializing may legitimately lack HTTP health; do not kill it solely
for that reason.
