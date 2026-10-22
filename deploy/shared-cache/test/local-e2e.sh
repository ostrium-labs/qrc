#!/usr/bin/env bash
# End-to-end check of the shared cache on one maintainer laptop, with no tailnet and no
# real store (deploy/shared-cache/README.md). It starts a throwaway RustFS on 127.0.0.1,
# sets it up with store/setup.sh, and builds a Rust project through sccache:
#
#   0. plain build without sccache, as the baseline time
#   1. cold build with a maintainer key: fills the cache
#   2. clean rebuild with the maintainer key: expects cache hits
#   3. clean rebuild with the ci-ro key and SCCACHE_S3_RW_MODE=READ_ONLY: expects hits
#      and no new objects in the bucket
#   4. the ci-ro key cannot write (and, as a control, the maintainer key can)
#   5. RustFS stopped while the sccache server runs: the build must pass
#   6. RustFS stopped, fresh sccache server: reported, not asserted
#   7. a blackholed endpoint, with a fresh server and then with a running read-only
#      server: timed, to show what an unreachable store costs; reported, not asserted
#
# Usage: local-e2e.sh <rust-project-dir>
# Needs on PATH: rustfs, rc, sccache, cargo, rustc, curl, openssl.
# Settings (environment, defaults in brackets):
#   E2E_CARGO_ARGS       cargo arguments of every build [build --workspace]
#   E2E_BUILD_LIMIT      seconds before a build is stopped [3600]
#   E2E_BLACKHOLE        an address that drops packets [10.255.255.1]
#   E2E_BLACKHOLE_LIMIT  seconds before a blackholed build is stopped [900]
#   E2E_KEEP             1 keeps the temporary directory with its logs and test keys [0]
#
# The project's own target directory is not touched: builds use a temporary one. Every
# key is a throwaway key for the throwaway store, and none is printed.
set -euo pipefail

# The header comment, without its leading '#', is the usage text.
usage() {
  awk 'NR > 1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0" >&2
  exit 2
}
[ "$#" -eq 1 ] || usage
case "$1" in -h | --help) usage ;; esac
[ -f "$1/Cargo.toml" ] || { echo "no Cargo.toml in $1" >&2; exit 2; }
project="$(cd "$1" && pwd)"

for tool in rustfs rc sccache cargo rustc curl openssl; do
  command -v "$tool" > /dev/null || { echo "$tool is not on PATH" >&2; exit 2; }
done

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
setup="$here/../store/setup.sh"
read -r -a cargo_args <<< "${E2E_CARGO_ARGS:-build --workspace}"
[ "${#cargo_args[@]}" -gt 0 ] || cargo_args=(build --workspace)
build_limit="${E2E_BUILD_LIMIT:-3600}"
blackhole="${E2E_BLACKHOLE:-10.255.255.1}"
blackhole_limit="${E2E_BLACKHOLE_LIMIT:-900}"
bucket=rust-cache

# Start from a clean slate: no inherited sccache, AWS or wrapper settings.
for name in $(env | sed -n 's/^\(SCCACHE_[A-Za-z0-9_]*\)=.*/\1/p; s/^\(AWS_[A-Za-z0-9_]*\)=.*/\1/p'); do
  unset "$name"
done
unset RUSTC_WRAPPER CARGO_BUILD_RUSTC_WRAPPER RUSTC_WORKSPACE_WRAPPER

free_port() { # a TCP port on 127.0.0.1 that nothing answers on
  local port tries=0
  port=$((20000 + RANDOM % 20000))
  while (exec 3<> "/dev/tcp/127.0.0.1/$port") 2> /dev/null; do
    tries=$((tries + 1))
    [ "$tries" -lt 100 ] || { echo "no free port found" >&2; return 1; }
    port=$((port + 1))
  done
  echo "$port"
}
s3_port="$(free_port)"
sccache_port="$(free_port)"
while [ "$sccache_port" = "$s3_port" ]; do sccache_port="$(free_port)"; done
# A private sccache server for this test, so a server you already run is left alone.
export SCCACHE_SERVER_PORT="$sccache_port"
endpoint="http://127.0.0.1:$s3_port"

