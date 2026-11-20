#!/usr/bin/env bash
# Preconditions: official Lean 4.33.1 in PATH; pinned dependencies and Mathlib
# cache already fetched. Run on the rented server, not the user's computer.
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
