# Layer 3: qrcd debug scheduler

**Status:** plan, written 2026-10-23. Nothing here is built or measured yet.  
**Owner decision:** `qrcd` is written in **Rust only**. Zig and Goose are rejected. The scheduler adopts and adapts proven designs from Cargo's `JobQueue`, Samurai/Ninja, Meta's Buck2, and QRC's own simulator.

This is the design plan for Layer 3 of the [design](design.md) (section 6, "Layer 3: orchestrator daemon `qrcd`"). It defines the architecture, language choice, scheduling algorithms, and reference repositories we will adapt to eliminate scheduling gaps in Rust debug builds.

---

## 1. Goal & Evidence

The Phase 0 baseline measurements demonstrate that scheduling inefficiencies represent one of the single largest bottlenecks in cold and incremental builds:

- **Nushell (`baseline-stable`, 14 cores):** 45% of slot-seconds in cold builds and 21% in cold tests are lost to **scheduling gaps (`idle` slots)**.
- **Ripgrep (`baseline-stable`, 14 cores):** 72% of slot-seconds in cold builds are lost to idle slots.

While Cargo compiles individual crates with multi-threading, Cargo's coarse-grained ready queue and lack of global critical-path prioritization leaves CPU slots severely underutilized while waiting on bottleneck crates (such as `nu-protocol` or `loams-meta`).

`qrcd` aims to maximize parallel CPU saturation and minimize wall time for debug builds by:
1. Prioritizing the build's critical path (longest downstream chain first).
2. Pipelining `.rmeta` metadata emissions to unblock dependents early.
3. Bypassing jobserver execution slots for cache hits.
4. Keeping graph and change-detection state warm in a persistent daemon.

---

## 2. Language Choice: Rust vs. Zig vs. Goose