tmp="$(mktemp -d "${TMPDIR:-/tmp}/qrc-cache-e2e.XXXXXX")"
mkdir -p "$tmp/data" "$tmp/logs" "$tmp/rc"   # setup.sh creates $tmp/keys, mode 0700
export RC_CONFIG_DIR="$tmp/rc"   # rc aliases for this test only

rustfs_pid=""
stop_rustfs() {
  [ -n "$rustfs_pid" ] || return 0
  kill "$rustfs_pid" 2> /dev/null || true
  wait "$rustfs_pid" 2> /dev/null || true
  rustfs_pid=""
}
cleanup() {
  local status=$?
  sccache --stop-server > /dev/null 2>&1 || true
  stop_rustfs
  if [ "${E2E_KEEP:-0}" = 1 ]; then
    echo "kept $tmp"
  else
    rm -rf "$tmp"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

die() { echo "error: $*" >&2; exit 1; }

failures=0
results=""
record() { # PASS|FAIL|INFO, text
  results="$results$1  $2"$'\n'
  echo "$1: $2"
  if [ "$1" = FAIL ]; then failures=$((failures + 1)); fi
}

last_secs=0
last_status=0
build() { # label, limit in seconds: one cargo build, timed, its log in $tmp/logs
  local label="$1" limit="$2" start=$SECONDS pid
  echo "   building ($label, log in $tmp/logs/$label.log)"
  (cd "$project" && exec cargo "${cargo_args[@]}") > "$tmp/logs/$label.log" 2>&1 &
  pid=$!
  while kill -0 "$pid" 2> /dev/null; do
    if [ $((SECONDS - start)) -ge "$limit" ]; then
      kill "$pid" 2> /dev/null || true
      wait "$pid" 2> /dev/null || true
      last_secs=$((SECONDS - start))
      last_status=124
      return 0
    fi
    sleep 1
  done
  if wait "$pid"; then last_status=0; else last_status=$?; fi
  last_secs=$((SECONDS - start))
}

log_tail() { tail -n 15 "$tmp/logs/$1.log" | sed 's/^/   | /'; }

stats() { # label: save the running server's stats and print the main lines
  sccache --show-stats > "$tmp/logs/$1.stats" 2>&1 || true
  grep -E '^(Compile requests|Cache hits|Cache misses|Cache timeouts|Cache read errors|Cache write errors|Non-cacheable calls) ' \
    "$tmp/logs/$1.stats" | sed 's/^/   /' || true
}

stat_value() { # label, stat name: a number from the saved stats, or empty
  awk -v name="$2" 'index($0, name) == 1 {
      rest = substr($0, length(name) + 1)
      if (rest ~ /^[ \t]+[0-9]+[ \t]*$/) { gsub(/[ \t]/, "", rest); print rest; exit }
    }' "$tmp/logs/$1.stats"
}

object_count() { # objects in the bucket, through the admin alias; empty if rc fails
  # The summary's field name is not part of rc's documented interface, so accept the
  # spellings a JSON encoder is likely to produce and take the last match.
  # An unavailable count is empty, not fatal: with `pipefail`, a failed `rc`, `grep` or `sed`
  # would otherwise fail this assignment under `set -e` and stop the test before it reports.
  { { rc ls --recursive --summarize --json "e2eadmin/$bucket" 2> /dev/null || true; } |
    grep -o -E '"(total_objects|totalObjects)"[[:space:]]*:[[:space:]]*[0-9]+' |
    sed -E 's/^[^:]*:[[:space:]]*//' | tail -n 1; } || true
}

use_key() { # key file from setup.sh: the S3 key the next sccache server uses
  AWS_ACCESS_KEY_ID="$(sed -n 's/^AWS_ACCESS_KEY_ID=//p' "$1")"
  AWS_SECRET_ACCESS_KEY="$(sed -n 's/^AWS_SECRET_ACCESS_KEY=//p' "$1")"
  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
}

