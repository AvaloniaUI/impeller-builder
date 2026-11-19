#!/usr/bin/env python3
"""Resolves a Flutter ref and prepares a checkout of it in ./flutter for an Impeller SDK build.

  fetch-flutter.py --ref avalonia/3.50.0 --resolve-only          print the resolved version as JSON (no checkout)
  fetch-flutter.py --ref 3.50.0 --rid linux-x64                  check out ./flutter and gclient sync for that RID
  fetch-flutter.py --repository R --sha S --rid linux-x64        check out a resolved commit (CI build jobs)

--ref is an avalonia/<flutter tag> branch of --repository (default AvaloniaUI/flutter), an upstream release tag of
flutter/flutter (3.50.0, 3.50.0-0.4.pre), or the full commit SHA of either. The version is the Flutter tag.
"""

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FLUTTER = ROOT / "flutter"
TOOLCHAINS = ROOT / "toolchains"
UPSTREAM = "flutter/flutter"
DEFAULT_REPOSITORY = "AvaloniaUI/flutter"
BRANCH_PREFIX = "avalonia/"

DEPOT_TOOLS_URL = "https://chromium.googlesource.com/chromium/tools/depot_tools.git"
DEPOT_TOOLS_COMMIT = "40487ee6a1fa701d19c97011b1c770a589bcb396"

STABLE = re.compile(r"^\d+\.\d+\.\d+$")
BETA = re.compile(r"^\d+\.\d+\.0-\d+\.\d+\.pre$")

RIDS = ["linux-x64", "linux-arm64", "osx-x64", "osx-arm64", "win-x64", "win-arm64",
        "android-arm64", "android-arm", "android-x64"]

# gclient custom_vars per target OS. Everything the sdk target does not need is off.
COMMON_VARS = {"download_esbuild": False, "setup_githooks": False, "download_fuchsia_deps": False,
               "download_android_deps": False}
OS_VARS = {"linux": {}, "android": {"download_android_deps": True}, "osx": {}, "win": {}}


def run(args, cwd=None, env=None, capture=False):
    print("+ " + " ".join(str(a) for a in args), file=sys.stderr, flush=True)
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else None


def ls_remote(repository, *patterns):
    out = run(["git", "ls-remote", f"https://github.com/{repository}.git", *patterns], capture=True)
    refs = {}
    for line in out.splitlines():
        if line:
            sha, name = line.split("\t")
            refs[name] = sha
    return refs


def tag_kind(tag):
    if STABLE.match(tag):
        return "stable"
    if BETA.match(tag):
        return "beta"
    return None


