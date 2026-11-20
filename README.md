# Riemann Hypothesis Lean verification

This repository records a compilation and kernel-checking attempt. The latest machine-readable state and verification outcome are in [results/status.json](results/status.json). A `running`, `failed`, or `runner_error` state is not a successful verification.

## Source under test

- Published mirror: https://github.com/CocoSgt/riemann-hypothesis-lean
- Exact source commit: `7cbf7a0c32fca1badf05931879a37c3f8a2b03b4`
- Exact `src` tree: `b41b516b1f7baee4d83ced235ecc71fd43c97695`
- Official Lean: `leanprover/lean4:v4.33.1`
- Linux release archive SHA-256: `890afd185370f85666025b883914ab4f4b339136f8c96167b69cfb62aecaf235`
- Official Mathlib revision: `0df444a360eaa60ab8c11dca51a86af692955474`
- Author announcement: https://alpo.ge/remark.pdf

The original source checkout is separate from this repository and remains unchanged. The bootstrap verifies all nine dependency pins, downloads the official Mathlib cache, and records exact versions. The submission's own modules are rebuilt from source. At this pinned source commit, all 175,187 submission modules belong to the `Main` import closure.

## Execution and evidence

The rented Ubuntu 22.04.5 container has **32 logical CPUs, a 60 GiB memory limit, and a 500 GiB data volume**. Host-wide `free` output is not the container memory limit. Everything is stored under `/root/autodl-tmp/rh-check` on that server. The operator's Windows computer controls SSH and relays network traffic in memory; project sources and build products are not saved there.

Result uploads use HTTPS and the operator's existing GitHub credential through a task-specific, RAM-only Git credential-cache socket. The token is not written to disk or this repository. After the final upload succeeds, the runner stops only that task's credential-cache helper. SSH login credentials, private keys, and unrelated files are excluded from the result repository. The result checkout uses repository-local `http.proxy=''` and `http.curloptResolve=github.com:443:140.82.112.3` for its direct GitHub route; normal TLS certificate and hostname verification remain enabled. The official Lean archive is additionally checked against the upstream release digest.

The runner records these separate stages:

1. Toolchain, source, dependency, cache and `Statements` environment preparation.
2. `lake --no-cache build Main`, with full output, time report and exact exit code.
3. An explicit type check against the standard Mathlib `RiemannHypothesis` and `#print axioms` for all five final theorems. Only `propext`, `Classical.choice` and `Quot.sound` are accepted by the axiom-report validator.
4. `leanchecker --fresh -v Main`, which replays the entire import closure in a fresh environment. **This uses the same Lean kernel, not an independent implementation.**
5. Final unchanged-source checks.

The initial scheduler setting is `LEAN_NUM_THREADS=2`. An audit-side TOML copy adds only `weakLeanArgs = ["-j1"]` to limit each compiler's worker setting; the published project configuration and official binaries are unchanged. This is not an aggregate memory guarantee. Resource samples use cgroup limits and counters, with their scope and sampling limitations recorded.

The platform reports `vm.max_map_count=65530` and denied an attempt to raise it. The runner only records this limit and the process's soft/hard open-file limits; it changes no global setting. A large import closure may encounter mapping limits or additional RAM costs, which must be distinguished from a proof error. Scheduler parallelism remains at its initial setting until actual build load is observed.

Completed `.setup.json` intermediates are deleted only after a newer successful Lake trace and nonempty `.olean` exist. Source and proof artifacts are retained. This controls disk usage in a project where large repeated import metadata would otherwise dominate storage.

## Viewing progress

- [Current status](results/status.json): running stage, exit codes, timestamps and resource samples.
- [Environment](results/environment.json): actual container limits and measurement scope.
- [Log tails](results/logtails): periodically updated recent output.
- [Evidence snapshots](results/evidence): versions, pinned source/dependency records, and the source/dependency prefetchers' explicit `result.json` provenance records.
- [Final logs](results/final): compressed full logs and hash manifests after the run ends.

The runner normally publishes every five minutes and on phase changes. If the network is unavailable, it retains server-side logs and retries. Final archive parts are at most 40 MiB; concatenate parts in manifest order, verify their SHA-256 values, and gunzip. A setup-only or statement-only success does not validate the proof. Stronger independent-checker results, if performed, will be reported separately.

## Reproduction

Install the prerequisites listed in the bootstrap script, configure a writable clone of this repository at `BASE/verification`, and provide a functioning network route. The included runner expects its loopback HTTPS proxy at `127.0.0.1:20080`; adapt that network setting for another environment.

```bash
python3 /root/autodl-tmp/rh-check/verification/scripts/run_all.py \
  --base /root/autodl-tmp/rh-check
```

The detached session uses `BASE/control/tmux.sock`. Server status and complete logs remain in `BASE/audit`, even if publishing is interrupted. The Windows relay accepts SSH configuration through standard input and does not write credentials or downloaded artifacts to disk.

## Provenance

The mathematical claim and original source belong to Levent Alpöge and the collaborators credited in his announcement. This repository contains verification tooling and observations and does not claim authorship of the proof. Successful compilation, axiom inspection, fresh replay, and verification by independently implemented kernels are distinct pieces of evidence.
