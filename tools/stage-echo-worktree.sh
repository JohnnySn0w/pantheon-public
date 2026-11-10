#!/usr/bin/env bash
# Stage a fresh /tmp/echo-<task> worktree off origin/main with vendor symlink.
#
# Why: `git worktree add` creates a new working tree with the gitignored
# `vendor/` directory absent. Echo's silero-vad adapter dlopens libonnxruntime
# via `ORT_DYLIB_PATH` (workspace-relative), so missing `vendor/onnxruntime-*`
# causes 9/9 silero_vad tests to fail with `libonnxruntime.so: No such file
# or directory`. T-0658 traced this across the T-0500 / T-0642 / T-0643 cycles.
#
# Fix: after `git worktree add`, symlink the worktree's `vendor/` entries to
# the canonical /home/claude/echo-prismatic/vendor/ entries (a seat clone's vendor/
# entry is a symlink into the shared read-only /var/lib/pantheon/echo-vendor, T-6014). Vendor binaries
# are identical across worktrees by design (same ORT version pinned in
# tools/setup-onnxruntime.sh) so the symlink is structurally correct.
#
# Usage:
#   bash tools/stage-echo-worktree.sh <ticket-id>
# Example:
#   bash tools/stage-echo-worktree.sh T-0700
# Produces:
#   /tmp/echo-T-0700/ worktree on branch agent/T-0700 off origin/main
#   /tmp/echo-T-0700/vendor/onnxruntime-linux-x64-1.22.0 -> canonical vendor

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <ticket-id>  (e.g. T-0700)" >&2
  exit 1
fi

TICKET="$1"
ECHO_CANONICAL="${ECHO_CANONICAL:-/home/claude/echo-prismatic}"  # T-5006: override for a seat with its own clone (mason/artisan)
WORKTREE_PATH="/tmp/echo-${TICKET}"
BRANCH_NAME="agent/${TICKET}"

# Disk floor guard (T-1668): each staged lane grows a full target/ (5-25G) and an
# in-flight multi-lane sprint outruns the arc-close GC — 2026-07-10 hit 99% disk
# (2G free) mid-sprint at 29G/h burn. Refuse to stage a NEW lane when the disk
# can't absorb one more build; override consciously with ECHO_STAGE_FORCE=1 after
# freeing space (clear IDLE lanes' target/ dirs only — never in-flight ones).
DISK_FLOOR_GB="${ECHO_STAGE_DISK_FLOOR_GB:-15}"
AVAIL_GB=$(df -BG --output=avail /tmp | tail -1 | tr -dc '0-9')
if [[ "${AVAIL_GB}" -lt "${DISK_FLOOR_GB}" && "${ECHO_STAGE_FORCE:-0}" != "1" ]]; then
  echo "error: only ${AVAIL_GB}G free (< floor ${DISK_FLOOR_GB}G) — a new lane's target/ won't fit. (T-1668 guard)" >&2
  echo "       Inspect: du -sh /tmp/echo-*/target /home/claude/echo-prismatic/target — clear IDLE lanes only," >&2
  echo "       or re-run with ECHO_STAGE_FORCE=1 / tune ECHO_STAGE_DISK_FLOOR_GB." >&2
  exit 1
fi

# auth-smoke (below) delegates to this tool (T-4122); resolve + check it BEFORE
# staging so a missing tool can't leave a half-staged lane behind.
EVIDENCE_TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/echo_evidence_build.py"
if [[ ! -f "${EVIDENCE_TOOL}" ]]; then
  echo "FATAL: ${EVIDENCE_TOOL} not found; auth-smoke delegates to it (T-4122)" >&2
  exit 1
