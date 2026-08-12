"""test_ancestor_consume.py — the-ancestor mailbox→veneration dispatch (T-0765 glue).

Tests the handler's dispatch + write-back logic with injected resurrect/send seams — no
live mailbox, no resume. Covers: a good resume_request resurrects + delivers the harvested
handoff to the requester; a failed resume sends a failure notice; non-resume + malformed
messages are ignored without dispatching.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ancestor_consume as ac  # noqa: E402
import mailbox  # noqa: E402
import veneration as ven  # noqa: E402


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.staging = self._tmp.name
        self.sent = []  # (inbox, payload, sender)

    def tearDown(self):
        self._tmp.cleanup()

    def _send(self, inbox, payload, sender, **kw):
        self.sent.append((inbox, payload, sender))
        return "msgid"

    def _handler(self, resurrect_fn):
        return ac.make_handler(resurrect_fn=resurrect_fn, send_fn=self._send,
                               staging_dir=self.staging)

    def _msg(self, payload):
        return {"id": "m1", "sender": "recovery", "payload": payload}

    # ----------------------------------------------------------------- success
    def test_successful_resume_delivers_handoff(self):
        def fake_resurrect(agent, dead_jsonl, target):
            Path(target).write_text("# Resurrected Handoff\nnext: finish T-0772")
            return ven.Result("ok", "harvested")
        h = self._handler(fake_resurrect)
        h(self._msg({"type": "resume_request", "agent": "recovery", "dead_jsonl": "/d.jsonl"}))
        self.assertEqual(len(self.sent), 1)
        inbox, payload, sender = self.sent[0]
        self.assertEqual(inbox, "/home/the-ancestor/mailbox/recovery")  # recovery's default reply inbox
        self.assertEqual(payload["type"], ac.HANDOFF_DELIVERY)
        self.assertIn("Resurrected Handoff", payload["handoff_text"])
        self.assertEqual(sender, "ancestor")

    def test_reply_inbox_override(self):
        def fake(agent, dj, t):
            Path(t).write_text("x"); return ven.Result("ok")
        h = self._handler(fake)
        h(self._msg({"type": "resume_request", "agent": "recovery", "dead_jsonl": "/d.jsonl",
                     "reply_inbox": "/custom/inbox"}))
        self.assertEqual(self.sent[0][0], "/custom/inbox")

    # ----------------------------------------------------------------- failure
    def test_failed_resume_sends_failure_notice(self):
        h = self._handler(lambda a, d, t: ven.Result("failed", "resumed session not ready"))
        h(self._msg({"type": "resume_request", "agent": "specialist", "dead_jsonl": "/d.jsonl"}))
        self.assertEqual(len(self.sent), 1)
        inbox, payload, _ = self.sent[0]
        self.assertEqual(inbox, "/home/the-ancestor/mailbox/specialist")
        self.assertEqual(payload["type"], ac.RESUME_FAILURE)
        self.assertIn("not ready", payload["detail"])

    def test_ok_but_staging_unreadable_is_failure(self):
        # resurrect claims ok but wrote nothing readable → failure notice, not a crash
        h = self._handler(lambda a, d, t: ven.Result("ok", "harvested"))  # never writes file
        h(self._msg({"type": "resume_request", "agent": "recovery", "dead_jsonl": "/d.jsonl"}))
        self.assertEqual(self.sent[0][1]["type"], ac.RESUME_FAILURE)
        self.assertIn("unreadable", self.sent[0][1]["detail"])

    # -------------------------------------------------------------- ignored
    def test_non_resume_message_ignored(self):
        called = []
        h = self._handler(lambda *a: called.append(a) or ven.Result("ok"))
        h(self._msg({"type": "handoff_delivery", "agent": "recovery", "handoff_text": "hi"}))
        self.assertEqual(self.sent, [], "must not dispatch a non-resume message")
        self.assertEqual(called, [], "must not resurrect on a non-resume message")

    def test_malformed_request_sends_failure(self):
        # missing dead_jsonl → don't resurrect, but DON'T silently drop: notify the requester.
        called = []
        h = self._handler(lambda *a: called.append(a) or ven.Result("ok"))
        h(self._msg({"type": "resume_request", "agent": "recovery"}))  # missing dead_jsonl
        self.assertEqual(called, [], "malformed request must not resurrect")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["type"], ac.RESUME_FAILURE)
        self.assertIn("malformed", self.sent[0][1]["detail"])

    # -------------------------------------------------------------- security
    def test_traversal_agent_rejected_before_side_effects(self):
        # a crafted agent (path traversal) must be rejected at the boundary — never reach
        # resurrect, and never create the staging file outside staging_dir.
        called = []
        h = self._handler(lambda a, d, t: called.append((a, d, t)) or ven.Result("ok"))
        h(self._msg({"type": "resume_request", "agent": "../../etc/evil",
                     "dead_jsonl": "/d.jsonl", "reply_inbox": "/custom/inbox"}))
        self.assertEqual(called, [], "traversal agent must not reach resurrect")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["type"], ac.RESUME_FAILURE)
        self.assertIn("rejected", self.sent[0][1]["detail"])
        # nothing should have been created up the tree from the staging dir
        self.assertFalse((Path(self.staging).parent / "etc").exists(),
                         "no mkdir/unlink side effect outside staging_dir")

    def test_unsafe_reply_inbox_falls_back_to_trusted(self):
        # an unsafe explicit reply_inbox (traversal) is ignored in favor of the trusted
        # per-agent inbox — send() must never target the attacker-supplied path.
        def fake(agent, dj, t):
            Path(t).write_text("ok handoff"); return ven.Result("ok")
        h = self._handler(fake)
        h(self._msg({"type": "resume_request", "agent": "recovery", "dead_jsonl": "/d.jsonl",
                     "reply_inbox": "/home/the-ancestor/../../etc/cron.d"}))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "/home/the-ancestor/mailbox/recovery",
                         "unsafe reply_inbox must fall back to the trusted derived inbox")

    def test_no_derivable_reply_inbox_drops(self):
        # unknown agent AND unknown sender → can't notify anyone → drop without resurrecting
        called = []
        h = self._handler(lambda *a: called.append(a) or ven.Result("ok"))
        h({"id": "m1", "sender": "stranger",
           "payload": {"type": "resume_request", "agent": "nobody", "dead_jsonl": "/d.jsonl"}})
        self.assertEqual(self.sent, [], "nothing to send when no reply inbox is derivable")
        self.assertEqual(called, [], "must not resurrect when it can't deliver the result")

    def test_stale_staging_not_delivered_as_fresh(self):
        # leftover content from a prior run must be cleared, never delivered as this harvest
        from pathlib import Path
        Path(self.staging, "recovery-HANDOFF.md").write_text("STALE from a previous run")
        h = self._handler(lambda a, d, t: ven.Result("ok", "harvested"))  # writes nothing new
        h(self._msg({"type": "resume_request", "agent": "recovery", "dead_jsonl": "/d.jsonl"}))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["type"], ac.RESUME_FAILURE, "no fresh content → failure")
        self.assertNotIn("STALE", str(self.sent[0][1]), "must not leak prior-run content")


class BogusReplyInboxDlqTest(unittest.TestCase):
    """T-2246: a well-formed-but-never-provisioned `reply_inbox` from an untrusted
    peer must retry-then-DLQ, and must NEVER be created on disk.

    This is the most security-relevant path in the mailbox surface: `reply_inbox`
    arrives inside a payload any peer can write. Before T-2216 a well-formed-but-
    wrong value was silently CREATED and written to — an attacker-or-typo-supplied
    path that provisions itself, and then receives the harvested handoff text.

    T-2216 closed it at the sink (`send()` raises MailboxNotFound unless
    `provision_if_missing=True`), but the end-to-end behaviour rests on EMERGENT
    COMPOSITION: send() raises -> propagates out of handler() -> caught by _drain's
    generic `except Exception` -> retried -> quarantined to failed/. Nothing failed
    if a future refactor wrapped that call site in try/except, changed _drain's
    catch, or reintroduced auto-provisioning. These tests assert it.

    UNLIKE every other test in this module, these use the REAL `mailbox.send` —
    the stubbed `send_fn` the HandlerTest cases inject is exactly what left this
    hole. Every path stays inside a tmpdir: the ancestor inbox, the bogus reply
    target, AND `ac._AGENT_INBOX` (patched, because if the bogus value ever failed
    `is_safe_inbox` the handler would fall back to that map and the real send()
    would write into a REAL agent inbox under /home).

    The two halves are separate test methods on purpose. Half 1 (DLQ) alone would
    still pass in a regression that provisions the box and then fails — half 2 is
    the assertion that actually guards the phantom-mailbox bug.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.staging = str(root / "harvest")
        self.inbox = root / "ancestor-inbox"
        mailbox.provision(self.inbox)
        # Well-formed but never provisioned: absolute, traversal-free, safe charset
        # (passes is_safe_inbox) yet missing tmp//new//cur/ (fails exists()).
        self.phantom_parent = root / "phantom"
        self.bogus = self.phantom_parent / "never-provisioned"
        # SAFETY: never let a fallback reach a real /home inbox. clear=True so the
        # orchestrator -> /home/claude/inbox entry cannot survive.
        patcher = mock.patch.dict(
            ac._AGENT_INBOX,
            {name: str(root / f"fallback-{name}") for name in ("orchestrator", "specialist", "recovery")},
            clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _resurrect_ok(self, agent, dead_jsonl, target):
        # Drive the DELIVERY path (ancestor_consume.py:134) — the dangerous one,
        # where the attacker-supplied inbox would receive the harvested handoff.
        Path(target).write_text("# Harvested\nsecrets from the resumed session\n")
        return ven.Result("ok", "harvested")

    def _send_bogus_request(self) -> str:
        self.assertTrue(mailbox.is_safe_inbox(str(self.bogus)),
                        "fixture precondition: the bogus path must PASS is_safe_inbox, "
                        "else the handler rejects it before send() is ever reached")
        self.assertFalse(mailbox.exists(self.bogus),
                         "fixture precondition: the bogus path must NOT be provisioned")
        return mailbox.send(self.inbox, {
            "type": ac.RESUME_REQUEST, "agent": "recovery", "dead_jsonl": "/d.jsonl",
            "reply_inbox": str(self.bogus),
        }, sender="stranger")

    # THE ATTEMPT BUDGET, DELIBERATELY A LITERAL.
    #
    # DO NOT REPLACE THIS WITH `mailbox._DEFAULT_DLQ_MAX_ATTEMPTS`. It looks like
    # duplication that wants tidying up. It is not: it is the only reason the
    # assertions below are capable of failing at all.
    #
    # THE GENERAL FORM, worth carrying to the rest of this file: AN ASSERTION
    # DERIVED FROM THE THING IT ASSERTS ABOUT CANNOT FAIL. It restates the
    # implementation instead of checking it, and it stays green through any change
    # to that implementation, because the expectation moves in lockstep with the
    # behaviour it is supposed to be pinning.
    #
    # THE MEASUREMENT, which is what makes this a rule rather than a preference:
    # this WAS `range(mailbox._DEFAULT_DLQ_MAX_ATTEMPTS)`, driving the test's loop
    # off the same constant production reads. Setting that constant 3 -> 1 —
    # quarantine on the FIRST failure, retry gone entirely, a real and shippable
    # regression that would cost real mail on any transient upstream outage — left
    # BOTH halves of this class GREEN. The loop bound moved with the mutation, so
    # the test could not see it.
    #
    # That is the trap the T-2252 note in this very file warns about (assertions
    # pinned to a value read from the thing under test, which is why 4266 green
    # tests said nothing), reproduced inside the file that carries the warning.
    # With the literal, a change to the production constant REDDENS here in both
    # directions — verified, not assumed: 3 -> 1 fails, 3 -> 5 fails. Whoever
    # changes the retry budget has to come here and say so. That is the feature.
    DLQ_MAX_ATTEMPTS = 3

    def _drain(self, times: int, **handler_kwargs) -> None:
        for _ in range(times):
            ac.consume_once(inbox=str(self.inbox), resurrect_fn=self._resurrect_ok,
                            staging_dir=self.staging, **handler_kwargs)

    def _drain_to_dlq(self, **handler_kwargs) -> str:
        """Send the hostile request and drain until the DLQ threshold is reached.

        One failure is recorded per drain, so quarantine needs DLQ_MAX_ATTEMPTS
        separate consume passes — the literal above, deliberately not the constant.
        """
        mid = self._send_bogus_request()
        self._drain(self.DLQ_MAX_ATTEMPTS, **handler_kwargs)
        return mid

    def test_bogus_reply_inbox_message_is_dlqd(self):
        """Half 1: it is quarantined, not retried forever and not silently acked."""
        mid = self._drain_to_dlq()
        self.assertTrue((self.inbox / "failed" / mid).exists(),
                        "a resume_request naming an unprovisioned reply_inbox must "
                        "retry-then-DLQ to failed/")
        self.assertFalse((self.inbox / "cur" / mid).exists(),
                         "it must NOT be acked as successfully handled")

    def test_bogus_reply_inbox_is_RETRIED_before_it_is_dlqd(self):
        """The RETRY half of "retry-then-DLQ" — the half nothing was asserting.

        The two tests either side of this one assert the DLQ END STATE, and both
        are mutation-proven. Neither can see the retry: quarantining on the first
        failure reaches the same end state by a different route, so a regression
        that DLQs an innocent message on its FIRST transient failure was invisible.
        Transient failure is the normal case retry exists for — an ACP usage cap, a
        wedged wrapper, a peer that is down for a minute — so this is the path that
        decides whether a recoverable blip costs a message.

        Asserted against the LITERAL budget, not the production constant. Both
        directions redden: lower the constant and the message is already in failed/
        at step 1; raise it and it is still absent at step 3.
        """
        mid = self._send_bogus_request()

        # N-1 drains: failures recorded, budget not yet spent.
        self._drain(self.DLQ_MAX_ATTEMPTS - 1)
        self.assertFalse(
            (self.inbox / "failed" / mid).exists(),
            f"quarantined after only {self.DLQ_MAX_ATTEMPTS - 1} failed drains — "
            f"the budget is {self.DLQ_MAX_ATTEMPTS}, so this message lost retries "
            "it was owed; a transient upstream outage would now cost real mail")
        self.assertTrue(
            (self.inbox / "new" / mid).exists(),
            "a message inside its retry budget must stay in new/ awaiting the next "
            "drain — not acked, not quarantined, not vanished")

        # The Nth drain spends the budget and quarantines.
        self._drain(1)
        self.assertTrue(
            (self.inbox / "failed" / mid).exists(),
            f"still not quarantined after {self.DLQ_MAX_ATTEMPTS} failed drains — "
            "retry without a terminal state is an infinite redelivery loop")

    def test_bogus_reply_inbox_path_is_never_created(self):
        """Half 2: the phantom mailbox is never provisioned. THE load-bearing half."""
        self._drain_to_dlq()
        self.assertFalse(self.bogus.exists(),
                         "an untrusted peer-supplied reply_inbox must never be created "
                         "on disk — that is the pre-T-2216 phantom-mailbox bug")
        self.assertFalse(self.phantom_parent.exists(),
                         "not even the parent: provisioning mkdirs parents, so a "
                         "surviving parent means the box was created and cleaned up")


class InboxMapTest(unittest.TestCase):
    """T-2252: the default-reply-target map, tested as an INVARIANT not as literals.

    The three assertions above used to pin the pre-ORG-026 per-home paths, which
    is why 4266 green tests said nothing while replies went to retired locations.
    Re-pinning fresh literals would reproduce that exactly, so these tests assert
    the PROPERTIES the map must have — hub-rooted, self-named, total over the
    persona registry, and closed — none of which a future wrong literal satisfies.

    ZERO filesystem I/O by construction: os.path.normpath is pure string work, so
    nothing here can reach (or create) a live maildir.
    """

    HUB = "/home/the-ancestor/mailbox"

    def test_every_mapping_is_that_agents_own_hub_subdir(self):
        # The invariant, not the literal: dirname must be the ORG-026 hub AND
        # basename must be the agent's own key. The basename half is what catches
        # a transposition (metis -> .../atlas), which a hub-root-only check passes.
        self.assertTrue(ac._AGENT_INBOX, "map must not be empty")
        for agent, path in ac._AGENT_INBOX.items():
            with self.subTest(agent=agent):
                norm = os.path.normpath(path)
                self.assertEqual(
                    os.path.dirname(norm), self.HUB,
                    f"{agent} reply target {path!r} is not under the ORG-026 hub "
                    f"{self.HUB} — a reply would go to a retired/unwatched location")
                self.assertEqual(
                    os.path.basename(norm), agent,
                    f"{agent} reply target {path!r} names a DIFFERENT agent's box — "
                    "mail would be delivered to the wrong peer")

    def test_every_known_persona_is_mapped_or_exempt(self):
        # Completeness DERIVED from the persona registry, never restated here: a
        # newly registered persona fails this test until someone decides for it.
        # Precedent: test_inbox_consumer.TestDeliveryCoverage
        # ::test_every_known_persona_is_classified (T-2144, same defect class).
        # agent_role_map, NOT mailbox_mcp — mailbox_mcp imports the MCP SDK at
        # module scope and would drag that dependency into this import path.
        from agent_role_map import AGENT_ROLE_MAP
        missing = sorted(set(AGENT_ROLE_MAP) - set(ac._AGENT_INBOX) - set(ac._NO_REPLY_INBOX))
        self.assertEqual(
            missing, [],
            "persona(s) with NO default-reply-target decision: %r. Add to "
            "_AGENT_INBOX (where replies go) or _NO_REPLY_INBOX (why they get "
            "none). Silent absence means the reply is DROPPED with no reason "
            "key — indistinguishable from a healthy no-op (T-2252)." % (missing,))

    def test_no_persona_is_both_mapped_and_exempt(self):
        both = sorted(set(ac._AGENT_INBOX) & set(ac._NO_REPLY_INBOX))
        self.assertEqual(both, [], "contradictory classification: %r" % (both,))

    def test_exemptions_carry_a_reason(self):
        for agent, reason in ac._NO_REPLY_INBOX.items():
            with self.subTest(agent=agent):
                self.assertTrue(str(reason).strip(),
                                f"{agent} has no reply target and no stated reason")

    def test_unknown_agent_names_are_not_derivable(self):
        # SECURITY, load-bearing. handler() resolves reply_inbox as
        #   (explicit if _safe_inbox(explicit) else None) or _AGENT_INBOX.get(agent)
        #                                                or _AGENT_INBOX.get(sender)
        # and BOTH `agent` and `sender` are untrusted payload fields that
        # _safe_inbox never re-checks. The "trusted (constant) per-agent inbox
        # map" guarantee holds ONLY while this map is a CLOSED ENUMERATION. If
        # anyone replaces the literals with str(MAILBOX_HUB / agent), every
        # string becomes derivable, test_no_derivable_reply_inbox_drops loses its
        # subject, and a traversal value becomes trusted-by-construction. This
        # test is the thing that stops that edit.
        for hostile in ("nobody", "stranger", "../../etc/evil", ""):
            with self.subTest(name=hostile):
                self.assertIsNone(
                    ac._AGENT_INBOX.get(hostile),
                    f"{hostile!r} resolved to a reply target — the map must stay a "
                    "closed enumeration, never a computed path")


if __name__ == "__main__":
    unittest.main()
