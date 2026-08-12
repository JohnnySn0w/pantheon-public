#!/usr/bin/env python3
"""Guards for the canonical OS-account → persona axis (T-2256).

WHY THIS FILE EXISTS. specialist's T-2255 inventory found ~34 hand-carried copies of
the OS-account → persona mapping in tools/, about half of them incomplete, and
two measured casualties in one evening — a hook deploy reporting success while
covering 3 of 6 accounts, and the T-0877 WIP cap failing OPEN for exactly one
seat for eight days. Both were silent by construction. T-2256 replaced the
fleet-wide copies with `agent_role_map.account_persona_map()` / `fleet_accounts()`,
derived from instance.yaml. These tests are the check that makes that structural
rather than aspirational.

THE TAUTOLOGY TRAP, and why FLEET_ACCOUNTS below is typed out by hand. A test
that pins six accounts is coverage. A test that pins six accounts as a literal it
ALSO imports from the code under test is a tautology: it passes forever and
asserts nothing. The precedent this copies is
test_ticket.TestOsUserPersonaMapCoversTheFleet, which writes the six out
independently. The literals here come from README.md's Topology table and were
cross-checked against `getent passwd` (which is also asserted below, so the
cross-check is re-run rather than remembered) — NOT from the module.

DO NOT infer account names from persona names. specialist's own first scan invented
`claude-mnemosyne` / `claude-metis` / `claude-atlas` by extending the pattern
from the three accounts it knew, ran clean, and printed a confident MISSING row
against a fix that had in fact landed. That is the error this file's literals
exist to make impossible to repeat quietly.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_YAML = REPO_ROOT / "instance.yaml"
sys.path.insert(0, str(REPO_ROOT))

from tools import agent_role_map  # noqa: E402
from tools.agent_role_map import (  # noqa: E402
    AGENT_ROLE_MAP,
    account_persona,
    account_persona_map,
    fleet_accounts,
    instance_binding,
)

# The perturbation itself is defined ONCE, in the accessor-hop guard, and imported here.
# Re-typing the suffix would give the two hops two spellings of "perturbed" that can drift
# apart while the two files still read as one story. Guard F asserts the manifest reaches
# agent_role_map's accessors; TestConsumersFollowThePerturbedManifest below asserts it
# reaches the CONSUMERS, against the same perturbed file.
from tools.test_instance_role_grants import (  # noqa: E402
    _PROBE_ACCOUNT_SUFFIX,
    _write_perturbed_manifest,
)

# --- Written out by hand. See the docstring. --------------------------------
# The six Claude Code seats (README.md Topology: three pre-ORG-042, plus ORG-042's
# second head and ORG-043's two sub-orchestrator seats).
FLEET_ACCOUNTS: dict[str, str] = {
    "claude": "orchestrator",
    "claude-research": "specialist",
    "claude-fallback": "recovery",
    "exocortex": "mnemosyne",
    "suborch1": "metis",
    "suborch2": "atlas",
}

# The two peer daemons. persona == account for both, deliberately: agent_role_map
# registers them with identity audit role-ids ("no separate role-id; persona ==
# account for both, which the bringup SOP §4 explicitly permits"). So an identity
# result is the CORRECT answer here — which is exactly why the fail-open guard
# below is scoped to FLEET_ACCOUNTS and not to the whole map.
PEER_DAEMON_ACCOUNTS: dict[str, str] = {
    "codex": "codex",
    "the-ancestor": "the-ancestor",
}

ALL_ACCOUNTS: dict[str, str] = {**FLEET_ACCOUNTS, **PEER_DAEMON_ACCOUNTS}


# --- T-2334: the CONSUMER hop. ----------------------------------------------
# Six modules bind a derived accessor to a module constant AT IMPORT TIME, and a seventh
# re-exports one of those constants. Guard F (test_instance_role_grants) proves the manifest
# reaches agent_role_map's accessors and stops there; re-enumerating any of these snapshots as
# a hand-written literal leaves every suite in this repo green while the manifest genuinely
# stops reaching the site.
#
# THE EXPECTED VALUES BELOW ARE PER-SITE ON PURPOSE. The six look alike and are not alike:
# graveyard maps accounts to AGENT NAMES with a documented non-persona override, links carries
# non-derivable aliases and one group that has no account at all, two sites are plain sorted
# account tuples, two are account->persona dicts. One shared expectation applied six times
# would be satisfiable by the five it fits, with the sixth passing for the wrong reason — the
# same "covers N-1 invisibly" shape this whole cluster is about. The MECHANICS are shared
# (one perturbed tree, one probe runner); the EXPECTATIONS are not.


def _probe_account(account: str) -> str:
    """The name *account* carries in the perturbed manifest copy."""
    return account + _PROBE_ACCOUNT_SUFFIX


# account -> persona as the PERTURBED manifest states it. Hand-written (it is ALL_ACCOUNTS,
# the literal this file's docstring defends, with the probe suffix applied) rather than read
# back out of the perturbed file: an expected value parsed by the same code the loader uses
# would agree with a broken loader. A consumer that re-enumerated its snapshot as a literal
# answers with UNSUFFIXED account names and fails every equality below.
PROBE_ACCOUNT_PERSONA: dict[str, str] = {_probe_account(a): p for a, p in ALL_ACCOUNTS.items()}

# graveyard maps account -> AGENT NAME. That is the persona for seven of eight, and for
# the-ancestor it is the documented override "ancestor" (its graveyard dir is already named
# that on disk; deriving the string would orphan the archives filed under it).
#
# THE OVERRIDE IS KEYED ON THE LIVE ACCOUNT STRING, so against probe accounts it does not
# fire and the correct expected value here is the persona for all eight. This test therefore
# does NOT exercise the override branch; the override is asserted live, in
# TestRepointedSitesStillDerive.test_graveyard_user_to_agent.
GRAVEYARD_NAME_OVERRIDES: dict[str, str] = {"the-ancestor": "ancestor"}
GRAVEYARD_PROBE_EXPECTED: dict[str, str] = {
    _probe_account("claude"): "orchestrator",
    _probe_account("claude-research"): "specialist",
    _probe_account("claude-fallback"): "recovery",
    _probe_account("exocortex"): "mnemosyne",
    _probe_account("suborch1"): "metis",
    _probe_account("suborch2"): "atlas",
    _probe_account("codex"): "codex",
    _probe_account("the-ancestor"): "the-ancestor",  # NOT "ancestor" — see above.
}

# links groups actor-strings that mean the same agent. NINE groups against EIGHT accounts,
# and the ninth is not an account short — it is `operator`, a non-agent actor that has no OS
# account and never will. Two further members are non-derivable and hand-carried in links
# itself: the legacy free-form role word `orchestrator`, and the audit role-ids. Written out
# in full here so the account slot is the only thing the perturbation moves.
LINKS_PROBE_GROUPS: list[set[str]] = [
    {"orchestrator", "orchestrator", "orchestrator", _probe_account("claude")},
    {"specialist", "specialist", _probe_account("claude-research")},
    {"recovery", _probe_account("claude-fallback")},  # audit role-id IS "recovery"
    {"mnemosyne", _probe_account("exocortex")},
    {"metis", _probe_account("suborch1")},
    {"atlas", _probe_account("suborch2")},
    {"codex", _probe_account("codex")},  # persona == account by design
    {"the-ancestor", _probe_account("the-ancestor")},
    {"operator"},  # the ninth: a non-agent actor, no account, no role-id
]

# Every module-level snapshot of the account axis this file guards, mapped to the per-site
# test that asserts it. THE MAPPING IS THE TABLE and GUARDED_SNAPSHOTS is derived from it, so
# a row cannot be added without naming a test, and TestEverySnapshotSiteIsGuarded checks the
# name resolves to a real method.
#
# This was a bare tuple until T-2334's review, alongside a docstring claiming "every guard
# above is keyed on it". It was not: the per-site guards are hand-written methods and nothing
# tied them to the table. Measured — planting a derived eighth consumer AND adding its row
# left the suite fully green with no per-site guard in existence, and the drift check's own
# failure message is what tells a reader to add the row.
PER_SITE_GUARDS: dict[tuple[str, str], str] = {
    ("disk_usage_monitor", "AGENT_HOMES"):
        "test_disk_usage_monitor_agent_homes_follows_the_manifest",
    ("graveyard", "_USER_TO_AGENT"):
        "test_graveyard_user_to_agent_follows_the_manifest",
    ("links", "_ALIAS_GROUPS"):
        "test_links_alias_groups_follow_the_manifest",
    ("mailbox_mcp", "_USER_TO_PERSONA"):
        "test_mailbox_mcp_user_to_persona_follows_the_manifest",
    ("session_reset_log", "KNOWN_ACCOUNTS"):
        "test_session_reset_log_known_accounts_follows_the_manifest",
    ("s3star_audit_runner", "_SNAPSHOT_MAILBOX_AGENTS"):
        "test_s3star_snapshot_mailbox_agents_follows_the_manifest",
    ("s3star_audit_runner", "_SNAPSHOT_UNIX_USERS"):
        "test_s3star_snapshot_unix_users_follows_the_manifest",
    ("session_reset_reconcile", "KNOWN_ACCOUNTS"):
        "test_session_reset_reconcile_known_accounts_follows_the_manifest",
    ("restart_drain", "DRAIN_AGENT_CONFIG"):
        "test_restart_drain_agent_config_follows_the_manifest",
    ("ticket", "_OS_USER_PERSONA"):
        "test_ticket_wip_cap_map_follows_the_manifest",
}

GUARDED_SNAPSHOTS: tuple[tuple[str, str], ...] = tuple(sorted(PER_SITE_GUARDS))

# Both sys.path shapes these modules actually run under. Not a formality: mailbox_mcp binds
# its accessor inside a `try: from tools.agent_role_map ... except ImportError: from
# agent_role_map ...`, and SCRIPT is the branch its production launch takes, so a literal
# planted in the except arm is invisible to package-mode assertions.
_PROBE_MODES = ("package", "script")


def _probe_body(symbol: str) -> str:
    """Child-process source that encodes module constant *symbol* (bound to `m`) as JSON."""
    return (
        f"value = getattr(m, {symbol!r})\n"
        "if isinstance(value, dict):\n"
        "    payload = dict(kind='dict', value=value)\n"
        "elif isinstance(value, tuple):\n"
        "    payload = dict(kind='tuple', value=list(value))\n"
        "elif isinstance(value, list) and value and all(isinstance(x, set) for x in value):\n"
        "    payload = dict(kind='groups', value=sorted(sorted(x) for x in value))\n"
        "else:\n"
        "    payload = dict(kind='unexpected:' + type(value).__name__, value=repr(value))\n"
        "print('@@' + json.dumps(payload, sort_keys=True))\n"
    )


class TestAccessorResolvesTheFleet(unittest.TestCase):
    def test_every_claude_seat_resolves_to_its_persona(self) -> None:
        for account, persona in FLEET_ACCOUNTS.items():
            with self.subTest(account=account):
                self.assertEqual(account_persona(account), persona)

    def test_every_peer_daemon_account_resolves(self) -> None:
        for account, persona in PEER_DAEMON_ACCOUNTS.items():
            with self.subTest(account=account):
                self.assertEqual(account_persona(account), persona)

    def test_no_claude_seat_resolves_to_itself(self) -> None:
        """The fail-OPEN signature (T-2255's worked instance).

        If the accessor ever hands back the OS account for a seat whose persona
        differs from it, every consumer keyed on personas — the WIP cap, mailbox
        routing, touched-by expansion — is keying on a value no ticket, ledger
        entry or inbox path carries. Nothing errors; the guard just stops
        applying. Scoped to FLEET_ACCOUNTS on purpose: codex/the-ancestor
        legitimately equal their accounts (see PEER_DAEMON_ACCOUNTS).
        """
        for account in FLEET_ACCOUNTS:
            with self.subTest(account=account):
                self.assertNotEqual(
                    account_persona(account),
                    account,
                    f"{account} resolves to itself -> every persona-keyed guard "
                    f"downstream silently stops matching for that seat",
                )

    def test_unknown_account_returns_none_not_the_account(self) -> None:
        """Pins the unknown-account posture, which is a deliberate DEPARTURE
        from this module's other three resolvers.

        resolve_role / resolve_persona / resolve_instance_role all pass unknown
        names through unchanged. account_persona does NOT, because an OS account
        is not a persona: handing it back manufactures a persona nothing carries.
        None makes "I don't know" unrepresentable as a valid answer and leaves
        the fail-open/fail-closed choice at the call site, where it is decided.
        """
        for unknown in ("runner", "root", "nobody", "operator", "orchestrator", ""):
            with self.subTest(account=unknown):
                self.assertIsNone(account_persona(unknown))


class TestBothDirections(unittest.TestCase):
    """metis's reviewer finding: existing coverage on this axis asserted only
    registry ⊆ map, never map ⊆ registry. A one-directional completeness check
    passes on a map with an extra bogus account in it forever."""

    def test_map_equals_the_hand_written_expectation_exactly(self) -> None:
        self.assertEqual(account_persona_map(), ALL_ACCOUNTS)

    def test_every_mapped_account_points_at_a_registered_persona(self) -> None:
        """map ⊆ registry — no account may resolve to a persona the registry
        does not know, or ticket target:/requester: validation rejects it."""
        unregistered = sorted(
            f"{a} -> {p}" for a, p in account_persona_map().items() if p not in AGENT_ROLE_MAP
        )
        self.assertEqual(unregistered, [])

    def test_every_persona_with_an_account_binding_appears_in_the_map(self) -> None:
        """registry ⊆ map — the direction that was already covered elsewhere,
        asserted here against the bindings rather than a literal so a new
        instance.yaml role is caught the moment it is added."""
        bound = {
            persona
            for persona in AGENT_ROLE_MAP
            if agent_role_map.instance_binding(persona).get("account")
        }
        self.assertEqual(bound, set(account_persona_map().values()))

    def test_fleet_accounts_is_the_sorted_key_set(self) -> None:
        self.assertEqual(fleet_accounts(), tuple(sorted(ALL_ACCOUNTS)))
        self.assertEqual(list(fleet_accounts()), sorted(fleet_accounts()))


class TestAgainstTheOperatingSystem(unittest.TestCase):
    """specialist's rule, made re-runnable: `/etc/passwd` cost one command and
    reversed an invented-account-name conclusion. A canonical map is what makes
    remembering /etc/passwd unnecessary — it is NOT a reason to stop checking."""

    def test_every_mapped_account_is_a_real_unix_account(self) -> None:
        getent = shutil.which("getent")
        if getent is None:  # pragma: no cover - non-glibc/CI host
            self.skipTest("getent unavailable (not a glibc host)")
        res = subprocess.run([getent, "passwd"], capture_output=True, text=True, timeout=30)
        if res.returncode != 0 or not res.stdout.strip():  # pragma: no cover
            self.skipTest("getent passwd returned nothing")
        real = {line.split(":", 1)[0] for line in res.stdout.splitlines() if line}
        if not (set(FLEET_ACCOUNTS) & real):
            self.skipTest("not the Pantheon VPS (no fleet accounts on this host)")
        missing = sorted(set(account_persona_map()) - real)
        self.assertEqual(
            missing,
            [],
            "instance.yaml binds accounts that do not exist on this host — either "
            "an invented name or an un-provisioned seat",
        )


class TestCollisionHandling(unittest.TestCase):
    """Two personas on one account is unresolvable, so it must be REPORTED.
    Raising at import would brick every ticket op, the mailbox MCP and every
    monitor on one bad manifest edit, so the loader stays fail-safe and the
    loudness lives in the recorded-collisions map plus this check."""

    def test_live_manifest_has_no_collisions(self) -> None:
        self.assertEqual(agent_role_map._ACCOUNT_PERSONA_COLLISIONS, {})

    def test_a_collision_is_recorded_and_resolution_is_deterministic(self) -> None:
        bindings = {
            "zeta": {"account": "shared"},
            "alpha": {"account": "shared"},
            "solo": {"account": "own"},
        }
        resolved, collisions = agent_role_map._invert_bindings(bindings)
        self.assertEqual(collisions, {"shared": ["alpha", "zeta"]})
        # sorted-first-wins, NOT dict insertion order: "zeta" is inserted first,
        # so an insertion-order implementation would answer "zeta" here and the
        # same broken manifest would resolve differently depending on YAML key
        # order. Deterministic beats arbitrary when neither can be correct.
        self.assertEqual(resolved["shared"], "alpha")
        self.assertEqual(resolved["own"], "solo")

    def test_bindings_without_an_account_are_skipped_not_crashed(self) -> None:
        resolved, collisions = agent_role_map._invert_bindings(
            {"a": {}, "b": {"account": ""}, "c": {"account": None}, "d": {"account": "x"}}
        )
        self.assertEqual(resolved, {"x": "d"})
        self.assertEqual(collisions, {})


class TestAccessorIsolation(unittest.TestCase):
    def test_account_persona_map_returns_a_copy(self) -> None:
        first = account_persona_map()
        first["claude"] = "corrupted"
        first["injected"] = "nobody"
        self.assertEqual(account_persona_map()["claude"], "orchestrator")
        self.assertNotIn("injected", account_persona_map())
        self.assertEqual(account_persona("claude"), "orchestrator")


class TestFallbackMirrorIsComplete(unittest.TestCase):
    """The hardcoded fallback is reached in any environment without a staged
    instance.yaml. If it under-covers the fleet, the derived accessor
    under-covers it too — which would turn one canonical map into a canonical
    map that is quietly wrong exactly where no manifest is present."""

    def test_fallback_covers_every_manifest_persona(self) -> None:
        missing = sorted(
            set(agent_role_map._PERSONA_TO_BINDING) - set(agent_role_map._FALLBACK_PERSONA_TO_BINDING)
        )
        self.assertEqual(missing, [])

    def test_fallback_accounts_invert_to_the_same_map(self) -> None:
        resolved, collisions = agent_role_map._invert_bindings(
            agent_role_map._FALLBACK_PERSONA_TO_BINDING
        )
        self.assertEqual(collisions, {})
        self.assertEqual(resolved, ALL_ACCOUNTS)


class TestRepointedSitesStillDerive(unittest.TestCase):
    """One test per fleet-wide site T-2256 repointed, asserting each site's LIVE value
    against the accessor it is supposed to have snapshotted — i.e. that the snapshot has
    not gone STALE relative to instance.yaml.

    WHAT THESE DO NOT DO — corrected in T-2334, because the previous version of this
    docstring claimed it and the tests never delivered it. It read "These fail if anyone
    re-enumerates a literal". They do not, and they cannot: every one of these sites is
    CORRECT today, so a hand-written literal copying today's values passes any comparison
    made on this box today. Worse, the comparison was against ALL_ACCOUNTS — a hand-written
    literal in this file — so the witness agreed with a hardcoded map by construction, which
    is the same witness-shares-the-accused shape the tests exist to prevent.

    The detector for a re-enumerated literal is TestConsumersFollowThePerturbedManifest
    below: it imports each site against a perturbed COPY of instance.yaml and requires the
    constant to follow. These tests are still worth having and now assert something they
    can actually fail: the site agrees with the accessor's live value, so a snapshot left
    behind by a manifest edit is RED here immediately.

    The accessor's own CONTENT is pinned against the hand-written literal in
    TestBothDirections.test_map_equals_the_hand_written_expectation_exactly, and its
    liveness by test_instance_role_grants Guard F, so nothing moved out of coverage.
    """

    def test_ticket_wip_cap_map(self) -> None:
        from tools import ticket

        self.assertEqual(ticket._OS_USER_PERSONA, account_persona_map())

    def test_mailbox_mcp_user_to_persona(self) -> None:
        try:
            from tools import mailbox_mcp
        except ImportError:  # pragma: no cover - mcp SDK not installed
            self.skipTest("mcp SDK unavailable")
        self.assertEqual(mailbox_mcp._USER_TO_PERSONA, account_persona_map())

    def test_send_schema_description_matches_its_own_enum(self) -> None:
        """The model-facing description is a THIRD copy of the recipient list,
        sitting one line below the enum it contradicted: it read "orchestrator, specialist,
        or recovery" — the pre-ORG-042 three — while the enum already carried six.
        A tool description IS the surface the model reads, so a stale one
        under-advertises half the fleet to every caller. Same defect class as
        the rest of this ticket; guarded so the third copy cannot re-diverge."""
        try:
            from tools import mailbox_mcp
        except ImportError:  # pragma: no cover - mcp SDK not installed
            self.skipTest("mcp SDK unavailable")
        prop = mailbox_mcp._SEND_SCHEMA["properties"]["recipient"]
        for recipient in prop["enum"]:
            with self.subTest(recipient=recipient):
                self.assertIn(recipient, prop["description"])

    def test_disk_usage_monitor_agent_homes(self) -> None:
        from tools import disk_usage_monitor

        self.assertEqual(disk_usage_monitor.AGENT_HOMES, fleet_accounts())

    def test_session_reset_log_known_accounts(self) -> None:
        from tools import session_reset_log

        self.assertEqual(session_reset_log.KNOWN_ACCOUNTS, fleet_accounts())

    def test_session_reset_reconcile_reexports_the_same_tuple(self) -> None:
        """The SEVENTH site, and it was not in the six T-2334 was filed against — found by
        grepping for the constants rather than for the accessors. session_reset_reconcile
        does `from tools.session_reset_log import KNOWN_ACCOUNTS`, which binds a module
        constant of its own: a second hop, on which replacing the import with a literal
        stops the manifest reaching the reconciler while session_reset_log stays derived.
        `is` rather than `==`, because a re-export must be the same object and any literal
        is not."""
        from tools import session_reset_log, session_reset_reconcile

        self.assertIs(session_reset_reconcile.KNOWN_ACCOUNTS, session_reset_log.KNOWN_ACCOUNTS)

    def test_graveyard_user_to_agent(self) -> None:
        """Keys derive; ONE value is a documented override. the-ancestor's
        graveyard dir is named "ancestor" and has archived snapshots + an
        index.md already filed under it, so deriving that string would re-home
        future archives and orphan the existing ones — in the tool whose job is
        not losing dying context.

        This is the ONLY test that exercises the override branch: it is keyed on the live
        account string, so the perturbed-manifest guard below cannot reach it.
        """
        from tools import graveyard

        live = account_persona_map()
        expected = {a: GRAVEYARD_NAME_OVERRIDES.get(a, p) for a, p in live.items()}
        self.assertEqual(set(graveyard._USER_TO_AGENT), set(live))
        self.assertEqual(graveyard._USER_TO_AGENT, expected)
        self.assertEqual(graveyard._USER_TO_AGENT["the-ancestor"], "ancestor")
        self.assertEqual(graveyard.agent_from_handoff_path("/home/exocortex/HANDOFF.md"), "mnemosyne")
        self.assertEqual(graveyard.agent_from_handoff_path("/home/suborch2/HANDOFF.md"), "atlas")
        self.assertIsNone(graveyard.agent_from_handoff_path("/home/nobody/HANDOFF.md"))

    def test_links_alias_groups_cover_every_account(self) -> None:
        """The live defect T-2256 opened with: `touched-by mnemosyne` matched
        only [mnemosyne] lines and missed all 80 [exocortex] ones, because
        alias_group() falls back to SELF and the seat had no group at all.

        Driven off the accessor, not off ALL_ACCOUNTS: an account instance.yaml binds and
        links has no group for is the defect, and iterating a literal cannot see it.
        """
        from tools import links

        for account, persona in account_persona_map().items():
            with self.subTest(account=account):
                group = links.alias_group(persona)
                self.assertIn(account, group)
                self.assertIn(persona, group)
                self.assertEqual(links.alias_group(account), group)

    def test_links_preserves_the_pre_existing_groups(self) -> None:
        """Derivation must not quietly drop the non-derivable members: the
        legacy `orchestrator` role word, the audit role-ids, and the non-agent
        `operator` actor. Unknown tokens must still match only themselves."""
        from tools import links

        self.assertEqual(links.alias_group("recovery"), {"recovery", "claude-fallback"})
        self.assertEqual(
            links.alias_group("orchestrator"), {"orchestrator", "orchestrator", "orchestrator", "claude"}
        )
        self.assertEqual(links.alias_group("specialist"), {"specialist", "claude-research", "specialist"})
        self.assertEqual(links.alias_group("operator"), {"operator"})
        self.assertEqual(links.alias_group("grooming"), {"grooming"})
        self.assertEqual(links.alias_group("auto-unblock"), {"auto-unblock"})

    def test_dispatch_import_error_fallback_mirrors_the_registry(self) -> None:
        """dispatch.py's fallback is the ONE list in this arc that cannot derive
        — it is the branch taken when agent_role_map cannot be imported at all.
        So it gets the repo's other answer to a hand-carried list: a check that
        fails. Exercised by actually breaking the import, not by reading the
        literal, so the fallback BRANCH is proven reachable too."""
        from tools import dispatch

        saved = sys.modules.get("tools.agent_role_map")
        try:
            sys.modules["tools.agent_role_map"] = None  # forces ImportError
            reloaded = importlib.reload(dispatch)
            self.assertEqual(reloaded._KNOWN_AGENTS, set(AGENT_ROLE_MAP))
        finally:
            if saved is not None:
                sys.modules["tools.agent_role_map"] = saved
            else:  # pragma: no cover
                sys.modules.pop("tools.agent_role_map", None)
            importlib.reload(dispatch)


class TestConsumersFollowThePerturbedManifest(unittest.TestCase):
    """T-2334 — the CONSUMER hop: instance.yaml reaches the module constants, not merely
    agent_role_map's accessors.

    Guard F (test_instance_role_grants.TestManifestWasActuallyLoaded) proves the manifest is
    READ and that what it says reaches account_persona_map() / fleet_accounts() /
    account_persona(). It stops there, and says so. Every site below binds one of those
    accessors to a module constant AT IMPORT TIME, which is one hop further out than any
    assertion in this repo reached before: re-enumerate any of them as a hand-written literal
    and the manifest genuinely stops reaching that site with every suite green, because the
    literal is correct today and only goes wrong the next time instance.yaml is edited. That
    is T-2255's measured casualty — a fleet list silently covering 3 of 6 accounts while
    reporting success — reproduced one module over.

    HOW: a materialised copy of tools/ plus a PERTURBED copy of instance.yaml in a tmp dir
    (_write_perturbed_manifest, shared with Guard F so there is one definition of
    "perturbed"), then each consumer imported in a SUBPROCESS rooted there. A subprocess and
    not importlib: a consumer does `from tools.agent_role_map import ...`, which in this
    process resolves out of sys.modules to the real module and the real manifest, so copying
    one file the way Guard F does cannot redirect it. Every account in the perturbed copy
    carries a probe suffix and every expected value below is written with that suffix, so a
    constant answering with an unsuffixed account name fails. The live instance.yaml is read
    to build the copy and never written: _write_perturbed_manifest writes under *root* only.

    ONE TEST PER SITE, WITH ITS OWN EXPECTED VALUE. The sites look alike and are not alike —
    see the per-site expectations at the top of this file. A single assertion looped over a
    table of (module, constant) would be satisfiable by the ones it fits while the odd one
    out passed for the wrong reason, which is the same defect class as the thing being
    guarded. The MECHANICS (tree, perturbation, probe runner) are shared; nothing else is.

    BOTH sys.path SHAPES. Each constant is probed under package semantics (repo root on the
    path) and script semantics (tools/ on the path, as systemd units and hooks invoke these).
    mailbox_mcp is the site that makes this load-bearing rather than tidy: it binds its
    accessor inside `try: from tools.agent_role_map ... except ImportError: from
    agent_role_map ...`, and SCRIPT is the arm its production launch takes.

    WHAT THIS DOES NOT COVER, stated because the gap it closes was itself a scope overclaim:
      - The CALL SITES of disk_usage_monitor.AGENT_HOMES and session_reset_log /
        session_reset_reconcile KNOWN_ACCOUNTS. Only their constants are asserted here. Their
        consumers walk /home/<account> and per-account state dirs, which a tmp tree does not
        have; a rewiring that leaves the constant derived and reads a literal in the loop is
        NOT detected. The four sites that do have a cheap pure entry point get one, below.
      - graveyard's the-ancestor -> "ancestor" override, which is keyed on the LIVE account
        string and therefore cannot fire against probe accounts. Asserted live, in
        TestRepointedSitesStillDerive.test_graveyard_user_to_agent.
      - Anything outside tools/*.py.
    """

    # These compare whole fleet maps; a truncated diff turns a real failure into a puzzle.
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        if not INSTANCE_YAML.exists():  # pragma: no cover - manifest-less env
            raise unittest.SkipTest(
                "no live instance.yaml to perturb — a manifest-less environment is supported "
                "(the fallbacks exist for it) and this guard has nothing to compare against"
            )
        cls.root = Path(tempfile.mkdtemp(prefix="t2334-consumers-"))
        cls.addClassCleanup(shutil.rmtree, cls.root, True)
        (cls.root / "tools").mkdir(parents=True, exist_ok=True)
        # REAL copies, not symlinks: agent_role_map locates the manifest via
        # Path(__file__).resolve(), which follows symlinks straight back to the live tree and
        # would silently un-do the redirect. Every tools/*.py, because a consumer may import a
        # sibling and a curated subset is one more list to keep in step.
        for src in (REPO_ROOT / "tools").glob("*.py"):
            shutil.copyfile(src, cls.root / "tools" / src.name)
        _write_perturbed_manifest(cls.root)

    def _run(self, module: str, body: str, mode: str) -> dict:
        """Import *module* from the perturbed tree in a subprocess and decode its payload."""
        prelude = (
            f"import tools.{module} as m\n" if mode == "package" else f"import {module} as m\n"
        )
        cwd = self.root if mode == "package" else self.root / "tools"
        res = subprocess.run(
            [sys.executable, "-c", "import json\n" + prelude + body],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=180,
            # PYTHONPATH cleared so the LIVE repo cannot leak onto sys.path and serve the
            # real manifest; that would make every assertion below green for the wrong tree.
            env={**os.environ, "PYTHONPATH": ""},
        )
        if "No module named 'mcp'" in res.stderr:  # pragma: no cover - SDK absent
            self.skipTest(f"{module}: mcp SDK unavailable on this host")
        self.assertEqual(
            res.returncode,
            0,
            f"{module} [{mode}] failed to import against the perturbed tree:\n{res.stderr}",
        )
        payloads = [ln for ln in res.stdout.splitlines() if ln.startswith("@@")]
        self.assertEqual(
            len(payloads), 1, f"{module} [{mode}] emitted no probe payload:\n{res.stdout}"
        )
        return json.loads(payloads[0][2:])

    def _constant(self, module: str, symbol: str, mode: str, kind: str):
        payload = self._run(module, _probe_body(symbol), mode)
        self.assertEqual(
            payload["kind"],
            kind,
            f"{module}.{symbol} is no longer a {kind} — the expected value below was written "
            f"for that shape and cannot judge a {payload['kind']}",
        )
        return payload["value"]

    # -- one site, one expectation -------------------------------------------

    def test_ticket_wip_cap_map_follows_the_manifest(self) -> None:
        """ticket._OS_USER_PERSONA is the T-0877 WIP cap's account -> persona map, with no
        overrides and no extra members: every bound account, mapped to its persona. This is
        the site the ticket was filed on, and the one whose failure mode is measured — the
        cap keyed on a value no ticket carries, one seat uncapped for eight days, silent."""
        for mode in _PROBE_MODES:
            with self.subTest(mode=mode):
                value = self._constant("ticket", "_OS_USER_PERSONA", mode, "dict")
                self.assertEqual(value, PROBE_ACCOUNT_PERSONA)

    def test_restart_drain_agent_config_follows_the_manifest(self) -> None:
        """restart_drain.DRAIN_AGENT_CONFIG became a guarded site at T-2370, when the
        hand-carried `_DRAIN_PERSONAS = ("recovery", "orchestrator", "specialist")` tuple was replaced by
        a derivation over the account axis. That tuple was the pre-ORG-042 three and it
        CONTRADICTED the manifest it claimed to be built from: instance.yaml grants
        restart_service to a fourth role (second-head/mnemosyne), the builder's own
        `"restart_service" in binding` predicate already selected it, and the outer tuple
        subtracted it back out — so a restart tombstone written for mnemosyne hit the
        `agent not in DRAIN_AGENT_CONFIG` branch and was quarantined as an unknown agent,
        silently, for a seat the manifest says is restartable.

        WHY THE PERTURBED ACCOUNT IS THE ONLY HONEST PROBE: every path in this config is
        home-rooted, so a hardcoded `/home/claude/HANDOFF.md` answers CORRECTLY on the live
        host and no same-host assertion can tell it from a derived one (the T-2324 shape).
        Only an account the running box does not have can.

        Asserted as a property rather than by rebuilding the expected dict here — a
        re-derivation would restate the implementation and pass for a site that had been
        rewired to a differently-wrong literal.
        """
        persona_to_probe_account = {p: a for a, p in PROBE_ACCOUNT_PERSONA.items()}
        for mode in _PROBE_MODES:
            with self.subTest(mode=mode):
                value = self._constant("restart_drain", "DRAIN_AGENT_CONFIG", mode, "dict")

                # 1. KEY SET — exactly the manifest's restart_service grantees. mnemosyne
                #    is named explicitly: it is the measured casualty, and a regression
                #    that dropped it while keeping the derivation would still satisfy a
                #    bare "keys are a subset of the fleet" check.
                grantees = {
                    p for p in PROBE_ACCOUNT_PERSONA.values()
                    if "restart_service" in instance_binding(p)
                }
                self.assertEqual(set(value), grantees)
                self.assertIn(
                    "mnemosyne", value,
                    "the drain no longer covers mnemosyne — T-2370's exact defect, in which "
                    "its restart tombstones are quarantined as an unknown agent",
                )

                # 2. THE ACCOUNT HOP — every home-rooted path names that persona's
                #    PERTURBED account, and no entry mentions a live one.
                for persona, entry in value.items():
                    probe_account = persona_to_probe_account[persona]
                    self.assertEqual(
                        entry["handoff_file"], f"/home/{probe_account}/HANDOFF.md",
                        f"{persona}'s handoff_file does not follow the manifest account",
                    )
                    self.assertTrue(
                        entry["restart_bin"].startswith(f"/home/{probe_account}/"),
                        f"{persona}'s restart_bin does not follow the manifest account: "
                        f"{entry['restart_bin']}",
                    )
                    live_account = probe_account[: -len(_PROBE_ACCOUNT_SUFFIX)]
                    self.assertNotIn(
                        f"/home/{live_account}/", entry["handoff_file"] + entry["restart_bin"],
                        f"{persona} resolved a path under the LIVE account against a manifest "
                        "that does not contain it — the account is enumerated, not derived",
                    )

    def test_mailbox_mcp_user_to_persona_follows_the_manifest(self) -> None:
        """mailbox_mcp._USER_TO_PERSONA is the same account -> persona map: $USER is looked
        up in it directly and an unknown key fails CLOSED, so a stale copy does not mis-route,
        it locks a seat out of its own mailbox."""
        for mode in _PROBE_MODES:
            with self.subTest(mode=mode):
                value = self._constant("mailbox_mcp", "_USER_TO_PERSONA", mode, "dict")
                self.assertEqual(value, PROBE_ACCOUNT_PERSONA)

    def test_disk_usage_monitor_agent_homes_follows_the_manifest(self) -> None:
        """disk_usage_monitor.AGENT_HOMES is fleet_accounts() — the sorted ACCOUNT tuple, no
        personas in it at all. Sorted is part of the contract (stable output/diffs), so it is
        asserted as an ordered tuple rather than a set."""
        for mode in _PROBE_MODES:
            with self.subTest(mode=mode):
                value = self._constant("disk_usage_monitor", "AGENT_HOMES", mode, "tuple")
                self.assertEqual(value, sorted(PROBE_ACCOUNT_PERSONA))

    def test_s3star_snapshot_unix_users_follows_the_manifest(self) -> None:
        """s3star_audit_runner._SNAPSHOT_UNIX_USERS is the account list the audit
        evidence bundle runs `id` over. T-2405 is the measured failure: it was a
        hardcoded five-tuple, the fleet grew to eight, and the bundle went on
        covering five while its OWN process capture showed the other three
        active. An under-covered identity artifact does not look broken — it
        looks like an answer, which is why it survived several cycles."""
        for mode in _PROBE_MODES:
            with self.subTest(mode=mode):
                value = self._constant(
                    "s3star_audit_runner", "_SNAPSHOT_UNIX_USERS", mode, "tuple"
                )
                self.assertEqual(value, sorted(PROBE_ACCOUNT_PERSONA))

    def test_s3star_snapshot_mailbox_agents_follows_the_manifest(self) -> None:
        """The PERSONA half of the same site — mailbox dirs are keyed by persona,
        `id` takes accounts, and the two lists were separately hardcoded. Personas
        are not perturbed by the probe (only accounts are), so this asserts the
        sorted persona set: the guard here is that the list is DERIVED from the
        same manifest at all, not that its strings move."""
        for mode in _PROBE_MODES:
            with self.subTest(mode=mode):
                value = self._constant(
                    "s3star_audit_runner", "_SNAPSHOT_MAILBOX_AGENTS", mode, "tuple"
                )
                self.assertEqual(value, sorted(PROBE_ACCOUNT_PERSONA.values()))

    def test_session_reset_log_known_accounts_follows_the_manifest(self) -> None:
        """session_reset_log.KNOWN_ACCOUNTS is the same sorted account tuple. Same shape as
        AGENT_HOMES and a different consumer: it bounds the cross-account event read, so an
        under-covered tuple silently drops another seat's resets from the reconciliation."""
        for mode in _PROBE_MODES:
            with self.subTest(mode=mode):
                value = self._constant("session_reset_log", "KNOWN_ACCOUNTS", mode, "tuple")
                self.assertEqual(value, sorted(PROBE_ACCOUNT_PERSONA))

    def test_session_reset_reconcile_known_accounts_follows_the_manifest(self) -> None:
        """The SEVENTH site, not among the six this ticket enumerated. session_reset_reconcile
        binds KNOWN_ACCOUNTS by importing it from session_reset_log — a snapshot of a
        snapshot. Replacing that import with a literal stops the manifest reaching the
        reconciler while session_reset_log itself stays perfectly derived, so the six-site
        table would have reported full coverage."""
        for mode in _PROBE_MODES:
            with self.subTest(mode=mode):
                value = self._constant(
                    "session_reset_reconcile", "KNOWN_ACCOUNTS", mode, "tuple"
                )
                self.assertEqual(value, sorted(PROBE_ACCOUNT_PERSONA))

    def test_graveyard_user_to_agent_follows_the_manifest(self) -> None:
        """graveyard._USER_TO_AGENT maps account -> AGENT NAME, which is the persona except
        for the one documented override. GRAVEYARD_PROBE_EXPECTED is written out in full, and
        deliberately expects the PERSONA for the-ancestor: the override is keyed on the live
        account string and does not fire here. Fitting this site to the account -> persona
        template would have been right by accident and wrong the moment the override key
        changed."""
        for mode in _PROBE_MODES:
            with self.subTest(mode=mode):
                value = self._constant("graveyard", "_USER_TO_AGENT", mode, "dict")
                self.assertEqual(value, GRAVEYARD_PROBE_EXPECTED)

    def test_links_alias_groups_follow_the_manifest(self) -> None:
        """links._ALIAS_GROUPS is NOT the account map. It is NINE groups against EIGHT
        accounts: one per registered persona — {persona, audit role-id, account} — plus the
        hand-carried `orchestrator` word on orchestrator, plus a ninth group for `operator`, a
        non-agent actor with no account. Only the ACCOUNT member of each group is derived, so
        only the account member moves under perturbation; asserting "equals the account map"
        here would be false, and asserting only "contains every account" would pass on a
        constant that had silently lost the non-derivable aliases."""
        expected = sorted(sorted(group) for group in LINKS_PROBE_GROUPS)
        for mode in _PROBE_MODES:
            with self.subTest(mode=mode):
                value = self._constant("links", "_ALIAS_GROUPS", mode, "groups")
                self.assertEqual(value, expected)

    # -- past the constants: the call sites (DEAD DELIVERY, one hop further) --

    def test_consumer_entry_points_follow_the_manifest(self) -> None:
        """The threat the table above does NOT cover: a constant left perfectly derived while
        the function that reads it is rewired to a literal. That is exactly the DEAD DELIVERY
        vector Guard F's F6 was added for, one hop out, and every assertion above is a
        constant.

        Four sites have a pure entry point cheap enough to drive here. Each is checked in BOTH
        directions: a probe account must resolve, and the LIVE account must not — because a
        hardcoded map still answers for the live one, and so does a call site that
        short-circuits on the running host (T-2324). The runner of this suite is a fleet seat,
        so the live half is not hypothetical.

        disk_usage_monitor and session_reset_log/reconcile get no entry-point check: their
        consumers walk real per-account home and state directories. Said plainly rather than
        approximated.
        """
        probe_exocortex = _probe_account("exocortex")
        probe_suborch2 = _probe_account("suborch2")

        with self.subTest(site="graveyard.agent_from_handoff_path"):
            body = (
                "out = {'probe': m.agent_from_handoff_path('/home/%s/HANDOFF.md'),\n"
                "       'live': m.agent_from_handoff_path('/home/exocortex/HANDOFF.md')}\n"
                "print('@@' + json.dumps(dict(kind='calls', value=out), sort_keys=True))\n"
            ) % probe_exocortex
            out = self._run("graveyard", body, "package")["value"]
            self.assertEqual(out["probe"], "mnemosyne")
            self.assertIsNone(
                out["live"],
                "graveyard resolved an agent for an account the manifest it loaded does not "
                "contain — the user->agent map is enumerated somewhere, not derived",
            )

        with self.subTest(site="links.alias_group"):
            body = (
                "out = {'persona': sorted(m.alias_group('mnemosyne')),\n"
                "       'live': sorted(m.alias_group('exocortex'))}\n"
                "print('@@' + json.dumps(dict(kind='calls', value=out), sort_keys=True))\n"
            )
            out = self._run("links", body, "package")["value"]
            self.assertEqual(out["persona"], sorted(["mnemosyne", probe_exocortex]))
            self.assertEqual(
                out["live"],
                ["exocortex"],
                "alias_group() grouped a live account that the manifest it loaded does not "
                "bind — unknown actors must match only themselves",
            )

        with self.subTest(site="mailbox_mcp._caller_persona"):
            # SCRIPT mode: `python3 .../tools/mailbox_mcp.py` is how every seat's MCP server
            # actually starts, and it is the arm of the try/except ImportError that runs.
            body = (
                "import os\n"
                "def who(user):\n"
                "    os.environ['USER'] = user\n"
                "    try:\n"
                "        return m._caller_persona()\n"
                "    except Exception as exc:\n"
                "        return 'RAISED:' + type(exc).__name__\n"
                "out = {'probe': who('%s'), 'live': who('suborch2')}\n"
                "print('@@' + json.dumps(dict(kind='calls', value=out), sort_keys=True))\n"
            ) % probe_suborch2
            out = self._run("mailbox_mcp", body, "script")["value"]
            self.assertEqual(out["probe"], "atlas")
            self.assertEqual(
                out["live"],
                "RAISED:ValueError",
                "mailbox identity resolved for an account the loaded manifest does not bind "
                "— _caller_persona must fail CLOSED on anything it did not read",
            )

        with self.subTest(site="ticket._claiming_persona"):
            body = (
                "import os\n"
                "os.environ['PANTHEON_ACTOR'] = ''\n"
                "def claim(user):\n"
                "    for key in ('LOGNAME', 'USER', 'LNAME', 'USERNAME'):\n"
                "        os.environ[key] = user\n"
                "    return m._claiming_persona()\n"
                "out = {'probe': claim('%s'), 'live': claim('suborch2')}\n"
                "print('@@' + json.dumps(dict(kind='calls', value=out), sort_keys=True))\n"
            ) % probe_suborch2
            out = self._run("ticket", body, "package")["value"]
            self.assertEqual(out["probe"], "atlas")
            self.assertEqual(
                out["live"],
                "suborch2",
                "the WIP cap resolved a persona for an account the loaded manifest does not "
                "bind. The cap fails OPEN by design, so the account must pass through "
                "unchanged; anything else means the map is enumerated, not derived",
            )


class TestEverySnapshotSiteIsGuarded(unittest.TestCase):
    """The enumeration is the weak point, so it is re-derived from the source.

    PER_SITE_GUARDS is a hand-maintained table. A module that binds fleet_accounts() to a
    constant tomorrow gets no coverage and nothing says so — the same "the list was a starting
    point, not a checklist" failure that produced this ticket. This walks tools/*.py and
    re-derives the set. It is how the seventh entry in the table (session_reset_reconcile)
    came to be there; the ticket enumerated six.

    WHAT THE SCAN DETECTS, precisely — a MODULE-SCOPE assignment in a non-test tools/*.py,
    including one inside a module-level if / try / for / while / with and including
    tuple-unpack targets, whose right-hand side CONTAINS ANYWHERE a call to
    account_persona_map / fleet_accounts / account_persona (under any import alias, called by
    name or as an attribute), directly or through a module-scope function that calls one, to a
    recursion depth of 2; plus a module-scope `from <guarded module> import <guarded constant>`
    re-export.

    "CONTAINS ANYWHERE" IS THE T-2334-REVIEW FIX AND IT IS THE WHOLE DIFFERENCE. The first cut
    inspected only the OUTERMOST callee of an assignment whose RHS *was* a Call, so the word
    `dict` was the line between covered and silent: `X = account_persona_map()` was caught and
    `X = dict(account_persona_map())` was not, along with every comprehension, wrapper, BinOp
    and ternary. It also fired a FALSE RED on a listed site rewritten that way, which trains
    readers to loosen the scan. Both measured by the reviewer.

    WHAT IT STILL DOES NOT DETECT — asserted, not asserted-to-be-complete: every shape below
    is planted in test_the_documented_blind_spots_are_really_blind, so widening the scan turns
    that test red and this list cannot silently go stale.
      1. a binding built ACROSS statements rather than in one assignment — `X = {}` then a
         module-level loop filling it, or `def _init(): global X; X = accessor()` called at
         import. The scan matches one statement at a time.
      2. a snapshot inside a `def` or `class` body (those do not run at import; the shape that
         does is (1)).
      3. an accessor reached through more than two levels of module-scope helper.
      4. an accessor reached INDIRECTLY — `getattr(arm, "fleet_accounts")()`, or a module-level
         name bound to the accessor object (`f = fleet_accounts` then `X = f()`).
      5. a walrus used as a bare expression statement, with no assignment target.
      6. a re-export of a constant PER_SITE_GUARDS does not already name (the re-export rule is
         seeded from the table, so it extends coverage but cannot discover a new second hop).
      7. anything outside tools/*.py.

    IT ALSO STOPS AT THIS AXIS. agent_role_map has a SIBLING manifest-derived axis —
    instance_binding() / resolve_instance_role(), persona -> account + restart-config — and
    the same import-time snapshot shape exists on it (context_fill_monitor.AGENT_CONFIG binds
    instance_binding() at import). That is outside this file's subject and gets no coverage
    from anything here; T-2334 scoped itself to the account -> persona axis and this note
    exists so the omission is visible rather than inferred from the accessor list above.

    restart_drain.DRAIN_AGENT_CONFIG USED TO BE NAMED HERE as a second uncovered example and
    no longer is — it CROSSED onto this axis at T-2370. Replacing its hand-carried
    `_DRAIN_PERSONAS` tuple with a derivation made it bind account_persona_map() at import,
    so the scan above began finding it and required the PER_SITE_GUARDS row it now has. Worth
    keeping as a worked example: a site migrates onto this axis by being FIXED, the detector
    catches the migration on the next run, and the guard is not optional at that point.
    """

    _ACCESSORS = frozenset({"account_persona_map", "fleet_accounts", "account_persona"})
    # Bodies that do NOT run at import. The scan descends into every OTHER module-level
    # compound statement (if / try / for / while / with), because those do — mailbox_mcp
    # already binds its accessor inside a module-level try/except, so this is the live shape.
    _NOT_IMPORT_TIME = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    # Header every planted probe module below shares.
    _PLANT_HEADER = "from tools.agent_role_map import account_persona_map, fleet_accounts\n"

    # SHAPES THE SCAN MUST FIND. The first six are shapes the pre-review scan MISSED.
    #
    # And the shape of the control matters as much as its existence: the control this class
    # shipped with planted `def _build(): return tuple(_fa())` / `SEVENTH_LIST = _build()`,
    # putting the wrapper call INSIDE a helper — a path the scan already followed. It proved
    # the scan ran; it could not prove the scan discriminated, and the hole was one word wide.
    # A control must plant the shape you FEAR, not the shape you built for.
    _MUST_FIND: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("bare call", "X = account_persona_map()", ("X",)),
        ("wrapper call", "X = dict(account_persona_map())", ("X",)),
        ("nested wrappers", "X = tuple(sorted(fleet_accounts()))", ("X",)),
        ("comprehension", "X = {a: p for a, p in account_persona_map().items()}", ("X",)),
        ("binop", "X = fleet_accounts() + ('extra',)", ("X",)),
        ("ternary", "X = fleet_accounts() if True else ()", ("X",)),
        ("annotated wrapper", "X: dict = dict(account_persona_map())", ("X",)),
        ("tuple unpack", "X, Y = account_persona_map(), fleet_accounts()", ("X", "Y")),
        (
            "module-level try",
            "try:\n    X = dict(account_persona_map())\nexcept Exception:\n    X = {}\n",
            ("X",),
        ),
        ("one-level helper", "def _b():\n    return dict(account_persona_map())\nX = _b()", ("X",)),
        (
            "two-level helper",
            "def _i():\n    return account_persona_map()\n"
            "def _b():\n    return dict(_i())\nX = _b()\n",
            ("X",),
        ),
    )

    # SHAPES THE SCAN IS DOCUMENTED NOT TO FIND, one per numbered blind spot in the class
    # docstring. Asserted so the list cannot quietly become false: widen the scan and this
    # goes red, forcing the docstring to be corrected in the same edit.
    _MUST_MISS: tuple[tuple[str, str], ...] = (
        ("1 across statements", "X = {}\nfor a, p in account_persona_map().items():\n    X[a] = p\n"),
        ("1 global initializer", "def _init():\n    global X\n    X = account_persona_map()\n_init()\n"),
        ("2 class body", "class C:\n    X = account_persona_map()\n"),
        (
            "3 three-level helper",
            "def _a():\n    return account_persona_map()\n"
            "def _b():\n    return _a()\ndef _c():\n    return _b()\nX = _c()\n",
        ),
        ("4 name alias", "f = account_persona_map\nX = f()\n"),
        (
            "4 getattr",
            "import tools.agent_role_map as arm\nX = getattr(arm, 'fleet_accounts')()\n",
        ),
        ("5 bare walrus", "(X := account_persona_map())\n"),
    )

    @staticmethod
    def _callee(call: ast.Call) -> str:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ""

    @classmethod
    def _module_scope(cls, body):
        """Yield every statement that RUNS AT IMPORT, descending into module-level compound
        statements but never into a def or class body."""
        for stmt in body or []:
            yield stmt
            if isinstance(stmt, cls._NOT_IMPORT_TIME):
                continue
            for field in ("body", "orelse", "finalbody"):
                yield from cls._module_scope(getattr(stmt, field, None))
            for handler in getattr(stmt, "handlers", None) or []:
                yield from cls._module_scope(handler.body)

    @staticmethod
    def _assigned_names(stmt) -> list[str]:
        """Every name *stmt* binds, tuple-unpack targets included."""
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        else:
            return []
        return [
            node.id
            for target in targets
            for node in ast.walk(target)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        ]

    def _scan_module(self, path: Path) -> set[tuple[str, str]]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = path.stem
        statements = list(self._module_scope(tree.body))

        aliases = set(self._ACCESSORS)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for name in node.names:
                    if name.name in self._ACCESSORS and name.asname:
                        aliases.add(name.asname)
        defs = {s.name: s for s in statements if isinstance(s, ast.FunctionDef)}

        def reaches(name: str, depth: int = 0) -> bool:
            if name in aliases:
                return True
            if depth >= 2 or name not in defs:
                return False
            return any(
                isinstance(node, ast.Call) and reaches(self._callee(node), depth + 1)
                for node in ast.walk(defs[name])
            )

        found: set[tuple[str, str]] = set()
        for stmt in statements:
            names = self._assigned_names(stmt)
            value = getattr(stmt, "value", None)
            # WALK the right-hand side. Inspecting only its head is what made `dict(...)` the
            # line between covered and silent — see the class docstring.
            if names and value is not None and any(
                isinstance(node, ast.Call) and reaches(self._callee(node))
                for node in ast.walk(value)
            ):
                found.update((module, name) for name in names)
            if isinstance(stmt, ast.ImportFrom):
                source = (stmt.module or "").split(".")[-1]
                for name in stmt.names:
                    if not name.asname and (source, name.name) in GUARDED_SNAPSHOTS:
                        found.add((module, name.name))
        return found

    def _scan_source(self, source: str, name: str) -> set[tuple[str, str]]:
        planted = Path(tempfile.mkdtemp(prefix="t2334-scan-")) / f"{name}.py"
        self.addCleanup(shutil.rmtree, planted.parent, True)
        planted.write_text(self._PLANT_HEADER + source, encoding="utf-8")
        return self._scan_module(planted)

    def test_the_scan_finds_every_shape_a_snapshot_can_take(self) -> None:
        """The control, re-pointed. A scan that matched nothing would report a clean sweep
        forever, so it is driven against each shape before its emptiness is believed."""
        for label, source, expected in self._MUST_FIND:
            with self.subTest(shape=label):
                self.assertEqual(
                    self._scan_source(source, "planted_site"),
                    {("planted_site", target) for target in expected},
                    f"the scan does not see a snapshot written as {label!r}. A site written "
                    "this way would be absent from the drift check and could then be "
                    "re-enumerated as a literal in total silence.",
                )

    def test_the_documented_blind_spots_are_really_blind(self) -> None:
        """The other direction, and the reason the blind-spot list can be trusted: each shape
        the docstring calls out is planted and asserted MISSED. If someone widens the scan,
        this fails and the list must be corrected in the same edit — which is the structural
        answer to an enumeration that quietly goes stale."""
        for label, source in self._MUST_MISS:
            with self.subTest(shape=label):
                self.assertEqual(
                    self._scan_source(source, "blind_site"),
                    set(),
                    f"the scan now DETECTS {label!r}, which the class docstring lists as a "
                    "blind spot. Good news — remove it from that list.",
                )

    def test_every_row_names_a_per_site_guard_that_exists(self) -> None:
        """The rows are not self-enforcing, so the link is asserted.

        The previous docstring claimed "every guard above is keyed on it". It was not: the
        per-site guards are hand-written methods, and nothing tied a row to a test — measured,
        planting a derived eighth consumer AND adding its row left the suite green with no
        per-site guard in existence, which is exactly what the drift check's failure message
        invites a reader to do. GUARDED_SNAPSHOTS is now DERIVED from PER_SITE_GUARDS so a row
        cannot be written without naming a test, and this asserts the name resolves.
        """
        missing = sorted(
            f"{module}.{constant} -> {test_name}"
            for (module, constant), test_name in PER_SITE_GUARDS.items()
            if not callable(getattr(TestConsumersFollowThePerturbedManifest, test_name, None))
        )
        self.assertEqual(
            missing,
            [],
            "PER_SITE_GUARDS names per-site test(s) that do not exist on "
            f"TestConsumersFollowThePerturbedManifest: {missing}. A row without a test silences "
            "the drift check while asserting nothing about the site.",
        )

    def test_every_module_level_snapshot_in_tools_is_in_the_table(self) -> None:
        found: set[tuple[str, str]] = set()
        for path in sorted((REPO_ROOT / "tools").glob("*.py")):
            if path.name.startswith("test_"):
                continue
            found |= self._scan_module(path)
        self.assertEqual(
            found,
            set(GUARDED_SNAPSHOTS),
            "tools/ module-level snapshots of the account axis have drifted from "
            "PER_SITE_GUARDS. An entry FOUND BUT NOT LISTED is a site with NO "
            "perturbed-manifest coverage: re-enumerating it as a literal would be silent — add "
            "it to PER_SITE_GUARDS *together with the per-site test the row must name*, not on "
            "its own. An entry LISTED BUT NOT FOUND means the site moved or was removed and its "
            "guard is now asserting nothing.",
        )


class TestScriptInvocationKeepsTheDerivation(unittest.TestCase):
    """INTEG (same-process): spawn each repointed module as a REAL subprocess.

    Not a formality. Every one of these files is invoked BOTH as `python3
    tools/<x>.py` (handoff_and_restart.sh, systemd units, hooks) and as a package
    import — and the two put different things on sys.path[0]. A `from tools.X
    import ...` added at module scope works in the test runner and raises
    ModuleNotFoundError under script invocation, which for these files means the
    restart path or the monitor dies at import. In-process assertions cannot see
    that at all.
    """

    CASES = (
        ("tools/session_reset_log.py", "session_reset_log", "KNOWN_ACCOUNTS"),
        ("tools/disk_usage_monitor.py", "disk_usage_monitor", "AGENT_HOMES"),
        ("tools/graveyard.py", "graveyard", "_USER_TO_AGENT"),
        ("tools/links.py", "links", "_ALIAS_GROUPS"),
        ("tools/ticket.py", "ticket", "_OS_USER_PERSONA"),
        # mailbox_mcp is the one repointed module that runs ONLY this way. It is
        # launched as `python3 .../tools/mailbox_mcp.py`, so sys.path[0] is
        # tools/ and the `except ImportError: from agent_role_map import ...`
        # fallback is not a safety net — it is the live production path for
        # every seat's MCP server. Blast radius of getting it wrong is "every
        # seat's mailbox dies at session start", and a green `claude mcp list`
        # is a known false positive for exactly this class (it reports the
        # process launched, not that the tools resolve).
        ("tools/mailbox_mcp.py", "mailbox_mcp", "_USER_TO_PERSONA"),
        # s3star_audit_runner runs BOTH ways and the script form is the one HO
        # actually types every audit cycle (`python3 tools/s3star_audit_runner.py
        # --launch-audit ...`). T-2405 added a module-scope `from tools.agent_role_map
        # import ...` to it, which is precisely the shape that works in the test
        # runner and dies with ModuleNotFoundError under `python3 tools/<x>.py`.
        ("tools/s3star_audit_runner.py", "s3star_audit_runner", "_SNAPSHOT_UNIX_USERS"),
    )

    def test_each_repointed_module_imports_under_script_semantics(self) -> None:
        for rel, modname, symbol in self.CASES:
            with self.subTest(module=rel):
                # sys.path[0] = tools/, exactly as `python3 tools/<x>.py` gives.
                code = (
                    f"import {modname} as m;"
                    f"v = m.{symbol};"
                    "print(sorted(v) if not isinstance(v, list) else 'ok')"
                )
                res = subprocess.run(
                    [sys.executable, "-c", code],
                    cwd=str(REPO_ROOT / "tools"),
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env={**os.environ, "PYTHONPATH": ""},
                )
                if "No module named 'mcp'" in res.stderr:  # pragma: no cover
                    # The MCP SDK is a runtime dep of mailbox_mcp only. Absent it,
                    # the module cannot import for a reason unrelated to this
                    # ticket — skip rather than fail, and NEVER swallow any other
                    # ImportError (a missing `tools` package is the defect this
                    # case exists to catch and must still fail loudly).
                    self.skipTest(f"{rel}: mcp SDK unavailable on this host")
                self.assertEqual(
                    res.returncode,
                    0,
                    f"{rel} fails to import under script-invocation sys.path:\n{res.stderr}",
                )

    def test_links_cli_expands_the_new_seats(self) -> None:
        """The user-visible surface of the F2 defect, through the real CLI."""
        res = subprocess.run(
            [sys.executable, "tools/links.py", "--json", "touched-by", "mnemosyne"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(sorted(payload["group"]), ["exocortex", "mnemosyne"])


_SEED_TOOLING = (REPO_ROOT / "tools" / "verify_seed.py", REPO_ROOT / "tools" / "seed_genericize.py")
_HAS_SEED_TOOLING = all(p.is_file() for p in _SEED_TOOLING)


@unittest.skipUnless(
    _HAS_SEED_TOOLING,
    "seed-build tooling absent — expected in a BUILT SEED, which excludes it (T-2368); "
    "upstream this must never skip",
)
class TestSeedScrubCoversTheAccountClass(unittest.TestCase):
    """INTEG (same-process): the published-seed scrubber + its independent
    checker, both driven as real subprocesses over a tmp tree.

    H2 in its sharpest form: before T-2256 the checker returned "OK — 0
    survivors" on a file containing all three of exocortex / suborch1 /
    suborch2. A scrub-checker that cannot fail is worse than none, because it
    certifies the seed clean.

    SKIPPED, not deleted, when the tooling is absent (T-2368): this file ships
    into the seed but the tools it drives do not, and the seed's own suite must
    stay honest — a skip says "not applicable here", where the raw failures
    would read as a regression. Upstream both files exist, so the guard is
    inert and the tests run; if it ever skips in pantheon-config, that is
    itself the bug. Note the failure shape it replaces is not uniform: the
    missing-script cases would have gone red on a stderr assertion (python
    exits 2 on an unopenable script, which is also this checker's leak-found
    code), while the import cases would have ERRORed.
    """

    ACCOUNTS = ("exocortex", "suborch1", "suborch2")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t2256-seed-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _plant(self, account: str) -> Path:
        p = self.tmp / f"{account}.md"
        p.write_text(
            f"The seat runs on {account}; its checkout is /home/{account}/pantheon-config.\n",
            encoding="utf-8",
        )
        return p

    def _verify(self, path: Path, *, pre_scrub: bool = False):
        argv = [sys.executable, "tools/verify_seed.py"]
        if pre_scrub:
            argv.append("--pre-scrub")
        argv.append(str(path))
        return subprocess.run(
            argv, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120
        )

    def test_planted_account_name_is_caught(self) -> None:
        for account in self.ACCOUNTS:
            with self.subTest(account=account):
                p = self._plant(account)
                res = self._verify(p)
                self.assertEqual(res.returncode, 2, f"checker passed on a planted {account}")
                self.assertIn("account-name", res.stderr)
                self.assertIn("home-path", res.stderr)
                p.unlink()

    def test_scrubbed_text_passes(self) -> None:
        for account in self.ACCOUNTS:
            with self.subTest(account=account):
                p = self._plant(account)
                res = self._verify(p, pre_scrub=True)
                self.assertEqual(res.returncode, 0, res.stderr)
                p.unlink()

    def test_scrub_removes_both_forms(self) -> None:
        """Both forms, not just the path one — a `/home/<acct>` rewrite alone
        leaves every bare mention standing."""
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import seed_genericize as sg  # noqa: E402

        for account in self.ACCOUNTS:
            with self.subTest(account=account):
                scrubbed = sg.scrub(f"on {account} at /home/{account}/x\n")
                self.assertNotIn(account, scrubbed)
                self.assertNotIn("/home/", scrubbed)

    def test_codex_is_deliberately_not_in_the_scrub_class(self) -> None:
        """Guards the recorded operator decision that project codenames are KEPT
        (codex is a public tool). It is the reason this one list stays
        enumerated instead of deriving from fleet_accounts(), so a future
        "make it consistent" sweep must trip here rather than silently
        overturning a decision.

        BOTH SURFACES, deliberately. The first version of this test checked only
        verify_seed, and the falsification sweep caught it: adding a `codex`
        substitution to seed_genericize left it green, because the two files are
        independent by design (that independence is the point — no shared blind
        spot — but it also means a one-sided guard covers one side). The decision
        being protected lives in the scrubber; the checker merely agrees with it.
        """
        sys.path.insert(0, str(REPO_ROOT / "tools"))
        import seed_genericize as sg  # noqa: E402

        # scrubber side: the name must survive genericization
        self.assertIn("codex", sg.scrub("the codex daemon runs an audit\n"))
        # checker side: and must not be reported as a survivor
        p = self.tmp / "codex.md"
        p.write_text("the codex daemon runs an audit\n", encoding="utf-8")
        res = self._verify(p)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
