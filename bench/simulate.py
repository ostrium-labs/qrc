#!/usr/bin/env python3
"""A scheduling simulator over recorded cargo builds (design §2 hypothesis 7, §6 Layer 3).

Replays the units of a recorded `cargo build --timings` report under other scheduling
policies and job counts, to estimate what qrcd's scheduler (Layer 3) could gain before
anyone builds it. Every number it prints is a simulated estimate made from recorded unit
durations: a target to validate, never a measurement. Method, assumptions and limits are in
bench/SIMULATE.md.

    python3 bench/simulate.py bench/results/raw/ripgrep/baseline-stable/cold-build/run-1/cargo-timing.html
    python3 bench/simulate.py bench/results/<date>-<label>.jsonl --scenario cold-build --config baseline-stable
    python3 bench/simulate.py <report> --jobs 4,8 --hit-fraction 0.8 --cache-aware --json
    python3 bench/simulate.py --self-test

Policies:
  cargo                cargo's recorded start order; .rmeta edges released as cargo released them
                       (the sanity check: at the recorded -j it should land near the recorded wall)
  fifo                 dependency-ready order; every dependent waits for the whole unit
  critical-path-first  longest remaining path first; every dependent waits for the whole unit
  pipelined            longest remaining path first; .rmeta edges released at metadata-ready time
  cache-hits           pipelined, with the hit units restored in --hit-time; with --cache-aware
                       they take no job slot

Standard library only (Python 3.11 or newer).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import heapq
import json
import random
import re
import statistics
import sys
import textwrap
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = 1
EPS = 1e-9
REPORT_NAME = "cargo-timing.html"

NOTE = ("Simulated estimates, not measurements: recorded unit durations replayed under other "
        "schedules. Treat every number as a target to validate; bench/SIMULATE.md gives the "
        "assumptions and limits.")

DEFAULT_JOBS = (1, 2, 4, 8, 16)
# Seconds a cache hit takes when --hit-time is not given. An assumed placeholder, not a
# measured restore time: set it from your cache's own numbers.
DEFAULT_HIT_TIME = 0.1
# cargo's report rounds times to 0.01 s; a unit that starts this long before its recorded
# release is counted as an inconsistency, one that starts this long after it as queued.
EARLY_TOLERANCE = 0.02
QUEUED_THRESHOLD = 0.05

META, DONE = 0, 1  # event kinds; at equal times metadata is released before completion


# --- the recorded build ---------------------------------------------------------------------


@dataclass
class Unit:
    i: int
    name: str
    version: str
    target: str
    duration: float
    start: float              # recorded start, on cargo's clock
    meta: float | None        # seconds after its start when its metadata (.rmeta) was ready
    parents: list[tuple[int, str]] = field(default_factory=list)   # (unit, "full" | "rmeta")
    children: list[tuple[int, str]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.name} v{self.version}{self.target}"


@dataclass
class Build:
    units: dict[int, Unit]
    order: list[int]          # a topological order
    startup: float            # the first unit's recorded start: cargo's work before any unit
    wall: float               # the last unit's recorded end, on cargo's clock
    jobs: int | None          # -j, from the report's "Max concurrency" row
    ncpu: int | None
    max_concurrency: int | None
    build_start: str | None
    edges: dict[str, int]     # "full", "rmeta", "dropped"
    meta_from_recording: int  # .rmeta releasers without a frontend section (metadata time read off the recording)
    early_starts: int         # units that started before their recorded release: a misread edge
    queued: int               # units that waited for a slot after their release in the recording


def _array(html: str, name: str):
    """One of the JSON arrays cargo embeds in its report (`const NAME = [...];`)."""
    marker = f"const {name} = "
    at = html.find(marker)
    if at < 0:
        return None
    idx = at + len(marker)
    while idx < len(html) and html[idx].isspace():
        idx += 1
    value, _ = json.JSONDecoder().raw_decode(html, idx)
    return value


def parse_report_text(html: str) -> dict:
    """The unit and concurrency data and the summary rows of a cargo --timings report (the same
    data bench/run.py's parse_timing_report reads)."""
    units = _array(html, "UNIT_DATA")
    if units is None:
        raise ValueError("no UNIT_DATA in it; is it a report from `cargo build --timings`?")
    concurrency = _array(html, "CONCURRENCY_DATA") or []
    summary: dict[str, str] = {}
    for key, val in re.findall(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>", html, re.S):
        k = re.sub("<[^>]+>", "", key).strip().rstrip(":")
        v = re.sub("<[^>]+>", " ", val).strip()
        if k in ("Total units", "Fresh units", "Dirty units", "Max concurrency", "Total time", "Build start", "Profile"):
            summary[k] = v
    row = summary.get("Max concurrency", "")
    m = re.search(r"jobs=(\d+)\s+ncpu=(\d+)", row)
    first = re.match(r"\s*(\d+)", row)
    if first:
        max_concurrency: int | None = int(first.group(1))
    else:
        actives = [int(c.get("active", 0)) for c in concurrency if isinstance(c, dict)]
        max_concurrency = max(actives) if actives else None
    return {
        "units": units,
        "concurrency": concurrency,
        "summary": summary,
        "jobs": int(m.group(1)) if m else None,
        "ncpu": int(m.group(2)) if m else None,
        "max_concurrency": max_concurrency,
    }


def frontend_end(u: dict) -> float | None:
    """When the unit's metadata was ready, in seconds from its start: the end of its `frontend`
    section, which cargo records when rustc reports the .rmeta written. None without sections."""
    ends = []
    for sec in u.get("sections") or []:
        try:
            if sec[0] == "frontend":
                ends.append(float(sec[1]["end"]))
        except (IndexError, KeyError, TypeError, ValueError):
            continue
    return max(ends) if ends else None


def topological_order(units: dict[int, Unit]) -> list[int]:
    indegree = {i: len(u.parents) for i, u in units.items()}
    queue = sorted(i for i, n in indegree.items() if n == 0)
    k = 0
    while k < len(queue):
        for c, _ in units[queue[k]].children:
            indegree[c] -= 1
            if indegree[c] == 0:
                queue.append(c)
        k += 1
    if len(queue) != len(units):
        raise ValueError(f"the recorded edges form a cycle through {len(units) - len(queue)} units")
    return queue


def build_graph(report: dict) -> Build:
    """The unit DAG, with dependencies inverted from cargo's release lists.

    A unit's `unblocked_units` are the units its completion released and its
    `unblocked_rmeta_units` the ones its metadata released: for each, the dependency that was
    the last one outstanding. cargo records no other edges, so every unit has at most the one
    parent that released it (SIMULATE.md, limits).
    """
    raw = report["units"]
    units: dict[int, Unit] = {}
    for u in raw:
        i = int(u["i"])
        d = max(0.0, float(u.get("duration") or 0.0))
        meta = frontend_end(u)
        units[i] = Unit(
            i=i, name=str(u.get("name", f"unit{i}")), version=str(u.get("version", "")),
            target=str(u.get("target") or ""), duration=d, start=float(u.get("start") or 0.0),
            meta=None if meta is None else min(max(meta, 0.0), d),
        )
    edges = {"full": 0, "rmeta": 0, "dropped": 0}
    for u in raw:
        p = int(u["i"])
        for key, kind in (("unblocked_units", "full"), ("unblocked_rmeta_units", "rmeta")):
            for c in u.get(key) or []:
                c = int(c)
                if c == p or c not in units:
                    edges["dropped"] += 1
                    continue
                units[c].parents.append((p, kind))
                units[p].children.append((c, kind))
                edges[kind] += 1
    # A unit that released dependents on its metadata but has no frontend section: take its
    # metadata time from the recording, as the earliest such dependent's start. That is no
    # earlier than the real moment (the dependent may have waited for a slot), so it errs on
    # the side of less pipelining.
    meta_from_recording = 0
    for u in units.values():
        starts = [units[c].start for c, kind in u.children if kind == "rmeta"]
        if starts and u.meta is None:
            u.meta = min(max(min(starts) - u.start, 0.0), u.duration)
            meta_from_recording += 1
    order = topological_order(units)
    startup = min((u.start for u in units.values()), default=0.0)
    wall = max((u.start + u.duration for u in units.values()), default=0.0)
    early = queued = 0
    for u in units.values():
        release = startup
        for p, kind in u.parents:
            pu = units[p]
            offset = pu.meta if kind == "rmeta" and pu.meta is not None else pu.duration
            release = max(release, pu.start + offset)
        if u.start < release - EARLY_TOLERANCE:
            early += 1
        elif u.start > release + QUEUED_THRESHOLD:
            queued += 1
    return Build(
        units=units, order=order, startup=startup, wall=wall, jobs=report.get("jobs"),
        ncpu=report.get("ncpu"), max_concurrency=report.get("max_concurrency"),
        build_start=(report.get("summary") or {}).get("Build start"), edges=edges,
        meta_from_recording=meta_from_recording, early_starts=early, queued=queued,
    )


# --- policies and the simulator -------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    name: str
    order: str        # "recorded", "ready" or "critical-path"
    pipelining: bool  # .rmeta edges released when the dependency's metadata is ready
    hits: bool        # the cache-hit set applies
    summary: str


POLICIES: dict[str, Policy] = {p.name: p for p in (
    Policy("cargo", "recorded", True, False,
           "cargo's recorded start order; .rmeta edges released as cargo released them"),
    Policy("fifo", "ready", False, False,
           "dependency-ready order; every dependent waits for the whole unit"),
    Policy("critical-path-first", "critical-path", False, False,
           "longest remaining path first; every dependent waits for the whole unit"),
    Policy("pipelined", "critical-path", True, False,
           "longest remaining path first; .rmeta edges released at metadata-ready time"),
    Policy("cache-hits", "critical-path", True, True,
           "pipelined, with the hit units restored in --hit-time (no job slot with --cache-aware)"),
)}
DEFAULT_POLICIES = ("cargo", "fifo", "critical-path-first", "pipelined")


@dataclass(frozen=True)
class CacheHits:
    units: frozenset[int]
    time: float   # seconds a hit takes, in place of the unit's duration
    aware: bool   # True: hits take no job slot (Layer 3's cache-aware scheduling)


def unit_times(build: Build, policy: Policy, hits: CacheHits | None) -> tuple[dict[int, float], dict[int, float]]:
    """Each unit's duration and metadata-ready offset under the policy. A hit's metadata and
    its full output arrive together, when the restore ends."""
    dur: dict[int, float] = {}
    meta: dict[int, float] = {}
    for i, u in build.units.items():
        if policy.hits and hits is not None and i in hits.units:
            dur[i] = meta[i] = hits.time
        else:
            dur[i] = u.duration
            meta[i] = u.meta if u.meta is not None else u.duration
    return dur, meta


def bottom_levels(build: Build, dur: dict[int, float], meta: dict[int, float], pipelining: bool) -> dict[int, float]:
    """The longest path from each unit's start to the end of the build, with unlimited slots:
    the priority of critical-path-first, and (from the roots) the critical path."""
    bl: dict[int, float] = {}
    for i in reversed(build.order):
        best = dur[i]
        for c, kind in build.units[i].children:
            offset = meta[i] if pipelining and kind == "rmeta" else dur[i]
            best = max(best, offset + bl[c])
        bl[i] = best
    return bl


def simulate(build: Build, policy: Policy, jobs: int, hits: CacheHits | None = None) -> dict:
    """Replays the build on `jobs` slots under `policy`: an event-driven list scheduler. At
    every event time, all releases at that time are applied first, then free slots are filled
    from the ready units in the policy's priority order. Durations are fixed."""
    if jobs < 1:
        raise ValueError("jobs must be at least 1")
    units = build.units
    pipelining = policy.pipelining
    dur, meta = unit_times(build, policy, hits)
    bl = bottom_levels(build, dur, meta, pipelining)
    hit_set = set(hits.units) if (policy.hits and hits is not None) else set()
    slotless = hit_set if (hits is not None and hits.aware) else set()
    has_rmeta_children = {i: any(kind == "rmeta" for _, kind in u.children) for i, u in units.items()}
    waiting = {i: len(u.parents) for i, u in units.items()}
    ready_at: dict[int, float] = {}
    started: dict[int, float] = {}
    finished: dict[int, float] = {}
    ready: list[tuple[tuple, int]] = []
    events: list[tuple[float, int, int, int]] = []
    counter = [0]
    free = jobs
    busy = 0.0
    now = build.startup

    def priority(i: int) -> tuple:
        if policy.order == "recorded":
            return (units[i].start, i)
        if policy.order == "ready":
            return (ready_at[i], i)
        return (-bl[i], i)

    def push_event(t: float, kind: int, i: int) -> None:
        heapq.heappush(events, (t, kind, counter[0], i))
        counter[0] += 1

    def begin(i: int, t: float) -> None:
        started[i] = t
        if pipelining and has_rmeta_children[i]:
            push_event(t + meta[i], META, i)
        push_event(t + dur[i], DONE, i)

    def become_ready(i: int, t: float) -> None:
        ready_at[i] = t
        if i in slotless:
            begin(i, t)  # restored outside the job slots, as soon as its inputs exist
        else:
            heapq.heappush(ready, (priority(i), i))

    def release(p: int, kinds: tuple[str, ...], t: float) -> None:
        for c, kind in units[p].children:
            if kind in kinds:
                waiting[c] -= 1
                if waiting[c] == 0:
                    become_ready(c, t)

    def apply_due() -> None:
        nonlocal free
        while events and events[0][0] <= now + EPS:
            t, kind, _, i = heapq.heappop(events)
            if kind == META:
                release(i, ("rmeta",), t)
            else:
                finished[i] = t
                if i not in slotless:
                    free += 1
                release(i, ("full",) if pipelining else ("full", "rmeta"), t)

    for i in build.order:
        if waiting[i] == 0:
            become_ready(i, now)

    while True:
        apply_due()
        while free > 0 and ready:
            _, i = heapq.heappop(ready)
            free -= 1
            busy += dur[i]
            begin(i, now)
            # A start can release a dependent at this same instant (a zero-length front end,
            # or a zero-length unit). Apply those releases before choosing the next unit, so a
            # newly ready, higher-priority dependent takes the slot rather than unrelated work.
            apply_due()
        if not events:
            break
        now = events[0][0]

    if len(finished) != len(units):
        raise RuntimeError(f"{len(units) - len(finished)} units never became ready (a cycle or a lost edge)")
    wall = max([build.startup, *finished.values()])
    capacity = wall * jobs
    critical_path = build.startup + max(bl.values(), default=0.0)
    work_bound = build.startup + busy / jobs
    return {
        "policy": policy.name,
        "jobs": jobs,
        "wall_s": wall,
        "busy_slot_s": busy,
        "idle_slot_s": capacity - busy,
        "utilisation": busy / capacity if capacity > 0 else None,
        "critical_path_s": critical_path,
        "work_bound_s": work_bound,
        "lower_bound_s": max(critical_path, work_bound),
        "gap_s": wall - critical_path,
        "gap_pct": (wall / critical_path - 1.0) * 100.0 if critical_path > 0 else None,
        "pipelining": pipelining,
        "hit_units": len(hit_set),
        "cache_aware": bool(hit_set) and bool(hits and hits.aware),
        "_schedule": {i: (started[i], finished[i]) for i in units},
    }


def select_hits(build: Build, fraction: float | None, patterns: list[str] | None, seed: int) -> tuple[frozenset[int], list[str]]:
    """The units that hit the cache: those matching a pattern (shell-style, against the
    package name, `name@version`, or the label `name vX.Y.Z target`), or a seeded random
    `fraction` of all units. Returns the set and the patterns that matched nothing."""
    chosen: set[int] = set()
    unmatched: list[str] = []
    for pat in patterns or []:
        matched = {i for i, u in build.units.items()
                   if any(fnmatch.fnmatchcase(s, pat) for s in (u.name, f"{u.name}@{u.version}", u.label))}
        if not matched:
            unmatched.append(pat)
        chosen |= matched
    if fraction is not None:
        ids = sorted(build.units)
        chosen |= set(random.Random(seed).sample(ids, round(fraction * len(ids))))
    return frozenset(chosen), unmatched


# --- sources: reports and results files -----------------------------------------------------


@dataclass
class Source:
    report: Path
    title: str
    group: str
    record: dict | None = None


@dataclass
class Filters:
    scenarios: list[str] | None = None
    configs: list[str] | None = None
    runs: list[int] | None = None
    workspace: str | None = None

    def match(self, rec: dict) -> bool:
        if self.scenarios and rec.get("scenario") not in self.scenarios:
            return False
        if self.configs and rec.get("config") not in self.configs:
            return False
        if self.runs and rec.get("run") not in self.runs:
            return False
        if self.workspace and self.workspace not in str(rec.get("workspace", "")):
            return False
        return True


def corpus_names(path: Path) -> dict[str, str]:
    """Results name a workspace by its GitHub `owner/repo`; the raw directories by its corpus
    name. This maps the one to the other."""
    try:
        corpus = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {ws["repo"].split("github.com/")[-1]: ws["name"]
            for ws in corpus.get("workspace", []) if "repo" in ws and "name" in ws}


def report_for_record(rec: dict, following: dict | None, raw_root: Path, names: dict[str, str], base: Path) -> Path | None:
    """Where bench/run.py left the timing report of one results record:
    <raw>/<corpus name>/<config>/<scenario>/run-<n>/cargo-timing.html. A `cold-build` record that
    run.py wrote for the deps-only scenario (it is followed by the derived deps-only record) has
    its report under deps-only/."""
    for key in ("timing_report", "raw_report", "raw"):
        value = rec.get(key)
        if isinstance(value, str) and value:
            p = Path(value).expanduser()
            if not p.is_absolute() and not p.exists():
                p = base / p
            return p if p.suffix == ".html" else p / REPORT_NAME
    ws = str(rec.get("workspace") or "")
    name = names.get(ws) or ws.split("/")[-1]
    scenario = rec.get("scenario")
    if (scenario == "cold-build" and following is not None and following.get("scenario") == "deps-only"
            and following.get("derived_from") == "cold-build" and following.get("run") == rec.get("run")
            and following.get("config") == rec.get("config")):
        scenario = "deps-only"
    if not (name and scenario and rec.get("config") and rec.get("run") is not None):
        return None
    return raw_root / name / str(rec["config"]) / str(scenario) / f"run-{rec['run']}" / REPORT_NAME


def sources_from_jsonl(path: Path, raw_root: Path, names: dict[str, str], filters: Filters) -> tuple[list[Source], list[str]]:
    records: list[dict] = []
    warnings: list[str] = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            warnings.append(f"{path}:{n}: not JSON ({e}); skipped")
    out: list[Source] = []
    for k, rec in enumerate(records):
        # Failed runs have no report; derived deps-only records are read off a cold build's.
        if rec.get("error") or rec.get("derived_from") or not filters.match(rec):
            continue
        following = records[k + 1] if k + 1 < len(records) else None
        title = f"{rec.get('workspace')} {rec.get('scenario')} {rec.get('config')} run {rec.get('run')}"
        report = report_for_record(rec, following, raw_root, names, path.parent)
        if report is None or not report.is_file():
            warnings.append(f"{title}: no timing report at {report}; skipped")
            continue
        group = f"{rec.get('workspace')} {rec.get('scenario')} {rec.get('config')}"
        out.append(Source(report=report, title=title, group=group, record=rec))
    return out, warnings


def stale_report(rec: dict | None, build_start: str | None) -> str | None:
    """run.py reuses run-N directories, so a report can belong to a later invocation than the
    record that points at it. The report's build start must fall within the record's run."""
    if not rec or not build_start or not rec.get("recorded_at"):
        return None
    try:
        started = dt.datetime.fromisoformat(build_start.strip().replace("Z", "+00:00"))
        recorded = dt.datetime.fromisoformat(str(rec["recorded_at"]))
        lag = (recorded - started).total_seconds()
    except (TypeError, ValueError):
        return None
    wall = float(rec.get("wall_s") or 0.0)
    if lag < -2.0 or lag > wall + 120.0:
        return (f"the report's build started at {build_start.strip()} but the record was written at "
                f"{rec['recorded_at']}; run.py reuses run-N directories, so the report may be from another invocation")
    return None


# --- one source -----------------------------------------------------------------------------


def analyse(source: Source, jobs_list: list[int], policies: list[str], hit_args: dict | None,
            recorded_jobs_override: int | None) -> dict:
    res: dict = {"source": str(source.report), "title": source.title, "group": source.group, "warnings": []}
    if source.record:
        rec = source.record
        res["record"] = {k: rec.get(k) for k in ("workspace", "commit", "scenario", "config", "run", "recorded_at")}
    try:
        report = parse_report_text(source.report.read_text(errors="replace"))
        build = build_graph(report)
    except (OSError, ValueError, KeyError, TypeError) as e:
        res["skipped"] = f"could not read the report: {e}"
        return res
    stale = stale_report(source.record, build.build_start)
    if stale:
        res["warnings"].append(stale)
    if build.early_starts:
        res["warnings"].append(f"{build.early_starts} units started before their recorded release; "
                               "the edges may be misread")
    res["units"] = len(build.units)
    res["edges"] = dict(build.edges)
    res["meta_from_recording"] = build.meta_from_recording
    if not build.units:
        res["skipped"] = "no units ran (nothing was rebuilt)"
        return res

    record_jobs = ((source.record or {}).get("machine") or {}).get("jobs")
    recorded_jobs = recorded_jobs_override or build.jobs or record_jobs or build.max_concurrency
    busy = sum(u.duration for u in build.units.values())
    res["recorded"] = {
        "wall_s": build.wall,
        "startup_s": build.startup,
        "jobs": recorded_jobs,
        "ncpu": build.ncpu,
        "max_concurrency": build.max_concurrency,
        "busy_slot_s": busy,
        "utilisation": busy / (build.wall * recorded_jobs) if recorded_jobs and build.wall > 0 else None,
        "queued_units": build.queued,
        "process_wall_s": (source.record or {}).get("wall_s"),
    }

    hits: CacheHits | None = None
    if hit_args:
        chosen, unmatched = select_hits(build, hit_args["fraction"], hit_args["patterns"], hit_args["seed"])
        for pat in unmatched:
            res["warnings"].append(f"--hit-units pattern `{pat}` matched no unit")
        hits = CacheHits(units=chosen, time=hit_args["time"], aware=hit_args["aware"])
        res["hits"] = {
            "units": sorted(build.units[i].label for i in chosen),
            "hit_time_s": hits.time,
            "cache_aware": hits.aware,
        }

    if recorded_jobs:
        replay = simulate(build, POLICIES["cargo"], recorded_jobs)
        diff = replay["wall_s"] - build.wall
        res["sanity"] = {
            "policy": "cargo",
            "jobs": recorded_jobs,
            "simulated_wall_s": replay["wall_s"],
            "recorded_wall_s": build.wall,
            "diff_s": diff,
            "diff_pct": diff / build.wall * 100.0 if build.wall > 0 else None,
        }

    rows = []
    for jobs in jobs_list:
        cargo_wall = None
        for name in policies:
            row = simulate(build, POLICIES[name], jobs, hits)
            row.pop("_schedule")
            if name == "cargo":
                cargo_wall = row["wall_s"]
            rows.append(row)
        for row in rows:
            if row["jobs"] == jobs:
                row["vs_cargo_pct"] = ((row["wall_s"] / cargo_wall - 1.0) * 100.0
                                       if cargo_wall and row["policy"] != "cargo" else None)
    res["results"] = rows
    return res


def summarize(results: list[dict]) -> list[dict]:
    """Medians across the recordings of one workspace, scenario and configuration."""
    groups: dict[str, list[dict]] = {}
    for res in results:
        if not res.get("skipped") and res.get("results"):
            groups.setdefault(res["group"], []).append(res)
    out = []
    for group, items in groups.items():
        if len(items) < 2:
            continue
        cells: dict[tuple[str, int], list[dict]] = {}
        for res in items:
            for row in res["results"]:
                cells.setdefault((row["policy"], row["jobs"]), []).append(row)
        rows = []
        for (policy, jobs), rs in cells.items():
            walls = [r["wall_s"] for r in rs]
            vs = [r["vs_cargo_pct"] for r in rs if r.get("vs_cargo_pct") is not None]
            rows.append({
                "policy": policy, "jobs": jobs, "recordings": len(rs),
                "wall_s_median": statistics.median(walls), "wall_s_min": min(walls), "wall_s_max": max(walls),
                "vs_cargo_pct_median": statistics.median(vs) if vs else None,
                "gap_s_median": statistics.median(r["gap_s"] for r in rs),
            })
        diffs = [res["sanity"]["diff_pct"] for res in items if res.get("sanity") and res["sanity"]["diff_pct"] is not None]
        out.append({
            "group": group,
            "recordings": len(items),
            "sanity_diff_pct_median": statistics.median(diffs) if diffs else None,
            "sanity_diff_pct_max_abs": max((abs(d) for d in diffs), default=None),
            "rows": rows,
        })
    return out


# --- output ---------------------------------------------------------------------------------


def _fmt(x, spec: str, none: str = "-") -> str:
    return none if x is None else format(x, spec)


def _rounded(x):
    if isinstance(x, float):
        return round(x, 4)
    if isinstance(x, dict):
        return {k: _rounded(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_rounded(v) for v in x]
    return x


def print_table(rows: list[dict], ncpu: int | None) -> bool:
    print(f"   {'policy':<20} {'-j':>3} {'wall s':>8} {'vs cargo':>9} {'util':>5} {'idle slot-s':>11} "
          f"{'cp s':>7} {'work bound s':>12} {'above cp':>9}")
    starred = False
    for r in rows:
        star = bool(ncpu and r["jobs"] > ncpu)
        starred |= star
        jobs = f"{r['jobs']}{'*' if star else ''}"
        vs = _fmt(r.get("vs_cargo_pct"), "+.1f") + ("%" if r.get("vs_cargo_pct") is not None else "")
        above = _fmt(r["gap_pct"], ".1f") + ("%" if r["gap_pct"] is not None else "")
        print(f"   {r['policy']:<20} {jobs:>3} {r['wall_s']:8.2f} {vs:>9} {_fmt(r['utilisation'], '.2f'):>5} "
              f"{r['idle_slot_s']:11.2f} {r['critical_path_s']:7.2f} {r['work_bound_s']:12.2f} {above:>9}")
    return starred


def print_text(doc: dict) -> None:
    print(textwrap.fill(doc["note"], 100))
    print()
    starred = False
    for res in doc["sources"]:
        print(f"== {res['title']}")
        if res["title"] != res["source"]:
            print(f"   report: {res['source']}")
        for w in res.get("warnings", []):
            print(f"   warning: {w}")
        if res.get("skipped"):
            print(f"   skipped: {res['skipped']}")
            print()
            continue
        e = res["edges"]
        line = (f"   {res['units']} units; {e['full'] + e['rmeta']} dependency edges recorded "
                f"({e['rmeta']} released at .rmeta, {e['full']} at completion")
        line += f", {e['dropped']} dropped)" if e["dropped"] else ")"
        print(line)
        rec = res["recorded"]
        line = f"   recorded: {rec['wall_s']:.2f} s on cargo's clock (first unit at {rec['startup_s']:.2f} s)"
        if rec["jobs"]:
            line += f", -j {rec['jobs']}, slot utilisation {_fmt(rec['utilisation'], '.2f')}"
        if rec["max_concurrency"] is not None:
            line += f", cargo's max concurrency {rec['max_concurrency']}"
        print(line)
        print(f"   {rec['queued_units']} units waited more than {QUEUED_THRESHOLD:.2f} s for a slot after their release")
        if rec.get("process_wall_s") is not None:
            print(f"   run.py timed the whole cargo process at {rec['process_wall_s']:.2f} s (process start-up and exit included)")
        s = res.get("sanity")
        if s:
            print(f"   sanity: cargo's order replayed at -j {s['jobs']} gives {s['simulated_wall_s']:.2f} s, "
                  f"{s['diff_s']:+.2f} s ({_fmt(s['diff_pct'], '+.1f')}%) from the recording")
        else:
            print("   sanity: skipped; the report does not say which -j it ran with (pass --recorded-jobs)")
        h = res.get("hits")
        if h:
            where = "outside the job slots (--cache-aware)" if h["cache_aware"] else "each holding a job slot"
            print(f"   cache hits: {len(h['units'])} of {res['units']} units, {h['hit_time_s']:.2f} s each, {where}")
        print()
        starred |= print_table(res["results"], rec.get("ncpu"))
        print()
    for g in doc.get("summary", []):
        print(f"== median over {g['recordings']} recordings: {g['group']}")
        print(f"   sanity difference: median {_fmt(g['sanity_diff_pct_median'], '+.1f')}%, "
              f"largest {_fmt(g['sanity_diff_pct_max_abs'], '.1f')}%")
        print(f"   {'policy':<20} {'-j':>3} {'wall s':>8} {'min':>7} {'max':>7} {'vs cargo':>9} {'gap to cp s':>11}")
        for r in g["rows"]:
            vs = _fmt(r["vs_cargo_pct_median"], "+.1f") + ("%" if r["vs_cargo_pct_median"] is not None else "")
            print(f"   {r['policy']:<20} {r['jobs']:>3} {r['wall_s_median']:8.2f} {r['wall_s_min']:7.2f} "
                  f"{r['wall_s_max']:7.2f} {vs:>9} {r['gap_s_median']:11.2f}")
        print()
    if starred:
        print("* more job slots than the recording machine had cores: the durations are this machine's,")
        print("  so the row describes a bigger machine whose units each take as long as they did here.")
    print("cp s: the critical path, a lower bound on any schedule's wall. work bound s: busy slot-seconds / -j.")
    print("above cp: how far the simulated wall is above the critical path. All values are simulated estimates.")


# --- self-test ------------------------------------------------------------------------------


def _u(i: int, dur: float, *, start: float = 0.0, full=(), rmeta=(), frontend: float | None = None, name: str | None = None) -> dict:
    """One synthetic UNIT_DATA entry."""
    sections = None
    if frontend is not None:
        sections = [["frontend", {"start": 0.0, "end": frontend}], ["codegen", {"start": frontend, "end": dur}]]
    return {"i": i, "name": name or f"u{i}", "version": "0.1.0", "mode": "todo", "target": "", "features": [],
            "start": start, "duration": dur, "unblocked_units": list(full), "unblocked_rmeta_units": list(rmeta),
            "sections": sections}


def _graph(units: list[dict], jobs: int | None = None) -> Build:
    return build_graph({"units": units, "concurrency": [], "summary": {}, "jobs": jobs, "ncpu": None, "max_concurrency": None})


def self_test() -> int:
    """Small synthetic DAGs whose schedules can be worked out by hand."""
    import tempfile

    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            failures.append(what)

    def close(a, b, tol: float = 1e-6) -> bool:
        return a is not None and abs(a - b) <= tol

    def wall(build: Build, policy: str, jobs: int, hits: CacheHits | None = None) -> float:
        return simulate(build, POLICIES[policy], jobs, hits)["wall_s"]

    every = ("cargo", "fifo", "critical-path-first", "pipelined")

    # A chain a(2) -> b(3) -> c(1): 6 s at any -j, and the critical path is the chain.
    chain = _graph([_u(0, 2.0, full=[1]), _u(1, 3.0, start=2.0, full=[2]), _u(2, 1.0, start=5.0)])
    for p in every:
        for j in (1, 2, 4):
            r = simulate(chain, POLICIES[p], j)
            check(close(r["wall_s"], 6.0) and close(r["critical_path_s"], 6.0) and close(r["gap_s"], 0.0),
                  f"chain {p} -j {j}: {r['wall_s']}, cp {r['critical_path_s']}")
    r = simulate(chain, POLICIES["fifo"], 1)
    check(close(r["utilisation"], 1.0) and close(r["idle_slot_s"], 0.0), f"chain -j 1 utilisation {r['utilisation']}")
    r = simulate(chain, POLICIES["fifo"], 2)
    check(close(r["utilisation"], 0.5) and close(r["idle_slot_s"], 6.0), f"chain -j 2 utilisation {r['utilisation']}")

    # A diamond a(1) -> b(2), c(3) -> d(1): 7 s on one slot, 5 s (the critical path) on two.
    diamond = _graph([_u(0, 1.0, full=[1, 2]), _u(1, 2.0, start=1.0, full=[3]),
                      _u(2, 3.0, start=1.0, full=[3]), _u(3, 1.0, start=4.0)])
    for p in every:
        check(close(wall(diamond, p, 1), 7.0), f"diamond {p} -j 1: {wall(diamond, p, 1)}")
        for j in (2, 4):
            check(close(wall(diamond, p, j), 5.0), f"diamond {p} -j {j}: {wall(diamond, p, j)}")
    check(close(simulate(diamond, POLICIES["fifo"], 2)["critical_path_s"], 5.0), "diamond critical path")

    # A wide fan-out: one root (1 s) releases eight 2 s units; 1 + 2 x ceil(8 / j).
    fan = _graph([_u(0, 1.0, full=list(range(1, 9)))] + [_u(k, 2.0, start=1.0) for k in range(1, 9)])
    for j, want in ((1, 17.0), (2, 9.0), (4, 5.0), (8, 3.0), (16, 3.0)):
        for p in every:
            check(close(wall(fan, p, j), want), f"fan-out {p} -j {j}: {wall(fan, p, j)} != {want}")
    r = simulate(fan, POLICIES["fifo"], 16)
    check(close(r["critical_path_s"], 3.0) and close(r["work_bound_s"], 17.0 / 16) and close(r["lower_bound_s"], 3.0),
          f"fan-out bounds: {r['critical_path_s']}, {r['work_bound_s']}")
    r = simulate(fan, POLICIES["fifo"], 2)
    check(close(r["work_bound_s"], 8.5) and close(r["lower_bound_s"], 8.5), f"fan-out -j 2 work bound {r['work_bound_s']}")

    # Priority: c(3) and d(3) are ready with a(1) -> b(3). On two slots FIFO (ties by unit
    # index) runs c and d first and ends at 7; longest-path-first starts a at once and ends at
    # 6. The recorded schedule is FIFO's, so the cargo replay at -j 2 reproduces it exactly.
    prio = _graph([_u(0, 3.0, name="c"), _u(1, 3.0, name="d"), _u(2, 1.0, start=3.0, full=[3], name="a"),
                   _u(3, 3.0, start=4.0, name="b")], jobs=2)
    check(close(wall(prio, "fifo", 2), 7.0), f"priority fifo: {wall(prio, 'fifo', 2)}")
    check(close(wall(prio, "critical-path-first", 2), 6.0), f"priority critical-path-first: {wall(prio, 'critical-path-first', 2)}")
    check(close(wall(prio, "cargo", 2), prio.wall) and close(prio.wall, 7.0), f"priority cargo replay: {wall(prio, 'cargo', 2)}")
    check(close(simulate(prio, POLICIES["fifo"], 2)["critical_path_s"], 4.0), "priority critical path")
    sched = simulate(prio, POLICIES["critical-path-first"], 2)["_schedule"]
    check(close(sched[2][0], 0.0) and close(sched[3][0], 3.0), f"priority schedule: {sched}")

    # An rmeta-pipelined pair: a (4 s, metadata at 1 s) releases b (5 s) on its metadata.
    # Pipelined on two slots: b runs 1-6, so 6 s; on one slot b waits for a's slot: 9 s.
    # Without pipelining b waits for all of a: 9 s on any -j.
    pair_units = [_u(0, 4.0, rmeta=[1], frontend=1.0), _u(1, 5.0, start=1.0, frontend=2.0)]
    pair = _graph(pair_units, jobs=2)
    check(pair.edges == {"full": 0, "rmeta": 1, "dropped": 0}, f"pair edges: {pair.edges}")
    check(close(wall(pair, "pipelined", 2), 6.0), f"pair pipelined -j 2: {wall(pair, 'pipelined', 2)}")
    check(close(wall(pair, "pipelined", 1), 9.0), f"pair pipelined -j 1: {wall(pair, 'pipelined', 1)}")
    check(close(wall(pair, "critical-path-first", 2), 9.0), f"pair unpipelined: {wall(pair, 'critical-path-first', 2)}")
    check(close(wall(pair, "cargo", 2), 6.0) and close(pair.wall, 6.0), f"pair cargo replay: {wall(pair, 'cargo', 2)}")
    check(close(simulate(pair, POLICIES["pipelined"], 2)["critical_path_s"], 6.0)
          and close(simulate(pair, POLICIES["fifo"], 2)["critical_path_s"], 9.0), "pair critical paths")

    # A zero-length front end: a (10 s, metadata at 0) releases d (100 s) at once, beside an
    # independent b (2 s). Pipelined on two slots, d takes the second slot at t=0 and the build
    # ends at 100, not 102 after b; a release must be applied before filling another slot.
    instant = _graph([_u(0, 10.0, rmeta=[1], frontend=0.0), _u(1, 100.0), _u(2, 2.0)], jobs=2)
    check(close(wall(instant, "pipelined", 2), 100.0), f"instant metadata pipelined -j 2: {wall(instant, 'pipelined', 2)}")

    # A releaser with no frontend section: its metadata time comes from the recording.
    nosec = _graph([_u(0, 4.0, rmeta=[1]), _u(1, 1.0, start=1.5)])
    check(nosec.meta_from_recording == 1 and close(nosec.units[0].meta, 1.5), f"metadata from recording: {nosec.units[0].meta}")

    # Cache hits: one root (1 s) releases four 2 s units, two of them hits at 0.1 s. Holding a
    # slot, the hits run after the misses (shortest remaining path); cache-aware, they take
    # none. Other policies ignore the hits.
    hitfan = _graph([_u(0, 1.0, full=[1, 2, 3, 4])] + [_u(k, 2.0, start=1.0) for k in (1, 2, 3, 4)])
    held = CacheHits(frozenset({3, 4}), 0.1, aware=False)
    aware = CacheHits(frozenset({3, 4}), 0.1, aware=True)
    for j, want_held, want_aware in ((1, 5.2, 5.0), (2, 3.1, 3.0)):
        check(close(wall(hitfan, "cache-hits", j, held), want_held), f"hits held -j {j}: {wall(hitfan, 'cache-hits', j, held)}")
        check(close(wall(hitfan, "cache-hits", j, aware), want_aware), f"hits aware -j {j}: {wall(hitfan, 'cache-hits', j, aware)}")
    r = simulate(hitfan, POLICIES["cache-hits"], 2, aware)
    check(close(r["busy_slot_s"], 5.0) and r["hit_units"] == 2 and r["cache_aware"], f"hits aware busy: {r['busy_slot_s']}")
    check(close(simulate(hitfan, POLICIES["cache-hits"], 2, held)["busy_slot_s"], 5.2), "hits held busy")
    check(close(wall(hitfan, "pipelined", 2, held), 5.0), f"hits ignored by pipelined: {wall(hitfan, 'pipelined', 2, held)}")
    check(close(simulate(hitfan, POLICIES["cache-hits"], 2, held)["critical_path_s"], 3.0), "hits critical path")

    chosen, unmatched = select_hits(hitfan, 0.4, None, seed=7)
    again, _ = select_hits(hitfan, 0.4, None, seed=7)
    check(len(chosen) == 2 and chosen == again, f"hit fraction: {sorted(chosen)}")
    chosen, unmatched = select_hits(hitfan, None, ["u3", "u4@0.1.0", "nope"], seed=0)
    check(chosen == frozenset({3, 4}) and unmatched == ["nope"], f"hit units: {sorted(chosen)}, {unmatched}")

    # Malformed graphs: a cycle is refused, an edge to an unknown unit dropped.
    try:
        _graph([_u(0, 1.0, full=[1]), _u(1, 1.0, full=[0])])
        failures.append("a cycle was accepted")
    except ValueError:
        pass
    check(_graph([_u(0, 1.0, full=[7])]).edges["dropped"] == 1, "an edge to an unknown unit was kept")

    # The report parser, and the way from a results record to its report.
    html = ("<html><script>\nconst UNIT_DATA = " + json.dumps(pair_units) + ";\nconst CONCURRENCY_DATA = "
            + json.dumps([{"t": 0.0, "active": 1, "waiting": 0, "inactive": 1}, {"t": 1.0, "active": 2, "waiting": 0, "inactive": 0}])
            + ";\nconst CPU_USAGE = [];\n</script><table>\n<tr>\n<td>Max concurrency:</td>\n<td>2 (jobs=2 ncpu=8)</td>\n</tr>\n"
            "<tr><td>Build start:</td><td>2026-10-22T17:23:53.544745Z</td></tr></table></html>")
    rep = parse_report_text(html)
    check((rep["jobs"], rep["ncpu"], rep["max_concurrency"], len(rep["units"])) == (2, 8, 2, 2), f"parse: {rep['jobs']}, {rep['ncpu']}")
    check(close(build_graph(rep).wall, 6.0), "parsed graph")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report = root / "raw" / "ripgrep" / "baseline-stable" / "deps-only" / "run-1" / REPORT_NAME
        report.parent.mkdir(parents=True)
        report.write_text(html)
        base = {"workspace": "BurntSushi/ripgrep", "config": "baseline-stable", "run": 1, "wall_s": 7.0,
                "recorded_at": "2026-10-22T17:24:01+00:00", "machine": {"jobs": 2}}
        lines = [dict(base, scenario="cold-build"), dict(base, scenario="deps-only", derived_from="cold-build"),
                 dict(base, scenario="noop", run=2), dict(base, scenario="check", error="cargo exited 101")]
        results = root / "results.jsonl"
        results.write_text("".join(json.dumps(x) + "\n" for x in lines))
        found, warnings = sources_from_jsonl(results, root / "raw", {}, Filters())
        check(len(found) == 1 and found[0].report == report, f"jsonl sources: {[s.report for s in found]}")
        check(len(warnings) == 1 and "noop" in warnings[0], f"jsonl warnings: {warnings}")
        check(stale_report(found[0].record if found else None, "2026-10-22T17:23:53.544745Z") is None, "fresh report flagged")
        check(stale_report(dict(base, recorded_at="2026-10-22T19:00:00+00:00"), "2026-10-22T17:23:53.544745Z") is not None,
              "stale report not flagged")
        res = analyse(found[0], [1, 2], list(DEFAULT_POLICIES), None, None) if found else {}
        check(close((res.get("sanity") or {}).get("diff_s"), 0.0), f"analyse sanity: {res.get('sanity')}")
        check(len(res.get("results", [])) == 2 * len(DEFAULT_POLICIES), "analyse rows")
        check(summarize([dict(res, group="g"), dict(res, group="g")])[0]["recordings"] == 2 if res else False, "summary")

    if failures:
        for f in failures:
            print(f"simulate.py self-test: FAILED: {f}", file=sys.stderr)
        return 1
    print("simulate.py self-test: ok")
    return 0


# --- main -----------------------------------------------------------------------------------


def _int_list(text: str, flag: str) -> list[int]:
    try:
        values = [int(v) for v in text.split(",") if v.strip()]
    except ValueError:
        sys.exit(f"{flag} takes comma-separated integers")
    if not values or any(v < 1 for v in values):
        sys.exit(f"{flag} takes integers of at least 1")
    return values


def _str_list(text: str | None) -> list[str] | None:
    return [v.strip() for v in text.split(",") if v.strip()] if text else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], epilog=NOTE,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="*", type=Path,
                    help="cargo-timing.html reports (or run-N directories holding one), or results JSONL files from bench/run.py")
    ap.add_argument("--jobs", default=",".join(map(str, DEFAULT_JOBS)), help="job counts to simulate (default: 1,2,4,8,16)")
    ap.add_argument("--policies", help=f"comma-separated, from: {', '.join(POLICIES)} "
                                       f"(default: {', '.join(DEFAULT_POLICIES)}, and cache-hits when hits are given)")
    hit = ap.add_mutually_exclusive_group()
    hit.add_argument("--hit-fraction", type=float, help="this fraction of the units hit the cache (a seeded random choice)")
    hit.add_argument("--hit-units", help="comma-separated patterns for the units that hit: package name, name@version or the unit label")
    ap.add_argument("--hit-time", type=float, default=DEFAULT_HIT_TIME,
                    help=f"seconds a cache hit takes (default {DEFAULT_HIT_TIME}, an assumed placeholder, not a measurement)")
    ap.add_argument("--cache-aware", action="store_true", help="hits take no job slot (Layer 3's cache-aware scheduling)")
    ap.add_argument("--seed", type=int, default=0, help="seed for --hit-fraction (default 0)")
    ap.add_argument("--recorded-jobs", type=int, help="the -j the recording ran with, when the report does not say")
    ap.add_argument("--scenario", help="JSONL input: only these scenarios (comma-separated)")
    ap.add_argument("--config", help="JSONL input: only these configurations (comma-separated)")
    ap.add_argument("--run", help="JSONL input: only these run indexes (comma-separated)")
    ap.add_argument("--workspace", help="JSONL input: only workspaces whose name contains this")
    ap.add_argument("--raw", type=Path, help="JSONL input: where run.py left the raw traces (default: <JSONL dir>/raw)")
    ap.add_argument("--group", help="reports given directly: a shared group for their combined median "
                                    "(default: each report is its own group, so unrelated builds are not pooled)")
    ap.add_argument("--corpus", type=Path, default=HERE / "corpus.toml", help="maps a result's workspace to its raw directory name")
    ap.add_argument("--json", action="store_true", help="print JSON instead of tables")
    ap.add_argument("--self-test", action="store_true", help="check the simulator on synthetic DAGs with known answers, then exit")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.sources:
        ap.print_usage(sys.stderr)
        print("simulate.py: give at least one report or results file (or --self-test)", file=sys.stderr)
        return 2

    jobs_list = _int_list(args.jobs, "--jobs")
    hits_given = args.hit_fraction is not None or bool(args.hit_units)
    if args.hit_fraction is not None and not 0.0 <= args.hit_fraction <= 1.0:
        sys.exit("--hit-fraction is between 0 and 1")
    if args.hit_time < 0:
        sys.exit("--hit-time cannot be negative")
    if args.cache_aware and not hits_given:
        sys.exit("--cache-aware needs --hit-fraction or --hit-units")
    if args.recorded_jobs is not None and args.recorded_jobs < 1:
        sys.exit("--recorded-jobs is at least 1")
    policies = _str_list(args.policies) or list(DEFAULT_POLICIES) + (["cache-hits"] if hits_given else [])
    for p in policies:
        if p not in POLICIES:
            sys.exit(f"unknown policy `{p}`; choose from {', '.join(POLICIES)}")
    if "cache-hits" in policies and not hits_given:
        sys.exit("the cache-hits policy needs --hit-fraction or --hit-units")
    hit_args = None
    if hits_given:
        hit_args = {"fraction": args.hit_fraction, "patterns": _str_list(args.hit_units), "seed": args.seed,
                    "time": args.hit_time, "aware": args.cache_aware}

    filters = Filters(scenarios=_str_list(args.scenario), configs=_str_list(args.config),
                      runs=_int_list(args.run, "--run") if args.run else None, workspace=args.workspace)
    names = corpus_names(args.corpus)
    sources: list[Source] = []
    notes: list[str] = []
    for path in args.sources:
        path = path.expanduser()
        if path.is_dir():
            path = path / REPORT_NAME
        if not path.is_file():
            sys.exit(f"no such file: {path}")
        if path.suffix == ".jsonl":
            raw_root = (args.raw or path.parent / "raw").expanduser()
            found, warnings = sources_from_jsonl(path, raw_root, names, filters)
            sources.extend(found)
            notes.extend(warnings)
        else:
            # Each report given directly is a separate build, so it gets its own group unless the
            # user says these reports belong together; summarize() then skips a group of one.
            sources.append(Source(report=path, title=str(path), group=args.group or str(path)))
    for n in notes:
        print(f"simulate.py: {n}", file=sys.stderr)
    if not sources:
        print("simulate.py: no timing reports to simulate", file=sys.stderr)
        return 2

    results = [analyse(s, jobs_list, policies, hit_args, args.recorded_jobs) for s in sources]
    doc = {
        "schema": SCHEMA,
        "kind": "simulated-estimate",
        "note": NOTE,
        "assumptions": {
            "durations": "fixed at their recorded values, at every -j",
            "edges": "only the dependency that released each unit (cargo's unblocked_units / unblocked_rmeta_units)",
            "not_modelled": ["memory", "jobserver tokens shared with rustc's own threads",
                             "build-script internals", "linker contention"],
            "hit_time_s": args.hit_time if hits_given else None,
        },
        "policies": {p: POLICIES[p].summary for p in policies},
        "jobs": jobs_list,
        "sources": results,
        "summary": summarize(results),
    }
    if args.json:
        print(json.dumps(_rounded(doc), indent=2, sort_keys=True))
    else:
        print_text(doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
