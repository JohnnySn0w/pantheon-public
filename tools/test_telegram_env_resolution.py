"""T-4131: fleet Telegram alerts must not be silently skipped by a credential pointer.

Eight systemd units pinned `PANTHEON_TELEGRAM_ENV=%h/.claude/channels/telegram/pantheon.env`.
On the claude account that file does not exist (only `.env` does), and the tools only
fall back from pantheon.env to .env when the variable is UNSET, so the explicit pin
defeated the fallback and every digest logged "no Telegram token ...; skipping send".

Two guards:
  1. No unit template may pin PANTHEON_TELEGRAM_ENV. The code default is the single
     resolution path (pantheon.env if present, else .env); a per-account separate bot is
     still expressed by creating pantheon.env on that account, never by a unit pin.
  2. Every module that owns a `_default_telegram_env()` resolves to a present `.env`
     when pantheon.env is absent, and prefers pantheon.env when it is present.

The explicit-pointer-to-a-missing-file => no-send semantics are deliberately KEPT:
tools/conftest.py and test_ticket_grooming use exactly that to disable live sends,
including in tool subprocesses that inherit the variable.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

SYSTEMD_DIR = Path(__file__).resolve().parent / "systemd"

_PIN_RE = re.compile(r"^\s*Environment\s*=.*\bPANTHEON_TELEGRAM_ENV\s*=", re.MULTILINE)

RESOLVING_MODULES = (
    "tools.ticket_lint",
    "tools.ticket_grooming",
    "tools.mirror_drift_sweep",
    "tools.watch_digest",
)


def _unit_files() -> list[Path]:
    return sorted(p for p in SYSTEMD_DIR.iterdir() if p.suffix in (".service", ".timer"))


def test_systemd_dir_has_units():
    # Guard the guard: an empty glob would make the pin check vacuous.
    assert len(_unit_files()) > 20


@pytest.mark.parametrize("unit", _unit_files(), ids=lambda p: p.name)
def test_no_unit_pins_telegram_env(unit: Path):
    text = unit.read_text(encoding="utf-8")
    assert not _PIN_RE.search(text), (
        f"{unit.name} pins PANTHEON_TELEGRAM_ENV. An explicit pointer bypasses the "
        "pantheon.env -> .env fallback, so an absent file silently skips every send "
        "(T-4131). Drop the Environment= line and let the tool's default resolve it."
    )


def _write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"TELEGRAM_BOT_TOKEN={token}\n", encoding="utf-8")


@pytest.mark.parametrize("modname", RESOLVING_MODULES)
def test_default_resolves_present_dotenv_when_pantheon_env_absent(modname, tmp_path, monkeypatch):
    mod = importlib.import_module(modname)
    from tools.watch_digest import read_telegram_token

    monkeypatch.setenv("HOME", str(tmp_path))
    chan = tmp_path / ".claude" / "channels" / "telegram"
    _write_token(chan / ".env", "dotenv-token")

    resolved = mod._default_telegram_env()
    assert resolved == chan / ".env"
    assert read_telegram_token(resolved) == "dotenv-token"


@pytest.mark.parametrize("modname", RESOLVING_MODULES)
def test_default_prefers_pantheon_env_when_present(modname, tmp_path, monkeypatch):
    mod = importlib.import_module(modname)
    from tools.watch_digest import read_telegram_token

    monkeypatch.setenv("HOME", str(tmp_path))
    chan = tmp_path / ".claude" / "channels" / "telegram"
    _write_token(chan / ".env", "dotenv-token")
    _write_token(chan / "pantheon.env", "pantheon-token")

    resolved = mod._default_telegram_env()
    assert resolved == chan / "pantheon.env"
    assert read_telegram_token(resolved) == "pantheon-token"
