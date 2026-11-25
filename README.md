# Riemann Hypothesis · Lean Verification

[![Verification: 4/4 passed](https://img.shields.io/badge/verification-4%2F4%20passed-brightgreen)](results/status.json)
[![Lean: 4.33.1](https://img.shields.io/badge/Lean-4.33.1-blue)](results/environment.json)
[![Evidence: archived](https://img.shields.io/badge/evidence-archived-blue)](results/final/20261121T060158Z-a03969de/manifest.json)

**English** | [简体中文](README.zh-CN.md)

> **Verification completed.** The full project builds, the final proof matches Mathlib's Riemann Hypothesis statement, and a fresh Lean kernel replay passes.

This repository records a reproduction and audit of **Levent Alpöge's Lean formalization**, together with the scripts, server loader repair, and evidence needed to inspect this run.

**175,187 Lean modules · 5 final theorems · 4/4 verification gates passed**  
Completed **2026-11-25, 07:18 UTC+8** (2026-11-24, 23:18 UTC).

**[Results](results/status.json) · [Archived evidence](results/final/20261121T060158Z-a03969de/) · [Reproduction](#reproduce-the-run) · [Original source](https://github.com/CocoSgt/riemann-hypothesis-lean/tree/7cbf7a0c32fca1badf05931879a37c3f8a2b03b4)**

## What passed

| Check | Result | What was checked |
|---|---|---|
| [Project build](results/logtails/20261121T060158Z-a03969de/build-build-main.log.tail.txt) | **Passed** | Built `Main` and its full import closure, covering all 175,187 submission modules. |
| [Final theorem check](results/evidence/20261121T060158Z-a03969de/build-Check.lean) | **Passed** | Explicitly checked the proof against Mathlib's standard `RiemannHypothesis` and printed the dependencies of five final theorems. |
| [Axiom whitelist](results/logtails/20261121T060158Z-a03969de/build-validate-axioms.log.tail.txt) | **Passed** | All five final theorems depend only on the three allowed standard axioms. |
| [Fresh kernel replay](results/evidence/20261121T060158Z-a03969de/build-fresh-replay.command.sh) | **Passed** | Rechecked `Main` with `leanchecker --fresh -v Main`; this phase took **17 h 7 min**. |

The [axiom report](results/logtails/20261121T060158Z-a03969de/build-axioms.log.tail.txt) lists only `propext`, `Classical.choice`, and `Quot.sound` for each of the five final theorems: **no extra axiom or `sorryAx` dependency**. The [original proof source remained unchanged](results/evidence/20261121T060158Z-a03969de/build-final-source-status.txt).

The explicit statement check is short:

```lean
import Main
example : _root_.RiemannHypothesis := _root_.riemannHypothesis
```

## Inspect the evidence

- **[Final status](results/status.json):** completion state, exit codes, phase timestamps, and resource samples.
- **[Full archive and SHA-256 manifest](results/final/20261121T060158Z-a03969de/manifest.json):** 84 compressed files containing logs, commands, timings, provenance, and source diffs. The manifest records the size and checksum of every part.
- **[Environment](results/environment.json) and [toolchain provenance](results/evidence/20261121T060158Z-a03969de/build-toolchain-provenance.json):** the actual machine limits, toolchain selection, and binary hashes.
- **[Initial source audit](STATIC_AUDIT.md):** module counts, import graph, statement review, and early findings, recorded before the completed run.

The completed evidence snapshot is preserved at commit [`c441248e`](https://github.com/sunnyspot114514/riemann-lean-verification-20261121/tree/c441248e463b0acc0d385c59631b1d8ffd83842e). Run ID: `20261121T060158Z-a03969de`.

## Server and loader repair

This run completed on the following [recorded configuration](results/environment.json):

| Resource | Configuration |
|---|---|
| CPU | 32 logical CPUs |
| Memory | 60 GiB cgroup limit |
| Data volume | [500 GiB capacity](results/status.json) |
| Build concurrency | 8 Lake workers, one Lean thread per worker |
| Verification runtime | Lean 4.33.1 with the documented loader repair |

The server's fixed `vm.max_map_count=65530` caused failures when Lean mapped too many module files. We added a bounded loader with a disk-backed arena and cleaned up completed `.setup.json` intermediates. This allowed the build and fresh replay to finish on the rented server.

The loader repair passed **[29/29 regression cases](results/loader-fix/20261121T060024Z-523a39ce/test-report.json)**. The [build manifest](results/evidence/20261121T060158Z-a03969de/build-loader-build-manifest.json) records unchanged kernel archive objects. See the [patch](patches/lean-4.33.1-bounded-mmap-arena.patch), [design and checks](patches/ARENA-NOTES.md), and [original failure diagnosis](results/diagnostics/20261121-map-limit/README.md) for details.

## Reproduce the run

The repository includes the full workflow:

| Entry point | Purpose |
|---|---|
| [`scripts/bootstrap.sh`](scripts/bootstrap.sh) | Fetch and check the pinned toolchain, source, dependencies, and Mathlib cache. |
| [`scripts/build_lean_loader.py`](scripts/build_lean_loader.py) / [`scripts/test_lean_loader.py`](scripts/test_lean_loader.py) | Build a separate repaired runtime and run the loader checks. |
| [`scripts/run_all.py`](scripts/run_all.py) | Run the verification stages, sample resources, and publish evidence. |
| [`scripts/clean_completed_setups.py`](scripts/clean_completed_setups.py) | Remove completed setup intermediates during a large build. |

<details>
<summary><strong>Recorded launch configuration</strong></summary>

Prepare the pinned source and toolchains first, including the separate `lean-4.33.1-loader-v2` installation and its build manifest. The runner uses the original server directory layout, `tmux`, a local HTTP proxy at `127.0.0.1:20080`, and a Git remote for evidence publication. Configure the paths, networking, and publication destination for your server before launching.

```bash
python3 /root/autodl-tmp/rh-check/verification/scripts/run_all.py \
  --base /root/autodl-tmp/rh-check \
  --toolchain-dir /root/autodl-tmp/rh-check/tools/lean-4.33.1-loader-v2 \
  --olean-arena-dir /root/autodl-tmp/rh-check/tmp/olean-arena \
  --mmap-regions 60000 \
  --lean-num-threads 8 \
  --resumed-from-run-id 20261120T184814Z-ebb611be
```

The final option records the interrupted run from which this verification resumed; use your own prior run ID when resuming. Omit it for a new run. Bootstrap prerequisites and exact recorded commands are available in the scripts and [evidence directory](results/evidence/20261121T060158Z-a03969de/).

</details>

## Versions and credit

| Component | Pinned version |
|---|---|
| Proof source, mirrored by CocoSgt | [`7cbf7a0c32fca1badf05931879a37c3f8a2b03b4`](https://github.com/CocoSgt/riemann-hypothesis-lean/tree/7cbf7a0c32fca1badf05931879a37c3f8a2b03b4) |
| Mathlib | [`0df444a360eaa60ab8c11dca51a86af692955474`](https://github.com/leanprover-community/mathlib4/tree/0df444a360eaa60ab8c11dca51a86af692955474) |
| Lean base release | [`4.33.1`](https://github.com/leanprover/lean4/releases/tag/v4.33.1), with the recorded loader patch |

The mathematical formalization is **Levent Alpöge's work**; see the [author's announcement](https://alpo.ge/remark.pdf). This repository contributes the verification workflow, platform adaptation, and public run evidence.

## Scope and limitations

- **Runtime:** the successful run uses the documented loader repair. The original proof source and kernel archive objects were unchanged, but this is a relinked runtime, not a completed run using only stock release binaries. A full compiler bootstrap and the full upstream test suite were not performed; see the [loader build manifest](results/evidence/20261121T060158Z-a03969de/build-loader-build-manifest.json).
- **Checker:** fresh replay uses the same Lean kernel implementation. This run did not execute independent checkers such as `nanoda`, `con-leche`, or `con-ron`.
- **Reproduction and resources:** the result applies to the pinned source commit. The build resumed incrementally and used the official Mathlib cache. Timings are not a cold-cache benchmark; 500 GiB is the allocated data volume, not a measured peak or a universal minimum requirement.
- **Evidence:** the archive contains verification records, not the full compiled project or a standalone proof export. It documents the formal checks performed here; a human-readable mathematical exposition is outside this repository's scope.
