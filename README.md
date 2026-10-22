# qrc: fastest possible Rust debug builds

qrc (quick rust compile) aims to make Rust **debug** builds, the ones you run in CI, in tests and
in the local edit loop, as fast as possible, while **release builds keep using stock rustc +
LLVM**.

qrc is an [Ostrium Labs](https://ostriumlabs.org) research project. It is dual-licensed MIT OR
Apache-2.0 so that anything useful can go upstream to rust-lang.

## Status

**Design only; Phase 0 setup is under way.** The benchmark corpus is pinned in
[`bench/corpus.toml`](bench/corpus.toml) and the profiling harness is in [`bench/`](bench/README.md);
the first runs are next. There is no qrc implementation code, no release and no measured speedup yet. Every number in the design is either quoted from a cited source or a target
to validate.

- Design: [docs/design.md](docs/design.md) (owner draft v0.2, fact-checked)
- Phase 0 plan: [docs/phase-0.md](docs/phase-0.md)
- Shared build cache over Tailscale (plan): [docs/tailscale-shared-cache.md](docs/tailscale-shared-cache.md)
- Layer 3 scheduler plan: [docs/layer-3-scheduler.md](docs/layer-3-scheduler.md)

## The two-lane principle

| Lane | Purpose | Tooling |
|---|---|---|
| **Fast lane** | Runnable debug binaries and test results as soon as possible: debug builds, CI, tests | `qrc` |
| **Verify lane** | The source of truth for correctness | stock `cargo check`, and release builds with stock rustc + LLVM |

The fast lane may be less strict than rustc (for example, it may skip borrow checking and lints),
because the verify lane runs in parallel and gates the merge. **Release is always stock rustc +
LLVM.** qrc never produces release artifacts.

## Layers

Each layer is independent, ships on its own, and is worth doing even if the later ones never ship.
Nothing gets built before Phase 0 shows where the time actually goes.

| Layer | What | Size |
|---|---|---|
| 1. Zero-new-code wins | Config bundle: cranelift backend for dev, less debug info, fast linker, nextest, fewer test binaries | days |
| 2. Adopt an existing cache | Benchmark kache against sccache, add a prebuilt dependency layer and a remote store (plan: [Tailscale shared cache](docs/tailscale-shared-cache.md)). Adopt, do not rebuild | days |
| 3. `qrcd` orchestrator | Cache-aware scheduling, `.rmeta` pipelining, critical-path scheduling, warm daemon state | weeks to months |
| 4. Fast backend | cg_clif tuning first, then TPDE evaluation; a custom single-pass backend only if needed | months |
| 5. Linking | mold/wild by default, fewer test binaries; an own linker only if profiling demands it | optional |

## Roadmap

| Phase | Scope | Exit criterion |
|---|---|---|
| 0 (week 0 to 1) | Benchmark corpus (3 to 5 real workspaces), profiling harness | Time-share breakdown per workload |
| 1 (week 1 to 2) | Layer 1 config bundle, `qrc init` that writes it | Documented speedup |
| 2 (weeks 2 to 3) | Adopt and benchmark kache/sccache, dependency layer, remote store ([Tailscale plan](docs/tailscale-shared-cache.md)) | Cold CI restores from cache; determinism check passes |
| 3 (weeks 3 to 10) | `qrcd` scheduler, cache-aware scheduling, pipelining | Better CPU utilization than plain cargo |
| 4 (from week 10) | cg_clif tuning, TPDE evaluation, optional custom backend | Beats cranelift by a clear margin on the corpus |
| 5 (optional) | Own linker, or a lighter front end | Only if profiling justifies it |

The targets in the design (for example "3x faster cold CI after Layers 1 and 2") are targets to
validate, not claims.

## Repository layout

| Directory | Contents |
|---|---|
| [`docs/`](docs/) | The design, the Phase 0 plan, and benchmark reports |
| [`bench/`](bench/) | Benchmark corpus definition and harness (scripts, result summaries) |
| [`driver/`](driver/) | Rust: `rustc_driver` shim and codegen plug-in |
| [`qrcd/`](qrcd/) | Rust: orchestrator daemon and cache client (plan: [docs/layer-3-scheduler.md](docs/layer-3-scheduler.md)) |
| [`backend/`](backend/) | Optional: single-pass code emitter |
| [`tests/`](tests/) | Differential and determinism test suites |

## Crate names

Nothing is published yet. The planned names are:

- CLI binary `qrc`, published as the crate **`qrc-cli`** (the crates.io name `qrc` belongs to
  another project);
- daemon **`qrcd`**.

## Community

Questions and ideas are welcome in [GitHub issues](https://github.com/ostrium-labs/qrc/issues)
and on the [Ostrium Labs Discord](https://discord.gg/eK6NanHQq). Please read
[CONTRIBUTING.md](CONTRIBUTING.md) before opening a large pull request.

## Licence

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or
  <https://www.apache.org/licenses/LICENSE-2.0>)
- MIT licence ([LICENSE-MIT](LICENSE-MIT) or <https://opensource.org/licenses/MIT>)

at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in
this work by you, as defined in the Apache-2.0 licence, shall be dual licensed as above, without
any additional terms or conditions.
