# Windows icon and foundation attribution - 2026-09-25

## Changes

- Removed the previous-handle attribution from README, the release plan and
  the preparation entry in the main audit, as requested by the maintainer.
- Credited `aivrar/portable-linux-in-a-box` as the distro and launcher foundation
  in README, the introductory manual page and license notices.
- Created `app.ico` with ten sizes (16 through 256 pixels), plus editable SVG,
  PNG export and `app-icon.rc` for future launcher builds.
- Embedded the same icon in the local `Omni_Studio.exe`. Preserved the original
  executable as the ignored `Omni_Studio.exe.pre-icon-backup`.

## Verification

- Inspected the rendered icon on light and dark backgrounds at small and large
  sizes. It uses the existing UI's white O and blue/violet tile.
- Windows loaded the ICO at 16, 20, 24, 32, 48 and 256 pixels, and extracted
  both large and small icons from the executable.
- Confirmed the existing manifest bytes and every non-resource PE section
  match the original executable. Confirmed all ten embedded image resources
  match the ICO and the group resource references those images.
- The packaged binary contains the `app.ico` lookup; the reviewed launcher
  source uses it for both `ICON_BIG` and `ICON_SMALL`.
- No launcher process, gateway, Comfy instance or worker was stopped or
  restarted. No app window was newly launched for visual verification.
- The current executable was unsigned before the resource update. Resource
  embedding for a future signed release must precede signing.

SHA-256:

| Artifact | Digest |
| --- | --- |
| Original executable | `b786050b6572f5b852ad867a35ef70b608d764326b100fae9feefd67e22b618f` |
| Updated executable | `4faafacc23a692f63a1ca757214a5e1ca6ee01b0a6dfdd3648171a711f7e9fa2` |
| `app.ico` | `7b73029f4ac06fb93e0af4e52dc00487a76b855e2366cf49e471532e5fa67aef` |

The executable and backup remain excluded from the source repository. The ICO,
resource script, SVG and PNG are intentional repository assets. See
[Windows icon packaging](../docs/windows-icon.md) for future builds.
