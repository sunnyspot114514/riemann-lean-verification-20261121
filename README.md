# Riemann Hypothesis Lean verification

This repository records a compilation and kernel-checking attempt. The latest machine-readable state and verification outcome are in [results/status.json](results/status.json). A `running`, `failed`, or `runner_error` state is not a successful verification.

## Source under test

- Published mirror: https://github.com/CocoSgt/riemann-hypothesis-lean
- Exact source commit: `7cbf7a0c32fca1badf05931879a37c3f8a2b03b4`
- Exact `src` tree: `b41b516b1f7baee4d83ced235ecc71fd43c97695`
- Base compiler release: `leanprover/lean4:v4.33.1` (the original release is retained; a separately identified loader repair is available below)
- Linux release archive SHA-256: `890afd185370f85666025b883914ab4f4b339136f8c96167b69cfb62aecaf235`
- Official Mathlib revision: `0df444a360eaa60ab8c11dca51a86af692955474`
- Author announcement: https://alpo.ge/remark.pdf

The original source checkout is separate from this repository and remains unchanged. The bootstrap verifies all nine dependency pins, downloads the official Mathlib cache, and records exact versions. The submission's own modules are rebuilt from source. At this pinned source commit, all 175,187 submission modules belong to the `Main` import closure.

## Execution and evidence

The rented Ubuntu 22.04.5 container has **32 logical CPUs, a 60 GiB memory limit, and a 500 GiB data volume**. Host-wide `free` output is not the container memory limit. Everything is stored under `/root/autodl-tmp/rh-check` on that server. The operator's Windows computer controls SSH and relays network traffic in memory; project sources and build products are not saved there.

Result uploads use HTTPS and the operator's existing GitHub credential through a task-specific, RAM-only Git credential-cache socket. The token is not written to disk or this repository. After the final upload succeeds, the runner stops only that task's credential-cache helper. SSH login credentials, private keys, and unrelated files are excluded from the result repository. Pushes first use the direct GitHub route with `http.proxy=''` and the repository's `http.curloptResolve=github.com:443:140.82.112.3`. If it fails and the loopback proxy on `127.0.0.1:20081` is reachable, they retry through the operator's in-memory PC network relay, using the same repository and credentials. Normal TLS certificate and hostname verification remain enabled on both routes. The official Lean archive is additionally checked against the upstream release digest.

The server's original Git 2.34.1 does not support `http.curloptResolve` and silently ignored that setting. This task uses official Git 2.56.1, installed under `BASE/tools/git-current`, to apply the direct route. The runner records the actual selected Git version and executable in the environment evidence. A timed-out Git operation kills only its own process group, including transport children.

The runner records these separate stages:

1. Toolchain, source, dependency, cache and `Statements` environment preparation.
2. `lake --no-cache build Main`, with full output, time report and exact exit code.
3. An explicit type check against the standard Mathlib `RiemannHypothesis` and `#print axioms` for all five final theorems. Only `propext`, `Classical.choice` and `Quot.sound` are accepted by the axiom-report validator.
4. `leanchecker --fresh -v Main`, which replays the entire import closure in a fresh environment. **This uses the same Lean kernel, not an independent implementation.**
5. Final unchanged-source checks.

The original run used 16 Lake scheduler workers with `-j1` for each compiler. Continuations select the scheduler count explicitly with `--lean-num-threads`, and record the actual setting in the environment evidence. An audit-side TOML copy adds only `weakLeanArgs = ["-j1"]`; the published proof configuration and source stay unchanged. Resource samples use the container's cgroup limits and counters.

## Loader repair for the platform's mapping limit

The original run encountered `vm.max_map_count=65530`. A recorded ShapeWin reproduction reached approximately 65,532 mappings and showed file/anonymous `mmap` returning `ENOMEM`, followed by a null-buffer `read` returning `EFAULT`. The platform could not raise the host limit. See the [original diagnosis](results/diagnostics/20261121-map-limit/README.md).

The [reviewable loader patch](patches/lean-4.33.1-bounded-mmap-arena.patch) leaves the mathematical source, artifact format, header/version checks and region relocation logic unchanged. It limits direct file-mapping attempts and provides an opt-in disk-backed pool for ordinary v2 artifact copies. The temporary files are private, immediately unlinked, preallocated, and shared-mapped in 256 MiB segments; the last region releases its segment. v3 incremental snapshots retain their heap fallback. This backend targets standalone compiler and spawn/exec use, not arbitrary fork/COW behavior. [Implementation and scope](patches/ARENA-NOTES.md)

