#!/usr/bin/env python3
"""The Phase 0 harness driver (docs/phase-0.md §2 to §4).

Fetches a corpus workspace at its pin, runs the scenarios under one named
configuration, and appends one JSON object per run to a results file in the
schema the plan gives. Standard library only; cargo, rustup and (for the
nightly configuration) measureme's `summarize` do the work.

    python3 bench/run.py --list
    python3 bench/run.py ripgrep                       # every scenario, baseline-stable, 3 runs
    python3 bench/run.py ripgrep --config baseline-nightly --scenarios cold-build,noop --runs 1

Scenarios (plan §2): cold-build, cold-test, check, noop, touch-leaf, touch-core, deps-only.
`deps-only` is derived from a cold build's timings (the moment the last dependency unit
finished) rather than run as its own build; see the bench README.

Configurations (plan §3): `baseline-stable` and `baseline-nightly` are Phase 0's. The Phase 1
names are defined here already so Phase 1 is a flag away, but they are not measured in Phase 0.

Phase 2 (design §5): `--cache sccache` or `--cache kache` runs the cache scenarios (cache-cold,
cache-hit, cache-partial, cache-warm-local) through that cache, with an empty cache directory of
the invocation's own under the work directory. See the bench README.

    python3 bench/run.py ripgrep --cache sccache       # the four cache scenarios, 3 runs each

Nothing here reads or writes outside the work directory, the results file and the raw directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import critical_path

SCHEMA = 1
HERE = Path(__file__).resolve().parent
REPO = HERE.parent

SCENARIOS = ("cold-build", "cold-test", "check", "noop", "touch-leaf", "touch-core", "deps-only")
# Phase 2 (design §5; docs/tailscale-shared-cache.md §5): the build through a cache wrapper.
CACHE_SCENARIOS = ("cache-cold", "cache-hit", "cache-partial", "cache-warm-local")
CACHES = ("none", "sccache", "kache")

# name -> (needs nightly, extra RUSTFLAGS, extra env, note)
CONFIGS: dict[str, tuple[bool, list[str], dict[str, str], str]] = {
    "baseline-stable": (False, [], {}, "the workspace's own toolchain and profile, unchanged"),
    "baseline-nightly": (True, [], {}, "the same on the pinned nightly: -Zself-profile and the unit graph"),
    # Phase 1 (plan §3), named now, not run in Phase 0.
    "cranelift": (True, ["-Zcodegen-backend=cranelift"], {}, "rustc_codegen_cranelift for the dev profile"),
    "line-tables-only": (False, [], {"CARGO_PROFILE_DEV_DEBUG": "line-tables-only"}, "debug = line-tables-only"),
    "debug-0": (False, [], {"CARGO_PROFILE_DEV_DEBUG": "0"}, "debug = 0"),
    "mold": (False, ["-Clink-arg=-fuse-ld=mold"], {}, "the mold linker (Linux)"),
    "wild": (False, ["-Clink-arg=-fuse-ld=wild"], {}, "the wild linker (Linux)"),
    "lld": (False, ["-Clink-arg=-fuse-ld=lld"], {}, "the lld linker"),
    "threads-4": (True, ["-Zthreads=4"], {}, "the parallel front end, 4 threads"),
    "share-generics": (True, ["-Zshare-generics=y"], {}, "-Zshare-generics"),
}

# Self-profile query labels -> the plan's hypotheses (design §2). Everything a rustc process
# does that is not codegen, linking or debug info is the front end (parse, expand, resolve,
# typeck, borrowck, MIR, metadata, incremental bookkeeping).
PHASE_PATTERNS = (
    ("link", re.compile(r"^(link|run_linker|linker)")),
    ("debuginfo", re.compile(r"debuginfo|dwarf", re.I)),
    ("codegen", re.compile(r"^(codegen|LLVM_|monomorph|target_machine|assemble|finish_ongoing_codegen|copy_all_cgu|write_|serialize_work_products)")),
)


def log(msg: str) -> None:
    print(f"[bench] {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=False, **kw)


def first_line(s: str) -> str:
    return s.strip().splitlines()[0] if s.strip() else ""


# --- corpus and workspace ------------------------------------------------------------------


def load_corpus(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def workspace(corpus: dict, name: str) -> dict:
    for ws in corpus["workspace"]:
        if ws["name"] == name:
            return ws
    sys.exit(f"no workspace `{name}` in the corpus; --list shows them")


def fetch(ws: dict, work: Path) -> Path:
    """The workspace at its pinned commit, cloned on first use and verified on every use."""
    d = work / ws["name"]
    if not d.exists():
        log(f"cloning {ws['repo']} at {ws['ref']} into {d}")
        r = run(["git", "clone", "--quiet", "--depth", "1", "--branch", ws["ref"], ws["repo"], str(d)])
        if r.returncode:
            sys.exit(f"clone failed: {r.stderr.strip()}")
    head = run(["git", "rev-parse", "HEAD"], cwd=d).stdout.strip()
    if head != ws["commit"]:
        sys.exit(f"{d} is at {head}, not the pinned {ws['commit']}; move it aside")
    dirty = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=d).stdout.strip()
    if dirty:
        sys.exit(f"{d} has local modifications; `git checkout -- .` there first:\n{dirty}")
    return d


SIZE_FACTS = ("manifests", "lock_packages", "rs_files", "rs_lines", "integration_tests", "build_scripts")


def size_facts(wsdir: Path) -> dict[str, int]:
    """The corpus's size facts (bench/corpus.toml), measured from the checkout.

    One definition for every workspace: `target/` and `.git/` are skipped; `rs_lines` counts
    newlines, as `wc -l` does; `integration_tests` counts `.rs` files directly in a crate's
    `tests/` directory (the one beside a Cargo.toml), cargo's default integration-test targets.
    """
    facts = dict.fromkeys(SIZE_FACTS, 0)
    manifest_dirs: set[Path] = set()
    tests_files: list[Path] = []
    for d, dirs, files in os.walk(wsdir):
        dirs[:] = [x for x in dirs if x not in ("target", ".git")]
        here = Path(d)
        for name in files:
            if name == "Cargo.toml":
                facts["manifests"] += 1
                manifest_dirs.add(here)
            elif name == "build.rs":
                facts["build_scripts"] += 1
            if name.endswith(".rs"):
                facts["rs_files"] += 1
                with open(here / name, "rb") as f:
                    facts["rs_lines"] += f.read().count(b"\n")
                if here.name == "tests":
                    tests_files.append(here)
    facts["integration_tests"] = sum(1 for t in tests_files if t.parent in manifest_dirs)
    lock = wsdir / "Cargo.lock"
    if lock.exists():
        facts["lock_packages"] = lock.read_text().count("\n[[package]]") + lock.read_text().startswith("[[package]]")
    return facts


def check_size_facts(ws: dict, measured: dict[str, int]) -> list[str]:
    """Fields where the checkout disagrees with the corpus. The checkout is at the pinned
    commit, so a difference means the recorded value is wrong, not the build."""
    return [f"{k}: corpus {ws[k]}, checkout {measured[k]}" for k in SIZE_FACTS if ws.get(k) != measured[k]]


# --- toolchain and machine -----------------------------------------------------------------


def toolchain_for(ws: dict, corpus: dict, config: str) -> str:
    needs_nightly = CONFIGS[config][0]
    return corpus["nightly"]["channel"] if needs_nightly else ws["toolchain"]


def ensure_toolchain(tc: str, config: str) -> dict:
    r = run(["rustup", "toolchain", "install", tc, "--profile", "minimal", "--no-self-update"])
    if r.returncode:
        sys.exit(f"rustup toolchain install {tc} failed: {first_line(r.stderr)}")
    if config == "cranelift":
        run(["rustup", "component", "add", "rustc-codegen-cranelift-preview", "--toolchain", tc])
    rustc = first_line(run(["rustc", f"+{tc}", "-vV"]).stdout)
    cargo = first_line(run(["cargo", f"+{tc}", "-V"]).stdout)
    host = ""
    for line in run(["rustc", f"+{tc}", "-vV"]).stdout.splitlines():
        if line.startswith("host:"):
            host = line.split(":", 1)[1].strip()
    return {"name": tc, "rustc": rustc, "cargo": cargo, "host": host}


def linker_info(extra_rustflags: list[str]) -> str:
    for f in extra_rustflags:
        if "-fuse-ld=" in f:
            name = f.split("-fuse-ld=")[1]
            v = run([name, "--version"])
            return first_line(v.stdout or v.stderr) or name
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc") or "cc"
    v = run([cc, "--version"])
    return f"system default via {first_line(v.stdout) or cc}"


def machine_info(jobs: int, label: str | None) -> dict:
    sysname = platform.system()
    cpu, ram_gib, disk = "", 0.0, "unknown"
    if sysname == "Darwin":
        cpu = run(["sysctl", "-n", "machdep.cpu.brand_string"]).stdout.strip()
        mem = run(["sysctl", "-n", "hw.memsize"]).stdout.strip()
        ram_gib = round(int(mem) / 2**30, 1) if mem.isdigit() else 0.0
        info = run(["diskutil", "info", "/"]).stdout
        m = re.search(r"Solid State:\s*(\w+)", info)
        disk = {"Yes": "ssd", "No": "hdd"}.get(m.group(1), "unknown") if m else "unknown"
    elif sysname == "Linux":
        try:
            for line in open("/proc/cpuinfo"):
                if line.lower().startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
            for line in open("/proc/meminfo"):
                if line.startswith("MemTotal:"):
                    ram_gib = round(int(line.split()[1]) / 2**20, 1)
                    break
        except OSError:
            pass
        rot = sorted(Path("/sys/block").glob("*/queue/rotational"))
        if rot:
            disk = "hdd" if any(p.read_text().strip() == "1" for p in rot) else "ssd"
    cpu = cpu or platform.processor() or platform.machine()
    # A label for the results file name and the report, not the hostname: results are
    # committed, and a hostname is a personal detail that says nothing about the hardware.
    slug = re.sub(r"[^a-z0-9]+", "-", cpu.lower()).strip("-")[:40] or "machine"
    return {
        "label": label or slug,
        "cpu": cpu,
        "arch": platform.machine(),
        "cores": os.cpu_count() or 0,
        "jobs": jobs,
        "ram_gib": ram_gib,
        "disk": disk,
        "os": platform.platform(),
    }


# --- measurement ---------------------------------------------------------------------------


class RssSampler(threading.Thread):
    """Peak resident memory of a process tree, sampled twice a second.

    rusage only reports the largest single child, and a build is many rustc processes at once;
    the plan asks for the peak of the whole build. The tree is walked with `pgrep -P` and only
    those pids are asked for their RSS (`/proc` on Linux, `ps -o rss= -p` elsewhere): listing
    every process on the machine can block on an unrelated wedged process, and a sample must
    never stall the build. A sample that takes longer than two seconds is dropped, and after
    three such drops the sampler gives up and the record says so (`rss_samples` = 0).
    """

    def __init__(self, root_pid: int, interval: float = 0.5) -> None:
        super().__init__(daemon=True)
        self.root = root_pid
        self.interval = interval
        self.peak_kib = 0
        self.samples = 0
        self.failures = 0
        # Not `_stop`: threading.Thread has a `_stop()` method that join() calls on 3.11 and 3.12.
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set() and self.failures < 3:
            self.sample()
            self._stop_event.wait(self.interval)

    def descendants(self) -> list[int]:
        pids, frontier = [self.root], [self.root]
        while frontier:
            try:
                r = subprocess.run(["pgrep", "-P", ",".join(map(str, frontier))], capture_output=True, text=True, timeout=2)
            except subprocess.TimeoutExpired:
                return pids
            frontier = [int(x) for x in r.stdout.split() if x.isdigit()]
            pids.extend(frontier)
        return pids

    def rss_kib(self, pids: list[int]) -> int | None:
        if platform.system() == "Linux":
            total = 0
            for pid in pids:
                try:
                    for line in open(f"/proc/{pid}/status"):
                        if line.startswith("VmRSS:"):
                            total += int(line.split()[1])
                            break
                except OSError:
                    continue
            return total
        try:
            r = subprocess.run(["ps", "-o", "rss=", "-p", ",".join(map(str, pids))], capture_output=True, text=True, timeout=2)
        except subprocess.TimeoutExpired:
            return None
        return sum(int(x) for x in r.stdout.split() if x.isdigit())

    def sample(self) -> None:
        pids = self.descendants()
        total = self.rss_kib(pids)
        if total is None:
            self.failures += 1
            return
        self.samples += 1
        self.peak_kib = max(self.peak_kib, total)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)


def measure(cmd: list[str], cwd: Path, env: dict[str, str], logfile: Path) -> dict:
    """Run one command: its wall time, its CPU time and its memory.

    CPU time and the largest single process come from wait4() on this command alone (cargo
    and every descendant it waited for), not from RUSAGE_CHILDREN, which is cumulative over
    the harness's life and would hand an earlier warm-up build's peak to a no-op build. The
    wall clock stops when the command exits, before the memory sampler is joined.
    """
    t0 = time.monotonic()
    with open(logfile, "w") as out:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, text=True)
        sampler = RssSampler(proc.pid)
        sampler.start()
        _, status, usage = os.wait4(proc.pid, 0)
        wall = time.monotonic() - t0
        code = os.waitstatus_to_exitcode(status)
        proc.returncode = code  # reaped here; Popen must not wait for it again
        sampler.stop()
    # ru_maxrss is bytes on macOS and KiB on Linux.
    max_process_mib = usage.ru_maxrss / 2**20 if platform.system() == "Darwin" else usage.ru_maxrss / 1024
    if sampler.samples == 0:
        log("  no memory sample succeeded; peak_rss_mib is the largest single process (rusage)")
    return {
        "exit": code,
        "wall_s": round(wall, 3),
        "user_s": round(usage.ru_utime, 3),
        "sys_s": round(usage.ru_stime, 3),
        "peak_rss_mib": round(max(sampler.peak_kib / 1024, max_process_mib), 1),
        "max_process_rss_mib": round(max_process_mib, 1),
        "rss_samples": sampler.samples,
    }


# --- cargo's timing report ----------------------------------------------------------------


def newest_timing_report(target: Path, after: float) -> Path | None:
    reports = [p for p in (target / "cargo-timings").glob("cargo-timing-*.html") if p.stat().st_mtime >= after - 1]
    return max(reports, key=lambda p: p.stat().st_mtime) if reports else None


def parse_timing_report(path: Path) -> dict:
    """The unit, concurrency and CPU data cargo embeds as JSON in its HTML report.

    Current cargo (stable 1.99 and the 2026-10 nightlies) takes `--timings` without a value and
    writes only this report; `--timings=json` is gone. The arrays are the same data the HTML
    charts draw from.
    """
    html = path.read_text()

    def array(name: str):
        i = html.index(f"const {name} = ")
        j = html.index(";", i)
        return json.loads(html[i + len(f"const {name} = ") : j])

    summary = {}
    for key, val in re.findall(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>", html, re.S):
        k = re.sub("<[^>]+>", "", key).strip().rstrip(":")
        v = re.sub("<[^>]+>", "", val).strip()
        if k in ("Total units", "Fresh units", "Dirty units", "Max concurrency", "Total time", "rustc", "Profile"):
            summary[k] = v
    m = re.search(r"jobs=(\d+)\s+ncpu=(\d+)", summary.get("Max concurrency", ""))
    return {
        "units": array("UNIT_DATA"),
        "concurrency": array("CONCURRENCY_DATA"),
        "cpu_usage": array("CPU_USAGE"),
        "summary": summary,
        "jobs": int(m.group(1)) if m else None,
        "ncpu": int(m.group(2)) if m else None,
    }


def workspace_member_names(cargo: list[str], cwd: Path, env: dict[str, str]) -> set[str]:
    r = run(cargo + ["metadata", "--format-version", "1", "--no-deps"], cwd=cwd, env=env)
    if r.returncode:
        return set()
    return {p["name"] for p in json.loads(r.stdout)["packages"]}


def sections(u: dict) -> tuple[float, float]:
    """(front end, codegen) seconds of one unit, from the sections cargo records when rustc
    reports the moment its metadata is ready; (duration, 0) when it did not."""
    secs = u.get("sections") or []
    fe = cg = 0.0
    for name, span in secs:
        length = max(0.0, float(span["end"]) - float(span["start"]))
        if name == "frontend":
            fe += length
        elif name == "codegen":
            cg += length
    if not secs:
        fe = float(u.get("duration", 0.0))
    return fe, cg


def classify_units(units: list[dict], members: set[str]) -> dict:
    """Slot-seconds per hypothesis from the unit data (plan §4, design §2).

    Build scripts (compiling and running them) and proc-macro crates -> proc_macro_build_rs;
    other non-member units -> deps; member units -> workspace, split into front end and
    codegen by the unit's sections. `all_front_end` / `all_codegen` are the same split over
    every compiled unit, dependencies included (hypotheses 3 and 4 across the whole build).
    """
    out = {"deps": 0.0, "proc_macro_build_rs": 0.0, "workspace": 0.0, "ws_front_end": 0.0, "ws_codegen": 0.0,
           "all_front_end": 0.0, "all_codegen": 0.0, "test_binaries": 0, "units": len(units)}
    for u in units:
        target = u.get("target", "")
        d = float(u.get("duration", 0.0))
        if u.get("mode") == "run-custom-build" or "build-script" in target or "proc-macro" in target:
            out["proc_macro_build_rs"] += d
            continue
        fe, cg = sections(u)
        out["all_front_end"] += fe
        out["all_codegen"] += cg
        if u.get("name") in members:
            out["workspace"] += d
            out["ws_front_end"] += fe
            out["ws_codegen"] += cg
            if "(test)" in target:
                out["test_binaries"] += 1
        else:
            out["deps"] += d
    return out


def deps_finished_at(units: list[dict], members: set[str]) -> float:
    ends = [u["start"] + u["duration"] for u in units if u.get("name") not in members]
    return round(max(ends), 3) if ends else 0.0


def idle_slot_seconds(concurrency: list[dict], jobs: int, wall: float) -> float:
    """Job slots that sat empty, integrated over the build (design §2 hypothesis 7).

    cargo samples the counts when a unit starts or ends, the first time at the first such
    event; before it, every slot is taken to be as busy as at that first sample.
    """
    if not concurrency or not jobs:
        return 0.0
    first = {"t": 0.0, "active": concurrency[0]["active"]}
    points = [first] + concurrency + [{"t": wall, "active": 0}]
    total = 0.0
    for a, b in zip(points, points[1:]):
        dt_ = max(0.0, b["t"] - a["t"])
        total += max(0, jobs - a["active"]) * dt_
    return total


# --- rustc self-profile (nightly) -----------------------------------------------------------


def summarize_profiles(prof_dir: Path) -> dict | None:
    """Per-crate self time by phase, from measureme's `summarize --json`."""
    summarize = shutil.which("summarize")
    files = sorted(prof_dir.glob("*.mm_profdata"))
    if not files:
        return None
    if not summarize:
        log("summarize (cargo install --git https://github.com/rust-lang/measureme summarize) is not on PATH; no phase split")
        return {"error": "summarize not installed", "profiles": len(files)}
    per_crate: dict[str, dict[str, float]] = {}
    failed: list[str] = []
    for f in files:
        prefix = str(f)[: -len(".mm_profdata")]
        r = run([summarize, "summarize", "--json", prefix])
        js = Path(prefix + ".json")
        if r.returncode or not js.exists():
            failed.append(f.name)
            continue
        data = json.loads(js.read_text())
        crate = f.name.rsplit("-", 1)[0]
        phases = per_crate.setdefault(crate, {"front_end": 0.0, "codegen": 0.0, "debuginfo": 0.0, "link": 0.0, "total": 0.0})
        for q in data.get("query_data", []):
            st = q["self_time"]["secs"] + q["self_time"]["nanos"] / 1e9
            label = q["label"]
            for phase, pat in PHASE_PATTERNS:
                if pat.search(label):
                    phases[phase] += st
                    break
            else:
                phases["front_end"] += st
            phases["total"] += st
    if failed:
        # One unreadable trace and the sums are short by an unknown amount: the shares that
        # depend on them stay unknown rather than reading as a measured zero.
        log(f"  summarize failed on {len(failed)} of {len(files)} traces; link and debuginfo stay unknown")
        return {"error": f"summarize failed on {len(failed)} of {len(files)} traces", "profiles": len(files), "failed": failed[:10]}
    return {"profiles": len(files), "crates": per_crate}


