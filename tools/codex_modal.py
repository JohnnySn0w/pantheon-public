"""tools/codex_modal.py -- shared codex first-run-modal detection + precise
dismissal (T-2594).

Single audited source of the "never blind-sends a keystroke into a live
pane" navigation logic. Extracted from
``tools.bringup_acceptance.harness_codex_ready`` (T-2572), which built and
live-verified it against real codex-cli pane text; that module and
``tools.runtime_controller`` (the real bringup path) both import from here
rather than carrying two drifting copies.

Deliberately a dependency-free leaf: no import of ``runtime_controller`` or
``tools.bringup_acceptance`` (either edge would be a layering inversion --
production runtime code depending on the acceptance-test scaffolding
package, or this leaf depending on the thing it's shared *by*). Each caller
translates :class:`ModalNavigationFailed` into its own pre-existing error
contract at its own boundary (``HardStop`` for the smoke,
``RuntimeInputReadiness`` for the real controller) -- this module knows
about neither.

Two modals are handled here: the update-available picker (above) and the
model-migration prompt (T-2599, ``codex-rs/tui/src/model_migration.rs``) --
a genuinely blocking full-screen "switch model" modal with NO config-lever
for the currently-active live presets (T-2597 wedge-map wedge #4:
``gpt-5.4``/``gpt-5.4-mini`` have no ``hide_*_migration_prompt`` key in the
pinned 0.146.0 build; see ``harness_adapter.codex.ensure_model_migration_
prompts_hidden``'s own "KNOWN RESIDUAL GAP" docstring). Only the keystroke
fallback can clear it. The first-run trust-directory modal is a SEPARATE,
lower-risk fix (config, not keystrokes) -- see
``tools.harness_adapter.codex.ensure_project_trusted``.

Model-migration modal has FOUR render shapes (source: ``model_migration.rs``
``migration_copy_for_models``/``ModelMigrationScreen::render_ref``), gated by
two independent bools:

* ``migration_markdown`` (server-provided, per-preset) present -> heading and
  content are EMPTY; only the dynamic markdown body renders. Both live
  presets active on this host as of 2026-08-08 (``gpt-5.4``/``gpt-5.4-mini``)
  carry ``migration_markdown`` -- so the compiled heading string below does
  NOT appear in today's actual render.
* ``can_opt_out`` (``= current_preset.is_some()``, i.e. the FROM-model is
  still itself a listed preset) -- currently true for both live presets, so
  the opt-out menu (``"Choose how you'd like Codex to proceed."`` + Try/Use
  existing) renders regardless of markdown/non-markdown.

Needle is an OR of the two compiled-constant substrings that survive across
markdown/non-markdown: the heading prefix (non-markdown shapes only) and the
opt-out menu prompt (both markdown shapes, when opt-out is available -- true
for both live presets today). The one shape with NO fixed text at all
(markdown body + NOT opt-out, i.e. the FROM-model has been fully retired
from the catalog) is a documented, unfixed residual gap -- mirrors the
T-2597 wedge-map's own wedge #5/#6 framing (dynamic content, no grep-able
needle exists to write).

Dismissal is always a single Enter, never navigation: source-confirmed both
render shapes accept on a fresh-render Enter (``ModelMigrationScreen::
confirm_selection``/``handle_key`` -- the non-opt-out path accepts
unconditionally on Enter/Esc; the opt-out path defaults
``highlighted_option = TryNewModel``, which Enter also accepts). Still
never blind-sent: every Enter is preceded by a fresh capture confirming the
needle is actually present, bounded the same way the update modal's
navigation is.
"""

from __future__ import annotations

import re
from typing import Callable

MAX_MODAL_NAV_ATTEMPTS = 4
MODAL_NAV_POLL_S = 0.3

_UPDATE_MODAL_NEEDLES = ("Update available!", "Skip until next version")

_SKIP_UNTIL_NEXT_VERSION_LINE = re.compile(r"^\s*›\s*3\.\s*Skip until next version\s*$")


class ModalNavigationFailed(RuntimeError):
    """The bounded cursor-navigation loop never confirmed the cursor landed
    on "Skip until next version" -- never a soft failure a caller can paper
    over by guessing and sending Enter anyway."""