fi
# The path is baked into auth-smoke: staging from a transient checkout leaves a
# wrapper that breaks once that checkout is removed.
case "${EVIDENCE_TOOL}" in
  /tmp/*|*/.claude/worktrees/*)
    echo "WARNING: baking a TRANSIENT tool path into auth-smoke: ${EVIDENCE_TOOL} — it breaks when that worktree is removed; stage from a durable pantheon-config clone" >&2 ;;
esac

if [[ ! -d "${ECHO_CANONICAL}" ]]; then
  echo "error: ${ECHO_CANONICAL} not found; expected the canonical Echo clone" >&2
  exit 1
fi

if [[ -e "${WORKTREE_PATH}" ]]; then
  echo "error: ${WORKTREE_PATH} already exists; remove it first or use a different ticket id" >&2
  exit 1
fi

cd "${ECHO_CANONICAL}"
git fetch origin >&2
git worktree add "${WORKTREE_PATH}" -b "${BRANCH_NAME}" origin/main >&2

# Vendor symlink: discover each entry under canonical vendor/, mirror into
# worktree vendor/. Handles future per-platform additions without hardcoding.
mkdir -p "${WORKTREE_PATH}/vendor"
for entry in "${ECHO_CANONICAL}"/vendor/*; do
  [[ -e "${entry}" ]] || continue
  name="$(basename "${entry}")"
  ln -s "${entry}" "${WORKTREE_PATH}/vendor/${name}"
done

# Activate the pre-push smoke gate for this worktree (T-0683). Belt-and-suspenders:
# once tools/install-hooks.sh has been run on the canonical clone the shared config
# already covers worktrees, but this makes a freshly-staged worktree self-sufficient.
git -C "${WORKTREE_PATH}" config core.hooksPath .githooks

# Shared lane target dir (T-1668(a)): every staged lane builds into ONE shared
# CARGO_TARGET_DIR instead of growing its own 5-25G target/ — this is the disk
# fix for the 2026-07-10 multi-lane 99%-disk incident. Cargo's build-directory
# lock makes concurrent lane builds mutually exclusive: a lane's cargo will print
# "Blocking waiting for file lock on build directory" while another lane builds.
# That blocking is INTENTIONAL — it is the serialize-the-smokes discipline made
# structural. Dependency artifacts (the bulk) share across lanes since all stage
# off origin/main; leaf-crate variants coexist under fingerprint hashes.
# NEVER `cargo clean` from a lane mid-sprint (it empties the shared dir for
# every lane); GC happens at arc close. Home-checkout builds keep their own
# target/ and never contend. Opt out per-stage with ECHO_STAGE_SHARED_TARGET=0.
# T-6014: under $HOME, not /home/claude, so a seat with its own clone (mason/artisan, via
# ECHO_CANONICAL) can stage; unchanged on the claude account.
LANE_TARGET_DIR="${ECHO_STAGE_LANE_TARGET_DIR:-${HOME}/.cache/echo-lane-target}"
if [[ "${ECHO_STAGE_SHARED_TARGET:-1}" == "1" ]]; then
  mkdir -p "${LANE_TARGET_DIR}"
  mkdir -p "${WORKTREE_PATH}/.cargo"
  # APPEND the [build] override below the worktree's tracked .cargo/config.toml
  # content — an overwrite here discards the tracked [env] table (ORT_DYLIB_PATH,
  # OPENSSL_VENDORED) and reintroduces the exact 9/9 silero dlopen failure this
  # script exists to prevent (T-1753; regression shipped in T-1668a).
  printf '\n[build]\ntarget-dir = "%s"\n' "${LANE_TARGET_DIR}" >> "${WORKTREE_PATH}/.cargo/config.toml"
  if ! grep -q '^ORT_DYLIB_PATH' "${WORKTREE_PATH}/.cargo/config.toml"; then
    echo "FATAL: ${WORKTREE_PATH}/.cargo/config.toml lost its [env] ORT_DYLIB_PATH entry after staging (T-1753 guard)" >&2
    exit 1
  fi
  # T-1986: .cargo/config.toml is TRACKED, and the append above puts a
  # /home/claude absolute path in it. Left visible, a `git add -A`/`commit -a`
  # sweeps that path into the branch and trips echo CI's /home/claude path guard
  # (T-1906) — a hard, NON-admin-bypassable ci-gate failure, discovered only
  # after review. skip-worktree marks the local edit invisible to git: `git
  # status` stays clean and `git add -A` never stages it, while cargo still reads
  # the file normally (skip-worktree is a git-index bit, nothing to do with cargo).
  git -C "${WORKTREE_PATH}" update-index --skip-worktree .cargo/config.toml
  echo "shared lane target-dir: ${LANE_TARGET_DIR} (cargo lock serializes concurrent lane builds — blocking is expected)" >&2
fi

# Authoritative-smoke isolation (T-1932 hybrid): the shared cache above stays the
# default for DEV loops, but APPROVE-gating smoke runs must not read sibling lanes'
# interleaved artifacts (bugs/echo-shared-target-dir-concurrent-lane-contention.md:
# phantom E0432s / vanishing rlibs with zero FAILED lines). Generate a per-worktree
# `auth-smoke` env-wrapper. T-4122: it DELEGATES to tools/echo_evidence_build.py (one
# implementation for lane and seat/reviewer worktrees): CARGO_TARGET_DIR keyed on the
# worktree realpath under /var/lib/pantheon/echo-evidence-targets (orphans reaped by
# `echo_evidence_build.py --gc` once the worktree is removed), CARGO_INCREMENTAL=0,
# umask 002. The T-1668 disk floor keeps its ECHO_STAGE_* knobs (mapped onto the
# tool's ECHO_EVIDENCE_* ones); the wrapper still warns on sibling cargo/rustc activity.
# The wrapper deliberately does NOT own the smoke command — the authoritative
# command comes from the CI workflow file; this only supplies isolation:
#   cd /tmp/echo-T-NNNN && ./auth-smoke cargo test --workspace
# (run it FROM the worktree: the tool refuses a cwd outside it, since cargo builds
# the cwd's sources into the target dir keyed on the worktree — T-4122.)
cat > "${WORKTREE_PATH}/auth-smoke" <<WRAPPER
#!/usr/bin/env bash
# Generated by stage-echo-worktree.sh (T-1932; T-4122 delegation). Runs the given
# command with an ISOLATED per-worktree CARGO_TARGET_DIR via echo_evidence_build.py.
set -euo pipefail
WT="\$(cd "\$(dirname "\$0")" && pwd)"
if pgrep -af 'cargo|rustc' | grep -v "\$\$" | grep -qv auth-smoke; then
  echo "auth-smoke: WARNING — sibling cargo/rustc processes active; isolation protects THIS run's artifacts, but CPU contention will slow it" >&2
fi
# T-1668 floor semantics preserved: a caller-set ECHO_EVIDENCE_DISK_FLOOR_GB wins, else
# ECHO_STAGE_DISK_FLOOR_GB (default 15); ECHO_STAGE_FORCE=1 maps to ECHO_EVIDENCE_FORCE=1.
export ECHO_EVIDENCE_DISK_FLOOR_GB="\${ECHO_EVIDENCE_DISK_FLOOR_GB:-\${ECHO_STAGE_DISK_FLOOR_GB:-15}}"
if [[ "\${ECHO_STAGE_FORCE:-0}" == "1" ]]; then export ECHO_EVIDENCE_FORCE=1; fi
exec python3 "${EVIDENCE_TOOL}" --worktree "\${WT}" -- "\$@"
WRAPPER
chmod +x "${WORKTREE_PATH}/auth-smoke"
echo "auth-smoke wrapper: cd ${WORKTREE_PATH} && ./auth-smoke cargo test --workspace — REQUIRED for APPROVE-gating smoke runs (T-1932 hybrid; dev loops keep the shared cache)" >&2

# T-1986: keep lane scratch out of `git add -A`. The generated auth-smoke wrapper
# and the untracked target dirs (target-auth/ was the pre-T-4122 wrapper's isolated
# dir, still excluded for lanes staged before the delegation; target-wip if a
# lane sets its own CARGO_TARGET_DIR) are siblings of `target/` and so are NOT
# matched by the tracked .gitignore's `target/` rule — a `git add -A` would stage
# them. These names are always lane scratch, so exclude them via the repo's
# info/exclude (idempotent; worktree-local behaviour, never committed).
EXCLUDE_FILE="$(git -C "${WORKTREE_PATH}" rev-parse --git-common-dir)/info/exclude"
mkdir -p "$(dirname "${EXCLUDE_FILE}")"
for pat in 'auth-smoke' 'target-auth/' 'target-wip/'; do
  grep -qxF "${pat}" "${EXCLUDE_FILE}" 2>/dev/null || echo "${pat}" >> "${EXCLUDE_FILE}"
done

echo "${WORKTREE_PATH} (branch ${BRANCH_NAME})"
