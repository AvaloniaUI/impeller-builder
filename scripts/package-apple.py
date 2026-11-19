#!/usr/bin/env python3
"""Combines the Apple mobile slices of build.py into impeller.framework bundles and packages a platform (macOS only).

  package-apple.py <ios|tvos|maccatalyst> --tag TAG [--slices DIR]

The slices are release/slices/impeller-<tag>-<slice>.tar.gz (build.py <slice>):

  ios          ios-arm64 -> frameworks/ios, iossimulator-arm64 + iossimulator-x64 -> frameworks/iossimulator
  tvos         tvos-arm64 -> frameworks/tvos, tvossimulator-arm64 + tvossimulator-x64 -> frameworks/tvossimulator
  maccatalyst  maccatalyst-arm64 + maccatalyst-x64 -> frameworks/maccatalyst

The arm64 and x64 libraries of a framework, and the DWARF of their dSYMs, are combined with lipo. iOS and tvOS
frameworks are shallow bundles; the Mac Catalyst framework is a versioned bundle with symbolic links, so it is also
zipped (impeller.framework.zip) for NuGet, which cannot hold links. The frameworks are signed ad-hoc; the app build
signs them again. The archives are written to release/:

  impeller-<tag>-<platform>.tar.gz          headers, frameworks/, licenses, README.md, build-info.json
  impeller-<tag>-<platform>-symbols.tar.gz  frameworks/<dir>/impeller.framework.dSYM
"""

import argparse
import json
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
RELEASE = ROOT / "release"
ARTIFACTS = ROOT / "artifacts"

# platform: [(framework directory, slices)]
PLATFORMS = {
    "ios": [("ios", ["ios-arm64"]), ("iossimulator", ["iossimulator-arm64", "iossimulator-x64"])],
    "tvos": [("tvos", ["tvos-arm64"]), ("tvossimulator", ["tvossimulator-arm64", "tvossimulator-x64"])],
    "maccatalyst": [("maccatalyst", ["maccatalyst-arm64", "maccatalyst-x64"])],
}

# framework directory: (CFBundleSupportedPlatforms, DTPlatformName, minimum version key and value, UIDeviceFamily)
BUNDLE = {
    "ios": ("iPhoneOS", "iphoneos", "MinimumOSVersion", "15.0", [1, 2]),
    "iossimulator": ("iPhoneSimulator", "iphonesimulator", "MinimumOSVersion", "15.0", [1, 2]),
    "tvos": ("AppleTVOS", "appletvos", "MinimumOSVersion", "16.0", [3]),
    "tvossimulator": ("AppleTVSimulator", "appletvsimulator", "MinimumOSVersion", "16.0", [3]),
    # Mac Catalyst 15.0 is macOS 12.0.
    "maccatalyst": ("MacOSX", "macosx", "LSMinimumSystemVersion", "12.0", None),
}

NAME = "impeller"
BUNDLE_ID = "com.avaloniaui.impeller"
INSTALL_NAME = "@rpath/impeller.framework/impeller"
LIPO_ARCH = {"arm64": "arm64", "x64": "x86_64"}
RELOCATIONS_ARCH = {"arm64": "aarch64", "x64": "x86_64"}