# --- the build cache under test (Phase 2) ---------------------------------------------------

# Generous, so nothing is evicted during an invocation: an eviction would read as a miss.
CACHE_SIZE = {"sccache": "200G", "kache": "200GiB"}
# Shell variables that would change what a run measures, or where its cache lives. Remote
# backend settings (SCCACHE_BUCKET, KACHE_S3_*, ...) are not among them: they pass through.
CACHE_UNSET = {
    "sccache": ("SCCACHE_NO_DAEMON", "SCCACHE_START_SERVER", "SCCACHE_RECACHE", "SCCACHE_CLIENT_SIDE"),
    "kache": ("KACHE_DISABLED", "KACHE_RUNTIME_DIR", "KACHE_SOCKET_PATH", "KACHE_TRUST_DOMAIN", "KACHE_READONLY_STORE"),
}
INSTALL_HINT = {"sccache": "cargo install sccache --locked", "kache": "cargo install kache --locked"}
KACHE_SUMMARY = ("local_hits", "prefetch_hits", "remote_hits", "misses", "dups", "errors", "passthroughs")
KACHE_TIMING = ("total_restore_ms", "total_store_ms")


class CacheStillRunning(RuntimeError):
    """The kache daemon did not exit, so its store must not be wiped underneath it."""


def read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def sccache_backend(env: dict[str, str]) -> str:
    """"s3" when the shell sets up sccache's S3 backend (SCCACHE_BUCKET, or `[cache.s3]` in an
    exported SCCACHE_CONF), else "local". A run that used some other cache is caught after it:
    the server reports where it stored entries (cache_after)."""
    if env.get("SCCACHE_BUCKET"):
        return "s3"
    conf = env.get("SCCACHE_CONF")
    if conf and "s3" in (read_toml(Path(conf)).get("cache") or {}):
        return "s3"
    return "local"


