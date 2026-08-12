#!/usr/bin/env python3
"""Anti-flattening guards for the account lists that are narrow ON PURPOSE (T-2256).

T-2256 gave the fleet one canonical OS-account → persona map and repointed the
fleet-wide copies at it. Of the 34 sites inventoried, 21 were DELIBERATELY narrow
and repointing them would have been a larger regression than the bug. specialist was
explicit about this when filing: "the inventory is an inventory, not a defect
list ... a sweep that flattened all twenty would be worse than the disease."

THE PROBLEM THIS FILE SOLVES. Now that a canonical map exists, every remaining
narrow list looks like an oversight — and the more consistent the codebase gets,
the more a future sweep (or a future lint of the "flag a dict literal whose keys
look like OS accounts" kind the ticket proposes) will want to "finish the job."
The comments on those literals say why they are narrow, but a comment is a policy
nobody reads. This file is the architecture: flattening any of them fails here,
with the reason attached.

These tests assert a NEGATIVE — that a set has NOT grown — which is unusual and
deliberate. Failing here does not mean the change is wrong; it means the change
crosses a boundary somebody recorded a reason for, and the reason is in the
failure message. Read it, then either revert or update this file WITH the ruling
that authorized the widening.

The four guarded here are the ones where flattening does concrete damage. The
remaining narrow sites (the fire-path three, the pane/health scopes, the ordered
reboot list, the frozen audit evidence, …) are left to their comments: their
failure modes are recoverable, and a guard per literal would be its own kind of
twenty-first list.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class TestCredentialMonitorStillExcludesCodex(unittest.TestCase):
    def test_codex_is_absent(self) -> None:
        """codex authenticates via OpenAI, and has no Claude OAuth credential
        file at all. Widening this to the full fleet does not add coverage — it
        adds a permanent false alarm about a file that will never exist."""
        from tools import credential_expiry_monitor as cem

        self.assertNotIn("codex", cem.CREDENTIALED_ACCOUNTS)
        self.assertNotIn("codex", set(cem.CREDENTIALED_ACCOUNTS.values()))

    def test_it_still_covers_every_claude_oauth_seat(self) -> None:
        """The other half: narrow-on-purpose is not a licence to go stale. This
        set must hold every account that DOES carry a Claude OAuth credential —
        i.e. every bound account except codex."""
        from tools import credential_expiry_monitor as cem
        from tools.agent_role_map import account_persona_map

        expected = {p for a, p in account_persona_map().items() if a != "codex"}
        self.assertEqual(set(cem.CREDENTIALED_ACCOUNTS), expected)


class TestMailStalenessCandidatesStayWiderThanThePersonaMap(unittest.TestCase):
    def test_specialist_a2a_survives(self) -> None:
        """CANDIDATE_INBOXES is deliberately WIDER than the persona map: it
        holds `specialist-a2a` precisely BECAUSE that is not a persona, and a
        persona-derived list is exactly the probe that missed it (T-2159).
        Repointing this at account_persona_map() deletes the one entry it was
        extended to catch."""
        from tools import mail_staleness_monitor as msm

        self.assertIn("specialist-a2a", msm.CANDIDATE_INBOXES)

    def test_it_is_not_merely_the_persona_set(self) -> None:
        from tools import mail_staleness_monitor as msm
        from tools.agent_role_map import account_persona_map

        personas = set(account_persona_map().values())
        self.assertTrue(
            set(msm.CANDIDATE_INBOXES) - personas,
            "CANDIDATE_INBOXES has been narrowed to the persona set — a list of "
            "names cannot contain the box nobody named, which is the whole reason "
            "it is a FALLBACK and not the primary source of truth",
        )


class TestPaneHomePrefixesStayFailClosed(unittest.TestCase):
    def test_it_has_not_silently_grown(self) -> None:
        """_AGENT_HOME_PREFIXES is a fail-CLOSED security boundary feeding an
        auto-resolve path: an account that is ABSENT cannot be auto-resolved to,
        so ADDING accounts LOOSENS it. Widening this is a security change and
        needs to be argued as one, not landed as a consistency refactor."""
        from tools import _pane_predicates as pp

        self.assertEqual(set(pp._AGENT_HOME_PREFIXES), {"orchestrator", "specialist", "recovery"})
        for persona, prefixes in pp._AGENT_HOME_PREFIXES.items():
            with self.subTest(persona=persona):
                self.assertTrue(all(p.startswith("/home/") for p in prefixes))


# The pre-push hook path, overridable so the guard below can be falsified
# against a synthetic hook WITHOUT mutating the live one. That hook governs
# every seat's pushes; a sabotage-and-restore window on it is a risk with no
# upside when an env seam gives the same recorded red. Amendment H explicitly
# sanctions an env seam for exactly this ("fake the gap-closing surface — an env
# seam is fine"). Unset in every real run, so the default IS the live hook.
_HOOK_PATH_ENV = "PANTHEON_TEST_PRE_PUSH_PATH"


# Matches ONE `    <a>|<b>|...) IS_SEAT=<n> ;;` case arm. `*` is inside the
# character class ON PURPOSE: without it a catch-all flipped to `*) IS_SEAT=1`
# — which gates EVERY account on the host — is not merely missed, it is
# unmatchable, and the guard reports the hook as healthy. With `*` in the class
# the flip surfaces as the alternative "*" and breaks the set equality.
_CASE_ARM_RE = re.compile(r"^[ \t]*([\w.|*-]+)\)[ \t]*IS_SEAT=(\d)\b", re.MULTILINE)


def _seat_gate_alternatives(hook_text: str) -> set[str]:
    """Return the UNION of shell-case alternatives across EVERY `IS_SEAT=1` arm.

    Parses each `    <a>|<b>|...) IS_SEAT=<n> ;;` arm, splits the arms that set
    IS_SEAT=1 on "|", and unions them. Returning a SET is the entire point — see
    the guard below for why substring checks are not an assertion here.

    WHY EVERY ARM AND NOT THE FIRST. The first version of this parser used
    `re.search`, i.e. the FIRST matching arm only. A shell `case` is a list, and
    the cheapest way to widen one is not to edit the existing arm at all — it is
    to add another arm. Measured, not theorised: appending
    `claude|claude-research|claude-fallback|exocortex) IS_SEAT=1 ;;` on the line
    below the real arm left this guard reporting 3 passed while `claude` and
    `claude-fallback` came out IS_SEAT=1 under bash. That shape was caught by the
    even-weaker substring guard this function replaced, so first-match-only was a
    strict regression. Union-across-arms is the only reading that matches what
    the shell actually does with the file.

    KNOWN AND ACCEPTED: unioning across all arms means an `IS_SEAT=1` arm that
    the shell would never reach — inside `if false; then`, or below the `*)`
    catch-all — still counts, producing a false RED. That is deliberate. This
    parser is a text guard, not a shell interpreter, and the failure direction of
    "shout about an unreachable arm" is the safe one; the failure direction of
    "stay quiet about a reachable one" bricks the fleet. The live hook has
    exactly one arm, so the cost is zero today. Do not "fix" this by teaching the
    parser reachability — that trades a loud false alarm for a silent real one.
    """
    arms = _CASE_ARM_RE.findall(hook_text)
    seat_arms = [alts for alts, value in arms if value == "1"]
    if not seat_arms:
        # Zero IS_SEAT=1 arms is fail-LOUD, not an empty set. An empty set would
        # still fail the equality assertion above today, but only by accident of
        # what it is compared against; any future caller asking "is <account>
        # gated?" would read the silence as a clean answer. And the state it
        # describes — the seat gate renamed, restructured or deleted outright —
        # is precisely the one that needs a human, since deleting the arm
        # silently retires PR-only-for-code for both seats. Note this also fires
        # when the hook has NO recognisable arms at all, which is the same
        # "parser no longer understands this file" verdict.
        raise AssertionError(
            "could not locate any `...) IS_SEAT=1` case arm in the pre-push hook — "
            "the seat gate was renamed, restructured or removed, any of which needs "
            "a human look rather than a silently-passing guard"
        )
    return {alt for alts in seat_arms for alt in alts.split("|") if alt}


class TestPrePushSeatGateStaysSeatOnly(unittest.TestCase):
    """.githooks/pre-push keys on suborch1/suborch2 to make PR-only-for-code
    STRUCTURAL for the seats. Widening it to the fleet would gate orchestrator/specialist/recovery
    main pushes — including recovery's RECOVERY pushes — and brick the fleet's ability
    to repair itself. This is the one narrow list whose flattening is
    unrecoverable from inside the repo.

    WHY THIS ASSERTS A SET AND NOT SUBSTRINGS. The first version of this guard
    did `assertIn("suborch1|suborch2) IS_SEAT=1 ;;")` plus
    `assertNotIn(f"|{account})")`, and it PASSED against the very flattening it
    exists to prevent. Both checks survive PREPENDING to the arm: the seats stay
    a contiguous tail so the assertIn still matches, and `|claude)` only appears
    when that account is the LAST alternative before the paren — a widened arm
    contains `|claude|`, `|claude-research|`, `|exocortex|`, none of which match.
    A guard that cannot distinguish the healthy state from the unrecoverable one
    is not an assertion (Amendment H2), and it is worse than an ordinary gap
    because the test's stated purpose was to prevent that exact case.

    Set equality is prepend-proof, append-proof and replace-proof in one
    assertion. It is the shape _AGENT_HOME_PREFIXES above already uses; this
    guard now copies it instead of inventing a weaker one.
    """

    def _hook_text(self) -> str:
        path = Path(os.environ.get(_HOOK_PATH_ENV) or (REPO_ROOT / ".githooks" / "pre-push"))
        return path.read_text(encoding="utf-8")

    def test_only_the_sub_orchestrator_accounts_are_gated(self) -> None:
        self.assertEqual(
            _seat_gate_alternatives(self._hook_text()),
            {"suborch1", "suborch2"},
            "the pre-push seat gate no longer matches EXACTLY suborch1|suborch2. "
            "Adding an account here gates that account's own pushes to main — for "
            "orchestrator/specialist/recovery that includes recovery pushes, and the fleet cannot "
            "un-brick itself from inside the repo. Removing one silently retires "
            "PR-only-for-code for that seat. Either way: ruling first, then this test.",
        )

    # A synthetic hook body: `%s` is everything between `case ... in` and `esac`,
    # so a shape can rewrite the catch-all and not just the seat arm.
    _SYNTHETIC = "SEAT_USER=\"$(id -un)\"\ncase \"${SEAT_USER}\" in\n%s\nesac\n"
    _REAL_ARM = "    suborch1|suborch2) IS_SEAT=1 ;;"
    _CATCH_ALL = "    *) IS_SEAT=0 ;;"

    def test_the_parser_reads_a_healthy_synthetic_hook_correctly(self) -> None:
        """Positive control for the shapes below. Without it, a parser that
        returned garbage for EVERYTHING would pass every assertNotEqual and look
        like a working guard."""
        healthy = self._SYNTHETIC % (self._REAL_ARM + "\n" + self._CATCH_ALL)
        self.assertEqual(_seat_gate_alternatives(healthy), {"suborch1", "suborch2"})

    def test_the_parser_can_actually_see_a_widening(self) -> None:
        """H2 on the PARSER itself: the guard above is only as good as this
        function's ability to report a widened arm. Each shape below is a real
        way the flattening would be written — the question asked of every one is
        "is this how somebody would actually widen a shell case?", not "does my
        regex handle this?".

        The first three shapes are IN-PLACE edits of the seat arm, and the
        substring guard that preceded this parser was blind to two of them. The
        rest are the shapes THIS parser was blind to, and they are the more
        likely ones: a `case` is a list, and the natural way to grant another
        account is to add a line, not to retype an existing one. All were
        measured against copies of the live hook through the env seam below —
        each left the guard reporting 3 passed while the block gated accounts it
        must never gate.
        """
        shapes = {
            # --- in-place edits of the seat arm (pre-existing coverage) ---
            "prepend": "    claude|claude-research|suborch1|suborch2) IS_SEAT=1 ;;\n"
            + self._CATCH_ALL,
            "append": "    suborch1|suborch2|claude|exocortex) IS_SEAT=1 ;;\n"
            + self._CATCH_ALL,
            "replace": "    claude|claude-research|claude-fallback) IS_SEAT=1 ;;\n"
            + self._CATCH_ALL,
            # --- added arms: invisible to a first-match-only parser ---
            # The orchestrator/specialist/recovery accounts, granted by a SECOND arm below the
            # real one. This is the shape that matters most: it gates recovery's
            # recovery pushes, and it is what a well-meaning "seats and the
            # orchestrators should follow the same rule" edit looks like.
            "second arm below": self._REAL_ARM
            + "\n    claude|claude-research|claude-fallback) IS_SEAT=1 ;;\n"
            + self._CATCH_ALL,
            # Same widening, written above the real arm — order in a `case` is
            # first-match-wins, so an author adding a more specific rule puts it
            # on top, and a parser keying on the first match then reads the NEW
            # arm and misses the real one in the opposite direction.
            "arm above": "    claude|exocortex) IS_SEAT=1 ;;\n"
            + self._REAL_ARM
            + "\n"
            + self._CATCH_ALL,
            # One account, no pipes at all — the smallest possible widening, and
            # the one least likely to look like a policy change in review.
            "single-account arm": self._REAL_ARM
            + "\n    claude-fallback) IS_SEAT=1 ;;\n"
            + self._CATCH_ALL,
            # The catch-all flipped: no arm added, no name typed, one character
            # changed, and EVERY account on the host is gated. `*` had to enter
            # the alternatives character class for this to be expressible at all.
            "catch-all flipped": self._REAL_ARM + "\n    *) IS_SEAT=1 ;;",
        }
        for shape, body in shapes.items():
            with self.subTest(shape=shape):
                self.assertNotEqual(
                    _seat_gate_alternatives(self._SYNTHETIC % body),
                    {"suborch1", "suborch2"},
                    f"the parser reports the {shape} widening as unchanged",
                )

    def test_a_missing_gate_fails_loudly_rather_than_passing(self) -> None:
        """A guard that silently no-ops when its target disappears is the
        skip-shaped hollow guard from H1's worked instance.

        Two ways to lose the gate, and the union parser must shout for both: the
        arm deleted while the case block survives (arms parse, none set 1), and
        the block gone entirely (nothing parses at all). Returning an empty set
        for either would pass the equality assertion's blame to the caller."""
        for shape, text in {
            "arm deleted, case block intact":
                "case \"${SEAT_USER}\" in\n    *) IS_SEAT=0 ;;\nesac\n",
            "gate gone entirely": "echo 'no seat gate here'\n",
        }.items():
            with self.subTest(shape=shape):
                with self.assertRaises(AssertionError):
                    _seat_gate_alternatives(text)


if __name__ == "__main__":
    unittest.main()
