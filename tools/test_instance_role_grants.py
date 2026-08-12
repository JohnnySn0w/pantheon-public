"""test_instance_role_grants.py — live-tree guards for instance role-key bindings (T-1922).

DELIBERATELY NOT HERMETIC. Sibling tests (e.g. test_repo_registry.py) use tmp dirs to
exercise loader failure modes; these tests instead assert invariants about the REAL
`instance.yaml` / `repos.yaml` checked into this repo. That is the point: the T-1922 defect
was live registry data drifting out of step with the persona registry, which no hermetic
fixture can catch. Precedent: the live-tree regression guard in the T-1309 tempfile-hygiene
lint. There is no CI here — `python3 -m unittest` is the gate.

What went wrong (the class these guard):
  metis / atlas / mnemosyne were registered in agent_role_map.AGENT_ROLE_MAP but had no
  `roles:` entry in instance.yaml. resolve_instance_role() falls back to IDENTITY on a miss,
  so it returned the persona name unchanged, the repos.yaml grants lookup missed, and every
  retask ticket declaring a `repos:` field failed ticket_lint. The failure was silent at the
  resolution layer and only surfaced as a confusing lint error downstream.

Design note — why membership, not output comparison:
  The obvious check is `resolve_instance_role(p) != p`. It is WRONG: it cannot distinguish a
  fall-through from a legitimate role-key that happens to equal the persona name (`recovery` is
  exactly that shape in the audit-id namespace). These tests assert MEMBERSHIP in the loaded
  map instead, which is the property actually wanted.

Scope note — why these do NOT assert grant coverage:
  A guard like "every role-key holds pantheon-config rw" would be RED today for
  peer-daemon-codex and peer-daemon-the-ancestor, and turning it green would mean widening
  grants beyond what T-1922 authorized. A guard that can only pass by expanding a permission
  set is the wrong guard, so these assert role-key EXISTENCE and internal consistency only.

Guard F (T-2316) — why the SYNC guards are not enough on their own:
  Guards D and E assert fallback ≡ manifest in both directions, which T-2256/T-2281
  completed. The side effect is that the loaded maps are now byte-equal to their hardcoded
  fallbacks, so every guard above is satisfiable by the manifest never being READ at all —
  measured: a manifest with `persona:` renamed to `agent:` parses fine, yields zero personas,
  silently falls back, and leaves all ten guards green. Guard F is the missing half: it
  asserts the manifest was READ and that what it says reaches the PUBLIC API. See the
  KNOWN CONSEQUENCE block in instance.yaml's header, which records this blind spot, its
  measured vector, and — since T-2316 — Guard F as the thing that closed it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_YAML = REPO_ROOT / "instance.yaml"

# The three bindings ruled by the T-1922 HO ruling (ORG-043 seats + ORG-042 second head).
T1922_BINDINGS = {
    "metis": "sub-orchestrator-1",
    "atlas": "sub-orchestrator-2",
    "mnemosyne": "second-head",
}


def _load_yaml(path: Path):
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestEveryRegisteredPersonaHasARoleKey(unittest.TestCase):
    """Guard A (class guard): the defect class, not just its three instances."""

    def test_every_persona_in_agent_role_map_has_an_instance_role_key(self) -> None:
        from tools.agent_role_map import AGENT_ROLE_MAP, _PERSONA_TO_INSTANCE_ROLE

        missing = sorted(set(AGENT_ROLE_MAP) - set(_PERSONA_TO_INSTANCE_ROLE))
        self.assertEqual(
            missing,
            [],
            "persona(s) registered in AGENT_ROLE_MAP with no instance.yaml `roles:` entry: "
            f"{missing}. resolve_instance_role() will fall through to identity for these, so "
            "any ticket targeting them that declares a `repos:` field will fail ticket_lint "
            "with a confusing 'no grant' error. Add a roles: entry in instance.yaml.",
        )


class TestT1922Bindings(unittest.TestCase):
    """Guard B: the specific bindings this ticket ruled, end to end through lint."""

    def test_bindings_resolve_exactly_as_ruled(self) -> None:
        from tools.agent_role_map import resolve_instance_role

        for persona, expected_key in sorted(T1922_BINDINGS.items()):
            with self.subTest(persona=persona):
                self.assertEqual(resolve_instance_role(persona), expected_key)

    def test_each_seat_passes_lint_for_pantheon_config_rw(self) -> None:
        """The actual T-1922 symptom: this call returned a 'no grant' error string."""
        from tools.repo_registry import load_repo_registry
        from tools.ticket_lint import validate_repos_field

        registry = load_repo_registry(REPO_ROOT)
        self.assertIsNotNone(registry, "repos.yaml failed to load from the live tree")

        for persona in sorted(T1922_BINDINGS):
            with self.subTest(persona=persona):
                self.assertIsNone(
                    validate_repos_field(["pantheon-config:rw"], persona, registry),
                    f"{persona} cannot declare pantheon-config:rw on a ticket",
                )


class TestNoOrphanRoleKeysInGrants(unittest.TestCase):
    """Guard C: catch a typo'd or retired role-key in repos.yaml before it silently denies."""

    def test_every_role_key_used_in_repos_yaml_is_defined_in_instance_yaml(self) -> None:
        instance = _load_yaml(REPO_ROOT / "instance.yaml")
        repos = _load_yaml(REPO_ROOT / "repos.yaml")

        defined = set((instance or {}).get("roles", {}) or {})

        used: set[str] = set()
        for entry in (repos or {}).get("repos", []) or []:
            used.update((entry.get("grants") or {}).keys())
        for entry in (repos or {}).get("always_available", []) or []:
            used.update(entry.get("agents") or [])

        orphans = sorted(used - defined)
        self.assertEqual(
            orphans,
            [],
            f"role-key(s) used in repos.yaml but not defined in instance.yaml `roles:`: "
            f"{orphans}. A grant keyed on an undefined role-key is dead config — it can never "
            "match, so it silently denies instead of granting.",
        )


