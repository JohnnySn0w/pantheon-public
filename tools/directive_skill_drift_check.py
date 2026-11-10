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

Since ORG-091 (T-4750) the canonical skill lives in this repo's harness/claude/; a
fork may carry it in a separate harness-config repo instead. Skill path resolves in
order: --skill arg, else $ORG_SKILL_PATH, else the known fallbacks (in-repo harness/claude,
then ~/.claude/skills). A configured path is never overridden: if it is missing the check
SKIPs, it does not fall through to a fallback (T-4842). The old ~/claude-config clone is
not consulted (frozen, goes stale: T-4763; retired: T-4750).

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
    os.path.join(REPO, "harness", "claude", "skills", "pantheon-org-discipline", "SKILL.md"),
    os.path.expanduser("~/.claude/skills/pantheon-org-discipline/SKILL.md"),
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
    if configured:
        # Configured wins outright. Missing => no path (caller SKIPs); never fall through to a
        # fallback, which would report OK for a skill file the seat does not actually load.
        return (configured if os.path.exists(configured) else None), configured
    for cand in SKILL_FALLBACKS:
        if os.path.exists(cand):
            return cand, None
    return None, None


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
                  f"{configured}\n  Set via --skill or $ORG_SKILL_PATH; fallbacks are NOT used "
                  f"when a path is configured. Fix the path, or unset both to use the in-repo "
                  f"harness/claude skill. (Decoupled per ORG-039 §4: the check only runs when the "
                  f"skill is present.)")
        else:
            # No harness configured — a clean fork without claude-code-config. Skip cleanly.
            print("[directive-skill-drift] SKIP: no harness-config repo configured "
                  "(pantheon-org-discipline SKILL.md not found). This is expected for a fork "
                  "without a harness-config repo. To enable the drift check, set $ORG_SKILL_PATH "
                  "(or --skill) to your harness-config skill, or restore harness/claude/ in this "
                  "repo. Strict validation runs only when the skill is present (ORG-039 §4).")
        return 0

    have = skill_ids(skill)
    missing = sorted(src - have)
    if missing:
        ids = ", ".join(f"D{n}" for n in missing)
        print(f"[directive-skill-drift] DRIFT: directives.md defines {ids} but the "
              f"pantheon-org-discipline skill does not mention them.\n"
              f"  source : {DIRECTIVES}\n  skill  : {skill}\n"
              f"  -> update the skill (harness/claude/skills/pantheon-org-discipline/ in this repo, "
              f"or your fork's harness-config repo). "
              f"This is the T-0603 staleness class.")
        return 1
    print(f"[directive-skill-drift] OK: skill covers all {len(src)} directive ids "
          f"(D1-D{max(src)}). skill={skill}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
