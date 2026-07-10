#!/usr/bin/env python3
"""Directive<->skill drift guard (T-0796).

Source of truth: rulebook/directives.md (the canonical D1..DN set).
Quick-ref that MUST stay in sync: the pantheon-org-discipline skill, auto-loaded
into every worker. The T-0603 failure was the skill silently lagging directives
(it carried D1-D3 after D11 shipped; D12 after the 2026-05-27 promotion) so a
D-governed dispatch never applied the directive because the worker's skill never
named it. This makes that drift LOUD instead of silent.

Checks that every `## DN` header in directives.md is mentioned in the skill.
Exit 0 = in sync; exit 1 = drift (skill missing one or more directive ids).

Cross-repo: directives.md lives in pantheon-config; the canonical skill lives in
the claude-code harness-config repo. Skill path resolves in order: --skill arg,
$ORG_SKILL_PATH, then a list of known local fallbacks.

Decoupling contract (ORG-039 §4): a fork of pantheon-config WITHOUT a harness-config
repo configured must not half-break. So:
  - harness skill PRESENT            -> strict drift check (exit 1 on drift).
  - harness path CONFIGURED but absent (--skill / $ORG_SKILL_PATH points at a
    missing file)                     -> explicit, actionable SKIP (exit 0).
  - NO harness configured at all (a clean fork)
                                      -> explicit SKIP naming how to enable it (exit 0).
The skip is LOUD and actionable, never silent — strict validation only runs when the
harness skill is actually present.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRECTIVES = os.path.join(REPO, "rulebook", "directives.md")

SKILL_FALLBACKS = [
    os.path.expanduser("~/claude-config/skills/pantheon-org-discipline/SKILL.md"),
    os.path.expanduser("~/.claude/skills/pantheon-org-discipline/SKILL.md"),
    os.path.join(REPO, "claude-config", "skills", "pantheon-org-discipline", "SKILL.md"),
]

DID = re.compile(r"\bD(\d+)\b")


def directive_ids(path):
    """The source set: every `## DN — ...` header in directives.md."""
    ids = set()
    with open(path) as f:
        for line in f:
            m = re.match(r"^#+\s*D(\d+)\b", line)
            if m:
                ids.add(int(m.group(1)))
    return ids


def skill_ids(path):
    """Every Dn token mentioned anywhere in the skill."""
    with open(path) as f:
        return {int(n) for n in DID.findall(f.read())}


def resolve_skill(arg):
    """Return (path_or_None, configured_path_or_None).

    `configured_path` is the explicitly-configured harness skill location (--skill or
    $ORG_SKILL_PATH) if one was given, regardless of whether it exists — so the caller
    can distinguish "configured but missing" (actionable) from "not configured" (clean fork).
    """
    configured = arg or os.environ.get("ORG_SKILL_PATH")
    for cand in [arg, os.environ.get("ORG_SKILL_PATH"), *SKILL_FALLBACKS]:
        if cand and os.path.exists(cand):
            return cand, configured
    return None, configured


def main():
    arg = None
    for a in sys.argv[1:]:
        if a.startswith("--skill="):
            arg = a.split("=", 1)[1]
        elif a == "--skill" and sys.argv.index(a) + 1 < len(sys.argv):
            arg = sys.argv[sys.argv.index(a) + 1]

    src = directive_ids(DIRECTIVES)
    skill, configured = resolve_skill(arg)
    if not skill:
        if configured:
            # A harness skill path WAS configured but the file is absent — actionable.
            print(f"[directive-skill-drift] SKIP: configured harness skill path not found: "
                  f"{configured}\n  The drift check is decoupled (ORG-039 §4) — it only runs "
                  f"when the harness-config skill is present. Fix the path or unset it.")
        else:
            # No harness configured — a clean fork without claude-code-config. Skip cleanly.
            print("[directive-skill-drift] SKIP: no harness-config repo configured "
                  "(pantheon-org-discipline SKILL.md not found). This is expected for a fork "
                  "without a harness-config repo. To enable the drift check, set $ORG_SKILL_PATH "
                  "(or --skill) to your harness-config skill, or clone claude-code-config beside "
                  "pantheon-config. Strict validation runs only when the skill is present (ORG-039 §4).")
        return 0

    have = skill_ids(skill)
    missing = sorted(src - have)
    if missing:
        ids = ", ".join(f"D{n}" for n in missing)
        print(f"[directive-skill-drift] DRIFT: directives.md defines {ids} but the "
              f"pantheon-org-discipline skill does not mention them.\n"
              f"  source : {DIRECTIVES}\n  skill  : {skill}\n"
              f"  -> update the skill (claude-config) + bump the vendored submodule pins. "
              f"This is the T-0603 staleness class.")
        return 1
    print(f"[directive-skill-drift] OK: skill covers all {len(src)} directive ids "
          f"(D1-D{max(src)}). skill={skill}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