class TestFallbackMapStaysInSync(unittest.TestCase):
    """Guard D: enforce the sync contract agent_role_map.py states in a comment.

    Equality, in both directions. This docstring previously read "Subset, not equality:
    instance.yaml legitimately carries personas the fallback omits (codex, the-ancestor)"
    — that described the state of the map, and was not a reason for it. T-2281 measured
    the consequence and completed the mirror: neither DISAGREEMENT nor ABSENCE is
    acceptable, because both make behavior depend on whether the manifest happened to
    load.
    """

    def test_fallback_never_contradicts_instance_yaml(self) -> None:
        from tools.agent_role_map import (
            _FALLBACK_PERSONA_TO_INSTANCE_ROLE,
            _PERSONA_TO_INSTANCE_ROLE,
        )

        conflicts = {
            persona: (fallback_key, _PERSONA_TO_INSTANCE_ROLE[persona])
            for persona, fallback_key in _FALLBACK_PERSONA_TO_INSTANCE_ROLE.items()
            if persona in _PERSONA_TO_INSTANCE_ROLE
            and _PERSONA_TO_INSTANCE_ROLE[persona] != fallback_key
        }
        self.assertEqual(
            conflicts,
            {},
            "fallback map disagrees with instance.yaml (persona: (fallback, manifest)): "
            f"{conflicts}. Resolution would then depend on whether the manifest loaded.",
        )

    def test_fallback_personas_all_exist_in_instance_yaml(self) -> None:
        from tools.agent_role_map import (
            _FALLBACK_PERSONA_TO_INSTANCE_ROLE,
            _PERSONA_TO_INSTANCE_ROLE,
        )

        stale = sorted(set(_FALLBACK_PERSONA_TO_INSTANCE_ROLE) - set(_PERSONA_TO_INSTANCE_ROLE))
        self.assertEqual(
            stale, [], f"fallback names persona(s) absent from instance.yaml roles:: {stale}"
        )

    def test_every_instance_yaml_persona_exists_in_the_fallback(self) -> None:
        """THE OTHER DIRECTION (T-2281) — the same missing half T-2256 closed for the
        sibling binding map, still open here.

        The two guards above assert fallback ⊆ manifest: no stale entries, no
        contradictions. Neither asserted manifest ⊆ fallback, so a persona bound in
        instance.yaml and simply ABSENT from the fallback passed all of it. codex and
        the-ancestor were in exactly that state, making the role-key fallback a 6-of-8
        mirror. The practical failure is not someone editing an entry — it is someone
        adding a `roles:` entry to instance.yaml and not touching agent_role_map.py.
        In a manifest-less environment the new persona's resolve_instance_role() then
        falls through to identity, the repos.yaml grants lookup misses, and ticket_lint
        reports a confusing "no grant" — the T-1922 defect this whole file exists for.
        """
        from tools.agent_role_map import (
            _FALLBACK_PERSONA_TO_INSTANCE_ROLE,
            _PERSONA_TO_INSTANCE_ROLE,
        )

        missing = sorted(set(_PERSONA_TO_INSTANCE_ROLE) - set(_FALLBACK_PERSONA_TO_INSTANCE_ROLE))
        self.assertEqual(
            missing,
            [],
            f"instance.yaml binds persona(s) the fallback mirror omits: {missing}. In any "
            "environment without a staged manifest, resolve_instance_role() falls through to "
            "identity for exactly these, so their repos.yaml grants silently deny. Add them to "
            "_FALLBACK_PERSONA_TO_INSTANCE_ROLE with the same role-key instance.yaml uses.",
        )


