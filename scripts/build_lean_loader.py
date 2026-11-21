#!/usr/bin/env python3
"""Build a separately identified Lean 4.33.1 loader-patched release capsule.

This is a Linux x86_64 release relink, not a bootstrap or the upstream full test
suite. The upstream checkout and the original release remain read-only. Only
module.cpp, util/shell.cpp, and runtime members requiring a different TLS model
are compiled. All kernel objects and all Lean/stdlib objects remain upstream's
release objects. No theorem source or existing project build cache is touched.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

LEAN_COMMIT = "819816b2e0a3bf405af45ae5c7af2491d8f5bee6"
MIMALLOC_COMMIT = "94036de6fe20bfd8a73d4a6d142fcf532ea604d9"
LLVM_SHA256 = "e6fd0de8f5dfe0845ca92ce93f3dbe2a8b45eb14565c0646da4b671f1572ff6c"
LLVM_SIZE = 175775389
TLS_RE = re.compile(r"\bR_X86_64_TPOFF(?:32|64)?\b")
EVIDENCE_BINARIES = (
    "bin/lean", "bin/lake", "bin/leanchecker",
    "lib/lean/libleanshared.so", "lib/lean/libleancpp.a",
)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def tls_members(text):
    current = None
    members = set()
    for line in text.splitlines():
        match = re.match(r"^File: .*\(([^()]+)\)\s*$", line)
        if match:
            current = match.group(1)
        if TLS_RE.search(line):
            if not current:
                raise RuntimeError("TLS relocation was not associated with an archive member")
            members.add(current)
    return sorted(members)


def exported_symbols(text):
    return {line.split()[-1] for line in text.splitlines() if line.split()}


def archive_member_payloads(path):
    """Yield individual regular-ar members without merging duplicate basenames."""
    with Path(path).open("rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise RuntimeError(f"Expected a non-thin Unix archive: {path}")
        names = b""
        while True:
            header = f.read(60)
            if not header:
                break
            if len(header) != 60 or header[58:60] != b"`\n":
                raise RuntimeError(f"Malformed archive member in {path}")
            size = int(header[48:58].strip())
            name = header[:16].decode("ascii").strip()
            payload = f.read(size)
            if len(payload) != size:
                raise RuntimeError(f"Truncated archive member in {path}")
            if size % 2 and len(f.read(1)) != 1:
                raise RuntimeError(f"Missing archive padding in {path}")
            if name == "//":
                names = payload
                continue
            if name in ("/", "/SYM64/"):
                continue
            if name.startswith("#1/"):
                n = int(name[3:])
                name, payload = payload[:n].rstrip(b"\0").decode(), payload[n:]
            elif name.startswith("/"):
                n = int(name[1:])
                name = names[n:].split(b"\n", 1)[0].rstrip(b"/").decode()
            else:
                name = name.rstrip("/")
            yield name, payload


def archive_objects(path):
    """Hash members, retaining duplicate names and their ordering."""
    return [{"name": name, "sha256": hashlib.sha256(payload).hexdigest()}
            for name, payload in archive_member_payloads(path)]


def select_root_lean_object(archive, obj_dir, inspect_symbols):
    """Identify the root object by definition, not its possibly repeated name."""
    names = {"Lean.c.o", "Lean.c.o.export", "Lean.o", "Lean.o.export"}
    unique, candidates = {}, []
    for index, (name, payload) in enumerate(archive_member_payloads(archive)):
        if name not in names:
            continue
        digest = hashlib.sha256(payload).hexdigest()
        if digest not in unique:
            path = obj_dir / f"Lean.candidate-{len(unique)}.o"
            path.write_bytes(payload)
            defined = exported_symbols(inspect_symbols(path, len(unique)))
            unique[digest] = {"path": path, "defines_initialize_Lean": "initialize_Lean" in defined}
        candidates.append({"archive_index": index, "member_name": name, "sha256": digest,
                           "defines_initialize_Lean": unique[digest]["defines_initialize_Lean"]})
    roots = [(digest, info) for digest, info in unique.items() if info["defines_initialize_Lean"]]
    if len(roots) != 1:
        raise RuntimeError(f"Expected one distinct object defining initialize_Lean; candidates: {candidates}")
    digest, info = roots[0]
    destination = obj_dir / "Lean.root.o"
    shutil.copy2(info["path"], destination)
    return destination, {"selected_sha256": digest, "candidates": candidates}


def compare_archive(original, rebuilt, allowed):
    before, after = archive_objects(original), archive_objects(rebuilt)
    if len(before) != len(after) or [x["name"] for x in before] != [x["name"] for x in after]:
        raise RuntimeError("Archive member list/order changed unexpectedly")
    changes = [{"name": a["name"], "original_sha256": a["sha256"], "rebuilt_sha256": b["sha256"]}
               for a, b in zip(before, after) if a["sha256"] != b["sha256"]]
    if {x["name"] for x in changes} != set(allowed):
        raise RuntimeError(f"Unexpected changed archive members: {changes}")
    return {"member_count": len(before), "changed_members": changes,
            "unchanged_members_verified": len(before) - len(changes)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "official", "llvm", "llvm-archive", "mimalloc", "patch", "output", "work"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--jobs", type=int, default=4)
    args = p.parse_args()
    if not 1 <= args.jobs <= 16:
        p.error("--jobs must be between 1 and 16")
    for key, value in vars(args).items():
        if isinstance(value, Path):
            setattr(args, key, value.resolve())
    src, official, llvm, work, output = args.source, args.official, args.llvm, args.work, args.output
    if work.exists() or output.exists():
        raise RuntimeError("Use new --work and --output paths; existing capsules are never overwritten")
    if work == output or work in output.parents or output in work.parents:
        raise RuntimeError("Build evidence and installed capsule must be separate directories")
    for destination in (work, output):
        for readonly in (src, official, llvm, args.mimalloc):
            if destination == readonly or readonly in destination.parents:
                raise RuntimeError("Build/output paths must be outside read-only input trees")
    work.mkdir(parents=True)
    (work / "logs").mkdir()
    env = os.environ.copy()
    for key in ("LEAN_PATH", "LEAN_CC", "LEAN_SYSROOT", "LD_LIBRARY_PATH", "LD_PRELOAD"):
        env.pop(key, None)
    env["LEAN_SYSROOT"] = str(official)
    env["LC_ALL"] = "C"
    # The standalone LLVM archive keeps target C++ runtimes in a target-specific
    # directory. Use the identical copies already bundled with the official
    # release for CXX tools only. Never add lib/lean: that would make capsule
    # validation accidentally load the original Lean shared library.
    cxx_env = env.copy()
    cxx_env["LD_LIBRARY_PATH"] = os.pathsep.join((str(official / "lib"), str(llvm / "lib")))
    status = {"schema": 1, "state": "building", "started_utc": utc(), "build_kind": "release-relink"}

    def write_status():
        (work / "build-status.json").write_text(json.dumps(status, indent=2) + "\n")

    def run(name, argv, *, cwd=None, run_env=None, accepted=(0,)):
        argv = [str(x) for x in argv]
        (work / "logs" / (name + ".command.json")).write_text(
            json.dumps({"argv": argv, "cwd": str(cwd or work),
                        "selected_environment": {key: (run_env or env)[key]
                                                 for key in ("LD_LIBRARY_PATH", "LEAN_SYSROOT", "LEAN_MAX_MMAP_REGIONS", "LC_ALL")
                                                 if key in (run_env or env)}}, indent=2) + "\n")
        with (work / "logs" / (name + ".log")).open("wb") as log:
            proc = subprocess.run(argv, cwd=cwd or work, env=run_env or env,
                                  stdout=log, stderr=subprocess.STDOUT, check=False)
        (work / "logs" / (name + ".exit-code.txt")).write_text(str(proc.returncode) + "\n")
        data = (work / "logs" / (name + ".log")).read_text(errors="replace")
        if proc.returncode not in accepted:
            raise RuntimeError(f"{name} exited {proc.returncode}; see {work / 'logs' / (name + '.log')}")
        return data, proc.returncode

    def capture(name, argv, **kw):
        return run(name, argv, **kw)[0]

    write_status()
    try:
        if capture("source-head", ["git", "-C", src, "rev-parse", "HEAD"]).strip() != LEAN_COMMIT:
            raise RuntimeError("Source is not the pinned official Lean 4.33.1 commit")
        if capture("source-status", ["git", "-C", src, "status", "--porcelain=v1", "--untracked-files=all"]).strip():
            raise RuntimeError("Source checkout must be pristine; the patch is applied only inside the capsule")
        if capture("mimalloc-head", ["git", "-C", args.mimalloc, "rev-parse", "HEAD"]).strip() != MIMALLOC_COMMIT:
            raise RuntimeError("mimalloc must be the exact v2.2.3 commit used by upstream")
        if capture("mimalloc-status", ["git", "-C", args.mimalloc, "status", "--porcelain=v1", "--untracked-files=all"]).strip():
            raise RuntimeError("mimalloc checkout must be pristine")
        if args.llvm_archive.stat().st_size != LLVM_SIZE or sha256(args.llvm_archive) != LLVM_SHA256:
            raise RuntimeError("LLVM package does not match the official 22.1.4 release asset")
        for path in (llvm / "bin/clang++", official / "bin/clang", official / "bin/llvm-ar"):
            if not path.is_file():
                raise RuntimeError(f"Missing compiler/archive tool: {path}")
        capture("cxx-version", [llvm / "bin/clang++", "--version"], run_env=cxx_env)
        original_version = capture("original-version", [official / "bin/lean", "--version"]).strip()
        if "version 4.33.1," not in original_version:
            raise RuntimeError("Original Lean is not release 4.33.1")
        if capture("original-githash", [official / "bin/lean", "--githash"]).strip() != LEAN_COMMIT:
            raise RuntimeError("Original Lean githash does not match the pinned release")
        if sha256(official / "include/lean/mimalloc.h") != sha256(args.mimalloc / "include/mimalloc.h"):
            raise RuntimeError("Bundled mimalloc header differs from the pinned source")
        originals = {name: sha256(official / name) for name in EVIDENCE_BINARIES}
        archives = {name: official / "lib/lean" / name for name in
                    ("libLean.a", "libInit.a", "libStd.a", "libleancpp.a", "libleanrt.a")}
        for path in archives.values():
            if not path.is_file() or path.is_symlink():
                raise RuntimeError(f"Expected a regular official archive: {path}")
        archive_hashes = {name: sha256(path) for name, path in archives.items()}
        ar = official / "bin/llvm-ar"
        members = {name: capture("members-" + name, [ar, "t", path]).splitlines()
                   for name, path in archives.items()}
        if members["libleancpp.a"].count("module.cpp.o") != 1:
            raise RuntimeError("Expected exactly one module.cpp.o in the official C++ archive")
        rt_relocations = capture("runtime-original-relocations", ["readelf", "-rW", archives["libleanrt.a"]])
        rt_members = tls_members(rt_relocations)
        if not rt_members:
            raise RuntimeError("Expected local-exec runtime TLS members in this release; review unexpected layout")
        (work / "runtime-tls-members.json").write_text(json.dumps(rt_members, indent=2) + "\n")

        # Only the reviewable patch is applied. Upstream source/build/stage0 stay read-only.
        patch_root = work / "patch-source"
        patched_module = patch_root / "src/library/module.cpp"
        patched_module.parent.mkdir(parents=True)
        shutil.copy2(src / "src/library/module.cpp", patched_module)
        patch_text = args.patch.read_text()
        headers = re.findall(r"^\+\+\+ ([^\t\n]+)", patch_text, flags=re.MULTILINE)
        if headers != ["b/src/library/module.cpp"]:
            raise RuntimeError("The patch must affect only src/library/module.cpp")
        run("patch-check", ["git", "apply", "--check", args.patch], cwd=patch_root)
        run("patch-apply", ["git", "apply", args.patch], cwd=patch_root)
        shutil.copy2(args.patch, work / "loader-patch.diff")
        private_include = work / "include"
        private_include.mkdir()
        (private_include / "githash.h").write_text(
            '// Release compatibility identity; loader modification is separately recorded.\n'
            f'#define LEAN_GITHASH "{LEAN_COMMIT}"\n')
        obj_dir = work / "objects"
        obj_dir.mkdir()
        lean_root_object, root_object_evidence = select_root_lean_object(
            archives["libLean.a"], obj_dir,
            lambda path, index: capture("root-candidate-symbols-" + str(index), ["nm", "-g", "--defined-only", path]))
        (work / "root-lean-object.json").write_text(json.dumps(root_object_evidence, indent=2) + "\n")
        common = [llvm / "bin/clang++", "-std=c++20", "-stdlib=libc++", "-O3", "-DNDEBUG",
                  '-DLEAN_BUILD_TYPE="Release"', "-DLEAN_EXPORTING", "-DLEAN_MMAP",
                  "-DLEAN_MULTI_THREAD", "-DLEAN_USE_GMP", "-DLEAN_CHECK_OLEAN_VERSION", "-D__CLANG__",
                  "-ffp-contract=off", "-fPIC", "-ftls-model=initial-exec",
                  "-fvisibility=hidden", "-fvisibility-inlines-hidden",
                  "-I", official / "include", "-I", src / "src", "-I", private_include]
        compile_jobs = [("module.cpp.o", patched_module, []),
                        ("shell.cpp.o", src / "src/util/shell.cpp", [])]
        for member in rt_members:
            if members["libleanrt.a"].count(member) != 1:
                raise RuntimeError(f"Ambiguous runtime archive member: {member}")
            if member == "static.c.o":
                source_file = args.mimalloc / "src/static.c"
                extra = ["-x", "c++", "-I", args.mimalloc / "include",
                         "-DMI_SHARED_LIB", "-DMI_SHARED_LIB_EXPORT", "-DMI_WIN_NOREDIRECT",
                         "-DMI_SECURE=0", "-Wno-unused-function", "-Wno-deprecated"]
            else:
                source_name = member.removesuffix(".o")
                candidates = list((src / "src/runtime").rglob(source_name))
                if len(candidates) != 1:
                    raise RuntimeError(f"Cannot locate runtime source for {member}: {candidates}")
                source_file, extra = candidates[0], []
            compile_jobs.append((member, source_file, extra))

        def compile_one(item):
            member, path, extra = item
            run("compile-" + member, common + extra + ["-c", path, "-o", obj_dir / member], run_env=cxx_env)
            return {"member": member, "source_sha256": sha256(path), "object_sha256": sha256(obj_dir / member)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            rebuilt_objects = list(pool.map(compile_one, compile_jobs))
        cpp_archive, rt_archive = work / "libleancpp.a", work / "libleanrt_initial-exec.a"
        shutil.copy2(archives["libleancpp.a"], cpp_archive)
        shutil.copy2(archives["libleanrt.a"], rt_archive)
        run("archive-cpp", [ar, "rcs", cpp_archive, obj_dir / "module.cpp.o"])
        run("archive-runtime", [ar, "rcs", rt_archive] + [obj_dir / member for member in rt_members])
        archive_comparison = {
            "libleancpp.a": compare_archive(archives["libleancpp.a"], cpp_archive, {"module.cpp.o"}),
            "libleanrt_initial-exec.a": compare_archive(archives["libleanrt.a"], rt_archive, rt_members),
        }
        (work / "archive-object-comparison.json").write_text(json.dumps(archive_comparison, indent=2) + "\n")
        new_relocations = capture("runtime-new-relocations", ["readelf", "-rW", rt_archive])
        if TLS_RE.search(new_relocations):
            raise RuntimeError("The relink runtime still contains local-exec TLS relocations")
        # Read the actual release's bundled-toolchain linker flags, rather than
        # substituting host GMP/libuv/OpenSSL/C++ runtimes.
        flags_program = work / "LinkFlags.lean"
        flags_program.write_text('import Lean.Compiler.FFI\n'
            'def main (args : List String) : IO Unit := do\n'
            '  let root : System.FilePath := args.headD ""\n'
            '  for arg in Lean.Compiler.FFI.getInternalLinkerFlags root do\n'
            '    IO.println arg\n')
        internal_flags = capture("release-internal-linker-flags",
                                 [official / "bin/lean", "--run", flags_program, official]).splitlines()
        if not internal_flags or not any(str(official / "lib") in x for x in internal_flags):
            raise RuntimeError("Could not obtain the official bundled linker configuration")
        linked_so = work / "libleanshared.so"
        link = [official / "bin/clang", "-shared", "-o", linked_so, "-fuse-ld=lld",
                "-Wl,-Bsymbolic", "-Wl,--gc-sections", "-Wl,-z,defs", "-Wl,-rpath,$ORIGIN/..:$ORIGIN",
                lean_root_object, obj_dir / "shell.cpp.o", "-Wl,--start-group",
                archives["libInit.a"], archives["libStd.a"], archives["libLean.a"], cpp_archive,
                rt_archive, official / "lib/libc++.a", official / "lib/libc++abi.a",
                "-Wl,--end-group"] + internal_flags + ["-pthread", "-ldl", "-lrt", "-lm"]
        run("relink-shared", link)
        capture("relinked-elf", ["readelf", "-dW", linked_so])
        old_symbols = exported_symbols(capture("symbols-original", ["nm", "-D", "--defined-only", official / "lib/lean/libleanshared.so"]))
        new_symbols = exported_symbols(capture("symbols-patched", ["nm", "-D", "--defined-only", linked_so]))
        missing = sorted(old_symbols - new_symbols)
        (work / "export-symbol-comparison.json").write_text(json.dumps(
            {"old_count": len(old_symbols), "new_count": len(new_symbols), "missing": missing,
             "added": sorted(new_symbols - old_symbols)}, indent=2) + "\n")
        if missing:
            raise RuntimeError(f"Relink removed {len(missing)} original exported symbols; capsule not installed")

        # A separate full copy avoids modifying in-use files or the official release.
        output.parent.mkdir(parents=True, exist_ok=True)
        run("copy-release", ["cp", "-a", "--reflink=auto", official, output])
        for source_file, relative in ((linked_so, "lib/lean/libleanshared.so"), (cpp_archive, "lib/lean/libleancpp.a")):
            destination = output / relative
            temporary = destination.with_name(destination.name + ".loader-new")
            shutil.copy2(source_file, temporary)
            os.replace(temporary, destination)
        shutil.copy2(args.patch, output / "loader-patch.diff")
        shutil.copy2(Path(__file__).resolve(), output / "build_lean_loader.py")
        selected_env = env.copy()
        selected_env["LEAN_SYSROOT"] = str(output)
        selected_env["PATH"] = str(output / "bin") + os.pathsep + env.get("PATH", "")
        selected_env["LEAN_MAX_MMAP_REGIONS"] = "0"
        if capture("patched-version", [output / "bin/lean", "--version"], run_env=selected_env).strip() != original_version:
            raise RuntimeError("Patched capsule changed the Lean release compatibility version")
        if capture("patched-githash", [output / "bin/lean", "--githash"], run_env=selected_env).strip() != LEAN_COMMIT:
            raise RuntimeError("Patched capsule changed the Lean release compatibility githash")
        capture("patched-lake-version", [output / "bin/lake", "--version"], run_env=selected_env)
        if {name: sha256(official / name) for name in EVIDENCE_BINARIES} != originals:
            raise RuntimeError("The original release changed during this build")
        manifest = {
            "schema": 1, "kind": "loader-patched", "build_kind": "release-relink",
            "base_lean_commit": LEAN_COMMIT, "lean_version": "4.33.1",
            "original_toolchain_dir": str(official), "patch_sha256": sha256(args.patch),
            "patch_path": "loader-patch.diff", "build_script_sha256": sha256(Path(__file__).resolve()),
            "binaries": {name: sha256(output / name) for name in EVIDENCE_BINARIES},
            "original_binaries": originals, "original_archives": archive_hashes,
            "llvm_archive_sha256": LLVM_SHA256, "mimalloc_commit": MIMALLOC_COMMIT,
            "cxx_tool_environment": {"LD_LIBRARY_PATH": cxx_env["LD_LIBRARY_PATH"]},
            "rebuilt_objects": rebuilt_objects, "runtime_tls_members_recompiled": rt_members,
            "archive_object_comparison": archive_comparison,
            "root_lean_object": root_object_evidence,
            "kernel_objects": "unchanged official archive members; only module.cpp.o replaced in libleancpp.a",
            "stdlib_objects": "unchanged official libLean.a/libInit.a/libStd.a",
            "full_bootstrap_performed": False, "upstream_full_test_suite_performed": False,
            "validation": {"exported_symbols_preserved": True, "release_version_and_githash_preserved": True,
                           "kernel_negative_and_region_regressions": "required-separate-test_lean_loader.py",
                           "shape_win": "not-run-by-build-script", "main_and_final_gates": "not-run-by-build-script"},
            "build_evidence_dir": str(work), "completed_utc": utc(),
        }
        (output / "loader-build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (work / "loader-build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        status.update(state="complete", completed_utc=utc(), output=str(output))
        write_status()
        print(json.dumps({"state": "complete", "toolchain": str(output), "manifest": str(output / "loader-build-manifest.json")}))
    except BaseException as exc:
        status.update(state="failed", completed_utc=utc(), error=str(exc))
        write_status()
        raise


if __name__ == "__main__":
    main()
