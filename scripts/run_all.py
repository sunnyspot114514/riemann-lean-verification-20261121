#!/usr/bin/env python3
"""Detach the pinned RH verification, monitor it, and publish an explicit result set.

Usage: python3 run_all.py --base /root/autodl-tmp/rh-check
The verification checkout and its SSH authentication must already be configured.
No credentials, source checkout, toolchain, or control files are added to Git.
"""

import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid


PIN = "7cbf7a0c32fca1badf05931879a37c3f8a2b03b4"
EXPECTED_CHECKS = ("build-main", "axioms", "validate-axioms", "fresh-replay")
CHUNK_BYTES = 40 * 1024 * 1024
EVIDENCE_PATTERNS = (
    "source.json", "dependencies-before-cache.json", "dependencies-after-cache.json",
    "release.sha256", "source-commit.txt", "lean-version.txt", "lake-version.txt",
    "final-source-status.txt", "build-config-sha256.txt", "lakefile-resource.toml",
    "resource-controls.txt", "Check.lean", "*.command.sh", "cgroup-start.txt", "cgroup-end.txt",
)
FINAL_PATTERNS = (
    "source-index*.txt", "*.time.txt", "*.exit-code.txt", "final-*.diff",
) + EVIDENCE_PATTERNS


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def read(path, default=""):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return default


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def numeric(text):
    try:
        return int(text)
    except (ValueError, TypeError):
        return None


def cpu_set_count(value):
    count = 0
    for part in value.split(","):
        if not part:
            continue
        bounds = part.split("-")
        count += int(bounds[-1]) - int(bounds[0]) + 1
    return count or None


def cgroup_paths():
    """Resolve this process's cgroups using mount roots, including containers."""
    memberships = {}
    for line in read("/proc/self/cgroup").splitlines():
        _, controllers, member = line.split(":", 2)
        for controller in controllers.split(","):
            memberships[controller] = member
    found = {}
    for line in read("/proc/self/mountinfo").splitlines():
        before, after = line.split(" - ", 1)
        left, right = before.split(), after.split()
        fs = right[0]
        if fs not in ("cgroup", "cgroup2"):
            continue
        root, mount = left[3], left[4]
        controllers = [""] if fs == "cgroup2" else right[2].split(",")
        for controller in controllers:
            if controller not in memberships:
                continue
            member = memberships[controller]
            # A cgroup namespace may expose '/' even when the mount root is deeper.
            rel = member[len(root):] if member.startswith(root.rstrip("/") + "/") else member
            path = Path(mount) / rel.lstrip("/")
            if not path.is_dir():
                path = Path(mount)
            found["v2" if fs == "cgroup2" else controller] = (path, Path(mount))
    return found


def ancestors(path, mount):
    while True:
        yield path
        if path == mount or path == path.parent or mount not in path.parents:
            break
        path = path.parent