class TestBindingFallbackStaysInSync(unittest.TestCase):
    """Guard E (T-2076): the monitor-binding fallback (account + restart-config) must
    not contradict instance.yaml. context_fill_monitor/restart_drain build their
    per-agent config from these bindings, so a fallback that disagrees with the
    manifest would make the LIVE monitor config depend on whether instance.yaml
    happened to load — the exact silent-drift class Guard D guards for role-keys."""

    def test_binding_fallback_never_contradicts_instance_yaml(self) -> None:
        from tools.agent_role_map import _FALLBACK_PERSONA_TO_BINDING, _PERSONA_TO_BINDING

        conflicts = {
            persona: (fb, _PERSONA_TO_BINDING[persona])
            for persona, fb in _FALLBACK_PERSONA_TO_BINDING.items()
            if persona in _PERSONA_TO_BINDING and _PERSONA_TO_BINDING[persona] != fb
        }
        self.assertEqual(
            conflicts,
            {},
            "monitor-binding fallback disagrees with instance.yaml (persona: (fallback, "
            f"manifest)): {conflicts}. The fleet monitor config would then depend on whether "
            "the manifest loaded. Keep _FALLBACK_PERSONA_TO_BINDING in sync with the roles' "
            "account + restart_service/restart_mode/restart_pane.",
        )

    def test_binding_fallback_personas_all_exist_in_instance_yaml(self) -> None:
        from tools.agent_role_map import _FALLBACK_PERSONA_TO_BINDING, _PERSONA_TO_BINDING

        stale = sorted(set(_FALLBACK_PERSONA_TO_BINDING) - set(_PERSONA_TO_BINDING))
        self.assertEqual(
            stale, [], f"binding fallback names persona(s) absent from instance.yaml roles:: {stale}"
        )

    def test_every_instance_yaml_persona_exists_in_the_binding_fallback(self) -> None:
        """THE OTHER DIRECTION (T-2256, metis's reviewer finding).

        The two guards above assert fallback ⊆ manifest — no stale entries, no
        contradictions. Neither asserted manifest ⊆ fallback, so a persona bound
        in instance.yaml and simply ABSENT from the fallback passed all of it.
        codex and the-ancestor were in exactly that state from T-2076 until
        T-2256: harmless while the manifest loads, and a silent 6-of-8 fleet the
        moment it does not. A one-directional completeness check is the defect
        class this arc exists to close, so it should not survive in the guard
        that enforces the canonical map itself.
        """
        from tools.agent_role_map import _FALLBACK_PERSONA_TO_BINDING, _PERSONA_TO_BINDING

        missing = sorted(set(_PERSONA_TO_BINDING) - set(_FALLBACK_PERSONA_TO_BINDING))
        self.assertEqual(
            missing,
            [],
            f"instance.yaml binds persona(s) the fallback mirror omits: {missing}. In any "
            "environment without a staged manifest, account_persona_map()/fleet_accounts() "
            "would silently under-cover the fleet by exactly these.",
        )


class TestSupervisorField(unittest.TestCase):
    """T-2658: specialist is marked control-plane-supervised; T-2684 adds recovery.
    Every other bound persona stays unmarked (implicit "bespoke" default,
    additive-only)."""

    def test_specialist_is_control_plane(self) -> None:
        from tools.agent_role_map import instance_binding

        self.assertEqual(instance_binding("specialist").get("supervisor"), "control-plane")

    def test_recovery_is_control_plane(self) -> None:
        """T-2684: recovery's control-plane cutover (T-2590) is mechanically
        complete; this makes the declaration match, closing the T-2671/
        T-2682 landmine (restart_drain/cfm both keyed on this field and
        missed recovery, targeting the disabled bespoke unit)."""
        from tools.agent_role_map import instance_binding

        self.assertEqual(instance_binding("recovery").get("supervisor"), "control-plane")

    def test_orchestrator_has_no_supervisor_key(self) -> None:
        """Additive-only: seats not cut over must not gain a stray key that
        could accidentally flip restart_drain.py / context_fill_monitor.py
        onto the control-plane branch for a still-bespoke seat."""
        from tools.agent_role_map import instance_binding

        self.assertNotIn(
            "supervisor",
            instance_binding("orchestrator"),
            "orchestrator must stay bespoke (no supervisor key) until its own cutover",
        )

    def test_control_plane_consume_service_derives_from_persona(self) -> None:
        from tools.agent_role_map import control_plane_consume_service

        self.assertEqual(
            control_plane_consume_service("specialist"),
            "pantheon-persona-consume@specialist.service",
        )
        self.assertEqual(
            control_plane_consume_service("recovery"),
            "pantheon-persona-consume@recovery.service",
        )


# ---------------------------------------------------------------------------
# Guard F (T-2316) — the manifest was actually LOADED.
#
# Independent parses of instance.yaml, written HERE rather than imported from
# agent_role_map. That duplication is the point: these are the WITNESS. If they
# imported the loader's own extraction they would agree with it by construction
# and could never disagree — the H2 shape this ticket exists to close. If a
# manifest schema migration is ever made deliberately, both implementations must
# be updated, and the second one is the reason someone notices.
# ---------------------------------------------------------------------------

# Mirrors agent_role_map._BINDING_RESTART_KEYS. Deliberately re-stated, same reason.
# T-2658: "supervisor" joined the loader-side tuple; mirrored here too.
_RESTART_KEYS = ("restart_service", "restart_mode", "restart_pane", "supervisor")


def _manifest_role_key_map(instance) -> dict[str, str]:
    """persona → instance role-key, read straight out of a parsed instance.yaml."""
    out: dict[str, str] = {}
    for role_key, binding in ((instance or {}).get("roles") or {}).items():
        if isinstance(binding, dict):
            persona = binding.get("persona")
            if isinstance(persona, str) and persona:
                out[persona] = role_key
    return out


