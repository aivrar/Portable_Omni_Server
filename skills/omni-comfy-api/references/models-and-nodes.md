# Models and custom nodes

## Model storage

Comfy models live inside the distro at:

```text
/opt/omni_studio/comfyui/models/<category>/
```

They are intentionally independent of other Omni model directories. Query
`GET /api/assets/comfy/storage`, `/scan`, or `/{category}` before choosing a
path.

The canonical `/opt/omni_studio` paths may be guarded symlinks to persistent
state beneath `/var/lib/omni_studio`. Use the asset/output APIs or inspect from
inside the distro. Windows UNC does not reliably follow absolute Linux
symlinks, so a missing UNC path does not prove a model or output disappeared.

Keep learned encoder projection matrices in `clip_projections`; do not place
them in `clip` or `text_encoders`. For example, MiniMax H3 ClipProj consumes a
text encoder from `text_encoders` and its matrix from `clip_projections`.

## Discover workflow dependencies

Analyze the API graph first. Prefer exact install actions returned in its model
and node report.

Model searches:

- Installed: `/api/registry/comfy/installed-models/search`
- Manager: `/api/registry/comfy/manager-models/search`
- Hugging Face: `/api/registry/comfy/models/search`

Node searches:

- Available Manager catalog: `/api/registry/comfy/nodes/search`
- Exact record: `/api/registry/comfy/nodes/{node_id}`
- Live loaded classes: `/api/comfy/{instance_id}/nodes/search`

## Download with Xet

Repository/file request:

```json
POST /api/assets/comfy/{category}/install
{
  "repo": "owner/repository",
  "file": "path/model.safetensors",
  "name": "model.safetensors"
}
```

Exact Hugging Face URL request:

```json
POST /api/assets/comfy/{category}/install-url
{
  "url": "https://huggingface.co/owner/repo/resolve/main/model.safetensors",
  "name": "model.safetensors"
}
```

These paths use Hugging Face Hub plus `hf_xet`. Poll the returned
`job_status_path`, inspect failures/logs, and verify `target_path` exists in the
correct Comfy category before re-analyzing the workflow.

Comfy need not run for direct asset downloads or deletion. Manager-catalog
model installs and custom-node operations do require a ready instance.

## Delete

`DELETE /api/assets/comfy/{category}/{relative_filename}` is immediate and
reports `recoverable: false`. Resolve the exact category/name first. Do not use
filesystem globs or broad deletion.

## Install or update custom nodes

Prefer the action returned by registry search. For the guarded lifecycle API,
use `POST /api/comfy/extensions/manage` with exact `node` or `repo_url`, and
include `expected_nodes` when known.

Manager can deliberately reject a repository that is absent from its catalog.
For an exact user-approved repository, fall back to:

```json
POST /api/assets/comfy/nodes/install
{
  "repo_url": "https://github.com/owner/repository",
  "ref": "verified-tag-or-commit"
}
```

Poll the returned asset job, restart Comfy once, and perform the same live
class verification. Direct installation does not make the repository part of
Manager `update_all`.

Installation is successful only when:

1. The guarded operation completes.
2. Any requested restart becomes ready.
3. Expected class names appear in live Comfy object information.
4. Workflow analysis no longer reports those classes missing.

A repository existing on disk is not sufficient verification.
