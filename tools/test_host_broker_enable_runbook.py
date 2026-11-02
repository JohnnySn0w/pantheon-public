#!/usr/bin/env python3
"""Tests for tools/host_broker_install.enable-runbook.md (T-2920 U5, arc T-2652).

The enable runbook is the ONE consolidated checklist orchestrator follows from "all units
merged" to the supervised live first fire. These tests keep it tied to the code
it describes, so it cannot drift silently:

* section 5 is GENERATED VERBATIM from U4a's pinned ``host_broker_rehearsal.GATE_TABLE``
  (``python3 -I tools/test_host_broker_enable_runbook.py --regen`` rewrites it);
* every carry-forward recorded on T-2920 has a ledger row (``REQUIRED_CARRY_FORWARDS``);
* the phases P0-P12 each carry the six required fields; the D3 holds are exactly
  P2, P8 and P9 (P7 is not a hold), and they cover every ``hold`` U1's real plan marks;
* each phase's "plan:" gate list equals the union of U1's ``GATES`` over the real
  plan's entry kinds in that phase;
* the readiness gates, the live-change properties (a)-(j) and the standing notes
  are present; commands are the tools' own CLIs; no operator-batch-shaped value.

The runbook lives beside ``host_broker_install.names.md`` (NOT under
tools/host_control, which ``NAME_SCAN`` reads, so the ratified name table is stable).
"""

from __future__ import annotations

import contextlib
import io
import re
import shlex
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import host_broker_install as H  # noqa: E402
from tools import host_broker_rehearsal as R  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = REPO_ROOT / "tools" / "host_broker_install.enable-runbook.md"
BEGIN = "<!-- BEGIN GENERATED GATE_TABLE -->"
END = "<!-- END GENERATED GATE_TABLE -->"

PHASE_IDS = tuple(f"P{n}" for n in range(13))
FIELDS = ("Preconditions", "Commands", "Expect", "HOLD", "Rollback", "Gates")
HOLD_PHASES = frozenset({"P2", "P8", "P9"})

#: every carry-forward / U5 / runbook-note item on T-2920 (ticket annotations, the
#: reviews and Recovery D15 rulings they cite, and mason's U5 rulings); each needs a row
REQUIRED_CARRY_FORWARDS = (
    *(f"CF-A{n}" for n in range(1, 14)),
    "CF-S1", "CF-S2", "CF-S3", "CF-S4",
    *(f"HO-Q{n}" for n in range(1, 13)), "HO-HOLDS", "HO-BATCH",
    "CF-U1", "CF-U2", "CF-U3", "CF-U4",
    "W-U3B", "W-U3A-ARGV", "W-U3A-TOKEN", "W-U3A-F", "W-TAR", "W-U3C", "W-U3D",
    "W-U4B-DESIGN", "W-SETPRIV", "W-U4B-TMPFS", "W-U4B-OWNER", "W-MOUNT-RULE", "CF-W8", "W-T5703",
    "CF-L1", "CF-L2", "CF-L3", "CF-L4", "CF-L5", "CF-LC", "CF-NOOP",
    "U4-B4", "U4-B5", "U4-B6", "U4-B7", "U4-CF7", "U4-PREREQ", "U4B-Q1", "U4B-Q2", "U4A-CF3",
    "CF-U5-1", "T-5644", "T-5659",
    "U4B-F3", "U4B-NIT1", "U4B-FACTORS", "U4A-WATCH", "U4A-PREA",
    "U4A-VERIFY-ARCHIVE", "U4A-LINT", "U4B-PSHELL", "W-MODE-7022", "W-ACL-EXACT", "U4B-PIN", "U4B-MALFORMED",
    "HO-LOW1B", "W-OBSERVE-CALLER", "CF-HOLD-K",
)

#: Recovery's four live-enable carry-forwards (U5 C2 brief) and the ledger row each tags
RECOVERY_FOUR = {"W1": "W-U4B-TMPFS", "W2": "W-U4B-OWNER", "W3": "W-MOUNT-RULE", "W4": "CF-W8"}
LIVE_CHANGE_PROPS = tuple(f"({c})" for c in "abcdefghijk")

#: the only executables a Commands field may name
ALLOWED_COMMAND_PREFIXES = (
    "python3 -I tools/host_broker_install.py ",
    "python3 -I tools/host_broker_install_apply.py ",
    "python3 -I tools/host_broker_rehearsal.py ",
    "python3 -m tools.harness_adapter.host_mcp_registration ",
    "/usr/bin/env -i LC_ALL=C PATH=/usr/bin:/bin /usr/bin/python3 -I <fw>/tools/host_mcp_adapter/dist/build_runtime.py ",
    "--reuid=nobody ",
    "openat(", "open(",
)
FORBIDDEN_IN_COMMANDS = ("sudo ", "systemctl ", "rm ", "chmod ", "chown ", "install ", "| sh", "bash -c", "curl ",
                         "apt-get ", "useradd ", "userdel ", "dbus-send ")


def render_gate_table(table) -> str:
    """The section-5 block: every GATE_TABLE row, strings VERBATIM."""

    lines = ["| Section | Id | Statement (verbatim from host_broker_rehearsal.GATE_TABLE) |", "|---|---|---|"]
    for section, rows in table.items():
        for key, text in rows.items():
            lines.append(f"| {section} | {key} | {text.replace('|', chr(92) + '|')} |")
    return "\n".join(lines)


