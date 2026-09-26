# License scope and third-party material

The [MIT license](LICENSE) covers original Omni Studio code and documentation
that its contributors own. Existing third-party copyright and license notices
remain in force. The MIT license does not relicense dependencies, downloaded
repositories, model weights, datasets, or media belonging to others.

The setup and model installers obtain ComfyUI, ComfyUI-Manager, Python
libraries, custom nodes, and model runtimes from upstream projects. Their
respective licenses and model-specific terms govern those downloads; retain
their notices when redistributing a built package. A source clone does not
include those installed dependencies or the separately built Windows launcher.

## Distro and launcher foundation

Omni Studio's distro packaging and Windows launcher were built from
[portable-linux-in-a-box](https://github.com/aivrar/portable-linux-in-a-box),
also maintained by `aivrar`. Preserve that project's license and existing
notices when distributing its launcher or other code with this app.

## Windows release runtimes

The prepared Windows package includes the official Python embeddable runtime
under `runtime/python/`, with its original `LICENSE.txt`. The launcher and
WebView integration also use Microsoft Visual C++ runtime DLLs distributed
beside the executable. These Microsoft components retain their own terms;
they are not covered by this project's MIT license. Their license is retained
under the package's `licenses/` directory and available from
[Microsoft](https://aka.ms/VCRedistLicense). App-local deployment is described
in Microsoft's [deployment documentation](https://learn.microsoft.com/en-us/cpp/windows/choosing-a-deployment-method).

The package's `licenses/` directory also retains the launcher foundation and
webview project's MIT notices. Dependency inventories and release provenance
identify the packaged versions. Linux package copyright files, Python package
license metadata and upstream source checkout notices remain in the distro.
Model weights are obtained separately under their respective model terms.

## Media and historical evidence

The selected PNGs under `manual/images/` were captured from Omni Studio for
the operator documentation. They show app controls and unsent examples, not
redistributed generated artwork or songs. Original documentation assets follow
the MIT scope above; displayed upstream product/model names retain their
respective rights. Capture details are in `manual/screenshots.md`.

Media in `test_assets/` and `reports/` may have separate input, model-output,
voice, likeness, or other rights. Their provenance has not been established
by the source audit. Do not infer permission to redistribute them from this
repository's MIT license. Binary media is excluded from the default Git source
release until the owner records its provenance and applicable terms. Files
remain available locally for their existing tests and investigations.

Historical reports describe the tested software and model versions at their
dates. Links to upstream artifacts are references, not a sublicense or a
claim that the linked material has the same license as Omni Studio.