class Resources:
    def __init__(self, base):
        self.base = base
        self.groups = cgroup_paths()
        self.observed_peak = 0
        self.initial_cpu = self.sample().get("cgroup_cpu_usage_seconds")

    def limits(self):
        quotas, memories, cpusets = [], [], []
        if "v2" in self.groups:
            for p in ancestors(*self.groups["v2"]):
                value = read(p / "cpu.max").split()
                if len(value) == 2 and value[0] != "max":
                    quotas.append(int(value[0]) / int(value[1]))
                memory = numeric(read(p / "memory.max"))
                if memory is not None:
                    memories.append(memory)
                cpus = read(p / "cpuset.cpus.effective")
                if cpus:
                    cpusets.append(cpu_set_count(cpus))
        else:
            if "cpu" in self.groups:
                for p in ancestors(*self.groups["cpu"]):
                    quota, period = numeric(read(p / "cpu.cfs_quota_us")), numeric(read(p / "cpu.cfs_period_us"))
                    if quota is not None and quota > 0 and period:
                        quotas.append(quota / period)
            if "memory" in self.groups:
                for p in ancestors(*self.groups["memory"]):
                    memory = numeric(read(p / "memory.limit_in_bytes"))
                    if memory is not None and memory < 2 ** 60:
                        memories.append(memory)
            if "cpuset" in self.groups:
                cpus = read(self.groups["cpuset"][0] / "cpuset.cpus")
                if cpus:
                    cpusets.append(cpu_set_count(cpus))
        affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
        effective = [v for v in quotas + cpusets + [affinity] if v is not None]
        return {"cpu_quota_cores": min(quotas) if quotas else None,
                "cpu_affinity_count": affinity,
                "cpuset_effective_count": min(cpusets) if cpusets else None,
                "effective_cpu_ceiling": min(effective) if effective else None,
                "memory_limit_bytes": min(memories) if memories else None,
                "cgroup_version": 2 if "v2" in self.groups else (1 if self.groups else None),
                "limit_note": "Minimum visible ancestor quota; null means no finite visible limit, not host capacity."}

    def sample(self):
        out = {"sampled_utc": utc()}
        if "v2" in self.groups:
            p = self.groups["v2"][0]
            out["cgroup_memory_current_bytes"] = numeric(read(p / "memory.current"))
            out["cgroup_memory_peak_since_creation_bytes"] = numeric(read(p / "memory.peak"))
            stat = dict(line.split() for line in read(p / "cpu.stat").splitlines())
            out["cgroup_cpu_usage_seconds"] = int(stat["usage_usec"]) / 1e6 if "usage_usec" in stat else None
            out["cgroup_cpu_throttled_seconds"] = int(stat["throttled_usec"]) / 1e6 if "throttled_usec" in stat else None
            out["cgroup_memory_events"] = {k: int(v) for k, v in (line.split() for line in read(p / "memory.events").splitlines())}
        else:
            if "memory" in self.groups:
                p = self.groups["memory"][0]
                out["cgroup_memory_current_bytes"] = numeric(read(p / "memory.usage_in_bytes"))
                out["cgroup_memory_peak_since_creation_bytes"] = numeric(read(p / "memory.max_usage_in_bytes"))
            if "cpuacct" in self.groups:
                value = numeric(read(self.groups["cpuacct"][0] / "cpuacct.usage"))
                out["cgroup_cpu_usage_seconds"] = value / 1e9 if value is not None else None
        current = out.get("cgroup_memory_current_bytes")
        if current is not None:
            self.observed_peak = max(self.observed_peak, current)
        out["sampled_memory_peak_this_runner_bytes"] = self.observed_peak or None
        usage, initial = out.get("cgroup_cpu_usage_seconds"), getattr(self, "initial_cpu", None)
        out["cgroup_cpu_seconds_since_runner_start"] = max(0, usage - initial) if usage is not None and initial is not None else None
        disk = shutil.disk_usage(self.base)
        out["disk_free_bytes"] = disk.free
        out["disk_total_bytes"] = disk.total
        fs = os.statvfs(self.base)
        out["disk_free_inodes"] = fs.f_favail
        return out


def safe_run_dir(pointer, allowed_root):
    value = read(pointer)
    if not value:
        return None
    path = Path(value).resolve()
    root = Path(allowed_root).resolve()
    return path if path.is_dir() and root in path.parents else None


def stage_results(path):
    if not path:
        return {"status": "not_started", "stages": {}}
    stages = {}
    for file in sorted(path.glob("*.exit-code.txt")):
        name = file.name.removesuffix(".exit-code.txt")
        stages[name] = {"exit_code": numeric(read(file)),
                        "start_utc": read(path / (name + ".start-utc.txt")) or None,
                        "end_utc": read(path / (name + ".end-utc.txt")) or None}
    return {"run_directory_name": path.name, "status": read(path / "status.txt"), "stages": stages}


def tail(path, byte_limit=32768):
    with path.open("rb") as src:
        src.seek(0, os.SEEK_END)
        size = src.tell()
        src.seek(max(0, size - byte_limit))
        data = src.read()
    return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-160:]) + "\n"