def _manifest_companion_units(binding: dict) -> str:
    """persona's companion_units, encoded EXACTLY as agent_role_map does --
    space-joined ``unit=session`` tokens (T-2648). Independently
    re-implemented (not imported), same reason as the rest of this witness:
    see the module-level comment above _RESTART_KEYS."""
    raw = binding.get("companion_units")
    if not isinstance(raw, list):
        return ""
    pairs = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        unit, session = item.get("unit"), item.get("tmux_session")
        if isinstance(unit, str) and unit and isinstance(session, str) and session:
            pairs.append(f"{unit}={session}")
    return " ".join(pairs)


def _manifest_binding_map(instance) -> dict[str, dict]:
    """persona → {account[, restart_*, companion_units]}, read straight out of
    a parsed instance.yaml."""
    out: dict[str, dict] = {}
    for _role_key, binding in ((instance or {}).get("roles") or {}).items():
        if not isinstance(binding, dict):
            continue
        persona, account = binding.get("persona"), binding.get("account")
        if not (isinstance(persona, str) and persona and isinstance(account, str) and account):
            continue
        entry = {"account": account}
        for key in _RESTART_KEYS:
            value = binding.get(key)
            if isinstance(value, str) and value:
                entry[key] = value
        companion_units = _manifest_companion_units(binding)
        if companion_units:
            entry["companion_units"] = companion_units
        out[persona] = entry
    return out


