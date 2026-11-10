"""Escalating rollback stops: generic engine vocabulary (T-5830 review R2; ORG-080 A-4).

A component's ``rollback_mutation`` raises :class:`EscalatingRollbackStop` when its
undo has ALREADY changed the host and then cannot finish, leaving what it manages
unsupervised (whatever the component manages left with nothing running it). The
engine treats it like any failed
inverse (the op stays outstanding, the run goes ROLLBACK_INCOMPLETE and keeps its
claim, ORG-080 D-Y), and additionally:

* writes a durable marker into the run record, ``rollback_incomplete.escalations``
  (a list of ``{op_id, subject, reason, recipient}``), which ``Engine.status`` shows;
* emits ONE ``escalation.page`` transcript event per (run_id, op_id, phase "rollback"), addressed to
  the component-named ``recipient``. A retried rollback never pages twice.

The engine never names a persona or a component concept: ``recipient`` and
``subject`` come from the component (component concepts stay in the component,
ORG-080 A-4). The page is a typed, durable record only; delivering it is a future
live handler's job (nothing here does I/O beyond the run's own files).

:class:`EscalatingHalt` (T-5858) is the forward-path twin: a handler raises it when the
halt itself leaves the subject unsupervised; the engine writes ``halted.escalations``
and pages once, naming the rollback command (see its docstring).
"""

from __future__ import annotations

from tools.persona_installer import OperationFailed


class EscalatingRollbackStop(OperationFailed):
    """An undo that changed the host, then stopped, leaving its subject unsupervised."""

    def __init__(self, reason: str, *, recipient: str, subject: str):
        if not (reason and recipient and subject):
            raise ValueError("an escalating rollback stop names its reason, recipient and subject")
        super().__init__(reason, evidence={"recipient": recipient, "subject": subject})
        self.reason = reason
        self.recipient = recipient
        self.subject = subject


class EscalatingHalt(OperationFailed):
    """A FORWARD op failure that leaves the component's subject unsupervised (T-5858).

    Raised by a handler's ``execute`` or ``verify_operation``. The run HALTS exactly as for
    any OperationFailed (claim retained, ``rollback(run_id)`` admissible), and additionally
    the engine writes ``halted.escalations`` (a list of ``{op_id, subject, reason,
    recipient, rollback}``, ``rollback`` being :func:`rollback_command`) into the run record
    WITH the halt, then emits ONE ``escalation.page`` per (run_id, op_id, phase "halt") carrying the same
    ``rollback`` command. ``recipient`` and ``subject`` come from the component; the engine
    names no persona (ORG-080 A-4). The marker is cleared when ``rollback`` starts (the
    halted field is reset), so a completed rollback leaves none; a stopped undo carries
    its own EscalatingRollbackStop marker. Exception (T-5866): when the halted op declared
    ``rollback_on_halt``, an incomplete rollback carries this marker into
    ``rollback_incomplete.escalations`` until nothing is outstanding (never paged again).
    """

    def __init__(self, reason: str, *, recipient: str, subject: str, recipients: tuple[str, ...] = ()):
        if not (reason and recipient and subject):
            raise ValueError("an escalating halt names its reason, recipient and subject")
        if not all(isinstance(r, str) and r for r in recipients):
            raise ValueError("an escalating halt's extra recipients are non-empty names")
        super().__init__(reason, evidence={"recipient": recipient, "subject": subject})
        self.reason = reason
        self.recipient = recipient
        self.subject = subject
        #: T-5868 Phase B: everyone paged for this halt, ``recipient`` first, duplicates dropped
        #: (an owner who is also the primary recipient is paged once).
        self.recipients: tuple[str, ...] = tuple(dict.fromkeys((recipient, *recipients)))


def rollback_command(run_id: str) -> str:
    """The operator command that undoes a halted run (``tools/germinate/engine/cli.py``
    ``rollback --run``; the simulated-host root is the operator's own argument)."""

    return f"python3 -m tools.germinate rollback --run {run_id}"