class Runner:
    def __init__(self, args):
        self.args = args
        self.base = args.base.resolve()
        self.scripts = Path(__file__).resolve().parent
        self.audit = self.base / "audit"
        self.repo = self.base / "verification"
        self.result = self.repo / "results"
        self.run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.run = self.audit / "runner-runs" / self.run_id
        self.run.mkdir(parents=True, exist_ok=True)
        self.resources = Resources(self.base)
        self.child = None
        self.interrupted = False
        self.driver_started = False
        self.old_driver_pointer = read(self.audit / "current-run.txt")
        self.old_bootstrap_pointer = read(self.base / "bootstrap-current-run.txt")
        self.status = {"run_id": self.run_id, "source_commit_requested": PIN,
                       "started_utc": utc(), "state": "starting", "runner_stage": None,
                       "bootstrap_exit_code": None, "driver_exit_code": None,
                       "verification_passed": False}
        self.environment = {"recorded_utc": utc(), "system": platform.system(),
                            "kernel_release": platform.release(), "machine": platform.machine(),
                            "python_version": platform.python_version(), "limits": self.resources.limits(),
                            "resource_scope": "Cgroup counters include every process in the same cgroup. Sampled peak can miss brief spikes; kernel peak is since cgroup creation. GNU time maximum RSS is not aggregate cgroup memory.",
                            "sampling_seconds": args.sample_seconds, "publication_seconds": args.publish_seconds,
                            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        self.last_push = 0.0
        self.last_attempt = 0.0
        self.last_fingerprint = None
        self.final_archives = None

    def phase_dirs(self):
        boot = safe_run_dir(self.base / "bootstrap-current-run.txt", self.base / "bootstrap-runs")
        build = safe_run_dir(self.audit / "current-run.txt", self.audit / "runs") if self.driver_started else None
        if boot and str(boot) == self.old_bootstrap_pointer:
            boot = None
        if build and str(build) == self.old_driver_pointer:
            build = None
        return {"bootstrap": boot, "build": build}

    def refresh(self):
        self.status["updated_utc"] = utc()
        self.status["resources"] = self.resources.sample()
        dirs = self.phase_dirs()
        self.status["phases"] = {k: stage_results(v) for k, v in dirs.items()}
        if dirs["build"]:
            for key, filename in (("lean_version", "lean-version.txt"), ("lake_version", "lake-version.txt"), ("source_commit_observed", "source-commit.txt")):
                value = read(dirs["build"] / filename)
                if value:
                    self.environment[key] = value
        write_json(self.audit / "status.json", self.status)
        write_json(self.audit / "environment.json", self.environment)
        with (self.run / "resources.jsonl").open("a") as out:
            out.write(json.dumps(self.status["resources"]) + "\n")

    def git(self, *args):
        proc = subprocess.run(["git", "-C", str(self.repo), *args], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=180, text=True)
        with (self.audit / "publication.log").open("a") as out:
            out.write(utc() + " git " + args[0] + "\n" + proc.stdout + "\n")
        if proc.returncode:
            raise RuntimeError("git " + args[0] + " exited " + str(proc.returncode))
        return proc.stdout

    def log_sources(self):
        sources = [("runner", p) for p in self.run.glob("*.log")]
        for group, path in self.phase_dirs().items():
            if path:
                sources.extend((group, p) for p in path.glob("*.log") if p.is_file() and not p.is_symlink())
                cleaner = path / "setup-cleaner.jsonl"
                if cleaner.is_file() and not cleaner.is_symlink():
                    sources.append((group, cleaner))
        return sources

    def archive_final(self):
        if self.final_archives is not None:
            return self.final_archives
        target = self.result / "final" / self.run_id
        target.mkdir(parents=True, exist_ok=True)
        sources = self.log_sources() + [("runner", self.run / "resources.jsonl")]
        for group, path in self.phase_dirs().items():
            if path:
                # Only final publication includes the (large) full source index.
                for pattern in FINAL_PATTERNS:
                    sources.extend((group, p) for p in path.glob(pattern) if p.is_file() and not p.is_symlink())
        manifests, files = [], []
        temporary = self.run / "archives"
        temporary.mkdir(exist_ok=True)
        for group, src in sorted(set(sources), key=lambda item: (item[0], str(item[1]))):
            name = group + "-" + src.name + ".gz"
            packed = temporary / name
            with src.open("rb") as inp, packed.open("wb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressor:
                    shutil.copyfileobj(inp, compressor)
            parts = []
            with packed.open("rb") as inp:
                index = 0
                while block := inp.read(CHUNK_BYTES):
                    suffix = "" if packed.stat().st_size <= CHUNK_BYTES else ".part" + str(index).zfill(4)
                    dest = target / (name + suffix)
                    dest.write_bytes(block)
                    files.append(dest)
                    parts.append({"filename": dest.name, "bytes": len(block), "sha256": hashlib.sha256(block).hexdigest()})
                    index += 1
            manifests.append({"source_group": group, "source_filename": src.name,
                              "uncompressed_bytes": src.stat().st_size, "compressed_parts_in_order": parts})
        manifest = target / "manifest.json"
        write_json(manifest, {"run_id": self.run_id, "archives": manifests,
                              "reassembly": "Concatenate listed parts in order, then gunzip. SHA256 values cover each compressed part."})
        files.append(manifest)
        self.final_archives = files
        return files

    def publish(self, final=False):
        self.last_attempt = time.monotonic()
        try:
            self.result.mkdir(parents=True, exist_ok=True)
            files = []
            for name, data in (("status.json", self.status), ("environment.json", self.environment)):
                path = self.result / name
                write_json(path, data)
                files.append(path)
            tails = self.result / "logtails" / self.run_id
            tails.mkdir(parents=True, exist_ok=True)
            for group, src in self.log_sources():
                dest = tails / (group + "-" + src.name + ".tail.txt")
                dest.write_text(tail(src))
                files.append(dest)
            evidence = self.result / "evidence" / self.run_id
            evidence.mkdir(parents=True, exist_ok=True)
            for group, directory in self.phase_dirs().items():
                if not directory:
                    continue
                for pattern in EVIDENCE_PATTERNS:
                    for src in sorted(directory.glob(pattern)):
                        if src.is_file() and not src.is_symlink() and src.stat().st_size <= 2 * 1024 * 1024:
                            dest = evidence / (group + "-" + src.name)
                            shutil.copyfile(src, dest)
                            files.append(dest)
            if final:
                files.extend(self.archive_final())
            # Explicit path list, including commit --only, preserves unrelated staged files.
            paths = [str(p.relative_to(self.repo)) for p in files]
            self.git("add", "--", *paths)
            changed = subprocess.run(["git", "-C", str(self.repo), "diff", "--cached", "--quiet", "--", *paths]).returncode
            if changed == 1:
                self.git("-c", "user.name=RH verification runner", "-c", "user.email=rh-verification@users.noreply.github.com",
                         "commit", "--only", "-m", "Verification " + self.status["state"] + " " + utc(), "--", *paths)
            elif changed != 0:
                raise RuntimeError("git diff failed")
            self.git("push", "origin", "HEAD:main")
            self.last_push = time.monotonic()
            write_json(self.audit / "publication-status.json", {"last_success_utc": utc(), "final": final, "run_id": self.run_id})
            return True
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            write_json(self.audit / "publication-status.json", {"last_attempt_utc": utc(), "success": False,
                        "error": str(exc), "details": "audit/publication.log", "final": final})
            print("Publication pending:", type(exc).__name__, flush=True)
            return False

    def interrupted_signal(self, signum, frame):
        self.interrupted = True
        if self.child and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def run_phase(self, name, command):
        self.status.update(state="running", runner_stage=name)
        self.refresh()
        self.publish()
        with (self.run / (name + ".stdout.log")).open("wb") as output:
            self.child = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                          env=os.environ.copy(), start_new_session=True)
            stop_deadline = None
            while self.child.poll() is None:
                self.refresh()
                fingerprint = json.dumps(self.status["phases"], sort_keys=True)
                if fingerprint != self.last_fingerprint or time.monotonic() - self.last_attempt >= self.args.publish_seconds:
                    self.publish()
                    self.last_fingerprint = fingerprint
                if self.interrupted:
                    stop_deadline = stop_deadline or time.monotonic() + 20
                    if time.monotonic() >= stop_deadline:
                        os.killpg(self.child.pid, signal.SIGKILL)
                time.sleep(self.args.sample_seconds)
            result = self.child.returncode
        self.child = None
        self.status[name + "_exit_code"] = result
        self.refresh()
        self.publish()
        return result

    def work(self):
        self.audit.mkdir(parents=True, exist_ok=True)
        with (self.audit / "runner.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("Another verification runner already holds the lock.")
            lock.seek(0)
            lock.truncate()
            lock.write(str(os.getpid()) + "\n")
            lock.flush()
            signal.signal(signal.SIGTERM, self.interrupted_signal)
            signal.signal(signal.SIGINT, self.interrupted_signal)
            try:
                if self.git("branch", "--show-current").strip() != "main":
                    raise RuntimeError("Verification results checkout must be on main.")
                rc = self.run_phase("bootstrap", ["bash", str(self.scripts / "bootstrap.sh"), str(self.base)])
                if rc == 0 and not self.interrupted:
                    self.driver_started = True
                    rc = self.run_phase("driver", ["bash", "-c",
                        'set -e; set -a; source "$1"; set +a; exec bash "$2" "$3" "$4"', "rh-driver",
                        str(self.base / "bootstrap.env"), str(self.scripts / "build_and_check.sh"),
                        str(self.base / "source"), str(self.audit)])
                self.refresh()
                build = self.status["phases"]["build"]
                checks = build["stages"]
                passed = rc == 0 and build["status"].startswith("complete:") and all(checks.get(name, {}).get("exit_code") == 0 for name in EXPECTED_CHECKS)
                self.status.update(state="interrupted" if self.interrupted else ("completed" if passed else "failed"),
                                   verification_passed=passed and not self.interrupted,
                                   exit_code=0 if passed and not self.interrupted else (rc or 1), finished_utc=utc())
                if rc == 0 and not passed:
                    self.status["failure_reason"] = "Driver exited zero without all required completion and check evidence."
            except Exception as exc:
                self.status.update(state="runner_error", verification_passed=False, exit_code=1,
                                   runner_error=type(exc).__name__ + ": " + str(exc), finished_utc=utc())
            finally:
                if self.child and self.child.poll() is None:
                    os.killpg(self.child.pid, signal.SIGTERM)
                    try:
                        self.child.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.child.pid, signal.SIGKILL)
                        self.child.wait()
                self.refresh()
                success = self.publish(final=True)
                for _ in range(2):
                    if success:
                        break
                    time.sleep(15)
                    success = self.publish(final=True)
            return 0 if self.status["verification_passed"] and success else (self.status.get("exit_code") or 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=Path("/root/autodl-tmp/rh-check"))
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--sample-seconds", type=int, default=15)
    parser.add_argument("--publish-seconds", type=int, default=300)
    parser.add_argument("--session", default="rh-verify")
    args = parser.parse_args()
    if args.sample_seconds < 1 or args.publish_seconds < 1:
        parser.error("Intervals must be positive.")
    args.base = args.base.resolve()
    for name in ("audit", "control", "tmp", "cache"):
        (args.base / name).mkdir(parents=True, exist_ok=True)
    os.environ["PATH"] = str(args.base / "tools/lean-4.33.1-linux/bin") + os.pathsep + os.environ.get("PATH", "")
    os.environ["XDG_CACHE_HOME"] = str(args.base / "cache")
    os.environ["TMPDIR"] = str(args.base / "tmp")
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ[key] = "http://127.0.0.1:20080"
    # These official data endpoints are directly reachable from this server.
    # Keep large Mathlib cache transfers off the control computer's SSH tunnel.
    for key in ("no_proxy", "NO_PROXY"):
        os.environ[key] = ".blob.core.windows.net,api.github.com,codeload.github.com"
    if args.worker:
        return Runner(args).work()
    socket = args.base / "control/tmux.sock"
    tmux = ["tmux", "-S", str(socket)]
    if subprocess.run(tmux + ["has-session", "-t", args.session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        print("Already running. Attach with:", shlex.join(tmux + ["attach", "-t", args.session]))
        return 0
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--base", str(args.base),
               "--sample-seconds", str(args.sample_seconds), "--publish-seconds", str(args.publish_seconds)]
    shell_command = shlex.join(command) + " >>" + shlex.quote(str(args.base / "audit/runner-console.log")) + " 2>&1"
    subprocess.run(tmux + ["new-session", "-d", "-s", args.session, shell_command], check=True)
    print("Detached verification. Attach with:", shlex.join(tmux + ["attach", "-t", args.session]))
    print("Status:", args.base / "audit/status.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
