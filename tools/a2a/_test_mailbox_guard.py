"""Structural test-isolation guard for the a2a test suite (T-1941).

Root cause of the containment defect this closes: `tools/test_a2a_orchestrator_
client.py`'s `_LiveServerTestCase.setUpClass` called `specialist_seat_server.
build_app()` with no `request_inbox`/`result_inbox` override, so it
defaulted to `DEFAULT_REQUEST_INBOX`/`DEFAULT_RESULT_INBOX` — the REAL
`/home/the-ancestor/mailbox/specialist`(-a2a) paths. BT-1..4's stub handler
never called `mailbox.send()` at all, so that default was previously
inert; BT-5's real `ResearchRequestExecutor._resume()` genuinely stages
on approval, so two above-bar-then-resume tests each fired one real
`mailbox.send()` against the live specialist inbox before the defect was
caught. Per-file discipline ("I passed a temp dir this time") is not
enough — a future a2a test file can reintroduce the identical shape by
simply forgetting the override. This module makes the mistake IMPOSSIBLE
to make silently: `guard_live_mailbox()` monkeypatches `tools.mailbox.
send`/`consume` (module-attribute patch, so it protects every caller that
does `mailbox.send(...)`/`mailbox.consume(...)` — including
`specialist_seat_server.py`'s own production call sites, since attribute
lookup happens at call time) to raise immediately if the resolved inbox
path falls under `/home/the-ancestor` (the shared mailbox substrate root
— see tools/mailbox.py's own docstring for the `/home/the-ancestor/
mailbox/<agent>` layout), for the lifetime of the `with` block.

Every a2a `_LiveServerTestCase`-shaped base class (see the three existing
a2a test modules) should wrap its `setUpClass`/`tearDownClass` span in
this guard. `tools/test_a2a_mailbox_isolation_guard.py` is the guard test
proving this module itself raises correctly, plus a source-level scan
that every `build_app(` call site in the a2a test suite passes an
explicit override (catches the exact historical defect shape at
collection time, before a test even runs).

Deliberately does NOT modify `tools/mailbox.py` (out of this ticket's
file scope, and mailbox.py itself has no reason to know it's under test).
"""

from __future__ import annotations

import contextlib
from unittest import mock

from tools import mailbox

# The shared mailbox substrate root (tools/mailbox.py docstring: each agent
# owns `/home/the-ancestor/mailbox/<agent>/{tmp,new,cur}`). ANY path under
# this root is a live sibling's real inbox — never a legitimate test target.
FORBIDDEN_PREFIX = "/home/the-ancestor"


class LiveMailboxGuardError(AssertionError):
    """Raised when a test attempts to send/consume against a live mailbox
    path. An AssertionError subclass so it surfaces as a normal test
    failure, not an opaque crash."""


def _assert_safe(path) -> None:
    resolved = str(path)
    if resolved.startswith(FORBIDDEN_PREFIX):
        raise LiveMailboxGuardError(
            f"a2a test attempted to touch a LIVE mailbox path {resolved!r} "
            f"— every a2a test must pass an explicit temp-maildir "
            f"request_inbox/result_inbox override (T-1941 containment "
            f"defect shape; see this module's docstring)."
        )


@contextlib.contextmanager
def guard_live_mailbox():
    """Monkeypatches `tools.mailbox.send`/`consume` so any call whose
    target inbox resolves under `/home/the-ancestor` raises
    `LiveMailboxGuardError` immediately, for the duration of the `with`
    block. Wrap a test class's live-server lifetime in this."""
    real_send = mailbox.send
    real_consume = mailbox.consume

    def _guarded_send(recipient_inbox, *args, **kwargs):
        _assert_safe(recipient_inbox)
        return real_send(recipient_inbox, *args, **kwargs)

    def _guarded_consume(inbox, *args, **kwargs):
        _assert_safe(inbox)
        return real_consume(inbox, *args, **kwargs)

    with mock.patch.object(mailbox, "send", _guarded_send), mock.patch.object(
        mailbox, "consume", _guarded_consume
    ):
        yield
