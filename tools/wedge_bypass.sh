#!/usr/bin/env bash
# tools/wedge_bypass.sh
#
# Wedge-bypass helper for onboarding a new harness as a Pantheon daemon.
# Automates host-level and config-level wedge fixes that block unattended
# operation.  Does NOT automate interactive wedges (device-code login,
# onboarding menu, persona registration).
#
# Usage:
#   wedge_bypass.sh --harness <name> --user <unix-user> [WEDGE_FLAGS]
#   wedge_bypass.sh --verify-only --harness <name> --user <unix-user>
#   wedge_bypass.sh --dry-run --all --harness codex --user codex
#   wedge_bypass.sh --help
#
# Wedge flags (select one or more, or --all):
#   --bwrap       Fix bwrap/userns AppArmor (Ubuntu 24.04+). Requires root/sudo.
#                 Writes /etc/apparmor.d/bwrap, reloads, verifies exit 0.
#   --approval    Write approval_policy=never + sandbox into ~/.codex/config.toml.
#                 Idempotent (check-then-insert; won't duplicate keys/tables).
#   --hooks-trust Stub requirements.toml to pre-answer codex hook-trust gate.
#                 Idempotent.
#   --nux-config  Pre-answer model-availability NUX in config.toml (codex gpt-5.5).
#                 Idempotent.
#   --all         Run all automatable wedge fixes above.
#
# Other flags:
#   --verify-only Check post-conditions only; no mutations. Exit 0 = all pass.
#   --dry-run     Print every mutating command without executing. Composable
#                 with all wedge flags or --all.
#   --help        Show this help and exit 0.
#
# Wedges NOT automated by this script (documented in the onboarder SOP):
#   - Device-code OAuth login (browser flow; run codex login --device-auth
#     by hand in a tmux pane before enabling the service)
#   - Onboarding-menu / trust-dir dialog (first-launch interactive TUI prompts;
#     send-keys recipe documented in the SOP)
#   - Persona registration in AGENT_ROLE_MAP + instance.yaml (requires a repo
#     edit + commit + push)
#   - Bad-auth crash-loop diagnosis (NRestarts check; run after arming service)
#   - Shared-repo checkout permissions (chmod g+w; site-specific)
#   - codex doctor --git (diagnostic; fix is -C <repo> in the supervise script)
#
# Supported harnesses: codex (first non-claude case, ORG-031)
# Future harnesses: extend the per-harness sections below.
#
# Author: T-1031 / ORG-031 §iii generalization, 2026-06-09

set -euo pipefail

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
SCRIPT_NAME="$(basename "$0")"
HARNESS=""
TARGET_USER=""
DO_BWRAP=0
DO_APPROVAL=0
DO_HOOKS_TRUST=0
DO_NUX_CONFIG=0
DO_ALL=0
VERIFY_ONLY=0
DRY_RUN=0

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    sed -n '3,/^# Supported harnesses/p' "$0" | sed 's/^# \{0,1\}//'
}

# ---------------------------------------------------------------------------
# Dry-run aware execution
# ---------------------------------------------------------------------------
run_cmd() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "[dry-run] $*"
    else
        "$@"
    fi
}

