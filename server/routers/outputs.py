"""Generated-media retrieval API.

Closes the #1 remote-use blocker: ComfyUI writes to ``OUTPUT_DIR/comfyui/``
and there was no API path to list, fetch, or delete those files.

Three filesystem roots are exposed via the ``kind`` query parameter:

  * ``output`` (default) -> ``OUTPUT_DIR/comfyui``
  * ``input``            -> ``OUTPUT_DIR/comfyui_input``
  * ``temp``             -> ``CACHE_DIR/comfyui_temp``

All file access goes through ``safe_subtree_path`` so traversal attempts
(``..``, NUL, drive letters, escaping symlinks) are rejected.

Output ↔ job linkage (Sprint B):

* ``?prompt_id=`` filter on the list endpoint.
* ``GET /api/outputs/{relpath}/metadata`` reads ComfyUI's ``prompt`` and
  ``workflow`` PNG tEXt chunks via stdlib zlib so we don't depend on PIL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
import wave
import zipfile
import zlib
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from config import CACHE_DIR, OUTPUT_DIR
from helpers import safe_subtree_path
from jobs import JobCancelled
from output_meta import has_keep_segment, output_meta
from state import comfy_registry, jobs

logger = logging.getLogger(__name__)

router = APIRouter()

_OUTPUT_ROOTS: dict[str, Path] = {
    "output": OUTPUT_DIR / "comfyui",
    "input": OUTPUT_DIR / "comfyui_input",
    "temp": CACHE_DIR / "comfyui_temp",
    "omni": OUTPUT_DIR / "omni",
}

_KIND_BY_EXT = {
    ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".webp": "image", ".gif": "image", ".bmp": "image", ".tiff": "image",
    ".mp4": "video", ".webm": "video", ".mov": "video", ".mkv": "video",
    ".wav": "audio", ".mp3": "audio", ".flac": "audio", ".ogg": "audio",
    ".m4a": "audio",
    ".json": "data", ".txt": "data", ".md": "data", ".log": "data",
    ".csv": "data", ".yaml": "data", ".yml": "data", ".toml": "data",
}

_READABLE_EXTENSIONS = {".json", ".txt", ".md", ".log", ".csv", ".yaml", ".yml", ".toml"}
_READABLE_LIMIT = 256 * 1024

# Note: \b doesn't work here because `_` is a word char (no boundary between
# `_` and a hex digit). Use explicit non-hex lookarounds so a UUID lifted out
# of `ComfyUI_00001_<uuid>.png` matches.
_PROMPT_ID_RE = re.compile(
    r"(?<![0-9a-fA-F])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-fA-F])"
)
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK = 64 * 1024
_THUMB_DIR = CACHE_DIR / "thumbs"


def _resolve_root(kind: str) -> Path:
    root = _OUTPUT_ROOTS.get(kind)
    if root is None:
        raise HTTPException(status_code=400,
                            detail=f"Invalid kind: {kind} (use one of {list(_OUTPUT_ROOTS)})")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _relative_to_output_root(target: Path, output_root: Path) -> str:
    """Return a media path even when the public output root is a symlink."""
    # Validate the target, then retain its directory entry identity.
    target.resolve().relative_to(output_root.resolve())
    lexical = Path(os.path.abspath(target))
    try:
        return lexical.relative_to(Path(os.path.abspath(output_root))).as_posix()
    except ValueError:
        return lexical.relative_to(output_root.resolve()).as_posix()


def _kind_for_path(p: Path) -> str:
    return _KIND_BY_EXT.get(p.suffix.lower(), "other")


def _mime_for_path(p: Path) -> str:
    mt, _ = mimetypes.guess_type(str(p))
    return mt or "application/octet-stream"


def _prompt_id_hint(p: Path) -> str | None:
    m = _PROMPT_ID_RE.search(p.name)
    return m.group(1) if m else None


def _prompt_paths_from_history(payload, prompt_id: str) -> dict[str, set[str]]:
    """Extract output-relative paths from one ComfyUI history response."""
    found: dict[str, set[str]] = {"output": set(), "input": set(), "temp": set()}
    if not isinstance(payload, dict):
        return found
    record = payload.get(prompt_id)
    if not isinstance(record, dict):
        return found
    outputs = record.get("outputs")
    if not isinstance(outputs, dict):
        return found
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for values in node_output.values():
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict):
                    continue
                filename = str(item.get("filename") or "").strip()
                subfolder = str(item.get("subfolder") or "").strip().strip("/")
                kind = str(item.get("type") or "output").strip().lower()
                if not filename or "/" in filename or "\\" in filename or kind not in found:
                    continue
                relpath = f"{subfolder}/{filename}" if subfolder else filename
                if all(segment not in {"", ".", ".."} for segment in relpath.split("/")):
                    found[kind].add(relpath)
    return found


async def _live_prompt_output_paths(prompt_id: str) -> dict[str, set[str]]:
    """Resolve a prompt id through ready ComfyUI instances' live history."""
    merged: dict[str, set[str]] = {"output": set(), "input": set(), "temp": set()}
    for instance in comfy_registry.all_instances():
        if getattr(instance, "status", "") != "ready":
            continue
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(
                    f"http://127.0.0.1:{instance.port}/history/{prompt_id}",
                )
            if response.status_code != 200:
                continue
            current = _prompt_paths_from_history(response.json(), prompt_id)
            for kind, paths in current.items():
                merged[kind].update(paths)
        except (httpx.HTTPError, ValueError, TypeError):
            continue
    return merged


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _read_png_text_chunks(path: Path, max_bytes: int = 4 * 1024 * 1024) -> dict[str, str]:
    """Pull tEXt/iTXt/zTXt chunks from a PNG. Stdlib only.

    ComfyUI writes the original prompt JSON under key ``prompt`` and the
    full workflow under ``workflow``. We stop after ``max_bytes`` so a
    pathological PNG can't make us read the whole thing.
    """
    out: dict[str, str] = {}
    decoded_bytes = 0
    def decode_text(data: bytes, compressed: bool = False) -> str:
        nonlocal decoded_bytes
        remaining = max_bytes - decoded_bytes
        if compressed:
            decoder = zlib.decompressobj()
            data = decoder.decompress(data, remaining + 1)
            if not decoder.eof:
                raise zlib.error("PNG metadata exceeds limit or is incomplete")
        if len(data) > remaining:
            raise zlib.error("PNG metadata exceeds limit")
        decoded_bytes += len(data)
        return data.decode("utf-8", errors="replace")
    try:
        with open(path, "rb") as f:
            sig = f.read(8)
            if sig != _PNG_SIGNATURE:
                return {}
            consumed = 8
            while consumed < max_bytes:
                header = f.read(8)
                if len(header) < 8:
                    break
                length, ctype_b = struct.unpack(">I", header[:4])[0], header[4:8]
                ctype = ctype_b.decode("ascii", errors="replace")
                consumed += 8
                if length + 4 > max_bytes - consumed:
                    return out
                data = f.read(length)
                f.read(4)  # CRC
                consumed += length + 4
                if ctype == "IEND":
                    break
                try:
                    if ctype == "tEXt":
                        sep = data.find(b"\x00")
                        if sep > 0:
                            key = data[:sep].decode("latin-1", errors="replace")
                            val = decode_text(data[sep + 1:])
                            out[key] = val
                    elif ctype == "zTXt":
                        sep = data.find(b"\x00")
                        if sep > 0:
                            key = data[:sep].decode("latin-1", errors="replace")
                            # data[sep+1] is compression method (0=zlib)
                            try:
                                val = decode_text(data[sep + 2:], compressed=True)
                                out[key] = val
                            except zlib.error:
                                pass
                    elif ctype == "iTXt":
                        # key\0compFlag\0compMethod\0lang\0transKey\0text
                        parts = data.split(b"\x00", 1)
                        if len(parts) == 2:
                            key = parts[0].decode("utf-8", errors="replace")
                            tail = parts[1]
                            if len(tail) >= 2:
                                comp_flag = tail[0]
                                # comp_method = tail[1]
                                rest = tail[2:]
                                lang_end = rest.find(b"\x00")
                                if lang_end >= 0:
                                    after_lang = rest[lang_end + 1:]
                                    transkey_end = after_lang.find(b"\x00")
                                    if transkey_end >= 0:
                                        text_bytes = after_lang[transkey_end + 1:]
                                        if comp_flag == 1:
                                            try:
                                                text = decode_text(text_bytes, compressed=True)
                                            except zlib.error:
                                                continue
                                        else:
                                            text = decode_text(text_bytes)
                                        out[key] = text
                except (UnicodeDecodeError, struct.error, zlib.error):
                    continue
    except OSError:
        return {}
    return out


