#!/usr/bin/env python3
"""Summarize Phase 0 results (docs/phase-0.md §5): medians and spread per scenario, the
time-share breakdown per workspace as a table and a stacked bar (text, or SVG files with
--svg-dir), and the top hypotheses.

    python3 bench/analyze.py bench/results/*.jsonl            # markdown on stdout
    python3 bench/analyze.py --out docs/benchmarks/phase-0.md bench/results/*.jsonl
    python3 bench/analyze.py --out docs/benchmarks/phase-0.md --svg-dir docs/benchmarks/phase-0 bench/results/*.jsonl
    python3 bench/analyze.py --self-test                       # the fixture under bench/tests

Standard library only. Reads the JSON objects bench/run.py writes, one per line.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

import critical_path

HERE = Path(__file__).resolve().parent
SHARE_KEYS = ("deps", "proc_macro_build_rs", "front_end", "codegen", "debuginfo", "link", "idle")
HYPOTHESIS = {
    "deps": "1. cold dependency compilation",
    "proc_macro_build_rs": "2. proc macros and build.rs",
    "front_end": "3. front end",
    "codegen": "4. monomorphization and codegen",
    "debuginfo": "5. debug info",
    "link": "6. linking",
    "idle": "7. scheduling gaps (idle slots)",
}
SCENARIO_ORDER = ("cold-build", "cold-test", "check", "noop", "touch-leaf", "touch-core", "deps-only",
                  "cache-cold", "cache-hit", "cache-partial", "cache-warm-local")
BAR = "█"
COLD = ("cold-build", "cold-test")  # the scenarios whose time share the report breaks down

# Chart colours: one fixed hue per hypothesis, in SHARE_KEYS order, so a hue names the same
# hypothesis in every chart whichever segments it skips. Blue #2a78d6, orange #eb6834,
# aqua #1baf7a, yellow #eda100, magenta #e87ba4, green #008300, violet #6250d6. In this order,
# neighbouring segments stay apart under simulated protanopia and deuteranopia (OKLab distance
# of 0.09 or more on white), both with all seven shares (nightly) and without debuginfo and
# link (stable). Colour is never the only cue: wide segments carry their share as text, the
# legend names every segment, and the table sits next to the chart.
PALETTE = dict(zip(SHARE_KEYS, ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6")))
INK, INK_MUTED, SURFACE, TRACK = "#1a1a19", "#52514e", "#ffffff", "#e3e3e0"
FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


def load(paths: list[Path]) -> list[dict]:
    rows = []
    for p in paths:
        for n, line in enumerate(p.read_text().splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                sys.exit(f"{p}:{n}: {e}")
    return rows


def median(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def spread(values: list[float]) -> float | None:
    """(max - min) / median, the run-to-run variance the plan asks to report."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    med = statistics.median(vals)
    return (max(vals) - min(vals)) / med if med else None


def fmt(v: float | None, digits: int = 1, unit: str = "") -> str:
    return "n/a" if v is None else f"{v:.{digits}f}{unit}"


def pct(v: float | None) -> str:
    return "n/a" if v is None else f"{100 * v:.0f}%"


def setting(r: dict) -> str:
    """The configuration a run belongs to, including the machine and -j: runs from different
    computers or job counts are separate baselines, never one median. So is a build through a
    cache (Phase 2): the cache and its backend are part of the setting. Records written before
    the `cache` field existed are uncached."""
    m = r.get("machine") or {}
    cache = r.get("cache") or "none"
    through = "" if cache == "none" else f" + {cache} ({r.get('cache_backend') or 'local'})"
    return f"{r['config']}{through} on {m.get('label', 'unknown machine')} (-j {m.get('jobs', '?')})"


def group(rows: list[dict]) -> dict:
    """{workspace: {setting: {scenario: [runs]}}}; failed runs stay in their scenario."""
    g: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in rows:
        g[r["workspace"]][setting(r)][r["scenario"]].append(r)
    return g


def _test_run_exit(r: dict) -> int | None:
    """The test run's exit code, when the scenario ran the tests after building them."""
    return (r.get("test_run") or {}).get("exit")


