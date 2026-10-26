#!/usr/bin/env bash
# kino_privilege_drop.sh — T-4106 (T-4027 item 4). One-shot root remediation.
#
# kino (the PWA service user) is the ONLY supplementary member of group
# `claude`, and /home/claude is 750 claude:claude, so every group-writable file
# in orchestrator's home is kino-writable, including dirs on orchestrator's PATH
# (~/.npm-global/bin, ~/.cargo/bin, ~/.bun/bin). kino is also in
# pantheon-shared, which makes orchestrator's pantheon-config checkout (tools/,
# .githooks/, .git/config) kino-writable. A compromised PWA gets exec-as-orchestrator.
#
# This gives the PWA its own kino-owned clone for ticket writes and takes kino
# out of BOTH groups. kino keeps READ of /home/claude/{revenue-project,
# echo-prismatic,pantheon-pwa} (the PWA's hardcoded ratifiable-file roots)
# through a traverse-only ACL on /home/claude; those repos are world-readable.
# Supersedes the kino-as-shared-writer topology in
# sop/standard/shared-repo-multi-writer-permissions.md (T-0281).
#
# Dry-run by default: prints the plan, changes nothing, needs no root.
#   sudo bash tools/kino_privilege_drop.sh --execute
set -euo pipefail

CLONE=/var/lib/kino/pantheon-config
ORIGIN_URL=git@github.com:example-org/pantheon-config.git
ENV_FILE=/etc/kino/.env
ORCHESTRATOR_HOME=/home/claude
ORCHESTRATOR_CHECKOUT=/home/claude/pantheon-config
PULL_UNIT=kino-pantheon-config-pull
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

EXECUTE=0
if [[ "${1:-}" == "--execute" ]]; then EXECUTE=1; fi

die()  { echo "ABORT: $*" >&2; exit 1; }
step() { echo; echo "== $*"; }
run()  { echo "+ $*"; if [[ $EXECUTE -eq 1 ]]; then "$@"; fi; }
# Run as kino with the kino service's own environment, so git sees the same
# ssh config / push key the PWA uses today.
as_kino() {
    systemd-run --quiet --wait --pipe --collect -p User=kino -p Group=kino \
        -p EnvironmentFile=-"$ENV_FILE" -p WorkingDirectory=/var/lib/kino "$@"
}
write_file() {  # write_file <path> <line>...
    local path=$1; shift
    echo "+ write $path"
    printf '    %s\n' "$@"
    if [[ $EXECUTE -eq 1 ]]; then printf '%s\n' "$@" > "$path"; fi
}

print_rollback() {
    echo
    echo "== rollback (if the PWA misbehaves; skip lines for steps that did not run)"
    cat <<EOF
    gpasswd -a kino claude; gpasswd -a kino pantheon-shared
    cp -p $ENV_FILE.bak-t4106-$STAMP $ENV_FILE
    systemctl disable --now $PULL_UNIT.timer
    setfacl -x u:kino $ORCHESTRATOR_HOME
    systemctl restart kino
EOF
}

id kino >/dev/null 2>&1 || die "no kino user"
if [[ $EXECUTE -eq 1 ]]; then
    [[ $EUID -eq 0 ]] || die "--execute must run as root"
    [[ -f "$ENV_FILE" ]] || die "$ENV_FILE missing"
    [[ -d /var/lib/kino ]] || die "/var/lib/kino (kino's home) missing"
fi
echo "mode: $([[ $EXECUTE -eq 1 ]] && echo EXECUTE || echo 'dry-run (no changes)')"
echo "kino groups now: $(id -nG kino)"

step "0. back up $ENV_FILE before any change"
run cp -p "$ENV_FILE" "$ENV_FILE.bak-t4106-$STAMP"
# From here on, always print the rollback, even if a step aborts.
trap print_rollback EXIT

step "1. kino-owned clone at $CLONE (and prove kino's push credential)"
if [[ -d "$CLONE/.git" ]]; then
    echo "exists; not re-cloning"
else
    run as_kino /usr/bin/git clone -q "$ORIGIN_URL" "$CLONE"
fi
run as_kino /usr/bin/git -C "$CLONE" config core.hooksPath .githooks
run as_kino /usr/bin/git -C "$CLONE" push --dry-run -q origin HEAD:main

step "2. freshness pull timer (the PWA pulls only on a push retry)"
write_file "/etc/systemd/system/$PULL_UNIT.service" \
    "[Unit]" \
    "Description=Keep kino's pantheon-config clone fresh for the PWA board (T-4106)" \
    "" \
    "[Service]" \
    "Type=oneshot" \
    "User=kino" \
    "Group=kino" \
    "EnvironmentFile=-$ENV_FILE" \
    "ExecStart=/usr/bin/git -C $CLONE pull --ff-only -q"
