#!/usr/bin/env bash
# Prepare the original pinned project on the server's data volume.
# Public upstreams only. Proxy settings are inherited from the runner, never logged.
# This does not build Main. Source bootstrap.env before build_and_check.sh.
set -Eeuo pipefail
umask 022

RH_CHECK_BASE=$(realpath -m "${1:-/root/autodl-tmp/rh-check}")
RH_PROJECT_DIR="$RH_CHECK_BASE/source"
RH_AUDIT_DIR="$RH_CHECK_BASE/audit"
RH_TOOLCHAIN_DIR="$RH_CHECK_BASE/tools/lean-4.33.1-linux"
RH_SCRIPTS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
RH_TOOLCHAIN_SHA=890afd185370f85666025b883914ab4f4b339136f8c96167b69cfb62aecaf235
RH_TOOLCHAIN_BYTES=570405234
RH_TOOLCHAIN_URL=https://api.github.com/repos/leanprover/lean4/releases/assets/523687465
RH_ARCHIVE="$RH_CHECK_BASE/downloads/lean-4.33.1-linux.tar.zst"
RH_ARIA2_PART="$RH_ARCHIVE.aria2.part"
RH_INSTALL_INVENTORY="$RH_CHECK_BASE/metadata/lean-4.33.1-files.sha256"

mkdir -p "$RH_CHECK_BASE"/{downloads,tools,metadata,cache/mathlib,tmp,audit,bootstrap-runs}
exec 8>"$RH_CHECK_BASE/bootstrap.lock"
flock -n 8 || { echo 'Another bootstrap owns this lock.' >&2; exit 1; }
RH_BOOT_RUN="$RH_CHECK_BASE/bootstrap-runs/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir "$RH_BOOT_RUN"
printf '%s\n' "$RH_BOOT_RUN" >"$RH_CHECK_BASE/bootstrap-current-run.txt"
RH_STAGE=initializing
finish_bootstrap() {
    RH_EXIT_CODE=$?
    printf '%s\n' "$RH_EXIT_CODE" >"$RH_BOOT_RUN/bootstrap.exit-code.txt"
    date -u +%Y-%m-%dT%H:%M:%SZ >"$RH_BOOT_RUN/bootstrap.end-utc.txt"
    if (( RH_EXIT_CODE != 0 )); then
        printf 'failed:%s\n' "$RH_STAGE" >"$RH_BOOT_RUN/status.txt"
        printf 'Bootstrap failed in %s; logs: %s\n' "$RH_STAGE" "$RH_BOOT_RUN" >&2
    fi
}
trap finish_bootstrap EXIT

stage() {
    RH_STAGE=$1; shift
    printf 'running:%s\n' "$RH_STAGE" >"$RH_BOOT_RUN/status.txt"
    date -u +%Y-%m-%dT%H:%M:%SZ >"$RH_BOOT_RUN/$RH_STAGE.start-utc.txt"
    printf '[%s] Starting %s\n' "$(date -u +%FT%TZ)" "$RH_STAGE"
    set +e
    (set -e; "$@") >"$RH_BOOT_RUN/$RH_STAGE.log" 2>&1
    RH_STAGE_RC=$?
    set -e
    printf '%s\n' "$RH_STAGE_RC" >"$RH_BOOT_RUN/$RH_STAGE.exit-code.txt"
    date -u +%Y-%m-%dT%H:%M:%SZ >"$RH_BOOT_RUN/$RH_STAGE.end-utc.txt"
    if (( RH_STAGE_RC != 0 )); then
        tail -n 60 "$RH_BOOT_RUN/$RH_STAGE.log" >&2
        return "$RH_STAGE_RC"
    fi
    printf '[%s] Completed %s\n' "$(date -u +%FT%TZ)" "$RH_STAGE"
}

preflight() {
    test "$(uname -s)" = Linux
    test "$(uname -m)" = x86_64
    for RH_CMD in aria2c curl git python3 tar zstd sha256sum stat flock realpath make cc; do
        command -v "$RH_CMD" >/dev/null || { echo "Missing prerequisite: $RH_CMD" >&2; return 1; }
    done
    test -f "$RH_SCRIPTS_DIR/prepare_dependencies.py"
    df -h "$RH_CHECK_BASE"
    python3 - "$RH_CHECK_BASE" <<'PY'
import shutil, sys
usage = shutil.disk_usage(sys.argv[1])
print(f"Data volume free bytes: {usage.free}")
if usage.free < 25 * 1024**3:
    raise SystemExit("Need at least 25 GiB free for bootstrap; the full build needs substantially more.")
PY
}

download_toolchain() {
    if [[ ! -f "$RH_ARCHIVE" ]]; then
        # The server reaches GitHub's official asset API directly. Only this
        # subprocess bypasses the runner's proxy; git/cache operations inherit it.
        # Keep aria2's partial file and .aria2 control file distinct from any
        # earlier curl .part download. --out fixes the name across redirects.
        env -u http_proxy -u https_proxy -u all_proxy \
            -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
            aria2c --no-conf=true --no-netrc=true \
            --split=16 --max-connection-per-server=16 --min-split-size=1M \
            --continue=true --check-integrity=true --file-allocation=none \
            --auto-file-renaming=false --auto-save-interval=30 \
            --max-tries=6 --retry-wait=5 --connect-timeout=60 --timeout=60 \
            --summary-interval=30 --console-log-level=notice --download-result=full \
            --header='Accept: application/octet-stream' \
            --checksum="sha-256=$RH_TOOLCHAIN_SHA" \
            --dir="$(dirname -- "$RH_ARIA2_PART")" \
            --out="$(basename -- "$RH_ARIA2_PART")" "$RH_TOOLCHAIN_URL"
        test "$(stat -c %s "$RH_ARIA2_PART")" = "$RH_TOOLCHAIN_BYTES"
        printf '%s  %s\n' "$RH_TOOLCHAIN_SHA" "$RH_ARIA2_PART" | sha256sum -c -
        mv -- "$RH_ARIA2_PART" "$RH_ARCHIVE"
    fi
    test "$(stat -c %s "$RH_ARCHIVE")" = "$RH_TOOLCHAIN_BYTES"
    printf '%s  %s\n' "$RH_TOOLCHAIN_SHA" "$RH_ARCHIVE" | sha256sum -c -
    printf '%s  lean-4.33.1-linux.tar.zst\n' "$RH_TOOLCHAIN_SHA" >"$RH_BOOT_RUN/release.sha256"
}

