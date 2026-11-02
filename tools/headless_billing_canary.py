#!/usr/bin/env python3
"""Daily headless canary for the subscription-reroute class (T-4634, cluster T-3897).

Reports #99371/#98994 show `claude -p` on a subscription login refused with "Credit balance
is too low": the CLI treated a headless call as API-credit-billed. Pantheon's unattended paths
are headless, so a reroute would fail them silently. The zero-cost `claude -p "/usage"` readout
never reaches inference and so cannot see it. This canary spends one tiny turn a day.

It runs two checks, both in the sanctioned plugin-less shape (`-p` with `--safe-mode`, exempt
from the no-second-claude rule because the Telegram plugin never loads), from a cwd outside the
seat's home:
  1. `/usage` (zero cost) must still say the session runs on the subscription;
  2. one minimal Haiku turn must succeed and answer OK.
It logs only counts, timings and status, never prompt or account content.

Exit codes: 0 ok; 1 billing-reroute signature (credit balance / not on subscription);
2 the turn failed for another reason; 3 the canary could not run at all; 4 the subscription
is explicitly at a 100% session/all-model weekly cap and the inference check is deferred.
The systemd unit treats 4 as a neutral success so cap windows do not page as defects.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

CLAUDE = os.environ.get("CANARY_CLAUDE_BIN", os.path.expanduser("~/.npm-global/bin/claude"))
SAFE = ["--safe-mode", "--tools", "", "--no-session-persistence", "--output-format", "json"]
# Anchored positive phrase plus an explicit negation check: a bare substring test let
# "not using your subscription" pass (T-4634 REBOUND).
ON_SUBSCRIPTION = re.compile(r"\byou are currently using your subscription\b")
NOT_ON_SUBSCRIPTION = re.compile(r"\bnot\b[^.\n]{0,40}\busing your subscription\b")
CAP_EXHAUSTED = re.compile(
    r"^current (?:session|week \(all models\)):\s*100% used\b",
    re.IGNORECASE | re.MULTILINE,
)
REROUTE_MARKERS = ("credit balance", "api usage billing", "insufficient credit")
# Credentials that would make the CLI bill an API key or a third-party provider instead of the
# seat's subscription login. Inheriting them would let the canary pass on the wrong billing path
# (T-4634 REBOUND), so the child runs without them.
BILLING_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
)


def child_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in BILLING_ENV}


def run_claude(prompt: str, *extra: str) -> dict:
    # /tmp explicitly: the cwd must sit outside the seat's home and repo so no project
    # settings or CLAUDE.md load, whatever TMPDIR a seat sets.
    with tempfile.TemporaryDirectory(prefix="pantheon-canary-", dir="/tmp") as cwd:
        r = subprocess.run(
            [CLAUDE, "-p", prompt, *SAFE, *extra],
            capture_output=True, text=True, timeout=180, cwd=cwd, env=child_env(),
        )
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"is_error": True, "result": (r.stdout + r.stderr)[-500:], "_exit": r.returncode}


def classify(usage: dict, turn: dict) -> tuple[int, str]:
    """Decide from the two JSON results. Pure, so the rules are testable without a model."""
    texts = " ".join(str(x.get("result", "")) for x in (usage, turn)).lower()
    if any(m in texts for m in REROUTE_MARKERS):
        return 1, "billing-reroute signature in a result"
    usage_text = str(usage.get("result", "")).lower()
    if (usage.get("is_error") or NOT_ON_SUBSCRIPTION.search(usage_text)
            or not ON_SUBSCRIPTION.search(usage_text)):
        return 1, "/usage does not say the session runs on the subscription"
    if turn.get("api_error_status") == 429 and CAP_EXHAUSTED.search(usage_text):
        return 4, "cap-window: subscription confirmed at 100%; inference billing check deferred"
    if turn.get("is_error") or turn.get("subtype") != "success":
        return 2, f"turn failed: subtype={turn.get('subtype')} api_error_status={turn.get('api_error_status')}"
    if str(turn.get("result", "")).strip().upper().rstrip(".") != "OK":
        return 2, "turn succeeded but did not answer OK"
    return 0, (
        f"ok: on subscription; turn num_turns={turn.get('num_turns')} "
        f"duration_api_ms={turn.get('duration_api_ms')}"
    )


def main() -> int:
    stripped = sorted(k for k in BILLING_ENV if k in os.environ)
    if stripped:  # names only, never values
        print(f"headless-billing-canary: stripped inherited billing env: {', '.join(stripped)}",
              file=sys.stderr)
    try:
        usage = run_claude("/usage")
        turn = run_claude("Reply with exactly: OK", "--model", "haiku")
    except Exception as e:
        print(f"headless-billing-canary: could not run: {e!r}", file=sys.stderr)
        return 3
    code, msg = classify(usage, turn)
    print(f"headless-billing-canary: {msg}", file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
