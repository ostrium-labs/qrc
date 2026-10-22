# Phase 0 baseline: where debug-build time goes

**Status: partial and provisional.** One workspace of the four (ripgrep), on one machine (an Apple
M3 laptop, 8 cores, 16 GiB, macOS), so these numbers are a first look at the
harness's output, not the Phase 0 exit criterion. loams, nushell and zed need a Linux machine with
more memory and disk (see [the corpus notes](../../bench/corpus.toml)), and the plan treats every
number as provisional until it reproduces on a second machine (§6). Regenerate this file with:

```sh
python3 bench/analyze.py --preamble docs/benchmarks/phase-0-preamble.md \
  --out docs/benchmarks/phase-0.md bench/results/*.jsonl
```

The runs: `python3 bench/run.py ripgrep --config <config> --runs 5 --jobs 8`, stable first, then
nightly, back to back on 2026-10-22.

**Run conditions.** The machine ran on battery, and other work (git checkouts, code searches) ran
on it at the same time, so the absolute times are noisier than a quiet machine would give: the
same stable cold build measured 7.4 s (median of 5) an hour earlier with less going on, and 9.0 s
here. Several scenarios have a spread above 5 percent even with 5 runs. The shares and the
ranking of hypotheses were the same in both sets; the wall times should be re-measured on a quiet
machine before anything is compared against them.

**Stable against nightly (§3).** The tables below put nightly's cold build at 11.7 s against
stable's 9.0 s. That gap is the order of the runs and the machine's load, not the toolchain: the
nightly set ran after the stable one, and its CPU time for the same work was higher run after
run. A check with the two interleaved (4 pairs, `cargo +<toolchain> clean` then
`cargo +<toolchain> --offline build --workspace -j 8`, a 45-second pause before each build) gave a
median wall time of 7.77 s for stable 1.99.1 and 7.06 s for nightly-2026-10-15, with the same CPU
time (21.9 s and 22.0 s user). Read the stable-to-nightly difference on this machine as zero.

ripgrep is the corpus's floor (§1): 59 packages in `Cargo.lock`, a cold build of 7 to 9 seconds here. What
it shows is how much of even a small build is spent outside the workspace's own code; it says
little about where a large workspace's time goes, which is what the other three are for.

