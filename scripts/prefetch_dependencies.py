#!/usr/bin/env python3
"""Hydrate pinned dependency checkouts from official codeload archives.

Original commit objects are included as verified public metadata; archive
downloads go directly to codeload.github.com. No server-side Git fetch is needed.
A full root-tree match and object check gate installation.
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
import posixpath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

sys.dont_write_bytecode = True
from prepare_dependencies import EXPECTED, SOURCE_REV, dependencies, source_record, verify


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def git(path, *args, capture=False, log=None, input_bytes=None):
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_NO_LAZY_FETCH="1")
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false",
         "-c", "core.attributesFile=/dev/null", "-c", "protocol.version=2",
         "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=90",
         "-C", str(path), *args], env=env, check=True,
        input=input_bytes, text=input_bytes is None,
        stdout=subprocess.PIPE if capture else log, stderr=log,
    )
    if not capture:
        return None
    output = result.stdout.decode("ascii") if isinstance(result.stdout, bytes) else result.stdout
    return output.strip()


def load_metadata(path):
    document = json.loads(path.read_text())
    records = document.get("repositories", {})
    if document.get("schema") != 1 or set(records) != set(EXPECTED):
        raise RuntimeError("Dependency metadata must describe exactly the nine pinned repositories")
    result = {}
    for name, (url, revision) in EXPECTED.items():
        record = records[name]
        if record.get("url") != url or record.get("revision") != revision:
            raise RuntimeError(f"Dependency metadata identity mismatch: {name}")
        raw = base64.b64decode(record["commit_base64"], validate=True)
        object_id = hashlib.sha1(b"commit " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        if object_id != revision or hashlib.sha256(raw).hexdigest() != record.get("commit_sha256"):
            raise RuntimeError(f"Original commit bytes do not hash to the manifest pin: {name}")
        tree = record.get("tree", "")
        if (len(tree) != 40 or any(c not in "0123456789abcdef" for c in tree)
                or raw.splitlines()[0] != b"tree " + tree.encode("ascii")):
            raise RuntimeError(f"Commit root tree mismatch in metadata: {name}")
        result[name] = dict(record, raw_commit=raw)
    return result


def archive_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract_archive(archive, worktree, prefix):
    symlinks = []
    count = 0
    with tarfile.open(archive, "r|gz") as bundle:
        for member in bundle:
            parts = PurePosixPath(member.name).parts
            if (not parts or parts[0] != prefix or member.name.startswith("/")
                    or ".." in parts or ".git" in parts):
                raise RuntimeError(f"Unsafe archive path: {member.name!r}")
            relative = parts[1:]
            if not relative:
                if not member.isdir():
                    raise RuntimeError("Archive root is not a directory")
                continue
            target = worktree.joinpath(*relative)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isreg():
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = bundle.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"No payload for {member.name}")
                with stream, target.open("xb") as output:
                    shutil.copyfileobj(stream, output, 1024 * 1024)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
                count += 1
            elif member.issym():
                link = member.linkname
                resolved = posixpath.normpath(posixpath.join(*relative[:-1], link))
                if (link.startswith("/") or resolved == ".." or resolved.startswith("../")
                        or ".git" in PurePosixPath(resolved).parts):
                    raise RuntimeError(f"Unsafe archive symlink: {member.name!r}")
                symlinks.append((target, link))
            else:
                raise RuntimeError(f"Special file/hardlink in archive: {member.name!r}")
    # Create symlinks last, so extraction never writes through an archive link.
    for target, link in symlinks:
        for parent in target.parents:
            if parent == worktree:
                break
            if parent.is_symlink():
                raise RuntimeError(f"Archive symlink parent is another link: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(link)
    # Resolve the complete link graph as well as the lexical targets above.
    # This rejects links whose otherwise harmless components traverse another
    # archive link before '..', and rejects cycles without following them.
    root = worktree.resolve()
    for target, _ in symlinks:
        resolved = target.resolve()
        if not resolved.is_relative_to(root) or ".git" in resolved.relative_to(root).parts:
            raise RuntimeError(f"Archive symlink resolves outside its worktree: {target}")
    return count + len(symlinks)


def prepare_one(base, source, run, name, url, revision, metadata):
    destination = source / ".lake/packages" / name
    log_path = run / f"{name}.log"
    (run / f"{name}.start-utc.txt").write_text(utc() + "\n")
    print(f"{utc()} starting {name}", flush=True)
    try:
        if destination.is_symlink():
            raise RuntimeError(f"Refusing symlink dependency checkout: {destination}")
        if destination.exists():
            record = verify(destination, url, revision)
            record["reused"] = True
        else:
            # A prior incomplete directory is never reused or overwritten.
            staging = Path(tempfile.mkdtemp(prefix=f"dependency-prefetch-staging-{name}-", dir=base))
            with log_path.open("w", buffering=1) as log:
                log.write(f"{utc()} importing verified original commit object for {url}@{revision}\n")
                git(staging, "init", "--quiet", log=log)
                git(staging, "remote", "add", "origin", url, log=log)
                original = metadata[name]
                imported = git(staging, "hash-object", "-t", "commit", "-w", "--stdin",
                               input_bytes=original["raw_commit"], capture=True, log=log)
                if imported != revision:
                    raise RuntimeError(f"Imported original commit identity mismatch: {name}")
                # A shallow boundary omits only the commit's history, just as a
                # depth-one fetch does. Every object in its source tree must be
                # reconstructed below and pass the full object checks.
                (staging / ".git/shallow").write_text(revision + "\n")
                expected_tree = original["tree"]

                repo_path = url.removeprefix("https://github.com/").removesuffix(".git")
                archive_url = f"https://codeload.github.com/{repo_path}/tar.gz/{revision}"
                archives = base / "downloads/dependency-archives"
                archives.mkdir(parents=True, exist_ok=True)
                archive = archives / f"{name}-{revision}.tar.gz"
                if not archive.exists():
                    direct_env = os.environ.copy()
                    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                        direct_env.pop(key, None)
                    partial = archive.with_suffix(archive.suffix + ".partial")
                    log.write(f"{utc()} downloading {archive_url} directly\n")
                    subprocess.run([
                        "curl", "--fail", "--location", "--proto", "=https", "--proto-redir", "=https",
                        "--retry", "6", "--retry-all-errors", "--retry-delay", "5",
                        "--connect-timeout", "30", "--max-time", "1800", "--silent", "--show-error",
                        "--output", str(partial), archive_url,
                    ], env=direct_env, check=True, stdout=log, stderr=log)
                    if partial.stat().st_size == 0:
                        raise RuntimeError(f"Empty archive: {name}")
                    os.replace(partial, archive)
                log.write(f"{utc()} extracting archive and hydrating Git objects\n")
                files = extract_archive(archive, staging, f"{repo_path.split('/')[-1]}-{revision}")
                git(staging, "add", "--force", "--all", "--", ".", log=log)
                actual_tree = git(staging, "write-tree", capture=True, log=log)
                if actual_tree != expected_tree:
                    raise RuntimeError(f"Archive tree mismatch for {name}: {actual_tree}, expected {expected_tree}")
                git(staging, "checkout", "--quiet", "--detach", revision, log=log)
                objects = git(staging, "rev-list", "--objects", "--missing=print", "HEAD", capture=True, log=log)
                if any(line.startswith("?") for line in objects.splitlines()):
                    raise RuntimeError(f"Missing hydrated objects: {name}")
                git(staging, "fsck", "--full", "--no-dangling", log=log)
                record = verify(staging, url, revision)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    raise RuntimeError(f"Destination appeared unexpectedly: {destination}")
                os.rename(staging, destination)
                record.update(name=name, path=str(destination), archive_url=archive_url,
                              archive_sha256=archive_sha(archive), archive_bytes=archive.stat().st_size,
                              extracted_files=files, reused=False,
                              commit_object_sha256=original["commit_sha256"],
                              commit_import="original-bytes-verified-against-manifest-pin")
        record["finished_utc"] = utc()
        (run / f"{name}.json").write_text(json.dumps(record, indent=2) + "\n")
        (run / f"{name}.exit-code.txt").write_text("0\n")
        print(f"{utc()} verified {name} at {revision}", flush=True)
        return record
    except BaseException as exc:
        (run / f"{name}.exit-code.txt").write_text("1\n")
        print(f"{utc()} FAILED {name}: {exc}", flush=True)
        raise
    finally:
        (run / f"{name}.end-utc.txt").write_text(utc() + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("/root/autodl-tmp/rh-check"))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--source-wait-seconds", type=int, default=1800)
    parser.add_argument("--metadata", type=Path,
                        default=Path(__file__).resolve().with_name("dependency_metadata.json"))
    parser.add_argument("--detach", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.workers <= 3 or not 1 <= args.source_wait_seconds <= 7200:
        parser.error("Use 1–3 workers and a source wait of 1–7200 seconds")
    base = args.base.absolute()
    metadata_path = args.metadata.absolute()
    metadata = load_metadata(metadata_path)
    audit = base / "audit"
    audit.mkdir(parents=True, exist_ok=True)
    if args.detach:
        with (audit / "dependency-prefetch.log").open("ab") as log:
            child = subprocess.Popen([
                sys.executable, "-u", str(Path(__file__).resolve()), "--base", str(base),
                "--workers", str(args.workers), "--source-wait-seconds", str(args.source_wait_seconds),
                "--metadata", str(metadata_path),
            ], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        (audit / "dependency-prefetch.pid").write_text(str(child.pid) + "\n")
        print(f"Dependency prefetch detached; PID {child.pid}; log {audit / 'dependency-prefetch.log'}")
        return
    source = base / "source"
    run = base / "dependency-prefetch-runs" / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}")
    run.mkdir(parents=True)
    (base / "dependency-prefetch-current-run.txt").write_text(str(run) + "\n")
    try:
        with (base / "dependencies.prepare.lock").open("a") as lock:
            (run / "status.txt").write_text("running:waiting-dependency-lock\n")
            fcntl.flock(lock, fcntl.LOCK_EX)
            (run / "status.txt").write_text("running:waiting-source\n")
            deadline = time.monotonic() + args.source_wait_seconds
            while not (source / "lake-manifest.json").is_file():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for verified source preparation")
                time.sleep(2)
            # This takes/releases the separate source lock. The normal helper
            # never holds that lock while waiting for dependencies.prepare.lock.
            source_record(source, False)
            specs = list(dependencies(source))  # Validate all exact manifest pins.
            if (source / ".lake").is_symlink() or (source / ".lake/packages").is_symlink():
                raise RuntimeError("Refusing symlink package storage")
            (run / "status.txt").write_text("running:dependency-hydration\n")
            results = []
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                jobs = [pool.submit(prepare_one, base, source, run, *spec, metadata) for spec in specs]
                for job in as_completed(jobs):
                    results.append(job.result())
            # Source tracked files must be unchanged after all .lake work.
            source_record(source, False)
            (run / "result.json").write_text(json.dumps({
                "source_commit": SOURCE_REV, "finished_utc": utc(),
                "metadata_sha256": archive_sha(metadata_path),
                "repositories": sorted(results, key=lambda r: r["name"]),
            }, indent=2) + "\n")
            (run / "status.txt").write_text("complete:nine-dependencies,trees,objects,source-unchanged\n")
            (run / "driver-exit-code.txt").write_text("0\n")
            print(f"{utc()} all nine dependencies verified", flush=True)
    except BaseException:
        (run / "status.txt").write_text("failed\n")
        (run / "driver-exit-code.txt").write_text("1\n")
        raise


if __name__ == "__main__":
    main()
