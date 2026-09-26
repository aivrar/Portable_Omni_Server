# Shutdown

![Shutdown control in Omni Studio](images/shutdown-control.png)

*The top-bar Shutdown button. Cancelling its confirmation was tested without stopping the gateway. Captured September 25, 2026.*

**Shutdown** is the red power control in the top bar. It is a full
application stop, not a tab close and not "kill this one worker."

The button label is **Shutdown**. Its tooltip: "Unload models and stop Omni
Studio."

## What the button does

When you click it, the UI asks:

> Shut down Omni Studio?
>
> This unloads every model, stops ComfyUI, cancels downloads and jobs, kills
> app-owned processes, and closes the app.

**Cancel** leaves everything running. **OK** sets the UI into shutting-down
state:

1. The button disables and reads **Shutting down...**
2. The log SSE connection closes
3. A full-screen overlay says models are unloading
4. The UI posts **`POST /api/app/shutdown`** with `{ "confirm": true }`
5. The bridge runs the Omni shutdown sweep (workers, Comfy, jobs, gateway)
6. The matching Windows host process (`Omni_Studio.exe`) exits

A dropped connection after the request is normal; the bridge may disappear
before the HTTP response returns. 400/401/403/413 are real refusals and the
overlay clears so you can try again.

You cannot generate during this overlay. Wait. If the window is still up
after several minutes, check Task Manager for a stuck `Omni_Studio.exe` and
WSL processes, then see recovery below.

## Full shutdown versus everything else

| Action | What stops | What survives |
|---|---|---|
| **Shutdown** (this button / `/api/app/shutdown`) | Workers, Comfy, jobs, gateway, bridge, watchdog, Windows window | Distro disk: models, workflows, Media library files |
| Runtime **Kill** one worker | That worker only | Everything else |
| Runtime **Kill all workers** | All Omni/audio workers | Comfy, gateway, window |
| Engine **Stop** / **Stop all** | Comfy instance(s) | Workers, gateway |
| CLI `omni-cli system shutdown` | Gateway receives SIGTERM and stops workers and Comfy; the bridge may relaunch it while the app stays open | Models, workflows, and media files on disk; Windows window |
| CLI `omni-cli system restart` | Gateway restarts; graceful gateway teardown also stops workers and Comfy instances | Models, workflows, and media files on disk; Windows window |

Do **not** use Kill all, Stop all, or Shutdown to unload a single ACE-Step
model. Unload the component, then delete that worker id.

Do **not** use SIGKILL tricks from `AGENTS.md` as an operator shutdown.
Those notes are for developers refreshing gateway source while preserving
workers. They are unsafe during a job and are not the Shutdown button.

After a normal gateway restart, start the required worker or Comfy instance
again before generating. A source-only developer refresh can preserve them
only through the separate guarded procedure in `AGENTS.md`.

## When to Shutdown

- You are done for the day
- You need Windows to reclaim WSL GPU memory completely
- You will copy/move the app folder (never move the VHDX while the distro
  is running)
- The UI is wedged after a failed CUDA init and you want a clean slate

## When not to Shutdown

- A 10-minute Music 3 generate is in flight and you wanted to keep it
- You only meant to leave Chat and start Comfy — kill the chat worker
  instead
- You are mid HuggingFace download and can **Cancel** that job instead

Shutdown cancels downloads. Partial files may remain and confuse a later
install; retry the variant after the next launch.

## After Shutdown

The Linux distro `linbox-Omni_Studio` still exists on disk (`ext4.vhdx` beside
the executable for this release, or `wsl/ext4.vhdx` in older installations).
Models and output stay inside the distro. Double-click `Omni_Studio.exe`
again to reconnect. First GPU use after a full stop may wait on CUDA
bring-up again.

If you need the distro stopped at the WSL layer as well (rare), after the
window closes:

```powershell
wsl -t linbox-Omni_Studio
```

Do not `wsl --unregister linbox-Omni_Studio` unless you intend to destroy
the studio disk.

## Recovery if the window died without Shutdown

1. Do not start a second unzipped copy of the app.
2. From the original folder, launch `Omni_Studio.exe` once.
3. If port 9200 is occupied, wait; the watchdog may still be tearing down.
4. `wsl -d linbox-Omni_Studio -- echo ok` confirms the distro.
5. Open Logs after connect and Runtime to see leftover workers. Kill
   stragglers before spawning new ones.

## Related pages

- [Launch and first run](02-launch-and-first-run.md)
- [Runtime](07-runtime.md)
- [Command-line interface](18-cli.md)
