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
        i.e. every bound account except the codex-harness ones (codex, and
        T-4884's codex-fallback, whose credential is ChatGPT OAuth). The retired
        the-ancestor (T-3421) is no longer in the account map."""
        from tools import credential_expiry_monitor as cem
        from tools.agent_role_map import account_persona_map

        expected = {p for a, p in account_persona_map().items()
                    if a not in ("codex", "codex-fallback")}
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


# Matches ONE `    <a>|<b>|...) IS_SEAT=<n> ;;` case arm. Two characters are inside the
# class ON PURPOSE:
#   `*`  without it a catch-all flipped to `*) IS_SEAT=0` — which RETIRES the gate
#        for every account on the host — is not merely missed, it is unmatchable,
#        and the guard reports the hook as healthy. With `*` in the class the flip
#        surfaces as the alternative "*" and breaks the set equality.
#   `"`  the allow arm is `claude|"") IS_SEAT=0`; the empty-string alternative is
#        the fail-OPEN branch for an unresolvable `id -un`. Without `"` in the
#        class that arm is unmatchable and the guard cannot see the allow set.
_CASE_ARM_RE = re.compile(r'^[ \t]*([\w.|*"-]+)\)[ \t]*IS_SEAT=(\d)\b', re.MULTILINE)

# The two sets the gate is pinned to. The empty-string alternative is spelled `""`
# because that is how it is written in the shell arm.
_EXPECTED_ALLOW = {"claude", '""'}   # HO's account + the fail-open unresolvable-identity branch
_EXPECTED_SEAT = {"*"}               # everything else


def _seat_gate_shape(hook_text: str) -> tuple[set[str], set[str]]:
    """Return (allow, seat): the UNION of shell-case alternatives across EVERY
    `IS_SEAT=0` arm, and across EVERY `IS_SEAT=1` arm.

    Parses each `    <a>|<b>|...) IS_SEAT=<n> ;;` arm and unions the alternatives per
    value. Returning SETS is the entire point — see the guard below for why
    substring checks are not an assertion here.

    WHY EVERY ARM AND NOT THE FIRST. The first version of the (suborch-only)
    parser used `re.search`, i.e. the FIRST matching arm only. A shell `case` is a
    list, and the cheapest way to change one is not to edit the existing arm at
    all — it is to add another arm. Measured, not theorised: appending
    `claude|claude-research|claude-fallback|exocortex) IS_SEAT=1 ;;` below the
    real arm left that guard reporting 3 passed while `claude` and `claude-fallback`
    came out IS_SEAT=1 under bash. Union-across-arms is the only reading that
    matches what the shell actually does with the file.

    KNOWN AND ACCEPTED: unioning across all arms means an arm the shell would
    never reach — inside `if false; then`, or below the `*)` catch-all — still
    counts, producing a false RED. That is deliberate. This parser is a text guard,
    not a shell interpreter, and the failure direction of "shout about an
    unreachable arm" is the safe one; the failure direction of "stay quiet about a
    reachable one" silently un-gates a seat. The live hook has exactly two arms, so
    the cost is zero today. Do not "fix" this by teaching the parser reachability.

    FAIL LOUD, NOT EMPTY. Zero `IS_SEAT=1` arms (the catch-all flipped or deleted)
    or zero `IS_SEAT=0` arms (HO's allow arm deleted, so HO is gated) raises rather
    than returning an empty set. An empty set would still fail the equality
    assertion, but only by accident of what it is compared against; any future
    caller asking "is <account> gated?" would read the silence as a clean answer.
    Both states need a human, and so does a hook with NO recognisable arms at all
    (the same "parser no longer understands this file" verdict).
    """
    arms = _CASE_ARM_RE.findall(hook_text)
    allow_arms = [alts for alts, value in arms if value == "0"]
    seat_arms = [alts for alts, value in arms if value == "1"]
    if not seat_arms:
        raise AssertionError(
            "could not locate any `...) IS_SEAT=1` case arm in the pre-push hook — "
            "the catch-all seat arm was flipped, renamed, restructured or removed, any "
            "of which un-gates every non-HO account and needs a human look rather than "
            "a silently-passing guard"
        )
    if not allow_arms:
        raise AssertionError(
            "could not locate any `...) IS_SEAT=0` case arm in the pre-push hook — "
            "with no allow arm the HO account (claude) is gated too; that, or a "
            "restructured gate, needs a human look"
        )
    union = lambda arm_list: {alt for alts in arm_list for alt in alts.split("|") if alt}  # noqa: E731
    return union(allow_arms), union(seat_arms)


class TestPrePushSeatGateCoversEveryNonHoAccount(unittest.TestCase):
    """.githooks/pre-push keys on the unix user to make PR-only-for-code STRUCTURAL:
    every account EXCEPT the HO's (`claude`) is a seat and may not push code
    (tools/, .github/, .githooks/) straight to main. This is the narrow-list guard,
    INVERTED: the list that must not grow is now the ALLOW arm, and the catch-all
    must stay a seat.

    T-4226 ruling (orchestrator, 2026-09-29): the seat gate enforces the standing rule
    "no code changes land on main outside the PR flow" (CLAUDE.md §Agent branch
    convention; T-0175 branch protection; T-1673 HO-exclusive merge) for EVERY
    non-HO account, superseding T-2256's suborch-only scope.

    WHY T-2256's OBJECTION NO LONGER HOLDS. T-2256 kept this gate at suborch1|
    suborch2 because widening it "would gate orchestrator/specialist/recovery main pushes —
    including recovery's RECOVERY pushes — and brick the fleet's ability to repair
    itself". Three answers:
      (a) Recovery's recovery pushes are not bricked. The hook fails OPEN on every
          anomaly (unreadable range, zero shas, empty ref lines, unresolvable
          identity — the `""` alternative below), the HO account (claude) is never
          gated, and `git push --no-verify` is the documented emergency escape,
          printed in the block message itself.
      (b) The direct-push flow T-2256 protected produced a red main on
          2026-09-28/29 (T-4205) and unreviewed code landing (T-4219, T-4224).
      (c) The recovery path is: Recovery uses --no-verify and says so in its report;
          the server-side direct-code-push audit
          (.github/workflows/direct-code-push-audit.yml) then flags it. That is
          the intended visibility, not a failure.

    WHAT THE GUARD STILL PROTECTS. Failing here does not mean the change is wrong;
    it means the change crosses a boundary somebody recorded a reason for. Two
    directions matter now, and both are asserted:
      - the ALLOW set must stay exactly {claude, ""}: every account added to it
        silently retires PR-only-for-code for that account (the un-gating direction);
      - the SEAT set must stay exactly {*}: replacing the catch-all with an
        enumerated list re-creates the pre-T-4226 hole for every account not named,
        including any account created tomorrow.
    Update this file WITH the ruling that authorized the change.

    WHY THIS ASSERTS SETS AND NOT SUBSTRINGS. The first version of the suborch-only
    guard did `assertIn("suborch1|suborch2) IS_SEAT=1 ;;")` plus
    `assertNotIn(f"|{account})")`, and it PASSED against the very flattening it
    existed to prevent: both checks survive PREPENDING to the arm, because the
    original names stay a contiguous tail and `|claude)` only appears when that
    account is the LAST alternative before the paren — a widened arm contains
    `|claude|`, `|claude-research|`, none of which match. A guard that cannot
    distinguish the healthy state from the broken one is not an assertion
    (Amendment H2). Set equality is prepend-proof, append-proof and replace-proof
    in one assertion.
    """

    def _hook_text(self) -> str:
        path = Path(os.environ.get(_HOOK_PATH_ENV) or (REPO_ROOT / ".githooks" / "pre-push"))
        return path.read_text(encoding="utf-8")

    def test_only_ho_is_exempt_and_the_catch_all_is_a_seat(self) -> None:
        allow, seat = _seat_gate_shape(self._hook_text())
        self.assertEqual(
            allow,
            _EXPECTED_ALLOW,
            "the pre-push allow arm no longer matches EXACTLY claude|\"\". Adding an "
            "account here silently retires PR-only-for-code for it (T-4226 ruling: the "
            "gate covers EVERY non-HO account). Removing `claude` gates HO; removing "
            "`\"\"` makes an unresolvable identity fail closed and can brick pushes. "
            "Either way: ruling first, then this test.",
        )
        self.assertEqual(
            seat,
            _EXPECTED_SEAT,
            "the pre-push seat arm is no longer EXACTLY the catch-all `*`. An enumerated "
            "seat list leaves every unlisted account (including one created tomorrow) "
            "un-gated — the pre-T-4226 hole. Ruling first, then this test.",
        )

    # A synthetic hook body: `%s` is everything between `case ... in` and `esac`,
    # so a shape can rewrite the catch-all and not just the allow arm.
    _SYNTHETIC = "SEAT_USER=\"$(id -un)\"\ncase \"${SEAT_USER}\" in\n%s\nesac\n"
    _REAL_ARM = "    claude|\"\") IS_SEAT=0 ;;"
    _CATCH_ALL = "    *) IS_SEAT=1 ;;"

    def test_the_parser_reads_a_healthy_synthetic_hook_correctly(self) -> None:
        """Positive control for the shapes below. Without it, a parser that
        returned garbage for EVERYTHING would pass every assertNotEqual and look
        like a working guard. Also covers a tab-indented arm."""
        for label, body in {
            "space-indented": self._REAL_ARM + "\n" + self._CATCH_ALL,
            "tab-indented": "\tclaude|\"\") IS_SEAT=0 ;;\n\t*) IS_SEAT=1 ;;",
            "trailing comments": self._REAL_ARM + "  # HO\n" + self._CATCH_ALL + "  # everyone",
        }.items():
            with self.subTest(shape=label):
                self.assertEqual(
                    _seat_gate_shape(self._SYNTHETIC % body),
                    (_EXPECTED_ALLOW, _EXPECTED_SEAT),
                )

    def test_the_live_hook_is_healthy(self) -> None:
        """The live hook, read through the same parser the shapes use — so a green
        run means the parser understood THIS file, not merely that it found nothing."""
        self.assertEqual(_seat_gate_shape(self._hook_text()), (_EXPECTED_ALLOW, _EXPECTED_SEAT))

    def test_the_parser_can_actually_see_a_gate_being_retired(self) -> None:
        """H2 on the PARSER itself: the guard above is only as good as this
        function's ability to report a changed arm. Each shape below is a real way
        the gate would be loosened or narrowed — the question asked of every one is
        "is this how somebody would actually edit a shell case?", not "does my
        regex handle this?".

        In-place edits of the allow arm come first, then the more likely ones: a
        `case` is a list, and the natural way to exempt another account is to add a
        line, not to retype an existing one.
        """
        shapes = {
            # --- in-place edits of the allow arm ---
            "prepend": "    claude-research|claude|\"\") IS_SEAT=0 ;;\n" + self._CATCH_ALL,
            "append": "    claude|\"\"|exocortex) IS_SEAT=0 ;;\n" + self._CATCH_ALL,
            "replace": "    claude|claude-fallback) IS_SEAT=0 ;;\n" + self._CATCH_ALL,
            # --- added arms: invisible to a first-match-only parser ---
            # Recovery, Specialist and Codex exempted by a SECOND arm below the real one: what
            # a well-meaning "these seats need to push their own fixes" edit looks like.
            "second arm below": self._REAL_ARM
            + "\n    claude-fallback|claude-research|codex) IS_SEAT=0 ;;\n"
            + self._CATCH_ALL,
            # Same exemption written above the real arm — order in a `case` is
            # first-match-wins, so an author adding a more specific rule puts it on
            # top, and a parser keying on the first match then reads the NEW arm and
            # misses the real one in the opposite direction.
            "arm above": "    claude-fallback) IS_SEAT=0 ;;\n"
            + self._REAL_ARM
            + "\n"
            + self._CATCH_ALL,
            # One account, no pipes at all — the smallest possible exemption, and
            # the one least likely to look like a policy change in review.
            "single-account allow arm": self._REAL_ARM
            + "\n    codex) IS_SEAT=0 ;;\n"
            + self._CATCH_ALL,
            # --- the seat arm narrowed back to a list ---
            # The pre-T-4226 shape: name the seats, allow everyone else.
            "enumerated seats, catch-all allows": "    suborch1|suborch2) IS_SEAT=1 ;;\n"
            + "    *) IS_SEAT=0 ;;",
            # The catch-all replaced by a list of seats with no catch-all at all: any
            # account not named falls through un-gated.
            "seat list, no catch-all": self._REAL_ARM
            + "\n    suborch1|suborch2|claude-fallback|codex) IS_SEAT=1 ;;",
        }
        for shape, body in shapes.items():
            with self.subTest(shape=shape):
                try:
                    got = _seat_gate_shape(self._SYNTHETIC % body)
                except AssertionError:
                    continue  # the parser shouted: also a detection
                self.assertNotEqual(
                    got,
                    (_EXPECTED_ALLOW, _EXPECTED_SEAT),
                    f"the parser reports the {shape} change as unchanged",
                )

    def test_a_retired_or_missing_gate_fails_loudly_rather_than_passing(self) -> None:
        """A guard that silently no-ops when its target disappears is the
        skip-shaped hollow guard from H1's worked instance.

        Four ways to lose the gate, and the parser must shout for each: the
        catch-all flipped to an allow (ONE character changed, EVERY account
        un-gated), the catch-all deleted, HO's allow arm deleted (the opposite
        direction: HO gated), and the block gone entirely. Returning an empty set
        for any of them would pass the equality assertion's blame to the caller."""
        for shape, text in {
            "catch-all flipped to allow":
                self._SYNTHETIC % (self._REAL_ARM + "\n    *) IS_SEAT=0 ;;"),
            "catch-all deleted, allow arm intact":
                self._SYNTHETIC % self._REAL_ARM,
            "HO allow arm deleted, catch-all intact":
                self._SYNTHETIC % self._CATCH_ALL,
            "gate gone entirely": "echo 'no seat gate here'\n",
        }.items():
            with self.subTest(shape=shape):
                with self.assertRaises(AssertionError):
                    _seat_gate_shape(text)


if __name__ == "__main__":
    unittest.main()
