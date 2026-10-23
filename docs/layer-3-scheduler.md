# Layer 3: qrcd and loams-build (orchestrator plan)

**Status: planned, not built.** Nothing in this document exists yet. Every number below is either a
measurement with a link to its source, or is labelled a target. Updated 2026-10-23.

**Engine fork:** [ostrium-labs/loams-build](https://github.com/ostrium-labs/loams-build), an upstream-first fork of Buck2 whose main goal is first-class TypeScript support (Bun, ts-rs, Vite, OXC, Rolldown; planned, not built).

**Owner decision (2026-10-23):** go with **loams-build**, a fork of [Buck2](https://github.com/facebook/buck2)
adapted for Rust and TypeScript monorepos, written in Rust.

## 1. Two tracks, and what is opt-in

| | `qrcd` (design [§6](design.md)) | `loams-build` (this plan) |
|---|---|---|
| What it is | A scheduler that wraps Cargo: reads Cargo's unit graph and schedules units itself | A Buck2-based build engine that replaces Cargo as the build driver |
| Build driver | Cargo stays | Buck2 (a fork) |
| Status | Planned, not built | Planned, not built |
| Who it is for | Everyone using Cargo | An **opt-in research track** |

Design [§1a](design.md) already named the main cost of Bazel or Buck2: it "requires leaving cargo as
the build driver". That cost applies to loams-build in full. So:

- **Plain Cargo stays supported** and remains the default. Layers 1 and 2 (the zero-code wins and an
  adopted cache) and the Cargo-wrapping `qrcd` do not depend on loams-build.
- **loams-build is gated on the results of Layers 1 and 2.** If those close most of the gap, the track
  stops there. We only start it if the measurements still leave a gap worth the cost of leaving Cargo.
- Nothing here changes how Loams builds today. The Loams monorepo uses Cargo for Rust and pnpm with
  Nx for TypeScript, and keeps doing so.

## 2. Evidence behind the plan

Every figure is from a harness run committed to this repository. Idle share is design §2's
hypothesis 7 (scheduling gaps): the fraction of the build's job slots (wall time times `-j`) that sat
idle. All are cold builds with `baseline-stable`, one machine each, so they are first measurements,
not a general result (Phase 0 requires reproduction on a second machine).

| Workspace | Scenario | Idle share | Machine and toolchain | Source |
|---|---|---|---|---|
| nushell | cold-build | 45% | Intel Core Ultra 5 225H, 14 cores, `-j 14`, rustc 1.96.1 | [results](../bench/results/2026-10-22-intel-r-core-tm-ultra-5-225h.jsonl), [how it was run](../bench/results/2026-10-22-intel-r-core-tm-ultra-5-225h.md) (PR #14) |
| nushell | cold-test | 21% | same | same |
| ripgrep | cold-build | 42% | Apple M3, 8 cores, `-j 8`, rustc 1.99.1 | [phase-0.md](benchmarks/phase-0.md), [results](../bench/results/2026-10-22-apple-m3.jsonl) |

An earlier draft of this plan quoted 72%. No committed result gives that figure, so it is removed.
The harness is [`bench/run.py`](../bench/README.md); the critical-path and simulator tools it feeds
are [`bench/critical_path.py`](../bench/README.md) (PR #10) and
[`bench/simulate.py`](../bench/SIMULATE.md) (PR #12). Cache scenarios came in with PR #11.

What these results do and do not say: a large share of slots sat idle on cold builds, which is a
reason to look at scheduling. They do not say how much of that a better scheduler can recover. The
simulator's output is an estimate to validate, not a measurement
([SIMULATE.md](../bench/SIMULATE.md)).

**Targets (not measured):**

- Scheduling overhead per unit of the new scheduler: **target** under 1 ms per unit, to be measured
  once something exists. An earlier draft said "sub-microsecond"; that was a guess and is removed.
- Any wall-time gain over plain Cargo: no target is set until Phase 0 and the simulator give one.

## 3. Language and name

- **Name:** the engine is **loams-build**. `qrcd` is the Cargo-wrapping daemon from design §6. Both
  are Rust.
- **Rust only** for `qrcd` and loams-build. Buck2 is written in Rust, so a fork stays in one language
  and can reuse crates such as `jobserver`, `tokio` and `notify`. Zig and Goose were considered and
  not chosen; this plan does not rest on any claim about them that is not sourced. Design §9 and §16
  are updated to match. The optional single-pass backend (design §7) is a separate question and
  stays open.

## 4. The Buck2 fork

### 4.1 Licence

Buck2 is licensed **MIT OR Apache-2.0**: the repository carries `LICENSE-MIT` and `LICENSE-APACHE`
and a `NOTICE` file, and its README says it is under both licences
([facebook/buck2](https://github.com/facebook/buck2)). Checked 2026-10-23. A fork must keep the
copyright notices, both licence files and the `NOTICE` file, and mark changed files as changed.
qrc itself is MIT OR Apache-2.0 ([CONTRIBUTING.md](../CONTRIBUTING.md)), so the two are compatible.

### 4.2 Fork policy: upstream first, small patch queue

- A change that is not specific to Meta's setup goes to `facebook/buck2` as a pull request first.
- What upstream does not take stays in a small, ordered patch queue on top of upstream.
- The fork is **rebased on upstream regularly**, so the queue stays small and short-lived. This is the
  same pattern Loams uses for its other forks, and it fits the repository's rule to
  [contribute upstream](../CONTRIBUTING.md) rather than fork.
- **The fork repository is not created yet.** It is created, under `ostrium-labs`, at the first
  milestone that needs a patch. Until then we use stock Buck2 and write no patches.

### 4.3 Why Buck2

- Written in Rust, so no foreign-language boundary.
- Incremental computation (DICE) re-evaluates only the parts of the graph that changed.
- Actions have declared inputs and outputs, and Buck2 speaks the Remote Execution API for
  content-addressed caching.
- Rules are written in Starlark, so Rust and TypeScript rules can be added without changing the engine.

These are properties from Buck2's own documentation, not something qrc measured.

### 4.4 Reuse before writing

Before writing any discovery layer that turns `Cargo.toml` into build targets, evaluate what exists:

- **Buck2's Rust prelude** (`prelude/rust` in the Buck2 repository, with rules such as `rust_library`
  and `rust_binary`, a `cargo_package.bzl` and a build-script runner).
- **[reindeer](https://github.com/facebookincubator/reindeer)** (`facebookincubator/reindeer`), Meta's
  tool that generates Buck targets from a Cargo workspace's dependencies. Licence: MIT (GitHub's
  licence detection and its `LICENSE` file, checked 2026-10-23).

Milestone 2 starts with that evaluation: run both on the Loams workspace and on nushell, and write
down what they cannot do (for example build scripts, proc macros, features, test targets) before
deciding whether any new layer is needed. The Cargo unit graph from
`cargo +nightly build --unit-graph -Zunstable-options` is the fallback source of truth, and it is
what `qrcd` consumes either way.

## 5. TypeScript

Tool names, checked 2026-10-23:

| Tool | What it is | Licence | Notes |
|---|---|---|---|
| [Bun](https://bun.sh) ([oven-sh/bun](https://github.com/oven-sh/bun)) | JavaScript runtime, package manager, test runner and bundler | MIT; the repository's `LICENSE.md` says "Bun itself is MIT-licensed" and lists statically linked libraries (JavaScriptCore is LGPL-2) | Used for running and testing |
| [OXC](https://github.com/oxc-project/oxc) | Rust JavaScript and TypeScript parser, transformer, linter | MIT | Candidate TypeScript transpiler |
| [SWC](https://github.com/swc-project/swc) | Rust TypeScript and JavaScript compiler | Apache-2.0 | Candidate TypeScript transpiler |
| [ts-rs](https://github.com/Aleph-Alpha/ts-rs) (crate `ts-rs`) | Generates TypeScript type declarations from Rust types | MIT | Contract types between Rust services and TypeScript clients |

- **`tsrc` is not a tool we can use.** An earlier draft named it as a Rust TypeScript transpiler. No
  such tool exists: the crates.io name `tsrc` is reserved for a TypeScript compiler project with "no
  code yet", and the other tool called `tsrc` on GitHub (your-tools/tsrc) manages groups of git
  repositories. It is replaced by **OXC's transformer or SWC**, to be chosen by a small benchmark.
  Transpiling does not type-check, so type checking stays with `tsc` (or whatever the project
  already uses) until a faster checker is shown to be good enough.
- **`tsrs` meant `ts-rs`.** The npm name `tsrs` is an unrelated project (a Rust port of the
  TypeScript compiler); do not confuse it with `ts-rs`.
- **Bun does not replace pnpm or Nx in Loams.** The Loams monorepo uses pnpm and Nx today and keeps
  them. Bun starts as the test and runtime step for qrc's own TypeScript targets only.

Plan, in order: `bun test` as a Buck2 test rule for qrc's own TypeScript; then a transpile rule
(OXC or SWC); then `ts-rs` generation as a build step; and only then any `.d.ts` pipelining, which
is a research idea (start downstream packages once declarations exist, like Rust's `.rmeta`), with no
evidence yet that it helps.

## 6. Developer experience (Nx-style), planned

The goal is commands in the style of Nx: `qrc build`, `qrc test`, `qrc affected`, `qrc run`. Affected
computation means: from the Git diff, find the Rust crates and TypeScript packages whose inputs
changed and run only those. The Rust side can use the unit graph; the TypeScript side reads
`package.json` workspaces. None of it exists yet.

## 7. Remote cache over Tailscale

This plan does not repeat the cache design. It is in
[tailscale-shared-cache.md](tailscale-shared-cache.md), and the deployment files merged in PR #13 are
in [deploy/shared-cache/](../deploy/shared-cache/README.md). That setup is an sccache store on a
tailnet. Pointing Buck2's remote cache client at the same kind of store is a later milestone and
depends on what Buck2 supports; it is not assumed here. Those deployment files have not yet been run
against a real tailnet (their README says so).

Cache-aware scheduling (do not occupy a job slot for a cache hit) is design §6's idea and stays a
design idea until measured.

## 8. Milestones

All planned, none started. Each starts only if the previous one's result justifies it.

0. **Gate:** Layers 1 and 2 results are in (design §4 and §5), and a gap remains. Without this, stop.
1. **Stock Buck2 trial (no fork):** build a small Rust workspace with unmodified Buck2 and its Rust
   prelude. Get the unit graph with `cargo +nightly build --unit-graph -Zunstable-options`
   (the output is already JSON; `--json` is not a `cargo build` flag) and compare it with the targets
   Buck2 sees. Record what is missing.
2. **Discovery evaluation:** Buck2's Rust prelude and reindeer against the Loams workspace and nushell
   (section 4.4). Decide whether a new discovery layer is needed.
3. **First patch, and the fork:** only if a change is needed that upstream will not take, create the
   fork under `ostrium-labs` and start the patch queue (section 4.2). Critical-path priority weighting
   from `bench/critical_path.py` and `bench/simulate.py` is the likely first change.
4. **TypeScript:** `bun test` rule, then transpile rule, then `ts-rs` generation (section 5).
5. **Remote cache:** Buck2 against the Tailscale store (section 7).
6. **CLI:** `qrc build`, `qrc test`, `qrc affected`, `qrc run` (section 6).

Success for the whole track is a measured wall-time gain on cold and incremental builds against plain
Cargo with the same cache, on at least two machines. Until then it is research.