restart_sccache() { # a fresh server (and zeroed stats) with the current environment
  sccache --stop-server > /dev/null 2>&1 || true
  sccache --start-server > "$tmp/logs/sccache-start.log" 2>&1 ||
    die "sccache --start-server failed: $(cat "$tmp/logs/sccache-start.log")"
}

clean_target() { rm -rf "$tmp/target"; }

# --- the store ---------------------------------------------------------------------------

echo "== the throwaway store: RustFS on $endpoint, data in $tmp/data"
(umask 077 && openssl rand -hex 10 > "$tmp/root-access-key" && openssl rand -hex 20 > "$tmp/root-secret-key")
cat > "$tmp/rustfs.env" << EOF
RUSTFS_ADDRESS=127.0.0.1:$s3_port
RUSTFS_VOLUMES=$tmp/data
RUSTFS_REGION=us-east-1
RUSTFS_ACCESS_KEY_FILE=$tmp/root-access-key
RUSTFS_SECRET_KEY_FILE=$tmp/root-secret-key
RUSTFS_CONSOLE_ENABLE=false
EOF

start_rustfs() {
  (
    set -a
    # shellcheck source=/dev/null
    . "$tmp/rustfs.env"
    set +a
    exec rustfs server
  ) > "$tmp/logs/rustfs.log" 2>&1 &
  rustfs_pid=$!
  local i
  for i in $(seq 1 60); do
    if curl -fsS --max-time 2 -o /dev/null "$endpoint/health" 2> /dev/null; then
      return 0
    fi
    kill -0 "$rustfs_pid" 2> /dev/null || {
      tail -n 20 "$tmp/logs/rustfs.log" >&2
      die "rustfs exited during start-up (attempt $i)"
    }
    sleep 1
  done
  die "rustfs did not answer on $endpoint/health within 60 s"
}
start_rustfs

echo "== store/setup.sh against it"
STORE_URL="$endpoint" RUSTFS_ENV="$tmp/rustfs.env" KEYS_DIR="$tmp/keys" "$setup" e2e \
  > "$tmp/logs/setup.log" 2>&1 || { cat "$tmp/logs/setup.log" >&2; die "setup.sh failed"; }
sed 's/^/   /' "$tmp/logs/setup.log"
maintainer_key="$tmp/keys/maintainer-e2e.env"
ci_ro_key="$tmp/keys/ci-ro.env"
if [ ! -s "$maintainer_key" ] || [ ! -s "$ci_ro_key" ]; then
  die "setup.sh did not write both key files"
fi

rc --quiet alias set e2eadmin "$endpoint" "$(cat "$tmp/root-access-key")" "$(cat "$tmp/root-secret-key")" > /dev/null

# --- the client settings, as in client/sccache.env.example ------------------------------

release="$(cd "$project" && rustc -vV | sed -n 's/^release: //p')"
host="$(cd "$project" && rustc -vV | sed -n 's/^host: //p')"
export SCCACHE_BUCKET="$bucket"
export SCCACHE_REGION=us-east-1
export SCCACHE_S3_USE_SSL=false
export SCCACHE_S3_KEY_PREFIX="e2e/$release/$host"
export SCCACHE_IGNORE_SERVER_IO_ERROR=1
export SCCACHE_ERROR_LOG="$tmp/logs/sccache-server-errors.log"
export CARGO_INCREMENTAL=0
export CARGO_TARGET_DIR="$tmp/target"
export RUSTFLAGS="--remap-path-prefix=$project=/workspace --remap-path-prefix=${CARGO_HOME:-$HOME/.cargo}=/cargo"
echo "== project $project, rustc $release, host $host, cargo ${cargo_args[*]}"

# --- 0. baseline -------------------------------------------------------------------------

echo "== 0. plain build, no sccache"
export RUSTC_WRAPPER=""   # an empty wrapper turns off any rustc-wrapper from Cargo config
clean_target
build baseline "$build_limit"
[ "$last_status" = 0 ] || { log_tail baseline; die "the project does not build without sccache (exit $last_status)"; }
baseline_secs=$last_secs
record INFO "baseline build without sccache: ${baseline_secs} s"
export RUSTC_WRAPPER=sccache
export SCCACHE_ENDPOINT="$endpoint"

