# Windows application icon

`app.ico` is the Omni Studio application icon. It extends the existing UI's
white O and blue-to-violet rounded tile. The editable vector is
[`assets/branding/omni-studio.svg`](../assets/branding/omni-studio.svg); a
512-pixel PNG is beside it. These are original project assets under the
repository's MIT license.

## Packaged window and taskbar

Ship [`app.ico`](../app.ico) beside `Omni_Studio.exe`. The
[portable-linux-in-a-box](https://github.com/aivrar/portable-linux-in-a-box)
launcher loads that filename for its large and small window icons. The file
contains 16, 20, 24, 32, 40, 48, 64, 96, 128 and 256-pixel images with alpha
transparency. Existing windows pick up the file when next created.

## Executable icon in Explorer

The executable also needs an embedded icon resource. The local packaged
`Omni_Studio.exe` was updated on September 25, 2026; its original manifest and
all non-resource PE sections were verified unchanged. Windows successfully
loaded the ICO and extracted both large and small icons from the updated EXE.
The existing launcher process was not stopped or restarted. Its original file
was retained as `Omni_Studio.exe.pre-icon-backup`, excluded from Git.

For subsequent builds, compile [`app-icon.rc`](../app-icon.rc) into the
launcher. In the upstream CMake project, after creating the `linux_template`
target, a Windows build can use:

```cmake
enable_language(RC)
set(OMNI_APP_SOURCE "" CACHE PATH "Path to the Omni Studio source directory")
target_sources(linux_template PRIVATE "${OMNI_APP_SOURCE}/app-icon.rc")
target_include_directories(linux_template PRIVATE "${OMNI_APP_SOURCE}")
```

Set `OMNI_APP_SOURCE` to this app's source directory when configuring CMake.
Keep the separate `app.ico` in the release even when the icon is embedded:
the current launcher uses that file for the window. Apply executable resources
before any release signing. Explorer may retain its previous cached icon until
the folder is refreshed.

The current task qualified resource loading and extraction without launching
another studio or restarting the gateway. A newly opened native app window
was not visually checked during this change.
