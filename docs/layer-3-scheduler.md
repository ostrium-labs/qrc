# Layer 3: qrcd Orchestrator & Build Engine (Buck2 Fork + TypeScript + Tailscale)

**Status:** plan, updated 2026-10-23.  
**Owner decision:** `qrc` will fork and adapt **Meta's Buck2** in **Rust**, expanding from a Rust-specific compiler accelerator into an ultra-fast monorepo orchestrator (combining **Nx**-style developer ergonomics with Buck2's hermetic execution engine). It introduces native **TypeScript** support powered by the latest **Bun**, fast Rust-based TS toolchains (`tsrc` / `tsrs`), and seamless remote cache synchronization over **Tailscale** private object stores.

---

## 1. Goal & Strategic Vision

`qrc` began by targeting Rust debug compilation bottlenecks (where Phase 0 showed **45% to 72% of CPU time lost to scheduling gaps** on Nushell and Ripgrep).

However, modern systems codebases (including Ostrium Labs' ecosystem with `loams`, `loams-mastra`, `client-rust`, and API services) are polyglot monorepos combining **Rust** backends and **TypeScript** frontend/AI agent services.

We are evolving `qrc` into a modern, unified build orchestrator:
1. **The Core Engine:** A specialized fork of **Buck2** (written in Rust) providing incremental graph computation (Dice) and fine-grained action pipelining.
2. **First-Class TypeScript Support:** High-performance TS execution, bundling, and testing powered by **Bun** and Rust-native tooling (`tsrc`, `tsrs`, SWC/OXC).
3. **Monorepo Ergonomics (Nx-style):** Automatic workspace dependency graphs, affected task computation, task caching, and simple CLI commands (`qrc run`, `qrc affected`, `qrc test`).
4. **Tailscale & Bucket Cache Integration:** Built-in remote cache streaming to S3-compatible bucket stores (RustFS) over private Tailscale WireGuard networks without public ingress.

---

## 2. Core Engine: Forking Meta's Buck2 in Rust

Rather than building a new scheduler and action executor from scratch, `qrcd` will fork and tailor **Meta's Buck2** ([`facebook/buck2`](https://github.com/facebook/buck2)).

### 2.1 Language Decision: Rust vs. Zig vs. Goose
We evaluated three candidate systems languages for the orchestrator engine: **Rust**, **Zig**, and **Goose** ([`aardappel/goose`](https://github.com/aardappel/goose)).

- **Goose (`aardappel/goose`) — Rejected:** Goose features no dynamic heap allocation, which is fundamentally incompatible with dynamic dependency graphs consisting of thousands of arbitrary nodes (e.g., `nushell`, `loams`, `zed`). It also lacks production POSIX IPC, jobserver protocols, and file-watching libraries, with no viable path to upstream contributions in `rust-lang`.
- **Zig — Rejected:** Severe FFI impedance when interfacing with Cargo and rustc internals, inability to leverage Rust crates (`jobserver`, `notify`), and zero possibility of upstream adoption into Cargo.
- **Rust — Selected:** Zero FFI overhead, sub-microsecond scheduling in release mode, direct crate reuse (`jobserver`, `tokio`, `notify`), and full compatibility with Buck2's codebase and `rust-lang`.

### 2.2 Why Buck2 as the Foundation?
- **Written 100% in Rust:** Matches our owner decision to use Rust exclusively. Zero FFI overhead, high concurrency, and direct integration with Rust's toolchain.
- **The Dice Computation Engine:** Buck2's incremental computation framework tracks dependencies at fine granularity, re-evaluating only invalidated graph slices.
- **Hermetic & Remote Execution API (REAPI):** Native understanding of action inputs/outputs, content-addressed storage (CAS), and remote execution caches.
- **Extensible Rule System:** Rules are defined in Starlark, making it straightforward to add specialized Rust rules (`qrc_rust_library`, `qrc_rust_binary`) and TypeScript rules (`bun_package`, `ts_library`).

### What We Strip & Tailor in the Fork:
- Remove Meta-internal infrastructure dependencies and bloated toolchains.
- Introduce zero-config auto-discovery for `Cargo.toml` and `package.json` workspaces so users do not have to write verbose `BUCK` files manually.
- Integrate QRC's Phase 0 critical-path priority weighting directly into the Buck2 action execution loop (`app/buck2_execute`).

---

## 3. TypeScript Integration: Bun, `tsrc`, and `tsrs`

TypeScript builds in traditional monorepos suffer from slow `tsc` type-checking, heavy Node.js startup latency, and disjoint package managers. `qrc` unifies TypeScript workflows natively:

### 3.1 Bun Runtime & Package Manager
- **Runtime & Test Runner:** Use the latest **Bun** ([`bun.sh`](https://bun.sh)) for lightning-fast test execution (`bun test`), script execution, and bundling.
- **Workspace Dependency Resolution:** Native lockfile ingestion (`bun.lock`) with instant symlinking of monorepo packages.

### 3.2 Rust-Native TypeScript Tooling (`tsrc` / `tsrs`)
- **Fast Transpilation (`tsrc`):** Integrate Rust-based TypeScript transpilation (via SWC or OXC engines) for sub-millisecond per-file compilation in debug lane workflows.
- **Cross-Language Type Synthesis (`tsrs`):** Integrate `ts-rs` / Rust-TS reflection shims to automatically generate TypeScript interface definitions directly from Rust structs and enums across workspace boundaries, ensuring compile-time contract safety between Rust services and TypeScript clients.
- **Declaration Pipelining (`.d.ts` Pipelining):** Analogous to Rust's `.rmeta` pipelining, downstream TypeScript libraries begin building as soon as upstream `.d.ts` declaration files are emitted, without waiting for JavaScript bundling.

---

## 4. Monorepo Experience: Nx Ergonomics + Tailscale Caching

`qrc` provides the developer experience of **Nx** with the speed of **Buck2** and the zero-trust security of **Tailscale**.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        Developer / CI Environment                      │
│                                                                        │
│  CLI:  qrc build //...  │  qrc affected --target=test  │  qrc dev      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ qrcd Orchestrator (Buck2 Core in Rust)                                 │
│                                                                        │
│  ┌────────────────────────┐              ┌──────────────────────────┐  │
│  │ Rust Workspaces        │              │ TypeScript Workspaces    │  │
│  │ (Cargo Unit Graph)     │              │ (Bun / package.json)     │  │
│  └───────────┬────────────┘              └─────────────┬────────────┘  │
│              │                                         │               │
│              ▼                                         ▼               │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Dice Incremental Graph & Critical-Path Scheduler (PR #10 / #12)  │  │
│  │ - Rust .rmeta pipelining                                         │  │
│  │ - TypeScript .d.ts pipelining                                    │  │
│  └──────────────────────────────────┬───────────────────────────────┘  │
└─────────────────────────────────────┼──────────────────────────────────┘
                                      │ Encrypted WireGuard
                                      ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Remote Cache Store (S3-Compatible Object Storage over Tailscale)       │
│                                                                        │
│  - Storage Backend: RustFS / SeaweedFS (Port 9000)                     │
│  - Network: tag:build-cache on Tailnet                                 │
│  - Cache Hits: Restored asynchronously without burning jobserver slots │
└────────────────────────────────────────────────────────────────────────┘
```

### 4.1 Affected Task Graph & Incremental Runs
- Changes to Git commits or working tree files automatically identify the minimal subgraph of affected Rust crates and TypeScript packages (like `nx affected`).
- Unaffected build targets and tests are restored instantly from cache.

### 4.2 S3 Bucket & Tailscale Private Cache
- Implements the [Tailscale shared build cache](tailscale-shared-cache.md) plan.
- Buck2's remote cache backend is wired directly to an S3-compatible store (RustFS) listening on private Tailscale IP addresses.
- **Zero-Slot Cache Hits:** When an action hash matches in the cache, the artifact is fetched and unpacked out-of-band without acquiring a jobserver concurrency token, keeping all CPU cores free for active compilations.

---

## 5. Architectural Comparison: Prior Art & Lineage

| Capability | Stock Cargo | Nx | Turborepo | **qrc (Buck2 Fork)** |
|---|---|---|---|---|
| **Language** | Rust | TypeScript | Go / Rust | **Rust** |
| **Execution Engine** | JobQueue FIFO | Node.js Task Runner | Basic DAG | **Buck2 Dice Engine (Hermetic)** |
| **Rust Pipelining** | Limited `.rmeta` | None | None | **Aggressive `.rmeta` Critical Path** |
| **TS Engine** | N/A | `tsc` / esbuild | `tsc` / esbuild | **Bun + `tsrc`/`tsrs` + `.d.ts` Pipelining** |
| **Cache Transport** | Local only | Nx Cloud (HTTP) | Vercel Remote Cache | **S3 / RustFS over Tailscale Mesh** |
| **Job Allocation** | Burns slot on hit | N/A | Process slots | **Cache-aware slot bypass** |

---

## 6. Implementation Milestones

1. **Milestone 1 (Buck2 Core Fork):**
   - Fork [`facebook/buck2`](https://github.com/facebook/buck2) into `crates/qrc-core` / `qrcd`.
   - Strip internal Meta shims and enable standalone Rust build.
2. **Milestone 2 (Cargo & Rust Adaptor):**
   - Integrate auto-generation of Buck2 actions from `cargo --unit-graph`.
   - Incorporate critical-path priority weighting derived from `bench/critical_path.py` (PR #10) and `bench/simulate.py` (PR #12).
3. **Milestone 3 (Bun & TypeScript Integration):**
   - Implement `bun_package` and `ts_library` rules.
   - Wire `tsrc` / OXC for sub-millisecond transpilation and `tsrs` for Rust-TypeScript type sharing.
   - Enable `.d.ts` pipelining to unblock dependent TypeScript packages early.
4. **Milestone 4 (Tailscale S3 Cache Provider):**
   - Connect Buck2's CAS / HTTP caching client to S3-compatible endpoints over Tailscale (`tag:build-cache`).
5. **Milestone 5 (Nx-Style Developer CLI):**
   - Deliver `qrc build`, `qrc test`, `qrc affected`, and `qrc run` CLI commands with zero-config workspace detection.