install_toolchain() {
    if [[ -e "$RH_TOOLCHAIN_DIR" ]]; then
        test -f "$RH_INSTALL_INVENTORY"
        (cd "$RH_TOOLCHAIN_DIR"; sha256sum --quiet --check "$RH_INSTALL_INVENTORY")
    else
        RH_EXTRACT_DIR=$(mktemp -d "$RH_CHECK_BASE/tools/extract-4.33.1.XXXXXX")
        tar --zstd --no-same-owner --no-same-permissions -xf "$RH_ARCHIVE" -C "$RH_EXTRACT_DIR"
        test -x "$RH_EXTRACT_DIR/lean-4.33.1-linux/bin/lean"
        mv -- "$RH_EXTRACT_DIR/lean-4.33.1-linux" "$RH_TOOLCHAIN_DIR"
        rmdir "$RH_EXTRACT_DIR"
        (cd "$RH_TOOLCHAIN_DIR"; find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) \
            >"$RH_INSTALL_INVENTORY.tmp"
        mv -- "$RH_INSTALL_INVENTORY.tmp" "$RH_INSTALL_INVENTORY"
    fi
    "$RH_TOOLCHAIN_DIR/bin/lean" --version
    "$RH_TOOLCHAIN_DIR/bin/lake" --version
    case "$("$RH_TOOLCHAIN_DIR/bin/lean" --version)" in
        *'version 4.33.1,'*) ;; *) echo 'Unexpected Lean version' >&2; return 1;;
    esac
}

official_cache() {
    cd "$RH_PROJECT_DIR"
    # These variables can override the official cache's trust/routing policy.
    unset MATHLIB_CACHE_GET_URL MATHLIB_CACHE_FROM MATHLIB_CACHE_REPO_SCOPE
    lake --no-cache exe cache get
}

statements_sanity() {
    cd "$RH_PROJECT_DIR"
    lake --no-cache build Statements
}

stage preflight preflight
stage toolchain-download download_toolchain
stage toolchain-install install_toolchain
export RH_CHECK_BASE RH_PROJECT_DIR RH_AUDIT_DIR RH_TOOLCHAIN_DIR
export PATH="$RH_TOOLCHAIN_DIR/bin:$PATH"
export MATHLIB_CACHE_DIR="$RH_CHECK_BASE/cache/mathlib"
export TMPDIR="$RH_CHECK_BASE/tmp"
export LAKE_ARTIFACT_CACHE=false
export LAKE_CACHE_DIR=''
export GIT_TERMINAL_PROMPT=0
export LEAN_NUM_THREADS="${LEAN_NUM_THREADS:-2}"

stage source-fetch python3 "$RH_SCRIPTS_DIR/prepare_dependencies.py" \
    --project "$RH_PROJECT_DIR" --phase source --output "$RH_BOOT_RUN/source.json"
stage dependency-fetch python3 "$RH_SCRIPTS_DIR/prepare_dependencies.py" \
    --project "$RH_PROJECT_DIR" --phase dependencies --output "$RH_BOOT_RUN/dependencies-before-cache.json"
stage mathlib-cache official_cache
stage statements-sanity statements_sanity
stage source-integrity python3 "$RH_SCRIPTS_DIR/prepare_dependencies.py" \
    --project "$RH_PROJECT_DIR" --phase verify --output "$RH_BOOT_RUN/dependencies-after-cache.json"

# Serialize only our non-secret paths/options. Never serialize the inherited environment.
{
    printf 'export RH_CHECK_BASE=%q\n' "$RH_CHECK_BASE"
    printf 'export RH_PROJECT_DIR=%q\n' "$RH_PROJECT_DIR"
    printf 'export RH_AUDIT_DIR=%q\n' "$RH_AUDIT_DIR"
    printf 'export RH_TOOLCHAIN_DIR=%q\n' "$RH_TOOLCHAIN_DIR"
    printf 'export PATH=%q/bin:"$PATH"\n' "$RH_TOOLCHAIN_DIR"
    printf 'export MATHLIB_CACHE_DIR=%q\n' "$MATHLIB_CACHE_DIR"
    printf 'export TMPDIR=%q\n' "$TMPDIR"
    printf 'export LAKE_ARTIFACT_CACHE=false\nexport LAKE_CACHE_DIR=\x27\x27\n'
    printf 'export GIT_TERMINAL_PROMPT=0\n'
} >"$RH_CHECK_BASE/bootstrap.env.tmp"
mv -- "$RH_CHECK_BASE/bootstrap.env.tmp" "$RH_CHECK_BASE/bootstrap.env"
printf 'complete:toolchain,source,dependencies,mathlib-cache,statements,source-unchanged\n' >"$RH_BOOT_RUN/status.txt"
printf 'Bootstrap complete. Source %s/bootstrap.env before running the proof build.\n' "$RH_CHECK_BASE"
