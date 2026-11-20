#!/usr/bin/env python3
"""Prepare the unchanged Git checkout from author archive plus Git metadata.

The fourteen archive parts are fetched directly. The exact upstream commit
bytes and pinned mirror blobs are verified by their original Git object IDs.
The shared lock makes bootstrap wait. No dependency or Lean build is run here.
The complete published Git tree and object graph are the final gates.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

sys.dont_write_bytecode = True
from prepare_dependencies import SOURCE_URL, SOURCE_REV, SOURCE_TREE, verify

ROOT_TREE = "02cfa4b3810fc22f6d43b317409071f380a5f1b4"
ARCHIVE_BYTES = 239275554
ARCHIVE_SHA256 = "9631b4f020199e580ba86178cb56c6e9b09f690c2369a80dc4334aa93f49c7fc"


def git_object_sha(kind, payload):
    return hashlib.sha1(kind.encode() + b" " + str(len(payload)).encode() + b"\0" + payload).hexdigest()


def install_original_commit(worktree):
    """Restore exact upstream commit bytes, never generate a replacement commit."""
    metadata = json.loads(Path(__file__).with_name("source_metadata.json").read_text())
    if (metadata["source_commit"], metadata["root_tree"], metadata["src_tree"]) != (SOURCE_REV, ROOT_TREE, SOURCE_TREE):
        raise RuntimeError("Unexpected source metadata pin")
    raw = base64.b64decode(metadata["raw_commit_base64"], validate=True)
    if git_object_sha("commit", raw) != SOURCE_REV or raw.splitlines()[0] != ("tree " + ROOT_TREE).encode():
        raise RuntimeError("Original commit bytes do not hash to the published commit")
    result = subprocess.run(["git", "-C", str(worktree), "hash-object", "-t", "commit", "-w", "--stdin"],
                            input=raw, stdout=subprocess.PIPE, check=True, env=git_env(no_lazy=True))
    if result.stdout.decode().strip() != SOURCE_REV:
        raise RuntimeError("Git rejected exact commit identity")
    # This is the same explicit history boundary as a depth-one fetch. Parent
    # history is not claimed to be present; the complete target tree is checked.
    (worktree / ".git/shallow").write_text(SOURCE_REV + "\n")
    return metadata


def fetch_mirror_files(worktree, metadata):
    files = metadata["mirror_files"]
    if set(files) != {"README.md", ".gitignore"}:
        raise RuntimeError("Unexpected mirror-only files")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for name, info in files.items():
        if info["mode"] != "100644":
            raise RuntimeError("Unexpected mirror file mode")
        raw_url = f"https://raw.githubusercontent.com/CocoSgt/riemann-hypothesis-lean/{SOURCE_REV}/{name}"
        api_url = f"https://api.github.com/repos/CocoSgt/riemann-hypothesis-lean/git/blobs/{info['blob_sha']}"
        payload = None
        for attempt in range(3):
            for url in (raw_url, api_url):
                try:
                    request = urllib.request.Request(url, headers={"User-Agent": "RH-verification"})
                    with opener.open(request, timeout=25) as response:
                        data = response.read(2 * 1024 * 1024)
                    candidate = (base64.b64decode(json.loads(data)["content"])
                                 if url == api_url else data)
                    if git_object_sha("blob", candidate) != info["blob_sha"]:
                        raise RuntimeError("Mirror file Git blob mismatch")
                    payload = candidate
                    break
                except (OSError, ValueError, KeyError, RuntimeError):
                    continue
            if payload is not None:
                break
            time.sleep(3)
        if payload is None:
            raise RuntimeError(f"Could not retrieve the verified upstream blob for {name}")
        with (worktree / name).open("xb") as output:
            output.write(payload)


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def git_env(no_lazy=False):
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    if no_lazy:
        env["GIT_NO_LAZY_FETCH"] = "1"
    else:
        env.pop("GIT_NO_LAZY_FETCH", None)
    return env


def git(path, *args, capture=False, no_lazy=False):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false",
         "-c", "core.attributesFile=/dev/null", "-c", "protocol.version=2",
         "-C", str(path), *args], env=git_env(no_lazy), check=True, text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else None


def archive_download(downloads, workers):
    archive = downloads / "lean-author-20261120.tar.gz"
    if archive.exists():
        if archive.stat().st_size == ARCHIVE_BYTES and digest(archive) == ARCHIVE_SHA256:
            print("Reusing the verified complete author archive", flush=True)
            return archive
        raise RuntimeError("Existing author archive failed integrity verification; refusing to overwrite it")
    direct_env = os.environ.copy()
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        direct_env.pop(name, None)

    def fetch_part(number):
        part = downloads / f"lean.tar.gz.part{number:02d}"
        if not part.exists():
            partial = part.with_suffix(part.suffix + ".partial")
            print(f"{utc()} downloading author part {number:02d}", flush=True)
            # Codeload/author endpoints need not support Range; start a failed
            # partial over, while already completed parts remain reusable.
            subprocess.run([
                "curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
                "--retry", "6", "--retry-all-errors", "--retry-delay", "5",
                "--connect-timeout", "30", "--max-time", "1800", "--silent", "--show-error",
                "--output", str(partial), f"https://alpo.ge/lean.tar.gz.part{number:02d}",
            ], env=direct_env, check=True)
            if not 0 < partial.stat().st_size <= ARCHIVE_BYTES:
                raise RuntimeError(f"Invalid length for part {number}")
            os.replace(partial, part)
        print(f"{utc()} completed author part {number:02d}: {part.stat().st_size} bytes", flush=True)
        return part

    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = [pool.submit(fetch_part, number) for number in range(1, 15)]
        for job in as_completed(jobs):
            job.result()
    combined = archive.with_suffix(archive.suffix + ".partial")
    with combined.open("wb") as output:
        for number in range(1, 15):
            with (downloads / f"lean.tar.gz.part{number:02d}").open("rb") as part:
                shutil.copyfileobj(part, output, 8 * 1024 * 1024)
    if combined.stat().st_size != ARCHIVE_BYTES or digest(combined) != ARCHIVE_SHA256:
        raise RuntimeError("Joined author archive SHA-256/size does not match the published snapshot")
    os.replace(combined, archive)
    return archive


def extract_verified_archive(archive, worktree):
    count, total = 0, 0
    # Stream and write regular files manually: no symlinks, hardlinks, devices,
    # path traversal, duplicate overwrite, or .git members are allowed.
    with tarfile.open(archive, "r|gz") as bundle:
        for member in bundle:
            name = member.name
            parts = PurePosixPath(name).parts
            if not parts or parts[0] != "lean" or name.startswith("/") or ".." in parts or ".git" in parts:
                raise RuntimeError(f"Unsafe/unexpected archive member: {name!r}")
            relative = parts[1:]
            if not relative:
                if not member.isdir():
                    raise RuntimeError("Archive root is not a directory")
                continue
            if not (member.isdir() or member.isreg()):
                raise RuntimeError(f"Archive member is not a regular file/directory: {name!r}")
            target = worktree.joinpath(*relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if relative[0] not in {"src", "lakefile.toml", "lake-manifest.json", "lean-toolchain"}:
                raise RuntimeError(f"Unexpected file outside the published source: {name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = bundle.extractfile(member)
            if stream is None:
                raise RuntimeError(f"Missing archive payload: {name}")
            with stream, target.open("xb") as output:
                shutil.copyfileobj(stream, output, 1024 * 1024)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
            count += 1
            total += member.size
            if count % 25000 == 0:
                print(f"{utc()} extracted {count} files", flush=True)
    print(f"Extracted {count} regular files ({total} bytes)", flush=True)
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("/root/autodl-tmp/rh-check"))
    parser.add_argument("--workers", type=int, default=14)
    parser.add_argument("--detach", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.workers <= 14:
        parser.error("--workers must be between 1 and 14")
    base = args.base.absolute()
    if args.detach:
        audit = base / "audit"
        audit.mkdir(parents=True, exist_ok=True)
        with (audit / "source-prefetch.log").open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-u", str(Path(__file__).resolve()),
                 "--base", str(base), "--workers", str(args.workers)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        (audit / "source-prefetch.pid").write_text(str(child.pid) + "\n")
        print(f"Source prefetch detached; PID {child.pid}; log {audit / 'source-prefetch.log'}")
        return
    source = base / "source"
    downloads = base / "downloads/author-parts"
    downloads.mkdir(parents=True, exist_ok=True)
    run = base / "source-prefetch-runs" / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}")
    run.mkdir(parents=True)
    (base / "source-prefetch-current-run.txt").write_text(str(run) + "\n")

    def stage(name):
        (run / "status.txt").write_text("running:" + name + "\n")
        print(f"{utc()} {name}", flush=True)

    try:
        with (base / "source.prepare.lock").open("a") as lock:
            stage("waiting-source-lock")
            fcntl.flock(lock, fcntl.LOCK_EX)
            if source.exists():
                record = verify(source, SOURCE_URL, SOURCE_REV)
                if git(source, "rev-parse", "HEAD:src", capture=True) != SOURCE_TREE:
                    raise RuntimeError("Existing source src tree mismatch")
                if record["tree"] != ROOT_TREE:
                    raise RuntimeError("Existing source root tree mismatch")
                (run / "result.json").write_text(json.dumps(record, indent=2) + "\n")
                (run / "status.txt").write_text("complete:existing-source-verified\n")
                return

            stage("author-archive-download")
            archive = archive_download(downloads, args.workers)
            stage("exact-commit-snapshot")
            worktree = base / f"source-prefetch-staging-{os.getpid()}"
            worktree.mkdir()  # Never reuse a partially hydrated or unknown staging directory.
            git(worktree, "init", "--quiet", "--object-format=sha1")
            git(worktree, "remote", "add", "origin", SOURCE_URL)
            metadata = install_original_commit(worktree)

            stage("archive-extraction")
            files = extract_verified_archive(archive, worktree)
            stage("mirror-blob-fetch")
            fetch_mirror_files(worktree, metadata)

            stage("hydrate-and-check-tree")
            # HEAD is still unborn, so add hashes the extracted worktree against
            # an empty/small index rather than asking for promised blob contents.
            git(worktree, "add", "--force", "--all", "--", ".", no_lazy=True)
            actual_tree = git(worktree, "write-tree", capture=True, no_lazy=True)
            if actual_tree != ROOT_TREE:
                raise RuntimeError(f"Hydrated tree mismatch: {actual_tree}, expected {ROOT_TREE}")
            git(worktree, "checkout", "--quiet", "--detach", SOURCE_REV, no_lazy=True)
            missing = git(worktree, "rev-list", "--objects", "--missing=print", "HEAD", capture=True, no_lazy=True)
            if any(line.startswith("?") for line in missing.splitlines()):
                raise RuntimeError("Hydrated repository still has missing Git objects")
            git(worktree, "fsck", "--full", "--no-dangling", no_lazy=True)
            record = verify(worktree, SOURCE_URL, SOURCE_REV)
            if git(worktree, "rev-parse", "HEAD:src", capture=True, no_lazy=True) != SOURCE_TREE:
                raise RuntimeError("Hydrated src tree mismatch")
            if source.exists():
                raise RuntimeError("Source destination appeared while locked; refusing to overwrite")
            os.rename(worktree, source)
            record.update(path=str(source), src_tree=SOURCE_TREE, archive_sha256=ARCHIVE_SHA256,
                          archive_bytes=ARCHIVE_BYTES, extracted_files=files, finished_utc=utc(),
                          retrieval="verified author archive, exact original commit bytes, pinned upstream mirror blobs")
            (run / "result.json").write_text(json.dumps(record, indent=2) + "\n")
            (run / "status.txt").write_text("complete:archive,git-objects,root-tree,source-unchanged\n")
            print(f"{utc()} verified source installed at {source}", flush=True)
    except BaseException:
        with (run / "status.txt").open("a") as output:
            output.write("failed\n")
        raise


if __name__ == "__main__":
    main()