def scenario_table(by_scenario: dict) -> list[str]:
    # A hit-rate column only where some run went through a build cache (Phase 2).
    cached = any(r.get("cache_stats") for runs in by_scenario.values() for r in runs)
    lines = ["| Scenario | Runs | Wall (median) | Spread | CPU time (user+sys) | CPU use | Peak RSS | Units | Test binaries |"
             + (" Hit rate |" if cached else ""),
             "|---|---|---|---|---|---|---|---|---|" + ("---|" if cached else "")]
    for s in sorted(by_scenario, key=lambda x: SCENARIO_ORDER.index(x) if x in SCENARIO_ORDER else 99):
        runs = [r for r in by_scenario[s] if "error" not in r]
        errors = len(by_scenario[s]) - len(runs)
        if not runs:
            lines.append(f"| {s} | 0 ({errors} failed) | | | | | | | |" + (" |" if cached else ""))
            continue
        walls = [r["wall_s"] for r in runs]
        cpu = [None if r.get("user_s") is None else r["user_s"] + r["sys_s"] for r in runs]
        sp = spread(walls)
        # The plan's rule: above 5 percent, run 5. Not for sub-second scenarios, where the
        # spread is scheduler noise measured in tens of milliseconds.
        wide = sp is not None and sp > 0.05 and (max(walls) - min(walls)) > 0.2
        flag = " (above 5%: run 5)" if wide and len(runs) < 5 else ""
        # Built test binaries tell nothing about whether the tests ran: a cold-test row whose test
        # run exited non-zero is marked, and the runs themselves are listed under the tables.
        test_failed = sum(1 for r in runs if _test_run_exit(r) not in (None, 0))
        bins = fmt(median([r.get("test_binaries") for r in runs]), 0)
        if test_failed:
            bins += f" ({test_failed} test runs failed)"
        lines.append(
            f"| {s} | {len(runs)}{' (+%d failed)' % errors if errors else ''} | {fmt(median(walls), 2, ' s')} | "
            f"{pct(sp)}{flag} | {fmt(median(cpu), 1, ' s')} | {pct(median([r.get('cpu_utilization') for r in runs]))} | "
            f"{fmt(median([r.get('peak_rss_mib') for r in runs]), 0, ' MiB')} | {fmt(median([r.get('units') for r in runs]), 0)} | "
            f"{bins} |"
            + (f" {pct(median([(r.get('cache_stats') or {}).get('hit_rate') for r in runs]))} |" if cached else "")
        )
    return lines


def share_rows(runs: list[dict]) -> dict[str, float | None]:
    out = {}
    for k in SHARE_KEYS:
        out[k] = median([(r.get("share") or {}).get(k) for r in runs])
    return out


def share_runs(by_scenario: dict, scenario: str) -> list[dict]:
    """The successful runs of a scenario that carry a time-share breakdown."""
    return [r for r in by_scenario.get(scenario, []) if "error" not in r and r.get("share")]


def bar(shares: dict[str, float | None], width: int = 60) -> str:
    parts, legend = [], []
    for k in SHARE_KEYS:
        v = shares.get(k)
        if v:
            n = max(1, round(v * width))
            parts.append(BAR * n)
            legend.append(f"{k} {pct(v)}")
    return "".join(parts) + "\n" + "  " + " · ".join(legend)


def slug(*parts: str) -> str:
    """A file-name stem of [a-z0-9-]: ('example/ws', 'baseline-stable on fixture (-j 8)',
    'cold-build') gives 'example-ws-baseline-stable-on-fixture-j-8-cold-build'."""
    return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-") or "chart"


