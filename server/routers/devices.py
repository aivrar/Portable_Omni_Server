"""GPU/CPU device discovery."""

from __future__ import annotations

from fastapi import APIRouter

from state import comfy_registry, worker_manager

router = APIRouter()


@router.get("/api/devices")
async def get_devices():
    devices = await worker_manager.detect_devices_async()
    # Annotate each device with the ComfyUI instances bound to it.
    for dev in devices:
        comfy_on = [i.instance_id for i in comfy_registry.all_instances()
                    if i.device == dev["id"]]
        dev["comfy_instances"] = comfy_on
    return {"devices": devices}
