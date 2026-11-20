#!/usr/bin/env python3
"""Fetch the published source and its nine exact dependency revisions.

Only public, explicitly pinned GitHub URLs are accepted. Existing checkouts are
reused only when their tracked files and origins match; no reset or clean is used.
"""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time

SOURCE_URL = "https://github.com/CocoSgt/riemann-hypothesis-lean.git"
SOURCE_REV = "7cbf7a0c32fca1badf05931879a37c3f8a2b03b4"
SOURCE_TREE = "b41b516b1f7baee4d83ced235ecc71fd43c97695"
EXPECTED = {
    "mathlib": ("https://github.com/leanprover-community/mathlib4", "0df444a360eaa60ab8c11dca51a86af692955474"),
    "plausible": ("https://github.com/leanprover-community/plausible", "b7eb3304aeae834b12dda98993a37f6a41f6f0bb"),
    "LeanSearchClient": ("https://github.com/leanprover-community/LeanSearchClient", "5f4d51b81cbd3f6b32b156bfad9056621a040404"),
    "importGraph": ("https://github.com/leanprover-community/import-graph", "16f02aa7642864af59f1ff0e384a015994db9118"),
    "proofwidgets": ("https://github.com/leanprover-community/ProofWidgets4", "4be2e3d5087eeb272cf5a8853b8f9dd025ef5957"),
    "aesop": ("https://github.com/leanprover-community/aesop", "3448c0bcc5ce01b2d1546e483ec3620e32df3d0e"),
    "Qq": ("https://github.com/leanprover-community/quote4", "92c15be17b7caf78c2ad767ec40f89052d908d81"),
    "batteries": ("https://github.com/leanprover-community/batteries", "4488d40d070b9700d4d5a6aa342f0d40c31b2a2d"),
    "Cli": ("https://github.com/leanprover/lean4-cli", "6130a47896ce867c6a4a55373441e59e565bad0f"),
}


def git(path, *args, capture=False, check=True):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never",
         "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=90", "-C", str(path), *args],
        check=check, text=True, stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def output(path, *args):
    return git(path, *args, capture=True).stdout.strip()


def verify(path, url, revision):
    if path.is_symlink() or not (path / ".git").is_dir():
        raise RuntimeError(f"Expected a normal Git checkout at {path}")
    origin = output(path, "remote", "get-url", "origin")
    if origin.removesuffix(".git") != url.removesuffix(".git"):
        raise RuntimeError(f"Unexpected origin in {path.name}; refusing to use it")
    head = output(path, "rev-parse", "HEAD")
    if head != revision:
        raise RuntimeError(f"Wrong HEAD in {path.name}: {head}; expected {revision}")
    if output(path, "status", "--porcelain=v1", "--untracked-files=no"):
        raise RuntimeError(f"Tracked files changed in {path}; no reset will be performed")
    git(path, "fsck", "--connectivity-only", "--no-dangling")
    return {"name": path.name, "url": url, "revision": head,
            "tree": output(path, "rev-parse", "HEAD^{tree}"), "tracked_files_clean": True}


def fetch_exact(path, url, revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("Revision must be a full commit SHA")
    if path.is_symlink():
        raise RuntimeError(f"Refusing a symlink checkout: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        if any(path.iterdir()):
            raise RuntimeError(f"Refusing a nonempty non-Git destination: {path}")
        git(path, "init", "--quiet")
        git(path, "remote", "add", "origin", url)
    if output(path, "remote", "get-url", "origin").removesuffix(".git") != url.removesuffix(".git"):
        raise RuntimeError(f"Origin mismatch in {path.name}")
    head = git(path, "rev-parse", "--verify", "HEAD", capture=True, check=False)
    if head.returncode == 0:
        return verify(path, url, revision)
    # An interrupted initial fetch may leave .git but no HEAD; retry it in place.
    for attempt in range(1, 4):
        print(f"Fetching {path.name} at {revision} (attempt {attempt})", flush=True)
        result = git(path, "fetch", "--no-tags", "--depth=1", "origin", revision, check=False)
        if result.returncode == 0:
            break
        if attempt == 3:
            raise RuntimeError(f"Could not fetch pinned revision for {path.name}")
        time.sleep(5 * attempt)
    if output(path, "rev-parse", "FETCH_HEAD") != revision:
        raise RuntimeError(f"Fetched revision mismatch for {path.name}")
    git(path, "checkout", "--quiet", "--detach", revision)
    return verify(path, url, revision)


def source_record(project, allow_fetch):
    project.parent.mkdir(parents=True, exist_ok=True)
    with (project.parent / f"{project.name}.prepare.lock").open("a") as lock:
        print("Waiting for the source preparation lock", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = (fetch_exact if allow_fetch else verify)(project, SOURCE_URL, SOURCE_REV)
        if output(project, "rev-parse", "HEAD:src") != SOURCE_TREE:
            raise RuntimeError("Published src tree identity mismatch")
        record["src_tree"] = SOURCE_TREE
        return record


def dependencies(project):
    manifest = json.loads((project / "lake-manifest.json").read_text())
    if manifest.get("packagesDir") != ".lake/packages":
        raise RuntimeError("Unexpected package directory")
    packages = manifest.get("packages", [])
    if len(packages) != len(EXPECTED):
        raise RuntimeError("Manifest does not contain exactly nine dependencies")
    seen = set()
    for package in packages:
        name = package.get("name")
        if name not in EXPECTED or name in seen:
            raise RuntimeError("Unexpected or duplicated package name in manifest")
        seen.add(name)
        if package.get("type") != "git" or package.get("subDir") is not None:
            raise RuntimeError(f"Unexpected package type/subDir: {name}")
        if (package.get("url"), package.get("rev")) != EXPECTED[name]:
            raise RuntimeError(f"Manifest pin or upstream URL changed: {name}")
        yield name, *EXPECTED[name]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--phase", choices=("source", "dependencies", "verify"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = args.project.absolute()
    records = [source_record(project, args.phase == "source")]
    if args.phase != "source":
        # source_record has released the source lock before taking this lock.
        # The optional fast prefetcher may hold it while awaiting source setup.
        with (project.parent / "dependencies.prepare.lock").open("a") as lock:
            print("Waiting for the dependency preparation lock", flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
            package_dir = project / ".lake/packages"
            if package_dir.is_symlink() or (project / ".lake").is_symlink():
                raise RuntimeError("Refusing symlink package storage")
            for name, url, rev in dependencies(project):
                action = fetch_exact if args.phase == "dependencies" else verify
                records.append(action(package_dir / name, url, rev))
    result = {"phase": args.phase, "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "source_commit": SOURCE_REV, "repositories": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    os.replace(temporary, args.output)
    print(json.dumps({"phase": args.phase, "verified_repositories": len(records), "status": "ok"}), flush=True)


if __name__ == "__main__":
    main()