def kache_backend(env: dict[str, str]) -> str:
    """The remote kache will use, in its own order: `[cache.remote]` in an exported KACHE_CONFIG,
    then KACHE_S3_BUCKET, then `[cache.remote]` in an exported KACHE_HOST_CONFIG; its `type`, or
    what kache infers without one (`filesystem` when `path` is set, else `s3`). "local" with no
    remote, or with KACHE_LOCAL_ONLY."""
    if env.get("KACHE_LOCAL_ONLY", "").lower() in ("1", "true"):
        return "local"

    def remote(var: str) -> dict | None:
        r = (read_toml(Path(env[var])).get("cache") or {}).get("remote") if env.get(var) else None
        return r if isinstance(r, dict) else None

    r = remote("KACHE_CONFIG")
    if r is None and env.get("KACHE_S3_BUCKET"):
        return "s3"
    r = r or remote("KACHE_HOST_CONFIG")
    if r is None:
        return "local"
    return str(r.get("type") or ("filesystem" if "path" in r else "s3"))


def find_cache(tool: str) -> dict | None:
    """The cache under test: its binary on PATH, version and backend. Exits if it is missing."""
    if tool == "none":
        return None
    path = shutil.which(tool)
    if not path:
        sys.exit(f"--cache {tool}: `{tool}` is not on PATH; install it ({INSTALL_HINT[tool]}) or choose another --cache")
    version = first_line(run([path, "--version"]).stdout) or "unknown"
    backend = sccache_backend(dict(os.environ)) if tool == "sccache" else kache_backend(dict(os.environ))
    if tool == "kache" and os.environ.get("KACHE_S3_BUCKET"):
        log("note: kache's daemon drops KACHE_S3_* from its environment (kache docs, Configuration: "
            "Pin file-backed settings); unless an exported KACHE_CONFIG names the remote, only the wrappers see it")
    return {"tool": tool, "path": path, "version": version, "backend": backend, "unset": CACHE_UNSET[tool]}