Earlier roadmap drafts considered Zig or Rust for `qrcd`. We evaluated three candidate systems languages: **Rust**, **Zig**, and **Goose** ([`aardappel/goose`](https://github.com/aardappel/goose)).

### 2.1 Goose (`aardappel/goose`) — Rejected

Goose (by Wouter van Oortmerssen) is an experimental systems programming language featuring no garbage collection, no lifetime annotations, and **no dynamic heap allocation**.

- **Incompatible with dynamic build graphs:** `qrcd` consumes Cargo's `--unit-graph`, which consists of thousands of dynamic nodes and edges (e.g., 1,263 units in `nushell`, 1,410 packages in `loams`). A language without dynamic heap allocation cannot naturally construct arbitrary DAGs, resize priority queues, or manage dynamic network/IPC buffers without huge static buffers.
- **No systems ecosystem:** `qrcd` requires JSON parsing, POSIX IPC (Unix domain sockets/named pipes), jobserver token passing via pipes, and OS file notifications (`inotify`/`kqueue`). Goose lacks production libraries for all of these.
- **No upstream path:** `qrc` is dual-licensed MIT OR Apache-2.0 specifically to upstream innovations into `rust-lang/cargo`. The Rust project will never accept a dependency on Goose.

### 2.2 Zig — Rejected

- **FFI impedance with Cargo:** `qrcd` must parse Cargo's unit graph, talk rustc's IPC protocol, and interact with Cargo's jobserver. In Zig, all Rust data structures and protocols must be re-implemented from scratch or interfaced via fragile C FFI.
- **No upstream adoption:** The Rust project will not accept a Zig orchestrator upstream into `cargo`.
- **False optimization:** While Zig offers fast self-compilation, `qrcd` runs as a long-lived background daemon. Its own compilation is a one-time event that has zero impact on user Rust debug build times.

### 2.3 Rust — Selected (Owner Decision)

- **Zero FFI & direct crate reuse:** Can directly use or adapt Cargo's own battle-tested [`jobserver`](https://crates.io/crates/jobserver) crate, `serde_json` for `--unit-graph`, and `notify` for cross-platform filesystem events.
- **Peak runtime performance:** Compiled in release mode, a Rust scheduler incurs sub-microsecond decision overhead per task with zero GC pauses.
- **Direct upstream path:** Any improvements to critical-path scheduling and pipelining can be directly contributed upstream to `rust-lang/cargo`.

**Decision:** `qrcd` is 100% Rust.

---

## 3. Mechanisms for Maximum Compile-Time Reduction

### 3.1 Critical-Path-First Scheduling (Longest Remaining Path First)

Cargo's internal ready queue launches unblocked units in a largely FIFO or topological order. When a core bottleneck crate sits at the end of the ready list, cores finish other leaves quickly and then starve waiting for the bottleneck.

`qrcd` dynamically weights every node $u$ in the build DAG by its longest remaining downstream latency to build completion:

$$Weight(u) = Duration(u) + \max_{v \in Dependents(u)} Weight(v)$$

When jobserver tokens become available, the scheduler always pops the node with the highest remaining weight from a `BinaryHeap` priority queue.

### 3.2 Aggressive `.rmeta` Pipelining

Downstream compilation requires only type signatures and metadata (`.rmeta`) from dependencies, not executable object code or full `.rlib` archives.

`qrcd` splits each unit's execution into two phases:
1. **Front-end (`.rmeta` generation):** Upon `.rmeta` readiness, downstream dependent units are immediately promoted to the ready queue.
2. **Back-end (codegen and archive):** Continues in the background to produce the final `.rlib`.

### 3.3 Cache-Aware Slot Allocation

Standard wrapper caches (like `sccache`) intercept rustc invocations *after* Cargo has already acquired a jobserver token. A cache hit consumes a worker slot while downloading or restoring artifacts.

`qrcd` queries the cache (Layer 2 local and Tailscale shared store) **before** claiming a jobserver token. Cache hits restore artifacts asynchronously in lightweight I/O tasks without burning jobserver CPU slots, leaving all compile tokens dedicated to real compiler jobs.

### 3.4 Memory-Aware Concurrency Throttling

Heavy workspaces (e.g. `loams` and `zed`) spike memory usage when multiple large crates codegen in parallel (e.g. `loams` requires throttling to `-j 4` on 16 GB machines). `qrcd` uses historical peak RSS metrics from Phase 0 to limit concurrent execution of memory-heavy units, preventing swap thrashing and OOM kills.

---

## 4. Repositories to Adopt and Adapt

Rather than designing from scratch, `qrcd` adapts proven scheduling and process-management patterns from four primary references:

### 4.1 QRC Simulator (`bench/simulate.py` & `bench/critical_path.py`)
- **Reference:** Ostrium Labs PR [#10](https://github.com/ostrium-labs/qrc/pull/10) and PR [#12](https://github.com/ostrium-labs/qrc/pull/12).
- **What to adapt:**
  - The DAG inversion and path calculation algorithm.
  - The separation between full edges (`unblocked_units`) and metadata edges (`unblocked_rmeta_units`).
  - Cache-aware slot bypass modeling.

### 4.2 Cargo's `JobQueue` (`rust-lang/cargo`)
- **Reference:** [`src/cargo/core/compiler/job_queue/mod.rs`](https://github.com/rust-lang/cargo/blob/master/src/cargo/core/compiler/job_queue/mod.rs) and `job.rs`.
- **What to adapt:**
  - Token acquisition and return using the `jobserver` crate.
  - IPC protocol reading `--json=artifacts` messages from rustc over stdout pipes.
  - Signal handling, process group management, and clean interruption (`Ctrl-C`).
  - Replace Cargo's ready deque with a critical-path priority queue.

### 4.3 Samurai / Ninja (`michaelforney/samurai` & `ninja-build/ninja`)
- **Reference:** [`michaelforney/samurai`](https://github.com/michaelforney/samurai) (`build.c`) and [`ninja-build/ninja`](https://github.com/ninja-build/ninja) (`src/build.cc`).
- **What to adapt:**
  - The gold standard for minimal-overhead DAG scheduling:
    - Pre-computed topological depth weighting.
    - Zero-allocation ready queue transitions upon task completion.
    - Sub-millisecond cycle detection and dependency resolution.

### 4.4 Meta's Buck2 Execution Engine (`facebook/buck2`)
- **Reference:** [`app/buck2_execute`](https://github.com/facebook/buck2/tree/main/app/buck2_execute) and [`app/buck2_build_api`](https://github.com/facebook/buck2/tree/main/app/buck2_build_api).
- **What to adapt:**
  - Modern Rust async action orchestration.
  - Integration of remote cache lookups into the dependency state machine.
  - Memory-pressure tracking and load-shedding mechanisms.

---

## 5. Architecture & Component Layout

```
                  ┌───────────────────────────────┐
                  │    qrc CLI (Frontend / Shim)  │
                  └──────────────┬────────────────┘
                                 │ Unix Domain Socket
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│ qrcd (Daemon)                                                   │
│                                                                 │
│  ┌───────────────────────┐         ┌─────────────────────────┐  │
│  │  Cargo Unit Graph     │         │   Cache Engine          │  │
│  │  Parser (--unit-graph)│         │   (sccache / Tailscale) │  │
│  └───────────┬───────────┘         └────────────┬────────────┘  │
│              │                                  │ Hit / Miss    │
│              ▼                                  │ Query         │
│  ┌──────────────────────────────────────────────▼────────────┐  │
│  │  Critical Path DAG Planner (Ninja/Samurai style)          │  │
│  │  - Static & historical duration weighting                 │  │
│  │  - Dynamic remaining-path BinaryHeap                      │  │
│  └───────────────────────────┬───────────────────────────────┘  │
│                              │                                  │
│                              ▼                                  │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  Job Execution Engine (Cargo JobQueue style)              │  │
│  │  - GNU Make jobserver client                              │  │
│  │  - Async process runner & signal handler                  │  │
│  │  - .rmeta pipe listener -> triggers dependent unblocking  │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 5.1 Crate Layout inside `qrc`
- `driver/`: `rustc_driver` shim to report `.rmeta` availability and timing.
- `qrcd/`: The daemon binary and library crates:
  - `qrcd-core`: DAG definitions, critical path weighting, priority queue.
  - `qrcd-jobserver`: Token passing and process slot manager.
  - `qrcd-cache`: Local and remote cache lookup client.
  - `qrcd-ipc`: Protocol between `qrc` CLI and `qrcd` daemon.

---

## 6. Implementation Milestones

1. **Milestone 1 (Graph & Planner):** Port `bench/simulate.py`'s critical-path calculation into a Rust crate (`qrcd-core`) consuming `cargo build --unit-graph --json`.
2. **Milestone 2 (Job Engine):** Adapt Cargo's `JobQueue` and `jobserver` token manager into a standalone Rust executor.
3. **Milestone 3 (Pipelining):** Implement the `.rmeta` listener to unblock downstream units before codegen completion.
4. **Milestone 4 (Cache Integration):** Hook into the Layer 2 cache client to bypass job slots on hits.
5. **Milestone 5 (Daemon & Benchmarking):** Package into `qrcd` with warm in-memory state and measure wall-time reductions against the Phase 0 baselines.
