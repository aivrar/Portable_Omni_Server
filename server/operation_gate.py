"""Reject overlapping engine mutations before worker selection or progress updates."""
import asyncio
import inspect
from functools import wraps

from fastapi import HTTPException


class OperationGate:
    def __init__(self, label):
        self.label = label
        self.owner = None

    def locked(self):
        return self.owner is not None

    def __call__(self, func):
        @wraps(func)
        async def guarded(*args, **kwargs):
            task = asyncio.current_task()
            if self.owner is task:
                return await func(*args, **kwargs)
            if self.owner is not None:
                raise HTTPException(409, f"{self.label} operation already in progress")
            self.owner = task
            try:
                return await func(*args, **kwargs)
            finally:
                self.owner = None
        guarded.__signature__ = inspect.signature(func, eval_str=True)
        return guarded
