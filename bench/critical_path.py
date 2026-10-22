#!/usr/bin/env python3
"""The build's critical path, from cargo's timing data (design §2 hypothesis 7 and §6, plan §4).

Layer 3 (`qrcd`, design §6) is to schedule the critical path first and to start dependents as
soon as `.rmeta` exists. Whether that can pay off depends on numbers this module reads off a cold
build's `--timings` report: how long the longest chain of dependent units is, how much of the
wall time it covers, and how much parallelism the build has around it.

    python3 bench/critical_path.py bench/results/raw/ripgrep/baseline-stable/cold-build/run-1/cargo-timing.html
    python3 bench/critical_path.py --self-test

bench/run.py adds a summary of it to each cold scenario's record (`critical_path`), and
bench/analyze.py prints one line per cold scenario from that. Standard library only.

The graph
---------
cargo's report has one entry per unit it ran (UNIT_DATA: `i`, `name`, `version`, `mode`,
`target`, `start`, `duration`, `unblocked_units`, `unblocked_rmeta_units`, `sections`; `i` is the
unit's id, not its position in the list). A unit's `unblocked_units` are the units its completion
released, the ones for which it was the last dependency outstanding. Its `unblocked_rmeta_units`
are the ones its metadata released: with pipelining, a unit that needs only a dependency's
`.rmeta` starts when that is written. Each is an edge u -> v here. A full edge costs u's whole
duration; a metadata edge costs u's time to its metadata, which is the end of its `frontend`
section (cargo's sections are relative to the unit's start), or its whole duration when the
report has no `frontend` section for it.

The path
--------
Every unit starts as soon as its incoming edges allow, or at 0 if no unit released it (it was
ready when the build began), and runs for its recorded duration: the schedule the build would
follow with unlimited job slots and the same durations. The critical path is the chain that ends
last in that schedule. Its length is `critical_path_s`, which is by definition also
`ideal_wall_s`, the wall time with unlimited jobs. `parallelism` is the total unit time divided by
it: the average number of units that schedule keeps busy. With fewer job slots than that, the
slots rather than the chain are the limit; with J slots no schedule beats
max(critical path, total unit time / J).

The chain normally ends at the unit that finished last in the real build (usually the link of a
binary), and `ends_at_last_unit` says whether it did. When it did not, the last unit was released
early and then waited for a slot, and the longest chain, not the last unit's, is what bounds the
build, so the longest chain is the one reported.

What cargo's numbers mean
-------------------------
`start` is when cargo began the unit, which it does only once it holds a job slot (a jobserver
token) for it, and `duration` runs from then until cargo recorded the unit finished. Time a unit
spent released but waiting for a slot is therefore not in its duration: it lies in the gap between
its releaser finishing and its own start, and this analysis leaves it out, which is what makes the
path an ideal. Everything after the start is in: rustc's process start-up, the compilation, the
waits inside rustc for extra jobserver tokens for parallel codegen (it runs on fewer threads when
none are free), and contention for cores, memory and disk with the units running beside it. The
durations are those of this build at this `-j`, not of each unit alone on an idle machine. The
analysis takes them as fixed, although with more slots free a unit's codegen could get more
threads, and with more units beside it, less of each core.

Limits
------
- Only the releasing edge. cargo records, for each unit, only the dependency whose completion (or
  metadata) released it, not every dependency. Each recorded edge is a real dependency, so the
  path found here is a path of the full unit graph, and its length a lower bound on that graph's
  longest path with the same durations: a longer chain whose units finished early in this build,
  because they happened to start early, does not appear. The full graph is
  `cargo build --unit-graph` (nightly, plan §4).
- Units only. The wall time also holds cargo's own work before the first unit starts (resolving,
  fingerprinting) and after the last one ends. No unit contains it, so no path does.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

# The scenarios whose record carries `critical_path`: the ones that start from an empty target
# directory. bench/run.py records a `deps-only` run under `cold-build`.
COLD_SCENARIOS = ("cold-build", "deps-only", "cold-test", "check")
TOP = 5


def label(u: dict) -> str:
    """A unit as cargo's report names it: `regex-syntax`, `ripgrep rg "bin"`, `libc build-script (run)`."""
    target = (u.get("target") or "").strip()
    name = u.get("name") or "?"
    return f"{name} {target}" if target else name