PREA_BEGIN = "<!-- BEGIN GENERATED PRE-A -->"
PREA_END = "<!-- END GENERATED PRE-A -->"
#: the U4a PRE-a constants and functions the runbook prose names (each must exist in the code)
PREA_NAMES = ("prea_install_line", "prea_rollback_lines", "PRE_A_INSTALL", "PRE_A_SIMULATE", "PRE_A_PURGE_FILTER",
              "PRE_A_PURGE_SIMULATE", "PRE_A_ROLLBACK", "PRE_A_VERIFY_QUERY", "PRE_A_VERIFY_SENTINEL",
              "PRE_A_WINDOW_NOTE")


def render_prea_block() -> str:
    """The PRE-a install line and rollback block, VERBATIM from U4a's functions."""

    return "\n".join(("```sh", R.prea_install_line(), *R.prea_rollback_lines(), "```"))


def generated_block(text: str, begin: str = BEGIN, end: str = END) -> str:
    head, _, rest = text.partition(begin)
    body, sep, _ = rest.partition(end)
    if not head or not sep:
        raise AssertionError(f"the generated markers {begin!r} are missing")
    return body.strip("\n")


def _replace_block(text: str, begin: str, end: str, body: str) -> str:
    head, _, rest = text.partition(begin)
    _, _, tail = rest.partition(end)
    return f"{head}{begin}\n{body}\n{end}{tail}"


def regenerate(path: Path = RUNBOOK) -> None:
    text = path.read_text(encoding="utf-8")
    text = _replace_block(text, BEGIN, END, render_gate_table(R.GATE_TABLE))
    text = _replace_block(text, PREA_BEGIN, PREA_END, render_prea_block())
    path.write_text(text, encoding="utf-8")


def phase_blocks(text: str) -> dict[str, str]:
    parts = re.split(r"^### (P\d+): .*$", text, flags=re.MULTILINE)
    blocks = {}
    for i in range(1, len(parts), 2):
        body = parts[i + 1].split("\n---\n", 1)[0]
        blocks[parts[i]] = body
    return blocks


def field_line(block: str, name: str) -> str:
    m = re.search(rf"^- \*\*{name}\b[^\n]*?\*\*:?(.*)$", block, flags=re.MULTILINE)
    if m is None:
        raise AssertionError(f"field {name} missing")
    return m.group(1)


def field_text(block: str, name: str) -> str:
    """The field's first line plus its indented sub-bullets."""

    lines = block.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^- \*\*{name}\b", line):
            out = [line]
            for nxt in lines[i + 1:]:
                if nxt.startswith("  "):
                    out.append(nxt)
                else:
                    break
            return "\n".join(out)
    raise AssertionError(f"field {name} missing")


#: the review-round key phrase that each ledger row's "Honoured in" location must contain
#: (a str applies to every location; a dict maps a location to its own phrase). Case-folded.
HONOURED_PHRASES: dict[str, object] = {
    "CF-A1": "reconcile=True", "CF-A2": "BusReachability", "CF-A3": "mailbox hub",
    "CF-A4": "destructive-capable write surface",
    "CF-A5": {"G-MB MB-6": "pantheon-approval", "P4": "Both runtimes are root-owned", "P5": "approver-policy.json",
              "P7": "per-approver bus fragments", "P9": "Restart HostApproval1", "P10": "host_mcp_registration"},
    "CF-A6": {"P9": "startup_record", "section 4": "structuredContent.error"},
    "CF-A7": {"G-MB": "BotFather", "G-PRE": "T-5518", "P0": "SCHEMA_DIGEST", "P5": "HumanIngressRegistration",
              "P8": "ingress", "P9": "egress residual", "P11": "gate 38"},
    "CF-A8": {"G-PRE": "T-5533", "P4": "FRAMEWORK_CODE_ARTIFACTS"},
    "CF-A9": {"P9": "resume_all_human_deliveries", "P11": "close-card"},
    "CF-A10": "runtime_invocation_receipt.py", "CF-A11": "Seat-local residue", "CF-A12": "framework-copy update",
    "CF-A13": "T-5597", "CF-S1": "compensate", "CF-S2": {"P5": "CredentialRef", "P7": "activation file"},
    "CF-S3": "Re-cite before resolve",
    "CF-S4": {"P4": "allowlist edit precedes the framework copy", "P10": "P4 framework copy"},
    "HO-Q1": "hybrid", "HO-Q2": "Recovery D15", "HO-Q3": "C5 is NOT a gate", "HO-Q4": "compensate",
    "HO-Q5": "pantheon-hostmonitor", "HO-Q6": "active", "HO-Q7": "never installed", "HO-Q8": "AF_INET",
    "HO-Q9": "systemd-nspawn", "HO-Q10": "Q10 backups", "HO-Q11": "healthsig", "HO-Q12": "T-2924",
    "HO-HOLDS": {"P2": "HOLD: HOLD", "P7": "P7 is NOT a hold", "P8": "HOLD: HOLD", "P9": "HOLD: HOLD"},
    "HO-BATCH": "ONE message", "CF-U1": "stdout", "CF-U2": "TRACKED files",
    "CF-U3": {"P0": "blocking_preconditions", "P2": "monitor_uids", "P5": "strict schemas"},
    "CF-U4": "PINNED_PYYAML_VERSIONS", "W-U3B": "shadow",
    "W-U3A-ARGV": {"P0": "dpkg", "P4": "build_runtime.py"}, "W-U3A-TOKEN": "O_NOFOLLOW",
    "W-U3A-F": {"P4": "venv and pip children", "section 2 (a)": "cwd"}, "W-TAR": "filter='data'",
    "W-U3C": "TOCTOU", "W-U3D": {"section 2 (g)": "ancestor walk", "section 4": "T-5659"},
    "W-U4B-DESIGN": {"section 2 (e)(h)": "mixin", "section 4": "canary"}, "W-SETPRIV": "--no-new-privs",
    "W-U4B-TMPFS": "cgroup2", "W-U4B-OWNER": "receipt's preimage", "W-MOUNT-RULE": "never self-widened",
    "W-T5703": "Recovery's D15 W3 ruling of 2026-11-01", "CF-W8": "owner_mode", "CF-L1": "PASSED THROUGH", "CF-L2": "openat walk", "CF-L3": "follows symlinks",
    "CF-L4": "rebuilds the plan", "CF-L5": "fresh `--receipt` path", "CF-LC": "LC_ALL=C", "CF-NOOP": "noop receipt",
    "U4-B4": {"section 5": "CF-5:ingress", "P9": "ingress", "P11": "real Telegram tap"}, "U4-B5": "DEGRADED",
    "U4-B6": "acl", "U4-B7": "StartServiceByName", "U4-CF7": "activation",
    "U4-PREREQ": {"section 4": "rollback-prerequisites", "P3": "wheelhouse"}, "U4B-Q1": "plan_sha256",
    "W-OBSERVE-CALLER": "observe()",
    "CF-HOLD-K": {"G-LIVE": "(k)", "section 2 (k)": "SAME receipt", "section 5": "Property (k)"},
    "U4B-Q2": "mountinfo", "U4A-CF3": "PARTIAL",
    "CF-U5-1": {"G-MEMBERSHIP": "key presence", "section 2 (j)": "key presence", "P10": "G-MEMBERSHIP"},
    "T-5644": "T-5644", "T-5659": "T-5659", "U4B-F3": "slot binding", "U4B-NIT1": "inputs file is not attested",
    "U4B-FACTORS": "attestation factors are never relaxed", "U4A-WATCH": "plan-derived installer targets",
    "U4A-PREA": "prea_rollback_lines", "U4A-VERIFY-ARCHIVE": "VERIFIES before archiving", "U4A-LINT": "UNKNOWN",
    "U4B-PSHELL": "P_SHELL", "W-MODE-7022": "0o7022", "W-ACL-EXACT": "exact planned or preimage ACL",
    "U4B-PIN": "pinned", "U4B-MALFORMED": "malformed receipt", "HO-LOW1B": "LOW-1 ruling (b)",
}

