# qrcd

The orchestrator daemon (design §6 and [docs/layer-3-scheduler.md](../docs/layer-3-scheduler.md)): reads
cargo's unit graph, schedules units with cache-aware and critical-path scheduling, pipelines on `.rmeta`,
and holds warm state between invocations. Also the cache client.

**Language:** Rust only (decided 2026-10-23, [design §9](../docs/design.md) and [docs/layer-3-scheduler.md](../docs/layer-3-scheduler.md)). `qrcd` keeps Cargo as the build driver. The Buck2-based engine, loams-build, is a separate opt-in research track described in the same plan.

Planned crate name: `qrcd`. Empty for now.
