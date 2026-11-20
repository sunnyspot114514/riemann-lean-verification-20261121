#!/usr/bin/env python3
"""Remove only completed Lake setup JSONs; never remove source or proof artifacts."""
import argparse
import json
import os
from pathlib import Path
import stat
import time

p = argparse.ArgumentParser()
p.add_argument("project", type=Path)
p.add_argument("stop_file", type=Path)
args = p.parse_args()
build = (args.project.resolve() / ".lake/build").resolve()
ir = build / "ir"
lib = build / "lib/lean"

def fingerprint(s):
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)

while not args.stop_file.exists():
    count = size = 0
    if ir.is_dir() and lib.is_dir():
        for parent, dirs, files in os.walk(ir, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(parent) / d).is_symlink()]
            for name in files:
                if not name.endswith(".setup.json"):
                    continue
                setup = Path(parent) / name
                rel = setup.relative_to(ir)
                stem = name.removesuffix(".setup.json")
                trace = lib / rel.with_name(stem + ".trace")
                olean = lib / rel.with_name(stem + ".olean")
                try:
                    ss, ts, ls = setup.lstat(), trace.lstat(), olean.lstat()
                    if not all(stat.S_ISREG(s.st_mode) for s in (ss, ts, ls)):
                        continue
                    # Lake writes trace only after compile + artifact computation succeed.
                    if not (ts.st_size and ls.st_size and ts.st_mtime_ns > ss.st_mtime_ns):
                        continue
                    if time.time_ns() - ss.st_mtime_ns < 2_000_000_000:
                        continue
                    if fingerprint(setup.lstat()) != fingerprint(ss):
                        continue
                    setup.unlink()
                    count += 1
                    size += ss.st_size
                except (FileNotFoundError, PermissionError):
                    continue
    if count:
        print(json.dumps({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "completed_setup_files_removed": count,
                          "bytes_removed": size}), flush=True)
    for _ in range(3):
        if args.stop_file.exists():
            break
        time.sleep(5)