def _import_agent_role_map_against(manifest_root: Path):
    """Import a fresh copy of agent_role_map.py rooted at *manifest_root*.

    agent_role_map resolves its manifest as `Path(__file__).resolve().parent.parent /
    "instance.yaml"`, and `.resolve()` follows symlinks — so redirecting it means a real
    copy of the module source at <manifest_root>/tools/agent_role_map.py. The module has
    no intra-repo imports, so one file is the whole copy. Loaded under a unique name and
    NOT registered in sys.modules, so the real module stays untouched.
    """
    import importlib.util
    import shutil

    pkg = manifest_root / "tools"
    pkg.mkdir(parents=True, exist_ok=True)
    target = pkg / "agent_role_map.py"
    shutil.copyfile(REPO_ROOT / "tools" / "agent_role_map.py", target)

    spec = importlib.util.spec_from_file_location("_t2316_probe_agent_role_map", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The probe suffixes, applied to every role-key and every account in the perturbed COPY, so
# NOTHING in a result can coincide with either fallback: anything that comes back unsuffixed
# did not come from the file. Named constants rather than inline strings because T-2334's
# consumer-hop guard (tools/test_account_persona_map.py) builds its expected values from the
# same suffix — two independent spellings of "perturbed" could drift apart while both suites
# still read as one story.
_PROBE_ROLE_KEY_SUFFIX = "-T2316PROBE"
_PROBE_ACCOUNT_SUFFIX = "-t2316probe"


def _write_perturbed_manifest(root: Path) -> dict:
    """Write a perturbed COPY of the live instance.yaml to <root>/instance.yaml; return it.

    The live manifest is only READ here. Every role-key gets _PROBE_ROLE_KEY_SUFFIX and every
    account _PROBE_ACCOUNT_SUFFIX.
    """
    import yaml

    instance = _load_yaml(INSTANCE_YAML)
    roles = (instance or {}).get("roles") or {}
    if not roles:
        raise AssertionError("live instance.yaml has no roles: stanza to perturb")

    perturbed_roles = {}
    for role_key, binding in roles.items():
        entry = dict(binding) if isinstance(binding, dict) else binding
        if isinstance(entry, dict) and isinstance(entry.get("account"), str):
            entry["account"] = entry["account"] + _PROBE_ACCOUNT_SUFFIX
        perturbed_roles[f"{role_key}{_PROBE_ROLE_KEY_SUFFIX}"] = entry
    perturbed = dict(instance)
    perturbed["roles"] = perturbed_roles

    (root / "instance.yaml").write_text(
        yaml.safe_dump(perturbed, allow_unicode=True), encoding="utf-8"
    )
    return perturbed


def _module_against_perturbed_manifest(root: Path):
    """Write a perturbed COPY of the live instance.yaml under *root* and import against it.

    Returns (module, perturbed_manifest). Shared by F2 and F6 so the single perturbation is
    asserted at both the constant and the public-API level.

    T-2334's consumer-hop guard reuses _write_perturbed_manifest but NOT this importer, and
    the reason is worth stating here: agent_role_map has no intra-repo imports, so one copied
    file is the whole module. A CONSUMER does `from tools.agent_role_map import ...`, which in
    this process resolves out of sys.modules to the REAL module and the REAL manifest — so the
    consumer hop needs a materialised tools/ tree and a subprocess, not a second importer here.
    """
    perturbed = _write_perturbed_manifest(root)
    return _import_agent_role_map_against(root), perturbed


class TestManifestWasActuallyLoaded(unittest.TestCase):
    """Guard F (T-2316): instance.yaml was READ, and what it says reaches the PUBLIC API.

    Every other guard in this file compares a loaded map to the fallback map. Since
    T-2256/T-2281 completed both mirrors those two are identical, so all of them pass
    when the manifest is never read — a guard suite satisfiable by the thing it guards
    being dead.

    SIX tests, grouped by THREAT — by how the manifest actually stops reaching a caller,
    not by what an assertion happens to cover. They are a COVER, not a partition: F2
    overlaps F1/F3/F4 by construction, because perturbing the manifest end-to-end fails
    under most breakages. What IS true, and is the useful claim, is that each threat below
    has at least one test that detects it ALONE, named after it. That column is the thing
    to keep honest when editing.

      manifest-side drift   the schema migrates (`persona:` → `agent:`, a `binding:`
                            nesting level) with role-keys intact, so it still parses and
                            Guard C still passes, but yields no personas.
                            → F1 (loaded vs an independent parse). Also F2/F6.
      loader-side death     someone simplifies/short-circuits _load_instance_*, an early
                            return slips in, or the fail-safe `except` swallows a newly
                            real error (yaml gone from the env, a renamed `roles:` lookup).
                            Manifest healthy, loader dead. → F3 is the only HERMETIC
                            detector (it needs no live manifest). F2/F6 also catch it.
      module-level clobber  the constants are reassigned from the fallback after load.
                            Values still correct, mechanism dead. → F2, and F6. F1 and F3
                            both agree with a clobbered constant.
      path drift            instance.yaml moves to config/ while the loader keeps the old
                            path — the ASYMMETRIC case. → F4.
      DEAD DELIVERY         the manifest is read into a variable that agent_role_map's own
                            PUBLIC API does not consume. A resolver rewired to the fallback,
                            or the account→persona map re-enumerated as a literal instead of
                            derived, leaves every loaded constant genuinely manifest-sourced
                            and every accessor answering from the fallback. → F6 ALONE.
                            F1-F4 all assert constants, and constants are not what callers
                            call. SCOPE: F6 reaches agent_role_map's accessors and stops
                            there. A CONSUMER that snapshots one of them at import time —
                            ticket.py's _OS_USER_PERSONA does exactly this — is a further hop
                            F6 does not follow, and nothing here should be read as covering
                            it. That next hop is guarded by
                            test_account_persona_map.TestConsumersFollowThePerturbedManifest
                            (T-2334), which shares this file's _write_perturbed_manifest.

    DEAD DELIVERY is the vector this class originally MISSED, and it is the cheapest way
    to get there. Measured on the first cut of this guard (T-2316 reviewer, 2026-07-29):
    rewiring only resolve_instance_role() and instance_binding() to the fallback left this
    file at 15 passed and test_account_persona_map at 30 passed, FULLY GREEN, with the
    loaded maps still genuinely following the file. Same for replacing _invert_bindings()
    with an enumerated literal — the twenty-first hand-carried fleet list agent_role_map's
    own docstring spends a paragraph forbidding — after which adding a seat to instance.yaml
    put it in _PERSONA_TO_BINDING (F2 green) while account_persona() returned None for its
    account and fleet_accounts() still counted 8. That is T-2255's measured casualty, a hook
    covering 3 of 6 accounts while reporting success, reproduced with the axis's own guard
    suite green. Hence F6, and hence the correction to this docstring: the earlier version
    claimed the split was complete and that no vector covered another. Both were false, and
    a wrong completeness claim is worse than none.

    HOST-COUPLING (T-2324 class) is a second way to be green for the wrong reason, and it is
    orthogonal to the table above: not "the manifest stopped being read" but "the test would
    have passed anyway, here". This suite runs on one seat, so F6 carries three assertions
    against it — see F6's docstring for which edit each one fires on. Measured (T-2316
    rebound): a host-coupled account_persona(), short-circuiting on getpass.getuser(), fails
    this class on suborch2/claude/claude-research and PASSES on ci-runner from byte-identical
    files, while the code as written is identical across all four.

    Two cases stay OUT of scope because they are already caught: a DELETED and a
    SYNTACTICALLY CORRUPT manifest both make Guard C raise on _load_yaml. A PARTIAL
    persona drop is caught by Guard A, because a partial parse is returned as-is rather
    than falling back — the irony this ticket closes is that partial breakage was
    detected and total breakage was not.

    On the skips: F1/F2/F4/F6 need the live manifest and skip explicitly without it, because
    a manifest-less environment is a SUPPORTED one — the fallbacks exist precisely so it
    resolves correctly, and a guard that cries wolf there gets deleted, taking the gap's
    only detector with it. Two things keep the skip from being a hole (H1/T-2249: a skip
    is the cheapest way to make a green test unfalsifiable). First, F3 and F5 are hermetic
    and run everywhere, so even off-box the suite keeps one falsifiable liveness detector.
    Second, in THIS repo the skip condition cannot fire quietly: removing instance.yaml
    makes Guard C ERROR on _load_yaml, loudly. (Reviewer-confirmed across six states,
    T-2316: every state that fires the skip also fails Guard C on the same path.)
    """

    @unittest.skipUnless(INSTANCE_YAML.exists(), "no live instance.yaml — see class docstring")
    def test_loaded_maps_equal_an_independent_parse_of_instance_yaml(self) -> None:
        """F1 — the cheap form the ticket asks for: loaded map == independent parse.

        EQUALITY, not "at least one persona came from the manifest". The weaker form
        survives a manifest that has half-migrated, and equality is already the contract
        Guards D and E enforce against the fallback — asserting a weaker one here would
        make the manifest the only surface allowed to disagree with itself.
        """
        from tools.agent_role_map import _PERSONA_TO_BINDING, _PERSONA_TO_INSTANCE_ROLE

        instance = _load_yaml(INSTANCE_YAML)

        self.assertEqual(
            _PERSONA_TO_INSTANCE_ROLE,
            _manifest_role_key_map(instance),
            "the role-key map agent_role_map exposes is not what instance.yaml says. Either "
            "the manifest's schema changed under the loader (roles: still keyed correctly but "
            "no longer yielding personas — that parses, so Guard C stays green) and the loader "
            "silently fell back, or the loader's extraction changed without this witness being "
            "updated. Both are the T-2316 defect: correct values, dead mechanism.",
        )
        self.assertEqual(
            _PERSONA_TO_BINDING,
            _manifest_binding_map(instance),
            "the binding map agent_role_map exposes is not what instance.yaml says — the fleet "
            "monitors, account_persona() and fleet_accounts() are all running on fallback "
            "values that are correct only until someone edits the manifest.",
        )

    @unittest.skipUnless(INSTANCE_YAML.exists(), "no live instance.yaml — see class docstring")
    def test_module_level_maps_follow_a_perturbed_manifest(self) -> None:
        """F2 — the liveness check instance.yaml's own header describes, executed.

        The header says: perturb a role-key, watch resolve_instance_role() follow the file.
        That is the only probe that still discriminates now that fallback ≡ manifest, and it
        is a temporary EDIT, which is why it was left as a manual instruction. Done here on a
        COPY in a tmp dir, against a fresh import of the module source rooted there, so it
        runs in the suite and the live manifest is never touched.

        This is the only assertion here that survives the constants being reassigned from the
        fallback after load: F1 and F3 both read a surface that a clobber leaves looking right.

        SCOPE, stated because the first cut of this class overclaimed it: this asserts the
        CONSTANTS of a FRESH COPY of the module, imported against a perturbed manifest. It says
        nothing about the live module (which cannot be re-imported against a different manifest
        without editing the real file), and nothing about whether any CALLER reads those
        constants — that is F6, and the gap between the two is where DEAD DELIVERY lived.
        """
        import tempfile

        from tools.agent_role_map import (
            _FALLBACK_PERSONA_TO_BINDING,
            _FALLBACK_PERSONA_TO_INSTANCE_ROLE,
        )

        with tempfile.TemporaryDirectory(prefix="t2316-liveness-") as tmp:
            module, perturbed = _module_against_perturbed_manifest(Path(tmp))

            expected_roles = _manifest_role_key_map(perturbed)
            expected_bindings = _manifest_binding_map(perturbed)

            self.assertEqual(
                module._PERSONA_TO_INSTANCE_ROLE,
                expected_roles,
                "agent_role_map's role-key map did NOT follow a perturbed instance.yaml. The "
                "manifest is not being read: the map is coming from somewhere else (the "
                "fallback, or a constant reassigned after load) and every sync guard in this "
                "file will stay green while it does.",
            )
            self.assertNotEqual(
                module._PERSONA_TO_INSTANCE_ROLE,
                _FALLBACK_PERSONA_TO_INSTANCE_ROLE,
                "loading a perturbed manifest produced the fallback map verbatim",
            )
            self.assertEqual(
                module._PERSONA_TO_BINDING,
                expected_bindings,
                "agent_role_map's binding map did NOT follow a perturbed instance.yaml — same "
                "defect as above on the account/restart-config axis the fleet monitors consume.",
            )
            self.assertNotEqual(
                module._PERSONA_TO_BINDING,
                _FALLBACK_PERSONA_TO_BINDING,
                "loading a perturbed manifest produced the fallback bindings verbatim",
            )

    @unittest.skipUnless(INSTANCE_YAML.exists(), "no live instance.yaml — see class docstring")
    def test_public_api_follows_a_perturbed_manifest(self) -> None:
        """F6 — DEAD DELIVERY: the manifest reaches agent_role_map's PUBLIC API.

        F1-F4 all assert module constants, which nothing outside this module reads: ticket_lint
        calls resolve_instance_role(), the fleet monitors call instance_binding(), and the
        T-2255 de-duplication calls account_persona()/account_persona_map()/fleet_accounts().
        Because fallback ≡ manifest, an accessor rewired to the fallback is invisible to every
        value comparison in this repo — measured green across this file (15) AND the account
        axis's own suite (30) before this test existed.

        SCOPE, and it is one hop, not the whole chain: this asserts the ACCESSORS follow the
        manifest. It does NOT assert that agent_role_map's consumers follow the accessors. A
        consumer that snapshots an accessor at import time into a module constant of its own —
        ticket.py does `_OS_USER_PERSONA = _account_persona_map()` — can be re-enumerated as a
        literal with this test still green, because this test never imports it. Read nothing
        here as covering it: the consumer hop is asserted by
        test_account_persona_map.TestConsumersFollowThePerturbedManifest (T-2334), one site at
        a time, against the perturbation _write_perturbed_manifest builds for both files.

        The account→persona axis is the sharp case and gets its own assertions. It is DERIVED
        by _invert_bindings(_PERSONA_TO_BINDING), and agent_role_map's docstring forbids
        re-enumerating it precisely because a hand-carried list is how it goes stale. Nothing
        asserted the derivation: replacing it with a literal passed everything, and then adding
        a seat to instance.yaml landed it in _PERSONA_TO_BINDING (F2 green) while
        account_persona() returned None for its account — T-2255's "deploy hook covering 3 of
        6 accounts while reporting success", exactly.

        Every value below carries a -T2316PROBE / -t2316probe suffix that exists only in the
        perturbed copy, so an accessor answering with anything unsuffixed answered from the
        fallback.

        NOT SATISFIABLE BY THE HOST (T-2324 class, folded in on the rebound). This suite runs
        on exactly one seat — suborch2/atlas — and an assertion that happened to exercise the
        axis through the RUNNING account would pass for a second, independent reason: where it
        ran. That is the measured shape of T-2324, where test_deploy_hook.py passed 73/73 on
        recovery and failed 5 on orchestrator from byte-identical files, because it hardcoded "claude" and
        asserted a cross-account path that does not exist when "claude" is the runner. Same
        family as the tautology one level up: a witness that shares something with the accused,
        the machine instead of the code.

        Three named assertions below, each of which fires on the edit it names:
          - `visited` vs the manifest — the per-persona/per-account loops must reach every
            entry, so narrowing one to the runner's seat is a failure and not a shortcut;
          - assertNotIn(runner, expected_accounts) — nothing asserted resolvable may be a real
            OS account, the runner's included;
          - assertIn(runner, stale_accounts) when the runner is a fleet account — the host's
            own identity must be among the accounts required to resolve to None.
        """
        import getpass
        import tempfile

        from tools.agent_role_map import _FALLBACK_PERSONA_TO_BINDING

        with tempfile.TemporaryDirectory(prefix="t2316-delivery-") as tmp:
            module, perturbed = _module_against_perturbed_manifest(Path(tmp))

            expected_roles = _manifest_role_key_map(perturbed)
            expected_bindings = _manifest_binding_map(perturbed)
            expected_accounts = {b["account"]: p for p, b in expected_bindings.items()}
            self.assertTrue(expected_roles and expected_accounts, "perturbation yielded nothing")

            runner = getpass.getuser()
            self.assertGreater(
                len(expected_accounts),
                1,
                "the axis is being proved on a single account — one seat's answer is not the "
                "axis (T-2324: a guard green because of where it ran).",
            )
            self.assertNotIn(
                runner,
                expected_accounts,
                f"the running OS account {runner!r} is among the accounts this test asserts "
                "resolvable. Every perturbed account must carry the -t2316probe suffix so no "
                "assertion here can be satisfied by the host it runs on.",
            )

            # Every accessor call made below records what it visited, and the visited sets are
            # asserted against the manifest afterwards. Without that, "checked" is a property of
            # the loop bounds and nothing objects when a loop is narrowed — measured by the
            # T-2316 reviewer: reducing these loops to the runner's own seat left this file at
            # 16 passed, which is precisely the host-coupling this test claims to exclude.
            visited: dict[str, set[str]] = {
                "resolve_instance_role": set(),
                "instance_binding": set(),
                "account_persona": set(),
            }

            for persona, role_key in sorted(expected_roles.items()):
                with self.subTest(persona=persona, accessor="resolve_instance_role"):
                    visited["resolve_instance_role"].add(persona)
                    self.assertEqual(
                        module.resolve_instance_role(persona),
                        role_key,
                        "resolve_instance_role() did not follow the manifest. This is the "
                        "accessor ticket_lint calls for every repos: field — the loaded map "
                        "can be perfectly manifest-sourced and this still be wired to the "
                        "fallback, which is invisible to every other guard in this file.",
                    )

            for persona, binding in sorted(expected_bindings.items()):
                with self.subTest(persona=persona, accessor="instance_binding"):
                    visited["instance_binding"].add(persona)
                    self.assertEqual(
                        module.instance_binding(persona),
                        binding,
                        "instance_binding() did not follow the manifest — this is what "
                        "context_fill_monitor and restart_drain build their per-agent config "
                        "from, so the fleet would be running on fallback accounts/services.",
                    )

            self.assertEqual(
                module.account_persona_map(),
                expected_accounts,
                "account_persona_map() did not follow the manifest. If it is ENUMERATED rather "
                "than derived from the bindings, adding a role to instance.yaml lands in "
                "_PERSONA_TO_BINDING and never reaches this map — the T-2255 casualty, with "
                "every value-comparison guard green.",
            )
            self.assertEqual(
                module.fleet_accounts(),
                tuple(sorted(expected_accounts)),
                "fleet_accounts() did not follow the manifest — a silently under-covered fleet "
                "is the exact shape T-2256 measured (a hook covering 3 of 6 accounts while "
                "reporting success).",
            )
            for account, persona in sorted(expected_accounts.items()):
                with self.subTest(account=account, accessor="account_persona"):
                    visited["account_persona"].add(account)
                    self.assertEqual(module.account_persona(account), persona)

            self.assertEqual(
                visited,
                {
                    "resolve_instance_role": set(expected_roles),
                    "instance_binding": set(expected_bindings),
                    "account_persona": set(expected_accounts),
                },
                "the per-persona/per-account loops above did not visit every entry in the "
                "manifest. One seat's answer is not the axis: narrowing any of these loops — "
                "to the runner's own seat especially — makes this test pass for where it ran "
                "rather than for what the manifest says (T-2324).",
            )

            # The other direction, and the one an enumerated literal fails: accounts that exist
            # ONLY in the live manifest must be unknown to a module loaded against the perturbed
            # copy. A hardcoded map still answers for them — and so does an accessor that
            # short-circuits on the running account (the T-2324 host-coupling shape), which is
            # why the runner's real account is deliberately inside this set.
            stale_accounts = sorted(
                b["account"] for b in _FALLBACK_PERSONA_TO_BINDING.values() if b.get("account")
            )
            if runner in {b.get("account") for b in _FALLBACK_PERSONA_TO_BINDING.values()}:
                self.assertIn(
                    runner,
                    stale_accounts,
                    "the runner is a fleet account but is not being checked for must-be-unknown "
                    "— the one account a host-coupled accessor would answer for is the one this "
                    "test would then skip.",
                )
            for stale_account in stale_accounts:
                with self.subTest(account=stale_account, direction="must-be-unknown"):
                    self.assertIsNone(
                        module.account_persona(stale_account),
                        f"account_persona({stale_account!r}) resolved against a manifest that "
                        "does not contain it — the map is enumerated somewhere, not derived, or "
                        "the accessor short-circuits on the running account.",
                    )

    def test_loaders_read_the_manifest_they_are_handed(self) -> None:
        """F3 — hermetic, and the one that runs in a manifest-less environment.

        A synthetic manifest sharing no persona, role-key or account with either fallback:
        anything the loaders return that resembles the fallback is a loader that did not read
        its argument. Also pins _load_instance_bindings' "only the keys present" contract —
        restart_mode/restart_pane are deliberately absent below.
        """
        import tempfile

        from tools.agent_role_map import _load_instance_bindings, _load_instance_role_map

        manifest = (
            "instance:\n"
            "  id: t2316-synthetic\n"
            "roles:\n"
            "  probe-role-alpha:\n"
            "    persona: t2316-probe-persona\n"
            "    account: t2316-probe-account\n"
            "    restart_service: t2316-probe.service\n"
        )
        with tempfile.TemporaryDirectory(prefix="t2316-synthetic-") as tmp:
            path = Path(tmp) / "instance.yaml"
            path.write_text(manifest, encoding="utf-8")

            self.assertEqual(
                _load_instance_role_map(path),
                {"t2316-probe-persona": "probe-role-alpha"},
                "_load_instance_role_map ignored the manifest it was handed",
            )
            self.assertEqual(
                _load_instance_bindings(path),
                {
                    "t2316-probe-persona": {
                        "account": "t2316-probe-account",
                        "restart_service": "t2316-probe.service",
                    }
                },
                "_load_instance_bindings ignored the manifest it was handed",
            )

    @unittest.skipUnless(INSTANCE_YAML.exists(), "no live instance.yaml — see class docstring")
    def test_module_resolves_the_live_repo_root_manifest(self) -> None:
        """F4 — the loader is pointed at the same file this suite reads.

        SCOPE, corrected (the first cut oversold this): a wholesale `src/` layout move does
        NOT reach here, because agent_role_map.py and this file both live in tools/ and move
        together, so REPO_ROOT tracks _MODULE_DIR.parent and this stays green — Guard C errors
        on the missing manifest instead. What F4 actually catches is ASYMMETRIC drift: the
        manifest moves to config/ (or is renamed) and one of the two paths is updated without
        the other. That case is otherwise invisible, because the loader falls back forever and
        returns values that still match.
        """
        from tools.agent_role_map import _INSTANCE_YAML

        self.assertEqual(
            Path(_INSTANCE_YAML).resolve(),
            INSTANCE_YAML.resolve(),
            f"agent_role_map reads {_INSTANCE_YAML}, this suite reads {INSTANCE_YAML}. If they "
            "are not the same file the loader is falling back permanently and nothing else in "
            "this file can tell.",
        )

    def test_loaders_fall_back_rather_than_raise_when_the_manifest_is_absent(self) -> None:
        """F5 — pins the fail-safe contract the skips above depend on.

        The fallbacks are a FEATURE: _load_instance_* swallow everything so one bad manifest
        edit cannot brick every ticket op, the mailbox MCP and every fleet monitor at import.
        T-2316 adds detection, it does not change that contract — so the contract is asserted
        here, where a future reader can see that the absent-manifest path is designed rather
        than merely tolerated, and where "fix" it by raising breaks a test on the way past.
        """
        import tempfile

        from tools.agent_role_map import (
            _FALLBACK_PERSONA_TO_BINDING,
            _FALLBACK_PERSONA_TO_INSTANCE_ROLE,
            _load_instance_bindings,
            _load_instance_role_map,
        )

        with tempfile.TemporaryDirectory(prefix="t2316-absent-") as tmp:
            missing = Path(tmp) / "instance.yaml"
            self.assertFalse(missing.exists())

            self.assertEqual(_load_instance_role_map(missing), _FALLBACK_PERSONA_TO_INSTANCE_ROLE)
            self.assertEqual(_load_instance_bindings(missing), _FALLBACK_PERSONA_TO_BINDING)


if __name__ == "__main__":
    unittest.main()
