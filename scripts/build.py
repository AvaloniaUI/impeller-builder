#!/usr/bin/env python3
"""Builds the Impeller SDK of the Flutter checkout in ./flutter (see fetch-flutter.py) for one RID and packages it.

  build.py <rid> [--tag TAG] [--skip-build] [--no-lto]

The archives are written to release/:

  impeller-<tag>-<rid>.tar.gz          headers, the stripped shared library, licenses, build-info.json
  impeller-<tag>-<rid>-symbols.tar.gz  the debug symbols of the library (.so.debug, .dylib.dSYM or .dll.pdb)
  impeller-<tag>-<rid>-tools.tar.gz    impellerc and its shader library (desktop RIDs only)

The tag is "local" when --tag is not given. --skip-build repackages an existing out directory. --no-lto is for
development hosts that cannot link with LTO; make-release.py rejects such archives by default.
"""

import argparse
import ctypes
import glob
import hashlib
import io
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
FLUTTER = ROOT / "flutter"
ENGINE = FLUTTER / "engine" / "src"
RELEASE = ROOT / "release"
ARTIFACTS = ROOT / "artifacts"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# rid: (target os, cpu, gn flags)
TARGETS = {
    "linux-x64": ("linux", "x64", []),
    "linux-arm64": ("linux", "arm64", ["--linux", "--linux-cpu", "arm64"]),
    "osx-x64": ("osx", "x64", ["--mac", "--mac-cpu", "x64"]),
    "osx-arm64": ("osx", "arm64", ["--mac", "--mac-cpu", "arm64"]),
    "win-x64": ("win", "x64", ["--windows-cpu", "x64"]),
    "win-arm64": ("win", "arm64", ["--windows-cpu", "arm64"]),
    "android-arm64": ("android", "arm64", ["--android", "--android-cpu", "arm64"]),
    "android-arm": ("android", "arm", ["--android", "--android-cpu", "arm"]),
    "android-x64": ("android", "x64", ["--android", "--android-cpu", "x64"]),
}

DESKTOP = {"linux", "osx", "win"}

LIBRARY = {"linux": "libimpeller.so", "android": "libimpeller.so", "osx": "libimpeller.dylib", "win": "impeller.dll"}

# Shared libraries the library may depend on.
LINUX_NEEDED = {"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1", "ld-linux-x86-64.so.2",
                "ld-linux-aarch64.so.1"}
ANDROID_NEEDED = {"libc.so", "libm.so", "libdl.so", "liblog.so", "libandroid.so", "libEGL.so", "libGLESv2.so",
                  "libvulkan.so"}
GLIBC_MAX = (2, 31)
ANDROID_PAGE = 0x4000

HOST_RIDS = {"linux-x64", "osx-arm64", "win-x64"}


def run(args, cwd=None, env=None, capture=False, check=True):
    print("+ " + " ".join(str(a) for a in args), file=sys.stderr, flush=True)
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=check, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.STDOUT if capture else None)
    return result.stdout if capture else result.returncode


def fail(message):
    sys.exit(f"error: {message}")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def host_os():
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "mac"
    if sys.platform == "win32":
        return "windows"
    fail(f"unsupported host {sys.platform}")


def host_cpu():
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "x64"


def depot_tools():
    existing = os.environ.get("DEPOT_TOOLS")
    return pathlib.Path(existing) if existing else ROOT / "toolchains" / "depot_tools"


def vs_install():
    """The newest Visual Studio installation (Build Tools count) with the C++ tools."""
    vswhere = pathlib.Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
        "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.exists():
        fail(f"vswhere not found at {vswhere}")
    out = run([vswhere, "-latest", "-products", "*", "-requires",
               "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-format", "json"], capture=True)
    installs = json.loads(out)
    if not installs:
        fail("No Visual Studio installation with the C++ x64 tools")
    return installs[0]