#: the four tools whose commands the runbook names, keyed by the command prefix
CLI_TOOLS = {
    "python3 -I tools/host_broker_install.py": "tools.host_broker_install",
    "python3 -I tools/host_broker_install_apply.py": "tools.host_broker_install_apply",
    "python3 -I tools/host_broker_rehearsal.py": "tools.host_broker_rehearsal",
    "python3 -m tools.harness_adapter.host_mcp_registration": "tools.harness_adapter.host_mcp_registration",
    # the Recovery-ruled P4 build line; dist/ is not a package, so the value is the file path
    "/usr/bin/env -i LC_ALL=C PATH=/usr/bin:/bin /usr/bin/python3 -I <fw>/tools/host_mcp_adapter/dist/build_runtime.py":
        "tools/host_mcp_adapter/dist/build_runtime.py",
}
PLACEHOLDER_WORDS = ("TODO", "TBD", "FIXME", "XXX", "<fill", "lorem")


class _Captured(Exception):
    def __init__(self, parser) -> None:
        super().__init__("captured")
        self.parser = parser


def real_parser(module_name: str):
    """The tool's REAL argparse parser, captured at its main()'s parse_args call. Nothing after
    parse_args runs (the patched call raises before any action), so no command executes."""

    import argparse
    import importlib
    import importlib.util
    from unittest import mock

    if module_name.endswith(".py"):
        spec = importlib.util.spec_from_file_location("_runbook_cli_" + Path(module_name).stem,
                                                      REPO_ROOT / module_name)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses resolve the defining module through sys.modules
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
    else:
        module = importlib.import_module(module_name)

    def capture(self, *a, **k):
        raise _Captured(self)

    with mock.patch.object(argparse.ArgumentParser, "parse_args", capture):
        try:
            module.main([])
        except _Captured as got:
            return got.parser
    raise AssertionError(f"{module_name}.main() never reached parse_args")


def runbook_commands(text: str) -> list[tuple[str, str]]:
    """(prefix, command) for every inline-code command of the CLI_TOOLS, anywhere in the runbook. A
    ``a|b`` alternation (``--adapter host|approval``) expands into one command per value."""

    out = []
    for code in re.findall(r"`([^`\n]+)`", text):
        for prefix in CLI_TOOLS:
            if code.startswith(prefix + " "):
                m = re.search(r"(?<=\s)([\w.-]+)\|([\w.-]+)(?=\s|$)", code)
                variants = [code] if m is None else [code[:m.start()] + v + code[m.end():] for v in m.groups()]
                out.extend((prefix, v) for v in variants)
    return out


def _option_strings(parser) -> set[str]:
    return {o for a in parser._actions for o in a.option_strings}