def is_codex_update_modal(pane_text: str) -> bool:
    """True iff ``pane_text`` is codex's interactive update-available picker.

    NOT the compact post-dismissal notice -- that lacks "Skip until next
    version" entirely, so this stays False for it and callers fall through
    to their own ``ready_for_submission`` check instead.
    """
    return all(needle in pane_text for needle in _UPDATE_MODAL_NEEDLES)


def cursor_on_skip_until_next_version(pane_text: str) -> bool:
    """True iff the modal's cursor glyph currently sits on option 3."""
    return any(
        _SKIP_UNTIL_NEXT_VERSION_LINE.match(line) for line in pane_text.splitlines()
    )


def dismiss_codex_update_modal(
    *,
    capture: Callable[[], str],
    send_keys: Callable[[str], None],
    sleep: Callable[[float], None],
    max_attempts: int = MAX_MODAL_NAV_ATTEMPTS,
    poll_interval_s: float = MODAL_NAV_POLL_S,
) -> None:
    """Navigate to "Skip until next version" and confirm it -- NEVER "Update
    now" (which would run ``npm install``).

    Precision is load-bearing: every Enter is preceded by a fresh capture
    confirming the cursor is actually on "Skip until next version" -- this
    never blind-sends a fixed key sequence. Bounded to ``max_attempts`` Down
    presses so an unrecognized render fails loudly instead of spinning
    forever.
    """
    for _ in range(max_attempts):
        if cursor_on_skip_until_next_version(capture()):
            send_keys("Enter")
            return
        send_keys("Down")
        sleep(poll_interval_s)
    raise ModalNavigationFailed(
        "codex update-modal dismissal: cursor never reached 'Skip until next "
        f"version' within {max_attempts} Down presses"
    )


# T-2599: the two compiled-constant substrings that survive across the
# model-migration modal's markdown/non-markdown render split (see module
# docstring). Neither alone covers every shape (the heading is absent when
# migration_markdown is set; the menu prompt is absent when the FROM-model
# has no opt-out) -- OR'd together they cover 3 of the 4 known shapes.
_MODEL_MIGRATION_MODAL_NEEDLES = (
    "Codex just got an upgrade. Introducing",
    "Choose how you'd like Codex to proceed.",
)


def is_codex_model_migration_modal(pane_text: str) -> bool:
    """True iff ``pane_text`` is (one of the detectable shapes of) codex's
    blocking model-migration modal.

    NOT exhaustive by construction: the markdown-body + no-opt-out shape has
    no compiled-constant text at all (see module docstring) and can never
    match here -- a documented gap, not a bug.
    """
    return any(needle in pane_text for needle in _MODEL_MIGRATION_MODAL_NEEDLES)


def dismiss_codex_model_migration_modal(
    *,
    capture: Callable[[], str],
    send_keys: Callable[[str], None],
    sleep: Callable[[float], None],
    max_attempts: int = MAX_MODAL_NAV_ATTEMPTS,
    poll_interval_s: float = MODAL_NAV_POLL_S,
) -> None:
    """Confirm the model-migration modal is actually on screen, then send a
    single Enter -- never navigation (source-confirmed a fresh-render Enter
    accepts on both detectable render shapes; see module docstring).

    Still never blind-sends: each attempt re-captures and re-confirms the
    needle before the Enter fires. Bounded to ``max_attempts`` capture
    attempts so an unrecognized render (or the modal never actually
    appearing) fails loudly instead of spinning or guessing.
    """
    for _ in range(max_attempts):
        if is_codex_model_migration_modal(capture()):
            send_keys("Enter")
            return
        sleep(poll_interval_s)
    raise ModalNavigationFailed(
        "codex model-migration-modal dismissal: needle never confirmed "
        f"present within {max_attempts} capture attempts"
    )


__all__ = [
    "MAX_MODAL_NAV_ATTEMPTS",
    "MODAL_NAV_POLL_S",
    "ModalNavigationFailed",
    "is_codex_update_modal",
    "cursor_on_skip_until_next_version",
    "dismiss_codex_update_modal",
    "is_codex_model_migration_modal",
    "dismiss_codex_model_migration_modal",
]