def _try_parse_json(s: str) -> object | None:
    try:
        return json.loads(s)
    except (ValueError, json.JSONDecodeError):
        return None


def _readable_document(path: Path) -> dict | None:
    """Return a bounded text preview for a local output-side document."""
    if path.suffix.lower() not in _READABLE_EXTENSIONS:
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as source:
            raw = source.read(_READABLE_LIMIT + 1)
    except OSError:
        return {"path": path.name, "error": "Document could not be read"}
    truncated = len(raw) > _READABLE_LIMIT
    text = raw[:_READABLE_LIMIT].decode("utf-8", errors="replace")
    document: dict = {
        "path": path.name,
        "format": path.suffix.lower().lstrip(".") or "text",
        "size": size,
        "truncated": truncated,
        "text": text,
    }
    if path.suffix.lower() == ".json":
        parsed = _try_parse_json(text) if not truncated else None
        if parsed is not None:
            document["parsed"] = parsed
    return document


def _related_media(root: Path, target: Path) -> list[dict]:
    """List playable siblings without recursively scanning the output tree."""
    related: list[dict] = []
    try:
        siblings = list(target.parent.iterdir())[:500]
    except OSError:
        return related
    for sibling in siblings:
        if not sibling.is_file() or sibling == target:
            continue
        media_kind = _kind_for_path(sibling)
        if media_kind not in {"image", "video", "audio"}:
            continue
        try:
            stat = sibling.stat()
            related.append({
                "path": _relative_to_output_root(sibling, root),
                "kind": media_kind,
                "mime": _mime_for_path(sibling),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
        except (OSError, ValueError):
            continue
    related.sort(key=lambda row: row["mtime"], reverse=True)
    return related[:100]


def _probe_artifact(path: Path, media_kind: str) -> dict:
    """Return cheap, bounded media facts for one explicitly requested file."""
    facts: dict = {
        "duration": None,
        "dimensions": None,
        "metadata": {},
        "integrity": {"status": "unknown", "checked": True, "detail": None},
    }
    suffix = path.suffix.lower()
    if suffix == ".png":
        try:
            with open(path, "rb") as source:
                header = source.read(26)
                source.seek(max(0, path.stat().st_size - 12))
                trailer = source.read(12)
            if len(header) >= 26 and header[:8] == _PNG_SIGNATURE:
                width, height = struct.unpack(">II", header[16:24])
                color_type = header[25]
                channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
                facts["dimensions"] = {"width": width, "height": height}
                facts["metadata"].update({
                    "channels": channels,
                    "has_alpha": color_type in {4, 6},
                    "png_color_type": color_type,
                })
                if trailer == b"\x00\x00\x00\x00IEND\xaeB`\x82":
                    facts["integrity"]["status"] = "ok"
                else:
                    facts["integrity"].update({
                        "status": "invalid",
                        "detail": "PNG is missing its final IEND chunk",
                    })
                return facts
        except (OSError, struct.error):
            facts["integrity"].update({
                "status": "invalid", "detail": "PNG header could not be read",
            })
            return facts
        facts["integrity"].update({
            "status": "invalid", "detail": "PNG signature or IHDR is invalid",
        })
        return facts
    if suffix == ".wav":
        try:
            with wave.open(str(path), "rb") as wav:
                frame_rate = wav.getframerate()
                facts["duration"] = (
                    wav.getnframes() / frame_rate if frame_rate else None
                )
                facts["metadata"].update({
                    "channels": wav.getnchannels(),
                    "sample_rate": frame_rate,
                    "sample_width_bytes": wav.getsampwidth(),
                })
                facts["integrity"]["status"] = "ok"
                return facts
        except (OSError, EOFError, wave.Error):
            facts["integrity"].update({
                "status": "invalid", "detail": "WAV container header is invalid",
            })
            return facts

    if media_kind not in {"image", "video", "audio"}:
        facts["integrity"].update({
            "status": "not-applicable", "detail": "No media probe is defined",
        })
        return facts
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        facts["integrity"].update({
            "status": "unavailable", "detail": "ffprobe is not installed",
        })
        return facts
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height,sample_rate,channels,pix_fmt",
                "-of", "json",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[-500:]
            facts["integrity"].update({
                "status": "invalid",
                "detail": detail or f"ffprobe exited with code {result.returncode}",
            })
            return facts
        payload = json.loads(result.stdout.decode("utf-8", errors="replace"))
        duration = (payload.get("format") or {}).get("duration")
        if duration not in (None, "", "N/A"):
            facts["duration"] = float(duration)
        for stream in payload.get("streams") or []:
            if stream.get("codec_type") == "video" and stream.get("width") and stream.get("height"):
                facts["dimensions"] = {
                    "width": int(stream["width"]),
                    "height": int(stream["height"]),
                }
                facts["metadata"].update({
                    "codec": stream.get("codec_name"),
                    "pixel_format": stream.get("pix_fmt"),
                    "has_alpha": "a" in str(stream.get("pix_fmt") or "").lower(),
                })
                break
            if stream.get("codec_type") == "audio":
                facts["metadata"].update({
                    "codec": stream.get("codec_name"),
                    "channels": stream.get("channels"),
                    "sample_rate": (
                        int(stream["sample_rate"])
                        if str(stream.get("sample_rate") or "").isdigit()
                        else None
                    ),
                })
        expected_shape = (
            facts["dimensions"] is not None
            if media_kind in {"image", "video"}
            else bool(facts["metadata"].get("codec"))
        )
        facts["integrity"].update({
            "status": "ok" if expected_shape else "invalid",
            "detail": None if expected_shape else "No expected media stream was found",
        })
        return facts
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        facts["integrity"].update({
            "status": "invalid", "detail": str(exc)[:500],
        })
        return facts


def _sidecar_provenance(root: Path, path: Path) -> dict:
    """Read a bounded same-directory manifest without exposing large payloads."""
    manifest = path.parent / "manifest.json"
    if not manifest.is_file():
        return {}
    try:
        if manifest.stat().st_size > 1024 * 1024:
            return {"manifest": "too-large"}
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"manifest": "invalid"}
    if not isinstance(payload, dict):
        return {"manifest": "invalid"}
    try:
        manifest_ref = _relative_to_output_root(manifest, root)
    except ValueError:
        return {}
    keep = (
        "job_id", "composition_id", "created_at", "mode", "model_variant",
        "lm_variant", "vae_swap", "sample_rate", "duration_s",
        "n_requested", "n_completed", "clap_available",
    )
    provenance = {
        "manifest": manifest_ref,
        **{key: payload[key] for key in keep if key in payload},
    }
    results = payload.get("results")
    if isinstance(results, list):
        matched = next((
            row for row in results
            if isinstance(row, dict)
            and (
                str(row.get("filename") or "") == path.name
                or str(row.get("url") or "").rstrip("/").endswith("/" + path.name)
            )
        ), None)
        if matched:
            provenance["result"] = {
                key: matched[key]
                for key in ("filename", "seed", "score", "rank", "duration_s")
                if key in matched
            }
    return provenance


def _file_record(root: Path, p: Path, *, probe: bool = False) -> dict:
    rel = _relative_to_output_root(p, root)
    st = p.stat()
    rec = {
        "path": rel,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mtime_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
        "kind": _kind_for_path(p),
        "mime": _mime_for_path(p),
        "prompt_id_hint": _prompt_id_hint(p),
    }
    rec["artifact"] = {
        "path": str(p),
        "ref": f"omni://outputs/{rel}",
        "media_type": rec["kind"],
        "mime": rec["mime"],
        "duration": None,
        "dimensions": None,
        "integrity": {"status": "not-probed", "checked": False, "detail": None},
        "provenance": {},
        "metadata": {
            "relpath": rel,
            "size_bytes": st.st_size,
            "mtime": st.st_mtime,
            "mtime_iso": rec["mtime_iso"],
            "prompt_id_hint": rec["prompt_id_hint"],
        },
    }
    if probe:
        probed = _probe_artifact(p, rec["kind"])
        rec["artifact"]["duration"] = probed["duration"]
        rec["artifact"]["dimensions"] = probed["dimensions"]
        rec["artifact"]["integrity"] = probed["integrity"]
        rec["artifact"]["provenance"] = _sidecar_provenance(root, p)
        if p.suffix.lower() == ".png":
            embedded_chunks = _read_png_text_chunks(p)
            embedded_keys = [
                key for key in ("prompt", "workflow", "parameters")
                if key in embedded_chunks
            ]
            if embedded_keys:
                rec["artifact"]["provenance"].update({
                    "source": "comfy-png",
                    "embedded_keys": embedded_keys,
                })
        rec["artifact"]["metadata"].update(probed["metadata"])
    root_kind = next((k for k, v in _OUTPUT_ROOTS.items() if v.resolve() == root.resolve()), "output")
    meta = output_meta.get(rel, kind=root_kind)
    if meta is not None:
        rec["tags"] = meta.tags
        rec["pinned"] = meta.pinned
        rec["collections"] = meta.collections
        if meta.notes:
            rec["notes"] = meta.notes
    return rec


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@router.get("/api/outputs")
async def list_outputs(
    kind: str = Query(default="output", description="output | input | temp | omni"),
    subdir: str = Query(default="", description="Optional subdirectory under the root"),
    since: float | None = Query(default=None, description="Only files newer than this epoch"),
    limit: int = Query(default=200, ge=1, le=500),
    prefix: str = Query(default="", description="Filter by filename prefix"),
    media_kind: str | None = Query(default=None,
                                   description="image | video | audio | data | other"),
    prompt_id: str | None = Query(default=None,
                                   description="Filter by ComfyUI prompt_id (matched against filename)"),
    tag: str | None = Query(default=None, description="Only files carrying this tag"),
    pinned: bool | None = Query(default=None, description="Only pinned (true) or unpinned (false)"),
    collection: str | None = Query(default=None,
                                    description="Only files in the named collection"),
    probe: bool = False,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: Literal["newest", "oldest", "name", "size"] = "newest",
):
    if probe and limit > 50:
        raise HTTPException(
            status_code=400,
            detail="Probed output listings are capped at limit=50",
        )
    if prompt_id and not _PROMPT_ID_RE.fullmatch(prompt_id):
        raise HTTPException(status_code=400, detail="Invalid ComfyUI prompt_id")
    prompt_paths = await _live_prompt_output_paths(prompt_id) if prompt_id else {}
    root = _resolve_root(kind)
    base = root if not subdir else safe_subtree_path(root, subdir)
    if not base.exists():
        return {"kind": kind, "subdir": subdir, "files": [], "total": 0}

    def _scan():
        root_resolved = root.resolve()
        items: list[dict] = []
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            # Skip files whose resolved path escapes the configured root
            # (e.g. operator-created symlinks pointing outside).
            try:
                if not p.resolve().is_relative_to(root_resolved):
                    continue
            except (OSError, ValueError):
                continue
            if prefix and not p.name.startswith(prefix):
                continue
            if media_kind and _kind_for_path(p) != media_kind:
                continue
            try:
                rec = _file_record(root, p)
            except (OSError, ValueError):
                continue
            if since is not None and rec["mtime"] < since:
                continue
            if (
                prompt_id
                and rec.get("prompt_id_hint") != prompt_id
                and rec.get("path") not in prompt_paths.get(kind, set())
            ):
                continue
            if tag is not None and tag not in (rec.get("tags") or []):
                continue
            if pinned is not None and bool(rec.get("pinned")) != pinned:
                continue
            if collection is not None and collection not in (rec.get("collections") or []):
                continue
            items.append(rec)
        if sort == "name":
            items.sort(key=lambda r: r["path"].casefold())
        elif sort == "size":
            items.sort(key=lambda r: r["size"], reverse=True)
        else:
            items.sort(key=lambda r: r["mtime"], reverse=sort != "oldest")
        return items

    items = await asyncio.to_thread(_scan)
    selected = items[offset:offset + limit]
    if probe:
        selected = [
            await asyncio.to_thread(
                _file_record, root, safe_subtree_path(root, row["path"]), probe=True,
            )
            for row in selected
        ]
    return {
        "kind": kind,
        "subdir": subdir,
        "files": selected,
        "total": len(items),
        "offset": offset,
        "next_offset": offset + len(selected) if offset + len(selected) < len(items) else None,
    }


# ---------------------------------------------------------------------------
# Stream a single file (Range-aware)
# ---------------------------------------------------------------------------
def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header:
        return None
    m = _RANGE_RE.fullmatch(header.strip())
    if not m:
        raise HTTPException(status_code=416, detail="Malformed Range")
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        raise HTTPException(status_code=416, detail="Malformed Range")
    if start_s == "":
        # suffix: bytes=-N
        n = int(end_s)
        if n <= 0:
            raise HTTPException(status_code=416, detail="Malformed Range")
        start = max(0, size - n)
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    if start >= size or start > end:
        raise HTTPException(status_code=416, detail="Range out of bounds",
                            headers={"Content-Range": f"bytes */{size}"})
    return start, min(end, size - 1)


def _stream_range(path: Path, start: int, end: int):
    remaining = end - start + 1
    with open(path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            chunk = f.read(min(_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


# ---------------------------------------------------------------------------
# Tags, pinning, collections (Sprint C)
# ---------------------------------------------------------------------------
class _TagsBody(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=64)


class _CollectionsBody(BaseModel):
    collections: list[str] = Field(default_factory=list, max_length=32)


class _NotesBody(BaseModel):
    notes: str = Field(default="", max_length=4096)


def _ensure_output_exists(kind: str, relpath: str) -> str:
    """Validate path safety + that the file actually exists. Returns the
    canonical relpath (POSIX, relative to the kind's root)."""
    root = _resolve_root(kind)
    target = safe_subtree_path(root, relpath)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Output not found")
    return _relative_to_output_root(target, root)


@router.get("/api/outputs/collections")
async def list_collections():
    """Summary of collection name -> count of pinned/tagged outputs."""
    return {
        "collections": [
            {"name": name, "count": count}
            for name, count in sorted(output_meta.collections_summary().items())
        ],
    }


@router.put("/api/outputs/meta/tags/{relpath:path}")
async def set_tags(
    relpath: str,
    body: _TagsBody,
    kind: str = Query(default="output"),
):
    canonical = _ensure_output_exists(kind, relpath)
    rec = output_meta.upsert(canonical, kind=kind, tags=body.tags)
    return {"path": canonical, "tags": rec.tags}


@router.put("/api/outputs/meta/pinned/{relpath:path}")
async def set_pinned(
    relpath: str,
    pinned: bool = Query(default=True),
    kind: str = Query(default="output"),
):
    canonical = _ensure_output_exists(kind, relpath)
    rec = output_meta.upsert(canonical, kind=kind, pinned=pinned)
    return {"path": canonical, "pinned": rec.pinned}


@router.put("/api/outputs/meta/collections/{relpath:path}")
async def set_collections(
    relpath: str,
    body: _CollectionsBody,
    kind: str = Query(default="output"),
):
    canonical = _ensure_output_exists(kind, relpath)
    rec = output_meta.upsert(canonical, kind=kind, collections=body.collections)
    return {"path": canonical, "collections": rec.collections}


@router.put("/api/outputs/meta/notes/{relpath:path}")
async def set_notes(
    relpath: str,
    body: _NotesBody,
    kind: str = Query(default="output"),
):
    canonical = _ensure_output_exists(kind, relpath)
    rec = output_meta.upsert(canonical, kind=kind, notes=body.notes)
    return {"path": canonical, "notes": rec.notes}


@router.delete("/api/outputs/meta/{relpath:path}")
async def clear_meta(
    relpath: str,
    kind: str = Query(default="output"),
):
    canonical = _ensure_output_exists(kind, relpath)
    output_meta.delete(canonical, kind=kind)
    return {"path": canonical, "status": "cleared"}


@router.get("/api/outputs/meta/{relpath:path}")
async def get_meta(
    relpath: str,
    kind: str = Query(default="output"),
):
    canonical = _ensure_output_exists(kind, relpath)
    rec = output_meta.get(canonical, kind=kind)
    if rec is None:
        return {"path": canonical, "tags": [], "pinned": False,
                "collections": [], "notes": ""}
    return {
        "path": canonical,
        "tags": rec.tags,
        "pinned": rec.pinned,
        "collections": rec.collections,
        "notes": rec.notes,
        "updated_at": rec.updated_at,
    }


# ---------------------------------------------------------------------------
# Output metadata, readable documents, and related media
# ---------------------------------------------------------------------------
@router.get("/api/outputs/metadata/{relpath:path}")
async def get_output_metadata(
    relpath: str,
    kind: str = Query(default="output"),
):
    """Read bounded artifact details for an output.

    PNGs expose embedded ComfyUI metadata. Text-like files expose a bounded
    readable preview. Media also includes its same-directory manifest when
    present, plus playable siblings that the UI can navigate to.
    """
    root = _resolve_root(kind)
    target = safe_subtree_path(root, relpath)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Output not found")
    artifact = await asyncio.to_thread(_file_record, root, target, probe=True)
    document = await asyncio.to_thread(_readable_document, target)
    companion = None
    if target.name != "manifest.json":
        manifest = target.parent / "manifest.json"
        if manifest.is_file():
            companion = await asyncio.to_thread(_readable_document, manifest)
            if companion:
                companion["path"] = _relative_to_output_root(manifest, root)
    related = await asyncio.to_thread(_related_media, root, target)
    if target.suffix.lower() != ".png":
        return {
            "path": relpath,
            "kind": kind,
            "artifact": artifact["artifact"],
            "metadata": {},
            "document": document,
            "companion": companion,
            "related": related,
        }

    chunks = _read_png_text_chunks(target)
    out: dict = {
        "path": relpath,
        "kind": kind,
        "artifact": artifact["artifact"],
        "metadata": {},
        "document": document,
        "companion": companion,
        "related": related,
    }
    other: dict[str, str] = {}
    for k, v in chunks.items():
        if k in ("prompt", "workflow", "parameters"):
            parsed = _try_parse_json(v)
            if parsed is not None:
                out["metadata"][k] = parsed
            else:
                out["metadata"][k + "_raw"] = v
        else:
            other[k] = v if len(v) <= 4000 else v[:4000] + "... (truncated)"
    embedded = [
        key for key in ("prompt", "workflow", "parameters")
        if key in out["metadata"] or key + "_raw" in out["metadata"]
    ]
    if embedded:
        out["artifact"]["provenance"].update({
            "source": "comfy-png",
            "embedded_keys": embedded,
        })
    if other:
        out["other"] = other
    return out


@router.head("/api/outputs/{relpath:path}")
async def head_output(
    relpath: str,
    kind: str = Query(default="output"),
):
    root = _resolve_root(kind)
    target = safe_subtree_path(root, relpath)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    st = target.stat()
    return Response(
        status_code=200,
        headers={
            "Content-Length": str(st.st_size),
            "Content-Type": _mime_for_path(target),
            "Accept-Ranges": "bytes",
            "Last-Modified": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(st.st_mtime)),
        },
    )


@router.get("/api/outputs/{relpath:path}")
async def get_output(
    request: Request,
    relpath: str,
    kind: str = Query(default="output"),
    download: int = Query(default=0),
    thumb: int = Query(default=0, description="If >0, return a width-N PIL thumbnail"),
    w: int = Query(default=256, ge=16, le=2048, description="Thumbnail width"),
):
    root = _resolve_root(kind)
    target = safe_subtree_path(root, relpath)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    st = target.stat()

    if thumb:
        return _serve_thumbnail(target, st, w)

    mime = _mime_for_path(target)
    headers: dict = {
        "Accept-Ranges": "bytes",
        "Last-Modified": time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(st.st_mtime)),
    }
    if download:
        # safe_subtree_path rejects / \ NUL .. but not a double-quote or
        # control chars in the name, either of which would break the header.
        raw_name = Path(relpath).name
        safe_name = raw_name.replace("\\", "_").replace('"', "_")
        safe_name = "".join(
            c if c.isprintable() and c not in '"\\' else "_"
            for c in safe_name) or "download"
        encoded_name = quote(raw_name, safe="")
        headers["Content-Disposition"] = (
            f'attachment; filename="{safe_name}"; '
            f"filename*=UTF-8''{encoded_name}")

    rng = _parse_range(request.headers.get("range"), st.st_size)
    if rng is not None:
        start, end = rng
        headers["Content-Range"] = f"bytes {start}-{end}/{st.st_size}"
        headers["Content-Length"] = str(end - start + 1)
        return StreamingResponse(
            _stream_range(target, start, end),
            status_code=206,
            media_type=mime,
            headers=headers,
        )

    headers["Content-Length"] = str(st.st_size)
    return StreamingResponse(
        _stream_range(target, 0, st.st_size - 1),
        media_type=mime,
        headers=headers,
    )


def _serve_thumbnail(src: Path, st, width: int):
    if _kind_for_path(src) != "image":
        raise HTTPException(status_code=415,
                            detail="Thumbnails only available for image files")
    _THUMB_DIR.mkdir(parents=True, exist_ok=True)
    # Hash the absolute path so two files with the same basename in
    # different subdirs cannot share a cache entry.
    import hashlib
    path_hash = hashlib.sha1(str(src).encode("utf-8")).hexdigest()[:16]
    cache_key = f"{path_hash}_{src.stat().st_mtime_ns}_{width}.webp"
    cache_path = _THUMB_DIR / cache_key
    if not cache_path.exists():
        try:
            from PIL import Image
        except ImportError as e:
            raise HTTPException(status_code=500,
                                detail=f"Pillow not available: {e}")
        try:
            with Image.open(src) as img:
                img.thumbnail((width, width * 4), Image.LANCZOS)
                img.save(cache_path, format="WEBP", quality=80)
        except Exception as e:
            raise HTTPException(status_code=500,
                                detail=f"Thumbnail failed: {e}")
    return StreamingResponse(open(cache_path, "rb"), media_type="image/webp")


@router.delete("/api/outputs/{relpath:path}")
async def delete_output(
    relpath: str,
    kind: str = Query(default="output"),
):
    root = _resolve_root(kind)
    target = safe_subtree_path(root, relpath)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        output_meta.delete_file(_relative_to_output_root(target, root), target, kind=kind)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")
    return {"status": "deleted", "path": relpath, "kind": kind}


# ---------------------------------------------------------------------------
# Long-form audio composition
# ---------------------------------------------------------------------------
class AudioComposeSegment(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    kind: str = Field(default="omni", pattern="^(output|input|temp|omni)$")
    start_s: float = Field(default=0.0, ge=0.0)
    end_s: float | None = Field(default=None, gt=0.0)
    gain_db: float = Field(default=0.0, ge=-48.0, le=24.0)
    label: str | None = Field(default=None, max_length=200)


class AudioComposeRequest(BaseModel):
    segments: list[AudioComposeSegment] = Field(min_length=2, max_length=64)
    crossfade_s: float = Field(default=4.0, ge=0.01, le=30.0)
    sample_rate: int = Field(default=48000, ge=8000, le=192000)
    bit_depth: int = Field(default=24)
    loudness_normalize: bool = False
    target_lufs: float = Field(default=-14.0, ge=-30.0, le=-5.0)
    true_peak_db: float = Field(default=-1.0, ge=-9.0, le=0.0)
    cpu_threads: int = Field(default=2, ge=1, le=4)
    title: str = Field(default="long-form-composition", min_length=1, max_length=120)


def _build_audio_compose_filter(segments: list[AudioComposeSegment], *,
                                sample_rate: int, crossfade_s: float,
                                loudness_normalize: bool,
                                target_lufs: float, true_peak_db: float) -> tuple[str, str]:
    filters: list[str] = []
    for idx, seg in enumerate(segments):
        chain = [
            f"aresample={sample_rate}",
            "aformat=sample_fmts=fltp:channel_layouts=stereo",
        ]
        trim = f"atrim=start={seg.start_s:.6f}"
        if seg.end_s is not None:
            trim += f":end={seg.end_s:.6f}"
        chain.extend((trim, "asetpts=PTS-STARTPTS"))
        if abs(seg.gain_db) > 1e-9:
            chain.append(f"volume={seg.gain_db:.4f}dB")
        filters.append(f"[{idx}:a]{','.join(chain)}[seg{idx}]")

    current = "seg0"
    for idx in range(1, len(segments)):
        output = f"mix{idx}"
        filters.append(
            f"[{current}][seg{idx}]acrossfade=d={crossfade_s:.6f}:c1=tri:c2=tri[{output}]"
        )
        current = output
    if loudness_normalize:
        filters.append(
            f"[{current}]loudnorm=I={target_lufs:.3f}:LRA=11:TP={true_peak_db:.3f}[master]"
        )
        current = "master"
    return ";".join(filters), current


async def _run_compose_ffmpeg(command: list[str], cancel_event) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    async def drain_errors():
        tail = bytearray()
        if proc.stderr is not None:
            while chunk := await proc.stderr.read(8192):
                tail.extend(chunk)
                del tail[:-32000]
        return bytes(tail).decode("utf-8", errors="replace")[-8000:]

    errors = asyncio.create_task(drain_errors())
    try:
        while proc.returncode is None:
            if cancel_event.is_set():
                raise JobCancelled()
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
        return proc.returncode or 0, await errors
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if not errors.done():
            errors.cancel()
        await asyncio.gather(errors, return_exceptions=True)


@router.post("/api/outputs/audio/compose")
async def compose_audio_outputs(req: AudioComposeRequest):
    """Crossfade output-library audio into one long-form, media-visible master."""
    if req.bit_depth not in (16, 24):
        raise HTTPException(status_code=400, detail="bit_depth must be 16 or 24")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HTTPException(status_code=503, detail="ffmpeg is not installed")

    resolved: list[Path] = []
    for seg in req.segments:
        root = _resolve_root(seg.kind)
        target = safe_subtree_path(root, seg.path)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail=f"Audio file not found: {seg.kind}:{seg.path}")
        if _kind_for_path(target) != "audio":
            raise HTTPException(status_code=400, detail=f"Not an audio file: {seg.kind}:{seg.path}")
        if seg.end_s is not None and seg.end_s <= seg.start_s:
            raise HTTPException(status_code=400, detail=f"end_s must exceed start_s for {seg.path}")
        resolved.append(target)

    composition_id = uuid.uuid4().hex
    safe_title = re.sub(r"[^A-Za-z0-9._-]+", "-", req.title).strip(".-_")[:80]
    safe_title = safe_title or "long-form-composition"
    output_root = _resolve_root("omni")
    target_dir = safe_subtree_path(output_root, f"compositions/{composition_id}")
    target_dir.mkdir(parents=True, exist_ok=False)
    target = target_dir / f"{safe_title}.wav"
    temp_target = target_dir / f".{safe_title}.tmp.wav"
    output_rel = _relative_to_output_root(target, output_root)

    async def _runner(progress, cancel_event):
        durations: list[float] = []
        progress(0, len(resolved) + 1, "probing inputs")
        for idx, (seg, path) in enumerate(zip(req.segments, resolved)):
            if cancel_event.is_set():
                raise JobCancelled()
            facts = await asyncio.to_thread(_probe_artifact, path, "audio")
            duration = facts.get("duration")
            if not isinstance(duration, (int, float)) or duration <= 0:
                raise RuntimeError(f"Could not determine audio duration: {seg.kind}:{seg.path}")
            end = min(float(seg.end_s), float(duration)) if seg.end_s is not None else float(duration)
            effective = end - float(seg.start_s)
            if effective <= req.crossfade_s:
                raise RuntimeError(
                    f"Segment {idx + 1} is {effective:.3f}s after trimming; "
                    f"it must exceed crossfade_s={req.crossfade_s:.3f}."
                )
            durations.append(effective)
            progress(idx + 1, len(resolved) + 1, f"probed segment {idx + 1}")

        filter_graph, output_label = _build_audio_compose_filter(
            req.segments,
            sample_rate=req.sample_rate,
            crossfade_s=req.crossfade_s,
            loudness_normalize=req.loudness_normalize,
            target_lufs=req.target_lufs,
            true_peak_db=req.true_peak_db,
        )
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for path in resolved:
            command.extend(("-i", str(path)))
        command.extend((
            "-filter_complex_threads", str(req.cpu_threads),
            "-filter_complex", filter_graph,
            "-map", f"[{output_label}]",
            "-threads", str(req.cpu_threads),
            "-c:a", "pcm_s24le" if req.bit_depth == 24 else "pcm_s16le",
            str(temp_target),
        ))
        progress(len(resolved), len(resolved) + 1, "composing")
        try:
            returncode, stderr = await _run_compose_ffmpeg(command, cancel_event)
            if returncode != 0:
                raise RuntimeError(f"ffmpeg composition failed: {stderr or 'unknown error'}")
            os.replace(temp_target, target)
            output_facts = await asyncio.to_thread(_probe_artifact, target, "audio")
            manifest = {
                "composition_id": composition_id,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "output_path": output_rel,
                "output_ref": f"omni://outputs/{output_rel}",
                "duration_s": output_facts.get("duration"),
                "sample_rate": req.sample_rate,
                "bit_depth": req.bit_depth,
                "crossfade_s": req.crossfade_s,
                "loudness_normalize": req.loudness_normalize,
                "segments": [
                    {**seg.model_dump(exclude_none=True), "effective_duration_s": duration}
                    for seg, duration in zip(req.segments, durations)
                ],
            }
            manifest_path = target_dir / "manifest.json"
            tmp_manifest = target_dir / ".manifest.json.tmp"
            tmp_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            os.replace(tmp_manifest, manifest_path)
            progress(len(resolved) + 1, len(resolved) + 1, "complete")
            return {
                **manifest,
                "url": f"/api/outputs/{quote(output_rel, safe='/')}?kind=omni",
                "size_bytes": target.stat().st_size,
            }
        finally:
            if temp_target.exists():
                try:
                    temp_target.unlink()
                except OSError:
                    pass

    job = jobs.enqueue_callable(
        kind="audio_compose",
        fn=_runner,
        meta={
            "task": "audio-compose",
            "composition_id": composition_id,
            "title": req.title,
            "segment_count": len(req.segments),
            "output_path": output_rel,
        },
    )
    return {
        "job_id": job.job_id,
        "status": job.status,
        "kind": "audio_compose",
        "composition_id": composition_id,
        "output_path": output_rel,
    }


# ---------------------------------------------------------------------------
# ZIP bundle
# ---------------------------------------------------------------------------
class ZipRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=1000)
    kind: str = "output"
    name: str | None = Field(default=None, max_length=200)
    max_size_bytes: int = Field(default=500 * 1024 * 1024, ge=1, le=10 * 1024 ** 3)


@router.post("/api/outputs/zip")
def zip_outputs(req: ZipRequest):
    if not req.paths:
        raise HTTPException(status_code=400, detail="No paths supplied")
    root = _resolve_root(req.kind)
    files: list[tuple[str, Path]] = []
    total = 0
    for relpath in req.paths:
        target = safe_subtree_path(root, relpath)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404,
                                detail=f"File not found: {relpath}")
        size = target.stat().st_size
        total += size
        if total > req.max_size_bytes:
            raise HTTPException(status_code=413,
                                detail=f"Bundle exceeds max_size_bytes={req.max_size_bytes}")
        files.append((relpath, target))

    name = req.name or f"omni-outputs-{int(time.time())}.zip"
    if not name.endswith(".zip"):
        name += ".zip"
    # Strip path separators from the user-supplied name to keep
    # Content-Disposition single-line and unambiguous.
    safe_name = "".join(c for c in name if c.isascii() and c.isprintable() and c not in '/\\"') or "outputs.zip"

    # Build the zip on disk so the response is always a complete archive.
    # Streaming the central directory while ZipFile is still appending
    # data corrupts the bundle (offsets in the central directory drift
    # relative to what the client has received). 500 MB cap means temp
    # files stay bounded.
    tmp_dir = CACHE_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="omni-zip-", suffix=".zip",
                                         dir=str(tmp_dir))
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(tmp_path, mode="w",
                             compression=zipfile.ZIP_DEFLATED) as zf:
            for relpath, target in files:
                zf.write(target, arcname=relpath)
        # Sanity check the assembled archive before we ship it.
        with zipfile.ZipFile(tmp_path, mode="r") as zf:
            bad = zf.testzip()
            if bad is not None:
                raise HTTPException(status_code=500,
                                    detail=f"Bundle integrity check failed at {bad}")
        size = os.path.getsize(tmp_path)
    except HTTPException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"Zip build failed: {e}")

    def _stream_then_unlink():
        try:
            with open(tmp_path, "rb") as f:
                while True:
                    chunk = f.read(_CHUNK)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return StreamingResponse(
        _stream_then_unlink(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Content-Length": str(size),
        },
    )


