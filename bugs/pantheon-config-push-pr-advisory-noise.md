---
symptom: "git push origin main prints 'Bypassed rule violations ... Changes must be made through a pull request' and looks like a rejection"
fix: "Advisory only - the admin bypass accepted the push; verify ground truth with git fetch origin then git log --oneline -1 origin/main before rerouting to a PR"
domain: git, github, branch-protection, pantheon
---
# pantheon-config `git push origin main` prints "Changes must be made through a pull request" — it is ADVISORY, the push usually LANDED

**Symptom:** direct push to `origin/main` prints:

```
remote: Bypassed rule violations for refs/heads/main:
remote: - Changes must be made through a pull request.
```

(or just the second line) and looks like a rejection.

**Reality:** the push was ACCEPTED by a bypass actor; the remote prints the bypassed rule as a warning. The `Bypassed rule violations` header is itself the tell — GitHub prints that phrasing precisely when a *bypass actor* pushes past a rule (a genuine hard rejection reads `Protected branch update failed` / `push declined`, no "Bypassed"). On 2026-07-20 this was misread as a rejection, spawning a pointless agent-branch/PR attempt that then failed with "No commits between main and branch" — because origin/main already had the commit.

**Mechanism (verified 2026-08-08, T-2665 close):** it is NOT classic branch protection — `gh api repos/example-org/pantheon-config/branches/main/protection` returns 404 `Branch not protected`, which makes it look absent. It is an **active repository ruleset** named `protect-main` (id 16495009; rules: `deletion`, `non_fast_forward`, `pull_request`). The `pull_request` rule emits the "must be made through a pull request" line. Its `bypass_actors` are two, both `bypass_mode: always`: the repo **DeployKey** and **User 12345678 (example-org / the operator)**. Because pantheon-config git auth is the SSH **deploy key** fleet-wide, every fleet member's direct push to main is a bypass actor and lands — this is structural, not "admin luck." The one push that WOULD be hard-rejected is one over a *non-deploy-key* credential (a stray PAT, a non-owner GitHub user). To inspect: `gh api repos/example-org/pantheon-config/rulesets` then `.../rulesets/16495009`.

**Rule:** after any scary-looking push output on pantheon-config, verify ground truth before rerouting: `git fetch origin && git log --oneline -1 origin/main`. If your commit is there, you're done. Also note `ticket.py` claim/resolve/block auto-commit AND auto-push their own commits (use `--no-push` to suppress) — origin may already be ahead of your local view mid-arc.

This does NOT change the discipline: worker code changes still go through `agent/<task>` PRs; the bypass path is for HO doc/ledger/ticket commits where direct push has always been the norm.

**Sibling quirk (seen twice 2026-07-20):** when the pre-push hook prints `local is N commit(s) behind origin/main` and auto-rebases, the push itself does NOT complete — you end `[ahead 1]` silently. After ANY push where the hook rebased, re-push and re-verify sync.