def run(args, capture=False):
    print("+ " + " ".join(str(a) for a in args), file=sys.stderr, flush=True)
    result = subprocess.run([str(a) for a in args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else None


def fail(message):
    sys.exit(f"error: {message}")


def uuids(path):
    return sorted(set(re.findall(r"UUID:\s*([0-9A-F-]+)", run(["xcrun", "dwarfdump", "--uuid", path], capture=True))))


def bundle_version(version, impeller_version):
    """CFBundleShortVersionString: the numeric part of the Flutter version, or IMPELLER_VERSION for local builds."""
    match = re.match(r"^(\d+\.\d+\.\d+)", version or "")
    return match.group(1) if match else impeller_version


def write_plist(path, directory, version):
    supported, dt_platform, min_key, min_value, families = BUNDLE[directory]
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": NAME,
        "CFBundlePackageType": "FMWK",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "CFBundleSupportedPlatforms": [supported],
        "DTPlatformName": dt_platform,
        min_key: min_value,
    }
    if families:
        info["UIDeviceFamily"] = families
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(info, f)


def make_framework(directory, slices, work, out_dir, version):
    """impeller.framework (and its dSYM) for one framework directory from its slices."""
    libraries = [work / rid / "lib" / "libimpeller.dylib" for rid in slices]
    framework = out_dir / "frameworks" / directory / "impeller.framework"
    versioned = directory == "maccatalyst"
    binary_dir = framework / "Versions" / "A" if versioned else framework
    resources = binary_dir / "Resources" if versioned else framework
    binary_dir.mkdir(parents=True)
    binary = binary_dir / NAME
    run(["lipo", "-create", *libraries, "-output", binary])
    write_plist(resources / "Info.plist", directory, version)
    if versioned:
        os.symlink("A", framework / "Versions" / "Current")
        os.symlink(f"Versions/Current/{NAME}", framework / NAME)
        os.symlink("Versions/Current/Resources", framework / "Resources")
    run(["codesign", "--force", "--sign", "-", framework])

    # The dSYM: the DWARF of each slice combined, named after the framework binary.
    dsym = out_dir / "symbols" / "frameworks" / directory / "impeller.framework.dSYM"
    dwarf_dir = dsym / "Contents" / "Resources" / "DWARF"
    dwarf_dir.mkdir(parents=True)
    run(["lipo", "-create", *[work / rid / "symbols" / "libimpeller.dylib.dSYM" / "Contents" / "Resources" / "DWARF" /
                              "libimpeller.dylib" for rid in slices], "-output", dwarf_dir / NAME])
    for rid in slices:
        source = work / rid / "symbols" / "libimpeller.dylib.dSYM" / "Contents" / "Resources" / "Relocations"
        arch = RELOCATIONS_ARCH[rid.split("-")[1]]
        yml = source / arch / "libimpeller.dylib.yml"
        if yml.exists():
            target = dsym / "Contents" / "Resources" / "Relocations" / arch
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(yml, target / f"{NAME}.yml")
    with open(work / slices[0] / "symbols" / "libimpeller.dylib.dSYM" / "Contents" / "Info.plist", "rb") as f:
        dsym_info = plistlib.load(f)
    dsym_info["CFBundleIdentifier"] = f"com.apple.xcode.dsym.{BUNDLE_ID}"
    with open(dsym / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(dsym_info, f)

    # Checks.
    archs = sorted(run(["lipo", "-archs", binary], capture=True).split())
    expected = sorted(LIPO_ARCH[rid.split("-")[1]] for rid in slices)
    if archs != expected:
        fail(f"{directory}: architectures {archs}, expected {expected}")
    run(["codesign", "--verify", "--strict", framework])
    run(["plutil", "-lint", resources / "Info.plist"])
    install_name = run(["otool", "-D", binary], capture=True).strip().splitlines()[-1].strip()
    if install_name != INSTALL_NAME:
        fail(f"{directory}: install name {install_name}, expected {INSTALL_NAME}")
    binary_uuids, dsym_uuids = uuids(binary), uuids(dsym)
    if not binary_uuids or binary_uuids != dsym_uuids:
        fail(f"{directory}: UUIDs differ, framework {binary_uuids}, dSYM {dsym_uuids}")
    slice_uuids = sorted(u for rid in slices for u in uuids(libraries[slices.index(rid)]))
    if slice_uuids != binary_uuids:
        fail(f"{directory}: framework UUIDs {binary_uuids} are not the slices' {slice_uuids}")

    result = {"slices": slices, "archs": archs, "uuids": binary_uuids, "installName": install_name,
              "size": binary.stat().st_size}
    if versioned:
        # NuGet packages cannot hold symbolic links: the Mac Catalyst framework is also shipped zipped.
        archive = framework.parent / "impeller.framework.zip"
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", framework, archive])
        check = work / "zip-check"
        run(["ditto", "-x", "-k", archive, check])
        run(["codesign", "--verify", "--strict", check / "impeller.framework"])
        if tree(framework) != tree(check / "impeller.framework"):
            fail(f"{archive.name} does not unpack to the framework")
        shutil.rmtree(check)
        result["zip"] = archive.name
    return result


def tree(root):
    """Each path under root with its link target or content, for comparisons."""
    result = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = pathlib.Path(directory) / name
            key = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[key] = ("link", os.readlink(path))
            elif path.is_file():
                result[key] = ("file", path.read_bytes())
            else:
                result[key] = ("dir", None)
    return result


def tar(path, root_name, source):
    """A .tar.gz of source under root_name/, with symbolic links kept, fixed owners and sorted entries."""
    def reset(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    entries = []
    for directory, dirs, files in os.walk(source):
        for name in dirs + files:
            entries.append(pathlib.Path(directory) / name)
    with tarfile.open(path, "w:gz", compresslevel=9) as archive:
        for entry in sorted(entries):
            archive.add(entry, arcname=f"{root_name}/{entry.relative_to(source).as_posix()}", recursive=False,
                        filter=reset)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("platform", choices=sorted(PLATFORMS))
    parser.add_argument("--tag", default="local", help="release tag, <version>-<sha10> (default local)")
    parser.add_argument("--slices", type=pathlib.Path, default=RELEASE / "slices",
                        help="folder of the slice archives (default release/slices)")
    args = parser.parse_args()
    if sys.platform != "darwin":
        fail("package-apple.py needs macOS (lipo, codesign, ditto)")

    platform = args.platform
    name = f"impeller-{args.tag}-{platform}"
    work = ARTIFACTS / f"{platform}-package"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    infos = {}
    for _, slices in PLATFORMS[platform]:
        for rid in slices:
            archive = args.slices / f"impeller-{args.tag}-{rid}.tar.gz"
            if not archive.exists():
                fail(f"Missing {archive}: run build.py {rid} --tag {args.tag}")
            with tarfile.open(archive) as t:
                t.extractall(work / "unpack")
            (work / "unpack" / f"impeller-{args.tag}-{rid}").rename(work / rid)
            infos[rid] = json.loads((work / rid / "build-info.json").read_text())

    first_rid, first = next(iter(infos.items()))
    errors = []
    for rid, info in infos.items():
        if info["rid"] != rid:
            errors.append(f"impeller-{args.tag}-{rid}.tar.gz holds a {info['rid']} build")
        for key in ("sha", "tag", "headerSha256", "impellerVersion", "version"):
            if info.get(key) != first.get(key):
                errors.append(f"{rid} {key} {info.get(key)} differs from {first_rid} {first.get(key)}")
    if errors:
        fail("\n".join(errors))

    out = work / "out"
    main_dir = out / "main"
    for part in ("include", "licenses"):
        shutil.copytree(work / first_rid / part, main_dir / part)
    if (work / first_rid / "README.md").exists():
        shutil.copy2(work / first_rid / "README.md", main_dir / "README.md")

    version = bundle_version(first.get("version"), first["impellerVersion"])
    frameworks = {}
    for directory, slices in PLATFORMS[platform]:
        frameworks[directory] = make_framework(directory, slices, work, main_dir, version)
    shutil.move(str(main_dir / "symbols"), str(out / "symbols"))

    lto = [info["toolchain"].get("lto") for info in infos.values()]
    toolchain = dict(first["toolchain"], lto=False if False in lto else first["toolchain"].get("lto"))
    info = {key: first.get(key) for key in ("repository", "ref", "sha", "version", "kind", "patched", "tag")}
    info.update({
        "rid": platform,
        "platform": platform,
        "target": first["target"],
        "toolchain": toolchain,
        "headerSha256": first["headerSha256"],
        "impellerVersion": first["impellerVersion"],
        "impellerVersionVariant": first["impellerVersionVariant"],
        "impellerVersionRaw": first["impellerVersionRaw"],
        "exports": first["exports"],
        "bundleId": BUNDLE_ID,
        "bundleVersion": version,
        "frameworks": frameworks,
        "slices": infos,
    })
    (main_dir / "build-info.json").write_text(json.dumps(info, indent=2) + "\n")

    RELEASE.mkdir(exist_ok=True)
    tar(RELEASE / f"{name}.tar.gz", name, main_dir)
    tar(RELEASE / f"{name}-symbols.tar.gz", f"{name}-symbols", out / "symbols")
    for suffix in ("", "-symbols"):
        archive = RELEASE / f"{name}{suffix}.tar.gz"
        print(f"{archive.name}  {archive.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