The modified runtime is a separately installed **release relink**, not the official binary and not a full compiler bootstrap. It reuses the official Lean/Init/Std objects and verifies that every kernel archive object is byte-for-byte unchanged. Only the loader object, the upstream shell object and the runtime members needed for the shared-library TLS model are rebuilt. Both original and modified binary hashes, source identities, exact commands and the patch digest are recorded in `loader-build-manifest.json`. The original release is never replaced.

[Regression tooling](scripts/test_lean_loader.py) compares the official and modified runtimes, checks acceptance/rejection and direct kernel rejection, tests imported artifact compatibility and bad headers, forces real arena use, observes region freeing, and replays the previously failing module without writing into the active project build. Build attempts and regression evidence are archived under [results/loader-fix](results/loader-fix). A passed loader regression does not imply that the full RH verification has passed.

The runner still bootstraps and checks the original official release first. For a patched continuation, it then explicitly selects the separate runtime and records `verification_runtime="loader-patched"`, its arena directory and mmap budget. It preserves the previous run's terminal snapshots and the incremental build cache. The four final verification gates are unchanged. Increasing disk or RAM alone does not alter the host's mapping limit; this repair changes how artifact buffers are allocated.

Completed `.setup.json` intermediates are deleted only after a newer successful Lake trace and nonempty `.olean` exist. Source and proof artifacts are retained. This controls disk usage in a project where large repeated import metadata would otherwise dominate storage.

## Viewing progress

- [Current status](results/status.json): running stage, exit codes, timestamps and resource samples.
- [Environment](results/environment.json): actual container limits and measurement scope.
- [Log tails](results/logtails): periodically updated recent output.
- [Evidence snapshots](results/evidence): versions, pinned source/dependency records, and the source/dependency prefetchers' explicit `result.json` provenance records.
- [Final logs](results/final): compressed full logs and hash manifests after the run ends.

The runner starts each computation phase before its initial publication, so upload delays do not postpone the calculation. It normally publishes every five minutes and on phase changes. Each push route has a 60-second attempt limit for ordinary snapshots and a 900-second limit for the final archives; chosen routes and failures are logged. If the network is unavailable, it retains server-side logs and retries. Final archive parts are at most 40 MiB; concatenate parts in manifest order, verify their SHA-256 values, and gunzip. A setup-only or statement-only success does not validate the proof. Stronger independent-checker results, if performed, will be reported separately.

## Reproduction

Install the prerequisites listed in the bootstrap script, configure a writable clone of this repository at `BASE/verification`, and provide a functioning network route. The included runner expects its loopback HTTPS proxy at `127.0.0.1:20080`; adapt that network setting for another environment.

```bash
python3 /root/autodl-tmp/rh-check/verification/scripts/run_all.py \
  --base /root/autodl-tmp/rh-check --lean-num-threads 16
```

For a loader-patched run, build the separate runtime with [scripts/build_lean_loader.py](scripts/build_lean_loader.py), run the loader regressions, and select it explicitly:

```bash
python3 /root/autodl-tmp/rh-check/verification/scripts/run_all.py \
  --base /root/autodl-tmp/rh-check \
  --toolchain-dir /root/autodl-tmp/rh-check/tools/lean-4.33.1-loader-v2 \
  --olean-arena-dir /root/autodl-tmp/rh-check/tmp/olean-arena \
  --mmap-regions 50000 --lean-num-threads 4
```

When resuming an existing terminal run, additionally pass its ID with `--resumed-from-run-id`; do not run two Lake builds against the same output directory.

The detached session uses `BASE/control/tmux.sock`. Server status and complete logs remain in `BASE/audit`, even if publishing is interrupted. The Windows relay accepts SSH configuration through standard input and does not write credentials or downloaded artifacts to disk.

## Provenance

The mathematical claim and original source belong to Levent Alpöge and the collaborators credited in his announcement. This repository contains verification tooling and observations and does not claim authorship of the proof. Successful compilation, axiom inspection, fresh replay, and verification by independently implemented kernels are distinct pieces of evidence.