def prepare_cache(cache: dict, work: Path, ws_name: str) -> None:
    """This invocation's own cache directory under the work directory, and the environment that
    points the tool at it. Settings the user exported for the tool's config files are kept."""
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    root = work / "cache" / f"{cache['tool']}-{ws_name}-{stamp}-{os.getpid()}"
    store = root / "store"
    # Its own server socket, so a server already running for the user's builds (port 4226)
    # never serves a benchmark build.
    sock = root / "server.sock"
    if cache["tool"] == "sccache" and len(os.fsencode(sock)) > 100:
        sys.exit(f"the sccache socket path {sock} is too long for a Unix socket; use a shorter --work")
    store.mkdir(parents=True)
    env: dict[str, str] = {}
    if cache["tool"] == "sccache":
        # The harness stops the server itself; the idle timeout only ends one it left behind.
        env.update({"SCCACHE_DIR": str(store), "SCCACHE_CACHE_SIZE": CACHE_SIZE["sccache"],
                    "SCCACHE_SERVER_UDS": str(sock), "SCCACHE_IDLE_TIMEOUT": "3600"})
        if "SCCACHE_CONF" not in os.environ:
            (root / "sccache.toml").write_text("")
            env["SCCACHE_CONF"] = str(root / "sccache.toml")
    else:
        # KACHE_SEED_NEW_TARGETS=0: seeding copies dependency units from another target directory
        # into a new one, so they never reach the wrapper; kache's own benchmark turns it off too.
        # A zero free-space floor is kache's CI default; below 10 GiB free, builds would
        # otherwise evict cache entries mid-run.
        env.update({"KACHE_CACHE_DIR": str(store), "KACHE_MAX_SIZE": CACHE_SIZE["kache"],
                    "KACHE_SEED_NEW_TARGETS": "0", "KACHE_AUTO_RECOVER_MIN_FREE_BYTES": "0"})
        if "KACHE_CONFIG" not in os.environ:
            (root / "kache.toml").write_text("")
            env["KACHE_CONFIG"] = str(root / "kache.toml")
        if "KACHE_HOST_CONFIG" not in os.environ:
            env["KACHE_HOST_CONFIG"] = ""  # empty turns /etc/kache/config.toml off
    cache.update({"root": root, "store": store, "env": env})


def cache_tool(cache: dict, env: dict[str, str], args: list[str], capture: bool = False) -> subprocess.CompletedProcess | None:
    """One sccache or kache command with the run's environment; None if it could not run.
    Output goes to /dev/null unless captured: a server it starts must not hold our pipes."""
    out = subprocess.PIPE if capture else subprocess.DEVNULL
    try:
        return subprocess.run([cache["path"], *args], env=env, text=True, stdin=subprocess.DEVNULL,
                              stdout=out, stderr=out, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"  {cache['tool']} {' '.join(args)}: {e}")
        return None


def wait_unlocked(lock: Path, timeout: float) -> bool:
    """Until no process holds `lock` (kache's daemon holds daemon.run.lock while it runs)."""
    try:
        fd = os.open(lock, os.O_WRONLY)
    except FileNotFoundError:
        return True  # no daemon has run
    except OSError:
        return False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True  # closing the descriptor releases the probe
            except BlockingIOError:
                pass
            except OSError:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
    finally:
        os.close(fd)


def cache_stop(cache: dict, env: dict[str, str]) -> float:
    """Stop this invocation's sccache server or kache daemon; the seconds that took. kache's
    daemon first finishes work a build queued (uploads to a remote, for one); sccache has written
    each entry before the compile returns. A no-op when none is running. Raises CacheStillRunning
    when kache's daemon is still alive: its store must not be wiped or reused while it could be
    writing into it."""
    t0 = time.monotonic()
    if cache["tool"] == "sccache":
        cache_tool(cache, env, ["--stop-server"])
        Path(env["SCCACHE_SERVER_UDS"]).unlink(missing_ok=True)
    else:
        cache_tool(cache, env, ["daemon", "stop"])
        if not wait_unlocked(cache["store"] / "daemon.run.lock", 900):
            cache["still_running"] = True
            raise CacheStillRunning(
                f"the kache daemon was still running 15 minutes after `kache daemon stop`; "
                f"leaving {cache['store']} in place")
    return round(time.monotonic() - t0, 3)


def cache_wipe(cache: dict) -> None:
    """Empty the local cache directory. The server or daemon must be stopped first."""
    shutil.rmtree(cache["store"], ignore_errors=True)
    cache["store"].mkdir(parents=True)


def sccache_stats(info: dict) -> dict:
    """The counters of `sccache --show-stats --stats-format json` (sccache 0.18.0's ServerInfo:
    per-language counts under `counts`, durations as `secs` and `nanos`)."""
    s = info.get("stats") or {}

    def count(v) -> int:
        return sum(int(x) for x in (v.get("counts") or {}).values()) if isinstance(v, dict) else int(v or 0)

    def secs(v) -> float | None:
        return round(v.get("secs", 0) + v.get("nanos", 0) / 1e9, 3) if isinstance(v, dict) else None

    hits, misses = count(s.get("cache_hits")), count(s.get("cache_misses"))
    return {
        "hits": hits,
        "misses": misses,
        "errors": count(s.get("cache_errors")),
        "hit_rate": round(hits / (hits + misses), 4) if hits + misses else None,
        "read_s": secs(s.get("cache_read_hit_duration")),
        "write_s": secs(s.get("cache_write_duration")),
        "writes": count(s.get("cache_writes")),
        "read_errors": count(s.get("cache_read_errors")),
        "write_errors": count(s.get("cache_write_errors")),
        "timeouts": count(s.get("cache_timeouts")),
        "not_cacheable": count(s.get("requests_not_cacheable")) + count(s.get("non_cacheable_compilations")),
    }


def kache_totals(report: dict) -> dict[str, int]:
    """kache's cumulative counters from its report (`kache report --format json`; the schema is
    kache's docs/commands/report.schema.json): one count per compiler event in the window."""
    s, t = report.get("summary") or {}, report.get("timing") or {}
    totals = {k: int(s.get(k) or 0) for k in KACHE_SUMMARY}
    totals.update({k: int(t.get(k) or 0) for k in KACHE_TIMING})
    return totals


def kache_stats(before: dict[str, int], after: dict[str, int]) -> dict:
    """The timed build's share of kache's counters: the totals after it minus those before. A
    duplicate (`dups`, a compile another compile of the same key was already building) is
    counted as a miss, as kache's own hit rate counts it."""
    d = {k: after[k] - before.get(k, 0) for k in after}
    hits = d["local_hits"] + d["prefetch_hits"] + d["remote_hits"]
    misses = d["misses"] + d["dups"]
    return {
        "hits": hits,
        "misses": misses,
        "errors": d["errors"],
        "hit_rate": round(hits / (hits + misses), 4) if hits + misses else None,
        "read_s": round(d["total_restore_ms"] / 1000, 3),
        "write_s": round(d["total_store_ms"] / 1000, 3),
        "local_hits": d["local_hits"],
        "prefetch_hits": d["prefetch_hits"],
        "remote_hits": d["remote_hits"],
        "passthroughs": d["passthroughs"],
    }