**The nightly test runs failed; the builds did not.** All five nightly `cold-test` runs built the
test binaries and then exited 101 from the test run itself (stable's five exited 0). 313 of
ripgrep's 323 integration tests panicked in `tests/tests.rs`, and the cause is the test binary's
location, not the build: the tests run `rg` from `<test executable dir>/../rg`, and
nightly-2026-10-15's cargo 1.101 writes test executables to
`target/debug/build/<crate>/<hash>/out/` instead of `target/debug/deps/`, so that path is a build
directory with no `rg` in it while the binary itself is at `target/debug/rg`. The layout change is
cargo's rather than the harness's: a two-file probe crate shows the same move, and the harness sets
nothing but `CARGO_TARGET_DIR`. Reproduced on 2026-10-22 on the pinned checkout: after
`cargo +nightly-2026-10-15 test --workspace --no-run`, `cargo +nightly-2026-10-15 test --workspace`
gives 10 passed and 313 failed. So the cold-test build timings stand, but nothing here says
ripgrep's suite passes on nightly; a cold-test result is only citable once a toolchain whose layout
matches 1.99.1's, or a workspace whose tests do not locate their binary relative to the test
executable, has run green.

Machines: apple-m3: Apple M3, 8 cores, -j 8, 16.0 GiB, macOS-26.5.2-arm64-arm-64bit-Mach-O

Shares are fractions of the build's slot capacity (wall time x jobs), so they add up to about 1.0 together with idle; `front_end` and `codegen` are the workspace's own crates, split by cargo's unit sections; `link` and `debuginfo` need the nightly self-profile and are n/a on stable. The deviation of the sum from 1.0 is the time neither source attributes. Timed runs never carry `-Zself-profile`; on nightly, the phase data comes from one extra, untimed, profiled build per cold scenario.

## BurntSushi/ripgrep
### baseline-nightly on apple-m3 (-j 8)
Commit e89fff89; rustc 1.101.0-nightly (7df81381d 2026-10-14)

| Scenario | Runs | Wall (median) | Spread | CPU time (user+sys) | CPU use | Peak RSS | Units | Test binaries |
|---|---|---|---|---|---|---|---|---|
| cold-build | 5 | 11.66 s | 64% | 47.9 s | 51% | 1306 MiB | 64 | 0 |
| cold-test | 5 | 15.77 s | 21% | 82.9 s | 64% | 1437 MiB | 93 | 14 (5 test runs failed) |
| check | 5 | 7.97 s | 18% | 26.4 s | 40% | 848 MiB | 65 | 0 |
| noop | 5 | 0.06 s | 180% | 0.1 s | 11% | 41 MiB | 0 | 0 |
| touch-leaf | 5 | 1.05 s | 58% | 1.3 s | 16% | 182 MiB | 2 | 0 |
| touch-core | 5 | 1.34 s | 64% | 2.4 s | 23% | 182 MiB | 7 | 0 |
| deps-only | 5 | 8.66 s | 73% | n/a | n/a | n/a | 52 | 0 |

Time share, cold-build (median of 5 runs; shares sum to 0.99):

| Hypothesis | Share |
|---|---|
| 1. cold dependency compilation | 37% |
| 2. proc macros and build.rs | 10% |
| 3. front end | 6% |
| 4. monomorphization and codegen | 4% |
| 5. debug info | 0% |
| 6. linking | 0% |
| 7. scheduling gaps (idle slots) | 41% |

```
█████████████████████████████████████████████████████████████
  deps 37% · proc_macro_build_rs 10% · front_end 6% · codegen 4% · debuginfo 0% · link 0% · idle 41%
```
Top hypotheses: 7. scheduling gaps (idle slots) (41%), 1. cold dependency compilation (37%), 2. proc macros and build.rs (10%).
Across every compiled crate, dependencies included: front end 66%, codegen 34% of compile time.

Time share, cold-test (median of 5 runs; shares sum to 0.99):

| Hypothesis | Share |
|---|---|
| 1. cold dependency compilation | 30% |
| 2. proc macros and build.rs | 10% |
| 3. front end | 31% |
| 4. monomorphization and codegen | 4% |
| 5. debug info | 0% |
| 6. linking | 2% |
| 7. scheduling gaps (idle slots) | 20% |

```
███████████████████████████████████████████████████████████
  deps 30% · proc_macro_build_rs 10% · front_end 31% · codegen 4% · debuginfo 0% · link 2% · idle 20%
```
Top hypotheses: 3. front end (31%), 1. cold dependency compilation (30%), 7. scheduling gaps (idle slots) (20%).
Across every compiled crate, dependencies included: front end 77%, codegen 23% of compile time.

### baseline-stable on apple-m3 (-j 8)
Commit e89fff89; rustc 1.99.1 (58cf8f9c3 2026-10-13)

| Scenario | Runs | Wall (median) | Spread | CPU time (user+sys) | CPU use | Peak RSS | Units | Test binaries |
|---|---|---|---|---|---|---|---|---|
| cold-build | 5 | 9.01 s | 15% | 34.5 s | 47% | 1228 MiB | 64 | 0 |
| cold-test | 5 | 10.42 s | 49% | 54.9 s | 66% | 1311 MiB | 93 | 14 |
| check | 5 | 4.08 s | 23% | 15.5 s | 48% | 837 MiB | 65 | 0 |
| noop | 5 | 0.05 s | 93% | 0.0 s | 11% | 40 MiB | 0 | 0 |
| touch-leaf | 5 | 0.70 s | 16% | 0.9 s | 16% | 206 MiB | 2 | 0 |
| touch-core | 5 | 0.94 s | 18% | 1.7 s | 23% | 179 MiB | 7 | 0 |
| deps-only | 5 | 6.48 s | 14% | n/a | n/a | n/a | 52 | 0 |

Time share, cold-build (median of 5 runs; shares sum to 0.99):

| Hypothesis | Share |
|---|---|
| 1. cold dependency compilation | 33% |
| 2. proc macros and build.rs | 12% |
| 3. front end | 7% |
| 4. monomorphization and codegen | 5% |
| 5. debug info | n/a |
| 6. linking | n/a |
| 7. scheduling gaps (idle slots) | 42% |

```
███████████████████████████████████████████████████████████
  deps 33% · proc_macro_build_rs 12% · front_end 7% · codegen 5% · idle 42%
```
Top hypotheses: 7. scheduling gaps (idle slots) (42%), 1. cold dependency compilation (33%), 2. proc macros and build.rs (12%).
Across every compiled crate, dependencies included: front end 63%, codegen 37% of compile time.

Time share, cold-test (median of 5 runs; shares sum to 0.99):

| Hypothesis | Share |
|---|---|
| 1. cold dependency compilation | 29% |
| 2. proc macros and build.rs | 13% |
| 3. front end | 35% |
| 4. monomorphization and codegen | 6% |
| 5. debug info | n/a |
| 6. linking | n/a |
| 7. scheduling gaps (idle slots) | 15% |

```
██████████████████████████████████████████████████████████
  deps 29% · proc_macro_build_rs 13% · front_end 35% · codegen 6% · idle 15%
```
Top hypotheses: 3. front end (35%), 1. cold dependency compilation (29%), 7. scheduling gaps (idle slots) (15%).
Across every compiled crate, dependencies included: front end 77%, codegen 23% of compile time.


## Test runs that failed

A cold-test row times the build of the test binaries; the test run is timed separately and does not affect those numbers. These test runs exited non-zero, so the row says the binaries built, not that the suite passed:

- BurntSushi/ripgrep baseline-nightly cold-test run 1: exit 101
- BurntSushi/ripgrep baseline-nightly cold-test run 2: exit 101
- BurntSushi/ripgrep baseline-nightly cold-test run 3: exit 101
- BurntSushi/ripgrep baseline-nightly cold-test run 4: exit 101
- BurntSushi/ripgrep baseline-nightly cold-test run 5: exit 101