run_cmd_root() {
    # Like run_cmd but uses sudo when not already root.
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "[dry-run/root] $*"
    elif [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

die() {
    echo "ERROR: $*" >&2
    exit 1
}

info() {
    echo "[$SCRIPT_NAME] $*"
}

ok() {
    echo "[$SCRIPT_NAME] OK: $*"
}

fail_check() {
    echo "[$SCRIPT_NAME] FAIL: $*" >&2
    return 1
}

# Return the home directory for TARGET_USER.
target_home() {
    eval echo "~${TARGET_USER}"
}

# ---------------------------------------------------------------------------
# Wedge 1: bwrap / userns / AppArmor (Ubuntu 24.04+)
# ---------------------------------------------------------------------------
# Ubuntu 24.04 ships kernel.apparmor_restrict_unprivileged_userns=1 which
# blocks codex's bubblewrap sandbox.  EVERY model shell command dies with
# "bwrap: setting up uid map: Permission denied".  Deceptive: daemon looks
# healthy but does zero file/git work.  Fix: AppArmor profile that exempts
# only /usr/bin/bwrap, leaving the rest of the hardening in place.

fix_bwrap() {
    info "fix_bwrap: writing /etc/apparmor.d/bwrap ..."
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "[dry-run/root] write /etc/apparmor.d/bwrap (bwrap AppArmor profile)"
        echo "[dry-run/root] apparmor_parser -r /etc/apparmor.d/bwrap"
    else
        # Write via a temp file to avoid heredoc stdin-binding issues with
        # run_cmd_root wrappers.
        local tmp_profile
        tmp_profile="$(mktemp /tmp/bwrap-profile.XXXXXX)"
        cat > "$tmp_profile" <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
PROFILE
        if [[ "$(id -u)" -eq 0 ]]; then
            cp "$tmp_profile" /etc/apparmor.d/bwrap
            apparmor_parser -r /etc/apparmor.d/bwrap
        else
            sudo cp "$tmp_profile" /etc/apparmor.d/bwrap
            sudo apparmor_parser -r /etc/apparmor.d/bwrap
        fi
        rm -f "$tmp_profile"
    fi
    info "fix_bwrap: done (run --verify-only to confirm)"
}

verify_bwrap() {
    info "verify_bwrap: testing bwrap --ro-bind / / /bin/true as ${TARGET_USER} ..."
    if [[ "$(id -u)" -eq 0 ]]; then
        # Running as root: use sudo -u to test as the actual daemon user.
        if sudo -u "${TARGET_USER}" bwrap --ro-bind / / /bin/true 2>&1; then
            ok "bwrap --ro-bind / / /bin/true exits 0 (userns unblocked)"
        else
            fail_check "bwrap --ro-bind / / /bin/true failed; AppArmor fix may not have applied"
        fi
    else
        if bwrap --ro-bind / / /bin/true 2>&1; then
            ok "bwrap exits 0 (userns unblocked)"
        else
            fail_check "bwrap failed; AppArmor fix may not have applied or needs root to write profile"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Wedge 2: approval_policy / sandbox (codex config.toml)
# ---------------------------------------------------------------------------
# codex default approval_policy is "on-request" which wedges every tool call
# in an unattended daemon.  Must be set to "never" with workspace-write
# sandbox (network_access=true) — live-verified on the codex daemon
# (ORG-031 §ii).  This function is idempotent: checks before inserting.
fix_approval() {
    local cfg
    cfg="$(target_home)/.codex/config.toml"

    if [[ ! -f "$cfg" ]]; then
        info "fix_approval: $cfg not found; creating ..."
        run_cmd mkdir -p "$(dirname "$cfg")"
        run_cmd touch "$cfg"
    fi

    # approval_policy
    if grep -q '^approval_policy' "$cfg" 2>/dev/null; then
        info "fix_approval: approval_policy already set in $cfg (skipping)"
    else
        info "fix_approval: inserting approval_policy=never ..."
        run_cmd bash -c "printf '\napproval_policy = \"never\"\n' >> \"$cfg\""
    fi

    # sandbox_mode
    if grep -q '^sandbox_mode' "$cfg" 2>/dev/null; then
        info "fix_approval: sandbox_mode already set (skipping)"
    else
        info "fix_approval: inserting sandbox_mode=workspace-write ..."
        run_cmd bash -c "printf 'sandbox_mode = \"workspace-write\"\n' >> \"$cfg\""
    fi

    # [sandbox_workspace_write] network_access = true
    if grep -q '^\[sandbox_workspace_write\]' "$cfg" 2>/dev/null; then
        info "fix_approval: [sandbox_workspace_write] table already present (skipping)"
    else
        info "fix_approval: inserting [sandbox_workspace_write] table ..."
        run_cmd bash -c "printf '\n[sandbox_workspace_write]\nnetwork_access = true\n' >> \"$cfg\""
    fi

    info "fix_approval: done"
}

verify_approval() {
    local cfg
    cfg="$(target_home)/.codex/config.toml"
    local pass=1

    if grep -q 'approval_policy.*never' "$cfg" 2>/dev/null; then
        ok "approval_policy=never present in $cfg"
    else
        fail_check "approval_policy=never not found in $cfg" || pass=0
    fi

    if grep -q 'sandbox_mode.*workspace-write' "$cfg" 2>/dev/null; then
        ok "sandbox_mode=workspace-write present"
    else
        fail_check "sandbox_mode=workspace-write not found in $cfg" || pass=0
    fi

    if grep -q 'network_access.*true' "$cfg" 2>/dev/null; then
        ok "network_access=true present"
    else
        fail_check "network_access=true not found in $cfg" || pass=0
    fi

    return $((1 - pass))
}

# ---------------------------------------------------------------------------
# Wedge 3: hook-trust (codex requirements.toml stub)
# ---------------------------------------------------------------------------
# codex gates new hooks behind an interactive trust prompt unless a
# requirements.toml pre-answers it.  Stub the file so hooks run unattended.
# Idempotent: no-op if the file already exists.
fix_hooks_trust() {
    local req_dir
    req_dir="$(target_home)/.codex"
    local req_file="$req_dir/requirements.toml"

    if [[ -f "$req_file" ]]; then
        info "fix_hooks_trust: $req_file already exists (skipping)"
        return 0
    fi

    info "fix_hooks_trust: creating requirements.toml stub ..."
    run_cmd mkdir -p "$req_dir"
    # Stub: empty requirements.toml satisfies the hooks-trust gate.
    # Operator should populate with actual hook specs from the SOP.
    run_cmd bash -c "cat > \"$req_file\" <<'EOF'
# codex hook-trust requirements stub.
# Populated at onboarding per sop/standard/onboarding-a-new-harness.md.
# An empty requirements.toml satisfies the trust gate without pre-approving
# any specific hooks; add [hooks.<Event>] entries as the daemon's hooks set
# is finalized.
EOF"
    info "fix_hooks_trust: done"
}

verify_hooks_trust() {
    local req_file
    req_file="$(target_home)/.codex/requirements.toml"
    if [[ -f "$req_file" ]]; then
        ok "requirements.toml present at $req_file"
    else
        fail_check "requirements.toml not found at $req_file"
    fi
}

# ---------------------------------------------------------------------------
# Wedge 4: GPT-5.5 NUX "press enter to continue" (codex TUI)
# ---------------------------------------------------------------------------
# After restart, codex sometimes shows a model-availability notice for
# gpt-5.5 that blocks the TUI until Enter is pressed.  Setting the NUX
# counter to 4 (above the display threshold) silences it.
# Idempotent: checks for the key before inserting.
fix_nux_config() {
    local cfg
    cfg="$(target_home)/.codex/config.toml"

    if [[ ! -f "$cfg" ]]; then
        info "fix_nux_config: $cfg not found; creating ..."
        run_cmd mkdir -p "$(dirname "$cfg")"
        run_cmd touch "$cfg"
    fi

    if grep -q '^\[tui\.model_availability_nux\]' "$cfg" 2>/dev/null; then
        info "fix_nux_config: [tui.model_availability_nux] already present (skipping)"
        return 0
    fi

    info "fix_nux_config: inserting [tui.model_availability_nux] gpt-5.5=4 ..."
    run_cmd bash -c "printf '\n[tui.model_availability_nux]\n\"gpt-5.5\" = 4\n' >> \"$cfg\""
    info "fix_nux_config: done"
}

verify_nux_config() {
    local cfg
    cfg="$(target_home)/.codex/config.toml"
    if grep -q '"gpt-5.5"' "$cfg" 2>/dev/null; then
        ok "gpt-5.5 NUX counter present in $cfg"
    else
        fail_check "gpt-5.5 NUX counter not found in $cfg"
    fi
}

# ---------------------------------------------------------------------------
# Verify-only: run all post-condition checks
# ---------------------------------------------------------------------------
run_verify_only() {
    local failures=0

    verify_bwrap || failures=$((failures + 1))

    local cfg
    cfg="$(target_home)/.codex/config.toml"
    if [[ -f "$cfg" ]]; then
        verify_approval || failures=$((failures + 1))
        verify_nux_config || failures=$((failures + 1))
    else
        info "verify: $cfg not found; skipping config checks"
    fi

    verify_hooks_trust || failures=$((failures + 1))

    if [[ "$failures" -eq 0 ]]; then
        info "All post-conditions passed."
        return 0
    else
        echo "[$SCRIPT_NAME] $failures post-condition check(s) FAILED." >&2
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
if [[ $# -eq 0 ]]; then
    usage
    exit 1
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            usage
            exit 0
            ;;
        --harness)
            HARNESS="${2:-}"
            shift 2
            ;;
        --user)
            TARGET_USER="${2:-}"
            shift 2
            ;;
        --bwrap)
            DO_BWRAP=1
            shift
            ;;
        --approval)
            DO_APPROVAL=1
            shift
            ;;
        --hooks-trust)
            DO_HOOKS_TRUST=1
            shift
            ;;
        --nux-config)
            DO_NUX_CONFIG=1
            shift
            ;;
        --all)
            DO_ALL=1
            shift
            ;;
        --verify-only)
            VERIFY_ONLY=1
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        *)
            die "Unknown option: $1  (run $SCRIPT_NAME --help)"
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Validate required arguments
# ---------------------------------------------------------------------------
[[ -n "$HARNESS" ]]     || die "--harness <name> is required"
[[ -n "$TARGET_USER" ]] || die "--user <unix-user> is required"