def kache_report(cache: dict, env: dict[str, str]) -> dict[str, int] | None:
    r = cache_tool(cache, env, ["report", "--format", "json", "--since", "365d"], capture=True)
    if r is None or r.returncode:
        return None
    try:
        return kache_totals(json.loads(r.stdout))
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return None


def cache_before(cache: dict, env: dict[str, str]) -> dict[str, int] | None:
    """Untimed, right before the timer: sccache's counters zeroed (which starts its server when
    none runs), or kache's totals so far and its daemon started."""
    if cache["tool"] == "sccache":
        r = cache_tool(cache, env, ["--zero-stats"])
        if r is None or r.returncode:
            log("  warning: sccache --zero-stats failed; this run's cache_stats may include earlier builds")
        return None
    before = kache_report(cache, env)
    if before is None and not (cache["store"] / "events.jsonl").exists():
        before = kache_totals({})  # nothing logged yet
    cache_tool(cache, env, ["daemon", "start"])
    return before


def cache_after(cache: dict, env: dict[str, str], before: dict[str, int] | None) -> tuple[dict, str | None]:
    """The timed build's cache counters, then the server or daemon stopped (`stop_s`). The second
    value is a reason to reject the run: the sccache server stored entries somewhere other than
    the cache set up for this invocation. The location itself only goes to the log: it can name
    a bucket or a home directory, and results are committed."""
    if cache["tool"] == "sccache":
        invalid = None
        r = cache_tool(cache, env, ["--show-stats", "--stats-format", "json"], capture=True)
        try:
            info = json.loads(r.stdout) if r is not None and r.returncode == 0 else None
        except json.JSONDecodeError:
            info = None
        if not isinstance(info, dict):
            stats = {"error": "sccache --show-stats gave no JSON"}
        else:
            stats = sccache_stats(info)
            location = str(info.get("cache_location", ""))
            if cache["backend"] == "s3":
                used_ours = location.lower().startswith("s3")
            else:
                used_ours = str(cache["store"]) in location
            if not used_ours:
                log(f"  sccache stored entries in {location!r}, not this invocation's {cache['backend']} cache")
                invalid = f"sccache did not use this invocation's {cache['backend']} cache (see the log)"
        stats["stop_s"] = cache_stop(cache, env)
        # A failed counter read is a rejected run: analyze.py only sees the row's `error`, and a
        # hit rate of n/a with no reason reads as a successful run that had nothing to count.
        return stats, invalid or stats.get("error")
    # kache: stopped first, so whatever its daemon finishes after the build is in the report.
    stop_s = cache_stop(cache, env)
    after = kache_report(cache, env)
    stats = kache_stats(before, after) if before is not None and after is not None else {"error": "kache report gave no JSON before or after the build"}
    stats["stop_s"] = stop_s
    return stats, stats.get("error")


# --- one run -------------------------------------------------------------------------------


def cargo_env(base: dict[str, str], config: str, target: Path, prof_dir: Path | None,
              cache: dict | None = None) -> dict[str, str]:
    env = dict(base)
    env["CARGO_TARGET_DIR"] = str(target)
    env.pop("RUSTC_WRAPPER", None)  # no build cache in the baseline (plan §6)
    env.pop("RUSTC_WORKSPACE_WRAPPER", None)
    if cache is not None:
        # Phase 2: the cache under test is the wrapper. Incremental compilation is off: neither
        # tool caches incremental units, and design §5 keeps it off in CI.
        for k in cache["unset"]:
            env.pop(k, None)
        env.update(cache["env"])
        env["RUSTC_WRAPPER"] = cache["path"]
        env["CARGO_INCREMENTAL"] = "0"
    env["CARGO_TERM_COLOR"] = "never"
    rustflags = list(CONFIGS[config][1])
    if prof_dir is not None:
        rustflags.append(f"-Zself-profile={prof_dir}")
    if rustflags:
        env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " " + " ".join(rustflags)).strip()
    env.update(CONFIGS[config][2])
    return env


def touch(path: Path, marker: str) -> None:
    with open(path, "a") as f:
        f.write(f"\n// qrc-bench {marker}\n")


def restore(wsdir: Path, rel: str) -> None:
    run(["git", "checkout", "--", rel], cwd=wsdir)


