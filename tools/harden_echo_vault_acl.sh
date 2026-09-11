#!/usr/bin/env bash
# harden_echo_vault_acl.sh — restore the T-2105 named-user POSIX ACL on
# /home/claude/echo_vault so ci-runner (adr-vault-lint) keeps read access
# and no other box account can read the vault contents.
#
# Run AS the vault owner (claude) after any operation that re-creates the
# vault directory (fresh git clone, disk-loss restore, container re-provision).
# The exposure T-2105 closed is world-readability under /srv/echo_vault
# (world-traversable /srv + a permissive bind-mount source), which returns
# silently on re-clone because POSIX ACLs live in the inode, not in git.
#
# Idempotent: safe to run repeatedly. Exits 0 on success, 1 on missing vault
# or wrong owner, 2 on setfacl/chmod failure.
#
# Refs: T-2105 (original tighten, the operator ruling B, 2026-08-12),
#       T-2885 (this durability follow-up).

set -euo pipefail

VAULT="${ECHO_VAULT_PATH:-/home/claude/echo_vault}"
CI_USER="${ECHO_VAULT_CI_USER:-ci-runner}"

if [ ! -d "$VAULT" ]; then
  echo "harden_echo_vault_acl: no vault at $VAULT — nothing to do (exit 1)" >&2
  exit 1
fi

owner=$(stat -c '%U' "$VAULT")
if [ "$owner" != "$(id -un)" ]; then
  echo "harden_echo_vault_acl: refusing — vault owner is $owner, running as $(id -un). Run as vault owner." >&2
  exit 1
fi

if ! id "$CI_USER" >/dev/null 2>&1; then
  echo "harden_echo_vault_acl: user $CI_USER does not exist on this box; skipping ACL apply (exit 1)" >&2
  exit 1
fi

# Apply the named-user ACL + a default ACL so new files inherit it.
setfacl -R -m "u:${CI_USER}:rX" "$VAULT" || { echo "harden_echo_vault_acl: setfacl -R -m failed" >&2; exit 2; }
setfacl -R -d -m "u:${CI_USER}:rX" "$VAULT" || { echo "harden_echo_vault_acl: setfacl -R -d -m failed" >&2; exit 2; }

# Strip world bits — only owner + named ACL entries can read.
chmod -R o-rwx "$VAULT" || { echo "harden_echo_vault_acl: chmod -R o-rwx failed" >&2; exit 2; }

# Verify by reading back one ACL entry.
if getfacl -p "$VAULT" 2>/dev/null | grep -qE "^user:${CI_USER}:r-x"; then
  echo "harden_echo_vault_acl: OK — $VAULT hardened, $CI_USER retains r-x."
else
  echo "harden_echo_vault_acl: post-apply verify FAILED — expected user:${CI_USER}:r-x on $VAULT" >&2
  exit 2
fi
