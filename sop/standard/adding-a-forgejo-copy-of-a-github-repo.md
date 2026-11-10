# Adding a Forgejo copy of a GitHub repo (ORG-101)

**Status:** Adopted 2026-11-02 by Recovery (methodology, below the goal-level bar per amended D3), after T-5993. The operator asked for the durable fix (TG 9201).

## When to use this

Use it whenever a task says, or implies, "give repo X a second copy on Forgejo (git.your-self-hosted-git.example)". Typical forms: an ORG-101 write mirror, a "backup of the backup", making a repo survive a GitHub outage, or a new writable `mirror` remote in `repos.yaml`.

## The fact this SOP exists to keep

**Every repo the GitHub owner has ALREADY has a Forgejo copy.** `tools/forgejo_repo_rollout.py` creates `git.your-self-hosted-git.example/github-mirror/<same name>` for each owned GitHub repo, private when the GitHub repo is private, and `tools/git_reconciler.py` (ORG-101 §3) keeps each copy in sync. The copy exists whether or not `repos.yaml` mentions it. So "add a Forgejo copy" almost always means "use the existing `github-mirror/<name>`", not "create a repo".

T-5993 (2026-11-02) is why this is written down. Recovery hand-created `vps-backup/pantheon-vps-backup` as "a new second copy" of root's VPS backup, and told the operator it was a new data location that needed his approval. `github-mirror/pantheon-vps-backup` already held the same history, private and reconciled. The reconciler's NO_COPY alert on the hand-made repo exposed the duplicate.

## Procedure

1. **Look it up first.** `GET /api/v1/repos/github-mirror/<name>` with `~/.forgejo-mirror.token` (user `pantheon-mirror`, repo-admin on `github-mirror/*`; send a non-default `User-Agent`). A 200 means the copy exists: note `private`, `mirror` (pull-mirror or converted) and `empty`.
2. **If it exists, reuse it.** Do not create another repo.
   - To WRITE to it (an ORG-101 writable mirror), it must be converted (`mirror: false`). `forgejo_repo_rollout.py --convert` converts it only when it equals GitHub, then protects the default branch.
   - Give the writer access per seat or per service, not by sharing a token: add a write collaborator (`PUT /repos/github-mirror/<name>/collaborators/<user>` `{"permission":"write"}`) and mint that user a `write:repository` token (see `reference_forgejo_admin_access` for the admin CLI). Check its scope with a negative control: an unrelated private repo must return 404.
   - Point the `repos.yaml` mirror at `https://<user>@git.your-self-hosted-git.example/github-mirror/<name>.git`, add the repo to the writable allowlist in `tools/test_remote_resolve_writable.py`, and run the manifest tests.
3. **If it does not exist,** create it by running `tools/forgejo_repo_rollout.py` (dry run first), not by hand. It creates a pull-mirror and keeps the visibility safe; conversion is a separate, coverage-gated step.
4. **Never hand-create a Forgejo repo for a GitHub repo.** The reconciler cannot see a repo outside `github-mirror/`, so it raises NO_COPY forever, and you will have two copies that drift.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Reconciler mails `NO_COPY` for a repo you just added to `repos.yaml` | the manifest mirror url is outside `github-mirror/` (a hand-made repo), or the reconciler's token cannot read it | point the mirror at `github-mirror/<name>`; delete the hand-made repo only after reading its state in a separate step |
| You are about to tell the operator "this puts the data somewhere new" | you have not checked step 1 | check step 1 first; the copy is usually already there |
| A deletion removes more than expected | the emptiness check and the delete ran in one command | run the check, read it, then delete in a separate call |

## Boundary

This covers where a Forgejo copy comes from and how a writer gets access to it. It does not cover: the reconciler's own operation (`tools/git_reconciler.py`), release mirrors (`refreshing-an-org-039-release-mirror.md`; those are genericized derivatives, not copies), or whether a NEW credential may be minted (the operator's call, D3; see T-5993 TG 9190-9191).

## Cross-references

- `tools/forgejo_repo_rollout.py`, `tools/git_reconciler.py`, `tools/remote_resolve.py` (`writable_mirror_for`)
- `repos.yaml` (`auth_refs`, `writable: true`), `tools/test_remote_resolve_writable.py`
- T-5993, T-4690 (ORG-101 rollout), ORG-101
