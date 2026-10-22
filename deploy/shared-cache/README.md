# Shared build cache: deployment files

These files carry out the plan in [docs/tailscale-shared-cache.md](../../docs/tailscale-shared-cache.md)
(issue [#4](https://github.com/ostrium-labs/qrc/issues/4)): one RustFS store on the tailnet, sccache
on maintainer laptops and in the loams CI.

**Status: nothing here has been run against a real tailnet, a real store or the loams CI yet.** The
scripts have only had static checks (`bash -n`, shellcheck, `py_compile`, a YAML parse). The first
real runs are the steps below, by the people named in each step.

Values in angle brackets are placeholders. Real tailnet names, addresses, logins and keys never go in
this repository, an issue, a pull request or chat.

Versions the files were written and checked against (each release at least 14 days old):
RustFS 1.0.1, its admin client `rc` (rustfs/cli) v0.1.36, sccache v0.18.0,
`tailscale/github-action` v4.2.0 and `mozilla-actions/sccache-action` v0.0.11.

## The files

| File | Used by | Where | Plan step (section 6) |
|---|---|---|---|
| `test/local-e2e.sh` | any maintainer | own laptop, before the rest | checks 6.1 to 6.5 locally |
| `store/rustfs.env.example` | owner | store box, `/etc/rustfs/rustfs.env` | 6.1, step 2 |
| `store/rustfs.service` | owner | store box, `/etc/systemd/system/` | 6.1, step 2 |
| `store/wait-for-address.sh` | owner | store box, `/usr/local/lib/qrc-cache/` | 6.1, step 2 |
| `store/setup.sh`, `store/policy-rust-cache-*.json` | owner | store box | 6.1, steps 2 and 3 |
| `tailnet-policy.hujson` | owner, reviewed by a second maintainer | Tailscale admin console | 6.2, step 5 |
| `client/sccache.env.example` | each maintainer | own laptop | 6.4 |
| `ci/loams-cache-steps.yml` | a maintainer, in a loams pull request | loams' build and test workflow | 6.5 |
| `ci/check_release_workflows.py` | a maintainer, in the same loams pull request | loams' CI | 3.5 |

## Order of work

### 0. Local test (any maintainer)

With `rustfs`, `rc`, `sccache`, `cargo`, `curl` and `openssl` on your PATH:

```sh
deploy/shared-cache/test/local-e2e.sh <path to a Rust project, for example your loams checkout>
```

It starts a throwaway RustFS on `127.0.0.1`, runs `store/setup.sh` against it, and builds the project
several times: without sccache (the baseline), cold and warm with a maintainer key, read-only with
the `ci-ro` key (hits, and no new objects), with the store stopped, and against a blackholed address.
It also checks that the `ci-ro` key cannot write. It prints a summary of what passed and how long each
build took. It does several full builds, so set `E2E_CARGO_ARGS` (for example `build -p <crate>`) to
keep it short. The script header lists the other settings.

### 1. The store (owner, plan 6.1)

1. Create the always-on Linux box and join it to the tailnet with `tag:build-cache`.
2. Install RustFS 1.0.1 and check it against the release's checksums, then install the files:

   ```sh
   gh release download 1.0.1 -R rustfs/rustfs -p 'rustfs-linux-x86_64-gnu-v1.0.1.zip' -p SHA256SUMS
   sha256sum --check --ignore-missing SHA256SUMS
   unzip -d rustfs-1.0.1 rustfs-linux-x86_64-gnu-v1.0.1.zip
   sudo install -m 0755 "$(find rustfs-1.0.1 -type f -name rustfs)" /usr/local/bin/rustfs

   sudo useradd --system --home-dir /srv/rustfs --shell /usr/sbin/nologin rustfs
   sudo install -d -o rustfs -g rustfs -m 0750 /srv/rustfs /srv/rustfs/data
   sudo install -d -g rustfs -m 0750 /etc/rustfs
   sudo sh -c 'umask 027; openssl rand -hex 10 > /etc/rustfs/root-access-key; openssl rand -hex 20 > /etc/rustfs/root-secret-key'
   sudo chgrp rustfs /etc/rustfs/root-access-key /etc/rustfs/root-secret-key
   sudo install -g rustfs -m 0640 store/rustfs.env.example /etc/rustfs/rustfs.env
   sudoedit /etc/rustfs/rustfs.env      # RUSTFS_ADDRESS=<the output of `tailscale ip -4`>:9000
   sudo install -D -m 0755 store/wait-for-address.sh /usr/local/lib/qrc-cache/wait-for-address.sh
   sudo install -m 0644 store/rustfs.service /etc/systemd/system/rustfs.service
   sudo systemctl daemon-reload && sudo systemctl enable --now rustfs
   ```

   (On an arm64 box, use the `aarch64` zip.)
3. Create the bucket, quota, lifecycle rule, policies and keys. Run it from the `store/` directory,
   because it reads the policy files next to it:

   ```sh
   sudo ./setup.sh <maintainer-a> <maintainer-b>
   ```

   It writes one key file per user to `/etc/rustfs/cache-keys/` (mode 0600) and prints no secret.
   Each `maintainer-<name>.env` goes to that maintainer over a private channel the owner chooses.
   The two values in `ci-ro.env` become GitHub secrets in step 3. To rotate a key, remove the user
   with `rc admin user rm`, delete its key file and run `setup.sh` again.
4. Check that the S3 port answers on the tailnet and nowhere else:

   ```sh
   curl -fsS --max-time 3 http://<build-cache-tailnet-name>:9000/health   # from a maintainer laptop: answers
   sudo ss -ltnp | grep ':9000'                                         # on the box: <tailnet-ip>:9000 only
   curl --max-time 3 http://<store-public-address>:9000/health            # from off the tailnet: must fail
   ```

### 2. The tailnet policy (owner, plan 6.2)

Merge the sections of `tailnet-policy.hujson` into the tailnet policy file in the Tailscale admin
console, with the placeholders replaced, and keep its `tests`. Tailscale refuses to save a policy
whose tests fail. If the tailnet still has the default rule that allows everything, the `deny` tests
fail: that rule has to go first. A second maintainer reviews the change.

### 3. OAuth client and GitHub secrets (owner, plan 6.3)

As in the plan. `gh secret set` also reads a value from standard input, so the `ci-ro` values can
go straight from the key file without being shown or pasted:

```sh
sed -n 's/^AWS_ACCESS_KEY_ID=//p'     <path to ci-ro.env> | gh secret set SCCACHE_CI_RO_KEY_ID --repo ostrium-labs/loams
sed -n 's/^AWS_SECRET_ACCESS_KEY=//p' <path to ci-ro.env> | gh secret set SCCACHE_CI_RO_SECRET --repo ostrium-labs/loams
```

### 4. Maintainer laptops (each maintainer, plan 6.4)

Copy `client/sccache.env.example` somewhere private, fill in the placeholders, and source it. It
turns `RUSTC_WRAPPER` on only when the store answers. To check it, run `sccache --stop-server`, build
something, and run `sccache --show-stats`: the cache location should name the `rust-cache` bucket.

### 5. loams CI (a maintainer, in a loams pull request, plan 6.5)

1. Add the steps in `ci/loams-cache-steps.yml` to loams' build and test jobs, never to a release,
   package, sign, promote or publish workflow.
2. Copy `ci/check_release_workflows.py` into loams (for example `scripts/`) and run it in loams' CI
   on every pull request, with `--self-test` first. By default a workflow is a release workflow when
   its file name matches `release*`, `publish*`, `*-release*`, `*-sign*` or `*promote*`. In loams on
   2026-10-22 that is the `release-*.yml` files, `desktop-electron-release.yml`, `desktop-sign.yml`,
   `desktop-promote.yml`, `sdk-release.yml` and `auto-promote.yml`. Add `--release-glob` for any
   other workflow that ships something, such as a deploy workflow. The script also follows local
   `uses: ./...` actions and reusable workflows, and fails if a workflow triggered by
   `pull_request_target` or `workflow_run` reaches the cache.

Then measure, as in section 5 of the plan.

## Limits found while writing these files

Read from the sccache v0.18.0 source and docs, and checked by `test/local-e2e.sh` (not yet run):

- If the store cannot be reached when the sccache server starts, the server does not start and every
  compile through the wrapper fails. `SCCACHE_IGNORE_SERVER_IO_ERROR=1` does not help there: it only
  covers errors after the server is up ([src/server.rs](https://github.com/mozilla/sccache/blob/v0.18.0/src/server.rs),
  `start_server`; [src/commands.rs](https://github.com/mozilla/sccache/blob/v0.18.0/src/commands.rs),
  `connect_or_start_server`). That is why the CI step and the laptop settings check the store before
  they set `RUSTC_WRAPPER`, and why the CI step keeps the server up for the whole job.
- sccache has no setting for S3 connect or request timeouts. Once the server is up, a cache lookup
  gives up after 60 seconds ([src/compiler/compiler.rs](https://github.com/mozilla/sccache/blob/v0.18.0/src/compiler/compiler.rs)).
  With a read-write key, the cache write after a miss is waited for before the compile result is
  returned, with no sccache time limit (`src/server.rs`). A store that disappears mid-build therefore
  slows a build down a lot rather than failing it; `local-e2e.sh` step 7 times this.
- Cache keys contain absolute paths. sccache's Rust support hashes the working directory, the full
  rustc argument list and the `CARGO_*` variables as they are
  ([src/compiler/rust.rs](https://github.com/mozilla/sccache/blob/v0.18.0/src/compiler/rust.rs)), and
  its README says absolute paths must match for a hit. So a laptop only shares entries with machines
  that have the same checkout path, `CARGO_HOME` path and `RUSTFLAGS`. All GitHub runners share
  paths with each other, laptops mostly do not. With read-only CI and only laptops writing, CI may see
  few hits; this is an open question in the plan.
- The lifecycle rule deletes objects by age since they were written. sccache's reads do not refresh
  that age, so an entry still in use is deleted after 30 days and written again on the next miss.
