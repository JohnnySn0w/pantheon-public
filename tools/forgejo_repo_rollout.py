#!/usr/bin/env python3
"""forgejo_repo_rollout — make every GitHub repo exist on Forgejo (ORG-101 §4/§6, T-4690 leg 3).

Inventory: every repo the GitHub account owns, mapped 1:1 to git.your-self-hosted-git.example/github-mirror/<name>.
Plan, per repo:

  MISSING     no Forgejo copy          -> CREATE it as a Forgejo PULL-mirror of the GitHub repo
  MIRROR      a pull-mirror            -> nothing (tracks GitHub on its own; conversion waits on
                                          reconciler coverage, ORG-101 §4)
  REGULAR     already converted        -> nothing here (the reconciler owns it)
  VISIBILITY  private flag differs     -> reported; only the EXPOSURE direction (public on
                                          Forgejo, private on GitHub) is fixed, by making the copy
                                          private. The other direction is the safe one.

Why create as a pull-mirror, not a regular repo: a pull-mirror tracks GitHub with no reconciler,
so a new repo can never silently stop tracking. Conversion is a separate, coverage-gated step.

--convert (ORG-101 §4, T-4690 leg 3a) is that step. Coverage: the inventory-mode reconciler
(git_reconciler.py) maps every owned GitHub repo to its Forgejo copy, so every copy is covered;
a converted copy is just pushed to instead of mirror-synced. Per repo:

  MIRROR, not archived  -> only if the copy's default-branch head EQUALS GitHub's: POST /convert,
                           then protect the default branch (PROTECTION). A copy that is behind
                           gets a mirror-sync instead and is converted on a later run, so a
                           conversion never freezes a stale copy.
  MIRROR, archived      -> left as a pull-mirror (GitHub is read-only; nothing to reconcile).
  REGULAR, not archived -> protect the default branch if it has no rule yet. This repairs a run
                           that converted but failed to protect, so a rerun is always safe.

--limit N caps conversions per run (dry run, then 1 repo, then batches).

Least privilege: the mirror's GitHub credential is the READ-only contents PAT (~/forgejo.tok),
never the reconciler's write PAT. Forgejo-side calls use the pantheon-mirror token.

Default is a dry run. --apply performs the actions; --only NAME limits to named repos.
Exit: 0 = nothing to do or all actions succeeded; 1 = an action failed or a lookup failed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

GITHUB_API = "https://api.github.com"
FORGEJO_API = "https://git.your-self-hosted-git.example/api/v1"
FORGEJO_ORG = "github-mirror"
GITHUB_OWNER = os.environ.get("PANTHEON_ROLLOUT_GITHUB_OWNER", "example-org")
MIRROR_INTERVAL = "24h"  # matches the existing 67 mirrors
UA = "pantheon-forgejo-rollout/1"
GITHUB_LIST_TOKEN = Path.home() / ".github-reconciler.token"  # listing only (sees private repos)
GITHUB_MIRROR_TOKEN = Path.home() / "forgejo.tok"            # READ-only; handed to Forgejo
FORGEJO_TOKEN = Path.home() / ".forgejo-mirror.token"

MISSING, MIRROR, REGULAR = "MISSING", "MIRROR", "REGULAR"

# The default-branch rule for a converted copy: the pantheon-config rule (2026-10-07) with the
# allowlist narrowed to the reconciler's own user. Seats are added per repo when its writable flip
# is wanted (ORG-101 §5), never here. Protected branches refuse force-push and deletion.
PROTECTION = {
    "enable_push": True,
    "enable_push_whitelist": True,
    "push_whitelist_usernames": ["pantheon-mirror"],
    "push_whitelist_deploy_keys": False,
    "required_approvals": 1,
    "block_on_rejected_reviews": True,
    "apply_to_admins": True,
}


def read_token(path: Path) -> str:
    """First line; a NAME=value line yields value (forgejo.tok is gh_pat=...)."""
    first = path.read_text().strip().splitlines()[0].strip()
    name, sep, value = first.partition("=")
    if sep and name.replace("_", "").isalnum() and value.strip("="):
        return value.strip()
    return first


def _req(url: str, headers: dict, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers={**headers, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def github_repos(token: str) -> list[dict]:
    out, page = [], 1
    while True:
        batch = _req(f"{GITHUB_API}/user/repos?affiliation=owner&per_page=100&page={page}",
                     {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        if not batch:
            return [r for r in out if r["owner"]["login"] == GITHUB_OWNER]
        out += batch
        page += 1


def forgejo_repos(token: str) -> dict[str, dict]:
    out, page = {}, 1
    while True:
        batch = _req(f"{FORGEJO_API}/orgs/{FORGEJO_ORG}/repos?limit=50&page={page}",
                     {"Authorization": f"token {token}"})
        if not batch:
            return out
        out.update({r["name"]: r for r in batch})
        page += 1


@dataclass
class Item:
    name: str
    state: str
    github_private: bool
    forgejo_private: bool | None
    archived: bool
    default_branch: str = "main"

    @property
    def exposure(self) -> bool:
        """Public on Forgejo while private on GitHub: the only visibility mismatch we fix."""
        return self.forgejo_private is False and self.github_private


def plan(gh: list[dict], fj: dict[str, dict], only: list[str] | None) -> list[Item]:
    items = []
    for r in sorted(gh, key=lambda r: r["name"].lower()):
        if only and r["name"] not in only:
            continue
        f = fj.get(r["name"])
        state = MISSING if f is None else (MIRROR if f.get("mirror") else REGULAR)
        items.append(Item(r["name"], state, bool(r["private"]),
                          None if f is None else bool(f["private"]), bool(r.get("archived")),
                          r.get("default_branch") or "main"))
    return items


def create_mirror(item: Item, fj_token: str, gh_read_token: str) -> None:
    _req(f"{FORGEJO_API}/repos/migrate", {"Authorization": f"token {fj_token}"}, "POST", {
        "clone_addr": f"https://github.com/{GITHUB_OWNER}/{item.name}.git",
        "auth_username": "x-access-token",
        "auth_password": gh_read_token,
        "mirror": True,
        "mirror_interval": MIRROR_INTERVAL,
        "private": item.github_private,  # ORG-101 §6: the per-repo flag mirrors GitHub
        "repo_owner": FORGEJO_ORG,
        "repo_name": item.name,
        "service": "git",  # code only, like the existing mirrors
    })


def make_private(item: Item, fj_token: str) -> None:
    _req(f"{FORGEJO_API}/repos/{FORGEJO_ORG}/{item.name}", {"Authorization": f"token {fj_token}"},
         "PATCH", {"private": True})


def _fj(fj_token: str) -> dict:
    return {"Authorization": f"token {fj_token}"}


def _head(url: str, headers: dict, key: str) -> str | None:
    """The branch's head sha, or None when the branch does not exist (an empty repo)."""
    try:
        return _req(url, headers)["commit"][key]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def heads(item: Item, gh_token: str, fj_token: str) -> tuple[str | None, str | None]:
    """(GitHub head, Forgejo head) of the default branch."""
    b = urllib.parse.quote(item.default_branch, safe="")
    g = _head(f"{GITHUB_API}/repos/{GITHUB_OWNER}/{item.name}/branches/{b}",
              {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}, "sha")
    f = _head(f"{FORGEJO_API}/repos/{FORGEJO_ORG}/{item.name}/branches/{b}", _fj(fj_token), "id")
    return g, f


