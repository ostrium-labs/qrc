# Scheduling simulator

[`simulate.py`](simulate.py) replays a recorded cargo build under other scheduling policies and
job counts. It exists to estimate what Layer 3's scheduler (`qrcd`, [design](../docs/design.md)
§6) could gain before anyone builds it, and to put a number on hypothesis 7 of design §2 (cargo
scheduling gaps: idle cores while the build waits on a long crate). The design's rule is to
measure before building; this tool is the step before a measurement. It reuses the durations a
real build recorded and asks what a different scheduler would have done with them.

**Everything it prints is a simulated estimate, a target to validate, not a measurement.** No
number from it should be quoted as a speedup until a real scheduler reproduces it.

Standard library Python 3.11 or newer. It reads files and runs no builds.

## Usage

```sh
# one recorded report
python3 bench/simulate.py bench/results/raw/ripgrep/baseline-stable/cold-build/run-1/cargo-timing.html

# the reports behind a results file (bench/run.py's JSONL), filtered, at chosen job counts
python3 bench/simulate.py bench/results/<date>-<label>.jsonl --scenario cold-build --config baseline-stable --jobs 1,2,4,8,16

# cache hits: 80 percent of the units restored in 0.1 s each, outside the job slots
python3 bench/simulate.py <report> --hit-fraction 0.8 --hit-time 0.1 --cache-aware

# named hits (package name, name@version, or the unit label; shell patterns allowed)
python3 bench/simulate.py <report> --hit-units 'regex*,serde*' --json

python3 bench/simulate.py --self-test
```

A results file is read record by record. Each record's report is found where `bench/run.py`
left it, `<raw>/<corpus name>/<config>/<scenario>/run-<n>/cargo-timing.html`, with `<raw>`
defaulting to `raw/` beside the JSONL (`--raw` changes it). The raw directory is git-ignored, so
this works only on the machine that ran the benchmark, or wherever the raw traces were copied.
`run.py` reuses the `run-<n>` directories, so the simulator compares the report's build start
with the record's timestamp and warns when the report looks like it came from another
invocation. Failed runs and the derived `deps-only` records (which have no build of their own)
are skipped. When a file holds several recordings of the same workspace, scenario and
configuration, the output ends with their medians.

`--json` prints the same results as JSON, with `"kind": "simulated-estimate"` at the top.

## Method

### Input

