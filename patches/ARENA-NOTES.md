# Lean 4.33.1 bounded loader and optional disk arena

This is a local loader resource patch for the RH build. It does not change project proofs, the kernel, the `.olean` file format, the serialized `CompactedRegion` layout, or the existing region relocation algorithm. It is a modified runtime and must be identified separately from the official Lean release in all build evidence.

## Source identity

- Official Lean commit: `819816b2e0a3bf405af45ae5c7af2491d8f5bee6` (`v4.33.1`).
- Official `src/library/module.cpp` Git blob: `be31567876a7965f01e6ff17f19709298b70caa8`.
- Official file SHA256: `a78bb4583a9746dea78570c1885500356da9bd1ecebdcb199645cf42709d890d`.
- `bounded-mmap.patch` is the retained first version, SHA256 `b774e1530d98d2cba8a7e30c09a991cce56963c6530dcf477dee6d6f1d1e12ce`.
- `bounded-mmap-arena.patch` is a complete patch against the official source, not an incremental patch on the first version. Its frozen SHA256 is `b478352b82fa6f095ff1a9f5362f060780ee155eb95ddee5ae4a4932d2a8a450`.
- Resulting arena `module.cpp` SHA256: `f132bed13a5d461174a95cc91c24575c4ceec2978274c9883e763939e63cb2d2`.

`git apply --check` against the exact official file passed. The file header and serialization code, input header validation, v3 parsing, and the `region_reader` invocation remain unchanged. Independent static review compared those portions with the official source.

## Controls and allocation policy

`LEAN_MAX_MMAP_REGIONS` is a strict unsigned decimal count of original per-file mapping attempts. It defaults to `50000` on Linux. The counter is atomic and monotonically decreases; failed attempts and subsequently freed mappings do not return slots. Setting it to `0` forces all imports through the existing copy-and-relocate path and is useful for regression tests. This is a file-mapping budget, not a guarantee about the process's total VMA count.

With `LEAN_OLEAN_ARENA_DIR` unset or empty, the Linux copy fallback uses Lean's bundled mimalloc allocator when available. This avoids one ordinary glibc mapping per copied region, but the copied contents consume anonymous memory and very large allocations may still create individual mappings.

With `LEAN_OLEAN_ARENA_DIR` set to an existing writable directory, ordinary v2 `.olean` copies use the disk arena. Use a task-owned directory on the server's data filesystem, not a RAM-backed filesystem. The loader does not create the directory. It creates an unpredictable mode-0600 temporary file with close-on-exec already set, immediately unlinks it, reserves disk space with `posix_fallocate`, and maps the file with `MAP_SHARED`. Each segment is 256 MiB, or a page-rounded larger size when one region requires more. The descriptor is closed after mapping. Regions share a segment through an aligned bump allocator.

These private-to-the-process files allow dirty relocated pages to be written back to disk and reclaimed, while ordinary early imports retain their original shared file mappings. Active files are unlinked, so disk allocation must be monitored with filesystem free space (`df`), not only directory size (`du`). Each process has its own arena files; disk usage can multiply with compiler concurrency. All mappings and unlinked files are reclaimed at process exit.

V3 incremental snapshots always retain the heap fallback. They may contain mutable references and closures and are outside the file-arena scope. Original successful file mappings remain copy-on-write for both formats.

## Ownership and error handling

Every Linux copy allocation has a private `max_align_t`-aligned prefix outside the copied file bytes. It records an arena owner or a null owner for a heap allocation. This metadata is never serialized. `CompactedRegion.isMemoryMapped` remains false for both kinds of copies; it still means an original per-file mapping.

One mutex protects the current segment, bump offset, and live-region counts. Creating a replacement segment is transactional: failure leaves the prior current segment intact. Each successful allocation adds one live reference. A final free clears the current pointer when applicable, unregisters the segment's sanitizer root, unmaps the whole segment, and deletes its metadata. The buffer's owner is copied before unmapping, and the prefix is not read afterward. Fully occupied old segments remain owned by their live region prefixes until their last free.

The loader retains ownership through a C++ RAII guard until the read and relocation finish. Seek failure, read failure, short read, or a relocation exception releases the allocation. `posix_fallocate` errors use its returned error code, including ENOSPC, rather than stale `errno`. The allocation checks header addition, alignment rounding, and file-offset range for overflow. Arena mappings are registered as whole LeakSanitizer roots; heap copies use the existing ignored-allocation treatment.

The arena is intended for standalone compiler processes and normal spawn-then-exec workflows. A PID change resets allocator state so a fork child cannot reuse the parent's bump offset or inherited mutex. This does **not** provide arbitrary fork/copy-on-write semantics for already shared region contents. Do not describe this implementation as fully fork-transparent.

## Validation status and required deployment checks

`allocator-smoke.py` extracts the exact allocator helper source and compiles a standalone C++ test. Lean support types and `mi_malloc`/`mi_free` are stubbed with standard equivalents, so this is an allocator-logic check, not a Lean or mimalloc integration test. It passed alignment, heap separation, last-reference release, reallocation after current-segment release, oversized regions, simulated ENOSPC and mmap failure, exception cleanup, eight-thread allocation/free, PID-reset behavior, and descriptor/mapping-count recovery.

The same helper smoke passed AddressSanitizer and UndefinedBehaviorSanitizer. LeakSanitizer's process scan is unsupported in the scratch execution environment, so that run used `ASAN_OPTIONS=detect_leaks=0`; no successful leak scan is claimed.

Before resuming the real build, the full patched Lean binary must pass the regression harness: accepted valid theorem, rejected invalid theorem, direct kernel acceptance/rejection, valid artifact compatibility, corrupted header rejection, explicit `freeRegions` cycles, and forced-zero-budget imports with observed arena mappings. The exact previously failing ShapeWin command must then complete successfully while recording actual arguments, VMA/RSS peaks, arena mappings, and exit status. The original failure evidence must remain available. The first anonymous-only patch's successful ShapeWin run does not establish arena correctness.

Even with those checks, final `Main` and the fresh replay remain required. The patch reduces loader mappings and makes copied region pages reclaimable; it does not guarantee that all later elaboration, kernel, or final import memory requirements fit a 60 GiB instance. Start with limited concurrency and measure both cgroup memory and data-filesystem free space.