def upstream_tag_sha(tag):
    refs = ls_remote(UPSTREAM, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def info(repository, ref, sha, version, patched):
    return {"repository": repository, "ref": ref, "sha": sha, "version": version, "kind": tag_kind(version),
            "patched": patched, "tag": f"{version}-{sha[:10]}"}


def resolve(repository, ref):
    accepted = (f"accepted refs: {BRANCH_PREFIX}<flutter tag> (a branch of {repository}), a release tag of "
                f"{UPSTREAM} (3.50.0 or 3.50.0-0.4.pre), or the full commit SHA of either")
    if ref.startswith(BRANCH_PREFIX):
        version = ref[len(BRANCH_PREFIX):]
        if not tag_kind(version):
            sys.exit(f"{ref}: {version} is not a Flutter release tag; {accepted}")
        sha = ls_remote(repository, f"refs/heads/{ref}").get(f"refs/heads/{ref}")
        if not sha:
            sys.exit(f"{ref} is not a branch of {repository}")
        if not upstream_tag_sha(version):
            sys.exit(f"{ref}: {version} is not a tag of {UPSTREAM}")
        return info(repository, ref, sha, version, True)

    if tag_kind(ref):
        sha = upstream_tag_sha(ref)
        if not sha:
            sys.exit(f"{ref} is not a tag of {UPSTREAM}")
        return info(UPSTREAM, ref, sha, ref, False)

    if re.fullmatch(r"[0-9a-f]{40}", ref):
        # A patch branch wins over an upstream tag at the same commit (a branch without patches yet).
        heads = ls_remote(repository, f"refs/heads/{BRANCH_PREFIX}*")
        branches = [name[len("refs/heads/"):] for name, sha in heads.items()
                    if sha == ref and tag_kind(name[len("refs/heads/") + len(BRANCH_PREFIX):] or "")]
        if len(branches) > 1:
            sys.exit(f"{ref} is the head of several branches: {', '.join(sorted(branches))}; pass the branch")
        if branches:
            return info(repository, branches[0], ref, branches[0][len(BRANCH_PREFIX):], True)
        tags = ls_remote(UPSTREAM, "refs/tags/*")
        peeled = {}
        for name, sha in tags.items():
            tag = name[len("refs/tags/"):]
            if tag.endswith("^{}"):
                peeled[tag[:-3]] = sha
            else:
                peeled.setdefault(tag, sha)
        matches = sorted(t for t, sha in peeled.items() if sha == ref and tag_kind(t))
        if not matches:
            sys.exit(f"{ref} is neither the head of a {BRANCH_PREFIX}* branch of {repository} nor a release tag "
                     f"of {UPSTREAM}; {accepted}")
        # A stable tag wins over a beta tag at the same commit.
        matches.sort(key=lambda t: 0 if STABLE.match(t) else 1)
        return info(UPSTREAM, matches[0], ref, matches[0], False)

    sys.exit(f"Cannot resolve {ref}; {accepted}")


def depot_tools():
    """depot_tools from $DEPOT_TOOLS, or a clone pinned to DEPOT_TOOLS_COMMIT in toolchains/."""
    existing = os.environ.get("DEPOT_TOOLS")
    if existing:
        return pathlib.Path(existing)
    path = TOOLCHAINS / "depot_tools"
    if (path / ".git").exists():
        head = run(["git", "rev-parse", "HEAD"], cwd=path, capture=True).strip()
        if head == DEPOT_TOOLS_COMMIT:
            return path
        shutil.rmtree(path)
    path.mkdir(parents=True)
    run(["git", "init", "-q"], cwd=path)
    run(["git", "fetch", "-q", "--depth", "1", DEPOT_TOOLS_URL, DEPOT_TOOLS_COMMIT], cwd=path)
    run(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=path)
    return path


def depot_env(path):
    env = dict(os.environ)
    env["PATH"] = str(path) + os.pathsep + env.get("PATH", "")
    env["DEPOT_TOOLS_UPDATE"] = "0"
    env["DEPOT_TOOLS_METRICS"] = "0"
    if os.name == "nt":
        env["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"
    return env


def checkout(repository, sha):
    if (FLUTTER / ".git").exists():
        head = run(["git", "rev-parse", "HEAD"], cwd=FLUTTER, capture=True).strip()
        if head == sha:
            return
        # Keep the synced dependencies: only the Flutter tree moves to the new commit.
        run(["git", "fetch", "-q", "--depth", "1", f"https://github.com/{repository}.git", sha], cwd=FLUTTER)
        run(["git", "checkout", "-q", "--force", "--detach", "FETCH_HEAD"], cwd=FLUTTER)
        return
    FLUTTER.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q"], cwd=FLUTTER)
    run(["git", "remote", "add", "origin", f"https://github.com/{repository}.git"], cwd=FLUTTER)
    run(["git", "fetch", "-q", "--depth", "1", "origin", sha], cwd=FLUTTER)
    run(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=FLUTTER)


def custom_vars(rid):
    target_os = rid.split("-")[0]
    return dict(COMMON_VARS, **OS_VARS[target_os])


def write_gclient(repository, rid):
    def py(value):
        return "True" if value is True else "False" if value is False else repr(value)

    variables = "".join(f"      {k!r}: {py(v)},\n" for k, v in sorted(custom_vars(rid).items()))
    text = (
        "# Written by impeller-builder scripts/fetch-flutter.py\n"
        "solutions = [\n"
        "  {\n"
        "    'custom_deps': {},\n"
        "    'deps_file': 'DEPS',\n"
        "    'managed': False,\n"
        "    'name': '.',\n"
        "    'safesync_url': '',\n"
        f"    'url': 'https://github.com/{repository}.git',\n"
        "    'custom_vars': {\n"
        f"{variables}"
        "    },\n"
        "  },\n"
        "]\n"
    )
    (FLUTTER / ".gclient").write_text(text)


def sync(repository, sha, rid):
    tools = depot_tools()
    env = depot_env(tools)
    write_gclient(repository, rid)
    gclient = "gclient.bat" if os.name == "nt" else "gclient"
    gclient = str(tools / gclient)
    # --revision pins the root solution to the checked-out commit, so gclient does not move it.
    run([gclient, "sync", "-D", "--no-history", "--shallow", "--force", "--reset", "--revision", f".@{sha}"],
        cwd=FLUTTER, env=env)


def windows_git_config():
    """Long paths and no line ending conversion for every git call of this script and gclient, without touching the
    user's git configuration."""
    if os.name != "nt":
        return
    settings = {"core.longpaths": "true", "core.autocrlf": "false"}
    base = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    for i, (key, value) in enumerate(settings.items()):
        os.environ[f"GIT_CONFIG_KEY_{base + i}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{base + i}"] = value
    os.environ["GIT_CONFIG_COUNT"] = str(base + len(settings))


def main():
    windows_git_config()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY,
                        help=f"GitHub repository of the {BRANCH_PREFIX}* branches (default {DEFAULT_REPOSITORY})")
    parser.add_argument("--ref", help="avalonia/<tag> branch, upstream release tag, or the SHA of either")
    parser.add_argument("--sha", help="check out this commit of --repository without resolving a ref")
    parser.add_argument("--rid", choices=RIDS, help="check out and sync the dependencies of a build for this RID")
    parser.add_argument("--resolve-only", action="store_true", help="print the resolved version without a checkout")
    parser.add_argument("--github-output", action="store_true", help="also write the result to $GITHUB_OUTPUT")
    args = parser.parse_args()

    if args.sha:
        if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
            sys.exit("--sha takes a full commit SHA")
        if not args.rid:
            sys.exit("--sha needs --rid")
        result = {"repository": args.repository, "sha": args.sha}
    elif args.ref:
        result = resolve(args.repository, args.ref)
    else:
        sys.exit("pass --ref, or --repository and --sha")

    if not args.resolve_only:
        if not args.rid:
            sys.exit("pass --rid to check out, or --resolve-only")
        checkout(result["repository"], result["sha"])
        sync(result["repository"], result["sha"], args.rid)

    print(json.dumps(result, indent=2))
    if args.github_output and "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            for key, value in result.items():
                out.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")


if __name__ == "__main__":
    main()