def build_env():
    env = dict(os.environ)
    tools = depot_tools()
    env["PATH"] = str(tools) + os.pathsep + env.get("PATH", "")
    env["DEPOT_TOOLS_UPDATE"] = "0"
    env["DEPOT_TOOLS_METRICS"] = "0"
    if host_os() == "windows":
        env["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"
        env.setdefault("GYP_MSVS_OVERRIDE_PATH", vs_install()["installationPath"])
    return env


def llvm_bin():
    """The bin directory of the Flutter checkout's host clang."""
    host = {"linux": "linux", "mac": "mac", "windows": "windows"}[host_os()] + "-" + host_cpu()
    for candidate in (ENGINE / "flutter" / "buildtools" / host / "clang" / "bin",
                      ENGINE / "buildtools" / host / "clang" / "bin"):
        if candidate.exists():
            return candidate
    found = sorted(glob.glob(str(ENGINE / "flutter" / "buildtools" / "*" / "clang" / "bin")))
    if found:
        return pathlib.Path(found[0])
    fail("No clang in the Flutter checkout's buildtools")


def tool(name):
    exe = name + (".exe" if host_os() == "windows" else "")
    path = llvm_bin() / exe
    if not path.exists():
        fail(f"{path} not found")
    return path


def ninja():
    exe = "ninja.exe" if host_os() == "windows" else "ninja"
    for candidate in (FLUTTER / "third_party" / "ninja" / exe, ENGINE / "flutter" / "third_party" / "ninja" / exe):
        if candidate.exists():
            return candidate
    found = shutil.which("ninja")
    if found:
        return pathlib.Path(found)
    fail("ninja not found in the Flutter checkout or on PATH")


def out_dir(rid):
    return ENGINE / "out" / ("impeller_" + rid.replace("-", "_"))


def gn_command(rid, lto):
    os_name, _, flags = TARGETS[rid]
    command = [sys.executable, ENGINE / "flutter" / "tools" / "gn",
               "--runtime-mode", "release", "--no-rbe", "--no-goma", "--no-enable-unittests", "--no-stripped",
               "--target-dir", out_dir(rid).name] + flags
    if not lto:
        command.append("--no-lto")
    # Release Linux builds have no debug information by default. Line tables are enough to symbolize crashes and
    # keep the LTO link of the Android libraries small.
    if os_name in ("linux", "android"):
        command += ["--gn-args", "symbol_level=1"]
    return command


def build(rid, lto, env):
    command = gn_command(rid, lto)
    run(command, cwd=ENGINE, env=env)
    target = [ninja(), "-C", out_dir(rid), "flutter/impeller/toolkit/interop:sdk"]
    # A first build has failed once on a missing .rsp file of an archive step; a second, incremental run passed.
    # A real compile error fails both runs.
    if run(target, cwd=ENGINE, env=env, check=False) != 0:
        print("ninja failed; retrying once", file=sys.stderr, flush=True)
        run(target, cwd=ENGINE, env=env)
    return command


def find_sdk_zip(rid):
    zips = sorted(glob.glob(str(out_dir(rid) / "zip_archives" / "**" / "impeller_sdk.zip"), recursive=True))
    if len(zips) != 1:
        fail(f"Expected one impeller_sdk.zip in {out_dir(rid) / 'zip_archives'}, found {len(zips)}")
    return pathlib.Path(zips[0])


# ----------------------------------------------------------------------------------------------------------------
# impeller.h


def header_exports(header):
    """The functions impeller.h declares with IMPELLER_EXPORT."""
    names = set()
    lines = header.read_text().splitlines()
    for i, line in enumerate(lines):
        # "IMPELLER_EXPORT" alone, or followed by IMPELLER_NODISCARD and the return type on the same line.
        if not re.match(r"^IMPELLER_EXPORT\b", line.strip()):
            continue
        decl = line.strip()[len("IMPELLER_EXPORT"):]
        for following in [""] + lines[i + 1:i + 10]:
            decl += " " + following
            if "(" in following:
                break
        match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", decl)
        if not match:
            fail(f"Cannot parse the declaration after line {i + 1} of {header}")
        names.add(match.group(1))
    if not names:
        fail(f"No IMPELLER_EXPORT declarations in {header}")
    return names


def header_version(header):
    text = header.read_text()
    parts = {}
    for key in ("VARIANT", "MAJOR", "MINOR", "PATCH"):
        match = re.search(rf"#define\s+IMPELLER_VERSION_{key}\s+(\d+)", text)
        if not match:
            fail(f"IMPELLER_VERSION_{key} not found in {header}")
        parts[key] = int(match.group(1))
    raw = (parts["VARIANT"] << 29) | (parts["MAJOR"] << 22) | (parts["MINOR"] << 12) | parts["PATCH"]
    return f"{parts['MAJOR']}.{parts['MINOR']}.{parts['PATCH']}", parts["VARIANT"], raw


# ----------------------------------------------------------------------------------------------------------------
# Symbols and checks per OS


def elf_split(lib, symbols_dir):
    debug = symbols_dir / (lib.name + ".debug")
    run([tool("llvm-objcopy"), "--only-keep-debug", "--compress-debug-sections=zlib", lib, debug])
    run([tool("llvm-strip"), "--strip-unneeded", lib])
    run([tool("llvm-objcopy"), f"--add-gnu-debuglink={debug}", lib])
    return [debug]


def elf_build_id(path):
    out = run([tool("llvm-readelf"), "-n", path], capture=True)
    match = re.search(r"Build ID:\s*([0-9a-f]+)", out)
    return match.group(1) if match else None


def elf_checks(os_name, lib, debug, checks):
    sections = run([tool("llvm-readelf"), "-SW", lib], capture=True)
    if re.search(r"\.debug_(info|line)", sections):
        fail(f"{lib.name} still has debug sections after the split")
    lib_id, debug_id = elf_build_id(lib), elf_build_id(debug)
    if not lib_id or lib_id != debug_id:
        fail(f"Build IDs differ: {lib.name} {lib_id}, {debug.name} {debug_id}")
    checks["buildId"] = lib_id
    if not re.search(r"\.debug_line", run([tool("llvm-readelf"), "-SW", debug], capture=True)):
        fail(f"{debug.name} has no line tables")

    dynamic = run([tool("llvm-readelf"), "-dW", lib], capture=True)
    needed = re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", dynamic)
    allowed = ANDROID_NEEDED if os_name == "android" else LINUX_NEEDED
    extra = sorted(set(needed) - allowed)
    if extra:
        fail(f"{lib.name} depends on unexpected libraries: {', '.join(extra)}")
    checks["needed"] = needed

    if os_name == "linux":
        versions = re.findall(r"GLIBC_(\d+)\.(\d+)", run([tool("llvm-readelf"), "-VW", lib], capture=True))
        highest = max(((int(a), int(b)) for a, b in versions), default=(0, 0))
        if highest > GLIBC_MAX:
            fail(f"{lib.name} needs glibc {highest[0]}.{highest[1]}, more than {GLIBC_MAX[0]}.{GLIBC_MAX[1]}")
        checks["glibc"] = f"{highest[0]}.{highest[1]}"
    else:
        segments = run([tool("llvm-readelf"), "-lW", lib], capture=True)
        aligns = [int(a, 16) for a in re.findall(r"^\s*LOAD\s.*\s(0x[0-9a-f]+)\s*$", segments, re.M)]
        if not aligns or min(aligns) < ANDROID_PAGE:
            fail(f"{lib.name} LOAD segments are not aligned to 16 KB: {[hex(a) for a in aligns]}")
        checks["loadAlignment"] = hex(min(aligns))

    symbols = run([tool("llvm-nm"), "-D", "--defined-only", lib], capture=True)
    return {m for m in re.findall(r"\s[TW]\s+(Impeller\w+)$", symbols, re.M)}


def xcrun(*args, capture=False):
    return run(["xcrun", *args], capture=capture)


def macho_uuids(path):
    return sorted(set(re.findall(r"UUID:\s*([0-9A-F-]+)", xcrun("dwarfdump", "--uuid", path, capture=True))))


def macho_split(lib, built, symbols_dir):
    # dsymutil reads the debug map of the library in the out directory, where the object files (and the LTO object,
    # -object_path_lto) are.
    dsym = symbols_dir / (lib.name + ".dSYM")
    xcrun("dsymutil", built, "-o", dsym)
    xcrun("strip", "-x", lib)
    # Flutter links the library with the install name ./libimpeller.dylib. The UUID does not change.
    xcrun("install_name_tool", "-id", "@rpath/libimpeller.dylib", lib)
    # strip and install_name_tool invalidate the linker's ad-hoc signature, which arm64 needs to load the library.
    run(["codesign", "--force", "--sign", "-", lib])
    return [dsym]


def macho_checks(lib, dsym, checks):
    lib_uuids, dsym_uuids = macho_uuids(lib), macho_uuids(dsym)
    if not lib_uuids or lib_uuids != dsym_uuids:
        fail(f"UUIDs differ: {lib.name} {lib_uuids}, {dsym.name} {dsym_uuids}")
    checks["uuid"] = lib_uuids
    dwarf = glob.glob(str(dsym / "Contents" / "Resources" / "DWARF" / "*"))
    if not dwarf or os.path.getsize(dwarf[0]) < 1 << 20:
        fail(f"{dsym.name} has no debug information")
    # strip leaves one OPT stab ("radr://5614542") on purpose; any other stab is a debug map entry.
    stabs = [line for line in xcrun("nm", "-a", lib, capture=True).splitlines()
             if re.search(r"\s-\s", line) and " OPT " not in line]
    if stabs:
        fail(f"{lib.name} still has debug map (stab) symbols after strip")

    install_name = xcrun("otool", "-D", lib, capture=True).strip().splitlines()[-1].strip()
    if install_name != "@rpath/libimpeller.dylib":
        fail(f"{lib.name} install name is {install_name}, expected @rpath/libimpeller.dylib")
    checks["installName"] = install_name
    run(["codesign", "--verify", "--strict", lib])
    checks["codesign"] = "ad-hoc"
    load = xcrun("otool", "-l", lib, capture=True)
    match = re.search(r"cmd LC_BUILD_VERSION.*?minos ([0-9.]+)", load, re.S)
    if not match:
        match = re.search(r"cmd LC_VERSION_MIN_MACOSX.*?version ([0-9.]+)", load, re.S)
    if not match:
        fail(f"No minimum macOS version in {lib.name}")
    checks["minimumMacOS"] = match.group(1)
    checks["dependencies"] = [line.strip().split(" ")[0]
                              for line in xcrun("otool", "-L", lib, capture=True).splitlines()[2:] if line.strip()]
    symbols = xcrun("nm", "-gU", lib, capture=True)
    return {m for m in re.findall(r"\s[TS]\s+_(Impeller\w+)$", symbols, re.M)}


def pe_split(lib, built, symbols_dir):
    pdb = built.parent / (built.name + ".pdb")
    if not pdb.exists():
        fail(f"{pdb} not found")
    target = symbols_dir / pdb.name
    shutil.copy2(pdb, target)
    return [target]


def pe_checks(rid, lib, pdb, checks):
    readobj = tool("llvm-readobj")
    debug_dir = run([readobj, "--coff-debug-directory", lib], capture=True)
    guid = re.search(r"PDBGUID:\s*\(?([0-9A-Fa-f{}-]+)", debug_dir)
    age = re.search(r"PDBAge:\s*(\d+)", debug_dir)
    name = re.search(r"PDBFileName:\s*(.+)", debug_dir)
    if not guid or not name:
        fail(f"{lib.name} has no CodeView debug directory entry")
    if pathlib.PureWindowsPath(name.group(1).strip()).name.lower() != pdb.name.lower():
        fail(f"{lib.name} names the PDB {name.group(1).strip()}, expected {pdb.name}")
    checks["pdbGuid"] = guid.group(1)
    checks["pdbAge"] = int(age.group(1)) if age else None
    pdbutil = llvm_bin() / "llvm-pdbutil.exe"
    if pdbutil.exists():
        summary = run([pdbutil, "dump", "--summary", pdb], capture=True)
        pdb_guid = re.search(r"GUID:\s*\{?([0-9A-Fa-f-]+)\}?", summary)
        if not pdb_guid or pdb_guid.group(1).strip("{}").lower() != guid.group(1).strip("{}").lower():
            fail(f"PDB GUID {pdb_guid.group(1) if pdb_guid else None} does not match {lib.name} {guid.group(1)}")
        checks["pdbGuidVerified"] = True

    imports = run([readobj, "--coff-imports", lib], capture=True)
    checks["imports"] = sorted(set(re.findall(r"Name:\s*(\S+\.dll)", imports, re.I)), key=str.lower)
    exports = run([readobj, "--coff-exports", lib], capture=True)
    return set(re.findall(r"Name:\s*(Impeller\w+)", exports))


# ----------------------------------------------------------------------------------------------------------------
# Smoke tests


SMOKE_C = r"""
#include <stdio.h>
#include "impeller.h"
int main(void) {
  uint32_t version = ImpellerGetVersion();
  printf("%u %u\n", version, (uint32_t)IMPELLER_VERSION);
  return version == IMPELLER_VERSION ? 0 : 1;
}
"""


def smoke(rid, stage, raw_version, checks):
    """Builds a C program against include/impeller.h, links it with the library and runs it."""
    os_name = TARGETS[rid][0]
    lib_dir = stage / "lib"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        if os_name == "win":
            # Loading through ctypes checks the DLL and its imports without a VS developer environment.
            library = ctypes.WinDLL(str(lib_dir / LIBRARY["win"]))
            library.ImpellerGetVersion.restype = ctypes.c_uint32
            actual = library.ImpellerGetVersion()
        else:
            source = tmp / "smoke.c"
            source.write_text(SMOKE_C)
            exe = tmp / "smoke"
            compiler = ["xcrun", "clang"] if os_name == "osx" else [os.environ.get("CC", "cc")]
            run(compiler + ["-std=c11", "-I", stage / "include", source, "-o", exe, "-L", lib_dir, "-limpeller",
                            f"-Wl,-rpath,{lib_dir}"])
            out = run([exe], capture=True, check=False)
            actual = int(out.split()[0]) if out.split() else None
    if actual != raw_version:
        fail(f"ImpellerGetVersion() returned {actual}, IMPELLER_VERSION is {raw_version}")
    checks["smoke"] = "passed"


# ----------------------------------------------------------------------------------------------------------------
# Toolchain


def toolchain(rid, env, lto):
    os_name = TARGETS[rid][0]
    # Flutter's tools/gn has no LTO switch for the Windows toolchain: null means not applicable.
    info = {"lto": None if os_name == "win" else lto, "host": f"{host_os()}-{host_cpu()}"}
    clang = llvm_bin() / ("clang-cl.exe" if host_os() == "windows" else "clang")
    if clang.exists():
        info["clang"] = run([clang, "--version"], capture=True).splitlines()[0].strip()
    if os_name == "osx":
        info["xcode"] = " ".join(run(["xcodebuild", "-version"], capture=True).split())
        info["sdk"] = run(["xcrun", "--show-sdk-version"], capture=True).strip()
    if os_name == "win":
        vs = vs_install()
        info["visualStudio"] = f"{vs['displayName']} {vs['catalog']['productDisplayVersion']}"
        info["visualStudioPath"] = env.get("GYP_MSVS_OVERRIDE_PATH")
        sdks = sorted(glob.glob(r"C:\Program Files (x86)\Windows Kits\10\Include\10.*"))
        info["windowsSdks"] = [pathlib.Path(s).name for s in sdks]
    if os_name == "android":
        for props in sorted(glob.glob(str(ENGINE / "flutter" / "third_party" / "android_tools" / "sdk" / "ndk" / "*" /
                                           "source.properties"))):
            match = re.search(r"Pkg\.Revision\s*=\s*(\S+)", pathlib.Path(props).read_text())
            if match:
                info["ndk"] = match.group(1)
                break
    return info


# ----------------------------------------------------------------------------------------------------------------
# Archives


def tar(path, root_name, source):
    """A .tar.gz of the files in source under root_name/, with fixed owners and sorted entries."""
    def reset(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    with tarfile.open(path, "w:gz", compresslevel=9) as archive:
        for file in sorted(source.rglob("*")):
            archive.add(file, arcname=f"{root_name}/{file.relative_to(source).as_posix()}", recursive=False,
                        filter=reset)


def copy_licenses(target):
    licenses = target / "licenses"
    (licenses / "flutter").mkdir(parents=True)
    (licenses / "sky_engine").mkdir(parents=True)
    shutil.copy2(FLUTTER / "LICENSE", licenses / "flutter" / "LICENSE")
    shutil.copy2(ENGINE / "flutter" / "sky" / "packages" / "sky_engine" / "LICENSE",
                 licenses / "sky_engine" / "LICENSE")


def git_head():
    return run(["git", "rev-parse", "HEAD"], cwd=FLUTTER, capture=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("rid", choices=sorted(TARGETS))
    parser.add_argument("--tag", default="local", help="release tag, <version>-<sha10> (default local)")
    parser.add_argument("--resolved", type=pathlib.Path,
                        help="JSON from fetch-flutter.py --resolve-only, recorded in build-info.json")
    parser.add_argument("--skip-build", action="store_true", help="package an existing out directory")
    parser.add_argument("--no-lto", action="store_true", help="link without LTO (development hosts)")
    args = parser.parse_args()

    rid = args.rid
    os_name, cpu, _ = TARGETS[rid]
    lto = not args.no_lto
    env = build_env()

    if args.skip_build:
        gn = gn_command(rid, lto)
    else:
        gn = build(rid, lto, env)

    out = out_dir(rid)
    sdk_zip = find_sdk_zip(rid)
    name = f"impeller-{args.tag}-{rid}"
    work = ARTIFACTS / rid
    if work.exists():
        shutil.rmtree(work)
    unpacked = work / "zip"
    main_dir, symbols_dir, tools_dir = work / "main", work / "symbols", work / "tools"
    for d in (unpacked, main_dir / "include", main_dir / "lib", symbols_dir / "lib"):
        d.mkdir(parents=True)
    with zipfile.ZipFile(sdk_zip) as z:
        z.extractall(unpacked)

    # Main archive: headers, the shared library (and the import library on Windows), licenses, README.
    # LF line endings on every RID: Flutter's .gitattributes (text=auto) checks out CRLF on Windows.
    for header in ("impeller.h", "impeller.hpp"):
        data = (unpacked / "include" / header).read_bytes().replace(b"\r\n", b"\n")
        (main_dir / "include" / header).write_bytes(data)
    library_name = LIBRARY[os_name]
    lib = main_dir / "lib" / library_name
    shutil.copy2(unpacked / "lib" / library_name, lib)
    if os_name == "win":
        shutil.copy2(unpacked / "lib" / "impeller.dll.lib", main_dir / "lib" / "impeller.dll.lib")
    copy_licenses(main_dir)
    for extra in ("LICENSE.sdk.md",):
        if (unpacked / extra).exists():
            shutil.copy2(unpacked / extra, main_dir / "licenses" / extra)
    if (unpacked / "README.md").exists():
        shutil.copy2(unpacked / "README.md", main_dir / "README.md")

    header = main_dir / "include" / "impeller.h"
    declared = header_exports(header)
    version, variant, raw_version = header_version(header)

    checks = {}
    built = out / library_name
    if os_name in ("linux", "android"):
        (debug,) = elf_split(lib, symbols_dir / "lib")
        exported = elf_checks(os_name, lib, debug, checks)
    elif os_name == "osx":
        (dsym,) = macho_split(lib, built, symbols_dir / "lib")
        exported = macho_checks(lib, dsym, checks)
    else:
        (pdb,) = pe_split(lib, built, symbols_dir / "lib")
        exported = pe_checks(rid, lib, pdb, checks)

    if "ImpellerGetVersion" not in exported:
        fail(f"{library_name} does not export ImpellerGetVersion")
    missing, unexpected = sorted(declared - exported), sorted(exported - declared)
    if missing or unexpected:
        fail(f"Exports differ from impeller.h: missing {missing}, not declared {unexpected}")
    checks["exports"] = len(exported)

    host_rid = {"linux": "linux", "mac": "osx", "windows": "win"}[host_os()] + "-" + host_cpu()
    if rid in HOST_RIDS and rid == host_rid:
        smoke(rid, main_dir, raw_version, checks)

    # Tools archive: impellerc and the shader library it includes from.
    if os_name in DESKTOP:
        exe = "impellerc.exe" if os_name == "win" else "impellerc"
        (tools_dir / "bin").mkdir(parents=True)
        shutil.copy2(out / exe, tools_dir / "bin" / exe)
        if os_name == "linux":
            run([tool("llvm-strip"), "--strip-unneeded", tools_dir / "bin" / exe])
        elif os_name == "osx":
            xcrun("strip", "-x", tools_dir / "bin" / exe)
        shutil.copytree(ENGINE / "flutter" / "impeller" / "compiler" / "shader_lib", tools_dir / "shader_lib")
        copy_licenses(tools_dir)

    resolved = json.loads(args.resolved.read_text()) if args.resolved else {}
    sha = git_head()
    if resolved.get("sha") and resolved["sha"] != sha:
        fail(f"./flutter is at {sha}, the resolved commit is {resolved['sha']}")
    args_gn = (out / "args.gn").read_text() if (out / "args.gn").exists() else ""
    info = {
        "repository": resolved.get("repository"),
        "ref": resolved.get("ref"),
        "sha": sha,
        "version": resolved.get("version"),
        "kind": resolved.get("kind"),
        "patched": resolved.get("patched"),
        "tag": args.tag,
        "rid": rid,
        "gn": [str(a) if not isinstance(a, pathlib.Path) else a.relative_to(ENGINE).as_posix()
               for a in gn[1:]],
        "argsGn": args_gn,
        "target": "flutter/impeller/toolkit/interop:sdk",
        "toolchain": toolchain(rid, env, lto),
        "headerSha256": sha256(header),
        "impellerVersion": version,
        "impellerVersionVariant": variant,
        "impellerVersionRaw": raw_version,
        "exports": sorted(exported),
        "checks": checks,
    }
    (main_dir / "build-info.json").write_text(json.dumps(info, indent=2) + "\n")

    RELEASE.mkdir(exist_ok=True)
    tar(RELEASE / f"{name}.tar.gz", name, main_dir)
    tar(RELEASE / f"{name}-symbols.tar.gz", f"{name}-symbols", symbols_dir)
    if os_name in DESKTOP:
        tar(RELEASE / f"{name}-tools.tar.gz", f"{name}-tools", tools_dir)
    for suffix in ("", "-symbols", "-tools"):
        archive = RELEASE / f"{name}{suffix}.tar.gz"
        if archive.exists():
            print(f"{archive.name}  {archive.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
