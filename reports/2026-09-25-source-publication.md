# Source publication checks - 2026-09-25

The maintainer authorized publication of `aivrar/Portable_Omni_Server` with
the agreed capability-focused description and 20 topics. The manual is
included in the source repository; the separate wiki can be populated after
the maintainer creates its initial page.

## Exact source boundary

The initial candidate set contained 406 files, approximately 6 MiB, before
adding this record. It included source, tests, documentation, JSON capability
fixtures, 18 manual screenshots, and the SVG/PNG/ICO app artwork.

Git excludes the live distro disk, model weights, caches, credentials, personal
generated media, bootstrap archives and local Windows executable/DLL files.
The original executable retained during icon embedding is also excluded.
There were no symlinks or files over GitHub's regular per-file limit in the
candidate set. Linux shell entrypoints are staged with executable permission.

## Publication review

- Examined the exact Git candidate list and the included report/fixture paths.
- Scanned all 386 eligible text files for common GitHub, Hugging Face, API,
  AWS and private-key patterns, JWTs, URL credentials and credential literals.
  The single credential-literal candidate was a shell variable reference in
  documentation, not a credential value. This is a pattern scan, not a guarantee
  against every possible secret format.
- Replaced the local Windows username path in the browser capture report with
  `%TEMP%`; the report retains the capture directory name for local lookup.
- The selected binary files are the icon and intentional documentation images.
  PNG validation passed and found no EXIF or XMP metadata.
- MIT and third-party notices accompany the source. Unreviewed fixture and
  report media remains local under the existing ignore rules.

## Verification

The staged source was exported to a fresh OS-temporary checkout without the
working distro, cached files, ignored media or launcher binaries. Checks used
the host's existing development dependencies:

| Check | Result |
| --- | --- |
| Python AST parsing | 143 source/test files passed |
| `node --check` | 16 JavaScript files passed |
| Ruff F401/F811/F821/F841 | Passed |
| Manual and CLI unittest modules | 13 tests passed in the clean checkout |
| Startup-layout pytest module | 7 tests passed in the clean checkout |
| Node UI audit regressions | Passed in the clean checkout |
| PNG file validation | 19 PNGs passed, including the branding export |
| Description/topics | 285-character description; 20 unique valid topic names |

Git's whitespace review reported Markdown hard line breaks, a few trailing
spaces in existing source/comments and two extra report EOF lines. These are
editorial and were not changed as part of publication.

No model workers, Comfy workflows, dependency installers, distro exports or
gateway restarts were run for these publication checks. The source repository
and a downloadable installed-distro archive are separate artifacts; this
publication does not include the user's working Linux disk.

## Published repository

- URL: [aivrar/Portable_Omni_Server](https://github.com/aivrar/Portable_Omni_Server)
- Visibility: public; default branch: `main`.
- Initial source commit: `bc47b8e97d56447c86d3cf849db015558e5618d4`.
- GitHub's remote tree matched the 407-file initial commit, including all 18
  manual PNGs and executable modes for the Linux entrypoints.
- GitHub's API confirmed the agreed 285-character description, all 20 topics,
  MIT license recognition and enabled wiki. The repository homepage links to
  the manual directory.
- An unauthenticated web read confirmed the public README and its prominent
  Linux distro / Windows WSL2 requirements.

The separate wiki awaits its first page from the maintainer. No installed-distro
archive or Windows executable was uploaded as part of this source publication.
