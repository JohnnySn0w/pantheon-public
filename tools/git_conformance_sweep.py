#!/usr/bin/env python3
"""git_conformance_sweep — daily read-only check of the ORG-101 git-redundancy standard (§7, T-4690).

Flags any repo or seat clone that departs from the standard, and mails the steward (Recovery):

  Repos (every repo GITHUB_OWNER owns on GitHub, against git.your-self-hosted-git.example/github-mirror/<name>):
    NO_COPY          no Forgejo counterpart (fix: forgejo_repo_rollout.py --apply)
    PULL_MIRROR      the copy is still a pull-mirror (fix: forgejo_repo_rollout.py --convert).
                     An ARCHIVED repo's pull-mirror is the disposition, not a finding.
    UNCOVERED        the name is one the reconciler refuses (git_reconciler._SAFE_NAME)
    EXPOSURE         the copy is public while GitHub is private (the other direction is safe)
    DEFAULT_BRANCH   the two default branches differ (the reconciler syncs GitHub's)
    PROTECTION       GitHub protects the default branch and the converted copy does not
    UNVERIFIED       a protection read failed (reported, never read as "fine")
  Seat clones (every fleet account x every manifest repo with remotes, at ~<account>/<repo>):
    ORIGIN           origin does not match the manifest primary (github-<repo> aliases match)
    URL_REWRITE      an insteadOf/pushInsteadOf rule, which disables the write mirror
    PIN              the manifest pin is missing or does not match the clone's repos.yaml,
                     so the seat refuses the manifest (no read failover, no write mirror)

Info lines (journal only): orphan Forgejo copies, a copy private while GitHub is public,
archived pull-mirrors. A seat with no clone is the clone-currency check's business, not this one.

Shape: the fleet clone-currency check. Read-only everywhere (GitHub/Forgejo GETs; on seats only
`git remote get-url`, `git config --get-regexp` and reading two files, through `sudo -n -u`).
Skip list: git-conformance-skip-list.yaml, entries {kind, subject, disposition_ticket, reason}.
An entry expires when its ticket is in tickets/done/. Fail-open: a broken skip list skips nothing.

Exit: 0 = the sweep ran (findings are mailed, not an exit code); 1 = an inventory failed.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pwd
import subprocess
import sys
import urllib.error
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import agent_role_map as arm  # noqa: E402
from tools import forgejo_repo_rollout as ro  # noqa: E402
from tools import git_reconciler as gr  # noqa: E402
from tools import remote_resolve as rr  # noqa: E402

STEWARD_INBOX = "/home/post-office/mailbox/recovery"
SKIP_LIST = REPO_ROOT / "git-conformance-skip-list.yaml"
DONE_DIR = REPO_ROOT / "tickets" / "done"
PIN_REL = ".local/state/pantheon/" + rr.PIN_FILENAME
SEAT_TIMEOUT = 30
PIN_REPO = "pantheon-config"  # the clone whose repos.yaml the seat pin covers
UNIT_DIR_REL = ".config/systemd/user"
READ_FAILOVER_ON = "PANTHEON_READ_FAILOVER=1"  # the T-1998 arming drop-in's line


@dataclass(frozen=True)
class Finding:
    kind: str
    subject: str
    detail: str
    info: bool = False

    def line(self) -> str:
        return f"{'info' if self.info else self.kind:<14} {self.subject}: {self.detail}"


# ---------------------------------------------------------------- repos


def github_protected(name: str, branch: str, token: str) -> bool:
    """GitHub's own answer: a branch rule (classic or ruleset) applies to the branch."""
    b = urllib.parse.quote(branch, safe="")
    try:
        got = ro._req(f"{ro.GITHUB_API}/repos/{ro.GITHUB_OWNER}/{name}/branches/{b}",
                      {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    except urllib.error.HTTPError as e:
        if e.code == 404:  # empty repo: no default branch exists, so nothing is protected (T-4736)
            return False
        raise
    return bool(got.get("protected"))


def repo_findings(gh: list[dict], fj: dict[str, dict], *, gh_protected, fj_protected) -> list[Finding]:
    out = []
    for r in sorted(gh, key=lambda r: r["name"].lower()):
        name, f = r["name"], fj.get(r["name"])
        private, archived = bool(r["private"]), bool(r.get("archived"))
        branch = r.get("default_branch") or "main"
        if not gr._SAFE_NAME.match(name) or name in (".", ".."):
            out.append(Finding("UNCOVERED", name, "the reconciler refuses this name"))
        if f is None:
            out.append(Finding("NO_COPY", name, "no Forgejo copy (forgejo_repo_rollout.py --apply)"))
            continue
        if f.get("mirror"):
            if archived:
                out.append(Finding("PULL_MIRROR", name, "archived; left as a pull-mirror", info=True))
            else:
                out.append(Finding("PULL_MIRROR", name, "still a pull-mirror "
                                   "(forgejo_repo_rollout.py --convert)"))
        if f.get("private") is False and private:
            out.append(Finding("EXPOSURE", name, "Forgejo copy is PUBLIC, GitHub is private"))
        elif f.get("private") and not private:
            out.append(Finding("VISIBILITY", name, "Forgejo private, GitHub public", info=True))
        fb = f.get("default_branch")
        if fb and not f.get("empty") and fb != branch:
            out.append(Finding("DEFAULT_BRANCH", name, f"Forgejo {fb!r}, GitHub {branch!r}"))
        try:
            unprotected = not f.get("mirror") and not archived and gh_protected(name, branch) \
                and not fj_protected(name, branch)
        except Exception as e:  # noqa: BLE001 — unknown is not "no": report it, keep sweeping (T-4736)
            out.append(Finding("UNVERIFIED", name, f"protection read failed: {e!r}"[:300]))
            continue
        if unprotected:
            out.append(Finding("PROTECTION", name, f"GitHub protects {branch!r}; the Forgejo copy "
                               "has no rule (forgejo_repo_rollout.py --convert repairs it)"))
    for name in sorted(set(fj) - {r["name"] for r in gh}, key=str.lower):
        out.append(Finding("ORPHAN", name, "Forgejo copy with no GitHub repo", info=True))
    return out


# ---------------------------------------------------------------- seats


def _seat_run(account: str, argv: list[str]) -> subprocess.CompletedProcess:
    """Run a read-only command as `account` (directly for our own account: sudo to self is denied)."""
    me = pwd.getpwuid(os.getuid()).pw_name
    cmd = argv if account == me else ["sudo", "-n", "-u", account, *argv]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=SEAT_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(cmd, 124, "", repr(e))


def seat_findings(manifest: rr.Manifest, accounts, run=_seat_run, home=None) -> list[Finding]:
    home = home or (lambda a: Path(pwd.getpwnam(a).pw_dir))
    out = []
    for account in accounts:
        try:
            h = home(account)
        except KeyError:
            continue
        for repo in sorted(manifest.repos.values(), key=lambda r: r.name):
            primary = next((r for r in repo.remotes if r.role == "primary"), None)
            if primary is None:
                continue
            clone, subject = h / repo.name, f"{account}:~/{repo.name}"
            got = run(account, ["git", "-C", str(clone), "remote", "get-url", "origin"])
            if got.returncode != 0:
                continue  # no clone (or no origin): the clone-currency check owns that
            origin = got.stdout.strip()
            if rr._origin_key(origin) != rr._origin_key(primary.url):
                out.append(Finding("ORIGIN", subject, f"origin {origin!r} is not the manifest "
                                   f"primary {primary.url!r}"))
            if any(r.writable for r in repo.remotes):
                rw = run(account, ["git", "-C", str(clone), "config", "--get-regexp",
                                   r"^url\..*\.(push)?insteadof$"])
                if rw.returncode == 0:
                    out.append(Finding("URL_REWRITE", subject, "url rewrite configured; the write "
                                       "mirror is refused: " + " | ".join(rw.stdout.split("\n")[:2])))
            if repo.name == PIN_REPO:
                out += _pin_finding(account, h, clone, subject, run)
    return out


def _pin_finding(account, h: Path, clone: Path, subject: str, run) -> list[Finding]:
    pin = run(account, ["cat", str(h / PIN_REL)])
    if pin.returncode != 0 or not pin.stdout.strip():
        return [Finding("PIN", subject, "no manifest pin (python3 tools/manifest_pin.py bootstrap)")]
    yml = run(account, ["cat", str(clone / rr.MANIFEST_REL)])
    if yml.returncode != 0:
        return [Finding("PIN", subject, "repos.yaml unreadable")]
    if hashlib.sha256(yml.stdout.encode()).hexdigest() != pin.stdout.strip():
        # T-5672: only an ARMED seat (read failover on) consults the pin, and only its pull
        # refreshes it; an unarmed seat's pin just ages. Stale there is info, not a refusal.
        if run(account, ["grep", "-rqs", READ_FAILOVER_ON, str(h / UNIT_DIR_REL)]).returncode != 0:
            return [Finding("PIN", subject, "pin does not match repos.yaml; seat unarmed (pin unused), "
                            "refresh before arming (python3 tools/manifest_pin.py refresh)", info=True)]
        return [Finding("PIN", subject, "pin does not match repos.yaml; the seat refuses the "
                        "manifest (python3 tools/manifest_pin.py refresh)")]
    return []


# ---------------------------------------------------------------- skip list + report


def load_skips(path: Path = SKIP_LIST, done_dir: Path = DONE_DIR) -> list[dict]:
    """Live skip entries. Fail-open: any problem yields no skips (never suppresses an alert)."""
    try:
        import yaml
        doc = yaml.safe_load(path.read_text()) or {}
        entries = doc.get("skips") or []
    except Exception:  # noqa: BLE001
        return []
    live = []
    for e in entries:
        if not isinstance(e, dict) or not all(e.get(k) for k in ("kind", "subject", "disposition_ticket")):
            continue
        if (done_dir / f"{e['disposition_ticket']}.md").exists():
            continue  # the disposition is closed: the skip expires
        live.append(e)
    return live


def apply_skips(findings: list[Finding], skips: list[dict]) -> tuple[list[Finding], list[Finding]]:
    keys = {(s["kind"], s["subject"]) for s in skips}
    kept = [f for f in findings if (f.kind, f.subject) not in keys]
    return kept, [f for f in findings if (f.kind, f.subject) in keys]


def render(alerts: list[Finding], info: list[Finding], skipped: list[Finding], n_repos: int) -> str:
    lines = [f"git conformance sweep (ORG-101 §7): {n_repos} GitHub repos; "
             f"{len(alerts)} finding(s), {len(skipped)} skipped, {len(info)} info"]
    lines += ["", *[f.line() for f in alerts]] if alerts else []
    lines += ["", "skipped:", *[f.line() for f in skipped]] if skipped else []
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print only; send no mail")
    ap.add_argument("--no-seats", action="store_true", help="skip the seat-clone checks")
    a = ap.parse_args(argv)
    try:
        gh_tok, fj_tok = ro.read_token(ro.GITHUB_LIST_TOKEN), ro.read_token(ro.FORGEJO_TOKEN)
        gh, fj = ro.github_repos(gh_tok), ro.forgejo_repos(fj_tok)
    except (OSError, urllib.error.URLError, ValueError) as e:
        print(f"ERROR: inventory failed: {e!r}", file=sys.stderr)
        return 1
    findings = repo_findings(
        gh, fj,
        gh_protected=lambda n, b: github_protected(n, b, gh_tok),
        fj_protected=lambda n, b: ro.is_protected(ro.Item(n, ro.REGULAR, True, True, False, b), fj_tok))
    if not a.no_seats:
        try:
            manifest = rr.load_production_manifest()
        except Exception as e:  # noqa: BLE001 — our own pin is broken: say so, keep the repo half
            findings.append(Finding("PIN", "runner", f"the sweep's own manifest load failed: {e!r}"))
        else:
            findings += seat_findings(manifest, arm.fleet_accounts())
    alerts, skipped = apply_skips([f for f in findings if not f.info], load_skips())
    info = [f for f in findings if f.info]
    report = render(alerts, info, skipped, len(gh))
    print(report)
    for f in info:
        print("  " + f.line())
    if alerts and not a.dry_run:
        from tools import mailbox
        mailbox.send(STEWARD_INBOX, report, "git_conformance_sweep",
                     subject=f"git conformance: {len(alerts)} finding(s) (ORG-101 §7)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