def one_run(ctx: dict, scenario: str, index: int) -> list[dict]:
    ws, wsdir, cargo, config = ctx["ws"], ctx["wsdir"], ctx["cargo"], ctx["config"]
    jobs: int = ctx["jobs"]
    target: Path = wsdir / "target"
    raw = ctx["raw"] / scenario / f"run-{index}"
    if raw.exists():
        shutil.rmtree(raw)
    raw.mkdir(parents=True)
    # The timed command never carries -Zself-profile: writing the profile data inflates wall
    # time (ripgrep's cold build went from 7 s to 11 s with it on), and the flag is part of
    # RUSTFLAGS, so it also decides what a warm build sees as fresh. The phase data for the
    # cold scenarios comes from one extra, untimed, profiled build after the timed one.
    cache: dict | None = ctx["cache"]
    env = cargo_env(ctx["env"], config, target, None, cache)
    base_cmd = cargo + ["--offline"] if ctx["offline"] else cargo
    profiled = CONFIGS[config][0] and scenario in ("cold-build", "deps-only", "cold-test", "check")

    def clean() -> None:
        if target.exists():
            shutil.rmtree(target)

    # Every scenario covers the whole workspace (`--workspace`), as CI does; a root package
    # with members would otherwise build only itself and its dependencies.
    build = ["build", "--workspace", "--timings", "-j", str(jobs)]

    def warm() -> None:
        """An untimed build first, whatever the target dir holds: a previous scenario may have
        left check artifacts or a test profile there, which look warm and are not. Same env
        as the timed build, so its fingerprints are the ones the timed build sees."""
        log(f"  warm-up build for {scenario} (untimed)")
        r = run(base_cmd + ["build", "--workspace", "-j", str(jobs)], cwd=wsdir, env=env)
        if r.returncode:
            sys.exit(f"warm-up build failed:\n{r.stderr[-2000:]}")

    def populate() -> None:
        """Fill the cache with one untimed build from a clean target, so every unit goes through
        the wrapper, unless this invocation has filled it already; then stop the server or
        daemon, which lets it finish writing and keeps its state out of the timed build."""
        if ctx["cache_warm"]:
            return
        log(f"  filling the {cache['tool']} cache for {scenario} (untimed)")
        clean()
        r = run(base_cmd + ["build", "--workspace", "-j", str(jobs)], cwd=wsdir, env=env)
        if r.returncode:
            sys.exit(f"cache-filling build failed:\n{r.stderr[-2000:]}")
        cache_stop(cache, env)
        ctx["cache_warm"] = True

    timed_cmd: list[str]
    touched: str | None = None
    if scenario in ("cold-build", "deps-only"):
        clean()
        timed_cmd = base_cmd + build
    elif scenario == "cold-test":
        clean()
        timed_cmd = base_cmd + ["test", "--workspace", "--no-run", "--timings", "-j", str(jobs)]
    elif scenario == "check":
        clean()
        timed_cmd = base_cmd + ["check", "--workspace", "--timings", "-j", str(jobs)]
    elif scenario == "noop":
        warm()
        timed_cmd = base_cmd + build
    elif scenario in ("touch-leaf", "touch-core"):
        warm()
        touched = ws["touch_leaf"] if scenario == "touch-leaf" else ws["touch_core"]
        touch(wsdir / touched, f"{scenario} run {index} {time.time():.0f}")
        timed_cmd = base_cmd + build
    # Phase 2. Every cache scenario first stops the server or daemon a previous run left, so
    # no timed run inherits one; populate() stops it again after filling the cache.
    elif scenario == "cache-cold":
        cache_stop(cache, env)
        cache_wipe(cache)
        ctx["cache_warm"] = False
        clean()
        timed_cmd = base_cmd + build
    elif scenario in ("cache-hit", "cache-partial"):
        cache_stop(cache, env)
        populate()
        if cache["tool"] == "kache" and cache["backend"] != "local":
            # A fresh CI runner: kache's local store starts empty, so hits come from the remote.
            cache_wipe(cache)
        clean()
        if scenario == "cache-partial":
            touched = ws["touch_core"]
            touch(wsdir / touched, f"{scenario} run {index} {time.time():.0f}")
        timed_cmd = base_cmd + build
    elif scenario == "cache-warm-local":
        # The edit loop: the server or daemon the warm-up build starts stays up into the timed build.
        cache_stop(cache, env)
        populate()
        warm()
        touched = ws["touch_leaf"]
        touch(wsdir / touched, f"{scenario} run {index} {time.time():.0f}")
        timed_cmd = base_cmd + build
    else:
        sys.exit(f"unknown scenario {scenario}")

    before = cache_before(cache, env) if cache else None
    started = time.time()
    log(f"  {scenario} run {index}: {' '.join(timed_cmd[1:])}")
    m = measure(timed_cmd, wsdir, env, raw / "cargo.log")
    cache_stats, invalid = cache_after(cache, env, before) if cache else (None, None)
    if touched:
        restore(wsdir, touched)
    if m["exit"] != 0:
        log(f"  cargo exited {m['exit']}; see {raw / 'cargo.log'}")
        failed = base_record(ctx, scenario, index, m)
        failed.update({"error": f"cargo exited {m['exit']}", "log": str(raw / "cargo.log")})
        if cache_stats is not None:
            failed["cache_stats"] = cache_stats
        return [failed]
    if scenario == "cache-cold" and not invalid:
        ctx["cache_warm"] = True

    report = newest_timing_report(target, started)
    timing = parse_timing_report(report) if report else None
    if report:
        shutil.copy(report, raw / "cargo-timing.html")
    members = ctx["members"]
    record = base_record(ctx, scenario, index, m)
    if cache_stats is not None:
        record["cache_stats"] = cache_stats
    if invalid:
        record["error"] = invalid
    if timing:
        cls = classify_units(timing["units"], members)
        capacity = m["wall_s"] * jobs
        idle = idle_slot_seconds(timing["concurrency"], jobs, m["wall_s"])
        record["units"] = cls["units"]
        record["test_binaries"] = cls["test_binaries"]
        # Fractions of the build's slot capacity (wall time x jobs). deps and the build-script /
        # proc-macro bucket come from cargo's unit data; the workspace's own units are split into
        # front end and codegen by cargo's sections; link and debuginfo come from rustc's
        # self-profile (nightly only) and are taken out of codegen, which contains them in the
        # sections view. What is left of the capacity is idle: slots with nothing to run.
        share = {
            "deps": round(cls["deps"] / capacity, 4),
            "proc_macro_build_rs": round(cls["proc_macro_build_rs"] / capacity, 4),
            "front_end": round(cls["ws_front_end"] / capacity, 4),
            "codegen": round(cls["ws_codegen"] / capacity, 4),
            "debuginfo": None,
            "link": None,
            "idle": round(idle / capacity, 4),
        }
        record["workspace_share"] = round(cls["workspace"] / capacity, 4)
        all_compiled = cls["all_front_end"] + cls["all_codegen"]
        record["sections_all_crates"] = {
            "front_end": round(cls["all_front_end"] / all_compiled, 4) if all_compiled else None,
            "codegen": round(cls["all_codegen"] / all_compiled, 4) if all_compiled else None,
        }
        record["cpu_utilization"] = round((m["user_s"] + m["sys_s"]) / capacity, 4) if capacity else None
        record["timings_summary"] = timing["summary"]
        if scenario in critical_path.COLD_SCENARIOS:
            record["critical_path"] = critical_path.summary(timing["units"])
        # Both read the timed build's target dir, so they come before the profiled pass,
        # which cleans it and rebuilds with other flags.
        record["target_dir_mib"] = target_mib(target)
        if scenario == "cold-test":
            record["test_run"] = time_test_run(ctx, base_cmd, env, jobs, raw)
        if profiled:
            prof = profile_pass(ctx, scenario, timed_cmd, raw, clean)
            if prof and "crates" in prof:
                member_ids = {mname.replace("-", "_") for mname in members}
                ws_phases = {k: 0.0 for k in ("front_end", "codegen", "debuginfo", "link")}
                all_phases = {k: 0.0 for k in ("front_end", "codegen", "debuginfo", "link", "total")}
                for crate, ph in prof["crates"].items():
                    for k in all_phases:
                        all_phases[k] += ph[k]
                    if crate.replace("-", "_") in member_ids:
                        for k in ws_phases:
                            ws_phases[k] += ph[k]
                share["link"] = round(ws_phases["link"] / capacity, 4)
                share["debuginfo"] = round(ws_phases["debuginfo"] / capacity, 4)
                share["codegen"] = round(max(0.0, cls["ws_codegen"] - ws_phases["link"] - ws_phases["debuginfo"]) / capacity, 4)
                t = all_phases["total"] or 1.0
                record["phase_all_crates"] = {k: round(all_phases[k] / t, 4) for k in ("front_end", "codegen", "debuginfo", "link")}
                record["self_profile"] = {"profiles": prof["profiles"], "crates": len(prof["crates"]), "wall_s": prof.get("wall_s")}
            elif prof:
                record["self_profile"] = prof
        record["share"] = share
        record["share_sum"] = round(sum(v for v in share.values() if v is not None), 4)
    else:
        record["target_dir_mib"] = target_mib(target)
    out = [record]
    if scenario == "deps-only" and timing:
        derived = base_record(ctx, "deps-only", index, m)
        derived.update({
            "derived_from": "cold-build",
            "wall_s": deps_finished_at(timing["units"], members),
            "user_s": None, "sys_s": None, "peak_rss_mib": None, "max_process_rss_mib": None,
            "units": sum(1 for u in timing["units"] if u.get("name") not in members),
            "dep_slot_seconds": round(classify_units(timing["units"], members)["deps"], 3),
        })
        record["scenario"] = "cold-build"
        out.append(derived)
    return out


def profile_pass(ctx: dict, scenario: str, timed_cmd: list[str], raw: Path, clean) -> dict | None:
    """The same cold command once more, untimed, with -Zself-profile, for the phase split.

    The profile directory is one fixed path per configuration (it is part of RUSTFLAGS), and
    the trace files are moved into the run's directory and summarized from there. The build
    it leaves behind has different flags from the timed one; the next scenario cleans or
    warms the target directory before it measures anything.
    """
    prof_dir = ctx["raw"] / "self-profile"
    shutil.rmtree(prof_dir, ignore_errors=True)
    prof_dir.mkdir(parents=True)
    env = cargo_env(ctx["env"], ctx["config"], ctx["wsdir"] / "target", prof_dir)
    cmd = [a for a in timed_cmd if a != "--timings"]
    clean()
    log(f"  {scenario}: profiled build (untimed, -Zself-profile)")
    t0 = time.monotonic()
    r = run(cmd, cwd=ctx["wsdir"], env=env)
    wall = round(time.monotonic() - t0, 3)
    if r.returncode:
        log(f"  profiled build exited {r.returncode}; no phase split for this run")
        return {"error": f"profiled build exited {r.returncode}"}
    run_prof = raw / "self-profile"
    shutil.move(str(prof_dir), str(run_prof))
    prof = summarize_profiles(run_prof)
    if prof is not None:
        prof["wall_s"] = wall
    if not ctx["keep_traces"]:
        # The binary traces run to tens of megabytes per crate; summarize's JSON beside each
        # one keeps every number the record uses.
        for f in run_prof.glob("*.mm_profdata"):
            f.unlink()
    return prof


