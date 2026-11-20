# Riemann Hypothesis Lean verification

Status: **setup in progress; no proof verification result yet**.

This repository records an attempt to independently compile and check the published Lean formalization claiming the Riemann Hypothesis and GRH for Dirichlet L-functions. It does not assert that the claim is correct.

## Source under test

- Mirror: https://github.com/CocoSgt/riemann-hypothesis-lean
- Pinned commit: `7cbf7a0c32fca1badf05931879a37c3f8a2b03b4`
- Lean: official `leanprover/lean4:v4.33.1`
- Mathlib: official `0df444a360eaa60ab8c11dca51a86af692955474`
- Author announcement: https://alpo.ge/remark.pdf

The source checkout will be kept separate from verification scripts and logs. Compilation will reuse the official Mathlib build cache, and rebuild the submission's own modules.

## Execution environment

Ubuntu 22.04.5 LTS, x86_64, AMD EPYC 9654 host. The rented container is assigned **32 logical CPUs and 60 GiB RAM**, with a **500 GiB data volume**. Host-wide memory reported by `free` is not the container's usable memory limit.

All project sources, tools, build products and logs are stored on the rented server. The operator's Windows computer is used only to control SSH and access existing GitHub credentials.

## Planned evidence

1. Pinned source identity and unchanged-source checks.
2. Complete `lake build Main` result, logs and exit status. All 175,187 submission modules are in its import closure at the pinned commit.
3. Exact axiom dependencies of all five final theorems and an explicit standard-RH type check.
4. `leanchecker --fresh Main` replay, if compilation and the axiom checks succeed. This uses the same Lean kernel and is **not an independently implemented verifier**.
5. Any stronger independent-checker runs will be recorded separately with their exact versions and outcomes.

Setup or build failures will be published as failures, with the relevant stage and logs. A successful source scan or statement-only build is not a successful proof check.

## Provenance

The mathematical claim and original source belong to Levent Alpöge and the collaborators credited in his announcement. This repository contains verification tooling and evidence; it does not claim authorship of the proof.