def metadata_s(u: dict) -> float:
    """Seconds from a unit's start until its metadata was ready: the end of its `frontend`
    section, or its whole duration when the report has none (build scripts, binaries, units
    built without pipelining)."""
    duration = max(0.0, float(u.get("duration") or 0.0))
    ends = [float(span["end"]) for name, span in (u.get("sections") or []) if name == "frontend"]
    return max(0.0, min(max(ends), duration)) if ends else duration


def compute(units: list[dict]) -> dict:
    """The critical path of one build's UNIT_DATA (see the module docstring for the model).

    Returns `critical_path_s` (= `ideal_wall_s`), `total_unit_s`, `parallelism` (total unit
    seconds / critical path; None for an empty build), `ends_at_last_unit`, and `path`, the units
    on it in order. Each path entry has the unit's `name`, `version`, `target`, `mode`, its
    observed `start_s` and `duration_s`, its `ideal_start_s` in the unlimited-jobs schedule,
    `path_s` (the part of it on the path: up to its metadata when the next unit waited only for
    that, else all of it) and `waited_on`, what the unit waited for from the one before it
    (`"rmeta"` or `"full"`; None for the first). The `path_s` values sum to `critical_path_s`.

    Raises ValueError on unit data that is not a DAG (a duplicate id or a cycle).
    """
    by_id: dict[int, dict] = {}
    for n, u in enumerate(units):
        i = u.get("i", n)
        if i in by_id:
            raise ValueError(f"unit id {i} appears twice")
        by_id[i] = u
    if not by_id:
        return {"critical_path_s": 0.0, "ideal_wall_s": 0.0, "total_unit_s": 0.0, "parallelism": None,
                "ends_at_last_unit": True, "path": []}

    duration = {i: max(0.0, float(u.get("duration") or 0.0)) for i, u in by_id.items()}
    meta = {i: metadata_s(u) for i, u in by_id.items()}
    edges: dict[int, list[tuple[int, str]]] = {i: [] for i in by_id}
    indegree = dict.fromkeys(by_id, 0)
    for i, u in by_id.items():
        for kind, key in (("full", "unblocked_units"), ("rmeta", "unblocked_rmeta_units")):
            for v in u.get(key) or []:
                # The report lists only the units cargo ran; a reference to any other is dropped.
                if v in by_id:
                    edges[i].append((v, kind))
                    indegree[v] += 1

    # Longest path by Kahn's algorithm: a unit's earliest start is final once every unit that
    # can release it has been visited.
    start = dict.fromkeys(by_id, 0.0)
    pred: dict[int, tuple[int, str] | None] = dict.fromkeys(by_id)
    ready = [i for i, d in indegree.items() if d == 0]
    visited = 0
    while ready:
        u = ready.pop()
        visited += 1
        for v, kind in edges[u]:
            t = start[u] + (meta[u] if kind == "rmeta" else duration[u])
            if pred[v] is None or t > start[v]:
                start[v], pred[v] = t, (u, kind)
            indegree[v] -= 1
            if indegree[v] == 0:
                ready.append(v)
    if visited != len(by_id):
        raise ValueError(f"the unit data has a cycle ({len(by_id) - visited} units on or after it)")

    end_at = {i: start[i] + duration[i] for i in by_id}
    last = max(by_id, key=lambda i: float(by_id[i].get("start") or 0.0) + duration[i])
    end = max(by_id, key=lambda i: (end_at[i], i == last))  # on a tie, the unit that finished last

    chain = [end]
    while (p := pred[chain[-1]]) is not None:
        chain.append(p[0])
    chain.reverse()
    path = []
    for k, i in enumerate(chain):
        u = by_id[i]
        nxt = pred[chain[k + 1]] if k + 1 < len(chain) else None
        path.append({
            "i": i,
            "name": u.get("name", ""),
            "version": u.get("version", ""),
            "target": (u.get("target") or "").strip(),
            "mode": u.get("mode", ""),
            "start_s": round(float(u.get("start") or 0.0), 3),
            "ideal_start_s": round(start[i], 3),
            "duration_s": round(duration[i], 3),
            "path_s": round(meta[i] if nxt is not None and nxt[1] == "rmeta" else duration[i], 3),
            "waited_on": pred[i][1] if pred[i] else None,
        })
    total = sum(duration.values())
    cp = end_at[end]
    return {
        "critical_path_s": round(cp, 3),
        "ideal_wall_s": round(cp, 3),
        "total_unit_s": round(total, 3),
        "parallelism": round(total / cp, 2) if cp > 0 else None,
        "ends_at_last_unit": end == last,
        "path": path,
    }


