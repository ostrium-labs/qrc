# Tailscale shared build cache

**Status:** plan, written 2026-10-22. The deployment files are in
[deploy/shared-cache](../deploy/shared-cache/README.md). None of them has been run against a real
tailnet or the loams CI yet, and nothing is measured.
**Owner decision:** maintainers' laptops and the CI of `ostrium-labs/loams` share one build cache over
Tailscale, and only maintainers can use it.

This is the plan for Layer 2 of the [design](design.md) (section 5, "adopt an existing build cache,
with a remote store"). It does not add new cache software. It decides where the store lives, who can
reach it, and how we check that it helps.

All names, addresses and keys below are placeholders. Do not put real tailnet names, IP addresses or
secrets in this repo, in issues or in chat.

## 1. Goal

Two or three maintainer laptops and the loams CI (GitHub-hosted runners on a public repository) share
Rust compile artefacts. A build that is cold on one machine becomes a cache hit if another machine
already built the same thing.

- This shortens CI jobs. It does **not** reduce the number of CI runs.
- Only maintainers can read or write the cache. Everyone else, including fork pull requests, builds
  without it, exactly as today.
- Release, package and publish builds never use it (section 3.5).

## 2. Architecture

```
 maintainer laptop --\                          /-- tag:build-cache
 (tailnet device)     \                        /    S3 store (RustFS), port 9000
                       >--- tailnet (WireGuard) -
 GitHub runner ------ /      encrypted, private
 (ephemeral, tag:ci)
```

### 2.1 The store

One always-on tailnet node runs an S3-compatible object store:

- **RustFS** (Apache-2.0). Loams already uses it, so we know how to run it. Preferred.
- **SeaweedFS** (Apache-2.0) is the fallback if RustFS turns out to be a poor fit.
- **Not MinIO.** Its licence is AGPL, which we avoid.

Use a small always-on box (a cheap VPS or a mini PC), tagged `tag:build-cache`. Do not use a laptop as
the server: a sleeping laptop makes the cache unreachable for everyone. Even so, builds must still
work when the cache is down (section 4).

The store listens on its tailnet address only. It must not listen on a public interface. Tailscale
already encrypts the traffic, so plain HTTP inside the tailnet is acceptable. If we want
defence in depth, put Tailscale Serve (HTTPS with a tailnet certificate) in front of it later.

### 2.2 The client: sccache over S3

Clients use **sccache** with its S3 backend, pointed at the store's tailnet address through
`SCCACHE_ENDPOINT`. sccache is a `RUSTC_WRAPPER`, so it needs no changes to loams.

### 2.3 sccache or kache

kache is qrc's other Layer 2 candidate and also supports an S3 remote. We do not decide this by
opinion. qrc's **Phase 2 benchmark** (cold, warm and partial-hit runs on the Phase 0 corpus, see
[design.md](design.md) section 5) picks the winner. Until then the plan is written for sccache,
because it is mature, widely used in CI, and a plain env-var setup. If kache wins:

- the store, the ACL, the credentials and the CI rules in this document stay the same;
- only the client env vars and the CI step change.

### 2.4 The JavaScript side

loams also has a TypeScript monorepo. Nx has a self-hosted remote cache option: set
`NX_SELF_HOSTED_REMOTE_CACHE_SERVER` to a cache server on the tailnet (with its access token in the
environment, never in the repo). It follows the same access rules as the Rust cache. It is a separate
follow-up, not part of the first milestone.

## 3. Access control

Four layers, each of which is enough to stop an outsider by itself.

### 3.1 Tailscale ACL (the network layer)

Only maintainers' devices and CI can reach the store, and only on the S3 port. Everything else on the
tailnet, and the whole internet, gets nothing.

```jsonc
{
  "tagOwners": {
    "tag:build-cache": ["group:maintainers"],
    "tag:ci":          ["group:maintainers"]
  },
  "groups": {
    "group:maintainers": ["<maintainer-1@example.org>", "<maintainer-2@example.org>"]
  },
  "grants": [
    // Maintainer devices reach the store on the S3 port only.
    { "src": ["group:maintainers"], "dst": ["tag:build-cache"], "ip": ["tcp:9000"] },
    // CI reaches the store on the S3 port only.
    { "src": ["tag:ci"],            "dst": ["tag:build-cache"], "ip": ["tcp:9000"] }
  ]
  // No other rule. Tailscale is deny by default. Do not add "*" rules.
}
```

Notes:

- The port is a placeholder. Use whatever the store listens on.
- A Tailscale ACL cannot say "read-only" at the S3 level. It only controls who can connect. Read-only
  versus read-write is enforced by the S3 credentials (3.2).
- The CI node cannot reach anything except the store, so a hijacked runner cannot probe maintainer
  laptops. Add no rule from `tag:ci` to anything else.
- Use `tests` in the ACL file to assert these rules (for example, that `tag:ci` cannot reach a
  laptop), so a later edit cannot widen them silently.
- Have a second maintainer review every ACL change.
- The version to merge, with its `tests`, is
  [deploy/shared-cache/tailnet-policy.hujson](../deploy/shared-cache/tailnet-policy.hujson).

### 3.2 S3 credentials (the data layer)

The store has separate keys, each limited to one bucket (for example `rust-cache`):

| Key | Used by | Rights |
|---|---|---|
| `maintainer-rw` | maintainer laptops (one key each, so one can be revoked alone) | read and write |
| `ci-ro` | CI on any allowed run | read only |
| `ci-rw` | CI runs on pushes to loams `dev` only | read and write, optional |

The default for CI is read-only. Write access for CI is optional and is a later step. It is only worth
it if CI pushes to `dev` keep the cache warm for everyone, and the benchmark shows that helps. If we
enable it, `ci-rw` lives in a GitHub **environment** whose deployment branches are limited to `dev`,
so no other workflow run can read it.

Keys are created in the store's admin console by an owner, and are rotated if a laptop is lost.

### 3.3 CI joins the tailnet

CI uses Tailscale's official GitHub Action, `tailscale/github-action`, **pinned by commit SHA**, with:

- an **OAuth client** (not a long-lived auth key) that may only create nodes with `tag:ci`;
- **ephemeral** nodes, so each runner disappears from the tailnet when the job ends;
- the client ID and secret stored in **GitHub Actions secrets** (`TS_OAUTH_CLIENT_ID`,
  `TS_OAUTH_SECRET`). They are never written to a file in any repo.

### 3.4 Fork pull requests never get the cache

GitHub does not give repository secrets to `pull_request` workflows that come from forks. So a fork's
run has no OAuth secret, cannot join the tailnet, and builds without the cache. This is the rule, and
we rely on it:

- **Never** use `pull_request_target`, `workflow_run` chained from fork code, or any other trigger
  that runs fork code with secrets, in any workflow that touches the cache.
- The Tailscale and cache steps are skipped when the secret is empty, so a fork run still passes, just
  slower.
- Pull requests from branches inside `ostrium-labs/loams` (that is, from maintainers) and pushes to
  `dev` do get the cache.
- Do not "approve and run" an untrusted fork's workflow in a way that exposes secrets. Keep the
  repository setting that requires approval for outside contributors turned on.

### 3.5 Releases never use the shared cache

Release, package and publish workflows (anything that produces an artefact we ship: binaries, crates,
container images, installers) do **not** join the tailnet and do **not** set the cache env vars. They
build from scratch. A poisoned cache entry therefore cannot reach a shipped artefact. Enforce it two
ways: keep the cache steps out of those workflow files, and add a CI check that fails if a release
workflow mentions the cache variables or the Tailscale action
([check_release_workflows.py](../deploy/shared-cache/ci/check_release_workflows.py)).

### 3.6 Write paths and poisoning

The remaining risk is a compromised maintainer laptop writing a bad entry that CI test builds then
read. How we limit that:

- Only maintainers have write keys, and only a small number of them. CI is read-only by default.
- CI uses its own read-only key, so a CI compromise cannot write.
- Releases are excluded (3.5), so the worst case is a wrong result in a test run, not a bad release.
- Determinism checks from the design's Layer 2 hazards: build twice and compare outputs, exclude
  crates with a non-hermetic `build.rs`, keep incremental compilation off. We run them as a scheduled
  job, and wipe the bucket if a check ever fails.
- The cache has a key prefix per toolchain and per target, so a purge is cheap and targeted.

## 4. Reliability

The cache is a speed-up, never a dependency. If it is down, builds must compile locally and pass.