write_file "/etc/systemd/system/$PULL_UNIT.timer" \
    "[Unit]" \
    "Description=Pull kino's pantheon-config clone every minute (T-4106)" \
    "" \
    "[Timer]" \
    "OnBootSec=1min" \
    "OnUnitActiveSec=1min" \
    "" \
    "[Install]" \
    "WantedBy=timers.target"
run systemctl daemon-reload
run systemctl enable --now "$PULL_UNIT.timer"

step "3. traverse-only ACL for kino on $ORCHESTRATOR_HOME (keeps the PWA's repo reads)"
run setfacl -m u:kino:--x "$ORCHESTRATOR_HOME"

step "4. point the PWA at the clone ($ENV_FILE; backup from step 0)"
if [[ $EXECUTE -eq 1 ]] && grep -q '^KINO_PANTHEON_CONFIG_PATH=' "$ENV_FILE"; then
    run sed -i "s|^KINO_PANTHEON_CONFIG_PATH=.*|KINO_PANTHEON_CONFIG_PATH=$CLONE|" "$ENV_FILE"
else
    echo "+ set KINO_PANTHEON_CONFIG_PATH=$CLONE (replace the line, or append if absent)"
    if [[ $EXECUTE -eq 1 ]]; then echo "KINO_PANTHEON_CONFIG_PATH=$CLONE" >> "$ENV_FILE"; fi
fi

step "5. remove kino from groups claude + pantheon-shared"
for g in claude pantheon-shared; do
    if id -nG kino | tr ' ' '\n' | grep -qx "$g"; then
        run gpasswd -d kino "$g"
    else
        echo "kino not in $g; skipping"
    fi
done

step "6. restart kino so the daemon drops the groups (brief PWA blip)"
run systemctl restart kino
if [[ $EXECUTE -eq 1 ]]; then
    for _ in $(seq 1 20); do systemctl is-active -q kino && break; sleep 0.5; done
fi

step "7. return kino-owned files in orchestrator's checkout to claude"
run find "$ORCHESTRATOR_CHECKOUT" -xdev -user kino -exec chown -h claude {} +

step "8. verify"
# Probes run AS kino (fresh process, so the new /etc/group applies). Positive
# controls first: a probe that could never say yes would prove nothing.
probe() {  # probe <expect yes|no> <test-flag> <path>
    local want=$1 flag=$2 path=$3 got=no
    if as_kino /usr/bin/test "$flag" "$path"; then got=yes; fi
    local tag=ok; [[ "$got" == "$want" ]] || tag="FAIL <<<"
    printf '    %-8s kino test %s %-50s -> %s (want %s)\n' "$tag" "$flag" "$path" "$got" "$want"
    [[ "$got" == "$want" ]]
}
if [[ $EXECUTE -eq 1 ]]; then
    pid="$(systemctl show -p MainPID --value kino || true)"
    echo "kino active: $(systemctl is-active kino || true)   timer: $(systemctl is-active "$PULL_UNIT.timer" || true)"
    if [[ -n "$pid" && "$pid" != "0" && -r "/proc/$pid/status" ]]; then
        echo "daemon $(grep '^Groups' "/proc/$pid/status")   (expect only kino's gid $(id -g kino))"
    else
        echo "daemon MainPID unavailable ($pid): check 'systemctl status kino'"
    fi
    echo "kino-owned files left in orchestrator's checkout: $(find "$ORCHESTRATOR_CHECKOUT" -xdev -user kino | wc -l)"
    fails=0
    probe yes -w "$CLONE"                               || fails=$((fails+1))
    probe yes -r "$ORCHESTRATOR_HOME/pantheon-pwa/package.json" || fails=$((fails+1))
    probe no  -w "$ORCHESTRATOR_CHECKOUT/tools/ticket.py"      || fails=$((fails+1))
    probe no  -w "$ORCHESTRATOR_CHECKOUT/.git/config"          || fails=$((fails+1))
    probe no  -w "$ORCHESTRATOR_CHECKOUT/tools"                || fails=$((fails+1))
    probe no  -w "$ORCHESTRATOR_HOME/.npm-global/bin"          || fails=$((fails+1))
    probe no  -w "$ORCHESTRATOR_HOME/.cargo/bin"               || fails=$((fails+1))
    probe no  -w "$ORCHESTRATOR_HOME/.claude"                  || fails=$((fails+1))
    if [[ $fails -eq 0 ]]; then echo "VERIFIED: kino writes only its own clone."
    else echo "!!! $fails probe(s) FAILED: report this output to Recovery before using the PWA."; fi
fi