# --- 1. cold build, maintainer key ------------------------------------------------------

echo "== 1. cold build with the maintainer key"
use_key "$maintainer_key"
clean_target
restart_sccache
build cold "$build_limit"
stats cold
objects_cold="$(object_count)"
if [ "$last_status" != 0 ]; then
  log_tail cold
  die "the cold build through sccache failed (exit $last_status); see $tmp/logs/sccache-server-errors.log"
fi
if [ "${objects_cold:-0}" -gt 0 ]; then
  record PASS "cold build with the maintainer key: ${last_secs} s, ${objects_cold} objects in the bucket"
else
  record FAIL "cold build with the maintainer key passed but wrote no objects (count: '${objects_cold}')"
fi

# --- 2. warm build, maintainer key ------------------------------------------------------

echo "== 2. clean rebuild with the maintainer key"
clean_target
restart_sccache
build warm "$build_limit"
stats warm
hits="$(stat_value warm 'Cache hits')"
if [ "$last_status" = 0 ] && [ "${hits:-0}" -gt 0 ]; then
  record PASS "rebuild with the maintainer key: ${last_secs} s, ${hits} cache hits"
else
  [ "$last_status" = 0 ] || log_tail warm
  record FAIL "rebuild with the maintainer key: exit ${last_status}, cache hits '${hits}'"
fi

# --- 3. read-only build, ci-ro key ------------------------------------------------------

echo "== 3. clean rebuild with the ci-ro key, SCCACHE_S3_RW_MODE=READ_ONLY"
use_key "$ci_ro_key"
export SCCACHE_S3_RW_MODE=READ_ONLY
objects_before="$(object_count)"
clean_target
restart_sccache
build ci-ro "$build_limit"
stats ci-ro
objects_after="$(object_count)"
hits="$(stat_value ci-ro 'Cache hits')"
if [ "$last_status" = 0 ] && [ "${hits:-0}" -gt 0 ] && [ -n "$objects_before" ] &&
  [ "$objects_before" = "$objects_after" ]; then
  record PASS "read-only rebuild with ci-ro: ${last_secs} s, ${hits} cache hits, objects unchanged at ${objects_after}"
else
  [ "$last_status" = 0 ] || log_tail ci-ro
  record FAIL "read-only rebuild with ci-ro: exit ${last_status}, cache hits '${hits}', objects '${objects_before}' before and '${objects_after}' after"
fi
unset SCCACHE_S3_RW_MODE

# --- 4. ci-ro cannot write --------------------------------------------------------------

echo "== 4. uploads with the ci-ro key and, as a control, the maintainer key"
echo "write probe" > "$tmp/probe.txt"
probe_key=e2e-write-probe   # outside the sccache key prefix
rc --quiet alias set e2eciro "$endpoint" \
  "$(sed -n 's/^AWS_ACCESS_KEY_ID=//p' "$ci_ro_key")" "$(sed -n 's/^AWS_SECRET_ACCESS_KEY=//p' "$ci_ro_key")" > /dev/null
rc --quiet alias set e2emaint "$endpoint" \
  "$(sed -n 's/^AWS_ACCESS_KEY_ID=//p' "$maintainer_key")" "$(sed -n 's/^AWS_SECRET_ACCESS_KEY=//p' "$maintainer_key")" > /dev/null
if rc --quiet cp "$tmp/probe.txt" "e2eciro/$bucket/$probe_key" > "$tmp/logs/put-ci-ro.log" 2>&1; then
  record FAIL "the ci-ro key could write an object"
else
  record PASS "the ci-ro key cannot write (rc cp failed: $(tail -n 1 "$tmp/logs/put-ci-ro.log"))"
fi
if rc --quiet cp "$tmp/probe.txt" "e2emaint/$bucket/$probe_key" > "$tmp/logs/put-maintainer.log" 2>&1; then
  record PASS "control: the maintainer key can write"
else
  record FAIL "control: the maintainer key could not write either, so the ci-ro result proves nothing"
