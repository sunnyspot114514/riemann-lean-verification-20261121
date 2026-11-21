#!/usr/bin/env bash
# Preconditions: verified Lean 4.33.1 (official or separately recorded loader
# patch) in PATH; pinned dependencies/cache fetched. Run on the rented server.
set -euo pipefail
project=$(realpath "${1:?project path required}")
audit_base=$(realpath -m "${2:?audit directory required}")
scripts=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
pin=7cbf7a0c32fca1badf05931879a37c3f8a2b03b4
case "$audit_base/" in "$project/"*) echo 'Audit files must live outside the source checkout.' >&2; exit 1;; esac
mkdir -p "$audit_base/runs"
exec 9>"$audit_base/build.lock"
flock -n 9 || { echo 'Another build driver owns this lock.' >&2; exit 1; }
run="$audit_base/runs/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir "$run"
printf '%s\n' "$run" >"$audit_base/current-run.txt"
cd "$project"
test "$(git rev-parse HEAD)" = "$pin"
git diff --quiet
git diff --cached --quiet
git rev-parse HEAD >"$run/source-commit.txt"
git ls-files -s >"$run/source-index.txt"
# Record and verify the selected runtime independently of its compatibility
# githash. A loader patch intentionally retains the original .olean format.
if ! python3 - "$run" "$audit_base" "$scripts/build_and_check.sh" <<'PY' >"$run/toolchain-provenance.log" 2>&1
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

run, audit, driver_path = map(Path, sys.argv[1:])
base_commit = "819816b2e0a3bf405af45ae5c7af2491d8f5bee6"
official = Path(os.environ.get("RH_ORIGINAL_TOOLCHAIN_DIR",
                              audit.parent / "tools/lean-4.33.1-linux")).resolve()
selected = Path(os.environ.get("RH_TOOLCHAIN_DIR", official)).resolve()
kind = "official" if selected == official else "loader-patched"
required = {"bin/lean", "bin/lake", "bin/leanchecker", "lib/lean/libleanshared.so"}

def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def inside(root, relative):
    item = Path(relative)
    if item.is_absolute() or ".." in item.parts:
        raise SystemExit("Toolchain manifest contains a non-relative path.")
    resolved = (root / item).resolve()
    if root not in resolved.parents or not resolved.is_file():
        raise SystemExit("Toolchain manifest file is missing or outside its toolchain.")
    return resolved

executables = {}
for name in ("lean", "lake", "leanchecker"):
    resolved = shutil.which(name)
    expected = inside(selected, "bin/" + name)
    if not resolved or Path(resolved).resolve() != expected:
        raise SystemExit("Selected toolchain does not own PATH executable " + name)
    executables[name] = {"path": str(expected), "sha256": sha(expected)}
sysroot = Path(os.environ.get("LEAN_SYSROOT", selected)).resolve()
if sysroot != selected:
    raise SystemExit("LEAN_SYSROOT does not match the selected toolchain.")
manifest_sha = patch_sha = None
if kind == "loader-patched":
    manifest_path = inside(selected, "loader-build-manifest.json")
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("schema") != 1 or manifest.get("kind") != kind
            or manifest.get("base_lean_commit") != base_commit
            or manifest.get("lean_version") != "4.33.1"
            or Path(manifest.get("original_toolchain_dir", "")).resolve() != official):
        raise SystemExit("Loader build manifest identity does not match the selected runtime.")
    binaries = manifest.get("binaries", {})
    original_binaries = manifest.get("original_binaries", {})
    if not required.issubset(binaries) or not required.issubset(original_binaries):
        raise SystemExit("Loader manifest omits required compiler/checker/shared-loader hashes.")
    for root, items in ((selected, binaries), (official, original_binaries)):
        for relative, expected in items.items():
            if sha(inside(root, relative)) != expected:
                raise SystemExit("Toolchain binary checksum mismatch: " + relative)
    patch = inside(selected, manifest.get("patch_path", ""))
    patch_sha = sha(patch)
    if patch_sha != manifest.get("patch_sha256"):
        raise SystemExit("Loader patch checksum differs from its manifest.")
    manifest_sha = sha(manifest_path)
    shutil.copyfile(manifest_path, run / "loader-build-manifest.json")
    shutil.copyfile(patch, run / "loader-patch.diff")
else:
    binaries = {relative: sha(inside(selected, relative)) for relative in sorted(required)}
    original_binaries = dict(binaries)

version = subprocess.check_output([str(selected / "bin/lean"), "--version"], text=True).strip()
original_version = subprocess.check_output([str(official / "bin/lean"), "--version"], text=True).strip()
for observed in (version, original_version):
    if "version 4.33.1," not in observed or "commit " + base_commit not in observed:
        raise SystemExit("Lean version or original compatibility githash differs from the pinned release.")
prefix = Path(subprocess.check_output([str(selected / "bin/lean"), "--print-prefix"], text=True).strip()).resolve()
if prefix != selected:
    raise SystemExit("Lean's actual installation prefix differs from the selected toolchain.")

with (run / "toolchain-binaries.sha256").open("w") as output:
    for root, items in ((selected, binaries), (official, original_binaries)):
        for relative, digest in sorted(items.items()):
            output.write(f"{digest}  {root / relative}\n")
