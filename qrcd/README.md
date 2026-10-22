# qrcd

The orchestrator daemon (design §6 and [docs/layer-3-scheduler.md](../docs/layer-3-scheduler.md)): reads
cargo's unit graph, schedules units with cache-aware and critical-path scheduling, pipelines on `.rmeta`,
and holds warm state between invocations. Also the cache client.

**Language:** Rust only (decision documented in [docs/layer-3-scheduler.md](../docs/layer-3-scheduler.md)).

Planned crate name: `qrcd`. Empty for now.
