# qrc: Fastest Possible Rust Debug Builds

**Status:** Draft v0.2 (Layer 2 changed to adopt an existing cache). **Name:** `qrc` (quick rust compile).
*(Owner draft of 2026-10-21. Copy edits only; fact-checked claims carry a source in
[Sources](#sources), and corrections are listed in [Fact-check notes](#fact-check-notes).)*

Nothing in this document has been built or measured yet. Every number is either a figure quoted
from a cited source or a target to validate.

## 1. Goal and non-goals

**Goal.** Make debug builds (CI, tests, local dev) as fast as possible while **release builds still use stock rustc + LLVM**.

**Non-goals**
- Replacing rustc for release or for correctness gating.
- Reimplementing the Rust front end (macro expansion, name resolution, trait solver, type inference). That is a multi-year effort and never reaches parity.
- Optimized output. Debug artifacts only need to be correct and debuggable enough.

**Core principle: two lanes.**

| Lane | Purpose | Tooling |
|---|---|---|
| **Fast lane** | Get runnable debug binaries and test results as soon as possible | `qrc` (this design) |
| **Verify lane** | Be the source of truth for correctness | stock `cargo check`, later a release build with rustc/LLVM |

The fast lane may be less strict than rustc (for example, it may skip borrow checking and lints) because the verify lane runs in parallel and gates the merge. `cargo check` skips codegen, so it is cheap.

## 1a. Prior art (build caches already exist)

Do not build a new general-purpose Rust cache. Adopt one and spend effort on the gaps.

| Tool | What it is | Notes |
|---|---|---|
| **sccache** (Mozilla) | `RUSTC_WRAPPER` cache, local or remote storage | Mature. Per its docs: crates that invoke the system linker (`bin`, `dylib`, `cdylib`, `proc-macro`) and incrementally compiled crates are not cached; proc macros that read files may not cache correctly [S1] |
| **kache** | Content-addressed `RUSTC_WRAPPER` cache; blake3 keys over normalized compiler inputs; restores by reflink where supported, otherwise a hardlink or copy; S3-compatible, GCS, shared-filesystem or OCI remotes [S2] | Newer and fast-moving (1.2.2 on 2026-10-15). Caches Rust executables and build-script runs by default on Linux and macOS [S2]. Falling back to sccache for what it declines: (unverified; its docs only mention migrating from sccache). Verify maturity and `build.rs`/proc-macro handling yourself |
| **objfs** | Distributed cache using the Remote Execution API v2, optional remote compile (link locally) [S3] | Small project (MIT, about 48 commits, last commit 2026-03-01 when checked); check activity before depending on it |
| **Bazel `rules_rust` / Buck2** | Hermetic remote cache and execution | Strongest guarantees, but requires leaving cargo as the build driver |
| **Cargo cross-workspace cache** (2026 project goal) | Cross-workspace build-unit cache, planned for nightly in 2026 [S4] | Initially basic crates only (no build scripts or proc macros); could later be pre-populated from a remote cache for CI, which the goal lists as a possible extension, not a deliverable [S4] |

**Gaps these leave (where qrc should focus):**
1. Wrapper-based caches see one rustc call at a time, so they cannot improve scheduling or start work earlier.
2. Hermeticity of `build.rs` and proc macros is the weakest area across tools.
3. Nothing here makes a cache miss cheaper. Cold compile speed still depends on the front end and the backend.
4. Linking and test-binary count are outside what a cache wrapper controls.

## 2. Where debug-build time goes (hypotheses to measure first)

1. Cold dependency compilation (usually the biggest chunk of CI time)
2. Proc macros and `build.rs` (compiled, then run)
3. Front end: parse, expand, resolve, typeck, MIR
4. Monomorphization and LLVM codegen (LLVM does a lot of work even at `-O0`)
5. Debug info generation
6. Linking, especially many test binaries
7. Cargo scheduling gaps (idle cores while waiting on a long crate)

**Rule:** do not build anything before Phase 0 shows which of these dominates for the target workloads. The Phase 0 plan is in [phase-0.md](phase-0.md).

## 3. Architecture overview

```
            +-----------------------------+
 cargo ---> |  qrcd (orchestrator daemon) |  unit graph, scheduling, hashing
            +--------------+--------------+
                           |
        +------------------+------------------+
        |                  |                  |
  +-----v------+    +------v-------+    +-----v------+
  | existing   |    | rustc driver |    |  linker    |
  | cache      |    | (front end)  |    | mold/wild/ |
  | kache or   |    | + qrc backend|    | later own  |
  | sccache    |    +--------------+    +------------+
  +------------+
```

The layers are independent and can ship one at a time. Each layer is worth doing even if the later ones never ship.

## 4. Layer 1: zero-new-code wins (days)

Land these first and record the gains.

- `cargo +nightly` with `codegen-backend = "cranelift"` under `[profile.dev]` (`rustc_codegen_cranelift`, installed with `rustup component add rustc-codegen-cranelift-preview --toolchain nightly`) [S5].
- `debug = "line-tables-only"` (or `0` in CI). Per the Rust project's 2026 goals, cranelift's advantage over LLVM was larger with line tables than with no debug info (12 percent against 5 percent on Zed) [S6], so measure both.
- Fast linker: `mold` (Linux), `wild`, or `lld`.
- `split-debuginfo = "unpacked"` on Linux and macOS.
- `panic = "abort"` where tests allow. Cranelift's unwinding support is limited: it is experimental, not supported on Windows or macOS, and `-Cpanic=abort` is the backend's default [S5].
- `-Zshare-generics`, `-Zthreads=N` (parallel front end), nightly only.
- Disable incremental in CI (it costs more than it saves on cold runs).
- Reduce dependency features, dedupe versions, add a workspace-hack crate if needed.
- Merge integration tests into one binary (`tests/main.rs`) to cut link steps.
- `cargo nextest` for test execution.
- A build cache (`kache` or `sccache`); see Layer 2.

## 5. Layer 2: adopt an existing build cache (days)

Goal: a cold CI runner downloads prebuilt artifacts instead of compiling. Use an existing tool (section 1a); do not build one.

**Steps**
1. Benchmark `kache` against `sccache` on the Phase 0 corpus: cold, warm, and partial-hit runs.
2. Pick one (or run `kache` with `sccache` as a fallback, if that turns out to be supported), pin its version, and wire it into CI with an S3-compatible remote.
3. Add a prebuilt dependency layer: key on `Cargo.lock` + features + target + profile + `rustc -vV` + flags, build dependencies once per lockfile change, and restore them on every job (the `cargo-chef` idea, but remote and shared).
4. Add `--remap-path-prefix` so outputs are relocatable across runners.

**Hazards to test (not to reimplement)**
- Non-hermetic `build.rs` (network, git, system libraries): list the crates that fail determinism checks and exclude them.
- Cache poisoning: write only from trusted CI branches; forks are read-only.
- Determinism: build twice and compare the outputs.
- Incremental compilation: keep it off in CI so units are cacheable.

**Contribute upstream** if you find gaps (for example, build-script handling) instead of forking.

## 6. Layer 3: orchestrator daemon `qrcd` (weeks to months)

The detailed plan is in [layer-3-scheduler.md](layer-3-scheduler.md). The owner decision is Rust only,
adopting the scheduling designs of Cargo's `JobQueue`, Samurai/Ninja, Buck2, and QRC's simulator.

- Consumes cargo's unit graph (`cargo build --unit-graph`, a nightly flag) and schedules units itself.
- **Cache-aware scheduling:** query the cache for hits before spawning rustc, so cached units never occupy a job slot. Wrapper-only caches cannot do this.
- **Pipelining:** start dependents as soon as `.rmeta` exists, not when the full `.rlib` finishes.
- **Smarter scheduling:** prioritize the critical path (longest chain first), be memory-aware, use one shared jobserver.
- **Fast change detection:** content hashes cached by (inode, mtime, size), and a file watcher in dev.
- **Warm state:** a long-lived process holds the hashes, the unit graph, and the cache index between invocations.
- Exposes the same CLI shape (`qrc build`, `qrc test`), wrapping cargo, so adoption is drop-in.

## 7. Layer 4: custom fast backend (months)

### 7.1 Where we plug in

rustc supports external codegen backends (`-Zcodegen-backend=...`). The backend receives monomorphized MIR through `rustc_codegen_ssa`; the front end (macros, typeck, traits) stays stock. We replace only "MIR to machine code".

### 7.2 Staged plan

1. **Baseline:** `rustc_codegen_cranelift`. Free, and it already works. Per the Rust project's 2026 goal page, local benchmarks on a 16-core machine in February 2026 measured start-to-finish incremental builds of Zed at 5 percent faster than LLVM with `-Cdebug-info=none` and 12 percent faster with `-Cdebug-info=line-tables-only`, and the goal targets a further 2x speedup inside that backend [S6]. Do not assume large wins; measure.
2. **Evaluate TPDE** (a backend framework). The Rust project's 2026 Fast Builds roadmap lists "Add an experimental TPDE compilation option" as a 2026 goal, with funding still to be found, and says TPDE "compiles 10-20x faster than LLVM -O0 with similar code quality" [S7]. If it lands upstream, contribute instead of duplicating.
3. **Only if (2) does not land or does not fit:** build `qrc-backend`, a single-pass x86-64/AArch64 emitter:
   - one pass over SSA/MIR-derived IR, no optimization passes
   - fixed stack slots, trivial register allocation
   - direct ELF/Mach-O object emission
   - line-table-only debug info
4. **Later idea:** interpret or lazily compile rarely called functions. The cg_clif goal lists both JIT compilation of functions as they are called and interpreting Cranelift IR for rarely called functions [S6].

### 7.3 Skipping work in the fast lane (to prototype)

- A custom `rustc_driver` using `Callbacks::config` + `override_queries` to skip `mir_borrowck` and lint passes. This needs a nightly pin and a spike to confirm there is no hidden dependency on borrowck results.
- This is only safe because the verify lane runs the real borrow checker.

### 7.4 Known gaps to track

Unwinding and `should_panic` tests, inline asm, some SIMD intrinsics (cg_clif supports `std::simd` fully but `std::arch` only partly [S5]), platform coverage (start with x86-64 Linux and AArch64 macOS/Linux), and `-Z` flag churn on nightly.

## 8. Layer 5: linking

- Default to mold or wild.
- Dev profile: dynamically link heavy dependencies where practical (`prefer-dynamic` experiments).
- Fewer, larger test binaries.
- Long-term option: an incremental linker, only if profiling says link time dominates after Layers 1 to 4. (The 2026 Fast Builds roadmap already lists incremental linking with Wild and Relink-Don't-Rebuild [S7]; track those before building anything.)

## 9. Language and implementation split

| Component | Language | Why |
|---|---|---|
| rustc driver and codegen shim | **Rust** | Must link `rustc_private` (nightly, unstable) |
| `qrcd`, hashing | **Zig** (or Rust) | Zig 0.16 `std.Io` gives clean parallel file I/O and hashing via `io.async`; `Io.Threaded` is enough (the release notes call it feature-complete and well-tested, while `Io.Evented` is work-in-progress and experimental) [S8] |
| Backend core (if built) | **Zig** or **Rust** | Single-pass emitters are straightforward in either; Zig arenas fit compiler memory patterns |

Notes
- Zig 0.16 shipped in April 2026 (14 April) with large API breakage: all I/O now takes an `Io` instance [S8, S9]. Pin the toolchain version.
- A Zig core called from a Rust shim over a C ABI is workable. Keep the interface a compact serialized IR, not the full `rustc_codegen_ssa` trait surface.
- If you want one language and one toolchain, writing everything in Rust is the lower-friction path. Zig wins mainly on the build speed of the tool itself and on allocator control.

## 10. Correctness strategy

- **Differential testing:** run each project's test suite under the fast lane and under stock rustc, and compare the results.
- Run rustc's own run-pass and codegen tests against the backend (as cg_clif does).
- Fuzz: generate programs and compare outputs between backends.
- **Never** ship fast-lane artifacts as release; CI enforces release = rustc + LLVM.
- The verify lane (`cargo check`) runs on every PR in parallel.

## 11. Metrics and targets

These are targets to validate, not claims.

| Metric | Baseline (Phase 0) | Target |
|---|---|---|
| Cold CI debug build + tests | measure | 3x faster after Layers 1 and 2 |
| Warm-cache CI (deps cached) | measure | 10x faster |
| Incremental local rebuild | measure | 2x faster |
| Cache hit rate | n/a | above 90 percent on main-branch CI |
| Fast-lane vs rustc test result mismatches | n/a | zero tolerated on the release gate |

Always record: wall time, CPU time, peak RSS, and a per-phase breakdown (`-Zself-profile`, `cargo build --timings`).

## 12. Roadmap

| Phase | Scope | Exit criterion |
|---|---|---|
| 0 (week 0 to 1) | Benchmark corpus (3 to 5 real workspaces of varied size), profiling harness | Time-share breakdown per workload |
| 1 (week 1 to 2) | Layer 1 config bundle, `qrc init` that writes it | Documented speedup |
| 2 (weeks 2 to 3) | Adopt and benchmark kache/sccache, dependency layer, remote store | Cold CI restores from cache; determinism check passes |
| 3 (weeks 3 to 10) | `qrcd` scheduler, cache-aware scheduling, pipelining | Better CPU utilization than plain cargo |
| 4 (from week 10) | cg_clif tuning, TPDE evaluation, optional custom backend | Beats cranelift by a clear margin on the corpus |
| 5 (optional) | Own linker, or a lighter front end | Only if profiling justifies it |

## 13. Risks

| Risk | Mitigation |
|---|---|
| Nightly `rustc_private` API breakage | Pin nightly, add a CI job that tries newer nightlies, keep the shim small |
| Upstream (cg_clif, TPDE) overtakes a custom backend | Layer 4 starts with upstream and builds custom only if clearly needed |
| Third-party cache is young or changes behavior | Pin versions, keep sccache as a fallback, benchmark before committing |
| Cache correctness (stale or poisoned artifacts) | Determinism tests, trusted-write policy |
| Fast-lane semantics diverge from rustc | Differential tests, verify-lane gate |
| Effort spent on the wrong bottleneck | Phase 0 gates everything |

## 14. Open questions

1. Target workloads: a single large workspace, many small crates, or dependency-heavy services?
2. CI platform and cache location (GitHub Actions, self-hosted, S3-compatible store)?
3. Is nightly acceptable in CI for the fast lane?
4. Which OS and architecture matrix is needed first?
5. Do any crates rely on unwinding, or on proc macros that are expensive to cache?

## 15. Suggested repo layout

```
qrc/
  docs/            this design, benchmarks
  bench/           corpus + harness (scripts, results)
  driver/          Rust: rustc_driver shim, codegen plug-in
  qrcd/            Zig or Rust: orchestrator + cache client
  backend/         optional: single-pass emitter
  tests/           differential + determinism suites
```

## 16. Immediate next steps

1. Pick 3 to 5 real workspaces and run `cargo build --timings` plus `-Zself-profile` ([phase-0.md](phase-0.md)).
2. Apply the Layer 1 bundle and record the deltas.
3. Benchmark kache against sccache and set up the dependency layer on one CI pipeline.
4. Decide Zig or Rust for `qrcd` after the cache benchmark shows the I/O shape.

## Fact-check notes

Checked on 2026-10-21 against the sources below. Only cheap-to-check claims were checked.

| Claim in the owner draft | Result |
|---|---|
| sccache does not cache linker-invoking or incremental crates; proc macros reading files may not cache | Holds [S1] |
| kache: blake3 keys over normalized inputs, S3 or shared-filesystem remotes, executables cached by default on Linux and macOS | Holds [S2]. "Hardlink restores" refined: reflink first, then a hardlink or copy [S2]. "Can fall back to sccache": not found in its docs, marked (unverified) |
| objfs: REAPI v2 cache with optional remote compile, small project | Holds [S3] |
| Cargo shared cache: nightly in 2026, basic crates only at first, remote pre-population later | Holds [S4]; pre-population is a possible extension, not a commitment |
| cg_clif: about 5 to 12 percent faster than LLVM on Zed (Feb 2026), 2x target, interpret rarely called functions | Holds [S6] |
| cg_clif unwinding is limited | Holds [S5] |
| "The Rust roadmap lists TPDE integration for 2027" | **Corrected:** the 2026 Fast Builds roadmap lists an experimental TPDE option as a 2026 goal (funding to be found). The 10 to 20x figure is quoted from the same page [S7] |
| Zig 0.16 shipped in April 2026 with large API breakage; `Io.Threaded` is enough | Holds [S8, S9] |

## Sources

- [S1] sccache, [README](https://github.com/mozilla/sccache) and [docs/Rust.md](https://github.com/mozilla/sccache/blob/main/docs/Rust.md) (Known caveats).
- [S2] kache, [github.com/kunobi-ninja/kache](https://github.com/kunobi-ninja/kache); crate docs [kache 0.16.0](https://docs.rs/crate/kache/0.16.0) and [latest](https://docs.rs/crate/kache/latest).
- [S3] objfs, [codeberg.org/ndn/objfs](https://codeberg.org/ndn/objfs).
- [S4] Rust project goal 2026, [Cargo cross workspace cache](https://goals.rust-lang.org/2026/cargo-cross-workspace-cache.html) (tracking rust-lang/cargo#5931).
- [S5] [rustc_codegen_cranelift README](https://github.com/rust-lang/rustc_codegen_cranelift).
- [S6] Rust project goal 2026, [Improve rustc_codegen_cranelift performance](https://goals.rust-lang.org/2026/improve-cg_clif-performance.html).
- [S7] Rust project goals 2026, [Fast Builds roadmap](https://goals.rust-lang.org/2026/roadmap-fast-builds.html).
- [S8] [Zig 0.16.0 release notes](https://ziglang.org/download/0.16.0/release-notes.html).
- [S9] Simon Willison, [link post on Zig 0.16.0, 15 April 2026](https://simonwillison.net/2026/Apr/15/juicy-main/).
