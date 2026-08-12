"""tools/bringup_acceptance/codex_hermetic_home.py -- synthetic-auth seeding
for a truly hermetic/disposable CODEX_HOME (T-2598).

Follow-up from the T-2572/T-2596 harness_codex_ready + harness_codex_rc_ready
pattern: those benches deliberately point CODEX_HOME at the REAL account
home (see ``harness_codex_ready.py``'s "Deliberately the REAL account home"
comment) because a fresh, empty CODEX_HOME wedges at codex's login/auth
onboarding screen -- the same class of first-run wall
``harness_adapter.codex.ensure_project_trusted`` already solves for the
SEPARATE trust-directory modal. This module is the auth-side sibling: it
lets a caller stand up a genuinely disposable CODEX_HOME (no real account,
no real credentials) that still clears the login screen.

Live-verified finding (T-2598 wet-run, codex-cli 0.136.0): codex gates its
login screen on a LOCAL STRUCTURAL check of ``auth.json``, not network
validation. A structurally-valid but entirely FABRICATED JWT ``id_token``
(fake claims, fake signature -- zero real credentials) makes
``codex login status`` print "Logged in using ChatGPT". An empty home
prints "Not logged in"; a non-JWT junk token fails to parse. This is the
exact analog of the claude ``hasCompletedOnboarding`` /
``hasTrustDialogAccepted`` synthetic-flag pattern in
``bugs/harness-swap-bench-second-claude-live-poller-blank-pane.md`` --
satisfy the structural gate with fabricated data rather than a real
credential, so a hermetic bench never needs (or risks leaking) a real
account.

Deliberately reuses ``harness_adapter.codex.ensure_project_trusted`` for the
trust-config half rather than re-implementing config.toml table-writing
here -- single source for that logic, same rationale ``tools/codex_modal.py``
gives for staying a dependency-free leaf on ITS half of first-run-modal
safety (this module is the config-only, no-keystrokes counterpart).

Does NOT touch ``harness_codex_ready.py`` / ``harness_codex_rc_ready.py``'s
existing real-CODEX_HOME default -- this is an additive, ready-to-use
seeding capability, not a rewire of those benches.
"""

from __future__ import annotations

import base64
import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tools.harness_adapter.codex import ensure_project_trusted

# Fabricated, never a real account -- the whole point is a hermetic home
# needs zero real credentials to clear codex's login-screen structural gate.
DEFAULT_SYNTHETIC_ACCOUNT_ID = "synthetic-account-0000"

_AUTH_JSON_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0o600, owner read/write only


def _b64url(data: bytes) -> str:
    """Base64url-encode with padding stripped, matching JWT segment shape."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_synthetic_id_token(account_id: str = DEFAULT_SYNTHETIC_ACCOUNT_ID) -> str:
    """Build a structurally-valid, entirely fabricated 3-part JWT.

    Mirrors the T-2598 wet-run fixture that codex's local structural check
    accepted: header/payload are real JSON, base64url-encoded per spec; the
    signature segment is an arbitrary base64url string codex never verifies
    against a real key (no network validation observed). NEVER a real
    credential -- ``exp`` is a fixed far-future sentinel (2100-01-01) so a
    seeded home never silently "expires" mid-bench-run.
    """
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "sub": "synthetic-sub",
        "email": "bench@example.invalid",
        "exp": 4102444800,  # 2100-01-01T00:00:00Z
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    segments = (
        _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        _b64url(b"synthetic-signature-not-a-real-signature"),
    )
    return ".".join(segments)


def build_synthetic_auth_json(
    *,
    account_id: str = DEFAULT_SYNTHETIC_ACCOUNT_ID,
    now: Callable[[], datetime] | None = None,
) -> dict:
    """Build the full synthetic ``auth.json`` payload (T-2598 wet-run shape).

    ``now`` is an injected clock seam (default real UTC now) so callers --
    including this module's own tests -- never depend on wall-clock time for
    determinism.
    """
    clock = now or (lambda: datetime.now(timezone.utc))
    last_refresh = clock().strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": build_synthetic_id_token(account_id),
            "access_token": "synthetic-access",
            "refresh_token": "synthetic-refresh",
            "account_id": account_id,
        },
        "last_refresh": last_refresh,
    }


def seed_hermetic_codex_home(
    codex_home: Path,
    trusted_cwd: Path,
    *,
    account_id: str = DEFAULT_SYNTHETIC_ACCOUNT_ID,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Seed a disposable ``codex_home`` so it clears BOTH first-run walls.

    Writes the trust-directory config entry for ``trusted_cwd`` (via
    ``ensure_project_trusted`` -- idempotent, never overwrites an existing
    entry) and a synthetic ``auth.json`` (mode 0600, always overwritten --
    unlike the trust half, re-seeding auth is expected to refresh the
    fixture, not preserve a prior one). ``codex_home`` is created if absent.

    Pure filesystem mutation -- never shells to a real codex binary, never
    touches a live pane. The gated live-check
    (``tools/t2598_live_codex_login_check.py``) is the separate, opt-in leg
    that actually proves a seeded home against a real ``codex login
    status`` invocation.
    """
    codex_home.mkdir(parents=True, exist_ok=True)
    ensure_project_trusted(codex_home, trusted_cwd)
    auth_path = codex_home / "auth.json"
    auth = build_synthetic_auth_json(account_id=account_id, now=now)
    auth_path.write_text(json.dumps(auth, indent=2) + "\n", encoding="utf-8")
    auth_path.chmod(_AUTH_JSON_MODE)


__all__ = [
    "DEFAULT_SYNTHETIC_ACCOUNT_ID",
    "build_synthetic_id_token",
    "build_synthetic_auth_json",
    "seed_hermetic_codex_home",
]