fi
rc --quiet rm --force "e2eadmin/$bucket/$probe_key" > /dev/null 2>&1 || true

# --- 5. store stopped, server running ---------------------------------------------------

echo "== 5. RustFS stopped while the sccache server runs"
use_key "$maintainer_key"
clean_target
restart_sccache   # starts while the store is up
stop_rustfs
if curl -fsS --max-time 2 -o /dev/null "$endpoint/health" 2> /dev/null; then
  die "the store still answers after it was stopped"
fi
build store-down-running "$build_limit"
stats store-down-running
read_errors="$(stat_value store-down-running 'Cache read errors')"
if [ "$last_status" = 0 ]; then
  record PASS "store stopped, server running: the build passed in ${last_secs} s (baseline ${baseline_secs} s), ${read_errors:-?} cache read errors"
else
  log_tail store-down-running
  record FAIL "store stopped, server running: the build failed (exit ${last_status}) with SCCACHE_IGNORE_SERVER_IO_ERROR=1"
fi

# --- 6. store stopped, fresh server -----------------------------------------------------

# From the sccache 0.18.0 source we expect this build to fail: the server checks the store
# when it starts and does not start if it cannot read it, and the wrapper then fails every
# compile. The CI step and the laptop settings probe the store first for this reason.
echo "== 6. RustFS stopped, fresh sccache server (expected to fail)"
clean_target
sccache --stop-server > /dev/null 2>&1 || true
build store-down-fresh "$build_limit"
if [ "$last_status" = 0 ]; then
  record INFO "store stopped, fresh server: the build passed in ${last_secs} s, so sccache coped; the probe is then only a speed-up"
else
  record INFO "store stopped, fresh server: the build failed after ${last_secs} s (exit ${last_status}), as expected; the wrapper must stay off until the store answers"
fi

# --- 7. blackholed endpoint -------------------------------------------------------------

export SCCACHE_ENDPOINT="http://$blackhole:9000"
echo "== 7a. blackholed endpoint $SCCACHE_ENDPOINT, fresh server (expected to fail)"
clean_target
sccache --stop-server > /dev/null 2>&1 || true
build blackhole-fresh "$blackhole_limit"
if [ "$last_status" = 124 ]; then
  record INFO "blackhole, fresh server: stopped after the ${blackhole_limit} s limit"
else
  record INFO "blackhole, fresh server: the build exited ${last_status} after ${last_secs} s"
fi

# A server that started before the store went away, modelled by skipping the start-up
# check. Read-only, as in CI: each cache lookup then waits for sccache's 60 s lookup
# limit. (With a read-write key each miss would also wait for its cache write, which has
# no sccache limit.)
echo "== 7b. blackholed endpoint, running read-only server"
use_key "$ci_ro_key"
export SCCACHE_S3_RW_MODE=READ_ONLY SCCACHE_SKIP_CACHE_CHECK=1
clean_target
restart_sccache
build blackhole-running "$blackhole_limit"
stats blackhole-running
if [ "$last_status" = 0 ]; then
  record INFO "blackhole, running read-only server: the build passed in ${last_secs} s (baseline ${baseline_secs} s)"
elif [ "$last_status" = 124 ]; then
  record INFO "blackhole, running read-only server: stopped after the ${blackhole_limit} s limit (baseline ${baseline_secs} s)"
else
  log_tail blackhole-running
  record FAIL "blackhole, running read-only server: the build failed (exit ${last_status})"
fi
unset SCCACHE_S3_RW_MODE SCCACHE_SKIP_CACHE_CHECK
sccache --stop-server > /dev/null 2>&1 || true

# --- summary -----------------------------------------------------------------------------

echo
echo "== summary (sccache $(sccache --version 2> /dev/null | awk '{print $2}'), rustc $release, $host)"
printf '%s' "$results"
if [ "$failures" -gt 0 ]; then
  echo "$failures check(s) failed. Logs: $tmp/logs (set E2E_KEEP=1 to keep them)"
  exit 1
fi
echo "all checks passed"
