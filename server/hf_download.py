"""Hugging Face download helpers used by installer subprocesses."""

from __future__ import annotations

import os
import random
import sys
import time
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")

_NON_RETRYABLE_ERROR_NAMES = {
    "DisabledRepoError",
    "EntryNotFoundError",
    "GatedRepoError",
    "RepositoryNotFoundError",
    "RevisionNotFoundError",
}
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404}
_ACCESS_PHRASES = (
    "401",
    "403",
    "access denied",
    "gated",
    "invalid username or password",
    "repo not found",
    "repository not found",
    "requires authorization",
    "unauthorized",
)


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _status_code(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    return None


def is_non_retryable_hf_error(exc: BaseException) -> bool:
    """Return True for access, auth, missing repo, and invalid request errors."""
    if exc.__class__.__name__ in _NON_RETRYABLE_ERROR_NAMES:
        return True
    code = _status_code(exc)
    if code in _NON_RETRYABLE_STATUS_CODES:
        return True
    text = str(exc).lower()
    return any(phrase in text for phrase in _ACCESS_PHRASES)


def hf_model_url(repo_id: str | None) -> str | None:
    if not repo_id:
        return None
    return f"https://huggingface.co/{repo_id.strip('/')}"


def run_with_hf_retries(
    label: str,
    repo_id: str | None,
    fn: Callable[[], T],
    *,
    attempts: int | None = None,
    base_sleep_seconds: float | None = None,
) -> T:
    """Run a HF download with retry/resume-friendly logging.

    The Hugging Face cache and local_dir arguments are supplied by callers, so
    retrying the same function resumes through the normal hub/xet cache paths.
    """
    total_attempts = attempts or _positive_int_env("OMNI_HF_DOWNLOAD_ATTEMPTS", 3)
    base_sleep = (
        float(os.environ.get("OMNI_HF_RETRY_BASE_SECONDS", "2"))
        if base_sleep_seconds is None
        else float(base_sleep_seconds)
    )
    access_url = hf_model_url(repo_id)
    last_exc: BaseException | None = None

    for attempt in range(1, total_attempts + 1):
        print(f"[hf] {label}: attempt {attempt}/{total_attempts}", flush=True)
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - installer boundary
            last_exc = exc
            if is_non_retryable_hf_error(exc):
                print(
                    f"ERROR: Hugging Face cannot access {repo_id or label}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                if access_url:
                    print(f"ACCEPT_URL: {access_url}", file=sys.stderr, flush=True)
                raise
            if attempt >= total_attempts:
                print(
                    f"ERROR: Hugging Face download failed after {total_attempts} attempts: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                if access_url:
                    print(f"MODEL_URL: {access_url}", file=sys.stderr, flush=True)
                raise
            delay = min(60.0, base_sleep * (2 ** (attempt - 1)))
            delay += random.uniform(0.0, min(1.0, base_sleep))
            print(
                f"WARNING: transient Hugging Face failure; retrying in {delay:.1f}s: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc


def snapshot_download_retry(*args, **kwargs):
    from huggingface_hub import snapshot_download

    repo_id = kwargs.get("repo_id") or (args[0] if args else None)
    return run_with_hf_retries(
        f"snapshot_download {repo_id}",
        str(repo_id) if repo_id else None,
        lambda: snapshot_download(*args, **kwargs),
    )


def hf_hub_download_retry(*args, **kwargs):
    from huggingface_hub import hf_hub_download

    repo_id = kwargs.get("repo_id") or (args[0] if args else None)
    filename = kwargs.get("filename") or (args[1] if len(args) > 1 else "")
    return run_with_hf_retries(
        f"hf_hub_download {repo_id}/{filename}",
        str(repo_id) if repo_id else None,
        lambda: hf_hub_download(*args, **kwargs),
    )
