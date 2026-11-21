#!/usr/bin/env python3
"""Run loader regressions and recorded heavy commands in the task's audit tree.

Recorded commands are JSON objects with argv, cwd, and env including LEAN_PATH.
--shape-build-log can instead extract the first recorded ShapeWin trace command.
Original heavy failures are preserved as evidence and are never repeated here.
Recorded commands are passed directly to Popen; no shell evaluates them.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid


BASE_COMMIT = "819816b2e0a3bf405af45ae5c7af2491d8f5bee6"
CONTROL_KEYS = ("LEAN_PATH", "LEAN_SYSROOT", "LEAN_NUM_THREADS", "LEAN_ABORT_ON_PANIC",
                "LEAN_MAX_MMAP_REGIONS", "LEAN_OLEAN_ARENA_DIR", "LEAN_LOADER_TEST_PHASE_FILE")
SAMPLE_SECONDS = 0.020


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def read_text(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def strip_outputs(argv):
    """Remove artifact output switches, preserving all other arguments."""
    result, removed = [], []
    i = 0
    separate = {"-o", "-i", "-c", "--olean", "--ilean", "--c"}
    while i < len(argv):
        arg = argv[i]
        if arg in separate:
            if i + 1 == len(argv):
                raise ValueError("Output option has no filename: " + arg)
            removed.append(argv[i:i + 2])
            i += 2
            continue
        if (any(arg.startswith(prefix) for prefix in ("--olean=", "--ilean=", "--c="))
                or (len(arg) > 2 and arg[:2] in {"-o", "-i", "-c"} and not arg.startswith("--"))):
            removed.append([arg])
        else:
            result.append(arg)
        i += 1
    return result, removed


def load_command(path, official, source):
    spec = json.loads(path.read_text())
    if not isinstance(spec.get("argv"), list) or not spec["argv"] or not all(isinstance(x, str) for x in spec["argv"]):
        raise ValueError("Recorded command needs a nonempty string argv.")
    if Path(spec["argv"][0]).resolve() != (official / "bin/lean").resolve():
        raise ValueError("Recorded command must name the original Lean executable.")
    if Path(spec.get("cwd", "")).resolve() != source:
        raise ValueError("Recorded heavy command cwd must be BASE/source.")
    env = spec.get("env", {})
    if not isinstance(env, dict) or not isinstance(env.get("LEAN_PATH"), str):
        raise ValueError("Recorded command must include its exact LEAN_PATH.")
    if any(key not in CONTROL_KEYS or not isinstance(value, str) for key, value in env.items()):
        raise ValueError("Only explicit Lean control variables are accepted in recorded env.")
    arguments, removed = strip_outputs(spec["argv"])
    if not any(arg.endswith(".lean") for arg in arguments[1:]):
        raise ValueError("Recorded command must read a Lean source file.")
    return {"original": spec, "argv_without_outputs": arguments,
            "removed_output_arguments": removed, "input_json_sha256": sha(path)}


def command_from_log(log, source, target):
    with log.open(errors="replace") as stream:
        for line in stream:
            if line.startswith("trace: .> ") and "/ShapeWin.lean " in line:
                tokens = shlex.split(line[len("trace: .> "):])
                environment = {}
                while tokens and "=" in tokens[0] and not tokens[0].startswith("/"):
                    key, value = tokens.pop(0).split("=", 1)
                    environment[key] = value
                write_json(target, {"argv": tokens, "cwd": str(source), "env": environment})
                return
    raise ValueError("No recorded ShapeWin trace command was found in the build log.")


def sample_process(pid, arena):
    status = read_text(f"/proc/{pid}/status")
    if not status:
        return None
    value = {"monotonic_seconds": time.monotonic(), "pid": pid}
    for name in ("VmRSS", "VmHWM", "RssAnon", "RssFile", "RssShmem", "VmSize"):
        match = re.search(r"(?m)^" + name + r":\s+(\d+)\s+kB", status)
        value[name + "_bytes"] = int(match.group(1)) * 1024 if match else None
    try:
        mappings = Path(f"/proc/{pid}/maps").read_bytes()
        value["map_count"] = mappings.count(b"\n")
        value["arena_map_count"] = mappings.count(os.fsencode(str(arena))) if arena else 0
    except OSError:
        value["map_count"] = None
        value["arena_map_count"] = None
    return value


def stop_test_process(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return proc.wait()


class Harness:
    def __init__(self, args):
        self.args = args
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run = args.base / "audit/loader-tests" / (stamp + "-" + uuid.uuid4().hex[:8])
        self.run.mkdir(parents=True, mode=0o700)
        self.fixtures = self.run / "fixtures"
        self.fixtures.mkdir()
        fixture_source = Path(__file__).resolve().parent / "loader-fixtures"
        for path in sorted(fixture_source.glob("*.lean")):
            shutil.copyfile(path, self.fixtures / path.name)
        if len(list(self.fixtures.glob("*.lean"))) != 6:
            raise RuntimeError("The six published loader fixtures are required beside this script.")
        self.arena = args.arena_dir
        if self.arena is None:
            self.arena = self.run / "arena"
        elif not self.arena.is_absolute():
            self.arena = self.run / self.arena
        self.arena = self.arena.resolve()
        if args.base not in self.arena.parents:
            raise ValueError("Arena directory must remain beneath the task base.")
        self.arena.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.arena.stat().st_mode & 0o077:
            raise ValueError("Arena directory must have private 0700 permissions.")
        filesystem = subprocess.check_output(["stat", "-f", "-c", "%T", str(self.arena)], text=True).strip()
        if filesystem in {"tmpfs", "ramfs"}:
            raise ValueError("The file-backed arena test needs a non-tmpfs data filesystem.")
        self.report = {
            "schema": 1, "started_utc": utc(), "state": "running", "passed": False,
            "run_directory": str(self.run), "base_lean_commit": BASE_COMMIT,
            "official_toolchain": str(args.official_toolchain),
            "patched_toolchain": str(args.patched_toolchain),
            "arena_directory": str(self.arena), "arena_filesystem": filesystem,
            "mmap_budget": args.mmap_budget,
            "sampler": {"requested_interval_seconds": SAMPLE_SECONDS,
                        "scope": "Direct Lean process PID, not a strace/time wrapper; sampled peaks are lower bounds."},
            "vm_max_map_count": read_text("/proc/sys/vm/max_map_count").strip(),
            "harness_sha256": sha(Path(__file__)),
            "fixture_sha256": {p.name: sha(p) for p in sorted(self.fixtures.glob("*.lean"))},
            "baseline_evidence": [], "cases": [],
            "original_heavy_baseline": "Not repeated; inspect copied baseline evidence.",
            "scope": "Loader compatibility, rejection, freeing, and recorded-module replay. Full RH checks are separate."
        }
        manifest = args.patched_toolchain / "loader-build-manifest.json"
        self.report["loader_manifest_sha256"] = sha(manifest)
        shutil.copyfile(manifest, self.run / manifest.name)
        evidence = self.run / "baseline-evidence"
        evidence.mkdir()
        for index, path in enumerate(args.baseline_evidence):
            path = path.resolve()
            if not path.is_file():
                raise ValueError("Baseline evidence is not a regular file: " + str(path))
            dest = evidence / (str(index).zfill(3) + "-" + path.name)
            shutil.copyfile(path, dest)
            self.report["baseline_evidence"].append({
                "original_path": str(path), "copied_path": str(dest.relative_to(self.run)),
                "bytes": dest.stat().st_size, "sha256": sha(dest)})
        self.save()
        print("Loader tests:", self.run, flush=True)

    def save(self):
        write_json(self.run / "test-report.json", self.report)

    def environment(self, toolchain, budget=None, arena=None, lean_path=None, recorded=None):
        environment = os.environ.copy()
        for key in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
            environment.pop(key, None)
        for key in CONTROL_KEYS:
            environment.pop(key, None)
        if recorded:
            environment.update(recorded)
        environment.update(PATH=str(toolchain / "bin") + os.pathsep + environment.get("PATH", ""),
                           LEAN_SYSROOT=str(toolchain), LEAN_NUM_THREADS="1", LEAN_ABORT_ON_PANIC="1")
        if lean_path is not None:
            environment["LEAN_PATH"] = str(lean_path)
        if budget is not None:
            environment["LEAN_MAX_MMAP_REGIONS"] = str(budget)
        else:
            environment.pop("LEAN_MAX_MMAP_REGIONS", None)
        if arena:
            environment["LEAN_OLEAN_ARENA_DIR"] = str(arena)
        else:
            environment.pop("LEAN_OLEAN_ARENA_DIR", None)
        return environment

    def case(self, name, argv, cwd, environment, *, reject=False, patterns=(), mapped_zero=False,
             free_cycles=False, extra=None):
        directory = self.run / name
        directory.mkdir()
        environment = dict(environment)
        if free_cycles:
            environment["LEAN_LOADER_TEST_PHASE_FILE"] = str(directory / "free-phase.txt")
        log = directory / "stdout.log"
        samples = directory / "resources.jsonl"
        controls = {key: environment.get(key) for key in CONTROL_KEYS}
        record = {"name": name, "started_utc": utc(), "argv": argv, "cwd": str(cwd),
                  "executable_sha256": sha(Path(argv[0])),
                  "controlled_environment": controls, "expected": "rejection" if reject else "success",
                  "cleared_environment": ["LD_LIBRARY_PATH", "LD_PRELOAD"],
                  "stdout": str(log.relative_to(self.run)), "resources": str(samples.relative_to(self.run)),
                  "timeout_seconds": self.args.timeout_seconds}
        if extra:
            record.update(extra)
        write_json(directory / "command.json", record)
        stop = threading.Event()
        peaks = {}
        phases = {}
        sampling = {"count": 0}
        arena = controls.get("LEAN_OLEAN_ARENA_DIR")
        start = time.monotonic()
        with log.open("wb") as output, samples.open("w", buffering=1) as sample_out:
            proc = subprocess.Popen(argv, cwd=cwd, env=environment, stdout=output,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            record["pid"] = proc.pid

            def monitor():
                while not stop.is_set():
                    begin = time.monotonic()
                    value = sample_process(proc.pid, arena)
                    if value:
                        phase_file = controls.get("LEAN_LOADER_TEST_PHASE_FILE")
                        phase = read_text(phase_file).strip() if phase_file else ""
                        if phase:
                            value["free_phase"] = phase
                            stats = phases.setdefault(phase, {"samples": 0, "arena_min": None, "arena_max": 0})
                            stats["samples"] += 1
                            maps = value.get("arena_map_count")
                            if maps is not None:
                                stats["arena_min"] = maps if stats["arena_min"] is None else min(stats["arena_min"], maps)
                                stats["arena_max"] = max(stats["arena_max"], maps)
                        sample_out.write(json.dumps(value) + "\n")
                        sampling["count"] += 1
                        for key, item in value.items():
                            if key not in {"monotonic_seconds", "pid", "free_phase"} and item is not None:
                                peaks[key] = max(peaks.get(key, 0), item)
                    stop.wait(max(0, SAMPLE_SECONDS - (time.monotonic() - begin)))

            watcher = threading.Thread(target=monitor, daemon=True)
            watcher.start()
            record["timed_out"] = False
            try:
                record["exit_code"] = proc.wait(timeout=self.args.timeout_seconds)
            except subprocess.TimeoutExpired:
                record["timed_out"] = True
                record["exit_code"] = stop_test_process(proc)
            except BaseException:
                stop_test_process(proc)
                raise
            finally:
                stop.set()
                watcher.join()
        record.update(finished_utc=utc(), elapsed_seconds=time.monotonic() - start,
                      sampled_peaks=peaks, sample_count=sampling["count"], stdout_sha256=sha(log))
        with log.open("rb") as stream:
            text = stream.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
        passed = not record["timed_out"] and (
            record["exit_code"] > 0 if reject else record["exit_code"] == 0)
        matches = {pattern: bool(re.search(pattern, text, re.IGNORECASE)) for pattern in patterns}
        passed = passed and all(matches.values())
        arena_zero = bool(arena) and controls.get("LEAN_MAX_MMAP_REGIONS") == "0" and not reject
        if arena_zero:
            record["arena_use_observed"] = peaks.get("arena_map_count", 0) > 0
            passed = passed and record["arena_use_observed"]
        if mapped_zero and not free_cycles:
            mapped = re.findall(r"number of memory-mapped modules:\s*(\d+)", text)
            imported = re.findall(r"number of imported modules:\s*(\d+)", text)
            record["memory_mapped_modules"] = [int(n) for n in mapped]
            passed = passed and bool(mapped) and all(int(n) == 0 for n in mapped)
            passed = passed and bool(imported) and all(int(n) > 0 for n in imported)
        if free_cycles:
            cycles = re.findall(r"FREE_CYCLE_OK (\d+) imported=(\d+) mapped=(\d+)", text)
            record["free_cycles"] = cycles
            passed = passed and [int(c[0]) for c in cycles] == [0, 1, 2]
            passed = passed and all(int(c[1]) > 0 for c in cycles)
            if mapped_zero:
                passed = passed and all(int(c[2]) == 0 for c in cycles)
            record["free_phase_samples"] = phases
            releases = []
            for cycle in range(3):
                loaded = phases.get(f"loaded:{cycle}", {}).get("arena_max")
                after = phases.get(f"after:{cycle}", {}).get("arena_min")
                releases.append(loaded is not None and after is not None and loaded > after)
            record["arena_last_reference_release_observed"] = any(releases)
            if arena_zero:
                passed = passed and any(releases)
        record.update(pattern_matches=matches, passed=bool(passed))
        write_json(directory / "result.json", record)
        self.report["cases"].append(record)
        self.save()
        print(("PASS " if passed else "FAIL ") + name + " " +
              json.dumps({"exit": record["exit_code"], "peaks": peaks}), flush=True)
        return bool(passed)

    def fixture_command(self, toolchain, name, run_main=False):
        options = [str(toolchain / "bin/lean"), "-j1", "--tstack=400000",
                   "--root=" + str(self.fixtures), "--stats"]
        if run_main:
            options.append("--run")
        return options + [str(self.fixtures / name)]

    def run_tests(self):
        official, patched = self.args.official_toolchain, self.args.patched_toolchain
        for label, toolchain in (("official", official), ("patched", patched)):
            if not self.case(label + "-version", [str(toolchain / "bin/lean"), "--version"],
                             self.fixtures, self.environment(toolchain),
                             patterns=(r"version 4\.33\.1,", BASE_COMMIT)):
                raise RuntimeError("Toolchain version does not match the recorded base commit.")
        valid = self.run / "artifact-valid"
        valid.mkdir()
        seed = self.fixture_command(official, "LoaderArtifact.lean")
        seed[1:1] = ["-o", str(valid / "LoaderArtifact.olean")]
        if not self.case("official-build-seed", seed, self.fixtures,
                         self.environment(official, lean_path=self.fixtures)):
            raise RuntimeError("The original toolchain could not produce the valid seed artifact.")

        configurations = [("official", official, None, None),
                          ("patched-default", patched, self.args.mmap_budget, self.arena),
                          ("patched-arena-zero", patched, 0, self.arena)]
        for label, toolchain, budget, arena in configurations:
            env = self.environment(toolchain, budget, arena, valid)
            zero = budget == 0
            self.case(label + "-good", self.fixture_command(toolchain, "LoaderGood.lean"),
                      self.fixtures, env, patterns=("GOOD_PROOF_OK",), mapped_zero=zero)
            self.case(label + "-bad-elaboration", self.fixture_command(toolchain, "LoaderBad.lean"),
                      self.fixtures, env, reject=True, patterns=(r"type mismatch", r"False", r"True"))
            self.case(label + "-kernel-rejection", self.fixture_command(toolchain, "LoaderKernelReject.lean"),
                      self.fixtures, env, patterns=("KERNEL_REJECTED_INVALID_DECL",), mapped_zero=zero)
            self.case(label + "-import-seed", self.fixture_command(toolchain, "LoaderImportArtifact.lean"),
                      self.fixtures, env, patterns=("ARTIFACT_IMPORT_OK",), mapped_zero=zero)
            self.case(label + "-free-regions", self.fixture_command(toolchain, "LoaderFree.lean", True),
                      self.fixtures, env, patterns=("FREE_REGIONS_OK",), mapped_zero=zero, free_cycles=True)
            self.case(label + "-checker", [str(toolchain / "bin/leanchecker"), "--fresh", "-v", "LoaderArtifact"],
                      self.fixtures, env)

        if self.args.test_anonymous_fallback:
            env = self.environment(patched, 0, None, valid)
            self.case("patched-anonymous-zero-good", self.fixture_command(patched, "LoaderGood.lean"),
                      self.fixtures, env, patterns=("GOOD_PROOF_OK",), mapped_zero=True)
            self.case("patched-anonymous-zero-free", self.fixture_command(patched, "LoaderFree.lean", True),
                      self.fixtures, env, patterns=("FREE_REGIONS_OK",), mapped_zero=True, free_cycles=True)

        for corruption, offset, value, expected in (
                ("bad-magic", 0, 0, "invalid header"),
                ("bad-version", 5, 255, "incompatible header")):
            damaged = self.run / ("artifact-" + corruption)
            shutil.copytree(valid, damaged)
            mutated = []
            for path in sorted(damaged.glob("LoaderArtifact.olean*")):
                data = bytearray(path.read_bytes())
                if data[:5] != b"olean" or len(data) < 7:
                    continue
                old = data[offset]
                data[offset] = value
                path.write_bytes(data)
                mutated.append({"file": path.name, "offset": offset, "before": old,
                                "after": value, "sha256": sha(path)})
            if not mutated:
                raise RuntimeError("No valid seed header was available to corrupt.")
            for label, toolchain, budget, arena in (configurations[0], configurations[2]):
                self.case(label + "-" + corruption,
                          self.fixture_command(toolchain, "LoaderImportArtifact.lean"), self.fixtures,
                          self.environment(toolchain, budget, arena, damaged),
                          reject=True, patterns=(expected,), extra={"mutated_artifacts": mutated})

        if not all(case["passed"] for case in self.report["cases"]):
            self.report["heavy_replay_skipped"] = "An earlier regression failed."
            self.save()
            return
        source = (self.args.base / "source").resolve()
        shape_json = self.args.shape_command_json
        if self.args.shape_build_log:
            shape_json = self.run / "shape-command-original.json"
            command_from_log(self.args.shape_build_log, source, shape_json)
        else:
            shutil.copyfile(shape_json, self.run / "shape-command-original.json")
        commands = [("shape", load_command(shape_json, official, source), True)]
        for index, path in enumerate(self.args.heavy):
            shutil.copyfile(path, self.run / f"heavy-{index}-command-original.json")
            commands.append((f"heavy-{index}", load_command(path, official, source), False))
        for label, command, both_budgets in commands:
            budgets = list(dict.fromkeys([0, self.args.mmap_budget])) if both_budgets else [self.args.mmap_budget]
            for budget in budgets:
                argv = list(command["argv_without_outputs"])
                argv[0] = str(patched / "bin/lean")
                env = self.environment(patched, budget, self.arena, recorded=command["original"]["env"])
                self.case(f"patched-{label}-budget-{budget}", argv, source, env,
                          extra={"original_command": command["original"],
                                 "removed_output_arguments": command["removed_output_arguments"],
                                 "input_json_sha256": command["input_json_sha256"],
                                 "artifact_outputs_removed": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-toolchain", type=Path, required=True)
    parser.add_argument("--patched-toolchain", type=Path, required=True)
    parser.add_argument("--base", type=Path, default=Path("/root/autodl-tmp/rh-check"))
    shape = parser.add_mutually_exclusive_group(required=True)
    shape.add_argument("--shape-command-json", type=Path)
    shape.add_argument("--shape-build-log", type=Path)
    parser.add_argument("--mmap-budget", type=int, default=50000)
    parser.add_argument("--arena-dir", type=Path)
    parser.add_argument("--baseline-evidence", type=Path, action="append", default=[])
    parser.add_argument("--heavy", type=Path, action="append", default=[])
    parser.add_argument("--test-anonymous-fallback", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()
    args.base = args.base.resolve()
    args.official_toolchain = args.official_toolchain.resolve()
    args.patched_toolchain = args.patched_toolchain.resolve()
    if args.official_toolchain == args.patched_toolchain:
        parser.error("Original and patched toolchains must be separate directories.")
    if args.mmap_budget < 0 or args.timeout_seconds <= 0:
        parser.error("The mmap budget must be non-negative and timeout must be positive.")
    harness = Harness(args)
    try:
        harness.run_tests()
        harness.report["passed"] = all(case["passed"] for case in harness.report["cases"])
        harness.report["state"] = "completed" if harness.report["passed"] else "failed"
    except BaseException as exc:
        harness.report.update(state="failed", passed=False, error=type(exc).__name__ + ": " + str(exc))
        print(harness.report["error"], file=sys.stderr, flush=True)
    finally:
        harness.report["finished_utc"] = utc()
        harness.save()
    print("Report:", harness.run / "test-report.json", flush=True)
    return 0 if harness.report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
