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

Other refs fail. The version is the Flutter tag. The workflow resolves the ref to a commit, builds the 9 RIDs and
the 3 Apple mobile platforms below, and when every build and check succeeds, publishes the release
`<version>-<sha10>` (for example `3.50.0-ab2912035e`). A run fails if that release exists. The Apple mobile platforms
need the patches of an `avalonia/<flutter tag>` branch from 3.50.0 on.

Each release has:

| Asset | Content |
|---|---|
| `impeller-<tag>-<rid>.tar.gz` | See [Archive layout](#archive-layout). |
| `impeller-<tag>-<rid>-symbols.tar.gz` | The debug symbols of the library. |
| `impeller-<tag>-<rid>-tools.tar.gz` | `impellerc` and its shader library, for the desktop RIDs. |
| `impeller-<tag>-<platform>.tar.gz` | `ios`, `tvos`, `maccatalyst`: `impeller.framework` per slice. See [Archive layout](#archive-layout). |
| `impeller-<tag>-<platform>-symbols.tar.gz` | The dSYM of each framework. |
| `SHA256SUMS` | The SHA-256 of each archive. |
| `manifest.json` | The source repository, ref, commit, version, kind (`stable` or `beta`), whether it is patched, `IMPELLER_VERSION`, the SHA-256 of `impeller.h`, and each archive by RID with its size and SHA-256. |

The RIDs are `linux-x64`, `linux-arm64`, `osx-x64`, `osx-arm64`, `win-x64`, `win-arm64`, `android-arm64`,
`android-arm` and `android-x64`. The Apple mobile platforms (manifest keys) are `ios`, `tvos` and `maccatalyst`. Each
is built as slices, one job per slice, which a packaging job combines:

| Platform | Slices | Frameworks |
|---|---|---|
| `ios` | `ios-arm64`, `iossimulator-arm64`, `iossimulator-x64` | `frameworks/ios` (arm64), `frameworks/iossimulator` (arm64, x86_64) |
| `tvos` | `tvos-arm64`, `tvossimulator-arm64`, `tvossimulator-x64` | `frameworks/tvos` (arm64), `frameworks/tvossimulator` (arm64, x86_64) |
| `maccatalyst` | `maccatalyst-arm64`, `maccatalyst-x64` | `frameworks/maccatalyst` (arm64, x86_64), also zipped |

iOS and Mac Catalyst need 15.0 or newer, tvOS 16.0 or newer.

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

impeller-<tag>-<platform>/              ios, tvos, maccatalyst
  include/, licenses/, README.md        as above
  frameworks/<dir>/impeller.framework   dir: ios, iossimulator, tvos, tvossimulator or maccatalyst
  frameworks/maccatalyst/impeller.framework.zip
                                        the versioned Mac Catalyst framework, zipped (NuGet cannot hold links)
  build-info.json                       as above, plus the frameworks and each slice's build-info

impeller-<tag>-<platform>-symbols/
  frameworks/<dir>/impeller.framework.dSYM
```

A framework's binary is `impeller` (install name `@rpath/impeller.framework/impeller`), its bundle id
`com.avaloniaui.impeller`. iOS and tvOS frameworks are shallow bundles; the Mac Catalyst one is versioned. The
frameworks are signed ad-hoc; the app build signs them again.

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
| `ios-arm64`, `iossimulator-<cpu>` | `--ios [--simulator --simulator-cpu <cpu>]` | `macos-15` | Metal only |
| `tvos-arm64`, `tvossimulator-<cpu>` | `--ios --ios-platform tvos [--simulator --simulator-cpu <cpu>]` | `macos-15` | needs the fork patches |
| `maccatalyst-<cpu>` | `--ios --ios-platform maccatalyst [--ios-cpu x64]` | `macos-15` | needs the fork patches |

`build.py` fails the build unless:

- the library exports exactly the functions that `impeller.h` declares with `IMPELLER_EXPORT`,
- on Linux, the library needs only libc, libm, libdl, libpthread, librt and the dynamic loader, and glibc 2.31 or older,
- on Android, it needs only libc, libm, libdl, liblog, libandroid, libEGL, libGLESv2 and libvulkan, and its `LOAD`
  segments are aligned to 16 KB or more,
- on macOS, its install name is `@rpath/libimpeller.dylib`,
- on the Apple mobile slices, its install name is `@rpath/impeller.framework/impeller`, its `LC_BUILD_VERSION` has
  the slice's platform and minimum (15.0, tvOS 16.0), it loads only system libraries, and a C program links with
  it (and runs, for Mac Catalyst on a macOS host),
- on Windows, its CodeView record names `impeller.dll.pdb`,
- the stripped library and its symbols have the same build ID, UUID or PDB GUID,
- on the host RID (`linux-x64`, `osx-arm64` or `win-x64`), the library loads and `ImpellerGetVersion()` equals
  `IMPELLER_VERSION` from the header.

`package-apple.py` checks each framework's architectures, signature, `Info.plist`, install name, and that its
dSYM has the same UUIDs. `make-release.py` also checks that all archives come from one commit, have the same `impeller.h` and
`IMPELLER_VERSION`, and were linked with LTO.

## Patch branches

Avalonia patches live in `AvaloniaUI/flutter` on `avalonia/<flutter tag>` branches. Each branch is the upstream tag
plus **one squashed commit**, so the patch can go upstream as is. To move it to a new Flutter release, create
`avalonia/<new tag>` from the upstream tag and cherry-pick that commit. The fork needs no tags: upstream tags are read
from `flutter/flutter`.

The commit on `avalonia/3.50.0` builds the Impeller SDK for iOS, adds tvOS and Mac Catalyst to the GN build
(`tools/gn --ios-platform tvos|maccatalyst`, `--ios-cpu x64`), lets the interop library build without GLES or
Vulkan, and makes Mac Catalyst use the macOS storage modes and uniform alignment of the Metal backend.

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

The Apple mobile slices build on macOS with Xcode (the iOS, tvOS and macOS SDKs). `build.py <slice>` writes
`release/slices/impeller-<tag>-<slice>.tar.gz`; once the slices of a platform are there, package it:

```sh
python3 scripts/build.py maccatalyst-arm64 --tag 3.50.0-<sha10> --resolved resolved.json
python3 scripts/build.py maccatalyst-x64 --tag 3.50.0-<sha10> --resolved resolved.json
python3 scripts/package-apple.py maccatalyst --tag 3.50.0-<sha10>
```

NImpeller reads a local `release/` folder with `./build.sh DownloadImpeller --builder-dir <path>`. `--partial` lets
`make-release.py` write a manifest for a subset of the RIDs.
