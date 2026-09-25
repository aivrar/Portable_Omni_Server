"""Prompt-id output listing must not depend on filenames containing UUIDs."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from routers import outputs  # noqa: E402


PROMPT_ID = "a8a0fbe0-c824-437f-9abc-e36c622e3763"


def test_prompt_paths_are_extracted_from_comfy_history():
    found = outputs._prompt_paths_from_history({PROMPT_ID: {
        "outputs": {"9": {"images": [{
            "filename": "cold_square_00001_.png",
            "subfolder": "tq_krea2/qualification",
            "type": "output",
        }]}}
    }}, PROMPT_ID)
    assert found["output"] == {
        "tq_krea2/qualification/cold_square_00001_.png",
    }


def test_list_outputs_uses_live_history_linkage(monkeypatch, tmp_path):
    output_root = tmp_path / "comfyui"
    target = output_root / "tq_krea2" / "qualification" / "cold_square_00001_.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"png")
    monkeypatch.setitem(outputs._OUTPUT_ROOTS, "output", output_root)

    async def linked(_prompt_id):
        return {
            "output": {"tq_krea2/qualification/cold_square_00001_.png"},
            "input": set(), "temp": set(),
        }

    monkeypatch.setattr(outputs, "_live_prompt_output_paths", linked)
    result = asyncio.run(outputs.list_outputs(
        kind="output", subdir="", since=None, limit=10, prefix="",
        media_kind="image", prompt_id=PROMPT_ID, tag=None, pinned=None,
        collection=None,
    ))
    assert result["total"] == 1
    assert result["files"][0]["path"] == target.relative_to(output_root).as_posix()
