---
symptom: "running any ticket.py verb (annotate/claim/resolve) from inside a /tmp agent worktree silently advances the agent branch: the auto-commit lands the ticket-log note on the BRANCH (main's ticket copy never gets it), and the CLI's repo-sync can merge origin/main into the branch mid-arc — even with --no-pull — invalidating in-flight sweep baselines"
fix: "run ticket.py ONLY from the main checkout (/home/claude/pantheon-config), or have the dispatcher (HO) persist worker-tier notes; if it already happened: nothing is lost — the note reaches main at PR merge, but re-measure any full-sweep baseline in a detached worktree pinned to the pre-merge commit, and do NOT also annotate the same ticket on main (double-append = PR merge conflict on the ticket file)"
domain: "ticket-cli / agent-worktree discipline"
---

# ticket.py inside an agent worktree commits to the branch and merges main into it

**Observed** 2026-08-12, T-2910 arc (broker-po-2): the PO ran `ticket.py annotate T-2910 --no-pull` from `/tmp/pantheon-config-T-2910`. The annotation auto-committed onto `agent/T-2910` (main's `tickets/active/T-2910.md` shows nothing), and `origin/main` got merged into the branch mid-arc, pulling in the just-landed T-2842 code and invalidating the PO's recorded full-sweep baseline.

**Mechanism.** `tools/_git_utils.py`'s sync helpers (`pull_repo_or_warn` / `pull_repo_strict` / the commit flow's freshness check) resolve `REPO_ROOT` from `__file__` and operate on the CURRENT checkout — in a worktree that is the agent branch, so "sync against origin/main" becomes "merge origin/main into the agent branch". `--no-pull` gates only the up-front pull site (`ticket.py:3715`), not every sync site in the auto-commit flow. (Exact per-site map unverified — labeled observed-behavior; a structural fix would be a repo-root/branch guard in `_git_utils`.)

**Consequences to know:**
- The ticket-log note is NOT lost — it rides the branch and lands on main at PR merge. Restart-safe against worker death (committed), NOT against worktree deletion (branch is local-only until push).
- Never append notes to the same ticket on main while a branch copy has diverged — both sides appending to the transition log = guaranteed merge conflict at PR time.
- Any full-sweep baseline recorded before the silent merge is stale; re-measure pinned to an exact commit (detached worktree), and supersede the stale figure in the log rather than editing it.

**Dispatcher guidance:** brief POs/implementers that ticket.py runs from the main checkout only — or route ticket-log persistence through the dispatcher. Worth a structural guard eventually (ticket.py refusing to run when `git branch --show-current` starts with `agent/`).

## See also
[`lessons/2026-08-27-repo-global-tool-ops-implicitly-bind-to-worktree.md`](../lessons/2026-08-27-repo-global-tool-ops-implicitly-bind-to-worktree.md) — this bug is one of three that ground the pattern-tier lesson (with the test-suite-real-git-pull and live-hook-rollout-strands sibling bugs).