cargo's `--timings` report embeds its unit data as JSON (`UNIT_DATA`; the [Cargo
book](https://doc.rust-lang.org/cargo/reference/timings.html) describes the report). Each unit
has an id `i`, `name`, `version`, `target`, `start` and `duration` (seconds on cargo's clock),
`sections` (for rustc units, a `frontend` span that ends when the `.rmeta` is written, then
`codegen`), and two release lists:

- `unblocked_units`: the units this unit's completion released;
- `unblocked_rmeta_units`: the units its metadata released. cargo already starts a library's
  dependents on the dependency's `.rmeta` (pipelining), and these are those edges.

### The graph

Each release becomes an edge from the releasing unit to the released one, a **full** edge or a
**.rmeta** edge. A full edge is satisfied when the dependency finishes; an .rmeta edge, under
the policies that pipeline, when the dependency's `frontend` section ends. A unit that released
dependents on its metadata but has no `frontend` section gets its metadata time from the
recording (the earliest of those dependents' starts). Units nobody released were ready when
the build began.

All simulated builds start where the recording's first unit started, so cargo's work before
any unit (resolving, fingerprinting) is kept as a fixed offset, and simulated and recorded wall
times are on the same clock.

### The scheduler

An event-driven list scheduler with `-j` slots. At each moment a unit finishes or writes its
metadata, the releases are applied, then free slots are filled from the ready units in the
policy's priority order. A running unit holds one slot for its whole recorded duration.

| Policy | Priority among ready units | When an .rmeta edge is satisfied | Cache hits |
|---|---|---|---|
| `cargo` | cargo's recorded start order | at the metadata, as cargo did | none |
| `fifo` | the order units became ready (ties: lower unit id) | when the dependency finishes | none |
| `critical-path-first` | longest remaining path to the end of the build | when the dependency finishes | none |
| `pipelined` | longest remaining path to the end of the build | at the metadata | none |
| `cache-hits` | longest remaining path to the end of the build | at the metadata | the hit units |

`cargo` is the sanity check. At the job count the recording ran with, replaying cargo's own
order should land close to the recorded wall time, and the output reports how far off it is.
That difference is the model's error for this recording.

`cache-hits` takes the units chosen by `--hit-fraction` (a seeded random choice; `--seed`) or
`--hit-units`, and gives each the duration `--hit-time` instead of its own; a hit's metadata
and output arrive together at the end of it. Without `--cache-aware`, a hit holds a job slot
for that time, as a `RUSTC_WRAPPER` cache does, since cargo has already handed the slot to the
wrapper. With `--cache-aware`, a hit takes no slot: it is restored as soon as its dependencies
are ready, which is Layer 3's cache-aware scheduling (design §6). The default `--hit-time` of
0.1 s is a placeholder, not a measured restore time; set it from the cache benchmark.

### Output, per policy and job count

| Column | Meaning |
|---|---|
| wall s | simulated wall time, on cargo's clock |
| vs cargo | the change against the `cargo` policy at the same `-j` |
| util | busy slot-seconds over the capacity, wall time x `-j` (the same capacity as the shares in [phase-0.md](../docs/phase-0.md) §4) |
| idle slot-s | capacity minus busy slot-seconds |
| cp s | the critical path: the longest chain of dependent work under the policy's edges and durations, plus the start offset. No schedule can beat it |
| work bound s | busy slot-seconds / `-j`, plus the start offset. No schedule can beat it either |
| above cp | how far the wall is above the critical path |

The header of each recording also gives the recorded wall time and job count, cargo's own
maximum concurrency, how many units waited for a slot after their release, and the sanity
difference.

## How to read it

- **The sanity difference is the noise floor.** A policy that beats `cargo` by less than the
  `cargo` replay misses the recording is within the model's error, not a gain.
- **`pipelined` against `cargo`** is the scheduling gain Layer 3 could add: both release .rmeta
  edges at the metadata, so only the order differs. This is the comparison for "prioritize the
  critical path" in design §6.
- **`critical-path-first` against `pipelined`** is what .rmeta pipelining is worth on this
  build. cargo already pipelines library dependencies, so this is not a gain over cargo; it
  shows how much of cargo's current speed depends on it. `fifo` and `critical-path-first` both
  wait for whole units, so they compare the two priorities with no pipelining.
- **`cache-hits` against `pipelined`** is what the hits are worth under Layer 3's order; running
  with and without `--cache-aware` separates the part that comes from hits not holding slots.
- **The gap to the critical path.** When the work bound is close to the wall (usually at low
  `-j`), the build is short of slots and no order helps much. When the work bound is far below
  the wall and the wall is still well above the critical path, slots sat idle while ready work
  existed or while the chain ran, which is where scheduling can matter. When the wall is close
  to the critical path, reordering has nothing left to win, and the only lever is a shorter
  chain: faster units (Layer 2 caching, Layer 4 backends) or a different crate graph.

## Assumptions and limits

- **Durations are fixed.** Each unit takes its recorded duration at every `-j` and under every
  order. In reality a unit's duration includes contention with whatever ran beside it, rustc's
  waits for extra jobserver tokens for parallel codegen, and its process start-up. With fewer
  units beside it a unit may run faster; with more, slower. A job count above the recording
  machine's core count (marked `*`) describes a larger machine whose units each take as long as
  they did on this one, not this machine at that `-j`.
- **Only the releasing dependency is known.** cargo records, for each unit, only the dependency
  that released it (the last one outstanding in that build), not all of its dependencies. The
  simulator's graph therefore has at most one parent per unit. In cargo's order this is enough,
  since the other dependencies finished earlier in the recording, and the sanity check tests
  it. A policy that reorders units can start one before a dependency the report does not show,
  so `fifo`, `critical-path-first`, `pipelined` and `cache-hits` are optimistic, and the
  critical path is a lower bound on the real one (still a valid lower bound on any wall time).
  The full graph needs `cargo build --unit-graph` (nightly; [phase-0.md](../docs/phase-0.md) §4
  names it as the input for Layer 3); feeding it in is the next step for this tool.
- **Not modelled:** memory (peak RSS, and the memory-aware scheduling of design §6); the
  jobserver shared between cargo's slots and rustc's own threads (one unit, one slot); what a
  build script does when it runs (its run is one unit holding one slot for its recorded time,
  even if it starts a C compiler with its own parallelism); and linker contention (links run
  side by side as their recorded durations say).
- **cargo can run more units than `-j`.** Its report's "Max concurrency" row can be above the
  `jobs=` value; the simulator never runs more than `-j` units at once, so a `cargo` replay at
  the recorded `-j` can come out slower than the recording.
- **Timing resolution.** cargo rounds times to 0.01 s, so small units and short gaps carry
  rounding error, and the simulator tolerates 0.02 s of it when it checks that each unit started
  after its release.
- **Cache hits.** The hit time is one assumed number for every unit, even small units that
  compile faster than a restore. Cache-aware restores run without limit in parallel (no disk or
  network contention), and a hit still waits for its dependencies. A random `--hit-fraction`
  spreads hits evenly; real hits are correlated (dependencies hit, the edited workspace crates
  miss), which `--hit-units` can express.
- **Start offset.** cargo's time before the first unit is kept as it was recorded; a scheduler
  that also cut that time is outside this model. `bench/run.py` times the whole cargo process,
  which is longer again (process start-up and exit); the simulator prints that time for
  reference but compares against the report's own clock.

## Turning estimates into measurements

The estimates become evidence for or against building `qrcd` once:

1. the `cargo` replay lands within a small difference of the recording on every corpus
   workspace (otherwise the model, not the scheduler, explains the numbers);
2. the full unit graph replaces the release edges, removing the optimism above;
3. a prototype scheduler, run on the same workspaces and machine under `bench/run.py`,
   reproduces the gain the simulator predicted.

The self-test (`--self-test`) checks the simulator on small synthetic graphs whose schedules can
be worked out by hand: a chain, a diamond, a wide fan-out, a case where the priority decides the
wall time, an .rmeta-pipelined pair, and cache hits with and without slots.