# Currently only codex is documented; guard against accidental use on
# unsupported harnesses.  Extend this list as new harnesses are onboarded.
case "$HARNESS" in
    codex) ;;
    *)
        die "Unsupported harness '$HARNESS'.  Currently supported: codex.  " \
            "Extend this script when onboarding a new harness per the SOP."
        ;;
esac

# Expand --all into individual flags.
if [[ "$DO_ALL" -eq 1 ]]; then
    DO_BWRAP=1
    DO_APPROVAL=1
    DO_HOOKS_TRUST=1
    DO_NUX_CONFIG=1
fi

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if [[ "$VERIFY_ONLY" -eq 1 ]]; then
    run_verify_only
    exit $?
fi

# At least one wedge flag must be set (or --all / --verify-only).
if [[ "$DO_BWRAP" -eq 0 && "$DO_APPROVAL" -eq 0 && \
      "$DO_HOOKS_TRUST" -eq 0 && "$DO_NUX_CONFIG" -eq 0 ]]; then
    die "No wedge selected.  Use --all or one of: --bwrap --approval --hooks-trust --nux-config"
fi

[[ "$DRY_RUN" -eq 1 ]] && info "DRY-RUN mode — no host mutations will be made."

[[ "$DO_BWRAP" -eq 1 ]]       && fix_bwrap
[[ "$DO_APPROVAL" -eq 1 ]]    && fix_approval
[[ "$DO_HOOKS_TRUST" -eq 1 ]] && fix_hooks_trust
[[ "$DO_NUX_CONFIG" -eq 1 ]]  && fix_nux_config

info "Wedge-bypass complete.  Run with --verify-only to check post-conditions."