- `SCCACHE_IGNORE_SERVER_IO_ERROR=1`: once the sccache server is running, a store that errors or
  disappears does not fail the build: a lookup that fails counts as a miss and compiles locally.
  It does not cover a store that cannot be reached when the sccache server starts. In sccache
  v0.18.0 the server then does not start, and every compile through the wrapper fails
  ([src/server.rs](https://github.com/mozilla/sccache/blob/v0.18.0/src/server.rs), `start_server`). So the wrapper is turned on only after a
  check: the CI step probes the store's `/health` and starts the sccache server before it sets
  `RUSTC_WRAPPER`, and keeps that server up for the whole job. The laptop settings probe `/health`
  too. If kache wins the benchmark, use its equivalent settings.
- **Timeouts:** sccache v0.18.0 has no setting for S3 connect or request timeouts. What a dead store
  costs is bounded instead by the probe (`curl --max-time 3`, and sccache waits at most 10 seconds
  for its server to start, [src/commands.rs](https://github.com/mozilla/sccache/blob/v0.18.0/src/commands.rs)) and, if the store disappears
  during a job, by sccache's limit of 60 seconds per cache lookup
  ([src/compiler/compiler.rs](https://github.com/mozilla/sccache/blob/v0.18.0/src/compiler/compiler.rs)). With a read-write key, the cache write
  after each miss has no such limit, so a laptop can be slowed down more than CI. Also put a time
  limit on the "join tailnet" step, and make it non-fatal (`continue-on-error`) so a Tailscale
  outage does not fail the job.
- **Size and eviction:** set a quota on the bucket and a lifecycle rule that deletes entries older
  than a set age (start with 30 days, tune from the measurements). S3 lifecycle expiry counts from
  when an object was written, and sccache's reads do not refresh it, so an entry that is still in
  use is also deleted at that age and written again on its next miss. Watch the disk on the store
  box.
- **Cross-machine hits:** keys only match if the inputs match, and for Rust the inputs include
  absolute paths. sccache v0.18.0 hashes the compile's working directory, the full rustc argument
  list and the `CARGO_*` variables as they are ([src/compiler/rust.rs](https://github.com/mozilla/sccache/blob/v0.18.0/src/compiler/rust.rs)),
  and its [README](https://github.com/mozilla/sccache/blob/v0.18.0/README.md) says absolute paths must match for a hit. Its `SCCACHE_BASEDIRS`
  setting strips paths, but `src/compiler/rust.rs` does not use it in that version. So:
  - pin the toolchain with `rust-toolchain.toml`, and use the same version on laptops and CI;
  - use the same `RUSTFLAGS` on every machine that should share entries, since they are part of the
    key. `--remap-path-prefix` (for example, map the workspace root to `/workspace` and
    `$HOME/.cargo` to `/cargo`) makes paths in the build output the same on every machine, but it
    does not make keys match, because the flag itself names the local path;
  - expect hits only between machines with the same checkout path and the same `CARGO_HOME` path.
    loams' GitHub-hosted runners share these with each other; laptops mostly do not;
  - keep incremental compilation off where the cache is used;
  - note that the OS and CPU architecture must match too, so Linux laptops share with the Linux CI,
    and a macOS laptop mostly shares only with other macOS laptops.
- Record sccache stats (`sccache --show-stats`) at the end of every CI job, so we can see the hit rate
  and spot a silent failure.

## 5. Measurement

This ties into qrc's Phase 0 (the benchmark corpus) and Phase 2 (adopt and benchmark the cache).
Nothing is claimed until we have numbers.

On loams' Phase 0 corpus, measure:

| Scenario | What it shows |
|---|---|
| Cold build, no cache | The baseline |
| Cold build, full cache hit | The best case (a fresh runner restoring from the store) |
| Partial-hit build after a typical change | The common case (one crate changed) |
| Warm local rebuild | Whether the cache slows the normal edit loop |

Also record:

- CI **wall time** for the main loams jobs, before and after, over at least a week or 20 runs each, so
  we compare medians and not one lucky run;
- the hit rate per job, and the time spent on cache download and upload;
- the same runs with the store unreachable, to prove the fallback (section 4).

Success is a clear drop in median CI wall time with a fallback that works, not just a high hit rate.
If the gain is small, stop and say so.

## 6. Steps

Steps marked **(owner)** need the Tailscale admin console or repository secrets. Only the owner does
those. Nobody should send keys or secrets in an issue, a pull request or chat.

The files and commands for every step, and who runs each, are in
[deploy/shared-cache/README.md](../deploy/shared-cache/README.md).

### 6.1 Server

1. **(owner)** Create the always-on box and join it to the tailnet with `tag:build-cache`.
2. Install RustFS (or SeaweedFS) there, bound to the tailnet address only. Create the bucket
   `rust-cache` and the three keys from section 3.2 with the minimal policies.
3. Set the quota and the lifecycle rule (section 4).
4. Check from a laptop: the S3 port answers over the tailnet, and does not answer from outside it.

### 6.2 ACL

5. **(owner)** Apply the ACL from section 3.1 in the Tailscale admin console, with its `tests`
   entries. Have a second maintainer review it.

### 6.3 OAuth client and secrets

6. **(owner)** In the Tailscale admin console, create an OAuth client with only the scope needed to
   create auth keys (`auth_keys`, write), restricted to the tag `tag:ci`.
7. **(owner)** Store the values as GitHub Actions secrets on `ostrium-labs/loams`. The CLI prompts for
   each value, so nothing lands in shell history or a file:

```sh
! gh secret set TS_OAUTH_CLIENT_ID --repo ostrium-labs/loams
! gh secret set TS_OAUTH_SECRET    --repo ostrium-labs/loams
! gh secret set SCCACHE_CI_RO_KEY_ID  --repo ostrium-labs/loams
! gh secret set SCCACHE_CI_RO_SECRET  --repo ostrium-labs/loams
```

   If CI write access is enabled later, add `ci-rw` as environment secrets on a `cache-write`
   environment limited to the `dev` branch:

```sh
! gh secret set SCCACHE_CI_RW_KEY_ID --repo ostrium-labs/loams --env cache-write
! gh secret set SCCACHE_CI_RW_SECRET --repo ostrium-labs/loams --env cache-write
```

### 6.4 Client env vars (maintainer laptops)

The full, commented file is
[deploy/shared-cache/client/sccache.env.example](../deploy/shared-cache/client/sccache.env.example).
Source it from your shell profile, or from a direnv `.envrc` in your loams checkout. The values in
angle brackets are placeholders, and the key file comes from the owner over a private channel the
owner chooses, never an issue.

```sh
export SCCACHE_BUCKET=rust-cache
export SCCACHE_ENDPOINT="http://<build-cache-tailnet-name>:9000"
export SCCACHE_REGION=us-east-1            # the store's RUSTFS_REGION
export SCCACHE_S3_USE_SSL=false            # plain HTTP, but inside the encrypted tailnet
export SCCACHE_S3_KEY_PREFIX="<rustc release>/<host target>"
export SCCACHE_IGNORE_SERVER_IO_ERROR=1
export CARGO_INCREMENTAL=0
export RUSTFLAGS="--remap-path-prefix=<path to your loams checkout>=/workspace --remap-path-prefix=$HOME/.cargo=/cargo"
set -a; . "<path to your maintainer-<name>.env key file>"; set +a   # AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
# The wrapper only when the store answers (section 4):
if curl -fsS --max-time 2 -o /dev/null "$SCCACHE_ENDPOINT/health"; then export RUSTC_WRAPPER=sccache; fi
```

There is no `SCCACHE_S3_USE_PATH_STYLE` setting: path-style addressing, which RustFS serves, is
sccache's default, and virtual-host style is opt-in through `SCCACHE_S3_ENABLE_VIRTUAL_HOST_STYLE`,
which we leave unset. `RUSTFLAGS` names your checkout path explicitly, because `$PWD` in a shell
profile is wherever the shell started. The names were checked against sccache v0.18.0
([docs/S3.md](https://github.com/mozilla/sccache/blob/v0.18.0/docs/S3.md)); check them again when the pinned version changes.

### 6.5 CI workflow snippet

This goes in loams' build and test workflow only, never in a release workflow. The full, commented
file is [deploy/shared-cache/ci/loams-cache-steps.yml](../deploy/shared-cache/ci/loams-cache-steps.yml);
if the two differ, the file is right. Actions are pinned by SHA (the comment shows the release each
SHA was taken from), and only to releases at least 14 days old: `tailscale/github-action` v4.3.0 came
out on 2026-10-22, so v4.2.0 (2026-09-22) is pinned.

```yaml
on:
  push:
    branches: [dev]
  pull_request:          # NOT pull_request_target

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 60
    env:
      HAS_CACHE_SECRETS: ${{ secrets.TS_OAUTH_SECRET != '' && secrets.SCCACHE_CI_RO_SECRET != '' }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      # Skipped on fork PRs, because GitHub gives them no secrets.
      - name: Join the tailnet
        id: tailnet
        if: env.HAS_CACHE_SECRETS == 'true'
        continue-on-error: true
        timeout-minutes: 2
        uses: tailscale/github-action@d1b6cd204f8dceda5b3eaad7f1f767be390056cd # v4.2.0
        with:
          oauth-client-id: ${{ secrets.TS_OAUTH_CLIENT_ID }}
          oauth-secret: ${{ secrets.TS_OAUTH_SECRET }}
          tags: tag:ci
          # The action registers ephemeral nodes for OAuth clients.

      - name: Install sccache
        id: sccache
        if: env.HAS_CACHE_SECRETS == 'true' && steps.tailnet.outcome == 'success'
        continue-on-error: true
        timeout-minutes: 2
        uses: mozilla-actions/sccache-action@fc920bf0ec8de6ee65d409111f7ec508035751ba # v0.0.11
        with:
          version: v0.18.0
          disable_annotations: true   # its post step would fail the job if the store went away

      # On only if the store answers and the sccache server starts (section 4).
      - name: Turn on the shared cache if the store answers
        if: env.HAS_CACHE_SECRETS == 'true' && steps.tailnet.outcome == 'success' && steps.sccache.outcome == 'success'
        continue-on-error: true
        timeout-minutes: 5
        env:
          SCCACHE_BUCKET: rust-cache
          SCCACHE_ENDPOINT: http://<build-cache-tailnet-name>:9000
          SCCACHE_REGION: us-east-1
          SCCACHE_S3_USE_SSL: 'false'
          SCCACHE_S3_RW_MODE: READ_ONLY      # the ci-ro key; sccache does not try to write
          SCCACHE_IGNORE_SERVER_IO_ERROR: '1'
          SCCACHE_IDLE_TIMEOUT: '0'          # keep the server up for the whole job
          AWS_ACCESS_KEY_ID: ${{ secrets.SCCACHE_CI_RO_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.SCCACHE_CI_RO_SECRET }}
        run: |
          curl -fsS --max-time 3 -o /dev/null "$SCCACHE_ENDPOINT/health" || exit 0
          release="$(rustc -vV | sed -n 's/^release: //p')"
          host="$(rustc -vV | sed -n 's/^host: //p')"
          export SCCACHE_S3_KEY_PREFIX="$release/$host"
          sccache --start-server || exit 0
          for name in SCCACHE_BUCKET SCCACHE_ENDPOINT SCCACHE_REGION SCCACHE_S3_USE_SSL \
                      SCCACHE_S3_RW_MODE SCCACHE_IGNORE_SERVER_IO_ERROR SCCACHE_IDLE_TIMEOUT \
                      SCCACHE_S3_KEY_PREFIX AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
            printf '%s=%s\n' "$name" "${!name}"
          done >> "$GITHUB_ENV"
          printf '%s\n' RUSTC_WRAPPER=sccache CARGO_INCREMENTAL=0 SHARED_CACHE=on >> "$GITHUB_ENV"

      - run: cargo nextest run --workspace --locked
      - name: Shared cache stats
        if: always() && env.HAS_CACHE_SECRETS == 'true' && env.SHARED_CACHE == 'on'
        continue-on-error: true
        run: sccache --show-stats
```

If the secret is empty (a fork), the cache steps are skipped and the build runs as it does today.
If the store does not answer, the cache stays off and the build runs as it does today. Keep the
secret-presence check on every cache step. Re-check each SHA against its upstream release before
merging, and let Dependabot or Renovate keep them current.

### 6.6 Rollback

The cache is additive, so rollback is simple and fast:

1. Remove the cache steps from the workflow (one small PR). Builds go back to compiling locally.
   Nothing else depends on the cache.
2. **(owner)** Delete the OAuth client in the Tailscale admin console, and remove the GitHub secrets
   (`! gh secret delete TS_OAUTH_SECRET --repo ostrium-labs/loams`, and the others).
3. Remove the `tag:ci` grant from the ACL.
4. On the laptops, unset the env vars above.
5. If poisoning is suspected: first empty the bucket, then rotate all keys, then rebuild.

## 7. Open questions

- sccache or kache: decided by the Phase 2 benchmark.
- Does CI write on `dev` pushes (the `ci-rw` key), or do only laptops write? Decide after the first
  read-only measurements. Because cache keys contain absolute paths (section 4), CI may get few hits
  from laptop writes, while runners would share entries with each other. That makes `ci-rw` more
  likely to be needed than this plan first assumed.
- Do laptops keep `--remap-path-prefix`? It does not make keys match across machines, and while CI
  sets no `RUSTFLAGS`, it also keeps laptop entries from matching CI's.
- Which box hosts the store, and who is on call for it.
