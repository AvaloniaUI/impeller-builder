# Impeller Builder

Builds Flutter's Impeller SDK (`impeller.h` and the `libimpeller` shared library) from
[AvaloniaUI/flutter](https://github.com/AvaloniaUI/flutter) and publishes it as GitHub releases for
[NImpeller](https://github.com/AvaloniaUI/NImpeller).

## Releases

Run the **Build Impeller** workflow (Actions → Build Impeller → Run workflow) with a ref:

- an `avalonia/<flutter tag>` branch of the `repository` input (default `AvaloniaUI/flutter`), such as
  `avalonia/3.50.0`: that Flutter release plus Avalonia patches,
- a release tag of `flutter/flutter`, such as `3.50.0` or `3.50.0-0.4.pre`: the release without patches,
- the full commit SHA of either.

Other refs fail. The version is the Flutter tag. The workflow resolves the ref to a commit, builds the 9 RIDs below,
and when every build and check succeeds, publishes the release `<version>-<sha10>` (for example
`3.50.0-ab2912035e`). A run fails if that release exists.

Each release has:

| Asset | Content |
|---|---|
| `impeller-<tag>-<rid>.tar.gz` | See [Archive layout](#archive-layout). |
| `impeller-<tag>-<rid>-symbols.tar.gz` | The debug symbols of the library. |
| `impeller-<tag>-<rid>-tools.tar.gz` | `impellerc` and its shader library, for the desktop RIDs. |
| `SHA256SUMS` | The SHA-256 of each archive. |
| `manifest.json` | The source repository, ref, commit, version, kind (`stable` or `beta`), whether it is patched, `IMPELLER_VERSION`, the SHA-256 of `impeller.h`, and each archive by RID with its size and SHA-256. |

The RIDs are `linux-x64`, `linux-arm64`, `osx-x64`, `osx-arm64`, `win-x64`, `win-arm64`, `android-arm64`,
`android-arm` and `android-x64`.

### Archive layout

```
impeller-<tag>-<rid>/
  include/impeller.h, impeller.hpp
  lib/libimpeller.so | lib/libimpeller.dylib | lib/impeller.dll and lib/impeller.dll.lib
  licenses/flutter/LICENSE              Flutter's LICENSE
  licenses/sky_engine/LICENSE           the engine and its third-party licenses
  licenses/LICENSE.sdk.md               the SDK's license note
  README.md                             the SDK's README
  build-info.json                       source, GN command and args.gn, toolchain, exports, check results

impeller-<tag>-<rid>-symbols/
  lib/libimpeller.so.debug              Linux, Android: zlib-compressed DWARF, linked with .gnu_debuglink
  lib/libimpeller.dylib.dSYM/           macOS
  lib/impeller.dll.pdb                  Windows

impeller-<tag>-<rid>-tools/
  bin/impellerc[.exe]
  shader_lib/                           engine/src/flutter/impeller/compiler/shader_lib, for --include
  licenses/
```

The libraries carry no debug information. The static library, the examples and `impellerc` of Flutter's
`impeller_sdk.zip` are not in the main archive.

## Builds

All builds run `engine/src/flutter/tools/gn --runtime-mode release --no-stripped` with LTO (the default) and build
`flutter/impeller/toolkit/interop:sdk`. Flutter's published `impeller_sdk.zip` files come from
`--runtime-mode debug --no-lto` builds instead.

| RID | GN flags | Runner | Notes |
|---|---|---|---|
| `linux-x64` | (host) | `ubuntu-24.04` | Flutter's clang and Debian bullseye sysroot (glibc 2.31), `symbol_level=1` |
| `linux-arm64` | `--linux --linux-cpu arm64` | `ubuntu-24.04-arm` | as linux-x64 |
| `osx-arm64`, `osx-x64` | `--mac --mac-cpu <cpu>` | `macos-15` | x64 is cross-compiled |
| `win-x64`, `win-arm64` | `--windows-cpu <cpu>` | `windows-2022` | Visual Studio 2022 and the Windows 10 SDK; arm64 is cross-compiled |
| `android-arm64`, `android-arm`, `android-x64` | `--android --android-cpu <cpu>` | `ubuntu-24.04` | Flutter's NDK, `symbol_level=1` |

`build.py` fails the build unless:

- the library exports exactly the functions that `impeller.h` declares with `IMPELLER_EXPORT`,
- on Linux, the library needs only libc, libm, libdl, libpthread, librt and the dynamic loader, and glibc 2.31 or older,
- on Android, it needs only libc, libm, libdl, liblog, libandroid, libEGL, libGLESv2 and libvulkan, and its `LOAD`
  segments are aligned to 16 KB or more,
- on macOS, its install name is `@rpath/libimpeller.dylib`,
- on Windows, its CodeView record names `impeller.dll.pdb`,
- the stripped library and its symbols have the same build ID, UUID or PDB GUID,
- on the host RID (`linux-x64`, `osx-arm64` or `win-x64`), the library loads and `ImpellerGetVersion()` equals
  `IMPELLER_VERSION` from the header.

`make-release.py` also checks that all archives come from one commit, have the same `impeller.h` and
`IMPELLER_VERSION`, and were linked with LTO.

## Patch branches

Avalonia patches live in `AvaloniaUI/flutter` on `avalonia/<flutter tag>` branches. Each starts from the upstream
tag. To move the patches to a new Flutter release, create `avalonia/<new tag>` from the upstream tag and cherry-pick
or rebase the patches onto it. The fork needs no tags: upstream tags are read from `flutter/flutter`.

## Building locally

You need Python 3.9 or newer and Git. Windows builds need Visual Studio 2022 (Build Tools are enough) with the C++
x64 and ARM64 tools and ATL, and the Windows 10 SDK with the Debugging Tools. The first sync downloads depot_tools to
`toolchains/depot_tools`, or uses the one in `DEPOT_TOOLS`.

```sh
python3 scripts/fetch-flutter.py --ref 3.50.0 --resolve-only > resolved.json   # the commit and tag of a ref
python3 scripts/fetch-flutter.py --ref 3.50.0 --rid linux-x64                    # check out ./flutter and sync
python3 scripts/build.py linux-x64 --tag 3.50.0-ab2912035e --resolved resolved.json
python3 scripts/build.py linux-x64 --skip-build ...                              # repackage the out directory
python3 scripts/make-release.py release --tag 3.50.0-ab2912035e --resolved resolved.json --partial
```

One checkout serves all RIDs of a host. Sync it again with `--rid android-arm64` to add the Android dependencies.
`build.py` writes the out directory to `flutter/engine/src/out/impeller_<rid>` and the archives to `release/`.
Without `--tag` the tag is `local`. `--no-lto` links without LTO for small development hosts; `make-release.py`
rejects such archives unless `--allow-no-lto <rid>` is given.

NImpeller reads a local `release/` folder with `./build.sh DownloadImpeller --builder-dir <path>`. `--partial` lets
`make-release.py` write a manifest for a subset of the RIDs.
