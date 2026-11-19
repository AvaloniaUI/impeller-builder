#!/usr/bin/env python3
"""Checks the archives of a release and writes SHA256SUMS, manifest.json and release-notes.md next to them.

  make-release.py <dir> --tag T --repository R --ref REF --sha S --version V --kind K --patched true|false
  make-release.py <dir> --tag T --resolved resolved.json          (the JSON of fetch-flutter.py --resolve-only)

Every RID needs a main and a symbols archive, and every desktop RID a tools archive. The Apple mobile platforms (ios,
tvos, maccatalyst; package-apple.py) need a main and a symbols archive. --partial accepts a subset, for local builds
that NImpeller reads with --builder-dir; a published release always has all of them.
The archives must agree on the commit, impeller.h and IMPELLER_VERSION, and be built with LTO unless the RID is
listed with --allow-no-lto.
"""

import argparse
import hashlib
import json
import pathlib
import sys
import tarfile

RIDS = ["linux-x64", "linux-arm64", "osx-x64", "osx-arm64", "win-x64", "win-arm64",
        "android-arm64", "android-arm", "android-x64"]
DESKTOP = {"linux-x64", "linux-arm64", "osx-x64", "osx-arm64", "win-x64", "win-arm64"}
# Apple mobile platforms: impeller.framework bundles of several slices (package-apple.py).
PLATFORMS = ["ios", "tvos", "maccatalyst"]


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def entry(path):
    return {"name": path.name, "size": path.stat().st_size, "sha256": sha256(path)}


def build_info(archive):
    root = archive.name[:-len(".tar.gz")]
    with tarfile.open(archive) as t:
        member = t.extractfile(f"{root}/build-info.json")
        if member is None:
            sys.exit(f"{archive.name} has no {root}/build-info.json")
        return json.load(member)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dir", type=pathlib.Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--resolved", type=pathlib.Path, help="JSON from fetch-flutter.py --resolve-only")
    parser.add_argument("--repository")
    parser.add_argument("--ref")
    parser.add_argument("--sha")
    parser.add_argument("--version")
    parser.add_argument("--kind", choices=["stable", "beta"])
    parser.add_argument("--patched", choices=["true", "false"])
    parser.add_argument("--partial", action="store_true", help="accept a subset of the RIDs (local builds only)")
    parser.add_argument("--allow-no-lto", action="append", default=[], metavar="RID",
                        help="accept an archive of this RID that was linked without LTO")
    args = parser.parse_args()

    source = json.loads(args.resolved.read_text()) if args.resolved else {}
    for key in ("repository", "ref", "sha", "version", "kind", "patched"):
        value = getattr(args, key)
        if value is not None:
            source[key] = value == "true" if key == "patched" else value
        if key not in source:
            sys.exit(f"--{key} (or --resolved) is required")
    if isinstance(source["patched"], str):
        source["patched"] = source["patched"] == "true"

    assets = {}
    infos = {}
    sums = []
    for rid in RIDS + PLATFORMS:
        base = args.dir / f"impeller-{args.tag}-{rid}"
        main_archive = pathlib.Path(f"{base}.tar.gz")
        symbols = pathlib.Path(f"{base}-symbols.tar.gz")
        tools = pathlib.Path(f"{base}-tools.tar.gz")
        if not main_archive.exists():
            if args.partial:
                continue
            sys.exit(f"Missing {main_archive.name}")
        if not symbols.exists():
            sys.exit(f"Missing {symbols.name}")
        if rid in DESKTOP and not tools.exists():
            sys.exit(f"Missing {tools.name}")
        asset = entry(main_archive)
        asset["symbols"] = entry(symbols)
        if rid in DESKTOP:
            asset["tools"] = entry(tools)
        assets[rid] = asset
        for a in (asset, asset["symbols"], asset.get("tools")):
            if a:
                sums.append(f"{a['sha256']}  {a['name']}")
        infos[rid] = build_info(main_archive)

    if not assets:
        sys.exit(f"No impeller-{args.tag}-*.tar.gz in {args.dir}")
    known = {f"impeller-{args.tag}-{rid}{suffix}.tar.gz" for rid in RIDS + PLATFORMS
             for suffix in ("", "-symbols", "-tools")}
    stray = sorted(p.name for p in args.dir.glob("*.tar.gz") if p.name not in known)
    if stray:
        sys.exit(f"Unexpected archives in {args.dir}: {', '.join(stray)}")

    errors = []
    first = next(iter(infos.values()))
    for rid, info in infos.items():
        if info["sha"] != source["sha"]:
            errors.append(f"{rid} was built from {info['sha']}, not {source['sha']}")
        if info["tag"] != args.tag:
            errors.append(f"{rid} has tag {info['tag']}, not {args.tag}")
        if info["rid"] != rid:
            errors.append(f"impeller-{args.tag}-{rid}.tar.gz holds a {info['rid']} build")
        for key in ("headerSha256", "impellerVersion"):
            if info[key] != first[key]:
                errors.append(f"{rid} {key} {info[key]} differs from {first['rid']} {first[key]}")
        # lto is null where the toolchain has no LTO (Windows).
        if info["toolchain"].get("lto") is False and rid not in args.allow_no_lto:
            errors.append(f"{rid} was linked without LTO (pass --allow-no-lto {rid} to accept it)")
    if errors:
        sys.exit("\n".join(errors))

    manifest = {
        "tag": args.tag,
        "repository": source["repository"],
        "ref": source["ref"],
        "sha": source["sha"],
        "version": source["version"],
        "kind": source["kind"],
        "patched": source["patched"],
        "impellerVersion": first["impellerVersion"],
        "headerSha256": first["headerSha256"],
        "assets": assets,
    }
    (args.dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.dir / "SHA256SUMS").write_text("\n".join(sums) + "\n")

    repo, sha = source["repository"], source["sha"]
    notes = [
        f"Impeller SDK of Flutter {source['version']} ({source['kind']}), "
        f"[{repo}@{sha[:10]}](https://github.com/{repo}/commit/{sha}), requested as `{source['ref']}`.",
        "",
        ("Built from an Avalonia patch branch." if source["patched"] else "Built from the upstream release tag, "
                                                                            "without patches."),
        f"`IMPELLER_VERSION` {first['impellerVersion']}. Release mode, "
        + ("with LTO" if all(i["toolchain"].get("lto") is not False for i in infos.values()) else "LTO off for: "
           + ", ".join(r for r, i in infos.items() if i["toolchain"].get("lto") is False))
        + (" (Windows has no LTO in Flutter's toolchain)." if any(i["toolchain"].get("lto") is None
                                                                  for i in infos.values()) else "."),
    ]
    if any(rid in assets for rid in PLATFORMS):
        notes += ["",
                  "The ios, tvos and maccatalyst archives hold `impeller.framework` per slice (device, simulator, "
                  "Mac Catalyst): iOS and Mac Catalyst 15.0 or newer, tvOS 16.0 or newer."]
    notes += [
        "",
        "| RID or platform | Library | Symbols | Tools |",
        "|---|---|---|---|",
    ]
    for rid, a in assets.items():
        tools = f"{a['tools']['size'] / 1e6:.1f} MB" if "tools" in a else ""
        notes.append(f"| {rid} | {a['size'] / 1e6:.1f} MB | {a['symbols']['size'] / 1e6:.1f} MB | {tools} |")
    (args.dir / "release-notes.md").write_text("\n".join(notes) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