def summary(units: list[dict], top: int = TOP) -> dict:
    """The record's `critical_path` field (bench/run.py, cold scenarios): the path's length, its
    unit count, how many of its links waited only on `.rmeta`, the total unit time and the
    parallelism bound, and the `top` units by their time on the path. Unit data that is not a
    DAG gives `{"error": ...}` rather than stopping the benchmark run."""
    try:
        cp = compute(units)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    ranked = sorted(cp["path"], key=lambda u: -u["path_s"])[:top]
    return {
        "seconds": cp["critical_path_s"],
        "units": len(cp["path"]),
        "rmeta_links": sum(1 for u in cp["path"] if u["waited_on"] == "rmeta"),
        "total_unit_s": cp["total_unit_s"],
        "parallelism": cp["parallelism"],
        "ends_at_last_unit": cp["ends_at_last_unit"],
        "top": [{k: u[k] for k in ("name", "version", "target", "duration_s", "path_s", "waited_on")} for u in ranked],
    }


def unit_text(u: dict) -> str:
    """`regex-automata 1.23 s (until .rmeta)`: a path unit and its time on the path."""
    until = " (until .rmeta)" if u["path_s"] < u["duration_s"] else ""
    return f"{label(u)} {u['path_s']:.2f} s{until}"


def report_lines(by_scenario: dict) -> list[str]:
    """One markdown bullet per cold scenario with critical-path data, for bench/analyze.py.

    `by_scenario` is analyze.py's {scenario: [records]} for one workspace and setting. The wall
    time is the median over the runs that have the data; the path, its unit count and its three
    longest units come from the run with the median critical path (the lower of the middle two
    for an even count), so they describe one real build.
    """
    lines = []
    for scenario in COLD_SCENARIOS:
        runs = [r for r in by_scenario.get(scenario, []) if "error" not in r and "seconds" in (r.get("critical_path") or {})]
        if not runs:
            continue
        runs.sort(key=lambda r: r["critical_path"]["seconds"])
        mid = runs[(len(runs) - 1) // 2]["critical_path"]
        wall = statistics.median([r["wall_s"] for r in runs])
        of_wall = f"{100 * mid['seconds'] / wall:.0f}%" if wall else "n/a"
        longest = ", ".join(unit_text(u) for u in mid["top"][:3]) or "none"
        lines.append(f"- {scenario}: critical path {mid['seconds']:.2f} s of {wall:.2f} s wall ({of_wall} of wall); "
                     f"{mid['units']} units on it; longest: {longest}.")
    return lines


def self_test() -> int:
    """The graph, the path, the summary and the report line on small synthetic UNIT_DATA."""

    def unit(i, name, start, duration, frontend=None, full=(), rmeta=(), target=""):
        return {"i": i, "name": name, "version": "1.0.0", "mode": "todo", "target": target, "features": [],
                "start": start, "duration": duration, "unblocked_units": list(full), "unblocked_rmeta_units": list(rmeta),
                "sections": None if frontend is None else [["frontend", {"start": 0.0, "end": frontend}],
                                                           ["codegen", {"start": frontend, "end": duration}]]}

    # a's metadata releases b, b's metadata releases c, a's completion releases the binary, and
    # slow depends on nothing. Listed out of id order, as cargo lists them. With unlimited jobs:
    # a 0 -> 4.0, b 1.0 -> 3.0, c 2.5 -> 6.0, app 4.0 -> 5.5, slow 0 -> 3.0.
    a = unit(0, "a", 0.1, 4.0, frontend=1.0, full=[3], rmeta=[1])
    b = unit(1, "b", 1.15, 2.0, frontend=1.5, rmeta=[2])
    c = unit(2, "c", 2.7, 3.5, full=[99])  # 99 is not in the report: dropped
    app = unit(3, "app", 4.2, 1.5, target=' app "bin"')
    slow = unit(4, "slow", 0.1, 3.0)
    units = [app, a, slow, c, b]

    cp = compute(units)
    assert cp["critical_path_s"] == cp["ideal_wall_s"] == 6.0, cp
    assert [u["name"] for u in cp["path"]] == ["a", "b", "c"], cp["path"]
    assert [u["path_s"] for u in cp["path"]] == [1.0, 1.5, 3.5], cp["path"]
    assert [u["ideal_start_s"] for u in cp["path"]] == [0.0, 1.0, 2.5], cp["path"]
    assert [u["waited_on"] for u in cp["path"]] == [None, "rmeta", "rmeta"], cp["path"]
    assert cp["total_unit_s"] == 14.0 and cp["parallelism"] == 2.33 and cp["ends_at_last_unit"], cp

    # A longer binary moves the path onto the full edge: a counts whole, the binary waited on it.
    long_app = dict(app, duration=3.0)
    cp = compute([long_app, a, slow, c, b])
    assert [(u["name"], u["path_s"], u["waited_on"]) for u in cp["path"]] == [("a", 4.0, None), ("app", 3.0, "full")], cp["path"]
    assert cp["critical_path_s"] == 7.0 and cp["ends_at_last_unit"], cp
    assert label(cp["path"][-1]) == 'app app "bin"', label(cp["path"][-1])

    # A unit that waited for a slot finishes last on a short chain of its own; the longest
    # chain is still the one that bounds the build.
    late = unit(5, "late", 7.0, 0.5)
    cp = compute(units + [late])
    assert cp["critical_path_s"] == 6.0 and not cp["ends_at_last_unit"], cp
    assert cp["path"][-1]["name"] == "c", cp["path"]

    # A metadata edge from a unit without sections costs the unit's whole duration.
    cp = compute([unit(0, "x", 0.0, 2.0, rmeta=[1]), unit(1, "y", 2.0, 1.0)])
    assert cp["critical_path_s"] == 3.0 and [u["path_s"] for u in cp["path"]] == [2.0, 1.0], cp
    assert [u["waited_on"] for u in cp["path"]] == [None, "rmeta"], cp

    # Not a DAG: the record says so instead of the run failing.
    assert "error" in summary([unit(0, "p", 0.0, 1.0, full=[1]), unit(1, "q", 1.0, 1.0, full=[0])])
    assert "error" in summary([unit(0, "p", 0.0, 1.0), unit(0, "q", 1.0, 1.0)])
    assert compute([])["critical_path_s"] == 0.0 and summary([])["units"] == 0

    s = summary(units)
    assert (s["seconds"], s["units"], s["rmeta_links"], s["total_unit_s"], s["parallelism"]) == (6.0, 3, 2, 14.0, 2.33), s
    assert [(t["name"], t["path_s"], t["duration_s"]) for t in s["top"]] == [("c", 3.5, 3.5), ("b", 1.5, 2.0), ("a", 1.0, 4.0)], s

    # The report: wall is the median of the runs, the path is the median run's; failed runs and
    # warm scenarios add no line.
    by_scenario = {
        "cold-build": [{"scenario": "cold-build", "wall_s": w, "critical_path": s} for w in (9.0, 7.0, 8.0)],
        "check": [{"scenario": "check", "wall_s": 1.0, "error": "cargo exited 101"}],
        "noop": [{"scenario": "noop", "wall_s": 0.2}],
    }
    want = ("- cold-build: critical path 6.00 s of 8.00 s wall (75% of wall); 3 units on it; "
            "longest: c 3.50 s, b 1.50 s (until .rmeta), a 1.00 s (until .rmeta).")
    assert report_lines(by_scenario) == [want], report_lines(by_scenario)
    assert report_lines({}) == []
    print("critical_path.py self-test: ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("reports", nargs="*", type=Path, help="cargo-timing.html files, such as the ones under bench/results/raw/")
    ap.add_argument("--self-test", action="store_true", help="check the path and the report line on synthetic unit data, then exit")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.reports:
        ap.error("give one or more cargo-timing.html files, or --self-test")
    from run import parse_timing_report  # run.py imports this module, so not at the top

    for report in args.reports:
        cp = compute(parse_timing_report(report)["units"])
        print(report)
        print(f"  critical path {cp['critical_path_s']:.2f} s over {len(cp['path'])} units; total unit time "
              f"{cp['total_unit_s']:.2f} s; parallelism bound {cp['parallelism']}")
        if not cp["ends_at_last_unit"]:
            print("  (the unit that finished last is not on it: it waited for a slot on a shorter chain)")
        print(f"  {'ideal start':>11}  {'actual start':>12}  {'on path':>8}  {'waited on':<9}  unit")
        for u in cp["path"]:
            print(f"  {u['ideal_start_s']:9.2f} s  {u['start_s']:10.2f} s  {u['path_s']:6.2f} s  {u['waited_on'] or '-':<9}  {label(u)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