def target_mib(target: Path) -> int:
    du = run(["du", "-sm", str(target)]).stdout.split()
    return int(du[0]) if du and du[0].isdigit() else 0


def time_test_run(ctx: dict, base_cmd: list[str], env: dict[str, str], jobs: int, raw: Path) -> dict:
    """The test run itself, timed separately from its build (plan §2 cold-test)."""
    nextest = run(ctx["cargo"] + ["nextest", "--version"]).returncode == 0
    cmd = base_cmd + (["nextest", "run", "--workspace", "-j", str(jobs)] if nextest else ["test", "--workspace", "-j", str(jobs)])
    m = measure(cmd, ctx["wsdir"], env, raw / "test-run.log")
    return {"runner": "nextest" if nextest else "cargo test", **{k: m[k] for k in ("exit", "wall_s", "user_s", "sys_s", "peak_rss_mib")}}


def base_record(ctx: dict, scenario: str, index: int, m: dict) -> dict:
    ws = ctx["ws"]
    cache = ctx["cache"]
    return {
        "schema": SCHEMA,
        "workspace": f"{ws['repo'].split('github.com/')[-1]}",
        "commit": ws["commit"],
        "scenario": scenario,
        "config": ctx["config"],
        "cache": cache["tool"] if cache else "none",
        "cache_backend": cache["backend"] if cache else "none",
        "cache_version": cache["version"] if cache else None,
        "run": index,
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "toolchain": {**ctx["toolchain"], "linker": ctx["linker"]},
        "size_facts": ctx["size_facts"],
        "machine": ctx["machine"],
        "wall_s": m["wall_s"],
        "user_s": m["user_s"],
        "sys_s": m["sys_s"],
        "peak_rss_mib": m["peak_rss_mib"],
        "max_process_rss_mib": m["max_process_rss_mib"],
        "rss_samples": m["rss_samples"],
        "units": 0,
        "test_binaries": 0,
        "target_dir_mib": 0,
        "share": None,
    }


