"""Substrate-tier ordering primitive PICK for C3 (T-3294 DECISIONS).

PICK: option (ii), a library-level readiness gate every ExecStart
wrapper (and every other invocation surface) checks -- NOT option (i), a
``pantheon-germinate.target`` systemd unit that engine-consuming units
``After=``/``Requires=``.

Why (i) is rejected, with a real citation rather than a re-derivation:
adr/ORG-080-germination-engine.md L343-347 (``[Documentary -- informs
C3]``) already ran this exact mechanism during the ADR's own v8->v9
review fold and found it structurally broken -- a system-manager
``pantheon-germinate.target`` cannot order user-manager component units
via ``After=``/``Requires=``, because ``systemctl --system show``
inspects the system manager and a user-manager ``After=`` resolves
against a same-manager target that never exists there. The ADR's own
conclusion is exactly option (ii)'s shape: enforcement has to live "at
the germinate-command tier ... because every scope invokes" it. This
ref-impl applies that already-settled provider fact (D11: read the
authoritative source, don't re-litigate a settled finding) rather than
re-proving it -- doing so empirically would need a root-capable fixture
that construct the real system/user-manager boundary, which
``harness.target_fixture.install_test_target()`` deliberately does not
provide (T-3290 DECISIONS 7, user-scope-only, no root).

What this module provides for option (ii): the reusable "gate prelude"
every ExecStart-invoked script (or any other invocation surface) MUST
prepend before running any engine-internal code, plus a structural lint
helper proving a given script's source actually carries it -- mirroring
the C4-4 no-swallow grep-lint pattern already established in the
shape charter README for "closed at the enforcement tier below where
bypass can occur" being checkable, not merely asserted.
"""

from __future__ import annotations

#: The single statement every invocation-surface script MUST execute
#: before any engine-internal mutation-tier code runs. Import path is
#: absolute (not relative) because this string is interpolated into a
#: standalone script's source, not this package's own module body.
_GATE_CALL = (
    "from tools.germinate.planner_sandbox.postboot import enforcement as "
    "_postboot_enforcement; _postboot_enforcement.require_or_refuse()"
)


def gate_prelude_source() -> str:
    """Return the mandatory prelude source every ExecStart-invoked script
    (or any other invocation surface bypassing the CLI) must prepend.

    This is the substrate-tier ordering primitive in library-gate form:
    instead of expressing "postboot before components" as a systemd
    ordering edge (rejected, see module docstring), it's expressed as a
    mandatory code-level precondition every surface's entry point must
    execute first.
    """

    return _GATE_CALL


def source_is_gated(script_source: str) -> bool:
    """Structural check: does ``script_source`` call the postboot gate?

    A CI-time lint over every real ExecStart script (production, not
    this ref-impl) would use this to refuse any component unit whose
    entry point doesn't carry the gate call -- proving closure "at the
    enforcement tier below where bypass can occur" is checkable, the
    same shape as C4-4's ``except PrimitiveUnavailable`` no-swallow
    sweep.

    Deliberately checked by substring rather than by requiring the exact
    ``gate_prelude_source()`` alias -- a real ExecStart script may import
    the postboot package under any local name; what matters is that
    *some* import of the postboot package plus a call to
    ``require_or_refuse()`` both appear, not that it's spelled exactly
    like this module's own reference prelude.
    """

    return "postboot" in script_source and "require_or_refuse()" in script_source