def command_problems(parser, prefix: str, code: str) -> list[str]:
    """Why the tool's REAL parser refuses ``code`` (empty = accepted). LOW-c: every ``--flag``
    token must EXACTLY equal an option string of the parser (or the chosen sub-command's parser),
    so argparse's prefix abbreviation never makes a typo pass."""

    import argparse

    tokens = shlex.split(re.sub(r"<[^>]*>", "PLACEHOLDER", code[len(prefix):]))
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(tokens)
    except SystemExit as exc:
        return [f"argparse refuses it (rc {exc.code})"]
    allowed = _option_strings(parser)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction) and tokens and tokens[0] in action.choices:
            allowed |= _option_strings(action.choices[tokens[0]])
    return [f"{t} is not an exact option string" for t in tokens
            if t.startswith("--") and t.split("=", 1)[0] not in allowed]


#: LOW-b: a claim verb near a NOT-EVIDENCED id is an over-claim unless the sentence negates it
CLAIM_RE = re.compile(r"\b(?:evidences|evidenced|proves|proven)\b", re.IGNORECASE)
NEGATION_RE = re.compile(r"\b(?:not|never|no)\b", re.IGNORECASE)


def overclaims(text: str, not_evidenced) -> list[str]:
    """Sentences OUTSIDE the generated GATE_TABLE block that put a NOT-EVIDENCED gate id in the
    same sentence as evidences/evidenced/proves/proven with no negation (not/NOT/never/no)."""

    head, _, rest = text.partition(BEGIN)
    _, _, tail = rest.partition(END)
    ids = re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, not_evidenced), key=len, reverse=True)) + r")\b")
    out = []
    for line in (head + tail).splitlines():
        for sentence in re.split(r"(?<=[.;!?])\s+", line):
            if ids.search(sentence) and CLAIM_RE.search(sentence) and not NEGATION_RE.search(sentence):
                out.append(sentence.strip())
    return out


def section_text(text: str, n: int) -> str:
    return text.split(f"\n## {n}. ", 1)[1].split("\n## ", 1)[0]


def indented_block(lines: list[str], i: int) -> str:
    indent = len(lines[i]) - len(lines[i].lstrip())
    out = [lines[i]]
    for nxt in lines[i + 1:]:
        if nxt.strip() and len(nxt) - len(nxt.lstrip()) > indent:
            out.append(nxt)
        else:
            break
    return "\n".join(out)


def resolve_location(text: str, blocks: dict[str, str], loc: str) -> str:
    """The runbook text a ledger "Honoured in" pointer names."""

    loc = loc.replace(" (generated block)", "")
    if loc == "header":
        return text.split("\n## 1. ", 1)[0]
    if re.fullmatch(r"P\d+", loc):
        return blocks[loc]
    m = re.fullmatch(r"section (\d) ?((?:\([a-z]\))*)", loc.replace(") (", ")("))
    if m:
        sec = section_text(text, int(m.group(1)))
        props = re.findall(r"\(([a-z])\)", m.group(2))
        if not props:
            return sec
        return "\n".join(line for line in sec.splitlines() for c in props if line.startswith(f"| ({c}) |"))
    gate = loc.split()[-1]  # "G-MB MB-6" -> MB-6
    lines = section_text(text, 1).splitlines()
    for i, line in enumerate(lines):
        if f"**{gate}" in line:
            return indented_block(lines, i)
    raise AssertionError(f"unresolvable Honoured-in pointer: {loc!r}")


def ledger_rows(text: str) -> list[list[str]]:
    ledger = text.split("## 6. Carry-forward ledger", 1)[1]
    rows = []
    for line in ledger.splitlines():
        if line.startswith("| ") and not line.startswith("| Id |") and not line.startswith("|---"):
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
            rows.append(cells)
    return rows


_PLAN = None


def real_plan():
    global _PLAN
    if _PLAN is None:
        from tools import test_host_broker_install as T

        _PLAN = T.plan()[0]
    return _PLAN


class RunbookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = RUNBOOK.read_text(encoding="utf-8")
        cls.blocks = phase_blocks(cls.text)

    # --- section 5: the verbatim gate table ---------------------------------

    def test_not_evidenced_table_equals_the_u4a_constant(self) -> None:
        self.assertEqual(generated_block(self.text), render_gate_table(R.GATE_TABLE),
                         "regenerate with: python3 -I tools/test_host_broker_enable_runbook.py --regen")

    def test_table_covers_every_org052_gate_and_the_live_fire_section(self) -> None:
        block = generated_block(self.text)
        for gate in R.org052_gate_ids():
            self.assertRegex(block, rf"\| {re.escape(gate)} \|")
        for key in R.GATE_TABLE["not_evidenced_left_to_live_fire"]:
            self.assertIn(f"| not_evidenced_left_to_live_fire | {key} |", block)
        self.assertIn("## 5. NOT EVIDENCED by the rehearsal, left to the live supervised fire", self.text)

    def test_render_refuses_a_drifted_table(self) -> None:
        drifted = {k: dict(v) for k, v in R.GATE_TABLE.items()}
        drifted["evidenced"]["E35"] += " (over-claimed)"
        self.assertNotEqual(render_gate_table(drifted), generated_block(self.text))

    # --- carry-forward coverage ------------------------------------------------

    def test_every_required_carry_forward_has_a_ledger_row(self) -> None:
        ledger = self.text.split("## 6. Carry-forward ledger", 1)[1]
        rows = set(re.findall(r"^\| ([A-Z0-9][A-Za-z0-9-]*) \|", ledger, flags=re.MULTILINE))
        missing = [cf for cf in REQUIRED_CARRY_FORWARDS if cf not in rows]
        self.assertEqual(missing, [])
        self.assertEqual(sorted(rows - set(REQUIRED_CARRY_FORWARDS) - {"Id"}), [],
                         "a ledger row without a required id: add it to REQUIRED_CARRY_FORWARDS")

    def test_recoveries_four_are_tagged_on_their_rows(self) -> None:
        ledger = self.text.split("## 6. Carry-forward ledger", 1)[1]
        for label, row_id in RECOVERY_FOUR.items():
            with self.subTest(label=label):
                row = next(line for line in ledger.splitlines() if line.startswith(f"| {row_id} |"))
                self.assertIn(f"({label})", row)
        notes = self.text.split("## 4. Standing notes", 1)[1].split("## 5.", 1)[0]
        self.assertIn("cgroup2", notes)
        self.assertIn("The PRIMARY control is the pinned nspawn argv golden", notes)
        self.assertIn("REAL mountinfo line captured from rehearsal 1", notes)
        # W1 is the tmpfs/cgroup2 residual ONLY: U4b's /dev is tmpfs only (devtmpfs dropped, F1)
        from tools import host_broker_rehearsal_live as L

        self.assertEqual(L.VIRTUAL_EXACT["/dev"], frozenset({"tmpfs"}))
        self.assertIn("`/dev`, which is tmpfs ONLY", notes)
        self.assertEqual({fs for v in L.VIRTUAL_EXACT.values() for fs in v} & {"devtmpfs"}, set())
        # W3's first real widening (Recovery D15, 2026-11-01, T-5703) is recorded with its date and its table
        self.assertIn("first real widening: Recovery's D15 W3 ruling of 2026-11-01", notes)
        self.assertIn("`RULED_MOUNTS`", notes)
        self.assertIn("`--private-network`", notes)
        self.assertEqual(len({(e["mountpoint"], e["fstype"]) for e in L.RULED_MOUNTS}), 6)

    def test_pre_a_block_is_generated_verbatim_from_u4a(self) -> None:
        self.assertEqual(generated_block(self.text, PREA_BEGIN, PREA_END), render_prea_block(),
                         "regenerate with: python3 -I tools/test_host_broker_enable_runbook.py --regen")
        self.assertIn("--no-remove", R.PRE_A_INSTALL)
        self.assertNotIn("--no-remove", R.PRE_A_ROLLBACK)

    def test_pre_a_prose_names_the_real_constants_not_a_stale_shape(self) -> None:
        notes = self.text.split("## 4. Standing notes", 1)[1].split("## 5.", 1)[0]
        for name in PREA_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(R, name), name)
                self.assertRegex(notes, rf"`[^`\n]*\b{name}\b[^`\n]*`")
        rollback = next(line for line in notes.splitlines() if "**PRE-a rollback**" in line)
        for needle in ("SUBSET guard", "OUTSIDE the recorded list", "VERIFIES before archiving",
                       "prerequisites can re-run", "simulation-to-purge window"):
            self.assertIn(needle, rollback)
        self.assertNotIn("EQUAL the recorded new-package list exactly", self.text)
        # the pure mirror agrees with the prose: a removal outside the list refuses, an empty set proceeds
        self.assertEqual(R.prea_rollback_decision("Purg other [1]\n", "pkg\n", "pkg\n"), "refuse")  # a cascade
        self.assertEqual(R.prea_rollback_decision("Purg pkg [1]\n", "pkg\nmore\n", "pkg\n"), "purge")  # subset
        self.assertEqual(R.prea_rollback_decision("", "pkg\n", ""), "skip")  # a failed install: clean rollback
        self.assertEqual(R.prea_rollback_verify("pkg installed\n", "pkg\n"), "refuse-db")  # no dpkg sentinel
        self.assertEqual(R.prea_rollback_verify("dpkg installed\npkg installed\n", "pkg\n"), "refuse-left")
        self.assertEqual(R.prea_rollback_verify("dpkg installed\npkg not-installed\n", "pkg\n"), "archive")

    def test_rehearsal_2_owner_mode_gate(self) -> None:
        gate = next(line for line in self.text.splitlines() if "**G-R2:" in line)
        for needle in ("owner_mode", "REAL binaries", "CF-W8", "T-5673", "D15"):
            self.assertIn(needle, gate)
        self.assertIn("does NOT proceed on rehearsal 1 alone", gate)

    # --- phases -----------------------------------------------------------------

    def test_phases_p0_to_p12_each_carry_the_six_fields(self) -> None:
        self.assertEqual(tuple(self.blocks), PHASE_IDS)
        for pid, block in self.blocks.items():
            for name in FIELDS:
                with self.subTest(phase=pid, field=name):
                    field_line(block, name)

    def test_holds_are_exactly_p2_p8_p9_and_p7_is_not_a_hold(self) -> None:
        holding = {pid for pid, b in self.blocks.items() if re.search(r"^- \*\*HOLD: HOLD", b, flags=re.MULTILINE)}
        self.assertEqual(holding, HOLD_PHASES)
        self.assertIn("P7 is NOT a hold", self.blocks["P7"])

    def test_every_plan_hold_lands_on_a_runbook_hold(self) -> None:
        plan_holds = {e["phase"] for e in real_plan()["entries"] if e.get("hold")}
        self.assertEqual(plan_holds, HOLD_PHASES)

    def test_plan_gate_lists_equal_u1_gates_over_the_real_plan(self) -> None:
        by_phase: dict[str, set[str]] = {}
        for e in real_plan()["entries"]:
            by_phase.setdefault(e["phase"], set()).update(H.GATES[e["kind"]])
        for pid, block in self.blocks.items():
            gates = field_line(block, "Gates")
            m = re.match(r"^ plan: (.*?); observed: ", gates)
            self.assertIsNotNone(m, f"{pid}: the Gates field must read 'plan: ...; observed: ...'")
            listed = set() if m.group(1) == "none" else {g.strip() for g in m.group(1).split(",")}
            with self.subTest(phase=pid):
                self.assertEqual(listed, by_phase.get(pid, set()))

    def test_phase_ids_match_u1_and_u4a(self) -> None:
        self.assertTrue(set(H.PHASES) <= set(PHASE_IDS))
        u4a = {p["id"] for p in R.PHASES}
        for pid in ("P0", "P1", "P6", "P9", "P10", "P11", "P12"):
            self.assertIn(pid, u4a)

    def test_commands_are_the_tools_own_clis(self) -> None:
        for pid, block in self.blocks.items():
            commands = field_text(block, "Commands")
            for code in re.findall(r"`([^`]+)`", commands):
                if " " not in code or code.startswith("<"):
                    continue  # a bare identifier (an entry kind, a module name) or a placeholder
                with self.subTest(phase=pid, command=code):
                    self.assertTrue(code.startswith(ALLOWED_COMMAND_PREFIXES), code)
                    for bad in FORBIDDEN_IN_COMMANDS:
                        self.assertNotIn(bad, f" {code} ")

    def test_every_runbook_command_parses_with_the_tools_real_argparse(self) -> None:
        commands = runbook_commands(self.text)
        self.assertEqual({p for p, _ in commands}, set(CLI_TOOLS), "every tool is named at least once")
        parsers = {prefix: real_parser(mod) for prefix, mod in CLI_TOOLS.items()}
        for prefix, code in commands:
            with self.subTest(command=code):
                self.assertEqual(command_problems(parsers[prefix], prefix, code), [])
        adapters = {c.split("--adapter ", 1)[1].split()[0] for p, c in commands if "build_runtime.py" in p}
        self.assertEqual(adapters, {"host", "approval"}, "host|approval expands into both values")

    def test_mutation_a_build_runtime_typo_is_refused(self) -> None:
        prefix = next(p for p in CLI_TOOLS if p.endswith("build_runtime.py"))
        parser = real_parser(CLI_TOOLS[prefix])
        good = next(c for p, c in runbook_commands(self.text) if p == prefix)
        self.assertEqual(command_problems(parser, prefix, good), [])
        self.assertNotEqual(command_problems(parser, prefix, good.replace("--wheelhouse", "--wheelhous")), [])
        self.assertNotEqual(command_problems(parser, prefix, good.replace("--adapter host", "--adapter hots")), [])
        mutated = self.text.replace("--wheelhouse <W>", "--wheelhous <W>", 1)
        self.assertTrue(any(command_problems(parser, p, c) for p, c in runbook_commands(mutated) if p == prefix))

    def test_mutation_c_an_abbreviated_flag_is_refused(self) -> None:
        prefix = "python3 -I tools/host_broker_install.py"
        parser = real_parser(CLI_TOOLS[prefix])
        good = next(c for p, c in runbook_commands(self.text) if p == prefix and " plan " in f" {c} ")
        self.assertIn("--tracked-files", good)
        bad = good.replace("--tracked-files", "--tracked-file")
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(shlex.split(re.sub(r"<[^>]*>", "PLACEHOLDER", bad[len(prefix):])))  # argparse alone accepts it
        self.assertEqual(command_problems(parser, prefix, bad), ["--tracked-file is not an exact option string"])

    def test_no_prose_overclaims_a_not_evidenced_gate(self) -> None:
        not_evidenced = R.GATE_TABLE["not_evidenced_left_to_live_fire"]
        self.assertEqual(overclaims(self.text, not_evidenced), [])

    def test_mutation_b_an_overclaim_in_p9_is_caught(self) -> None:
        not_evidenced = R.GATE_TABLE["not_evidenced_left_to_live_fire"]
        anchor = "- **HOLD: HOLD** (the plan marks it"
        self.assertIn(anchor, self.text)
        mutated = self.text.replace(anchor, "- Rehearsal 1 evidences E38.\n" + anchor, 1)
        self.assertEqual(overclaims(mutated, not_evidenced), ["- Rehearsal 1 evidences E38."])
        negated = self.text.replace(anchor, "- Rehearsal 1 does not evidence or prove E38; it is never evidenced.\n"
                                    + anchor, 1)
        self.assertEqual(overclaims(negated, not_evidenced), [])
        # E1 must not match E14/E18 (word boundary), and an evidenced gate (E35) is not flagged
        self.assertEqual(overclaims("Rehearsal 1 evidences E35.", not_evidenced), [])

    def test_low_d_p10_phase_bound_and_no_rerehearsal(self) -> None:
        p10 = self.blocks["P10"]
        commands = field_text(p10, "Commands")
        self.assertIn("explicit P4-P6 phase bound", commands)
        self.assertIn("does not re-stop at the P2, P8 and P9 holds", commands)
        self.assertIn("only in the `_CLAUDE_MCP_ALLOWLIST` persona sets", p10)
        self.assertIn("ACCEPTED, mason ruling", p10)
        self.assertIn("P10's own gate-32a check and the broker and approval health checks are its evidence", p10)
        k = next(line for line in section_text(self.text, 2).splitlines() if line.startswith("| (k) |"))
        self.assertIn("The explicit phase bound is part of (k)", k)

    def test_the_parse_check_refuses_a_bogus_verb_and_flag(self) -> None:
        parser = real_parser(CLI_TOOLS["python3 -I tools/host_broker_install_apply.py"])
        for bad in (["apply", "--bogus"], ["install", "--plan", "x"]):
            with self.subTest(argv=bad), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args(bad)

    def test_ledger_rows_and_rollback_fields_are_substantive(self) -> None:
        for cells in ledger_rows(self.text):
            with self.subTest(row=cells[0]):
                self.assertEqual(len(cells), 4, cells)
                self.assertTrue(all(len(c) >= 2 for c in cells), cells)
                self.assertGreaterEqual(len(cells[1]), 25, "the carry-forward text is too thin")
                for word in PLACEHOLDER_WORDS:
                    self.assertNotIn(word, " ".join(cells))
        for pid, block in self.blocks.items():
            rollback = field_text(block, "Rollback")
            with self.subTest(phase=pid):
                self.assertGreaterEqual(len(rollback), 60)
                for word in PLACEHOLDER_WORDS:
                    self.assertNotIn(word, rollback)

    def test_every_honoured_in_location_contains_its_key_phrase(self) -> None:
        rows = {cells[0]: cells[3] for cells in ledger_rows(self.text)}
        self.assertEqual(set(rows), set(HONOURED_PHRASES), "every ledger row needs a key phrase here")
        for row_id, pointers in rows.items():
            phrase = HONOURED_PHRASES[row_id]
            for loc in (p.strip() for p in pointers.split(",")):
                want = phrase if isinstance(phrase, str) else phrase[loc]
                with self.subTest(row=row_id, location=loc):
                    self.assertIn(want.casefold(), resolve_location(self.text, self.blocks, loc).casefold())

    # --- review round 1 (MED-1, MED-2, L2, L3, L5, L6) ---------------------------

    def test_med1_property_k_makes_the_d3_holds_tool_enforced(self) -> None:
        sec2 = section_text(self.text, 2)
        k = next(line for line in sec2.splitlines() if line.startswith("| (k) |"))
        for needle in ("STOPS at each plan `hold` entry", "SAME receipt", "explicit operator go", "tool-enforced"):
            self.assertIn(needle, k)
        self.assertIn("acceptable ONLY because the machine is disposable", sec2)
        self.assertIn("NOT evidence for property (k)", sec2)
        live = next(line for line in section_text(self.text, 1).splitlines() if "**G-LIVE:" in line)
        self.assertIn("(a)-(k)", live)
        self.assertIn("Property (k)", section_text(self.text, 5).split(BEGIN, 1)[0])
        for pid in sorted(HOLD_PHASES):
            hold = field_text(self.blocks[pid], "HOLD")
            with self.subTest(phase=pid):
                self.assertIn("property (k)", hold)
                self.assertIn("SAME receipt", hold)
        # the code fact (k) answers: U3a's apply only STAMPS a hold entry, it never stops on one
        apply_src = (REPO_ROOT / "tools/host_broker_install_apply.py").read_text(encoding="utf-8")
        self.assertIn('out["hold_acknowledged"] = f"fixture: {e[\'hold\']}"', apply_src)

    def test_med1_every_rollback_states_its_scope(self) -> None:
        whole = "**Scope: the WHOLE receipt, reverse sequence.**"
        expected_whole = {"P2", "P4", "P5", "P6", "P7", "P8", "P9", "P11"}
        for pid, block in self.blocks.items():
            rollback = field_text(block, "Rollback")
            with self.subTest(phase=pid):
                self.assertIn("**Scope:", rollback)
                self.assertEqual(whole in rollback, pid in expected_whole)
        self.assertIn("**Scope: none.**", field_text(self.blocks["P0"], "Rollback"))
        self.assertIn("**Scope: P10's own, HOLD-free.**", field_text(self.blocks["P10"], "Rollback"))
        # the code fact: rollback reverses EVERY mutating receipt entry by descending seq
        apply_src = (REPO_ROOT / "tools/host_broker_install_apply.py").read_text(encoding="utf-8")
        self.assertIn('key=lambda r: -r["seq"]', apply_src)

    def test_med2_membership_is_a_gate_before_p10_not_the_p0_precondition(self) -> None:
        from tools import persona_installer_contracts as PC

        gates = section_text(self.text, 1)
        gate = next(line for line in gates.splitlines() if "**G-MEMBERSHIP:" in line)
        self.assertIn("immediately before P10, NOT before P0", gate)
        self.assertLess(gate.index("doc row"), gate.index("membership commit"))
        self.assertLess(gate.index("membership commit"), gate.index("membership check"))
        self.assertIn("after the daemons are healthy", gate)
        self.assertNotIn("G-MEMBERSHIP", field_text(self.blocks["P0"], "Preconditions"))
        j = next(line for line in section_text(self.text, 2).splitlines() if line.startswith("| (j) |"))
        self.assertIn("NOT the P0 precondition", j)
        p10 = self.blocks["P10"]
        for needle in ("G-MEMBERSHIP is ticked", "re-plan at the new commit", "the P4 framework copy",
                       "the P6 plan registration", "restart of the broker and approval units onto the new rev",
                       "a fresh gate-32a check", "revert the membership commit", "HOLD-free"):
            with self.subTest(needle=needle):
                self.assertIn(needle, p10)
        # the code facts: the precondition is a P0 entry gating the framework copy (P4), and the
        # membership edit's file is framework code, so it moves the rev and the plan sha
        pre = next(e for e in real_plan()["entries"] if e["id"] == "precondition:mcp-allowlist")
        self.assertEqual(pre["phase"], "P0")
        self.assertTrue(any("precondition:mcp-allowlist" in (e.get("after") or []) and e["phase"] == "P4"
                            for e in real_plan()["entries"]))
        self.assertIn("tools/persona_installer.py", PC.FRAMEWORK_CODE_ARTIFACTS)
        cf = next(c for c in ledger_rows(self.text) if c[0] == "CF-U5-1")
        self.assertIn("G-MEMBERSHIP", cf[1])

    def test_l2_q10_backups_are_apply_s_not_a_dry_run_s(self) -> None:
        self.assertIn("P1 takes NO backup", self.blocks["P1"])
        for pid in ("P2", "P3"):
            self.assertIn("<receipt>.d/backups", field_text(self.blocks[pid], "Expect"))
        self.assertNotIn("tar into", self.text)
        self.assertNotIn("backed up in P1", self.text)
        self.assertNotIn("P1 Q10 backup", self.text)
        apply_src = (REPO_ROOT / "tools/host_broker_install_apply.py").read_text(encoding="utf-8")
        self.assertIn('_backup_state_dirs(ctx, started, "pre-apply")', apply_src)

    def test_l3_the_subset_ruling_is_credited_to_mason(self) -> None:
        self.assertNotIn("orchestrator's final ruling", self.text)
        self.assertIn("mason's ruling, T-2920 annotation `@fee4e14dd`", self.text)

    def test_l5_new_observe_callers_are_d15(self) -> None:
        i = next(line for line in section_text(self.text, 2).splitlines() if line.startswith("| (i) |"))
        self.assertIn("new external caller of `observe()`", i)

    def test_l6_no_overclaimed_gates(self) -> None:
        self.assertRegex(field_line(self.blocks["P1"], "Gates"), r"observed: none \(")
        p0 = field_text(self.blocks["P0"], "Preconditions")
        self.assertIn("generator consistency only", p0)
        self.assertIn("T-5643 (still open", p0)

    # --- readiness gates, the live change, standing notes ----------------------

    def test_readiness_gates(self) -> None:
        gates = self.text.split("## 1. Enable-readiness gates", 1)[1].split("## 2.", 1)[0]
        for needle in ("G-S3", "T-5643", "MB-1", "MB-2", "MB-3", "MB-4", "MB-5", "MB-6", "G-R1", "G-R2", "G-LIVE",
                       "G-PRE", "G-FIRE", "--check-evidence", "**C5 is NOT a gate**"):
            self.assertIn(needle, gates)
        fire = next(line for line in gates.splitlines() if "**G-FIRE:" in line)
        for needle in ("orchestrator's supervised fire", "G-MB", "G-S3", "T-5643"):
            self.assertIn(needle, fire)
        self.assertLess(gates.index("MB-4 FIRST"), gates.index("MB-1:"))

    def test_live_change_properties_a_to_j(self) -> None:
        section = self.text.split("## 2. Order", 1)[1].split("## 3.", 1)[0]
        self.assertIn("which does NOT exist yet", section)
        self.assertIn("The rehearsals come BEFORE any live step", section)
        for prop in LIVE_CHANGE_PROPS:
            self.assertIsNotNone(re.search(rf"^\| {re.escape(prop)} \|", section, flags=re.MULTILINE), prop)
        self.assertIn("OPERATOR_BOOTSTRAP_ENABLED", section)
        self.assertIn("CF-U5-1", section)

    def test_pre_loop_note_matches_the_apply_refusal(self) -> None:
        source = (REPO_ROOT / "tools/host_broker_install_apply.py").read_text(encoding="utf-8")
        self.assertIn("(or its .d dir) already exists (O_EXCL)", source)
        self.assertIn("(or its .d dir) already exists", self.text)
        self.assertIn("**Use a fresh `--receipt` path**", self.text)

    def test_standing_rule_never_self_widen_the_mount_allowlist(self) -> None:
        self.assertIn("**The mount allowlist is never self-widened.**", self.text)
        self.assertIn("needs a NEW Recovery D15 ruling", self.text)

    def test_p10_membership_is_a_separate_ratified_commit_and_rows_are_empty(self) -> None:
        from tools import persona_installer as PI

        self.assertIn("**Membership is a SEPARATE, orchestrator-ratified commit before registration.**", self.blocks["P10"])
        self.assertIn("**The mcp-adoption.md §2 doc row comes before any persona set is filled.**", self.blocks["P10"])
        self.assertIn("LOW-1 ruling (b)", self.blocks["P10"])
        for name in ("pantheon-host", "pantheon-approval"):
            self.assertEqual(PI._CLAUDE_MCP_ALLOWLIST[name], frozenset())

    def test_no_operator_batch_shaped_values(self) -> None:
        self.assertIsNone(re.search(r"\b\d{6,}:[A-Za-z0-9_-]{20,}", self.text), "a bot-token-shaped value")
        self.assertEqual(re.findall(r"(?<![\w.-])\d{6,}(?![\w-])", self.text), [], "a long numeric id")
        self.assertNotIn("@", re.sub(r"`[^`]*`", "", self.text).replace("@MAILBOX_HUB@", ""))

    def test_runbook_is_outside_the_name_scan(self) -> None:
        rel = RUNBOOK.relative_to(REPO_ROOT).as_posix()
        self.assertFalse(any(rel == s or rel.startswith(s + "/") for s in H.NAME_SCAN))


if __name__ == "__main__":
    if sys.argv[1:] == ["--regen"]:
        regenerate()
        print(f"regenerated {RUNBOOK.relative_to(REPO_ROOT)}")
        raise SystemExit(0)
    unittest.main()
