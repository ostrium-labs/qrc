#!/usr/bin/env bash
# Create the shared cache's bucket, policies and keys on a running RustFS store
# (docs/tailscale-shared-cache.md §3.2, §4 and §6.1, step 2 and 3).
#
# Run it on the store box (tag:build-cache) as the owner, once rustfs.service is up:
#
#   sudo ./setup.sh alice bob          # one read-write key per maintainer, plus ci-ro
#   sudo ./setup.sh --ci-rw alice bob  # also the optional ci-rw key (§3.2)
#
# It is idempotent: a user whose key file exists is left alone, the bucket, quota and
# lifecycle rule are only created when missing, and the policies are re-applied.
#
# It never prints a secret. Each key is written to $KEYS_DIR/<user>.env (directory
# mode 0700, file mode 0600) as AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY lines.
# Hand them over through a private channel the owner chooses, never an issue, a pull
# request or chat. While a user is created its secret is a command-line argument of
# `rc` for a moment, visible to other local users of this box; the store box has no
# other users.
#
# Settings (environment, defaults in brackets):
#   STORE_URL    S3 endpoint as seen from this box [http://<RUSTFS_ADDRESS from RUSTFS_ENV>]
#   RUSTFS_ENV   RustFS environment file [/etc/rustfs/rustfs.env]
#   KEYS_DIR     where generated keys are written [/etc/rustfs/cache-keys]
#   BUCKET       [rust-cache]
#   QUOTA        bucket quota, rc units [100G]
#   EXPIRY_DAYS  delete entries older than this, by age (S3 lifecycle cannot see reads) [30]
#   RC           the RustFS client [rc]
set -euo pipefail

# The header comment, without its leading '#', is the usage text.
usage() {
  awk 'NR > 1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0" >&2
  exit 2
}

ci_rw=0
maintainers=()
for arg in "$@"; do
  case "$arg" in
    --ci-rw) ci_rw=1 ;;
    -h|--help) usage ;;
    -*) echo "unknown option: $arg" >&2; usage ;;
    *)
      # User names become access keys and file names: keep them plain.
      if [[ ! "$arg" =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]]; then
        echo "maintainer names are lowercase letters, digits and dashes: $arg" >&2; exit 2
      fi
      maintainers+=("$arg") ;;
  esac
done
[ "${#maintainers[@]}" -gt 0 ] || usage

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUSTFS_ENV="${RUSTFS_ENV:-/etc/rustfs/rustfs.env}"
KEYS_DIR="${KEYS_DIR:-/etc/rustfs/cache-keys}"
BUCKET="${BUCKET:-rust-cache}"
QUOTA="${QUOTA:-100G}"
EXPIRY_DAYS="${EXPIRY_DAYS:-30}"
RC="${RC:-rc}"

# The root credentials and the listen address come from RustFS's own environment
# file, so nothing secret is passed to this script or kept in the shell's history.
envval() { sed -n "s/^$1=//p" "$RUSTFS_ENV" | tail -n 1 | sed 's/[[:space:]]*#.*$//; s/^"//; s/"$//'; }
[ -r "$RUSTFS_ENV" ] || { echo "cannot read $RUSTFS_ENV" >&2; exit 1; }
if [ -z "${STORE_URL:-}" ]; then
  addr="$(envval RUSTFS_ADDRESS)"
  [ -n "$addr" ] || { echo "RUSTFS_ADDRESS is not set in $RUSTFS_ENV; set STORE_URL" >&2; exit 1; }
  STORE_URL="http://$addr"
fi
ak_file="$(envval RUSTFS_ACCESS_KEY_FILE)"
sk_file="$(envval RUSTFS_SECRET_KEY_FILE)"
if [ ! -r "$ak_file" ] || [ ! -r "$sk_file" ]; then
  echo "RUSTFS_ACCESS_KEY_FILE and RUSTFS_SECRET_KEY_FILE in $RUSTFS_ENV must name readable files" >&2; exit 1
fi

# A private rc configuration for this run only, removed on exit: the admin alias
# (which holds the root key) is never left on disk.
RC_CONFIG_DIR="$(mktemp -d)"
export RC_CONFIG_DIR
work="$(mktemp -d)"
cleanup() { rm -rf "$RC_CONFIG_DIR" "$work"; }
trap cleanup EXIT
"$RC" --quiet alias set cacheadmin "$STORE_URL" "$(cat "$ak_file")" "$(cat "$sk_file")" > /dev/null

"$RC" --quiet bucket create "cacheadmin/$BUCKET" --ignore-existing
echo "bucket $BUCKET: present"

if "$RC" --quiet bucket quota info "cacheadmin/$BUCKET" --json 2>/dev/null | grep -q '"quota"[[:space:]]*:[[:space:]]*[1-9]'; then
  echo "quota: already set, left as is"
else
  "$RC" --quiet bucket quota set "cacheadmin/$BUCKET" "$QUOTA"
  echo "quota: $QUOTA"
fi

if [ -n "$("$RC" bucket lifecycle rule list "cacheadmin/$BUCKET" --json 2>/dev/null | grep -i expir || true)" ]; then
  echo "lifecycle: an expiry rule exists, left as is"
else
  "$RC" --quiet bucket lifecycle rule add "cacheadmin/$BUCKET" --expiry-days "$EXPIRY_DAYS"
  echo "lifecycle: entries expire $EXPIRY_DAYS days after they were written"
fi

# The policies name the bucket; substitute it when BUCKET is not the default.
for mode in rw ro; do
  sed "s/arn:aws:s3:::rust-cache/arn:aws:s3:::$BUCKET/g" "$here/policy-rust-cache-$mode.json" > "$work/$mode.json"
  "$RC" --quiet admin policy create cacheadmin "$BUCKET-$mode" "$work/$mode.json"
done
echo "policies: $BUCKET-rw, $BUCKET-ro"

install -d -m 0700 "$KEYS_DIR"
add_user() { # user policy
  local user="$1" policy="$2" file="$KEYS_DIR/$1.env" secret
  if [ -e "$file" ]; then
    echo "user $user: key file exists, left as is"
    return
  fi
  secret="$(openssl rand -hex 20)"
  # The key file is written only after the user and its policy exist, so a run that
  # fails half-way is completed by the next run instead of leaving a key file for a
  # user the store does not have. stdout is discarded because rc prints the secret
  # when its output is JSON.
  "$RC" --quiet admin user add cacheadmin "$user" "$secret" > /dev/null
  "$RC" --quiet admin policy attach cacheadmin "$policy" --user "$user" > /dev/null
  ( umask 077; printf 'AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\n' "$user" "$secret" > "$file.tmp" )
  mv "$file.tmp" "$file"
  echo "user $user: created with $policy, key in $file"
}
for m in "${maintainers[@]}"; do add_user "maintainer-$m" "$BUCKET-rw"; done
add_user ci-ro "$BUCKET-ro"
if [ "$ci_rw" = 1 ]; then add_user ci-rw "$BUCKET-rw"; fi

echo "done. Keys are in $KEYS_DIR; the S3 port must answer only on the tailnet (§6.1 step 4)."