provenance = {
    "schema": 1, "kind": kind, "base_lean_commit": base_commit,
    "selected_toolchain_dir": str(selected), "original_toolchain_dir": str(official),
    "lean_sysroot": str(sysroot), "lean_installation_prefix": str(prefix),
    "lean_version": version, "original_lean_version": original_version,
    "executables": executables, "binaries": binaries, "original_binaries": original_binaries,
    "loader_manifest_sha256": manifest_sha, "patch_sha256": patch_sha,
    "lean_max_mmap_regions_environment": os.environ.get("LEAN_MAX_MMAP_REGIONS"),
    "lean_olean_arena_dir_environment": os.environ.get("LEAN_OLEAN_ARENA_DIR"),
    "ld_library_path_environment": os.environ.get("LD_LIBRARY_PATH"),
    "ld_preload_environment": os.environ.get("LD_PRELOAD"),
    "driver_sha256": sha(driver_path),
    "compatibility_note": "A matching githash permits existing .olean reuse; it does not identify a patched binary as the official release."
}
temporary = run / "toolchain-provenance.json.tmp"
temporary.write_text(json.dumps(provenance, indent=2) + "\n")
temporary.replace(run / "toolchain-provenance.json")
print(json.dumps({"runtime": kind, "manifest_sha256": manifest_sha, "patch_sha256": patch_sha}))
PY
then
    printf 'failed:toolchain-provenance\n' >"$run/status.txt"
    printf '1\n' >"$run/driver-exit-code.txt"
    cat "$run/toolchain-provenance.log" >&2
    exit 1
fi
lean --version >"$run/lean-version.txt"
lake --version >"$run/lake-version.txt"
case "$(cat "$run/lean-version.txt")" in *'version 4.33.1,'*) ;; *) echo 'Expected Lean 4.33.1.' >&2; exit 1;; esac
export LAKE_ARTIFACT_CACHE=false
export LAKE_CACHE_DIR=''
export LEAN_NUM_THREADS="${LEAN_NUM_THREADS:-2}"
# LEAN_NUM_THREADS tunes Lake's scheduler; it is not a strict aggregate RAM quota.
python3 - "$project/lakefile.toml" "$run/lakefile-resource.toml" <<'PY'
from pathlib import Path
import re
import sys
source = Path(sys.argv[1]).read_bytes()
if re.search(rb'(?m)^\s*weakLeanArgs\s*=', source):
    raise SystemExit('Existing weakLeanArgs requires explicit review; overlay not created.')
Path(sys.argv[2]).write_bytes(b'weakLeanArgs = ["-j1"]\n' + source)
PY
sha256sum "$project/lakefile.toml" "$run/lakefile-resource.toml" >"$run/build-config-sha256.txt"
printf 'Lake runtime worker setting: LEAN_NUM_THREADS=%s\nLean compiler worker setting: -j1\nOriginal lakefile.toml is unchanged; external overlay adds only weakLeanArgs.\n' "$LEAN_NUM_THREADS" >"$run/resource-controls.txt"
lake_cmd=(lake -d "$project" -f "$run/lakefile-resource.toml")
capture_cgroup() {
    output=$1
    printf 'Visible cgroup counters include other processes and are not reset for this run.\n' >"$output"
    for field in memory.current memory.peak memory.max cpu.max cpuset.cpus.effective; do
        if [[ -r "/sys/fs/cgroup/$field" ]]; then
            printf '%s=' "$field" >>"$output"
            cat "/sys/fs/cgroup/$field" >>"$output"
        fi
    done
}
capture_cgroup "$run/cgroup-start.txt"
python3 "$scripts/clean_completed_setups.py" "$project" "$run/stop-cleaner" >"$run/setup-cleaner.jsonl" 2>&1 &
cleaner_pid=$!
finish() {
    rc=$?
    touch "$run/stop-cleaner"
    wait "$cleaner_pid" || true
    capture_cgroup "$run/cgroup-end.txt"
    git status --porcelain=v1 --untracked-files=no >"$run/final-source-status.txt" || true
    printf '%s\n' "$rc" >"$run/driver-exit-code.txt"
    if (( rc != 0 )); then printf 'failed\n' >"$run/status.txt"; fi
}
trap finish EXIT
stage() {
    name=$1; shift
    printf 'running:%s\n' "$name" >"$run/status.txt"
    date -u +%Y-%m-%dT%H:%M:%SZ >"$run/$name.start-utc.txt"
    printf '%q ' "$@" >"$run/$name.command.sh"
    printf '\n' >>"$run/$name.command.sh"
    set +e
    /usr/bin/time -v -o "$run/$name.time.txt" "$@" >"$run/$name.log" 2>&1
    rc=$?
    set -e
    printf '%s\n' "$rc" >"$run/$name.exit-code.txt"
    date -u +%Y-%m-%dT%H:%M:%SZ >"$run/$name.end-utc.txt"
    return "$rc"
}
stage build-main "${lake_cmd[@]}" --no-cache build Main
cat >"$run/Check.lean" <<'LEAN'
import Main

example : _root_.RiemannHypothesis := _root_.riemannHypothesis

#print axioms riemannHypothesis
#print axioms riemannZeta_eq_zero_re
#print axioms LFunction_ne_zero_of_half_lt_re
#print axioms LFunction_eq_zero_re
#print axioms LFunction_eq_zero_re_of_isPrimitive
LEAN
stage axioms "${lake_cmd[@]}" env lean -j1 --tstack=400000 --root="$run" "$run/Check.lean"
stage validate-axioms python3 "$scripts/validate_axioms.py" "$run/axioms.log"
# Same Lean kernel, fresh-environment replay; NOT an independent implementation.
stage fresh-replay "${lake_cmd[@]}" env leanchecker --fresh -v Main
git diff --exit-code >"$run/final-source.diff"
git diff --cached --exit-code >"$run/final-index.diff"
test "$(git rev-parse HEAD)" = "$pin"
printf 'complete:build-main,axioms,allowed-axioms,fresh-replay,source-unchanged\n' >"$run/status.txt"