def mirror_sync(item: Item, fj_token: str) -> None:
    _req(f"{FORGEJO_API}/repos/{FORGEJO_ORG}/{item.name}/mirror-sync", _fj(fj_token), "POST")


def convert(item: Item, fj_token: str) -> None:
    _req(f"{FORGEJO_API}/repos/{FORGEJO_ORG}/{item.name}/convert", _fj(fj_token), "POST")


def is_protected(item: Item, fj_token: str) -> bool:
    rules = _req(f"{FORGEJO_API}/repos/{FORGEJO_ORG}/{item.name}/branch_protections", _fj(fj_token))
    return any(r.get("rule_name") == item.default_branch or r.get("branch_name") == item.default_branch
               for r in rules or [])


def protect(item: Item, fj_token: str) -> None:
    _req(f"{FORGEJO_API}/repos/{FORGEJO_ORG}/{item.name}/branch_protections", _fj(fj_token), "POST",
         {**PROTECTION, "rule_name": item.default_branch, "branch_name": item.default_branch})


def run_convert(items: list[Item], apply: bool, limit: int | None) -> int:
    """The --convert pass (see the module doc). Returns the number of failed actions."""
    mode = "APPLY" if apply else "DRY-RUN"
    fj, gh = read_token(FORGEJO_TOKEN), read_token(GITHUB_LIST_TOKEN)
    failed = converted = 0
    for it in items:
        if it.archived:
            if it.state == MIRROR:
                print(f"  skip {it.name}: archived on GitHub; left as a pull-mirror")
            continue
        try:
            if it.state == REGULAR:
                if is_protected(it, fj):
                    continue
                if apply:
                    protect(it, fj)
                print(f"  [{mode}] {it.name}: {'protected' if apply else 'would protect'} "
                      f"{it.default_branch} (regular copy had no rule)")
                continue
            if it.state != MIRROR:
                continue
            if limit is not None and converted >= limit:
                print(f"  defer {it.name}: --limit {limit} reached")
                continue
            g, f = heads(it, gh, fj)
            if g != f:
                if apply:
                    mirror_sync(it, fj)
                print(f"  [{mode}] {it.name}: copy behind on {it.default_branch} "
                      f"({(f or '-')[:9]} vs github {(g or '-')[:9]}); mirror-sync, convert next run")
                continue
            converted += 1
            if not apply:
                print(f"  [{mode}] {it.name}: would convert + protect {it.default_branch} "
                      f"(in sync at {(g or 'empty')[:9]})")
                continue
            convert(it, fj)
            protect(it, fj)
            print(f"  [{mode}] {it.name}: converted + protected {it.default_branch}: done")
        except Exception as e:  # noqa: BLE001 — report and continue with the next repo
            failed += 1
            print(f"  [{mode}] {it.name}: FAILED {e!r}"[:400])
    print(f"convert: {converted} {'converted' if apply else 'convertible'}, {failed} failed")
    return failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="perform the actions (default: dry run)")
    ap.add_argument("--only", action="append", help="limit to this repo name (repeatable)")
    ap.add_argument("--convert", action="store_true",
                    help="convert in-sync pull-mirrors to regular repos and protect them")
    ap.add_argument("--limit", type=int, help="with --convert: at most N conversions this run")
    a = ap.parse_args(argv)
    if a.limit is not None and not a.convert:
        ap.error("--limit needs --convert")
    try:
        gh = github_repos(read_token(GITHUB_LIST_TOKEN))
        fj = forgejo_repos(read_token(FORGEJO_TOKEN))
    except (OSError, urllib.error.URLError, ValueError) as e:
        print(f"ERROR: inventory failed: {e!r}", file=sys.stderr)
        return 1
    items = plan(gh, fj, a.only)
    counts: dict[str, int] = {}
    for it in items:
        counts[it.state] = counts.get(it.state, 0) + 1
    print(f"inventory: {len(items)} GitHub repos owned by {GITHUB_OWNER}; " +
          ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if a.convert:
        return 1 if run_convert(items, a.apply, a.limit) else 0
    mode = "APPLY" if a.apply else "DRY-RUN"
    failed = 0
    for it in items:
        actions = []
        if it.state == MISSING:
            actions.append(("create pull-mirror" + (" (private)" if it.github_private else " (public)"),
                            lambda it=it: create_mirror(it, read_token(FORGEJO_TOKEN),
                                                        read_token(GITHUB_MIRROR_TOKEN))))
        if it.exposure:
            actions.append(("make Forgejo copy private (exposure)",
                            lambda it=it: make_private(it, read_token(FORGEJO_TOKEN))))
        elif it.forgejo_private is not None and it.forgejo_private != it.github_private:
            print(f"  note {it.name}: Forgejo private={it.forgejo_private}, GitHub "
                  f"private={it.github_private} (safe direction; left as is)")
        for label, fn in actions:
            if not a.apply:
                print(f"  [{mode}] {it.name}: would {label}")
                continue
            try:
                fn()
                print(f"  [{mode}] {it.name}: {label}: done")
            except Exception as e:  # noqa: BLE001 — report and continue with the next repo
                failed += 1
                print(f"  [{mode}] {it.name}: {label}: FAILED {e!r}"[:400])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