def self_test() -> int:
    """What CI can check without building a workspace: the measurement path, the size facts,
    and the share arithmetic. The measurement path runs real subprocesses, so a regression in
    the sampler or in wait4 handling fails here rather than on the first benchmark."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        m = measure(["sleep", "0.2"], root, dict(os.environ), root / "sleep.log")
        assert m["exit"] == 0 and m["wall_s"] >= 0.2 and m["peak_rss_mib"] >= 0, m
        m = measure(["sh", "-c", "exit 3"], root, dict(os.environ), root / "exit.log")
        assert m["exit"] == 3, m

        ws = root / "ws"
        files = {
            "Cargo.toml": "[workspace]\n",
            "Cargo.lock": "version = 4\n\n[[package]]\nname = \"a\"\n\n[[package]]\nname = \"b\"\n",
            "src/main.rs": "fn main() {\n}\n// end\n",
            "tests/cli.rs": "#[test]\nfn t() {}\n",
            "crates/x/Cargo.toml": "[package]\n",
            "crates/x/build.rs": "fn main() {}\n",
            "crates/x/src/tests/unit.rs": "// a unit-test module, not an integration test\n",
            "crates/x/src/prebuild.rs": "// not a build script\n",
            "target/debug/build/gen.rs": "// skipped: target/\n",
        }
        for rel, text in files.items():
            (ws / rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / rel).write_text(text)
        got = size_facts(ws)
        want = {"manifests": 2, "lock_packages": 2, "rs_files": 5, "rs_lines": 8, "integration_tests": 1, "build_scripts": 1}
        assert got == want, (got, want)

    units = [
        {"name": "dep", "target": "", "mode": "todo", "duration": 2.0, "sections": [["frontend", {"start": 0, "end": 1.5}], ["codegen", {"start": 1.5, "end": 2.0}]]},
        {"name": "dep", "target": " build-script (run)", "mode": "run-custom-build", "duration": 1.0},
        {"name": "app", "target": " app \"bin\"", "mode": "todo", "duration": 3.0, "sections": [["frontend", {"start": 0, "end": 1.0}], ["codegen", {"start": 1.0, "end": 3.0}]]},
        {"name": "app", "target": " it \"test\" (test)", "mode": "todo", "duration": 1.0},
    ]
    c = classify_units(units, {"app"})
    assert (c["deps"], c["proc_macro_build_rs"], c["workspace"], c["test_binaries"]) == (2.0, 1.0, 4.0, 1), c
    assert (c["ws_front_end"], c["ws_codegen"]) == (2.0, 2.0), c
    assert deps_finished_at([{"name": "dep", "start": 1.0, "duration": 2.0}, {"name": "app", "start": 3.0, "duration": 1.0}], {"app"}) == 3.0
    # 4 slots, 2 busy for the first 5 s, 4 busy for the last 5: 10 idle slot-seconds.
    assert idle_slot_seconds([{"t": 0.0, "active": 2}, {"t": 5.0, "active": 4}], 4, 10.0) == 10.0

    # Phase 2: the baseline environment has no wrapper; a cache run sets it and turns
    # incremental compilation off, and the variables that would skew it are gone.
    base = {"RUSTC_WRAPPER": "sccache", "SCCACHE_RECACHE": "1", "PATH": "/bin"}
    env = cargo_env(base, "baseline-stable", Path("/t"), None)
    assert "RUSTC_WRAPPER" not in env and "CARGO_INCREMENTAL" not in env, env
    cache = {"tool": "sccache", "path": "/opt/sccache", "env": {"SCCACHE_DIR": "/c"}, "unset": CACHE_UNSET["sccache"]}
    env = cargo_env(base, "baseline-stable", Path("/t"), None, cache)
    assert (env["RUSTC_WRAPPER"], env["CARGO_INCREMENTAL"], env["SCCACHE_DIR"]) == ("/opt/sccache", "0", "/c"), env
    assert "SCCACHE_RECACHE" not in env, env
    assert sccache_backend({"SCCACHE_BUCKET": "b"}) == "s3" and sccache_backend({}) == "local"
    assert kache_backend({"KACHE_S3_BUCKET": "b"}) == "s3" and kache_backend({"KACHE_S3_BUCKET": "b", "KACHE_LOCAL_ONLY": "1"}) == "local"
    info = {"stats": {"cache_hits": {"counts": {"Rust": 9}, "adv_counts": {}}, "cache_misses": {"counts": {"Rust": 1}, "adv_counts": {}},
                      "cache_errors": {"counts": {}, "adv_counts": {}}, "cache_writes": 1, "requests_not_cacheable": 2,
                      "cache_read_hit_duration": {"secs": 1, "nanos": 500000000}, "cache_write_duration": {"secs": 0, "nanos": 250000000}}}
    st = sccache_stats(info)
    assert (st["hits"], st["misses"], st["errors"], st["hit_rate"], st["read_s"], st["write_s"], st["not_cacheable"]) == (9, 1, 0, 0.9, 1.5, 0.25, 2), st
    before = kache_totals({"summary": {"local_hits": 5, "misses": 10}, "timing": {"total_restore_ms": 100, "total_store_ms": 2000}})
    after = kache_totals({"summary": {"local_hits": 13, "remote_hits": 1, "misses": 10, "dups": 1}, "timing": {"total_restore_ms": 1100, "total_store_ms": 2250}})
    st = kache_stats(before, after)
    assert (st["hits"], st["misses"], st["errors"], st["hit_rate"], st["read_s"], st["write_s"]) == (9, 1, 0, 0.9, 1.0, 0.25), st
    print("run.py self-test: ok")
    return 0


# --- main ----------------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workspace", nargs="?", help="a workspace name from the corpus")
    ap.add_argument("--corpus", type=Path, default=HERE / "corpus.toml")
    ap.add_argument("--work", type=Path, default=Path(os.environ.get("QRC_BENCH_WORK", Path.home() / "qrc-bench")),
                    help="disk-backed directory the workspaces are fetched into (never /tmp; plan §6)")
    ap.add_argument("--config", default="baseline-stable", choices=sorted(CONFIGS))
    ap.add_argument("--scenarios", default="all", help="comma-separated, or `all`")
    ap.add_argument("--runs", type=int, default=3, help="runs per scenario (plan §2: 3, or 5 when variance is above 5 percent)")
    ap.add_argument("--jobs", type=int, default=0, help="cargo -j (default: the core count); always recorded")
    ap.add_argument("--results", type=Path, help="JSONL file to append to (default: bench/results/<date>-<label>.jsonl)")
    ap.add_argument("--label", help="a short machine label for the results (default: the CPU model, slugified)")
    ap.add_argument("--raw", type=Path, default=HERE / "results" / "raw", help="where traces and logs go (git-ignored)")
    ap.add_argument("--online", action="store_true", help="let the timed cargo runs reach the network (default: --offline after one untimed `cargo fetch`)")
    ap.add_argument("--keep-traces", action="store_true", help="keep the binary self-profile traces (default: delete them once summarized)")
    ap.add_argument("--cache", default="none", choices=CACHES,
                    help="Phase 2: build through this cache, in an empty cache directory of the invocation's own; "
                         "runs the cache-* scenarios, which need it (default: none)")
    ap.add_argument("--list", action="store_true", help="list workspaces, scenarios and configurations")
    ap.add_argument("--self-test", action="store_true", help="check the measurement path, size facts and share arithmetic, then exit")
    args = ap.parse_args()
    if args.self_test:
        return self_test()

    corpus = load_corpus(args.corpus)
    if args.list or not args.workspace:
        print("workspaces:   " + ", ".join(ws["name"] for ws in corpus["workspace"]))
        print("scenarios:    " + ", ".join(SCENARIOS))
        print("              " + ", ".join(CACHE_SCENARIOS) + " (Phase 2; these need --cache sccache or --cache kache)")
        print("configs:      " + ", ".join(f"{k}{'' if i < 2 else ' (Phase 1)'}" for i, k in enumerate(CONFIGS)))
        print("caches:       " + ", ".join(CACHES) + " (--cache; default none)")
        return 0 if args.list else 2

    ws = workspace(corpus, args.workspace)
    cached = args.cache != "none"
    if args.scenarios == "all":
        scenarios = list(CACHE_SCENARIOS if cached else SCENARIOS)
    else:
        scenarios = args.scenarios.split(",")
    for s in scenarios:
        if s in CACHE_SCENARIOS and not cached:
            sys.exit(f"scenario `{s}` builds through a cache; add --cache sccache or --cache kache")
        if s in SCENARIOS and cached:
            # One kind per invocation, so a record's `cache` field always says which kind it is.
            sys.exit(f"--cache runs only the cache scenarios ({', '.join(CACHE_SCENARIOS)}); run `{s}` without --cache")
        if s not in SCENARIOS and s not in CACHE_SCENARIOS:
            sys.exit(f"unknown scenario `{s}`; --list shows them")
    cache = find_cache(args.cache)
    if "deps-only" in scenarios and "cold-build" in scenarios:
        scenarios.remove("cold-build")  # deps-only is derived from the same cold build
    jobs = args.jobs or os.cpu_count() or 1
    # Absolute paths: CARGO_TARGET_DIR is resolved by cargo from the workspace directory, so a
    # relative --work would point cargo and the harness at different target directories.
    args.work = args.work.expanduser().resolve()
    args.raw = args.raw.expanduser().resolve()
    if args.results:
        args.results = args.results.expanduser().resolve()
    if any(str(args.work).startswith(t) for t in ("/tmp", "/private/tmp")):
        sys.exit("--work must not be under /tmp (plan §6)")
    args.work.mkdir(parents=True, exist_ok=True)

    wsdir = fetch(ws, args.work)
    tc = toolchain_for(ws, corpus, args.config)
    toolchain = ensure_toolchain(tc, args.config)
    cargo = ["cargo", f"+{tc}"]
    env = dict(os.environ)
    members = workspace_member_names(cargo, wsdir, env)
    if not members:
        sys.exit("cargo metadata gave no workspace members")
    facts = size_facts(wsdir)
    for mismatch in check_size_facts(ws, facts):
        log(f"warning: bench/corpus.toml is wrong for {ws['name']}: {mismatch}")
    # The registry index and every crate the lockfile names, fetched once and untimed, so that
    # no timed run contains a download; the timed runs then pass --offline unless --online.
    log("cargo fetch (untimed)")
    r = run(cargo + ["fetch", "--locked"], cwd=wsdir, env=env)
    if r.returncode:
        sys.exit(f"cargo fetch failed: {r.stderr[-1500:]}")
    machine = machine_info(jobs, args.label)
    results = args.results or HERE / "results" / f"{dt.date.today().isoformat()}-{machine['label']}.jsonl"
    results.parent.mkdir(parents=True, exist_ok=True)
    raw = args.raw / ws["name"] / (args.config if cache is None else f"{args.config}+{cache['tool']}")
    ctx = {
        "ws": ws, "wsdir": wsdir, "cargo": cargo, "config": args.config, "jobs": jobs, "env": env,
        "members": members, "toolchain": toolchain, "linker": linker_info(CONFIGS[args.config][1]),
        "machine": machine, "raw": raw, "offline": not args.online, "keep_traces": args.keep_traces,
        "size_facts": facts, "cache": cache, "cache_warm": False,
    }
    log(f"{ws['name']} @ {ws['commit'][:8]} | {args.config} | {toolchain['rustc']} | -j {jobs} | {machine['cpu']}, {machine['cores']} cores, {machine['ram_gib']} GiB")
    if cache is not None:
        prepare_cache(cache, args.work, ws["name"])
        log(f"cache: {cache['version']} ({cache['backend']}), local directory {cache['store']}")
        if cache["backend"] != "local" and "cache-cold" in scenarios:
            log(f"note: cache-cold empties only the local directory; the {cache['backend']} remote keeps what it holds "
                "(use a fresh key prefix for a cold remote)")
    log(f"results -> {results}; raw -> {raw}")
    try:
        with open(results, "a") as out:
            for scenario in scenarios:
                for i in range(1, args.runs + 1):
                    for rec in one_run(ctx, scenario, i):
                        out.write(json.dumps(rec, sort_keys=True) + "\n")
                        out.flush()
                        if "error" in rec:
                            log(f"  -> {rec['scenario']} run {i}: FAILED ({rec['error']})")
                        else:
                            sh = rec.get("share") or {}
                            cs = rec.get("cache_stats") or {}
                            log(f"  -> {rec['scenario']} run {i}: wall {rec['wall_s']}s cpu {rec.get('user_s')}+{rec.get('sys_s')}s "
                                f"peak {rec.get('peak_rss_mib')} MiB units {rec.get('units')} "
                                f"deps {sh.get('deps')} pm {sh.get('proc_macro_build_rs')} idle {sh.get('idle')}"
                                + (f" hit rate {cs.get('hit_rate')}" if cs else ""))
    except CacheStillRunning as e:
        log(f"error: {e}")
        return 1
    finally:
        if cache is not None and "root" in cache:
            # The cache is this invocation's alone; nothing reuses it, and it runs to gigabytes.
            # Keep it when its daemon may still be writing to it (CacheStillRunning).
            if cache.get("still_running"):
                log(f"  keeping the {cache['tool']} cache directory {cache['root']}: its daemon may still be running")
            else:
                try:
                    cache_stop(cache, cargo_env(env, args.config, wsdir / "target", None, cache))
                except CacheStillRunning as e:
                    log(f"  {e}")
                else:
                    shutil.rmtree(cache["root"], ignore_errors=True)
                    log(f"removed the {cache['tool']} cache directory {cache['root']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
