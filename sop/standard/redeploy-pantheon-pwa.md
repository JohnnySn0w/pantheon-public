# SOP — Redeploy pantheon-pwa to the live kino server

**Status:** Authored 2026-05-21 by Orchestrator (HO) per the operator msg_1825. Content derived from
verified production state (2026-05-21) + the established carrier precedents
T-0232/T-0239/T-0251/T-0265/T-0285/T-0287 (Recovery-executed). Open to the operator review.

**Owner:** Recovery (`claude-fallback`) — holds the kino-pwa repair-sudo path. Coordinates with
Orchestrator (HO) only if the sudo-into-claude path is unavailable.

**When:** After *any* change lands on `pantheon-pwa` main that affects the served bundle or the
kino backend. **Merge ≠ deploy** — a git push to `pantheon-pwa` main does NOT update the live
PWA (see [`visual-verification-for-ui-tickets.md`](visual-verification-for-ui-tickets.md) §3–5,
the deploy-carrier pattern). Redeploys are normally tracked as a `target=recovery` carrier ticket
(e.g. T-0515).

## Production layout (verified 2026-05-21)

- **Source checkout:** `/home/claude/pantheon-pwa` (owned `claude:claude`). Build happens here.
- **Deployment copy:** `/opt/pantheon-pwa` (owned `claude:claude`, mode 775). The kino daemon's
  `WorkingDirectory`. Has its own `node_modules` (backend deps) + `build/web` (served bundle).
- **Service:** `kino.service` — `User=kino`, `ExecStart=/usr/local/bin/kino-launcher.sh`
  (launches per-agent `kino-agent@*` then execs the bun daemon), listens `127.0.0.1:8443`.
  System unit → **restart needs root** (`sudo`).
- **Served bundle path:** `KINO_WEB_BUILD_DIR` defaults to `./build/web` relative to
  `WorkingDirectory` → `/opt/pantheon-pwa/build/web`.
- **bun:** at `/home/claude/.bun/bin/bun` — **not on the default PATH**; export it explicitly.

Because both source and deployment copy are `claude`-owned, Recovery executes this via its
**sudo-into-claude path** (the same path used for the precedent carriers); the rsync itself
needs no root once running as `claude`, but the `kino.service` restart does.

## Procedure

```bash
# 0. Confirm the change is on pantheon-pwa main and the PWA is still being served
#    (NB: T-0508 proposes retiring old kino daemons — verify kino.service is wanted before deploying)
sudo systemctl is-active kino                     # expect: active
git -C /home/claude/pantheon-pwa log --oneline -1 # expect: the merged tip you intend to ship

# 1. Build at source (explicit bun PATH — this is the classic gotcha)
cd /home/claude/pantheon-pwa
export PATH="$HOME/.bun/bin:$PATH"
bun run build:web        # cd web && bun install && bun run build → outputs ./build/web/

# 2. Smoke the build — all three artifacts must be present before shipping
bun run smoke:build      # asserts build/web/{index.html, manifest.webmanifest, sw.js}

# 3. Ship source + built bundle to the deployment copy (no --delete; exclude deps/vcs)
rsync -a \
  --exclude node_modules --exclude .git --exclude web/node_modules \
  /home/claude/pantheon-pwa/ /opt/pantheon-pwa/

# 4. Restart the service so the backend + served bundle pick up the new code (root)
sudo systemctl restart kino

# 5. Verify live
sudo systemctl is-active kino                     # active
journalctl -u kino -n 30 --no-pager               # clean: no errors, "N agents registered", listening 127.0.0.1:8443
ls -l --time-style=+%H:%M /opt/pantheon-pwa/build/web/index.html   # mtime should be fresh (this deploy)
grep -rl '<new-identifier>' /opt/pantheon-pwa/build/web/ | head    # optional: confirm the new code is what shipped
```

## Acceptance (carrier-ticket "done" criteria)

Per the deploy-carrier pattern, the carrier ticket is NOT done at "merged to main". It is done when:

1. **Build green** — `smoke:build` passed.
2. **Deploy actuated** — `/opt/pantheon-pwa` in sync with the merged ref (rsync ran).
3. **Service restarted** — `kino.service` restarted, `is-active` green, journal clean.
4. **Verified live** — fresh `build/web` mtime; ideally the new behavior confirmed end-to-end.

Visual acceptance of the *carried* UI change belongs to the original UI ticket (done at the
implementer cycle), NOT to this redeploy carrier — see visual-verification SOP §3. After deploy,
the operator (the operator) typically does a real-device smoke; dev-vs-production render drift is
cataloged separately.

## Cross-references

- [`visual-verification-for-ui-tickets.md`](visual-verification-for-ui-tickets.md) §3–5 — the
  deploy-carrier pattern this SOP instantiates (merge ≠ deploy; build + ship + restart + verify-live).
- `pantheon-pwa/README.md` §"PWA build + serve" — `build:web` / `smoke:build` pipeline, `KINO_WEB_BUILD_DIR`.
- `adr/ORG-006-kino-pwa.md` — kino-pwa architecture (kino-launcher, kino-agent@ instances, agent registry).
- Precedent carriers: T-0265 (ratification UI), T-0285 (stats panel), T-0287 (chat-bubble width) —
  resolve logs record the build→rsync→restart→verify sequence this SOP formalizes.
