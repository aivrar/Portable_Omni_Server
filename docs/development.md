# Development and source setup

Run commands below from the Omni Studio source root with the gateway/test
Python dependencies installed. Read [AGENTS.md](../AGENTS.md) before operating
the distro or its model workers.

## Focused checks

```text
python -m unittest tests.test_manual_coverage tests.test_cli
python -m pytest -q tests/test_startup_layout_regression.py tests/test_ace_step_audio_sources.py
python -m ruff check bridge.py bridge_watchdog.py windows_loopback_relay.py cli server comfy_nodes tests --select F401,F811,F821,F841
```

Check changed browser JavaScript with `node --check`.

The complete suite includes process lifecycle checks that have disrupted an
interactive Windows host. Directory-wide pytest is disabled on Windows by
default. Use reviewed explicit test files on an interactive host; a separate
disposable test host may set `OMNI_DISPOSABLE_TEST_HOST=1` for a full campaign.
Redirecting output or starting a child Python process alone does not isolate
those lifecycle effects.

## Install source into the Linux distro

`server/setup.sh` synchronizes the server, CLI, API guidance and agent skills
into `/opt/omni_studio`. From inside the child Linux distro:

```bash
bash /path/to/Omni_Studio/server/setup.sh
```

For manifest-driven setup, set `OMNI_STUDIO_SOURCE_DIR` to the clone's Linux
path, or launch setup from its root. The manifest also accepts the packaged
source path and the original Windows template location. Missing source is an
explicit error. Model downloads and GPU inference use the running packaged app
and the analysis/cleanup procedures in AGENTS.md.

The Windows launcher comes from
[portable-linux-in-a-box](https://github.com/aivrar/portable-linux-in-a-box).
This child repository contains the application, not that launcher's full build
tree. See [Windows icon packaging](windows-icon.md) for the app resource and
[repository layout](repository-layout.md) for source/runtime boundaries.

## Engineering records

- [Source repository audit](../reports/2026-09-25-repository-audit.md)
- [Repair ledger](../reports/2026-09-25-full-read-only-code-audit.md)
- [Capability evidence](capability-confidence.md)
- [Release preparation](github-release-plan.md)

Historical records describe their dated checks. Use later entries when they
supersede earlier installation snapshots. Retained media and reports require
a publication review for private information and redistribution rights.
