#!/usr/bin/env python3
"""Archive only known loader-build/test evidence; never commit or push Git."""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import uuid

LIMIT = 40 * 1024 * 1024
ATTEMPTS = ("loader-build-v1", "loader-build-v1a", "loader-build-v1b", "loader-build-v2")
BUILD_FILES = {
    "build-status.json", "loader-build-manifest.json", "loader-patch.diff",
    "archive-object-comparison.json", "export-symbol-comparison.json",
    "root-lean-object.json", "runtime-tls-members.json", "LinkFlags.lean",
}
CASE_FILES = {"command.json", "result.json", "stdout.log", "resources.jsonl", "free-phase.txt"}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for data in iter(lambda: f.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def load_summary(path):
    if not path.is_file():
        return {"state": "missing"}
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except (OSError, ValueError) as exc:
        return {"state": "unreadable", "error": str(exc)}


def regular_file(root, path):
    """Reject symlink entries/parents and non-regular files inside each known tree."""
    if root.is_symlink() or not root.is_dir():
        return False
    for parent in path.relative_to(root).parents:
        if (root / parent).is_symlink():
            return False
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


class HashReader:
    def __init__(self, stream):
        self.stream, self.digest = stream, hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


def fingerprint(s):
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--test-run", type=Path, required=True)
    parser.add_argument("--toolchain", type=Path, required=True)
    args = parser.parse_args()
    base, tests, toolchain = (p.resolve() for p in (args.base, args.test_run, args.toolchain))
    audit = base / "audit"
    if audit not in tests.parents or not tests.is_dir():
        parser.error("--test-run must be an existing directory beneath BASE/audit")
    if base / "tools" not in toolchain.parents or not toolchain.is_dir():
        parser.error("--toolchain must be an existing directory beneath BASE/tools")
    for required in (tests / "test-report.json", toolchain / "loader-build-manifest.json"):
        if not regular_file(required.parent, required):
            parser.error("Missing regular report/manifest: " + str(required))

    selected = {}

    def add(root, path, archive_path):
        if regular_file(root, path):
            selected[archive_path] = path

    attempts = []
    for name in ATTEMPTS:
        root = audit / name
        record = {"name": name, "directory_present": root.is_dir() and not root.is_symlink()}
        if record["directory_present"]:
            state = load_summary(root / "build-status.json")
            record.update({k: state[k] for k in ("state", "started_utc", "completed_utc", "error") if k in state})
            for name_in_run in BUILD_FILES:
                add(root, root / name_in_run, f"build-attempts/{name}/{name_in_run}")
            logs = root / "logs"
            if logs.is_dir() and not logs.is_symlink():
                for path in sorted(logs.iterdir()):
                    if path.name.endswith((".command.json", ".exit-code.txt", ".log")):
                        add(root, path, f"build-attempts/{name}/logs/{path.name}")
        attempts.append(record)

    for path in sorted(tests.iterdir()):
        if path.name in {"test-report.json", "loader-build-manifest.json", "shape-command-original.json"} or (
                path.name.startswith("heavy-") and path.name.endswith("-command-original.json")):
            add(tests, path, "test-run/" + path.name)
        if path.name == "fixtures" and path.is_dir() and not path.is_symlink():
            for fixture in sorted(path.glob("*.lean")):
                add(tests, fixture, "test-run/fixtures/" + fixture.name)
        elif path.name == "baseline-evidence" and path.is_dir() and not path.is_symlink():
            for baseline in sorted(path.iterdir()):
                if baseline.suffix in {".json", ".jsonl", ".log", ".txt", ".diff"}:
                    add(tests, baseline, "test-run/baseline-evidence/" + baseline.name)
        elif path.is_dir() and not path.is_symlink():
            for filename in CASE_FILES:
                add(tests, path / filename, "test-run/" + path.name + "/" + filename)
    for name in ("loader-build-manifest.json", "loader-patch.diff", "build_lean_loader.py"):
        add(toolchain, toolchain / name, "toolchain/" + name)

    report = load_summary(tests / "test-report.json")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    destination = base / "verification/results/loader-fix" / stamp
    destination.mkdir(parents=True, exist_ok=False)
    summary = {
        "schema": 1, "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": "Loader repair build and regression evidence; the full RH verification gates are separate.",
        "build_attempts": attempts, "test_run": str(tests.relative_to(base)),
        "toolchain": str(toolchain.relative_to(base)),
        "tests": {key: report[key] for key in ("state", "passed", "started_utc", "finished_utc", "error") if key in report},
        "case_count": len(report.get("cases", [])),
        "failed_cases": [case.get("name") for case in report.get("cases", []) if not case.get("passed", False)],
        "excluded": "Binaries, .olean/.o files, static archives, arena files, whole source/toolchain trees, control files and credentials.",
    }
    sources, parts, direct = [], [], []
    # The temporary compressed stream is outside the repository. Published files
    # are always at most LIMIT bytes, including when a large log needs splitting.
    with tempfile.TemporaryDirectory(prefix="loader-evidence-", dir=audit) as temporary:
        compressed = Path(temporary) / "loader-evidence.tar.gz"
        with compressed.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                for archive_path, path in sorted(selected.items()):
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "rb") as stream:
                        before = os.fstat(stream.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            raise RuntimeError("Evidence changed to a non-regular file: " + str(path))
                        info = tarfile.TarInfo(archive_path)
                        info.size, info.mode, info.mtime = before.st_size, 0o644, int(before.st_mtime)
                        reader = HashReader(stream)
                        archive.addfile(info, reader)
                        if fingerprint(before) != fingerprint(os.fstat(stream.fileno())):
                            raise RuntimeError("Evidence changed while archiving: " + str(path))
                        sources.append({"path": archive_path, "source": str(path.relative_to(base)),
                                        "bytes": before.st_size, "sha256": reader.digest.hexdigest()})
        compressed_sha, total_compressed = sha(compressed), compressed.stat().st_size
        with compressed.open("rb") as stream:
            index = 1
            while True:
                chunk = stream.read(LIMIT)
                if not chunk:
                    break
                name = "loader-evidence.tar.gz" if total_compressed <= LIMIT else f"loader-evidence.tar.gz.part-{index:04d}"
                (destination / name).write_bytes(chunk)
                parts.append({"path": name, "bytes": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
                index += 1

    expected = {item["path"]: item for item in sources}
    for original, name, archive_path in (
            (tests / "test-report.json", "test-report.json", "test-run/test-report.json"),
            (toolchain / "loader-build-manifest.json", "loader-build-manifest.json", "toolchain/loader-build-manifest.json")):
        if original.stat().st_size > LIMIT:
            raise RuntimeError("Direct-readable JSON exceeds 40 MiB; its archive is retained: " + str(original))
        target = destination / name
        shutil.copyfile(original, target)
        digest = sha(target)
        if digest != expected[archive_path]["sha256"]:
            raise RuntimeError("Report/manifest changed after snapshot: " + str(original))
        direct.append({"path": name, "bytes": target.stat().st_size, "sha256": digest})
    summary.update(source_file_count=len(sources), source_bytes=sum(x["bytes"] for x in sources),
                   compressed_bytes=total_compressed, archive_parts=len(parts))
    summary_path = destination / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    direct.append({"path": "summary.json", "bytes": summary_path.stat().st_size, "sha256": sha(summary_path)})
    manifest = {"schema": 1, "format": "tar.gz", "max_file_bytes": LIMIT,
                "archive_sha256": compressed_sha, "archive_bytes": total_compressed,
                "restore": "Concatenate archive_parts in listed order, then extract the resulting gzip tar stream.",
                "archive_parts": parts, "direct_files": direct, "source_files": sources}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    paths = sorted(destination.iterdir())
    if any(path.stat().st_size > LIMIT for path in paths):
        raise RuntimeError("An output exceeds the 40 MiB limit")
    print(json.dumps({"directory": str(destination), "paths": [str(p) for p in paths], "summary": summary},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
