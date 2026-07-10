"""Suite-wide safety net: no test may fire a real Telegram notification.

Several tools modules read the Telegram bot token from a FIXED file
(`~/.claude/channels/telegram/.env`) via their own module-level `TELEGRAM_ENV`
constant, with no env override. If a test calls a send/notify path without
mocking the transmit primitive — or its patch *misses* because of the
run-as-script (`import inbox_consumer`) vs run-as-package
(`import tools.inbox_consumer`) dual-module-identity problem — the real token
gets read and the operator gets pinged. (That is exactly what spammed the operator during
the T-0856 suite runs.)

This autouse fixture redirects every sender module's `TELEGRAM_ENV` to a
nonexistent path, so `read_telegram_token()` returns None ("NOT sent") and the
real token is never read. Tests that legitimately exercise the send path patch
`TELEGRAM_ENV` themselves *inside* the test body (after this fixture runs), and
that inner patch wins — so they continue to work. (T-0856.)
"""

import pytest

# Canonical (package) identities of the modules that read the real bot token.
# Repo-root is the canonical pytest invocation, where only these package
# identities exist (no bare `inbox_consumer` second identity).
_SENDER_MODULES = (
    "tools.activity_digest",
    "tools.context_fill_monitor",
    "tools.inbox_consumer",
)


@pytest.fixture(autouse=True)
def _neutralize_telegram_token(monkeypatch, tmp_path):
    """Point every sender module's TELEGRAM_ENV at a nonexistent file."""
    nonexistent = tmp_path / "no-telegram-token.env"  # never created
    for mod in _SENDER_MODULES:
        # raising=False: a module may not be importable in a given subset run;
        # skip it rather than error the whole suite.
        try:
            monkeypatch.setattr(f"{mod}.TELEGRAM_ENV", nonexistent, raising=False)
        except (ImportError, AttributeError):
            pass
    # ticket_lint honors this env override for its own token path.
    monkeypatch.setenv("PANTHEON_TELEGRAM_ENV", str(nonexistent))
    yield