def luminance(colour: str) -> float:
    """WCAG relative luminance of a #rrggbb colour."""
    def linear(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = (linear(int(colour[i:i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def label_colour(fill: str) -> str:
    """White or ink for text on a fill, whichever has the higher contrast ratio."""
    return "#ffffff" if contrast("#ffffff", fill) >= contrast(INK, fill) else INK


def text_width(text: str, size: float) -> float:
    """A generous estimate of the rendered width of sans-serif text: a hand-written SVG has no
    layout engine to ask."""
    return 0.62 * size * len(text)


def bar_svg(workspace: str, setting_name: str, scenario: str, shares: dict[str, float | None], runs: int) -> str:
    """One horizontal stacked bar of the shares, as a standalone SVG document.

    The bar is the build's whole slot capacity. Null and zero shares are skipped. When the
    shares sum to less than 1.0 the rest is drawn as an empty track, "not attributed"; when
    they sum to more (the sources overlap) the bar is scaled to fit. Every text value is
    XML-escaped."""
    pad, font, bar_y, bar_h, gap = 16, 12, 64, 28, 2
    uid = "chart-" + slug(workspace, setting_name, scenario)
    drawn = [(k, shares[k]) for k in SHARE_KEYS if shares.get(k) is not None and shares[k] > 0]
    total = sum(v for _, v in drawn)
    rest = 1 - total if total < 0.995 else 0.0  # slot capacity neither source attributes
    scale = 1 / (total + rest)
    parts = [(HYPOTHESIS[k], v, PALETTE[k]) for k, v in drawn]
    if rest:
        parts.append(("not attributed", rest, TRACK))

    of_runs = f"median of {runs} run{'' if runs == 1 else 's'}"
    title = f"{workspace}, {setting_name}, {scenario}: time share per hypothesis, {of_runs}"
    heading = f"Time share: {workspace}, {scenario}"
    sub = f"{setting_name}, {of_runs}"
    if total > 1.005:
        sub += f"; the shares sum to {total:.2f} and are drawn scaled to fit"
    listing = [f"{HYPOTHESIS[k]}: {pct(shares.get(k))}" for k in SHARE_KEYS]
    if rest:
        listing.append(f"not attributed: {pct(rest)}")
    desc = (f"Stacked bar of the share of the build's slot capacity (wall time x jobs) per hypothesis, {of_runs}. "
            + "; ".join(listing) + f". The shares shown sum to {total:.2f}.")

    legend = [(colour, name, pct(v)) for name, v, colour in parts]
    item_w = max(text_width(f"{name} {value}", font) for _, name, value in legend) + 16 + 24
    width = math.ceil(max(720, text_width(heading, 15) + 2 * pad, text_width(sub, font) + 2 * pad, item_w + 2 * pad))
    inner = width - 2 * pad
    cols = 2 if 2 * item_w <= inner else 1
    legend_y = bar_y + bar_h + 20
    height = legend_y + (math.ceil(len(legend) / cols) - 1) * 20 + 10 + pad

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-labelledby="{uid}-title {uid}-desc" font-family="{FONT}">',
        f'<title id="{uid}-title">{escape(title)}</title>',
        f'<desc id="{uid}-desc">{escape(desc)}</desc>',
        f'<defs><clipPath id="{uid}-clip"><rect x="{pad}" y="{bar_y}" width="{inner}" height="{bar_h}" rx="4"/></clipPath></defs>',
        f'<rect width="{width}" height="{height}" fill="{SURFACE}"/>',
        f'<text x="{pad}" y="30" font-size="15" font-weight="600" fill="{INK}">{escape(heading)}</text>',
        f'<text x="{pad}" y="50" font-size="{font}" fill="{INK_MUTED}">{escape(sub)}</text>',
        f'<g clip-path="url(#{uid}-clip)">',
    ]
    labels = []
    x = float(pad)
    for i, (name, v, colour) in enumerate(parts):
        w = v * scale * inner
        seg_w = w if i == len(parts) - 1 else max(w - gap, 0.5)  # a 2 px surface gap between segments
        value = pct(v)
        out.append(f'<rect x="{x:.2f}" y="{bar_y}" width="{seg_w:.2f}" height="{bar_h}" fill="{colour}">'
                   f'<title>{escape(f"{name}: {value}")}</title></rect>')
        if seg_w >= text_width(value, font) + 12:
            labels.append(f'<text x="{x + seg_w / 2:.2f}" y="{bar_y + bar_h / 2 + font * 0.35:.2f}" font-size="{font}" '
                          f'font-weight="600" text-anchor="middle" fill="{label_colour(colour)}">{escape(value)}</text>')
        x += w
    out.append("</g>")
    out += labels
    for i, (colour, name, value) in enumerate(legend):
        lx = pad + (i % cols) * inner / cols
        ly = legend_y + (i // cols) * 20
        out.append(f'<rect x="{lx:.2f}" y="{ly}" width="10" height="10" rx="2" fill="{colour}"/>')
        out.append(f'<text x="{lx + 16:.2f}" y="{ly + 10}" font-size="{font}" fill="{INK}">{escape(name)} '
                   f'<tspan fill="{INK_MUTED}">{escape(value)}</tspan></text>')
    out.append("</svg>")
    return "\n".join(out)


def write_charts(rows: list[dict], svg_dir: Path) -> dict[tuple[str, str, str], Path]:
    """One stacked-bar SVG per workspace, setting and cold scenario that has shares, written to
    svg_dir; returns {(workspace, setting, scenario): path}."""
    svg_dir.mkdir(parents=True, exist_ok=True)
    g = group(rows)
    written: dict[tuple[str, str, str], Path] = {}
    used: set[str] = set()
    for ws in sorted(g):
        for config in sorted(g[ws]):
            for scenario in COLD:
                runs = share_runs(g[ws][config], scenario)
                if not runs:
                    continue
                stem = base = slug(ws, config, scenario)
                n = 1
                while stem in used:  # two settings that differ only in punctuation
                    n += 1
                    stem = f"{base}-{n}"
                used.add(stem)
                path = svg_dir / f"{stem}.svg"
                path.write_text(bar_svg(ws, config, scenario, share_rows(runs), len(runs)) + "\n", encoding="utf-8")
                written[(ws, config, scenario)] = path
    return written


def chart_link(svg: Path, out: Path) -> str:
    """The SVG's path relative to the report's directory, as the URL of a markdown image."""
    return urllib.parse.quote(Path(os.path.relpath(svg.resolve(), out.resolve().parent)).as_posix())


def report(rows: list[dict], preamble: str = "", charts: dict[tuple[str, str, str], str] | None = None) -> str:
    """The §5 report as markdown. `charts` maps (workspace, setting, scenario) to an image link
    that takes the place of that scenario's text bar."""
    g = group(rows)
    out = ["# Phase 0 baseline: where debug-build time goes", ""]
    if preamble.strip():
        out += [preamble.strip(), ""]
    machines = {(r["machine"].get("label"), r["machine"].get("cpu"), r["machine"].get("cores"), r["machine"].get("jobs"), r["machine"].get("ram_gib"), r["machine"].get("os"))
                for r in rows if "machine" in r}
    out.append("Machines: " + "; ".join(f"{h}: {c}, {n} cores, -j {j}, {ram} GiB, {o}" for h, c, n, j, ram, o in sorted(machines, key=str)))
    out.append("")
    out.append("Shares are fractions of the build's slot capacity (wall time x jobs), so they add up to about 1.0 together "
               "with idle; `front_end` and `codegen` are the workspace's own crates, split by cargo's unit sections; "
               "`link` and `debuginfo` need the nightly self-profile and are n/a on stable. The deviation of the sum "
               "from 1.0 is the time neither source attributes. Timed runs never carry `-Zself-profile`; on nightly, "
               "the phase data comes from one extra, untimed, profiled build per cold scenario.")
    out.append("")
    if any((r.get("cache") or "none") != "none" for r in rows):
        out.append("Settings with `+ sccache` or `+ kache` built through that cache, with incremental compilation off "
                   "(Phase 2, design §5); the backend is in brackets. Hit rate is hits / (hits + misses) from the "
                   "cache's own counters for the timed build. Under sccache, rustc runs in the sccache server, outside "
                   "cargo's process tree, so CPU time and peak RSS leave those compiles out; compare wall time.")
        out.append("")
    for ws in sorted(g):
        out.append(f"## {ws}")
        # Uncached settings first, then the ones through a cache.
        for config in sorted(g[ws], key=lambda c: (" + " in c, c)):
            by_scenario = g[ws][config]
            commits = {r.get("commit", "")[:8] for r in sum(by_scenario.values(), [])}
            tcs = {r["toolchain"]["rustc"] for r in sum(by_scenario.values(), []) if "toolchain" in r}
            tools = {r["cache_version"] for r in sum(by_scenario.values(), []) if r.get("cache_version")}
            out.append(f"### {config}")
            out.append(f"Commit {', '.join(sorted(commits))}; {'; '.join(sorted(tcs) + sorted(tools))}")
            out.append("")
            out.extend(scenario_table(by_scenario))
            out.append("")
            cp_lines = critical_path.report_lines(by_scenario)
            if cp_lines:
                out.extend(cp_lines + [""])
            for scenario in COLD:
                runs = share_runs(by_scenario, scenario)
                if not runs:
                    continue
                shares = share_rows(runs)
                sums = [r.get("share_sum") for r in runs if r.get("share_sum") is not None]
                out.append(f"Time share, {scenario} (median of {len(runs)} runs; shares sum to {fmt(median(sums), 2)}):")
                out.append("")
                out.append("| Hypothesis | Share |")
                out.append("|---|---|")
                for k in SHARE_KEYS:
                    out.append(f"| {HYPOTHESIS[k]} | {pct(shares[k])} |")
                out.append("")
                link = (charts or {}).get((ws, config, scenario))
                if link:
                    alt = f"Stacked bar of the time share, {ws}, {config}, {scenario}".replace("[", "(").replace("]", ")")
                    out += [f"![{alt}]({link})", ""]
                else:
                    out += ["```", bar(shares), "```"]
                top = sorted(((v, k) for k, v in shares.items() if v is not None), reverse=True)[:3]
                out.append("Top hypotheses: " + ", ".join(f"{HYPOTHESIS[k]} ({pct(v)})" for v, k in top) + ".")
                all_crates = [r.get("sections_all_crates") for r in runs if r.get("sections_all_crates")]
                if all_crates:
                    fe = median([a["front_end"] for a in all_crates])
                    cg = median([a["codegen"] for a in all_crates])
                    out.append(f"Across every compiled crate, dependencies included: front end {pct(fe)}, codegen {pct(cg)} of compile time.")
                out.append("")
        out.append("")
    failed = [r for r in rows if "error" in r]
    if failed:
        out.append("## Failed runs")
        for r in failed:
            through = "" if (r.get("cache") or "none") == "none" else f" + {r['cache']}"
            out.append(f"- {r['workspace']} {r['config']}{through} {r['scenario']} run {r['run']}: {r['error']}")
        out.append("")
    test_failed = [r for r in rows if _test_run_exit(r) not in (None, 0)]
    if test_failed:
        out.append("## Test runs that failed")
        out.append("")
        out.append("A cold-test row times the build of the test binaries; the test run is timed separately and "
                   "does not affect those numbers. These test runs exited non-zero, so the row says the binaries "
                   "built, not that the suite passed:")
        out.append("")
        for r in test_failed:
            out.append(f"- {r['workspace']} {r['config']} cold-test run {r['run']}: exit {_test_run_exit(r)}")
        out.append("")
    return "\n".join(out)


def self_test() -> int:
    fixture = HERE / "tests" / "fixture.jsonl"
    rows = load([fixture])
    g = group(rows)
    runs = g["example/ws"]["baseline-stable on fixture (-j 8)"]["cold-build"]
    walls = [r["wall_s"] for r in runs]
    assert median(walls) == 10.0, median(walls)
    assert abs(spread(walls) - 0.2) < 1e-9, spread(walls)
    shares = share_rows(runs)
    assert shares["deps"] == 0.5 and shares["idle"] == 0.1, shares
    assert shares["link"] is None
    text = report(rows, "Status: fixture.")
    assert text.splitlines()[2] == "Status: fixture.", text.splitlines()[:4]
    assert "| cold-build | 3 | 10.00 s | 20% (above 5%: run 5) |" in text, text
    assert "1. cold dependency compilation | 50%" in text
    assert "Failed runs" in text and "run 1: cargo exited 101" in text
    # A failed run is counted in its own scenario row, under the same workspace and setting.
    assert "| check | 0 (1 failed) |" in text, text
    # Built test binaries do not say the tests passed: a non-zero test run is marked in its row
    # and listed under the tables.
    assert "| 14 (1 test runs failed) |" in text, text
    assert "Test runs that failed" in text and "cold-test run 1: exit 101" in text, text
    # A second machine is a second baseline: its 30 s run does not move the first one's median.
    assert "baseline-stable on fixture-2 (-j 8)" in g["example/ws"], list(g["example/ws"])
    assert median([r["wall_s"] for r in g["example/ws"]["baseline-stable on fixture-2 (-j 8)"]["cold-build"]]) == 30.0
    # Idle is hypothesis 7 and can rank: on the second machine it is the largest share.
    assert "Top hypotheses: 7. scheduling gaps (idle slots) (70%)" in text, text
    # Phase 2: a build through a cache is a setting of its own, listed after the uncached ones,
    # and only its table has the hit-rate column (the median of 90% and 100%).
    cached = "baseline-stable + sccache (local) on fixture (-j 8)"
    assert cached in g["example/ws"], list(g["example/ws"])
    assert text.index("### baseline-stable on fixture (-j 8)") < text.index(f"### {cached}"), text
    assert text.count("| Test binaries | Hit rate |") == 1, text
    hit_rows = [line for line in text.splitlines() if line.startswith("| cache-hit | 2 |")]
    assert len(hit_rows) == 1 and hit_rows[0].endswith("| 95% |"), hit_rows
    # Without --svg-dir the report keeps its text bar, so the default output does not change.
    assert BAR in text and "![" not in text, text
    # One SVG per workspace, setting and cold scenario with shares; noop has shares but no chart.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        written = write_charts(rows, root / "charts")
        first = ("example/ws", "baseline-stable on fixture (-j 8)", "cold-build")
        second = ("example/ws", "baseline-stable on fixture-2 (-j 8)", "cold-build")
        cold_test = ("example/ws", "baseline-stable on fixture (-j 8)", "cold-test")
        assert sorted(written) == [first, cold_test, second], written
        assert written[first] == root / "charts" / "example-ws-baseline-stable-on-fixture-j-8-cold-build.svg", written
        assert all(p.is_file() for p in written.values()), written
        svg = written[first].read_text(encoding="utf-8")
        ET.fromstring(svg)  # well-formed XML
        assert "<title" in svg and "<desc" in svg and "median of 3 runs" in svg, svg
        # The <desc> lists every share, the unmeasured ones too.
        for share in ("1. cold dependency compilation: 50%", "2. proc macros and build.rs: 5%", "3. front end: 20%",
                      "4. monomorphization and codegen: 15%", "5. debug info: n/a", "6. linking: n/a",
                      "7. scheduling gaps (idle slots): 10%"):
            assert share in svg, (share, svg)
        assert svg.count(">50%<") == 2, svg  # the deps segment's label and its legend entry
        assert "not attributed" not in svg, svg  # the fixture's shares sum to 1.0
        svg2 = written[second].read_text(encoding="utf-8")
        ET.fromstring(svg2)
        assert "7. scheduling gaps (idle slots): 70%" in svg2 and ">70%<" in svg2, svg2
        # Text is escaped: a hostile name still parses, and reads back as written.
        odd = bar_svg('a<b>&"c"', "x & y", "cold-build", shares, len(runs))
        assert ET.fromstring(odd).findtext("{http://www.w3.org/2000/svg}title").startswith('a<b>&"c", x & y'), odd
        # With --out, the report links each chart relative to the report file, in place of the text bar.
        out = root / "report" / "phase-0.md"
        linked = report(rows, "", {key: chart_link(p, out) for key, p in written.items()})
        assert "](../charts/example-ws-baseline-stable-on-fixture-j-8-cold-build.svg)" in linked, linked
        assert BAR not in linked, linked
    print("analyze.py self-test: ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("results", nargs="*", type=Path)
    ap.add_argument("--out", type=Path, help="write the markdown here instead of stdout")
    ap.add_argument("--preamble", type=Path, help="markdown placed under the title (status, caveats), kept across regenerations")
    ap.add_argument("--svg-dir", type=Path, metavar="DIR",
                    help="write each cold scenario's time-share bar here as an SVG; with --out, the report embeds them")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    paths = args.results or sorted((HERE / "results").glob("*.jsonl"))
    if not paths:
        sys.exit("no results files")
    rows = load(paths)
    charts = None
    if args.svg_dir:
        written = write_charts(rows, args.svg_dir)
        print(f"wrote {len(written)} charts to {args.svg_dir}", file=sys.stdout if args.out else sys.stderr)
        if args.out:
            charts = {key: chart_link(svg, args.out) for key, svg in written.items()}
    text = report(rows, args.preamble.read_text() if args.preamble else "", charts)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