# ---------------------------------------------------------------------------
# Prune (background job)
# ---------------------------------------------------------------------------
class PruneRequest(BaseModel):
    kind: str = "output"
    older_than_days: float | None = Field(default=None, ge=0)
    max_total_gb: float | None = Field(default=None, ge=0)
    dry_run: bool = False


@router.post("/api/outputs/prune")
async def prune_outputs(req: PruneRequest):
    root = _resolve_root(req.kind)
    if req.older_than_days is None and req.max_total_gb is None:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of older_than_days or max_total_gb")

    async def _runner(progress, cancel_event):
        # Mirror maintenance._prune_root protections: never delete files the
        # user pinned (/api/outputs/meta/pinned) or anything under a non-final
        # "keep" path segment.
        pinned_paths = {r.path for r in output_meta.list() if r.pinned and r.kind == req.kind}
        candidates: list[tuple[Path, os.stat_result, str]] = []
        for p in root.rglob("*"):
            if cancel_event.is_set():
                raise JobCancelled()
            if not p.is_file():
                continue
            try:
                rel = _relative_to_output_root(p, root)
            except ValueError:
                continue
            if has_keep_segment(rel):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            candidates.append((p, st, rel))
        candidates.sort(key=lambda item: item[1].st_mtime)

        now = time.time()
        cutoff = (now - req.older_than_days * 86400.0
                  if req.older_than_days is not None else None)
        max_bytes = (int(req.max_total_gb * 1024 ** 3)
                     if req.max_total_gb is not None else None)

        deletions: list[dict] = []
        failures: list[dict] = []
        running_total = sum(st.st_size for _, st, _ in candidates)
        progress(0, len(candidates), "scanning")

        for idx, (p, st, rel) in enumerate(candidates):
            if cancel_event.is_set():
                raise JobCancelled()
            if rel in pinned_paths:
                progress(idx + 1, len(candidates), "pinned")
                continue
            should_delete = False
            reason = None
            if cutoff is not None and st.st_mtime < cutoff:
                should_delete = True
                reason = "older_than"
            if (max_bytes is not None and not should_delete
                    and running_total > max_bytes):
                should_delete = True
                reason = "max_total"
            if should_delete:
                if not req.dry_run:
                    try:
                        output_meta.delete_file(rel, p, kind=req.kind)
                    except OSError as exc:
                        failures.append({"path": rel, "error": str(exc)})
                        continue
                deletions.append({"path": rel, "size": st.st_size, "reason": reason})
                running_total -= st.st_size
            progress(idx + 1, len(candidates), reason or "kept")

        return {
            "kind": req.kind,
            "deleted": len(deletions),
            "freed_bytes": sum(d["size"] for d in deletions),
            "dry_run": req.dry_run,
            "items": deletions[:200],
            "failures": failures[:200],
        }

    job = jobs.enqueue_callable(
        kind="maintenance",
        fn=_runner,
        meta={"task": "prune-outputs", "kind": req.kind,
              "older_than_days": req.older_than_days,
              "max_total_gb": req.max_total_gb, "dry_run": req.dry_run},
    )
    return {"job_id": job.job_id, "status": job.status, "kind": "maintenance"}
