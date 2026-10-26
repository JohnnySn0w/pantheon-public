"""test_instance_role_grants.py — live-tree guards for instance role-key bindings (T-1922).

DELIBERATELY NOT HERMETIC. Sibling tests (e.g. test_repo_registry.py) use tmp dirs to
exercise loader failure modes; these tests instead assert invariants about the REAL
`instance.yaml` / `repos.yaml` checked into this repo. That is the point: the T-1922 defect
was live registry data drifting out of step with the persona registry, which no hermetic
fixture can catch. Precedent: the live-tree regression guard in the T-1309 tempfile-hygiene
lint. There is no CI here — `python3 -m unittest` is the gate.

What went wrong (the class these guard):
  mason / artisan / archivist were registered in agent_role_map.AGENT_ROLE_MAP but had no
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

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_YAML = REPO_ROOT / "instance.yaml"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from tools._snapshot_site_scan import SnapshotSiteScanner  # noqa: E402

# The three bindings ruled by the T-1922 HO ruling (ORG-043 seats + ORG-042 second head);
# role-keys carry the operator's T-3807 ratified roles vocabulary.
T1922_BINDINGS = {
    "mason": "engineering-orchestrator-1",
    "artisan": "engineering-orchestrator-2",
    "archivist": "head-memory-orchestrator",
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
        """THE OTHER DIRECTION (T-2256, mason's reviewer finding).

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
    """T-2658: specialist is marked control-plane-supervised. T-2684 briefly marked
    recovery control-plane too, but T-3082 reverted that (2026-08-23): recovery's
    control-plane cutover (T-2590) covers only the consume plane -- the
    interactive session still lives on the bespoke claude-telegram-fallback.
    service, so the T-2684 marking made cfm/restart_drain derive a restart
    target (the oneshot consume unit) that cannot cycle recovery's session.
    T-3026 (the interactive-session cutover) will re-add the marking once
    that move actually lands. T-2953 R31 briefly marked orchestrator control-plane
    too, but T-3098 reverted that (2026-08-23) for the identical reason --
    orchestrator's interactive session still lives on the bespoke claude-telegram.
    service pre-cutover; T-2953's full cutover will re-add the marking once
    it lands. Every other bound persona stays unmarked (implicit "bespoke"
    default, additive-only)."""

    def test_specialist_is_control_plane(self) -> None:
        from tools.agent_role_map import instance_binding

        self.assertEqual(instance_binding("specialist").get("supervisor"), "control-plane")

    def test_recovery_is_control_plane_post_t3026(self) -> None:
        """T-3026 (2026-09-29): recovery's interactive session moved onto the
        control plane (runtime controller + consume timer), so its binding now
        claims control-plane, as specialist's does. Pre-cutover this asserted absence
        (T-3082); the marker landed only after Gate B and verification passed."""
        from tools.agent_role_map import instance_binding

        self.assertEqual(instance_binding("recovery").get("supervisor"), "control-plane")

    def test_orchestrator_is_control_plane_post_t2953(self) -> None:
        """T-2953 (2026-10-24): orchestrator's interactive session moved onto the
        control plane (runtime controller + consume timer), so its binding now
        claims control-plane, as recovery's and specialist's do. Pre-cutover this asserted
        absence (T-3098); the marker landed only after Gate B and verification passed."""
        from tools.agent_role_map import instance_binding

        self.assertEqual(instance_binding("orchestrator").get("supervisor"), "control-plane")

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


_EXPECTED_POLICY_SKIP_RE = re.compile(r"^[a-z0-9_-]+$")


def _manifest_expected_policy_skip(binding: dict) -> str | None:
    """persona's expected_policy_skip, validated EXACTLY as agent_role_map
    does -- a reason-match token, not a free-text label (T-3210).
    Independently re-implemented (not imported), same reason as the rest of
    this witness: see the module-level comment above _RESTART_KEYS."""
    raw = binding.get("expected_policy_skip")
    if not isinstance(raw, str) or not raw:
        return None
    return raw if _EXPECTED_POLICY_SKIP_RE.match(raw) else None


def _manifest_binding_map(instance) -> dict[str, dict]:
    """persona → {account[, restart_*, companion_units, expected_policy_skip]},
    read straight out of a parsed instance.yaml."""
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
        expected_policy_skip = _manifest_expected_policy_skip(binding)
        if expected_policy_skip:
            entry["expected_policy_skip"] = expected_policy_skip
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
        on exactly one seat — suborch2/artisan — and an assertion that happened to exercise the
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


# ---------------------------------------------------------------------------
# T-2365 — the SIBLING-axis CONSUMER hop.
#
# Guard F above proves instance.yaml reaches agent_role_map's accessors —
# resolve_instance_role() / instance_binding() / the account-map trio — and
# stops there, and says so (see F6's DEAD-DELIVERY scope note). T-2334
# (test_account_persona_map.TestConsumersFollowThePerturbedManifest) carried the
# ACCOUNT axis one hop further, to the module constants that snapshot
# account_persona_map() / fleet_accounts() at import. Nothing carried the
# instance_binding() SIBLING axis that far. Two fleet monitors snapshot it AT
# IMPORT TIME:
#     context_fill_monitor.AGENT_CONFIG    = _build_agent_config()   (6 entries)
#     restart_drain.DRAIN_AGENT_CONFIG     = _build_drain_config()   (grantees)
# Re-enumerate either as a hand-written literal and the manifest genuinely stops
# reaching the monitor with every suite green — the monitor then reads jsonl from,
# restarts the service of, and pokes the pane of a seat the manifest no longer
# says it should. That is the T-2313 silent-skip class with a restart attached.
#
# WHY A SUBPROCESS AND NOT _module_against_perturbed_manifest. That importer works
# for agent_role_map ALONE because it has no intra-repo imports, so a single copied
# file is the whole module. These two consumers do `from tools.agent_role_map
# import instance_binding`, which in THIS process resolves out of sys.modules to
# the REAL module reading the REAL manifest — copying one file cannot redirect it.
# So this mirrors T-2334's mechanism exactly: a materialised tools/ tree plus a
# PERTURBED copy of instance.yaml (via _write_perturbed_manifest, shared so there
# is one definition of "perturbed"), each consumer imported in a subprocess rooted
# there with PYTHONPATH cleared. Not a divergent mechanism — the same one, one axis
# over. (The subprocess _run helper is re-stated here rather than imported from
# test_account_persona_map, which already imports _write_perturbed_manifest FROM
# this file; importing back the other way would be a circular import.)
#
# TWO SITES, DIFFERENT SHAPES, PER-CATEGORY ASSERTIONS — NOT one table twice.
# WITHIN a single entry the fields split three ways (artisan's T-2365 analysis), and
# only two of the three may follow a perturbed manifest:
#   1. BINDING-DERIVED, ACCOUNT-CARRYING (sudo_user, jsonl_dir, handoff_file,
#      restart_bin, supervise_script prefix) — the perturbed account MUST appear and
#      the live account must NOT. This is the only category that proves liveness,
#      because _write_perturbed_manifest perturbs the ACCOUNT (and role-key), and a
#      re-enumerated literal carries the OLD, unsuffixed account.
#   2. BINDING-DERIVED, NON-ACCOUNT (restart_service, restart_mode,
#      restart_target_pane, supervisor) — from the binding but NOT perturbed, so
#      asserting them proves shape/derivation, not liveness. Checked, but never
#      leaned on for the "manifest reached it" claim, and asserted to NOT carry the
#      probe suffix.
#   3. MONITOR-OWNED TUNING (context_window, model_pin, observation_only, …) — keyed
#      by PERSONA in _MONITOR_TUNING, not in the manifest at all. Asserted to be
#      UNCHANGED under perturbation: if tuning ever started following the manifest,
#      persona-keyed monitor config would have been wired into the identity manifest,
#      its own defect. A positive assertion, not a tolerated gap.
#
# restart_drain.DRAIN_AGENT_CONFIG's ACCOUNT hop is ALSO guarded by T-2334
# (test_restart_drain_agent_config_follows_the_manifest) — it crossed onto the
# account axis at T-2370 when it began binding account_persona_map() at import. It
# is re-guarded here on the SIBLING axis it also consumes (instance_binding() for
# restart_service/mode/pane), so the axis this file owns has its own falsifiable
# detector rather than depending on a test in the account-axis file. AGENT_CONFIG
# is the genuinely-unguarded consumer this ticket exists for.
# ---------------------------------------------------------------------------

# Category-3 keys: monitor-owned tuning, keyed by persona in _MONITOR_TUNING, never
# in instance.yaml. Re-stated here (not imported from context_fill_monitor) so a
# change to the module's tuning set does not silently redefine what this guard
# treats as "must NOT follow the manifest".
_MONITOR_TUNING_KEYS = frozenset(
    {"context_window", "model_pin", "observation_only", "remediate", "hook_check_via_recovery", "use_tombstone"}
)


def _consumer_probe_body(symbol: str) -> str:
    """Child-process source that encodes the dict-of-dict constant *symbol* (bound to
    `m`) as JSON, str()-ing every leaf so Path / int / bool values survive the hop."""
    return (
        f"value = getattr(m, {symbol!r})\n"
        "enc = {k: {kk: str(vv) for kk, vv in v.items()} for k, v in value.items()}\n"
        "print('@@' + json.dumps(enc, sort_keys=True))\n"
    )


class TestFleetMonitorConsumersFollowThePerturbedManifest(unittest.TestCase):
    """T-2365 — instance_binding() reaches the fleet monitors' import-time snapshots.

    See the block comment above for the mechanism and the three field categories.
    One test per site, each with its own expected values written for that site's
    shape; the mechanics (perturbed tree, subprocess probe) are shared, the
    expectations are not.
    """

    maxDiff = None
    _PROBE_MODES = ("package", "script")

    @classmethod
    def setUpClass(cls) -> None:
        import shutil
        import tempfile

        if not INSTANCE_YAML.exists():  # pragma: no cover - manifest-less env
            raise unittest.SkipTest(
                "no live instance.yaml to perturb — a manifest-less environment is supported "
                "(the fallbacks exist for it) and this guard has nothing to compare against"
            )
        cls.root = Path(tempfile.mkdtemp(prefix="t2365-consumers-"))
        cls.addClassCleanup(shutil.rmtree, cls.root, True)
        (cls.root / "tools").mkdir(parents=True, exist_ok=True)
        # REAL copies of every tools/*.py, not symlinks: agent_role_map locates the manifest
        # via Path(__file__).resolve(), which follows a symlink straight back to the live tree
        # and would un-do the redirect. A consumer may import a sibling, so copy them all.
        for src in (REPO_ROOT / "tools").glob("*.py"):
            shutil.copyfile(src, cls.root / "tools" / src.name)
        cls.perturbed = _write_perturbed_manifest(cls.root)
        # persona -> {account, restart_service?, restart_mode?, restart_pane?, supervisor?},
        # read from the LIVE file by this file's OWN independent witness parse (NOT the accessor
        # the consumers call). Expected perturbed accounts are this witness's account + the probe
        # suffix, which is exactly what _write_perturbed_manifest wrote — so a consumer answering
        # with an unsuffixed (live) account fails, and the expected value is not laundered through
        # the code under test.
        cls.witness = _manifest_binding_map(_load_yaml(INSTANCE_YAML))

    def _run(self, module: str, symbol: str, mode: str) -> dict:
        """Import *module* from the perturbed tree in a subprocess and decode its constant."""
        import json
        import os
        import subprocess
        import sys

        prelude = (
            f"import tools.{module} as m\n" if mode == "package" else f"import {module} as m\n"
        )
        cwd = self.root if mode == "package" else self.root / "tools"
        res = subprocess.run(
            [sys.executable, "-c", "import json\n" + prelude + _consumer_probe_body(symbol)],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=180,
            # PYTHONPATH cleared so the LIVE repo cannot leak onto sys.path and serve the real
            # manifest — that would make every assertion below green for the wrong tree.
            env={**os.environ, "PYTHONPATH": ""},
        )
        self.assertEqual(
            res.returncode,
            0,
            f"{module}.{symbol} [{mode}] failed to import against the perturbed tree:\n{res.stderr}",
        )
        payloads = [ln for ln in res.stdout.splitlines() if ln.startswith("@@")]
        self.assertEqual(
            len(payloads), 1, f"{module}.{symbol} [{mode}] emitted no probe payload:\n{res.stdout}"
        )
        return json.loads(payloads[0][2:])

    def _probe_account(self, persona: str) -> str:
        return self.witness[persona]["account"] + _PROBE_ACCOUNT_SUFFIX

    def _live_home_segment(self, persona: str) -> str:
        return f"/home/{self.witness[persona]['account']}/"

    def test_context_fill_monitor_agent_config_follows_the_manifest(self) -> None:
        """context_fill_monitor.AGENT_CONFIG — 6 entries, three field categories.

        The PERSONA SET is monitor-owned (which seats cfm watches, from _MONITOR_TUNING),
        NOT manifest-derived, so it is asserted stable under perturbation rather than pinned
        to the manifest — pinning it would assert a property the manifest does not own. What
        the manifest DOES own is every entry's account, which must arrive perturbed in the
        account-carrying paths (category 1) and be absent from them as the live string.

        mason/artisan carry ONLY sudo_user + jsonl_dir + tuning (no restart config); archivist
        has handoff_file but no restart_bin (observation_only suppresses the fire path);
        orchestrator/specialist/recovery carry the full set. jsonl_dir's project slug is
        -home-<account>-pantheon-config for a control-plane seat (specialist) and -home-<account>
        otherwise — asserted exactly, per the witness's supervisor field.
        """
        from tools.context_fill_monitor import AGENT_CONFIG as live_config

        for mode in self._PROBE_MODES:
            with self.subTest(mode=mode):
                config = self._run("context_fill_monitor", "AGENT_CONFIG", mode)

                self.assertGreater(
                    len(config),
                    1,
                    "AGENT_CONFIG built one entry or none against the perturbed tree — one "
                    "seat's answer is not the fleet (T-2324: green because of where it ran).",
                )
                # The persona set is monitor-owned and manifest-independent: perturbing the
                # manifest's accounts must not add or drop a watched seat.
                self.assertEqual(
                    set(config),
                    set(live_config),
                    "perturbing instance.yaml changed WHICH seats cfm watches — the persona set "
                    "comes from _MONITOR_TUNING, not the manifest, and must not move.",
                )

                for persona, entry in config.items():
                    self.assertIn(
                        persona,
                        self.witness,
                        f"AGENT_CONFIG carries {persona!r}, absent from instance.yaml roles:",
                    )
                    pa = self._probe_account(persona)
                    live_home = self._live_home_segment(persona)
                    control_plane = self.witness[persona].get("supervisor") == "control-plane"
                    # T-3838: bespoke_relaunch seats (mason, artisan post-T-3846
                    # arm) also slugify their live-session dir to
                    # -home-<account>-pantheon-config, not the plain
                    # -home-<account>, because they respawn with
                    # cwd=<account>/pantheon-config. The MONITOR_TUNING is
                    # code-side state (context_fill_monitor._MONITOR_TUNING),
                    # not manifest state, so the witness reads it directly
                    # rather than inferring from the (empty) supervisor field.
                    # This mirrors the code's own branch at
                    # tools/context_fill_monitor.py:852-856.
                    from tools.context_fill_monitor import _MONITOR_TUNING
                    bespoke_relaunch = bool(
                        _MONITOR_TUNING.get(persona, {}).get("bespoke_relaunch")
                    )

                    # -- category 1: binding-derived, ACCOUNT-CARRYING (the liveness proof) --
                    self.assertEqual(
                        entry["sudo_user"],
                        pa,
                        f"{persona}: sudo_user did not follow the perturbed manifest account",
                    )
                    slug = (
                        f"-home-{pa}-pantheon-config"
                        if (control_plane or bespoke_relaunch)
                        else f"-home-{pa}"
                    )
                    self.assertEqual(
                        entry["jsonl_dir"],
                        f"/home/{pa}/.claude/projects/{slug}",
                        f"{persona}: jsonl_dir did not follow the perturbed account (cfm would "
                        "watch the wrong project dir and never fire)",
                    )
                    if "handoff_file" in entry:
                        self.assertEqual(entry["handoff_file"], f"/home/{pa}/HANDOFF.md")
                    if "restart_bin" in entry:
                        # T-3838: armed-bespoke_relaunch seats (artisan post-T-3846
                        # arm, and mason at its own arm) use pantheon-seat-relaunch.sh
                        # rather than the standard handoff_and_restart.sh. The
                        # bespoke path is gated in cfm at tools/context_fill_monitor.py:900
                        # on `bespoke_relaunch AND not has_restart AND not observation_only`,
                        # so mirror it here to preserve the same manifest→consumer parity.
                        expected_restart_bin = (
                            f"/home/{pa}/pantheon-config/tools/pantheon-seat-relaunch.sh"
                            if bespoke_relaunch
                            else f"/home/{pa}/pantheon-config/tools/handoff_and_restart.sh"
                        )
                        self.assertEqual(entry["restart_bin"], expected_restart_bin)
                    if "supervise_script" in entry:
                        # The script NAME is monitor-owned; only the /home/<account>/ prefix is
                        # binding-derived, so prefix + not-live is the precise statement here.
                        self.assertTrue(
                            entry["supervise_script"].startswith(f"/home/{pa}/.local/bin/"),
                            f"{persona}: supervise_script prefix did not follow the account: "
                            f"{entry['supervise_script']}",
                        )
                    for field in ("sudo_user", "jsonl_dir", "handoff_file", "restart_bin",
                                  "supervise_script"):
                        if field in entry:
                            self.assertNotIn(
                                live_home,
                                entry[field],
                                f"{persona}: {field} carries the LIVE account {live_home!r} "
                                "against a manifest that does not contain it — the account is "
                                "a hand-literal, not derived",
                            )

                    # -- category 2: binding-derived, NON-account — must NOT carry the probe --
                    # T-3838: bespoke_relaunch seats compute restart_target_pane +
                    # relaunch_session + relaunch_scope_unit from `{account}-main`
                    # via _MONITOR_TUNING (session name IS account-driven for the
                    # bespoke tmux shape), so those specific fields DO follow the
                    # account under perturbation on those seats. The category-2
                    # assumption ("pane is monitor-owned, not account-derived")
                    # holds for control-plane + standard-restart-service seats
                    # only. Narrow the checked field-set on bespoke_relaunch.
                    non_account_fields = ("restart_service", "restart_mode", "supervisor")
                    if not bespoke_relaunch:
                        non_account_fields = non_account_fields + ("restart_target_pane",)
                    for field in non_account_fields:
                        if field in entry:
                            self.assertNotIn(
                                _PROBE_ACCOUNT_SUFFIX,
                                entry[field],
                                f"{persona}: {field} absorbed the account perturbation — it is "
                                "derived from restart_service/mode/pane/supervisor, not account",
                            )

                    # -- category 3: monitor-owned tuning — must be UNCHANGED under perturbation --
                    for key in _MONITOR_TUNING_KEYS:
                        if key in entry:
                            self.assertNotIn(
                                _PROBE_ACCOUNT_SUFFIX,
                                entry[key],
                                f"{persona}: tuning key {key!r} followed the manifest — "
                                "persona-keyed monitor config must never depend on it",
                            )
                            self.assertEqual(
                                entry[key],
                                str(live_config[persona][key]),
                                f"{persona}: tuning key {key!r} differs between the real and the "
                                "perturbed manifest — tuning is manifest-independent and must not",
                            )

    def test_restart_drain_agent_config_follows_the_manifest(self) -> None:
        """restart_drain.DRAIN_AGENT_CONFIG — a DIFFERENT shape: keyed by the manifest's
        restart_service grantees only, and carrying a persona-keyed restart_lockfile that
        AGENT_CONFIG has no analogue of.

        The KEY SET is manifest-derived here (unlike AGENT_CONFIG's monitor-owned set): exactly
        the personas instance.yaml grants restart_service, computed from the witness. archivist
        is named explicitly — it is T-2370's measured casualty, dropped by a hand-carried
        _DRAIN_PERSONAS tuple that contradicted the manifest and had its tombstones quarantined
        as an unknown agent. handoff_file/restart_bin carry the perturbed account (liveness);
        restart_lockfile is persona-keyed and asserted exactly; restart_service/mode/pane are
        binding-derived-but-unperturbed and must not absorb the probe.
        """
        grantees = {p for p, b in self.witness.items() if "restart_service" in b}
        self.assertIn(
            "archivist",
            grantees,
            "the live manifest no longer grants archivist restart_service — the witness for "
            "this test is stale (or T-2370's fix regressed in instance.yaml)",
        )

        for mode in self._PROBE_MODES:
            with self.subTest(mode=mode):
                config = self._run("restart_drain", "DRAIN_AGENT_CONFIG", mode)

                self.assertEqual(
                    set(config),
                    grantees,
                    "DRAIN_AGENT_CONFIG's key set is not exactly the manifest's restart_service "
                    "grantees — a persona the manifest says is restartable is uncovered, or one "
                    "it does not is carried (T-2370's shape).",
                )
                self.assertIn(
                    "archivist",
                    config,
                    "the drain no longer covers archivist — T-2370's exact defect, its restart "
                    "tombstones quarantined as an unknown agent",
                )

                for persona, entry in config.items():
                    pa = self._probe_account(persona)
                    live_home = self._live_home_segment(persona)
                    control_plane = self.witness[persona].get("supervisor") == "control-plane"

                    # -- category 1: account-carrying paths (the liveness proof) --
                    self.assertEqual(entry["handoff_file"], f"/home/{pa}/HANDOFF.md")
                    self.assertEqual(
                        entry["restart_bin"],
                        f"/home/{pa}/pantheon-config/tools/handoff_and_restart.sh",
                    )
                    self.assertNotIn(
                        live_home,
                        entry["handoff_file"] + entry["restart_bin"],
                        f"{persona}: a home-rooted path carries the LIVE account against a "
                        "manifest that does not contain it — the account is enumerated, not derived",
                    )

                    # restart_lockfile is PERSONA-keyed, not account-derived: exact, unperturbed.
                    self.assertEqual(
                        entry["restart_lockfile"], f"/tmp/handoff_and_restart_{persona}.lock"
                    )

                    # -- category 2: binding-derived, NON-account — checked, never leaned on --
                    self.assertEqual(entry["restart_mode"], self.witness[persona].get("restart_mode", "user"))
                    if "restart_pane" in self.witness[persona]:
                        self.assertEqual(
                            entry["restart_target_pane"], self.witness[persona]["restart_pane"]
                        )
                    self.assertNotIn(
                        _PROBE_ACCOUNT_SUFFIX,
                        entry["restart_service"],
                        f"{persona}: restart_service absorbed the account perturbation — it is "
                        "the manifest's service string (or the derived consume unit), not the account",
                    )
                    expected_service = (
                        f"pantheon-persona-consume@{persona}.service"
                        if control_plane
                        else self.witness[persona]["restart_service"]
                    )
                    self.assertEqual(
                        entry["restart_service"],
                        expected_service,
                        f"{persona}: restart_service is not the manifest's service string",
                    )

    def _assert_agent_config_reexport(self, module: str) -> None:
        """A module that does `from context_fill_monitor import AGENT_CONFIG` RE-EXPORTS a
        guarded snapshot (T-3573; the account axis's session_reset_reconcile is the same shape).
        Re-enumerating that import as a hand-literal detaches it from the manifest while cfm's
        own AGENT_CONFIG stays perfectly derived — so the sibling drift-scan flags the re-export
        as its OWN site, and this asserts it still tracks the source under a perturbed manifest."""
        for mode in self._PROBE_MODES:
            with self.subTest(module=module, mode=mode):
                reexport = self._run(module, "AGENT_CONFIG", mode)
                source = self._run("context_fill_monitor", "AGENT_CONFIG", mode)
                self.assertEqual(
                    reexport,
                    source,
                    f"{module}.AGENT_CONFIG diverged from context_fill_monitor.AGENT_CONFIG "
                    "against a perturbed manifest — it is re-enumerated as a literal, not "
                    "re-exported, so the manifest no longer reaches it.",
                )
                self.assertTrue(
                    reexport and any(
                        _PROBE_ACCOUNT_SUFFIX in entry.get("sudo_user", "")
                        for entry in reexport.values()
                    ),
                    f"{module}.AGENT_CONFIG carries no perturbed account — 'equals the source' "
                    "must not be satisfiable by two identical LIVE literals.",
                )

    def test_channel_liveness_monitor_agent_config_reexports_the_manifest(self) -> None:
        self._assert_agent_config_reexport("channel_liveness_monitor")

    def test_hook_allowlist_lint_agent_config_reexports_the_manifest(self) -> None:
        self._assert_agent_config_reexport("hook_allowlist_lint")


# ---------------------------------------------------------------------------
# T-3573 — fleet_health.FLEET: a sibling-axis import-time snapshot the enumeration missed.
#
# The T-2365 block above enumerated TWO instance_binding() consumers
# (context_fill_monitor.AGENT_CONFIG, restart_drain.DRAIN_AGENT_CONFIG). The
# automated scan below (TestEverySiblingSnapshotSiteIsGuarded) re-derives the set
# from the source rather than trusting that enumeration — and it finds THREE more:
# two AGENT_CONFIG re-exports (channel_liveness_monitor, hook_allowlist_lint,
# guarded on the consumer class above) and this direct one —
# fleet_health.FLEET binds instance_binding() at import through the module-scope
# helper _liveness_services(persona, bespoke), which reads the persona's
# `supervisor` marker to choose the consume TIMER (control-plane seat) over the
# bespoke unit list. That is the exact "the enumeration was a starting point, not
# a checklist" drift the scan exists to catch (same worked example as the account
# axis's session_reset_reconcile, its seventh entry).
#
# WHY A DIFFERENT PERTURBATION. AGENT_CONFIG/DRAIN_AGENT_CONFIG carry
# account-derived paths, so _write_perturbed_manifest (which suffixes the ACCOUNT
# and role-key) moves them and proves liveness. FLEET carries NO account-derived
# field — its `user`/`session` are hand-literals and its only manifest hop is the
# `supervisor` marker, which _write_perturbed_manifest does NOT touch. So the
# account perturbation leaves FLEET byte-identical and cannot tell a derived FLEET
# from a re-enumerated literal. This guard perturbs the SUPERVISOR marker instead:
# it flips a currently-bespoke seat (orchestrator) to control-plane in a copied manifest
# and asserts FLEET's services for it become the derived consume timer. A FLEET
# re-enumerated as a literal keeps the bespoke unit and reddens here — which is the
# drift (a completed cutover flipping a seat's supervisor while fleet_health goes
# on probing the retired bespoke unit and reporting a false verdict).
#
# Same mechanism family as T-2365: a materialised tools/ tree + a perturbed copy of
# instance.yaml, fleet_health imported in a subprocess rooted there with PYTHONPATH
# cleared, run in both package and script sys.path shapes.
# ---------------------------------------------------------------------------

_FLEET_HEALTH_FLIP_PERSONA = "orchestrator"  # bespoke pre-T-2953; a control-plane flip is observable


class TestFleetHealthFleetFollowsTheSupervisorMarker(unittest.TestCase):
    """T-3573 — fleet_health.FLEET follows instance.yaml's `supervisor` marker.

    See the block comment above for why this perturbs `supervisor` rather than the
    account. One subprocess per sys.path shape; the expectation is the derived
    consume-timer name, which a hand-literal FLEET cannot reproduce under the flip.
    """

    maxDiff = None
    _PROBE_MODES = ("package", "script")

    @classmethod
    def setUpClass(cls) -> None:
        import shutil
        import tempfile

        if not INSTANCE_YAML.exists():  # pragma: no cover - manifest-less env
            raise unittest.SkipTest(
                "no live instance.yaml to perturb — a manifest-less environment is supported "
                "(the fallbacks exist for it) and this guard has nothing to compare against"
            )
        cls.witness = _manifest_binding_map(_load_yaml(INSTANCE_YAML))
        # The flip is only meaningful if the seat is bespoke in the LIVE manifest.
        if cls.witness.get(_FLEET_HEALTH_FLIP_PERSONA, {}).get("supervisor") == "control-plane":
            raise unittest.SkipTest(
                f"{_FLEET_HEALTH_FLIP_PERSONA} is already control-plane in the live manifest — "
                "pick a seat that is bespoke pre-cutover so flipping supervisor is observable"
            )
        cls.root = Path(tempfile.mkdtemp(prefix="t3573-fleet-health-"))
        cls.addClassCleanup(shutil.rmtree, cls.root, True)
        (cls.root / "tools").mkdir(parents=True, exist_ok=True)
        # REAL copies (not symlinks): agent_role_map locates the manifest via
        # Path(__file__).resolve(), which follows a symlink back to the live tree and
        # would un-do the redirect. A consumer may import a sibling, so copy them all.
        for src in (REPO_ROOT / "tools").glob("*.py"):
            shutil.copyfile(src, cls.root / "tools" / src.name)
        cls._write_supervisor_flipped_manifest(cls.root, _FLEET_HEALTH_FLIP_PERSONA)

    @staticmethod
    def _write_supervisor_flipped_manifest(root: Path, flip_persona: str) -> None:
        """Copy the live instance.yaml under *root*, marking *flip_persona*'s role
        `supervisor: control-plane`. Accounts/role-keys are left UNtouched — this
        guard isolates the supervisor hop FLEET actually reads."""
        import yaml

        instance = _load_yaml(INSTANCE_YAML)
        roles = (instance or {}).get("roles") or {}
        flipped = None
        new_roles = {}
        for role_key, binding in roles.items():
            entry = dict(binding) if isinstance(binding, dict) else binding
            if isinstance(entry, dict) and entry.get("persona") == flip_persona:
                entry["supervisor"] = "control-plane"
                flipped = role_key
            new_roles[role_key] = entry
        if flipped is None:
            raise AssertionError(
                f"live instance.yaml has no role bound to persona {flip_persona!r} to flip"
            )
        perturbed = dict(instance)
        perturbed["roles"] = new_roles
        (root / "instance.yaml").write_text(
            yaml.safe_dump(perturbed, allow_unicode=True), encoding="utf-8"
        )

    def _run(self, mode: str) -> dict:
        """Import fleet_health from the flipped tree in a subprocess; return FLEET."""
        import json
        import os
        import subprocess

        prelude = (
            "import tools.fleet_health as m\n" if mode == "package" else "import fleet_health as m\n"
        )
        cwd = self.root if mode == "package" else self.root / "tools"
        body = (
            "import json\n"
            + prelude
            + "print('@@' + json.dumps(m.FLEET, sort_keys=True, default=str))\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", body],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ, "PYTHONPATH": ""},
        )
        self.assertEqual(
            res.returncode,
            0,
            f"fleet_health.FLEET [{mode}] failed to import against the flipped tree:\n{res.stderr}",
        )
        payloads = [ln for ln in res.stdout.splitlines() if ln.startswith("@@")]
        self.assertEqual(
            len(payloads), 1, f"fleet_health.FLEET [{mode}] emitted no probe payload:\n{res.stdout}"
        )
        return json.loads(payloads[0][2:])

    def test_fleet_health_fleet_follows_the_supervisor_marker(self) -> None:
        flip = _FLEET_HEALTH_FLIP_PERSONA
        flip_timer = f"pantheon-persona-consume@{flip}.timer"
        # A seat that is control-plane in the LIVE (unflipped) manifest — its timer
        # must already be derived, proving the derivation is not unique to the flip.
        live_cp = sorted(
            p for p, b in self.witness.items() if b.get("supervisor") == "control-plane"
        )
        # A seat that stays bespoke under the flip — must NOT be timerised, proving
        # `supervisor` specifically (not a blanket rule) drives the choice.
        still_bespoke = next(
            (
                p
                for p, b in self.witness.items()
                if p != flip and b.get("supervisor") != "control-plane" and p in {
                    "recovery", "orchestrator", "specialist", "codex", "mason", "artisan", "archivist"
                }
            ),
            None,
        )

        for mode in self._PROBE_MODES:
            with self.subTest(mode=mode):
                fleet = self._run(mode)

                self.assertGreater(
                    len(fleet), 1,
                    "FLEET built one entry or none — one seat's answer is not the fleet "
                    "(T-2324: green because of where it ran).",
                )
                self.assertIn(flip, fleet, f"FLEET dropped the flipped seat {flip!r}")
                self.assertEqual(
                    fleet[flip]["services"],
                    [flip_timer],
                    f"flipping {flip}'s supervisor to control-plane did NOT move FLEET's "
                    "liveness target to the derived consume timer — FLEET is re-enumerated as a "
                    "literal, so a real cutover would leave fleet_health probing the retired "
                    "bespoke unit and reporting a false verdict.",
                )
                for persona in live_cp:
                    self.assertEqual(
                        fleet[persona]["services"],
                        [f"pantheon-persona-consume@{persona}.timer"],
                        f"{persona} is control-plane in the manifest but FLEET's services are not "
                        "its derived consume timer — the supervisor marker is not reaching FLEET.",
                    )
                if still_bespoke is not None:
                    self.assertNotIn(
                        f"pantheon-persona-consume@{still_bespoke}.timer",
                        fleet[still_bespoke]["services"],
                        f"{still_bespoke} has no control-plane supervisor yet FLEET timerised it — "
                        "the derivation is not keyed on the supervisor marker.",
                    )


# ---------------------------------------------------------------------------
# T-3573 — the SIBLING-axis automated snapshot-site guard.
#
# The ACCOUNT axis has test_account_persona_map.TestEverySnapshotSiteIsGuarded,
# which re-derives every import-time snapshot of {account_persona_map,
# fleet_accounts, account_persona} from tools/*.py and asserts each is in that
# file's PER_SITE_GUARDS. Its docstring STOPS at the account axis and explicitly
# names context_fill_monitor.AGENT_CONFIG as the uncovered SIBLING example. This
# closes exactly that: the same scan, pointed at the sibling accessors, co-located
# with the T-2365 guards it enforces.
# ---------------------------------------------------------------------------

# SIBLING _ACCESSORS — the public accessors that read a persona-keyed map loaded
# from instance.yaml's `roles:` bindings, re-verified against agent_role_map:
#   instance_binding(persona)      -> _PERSONA_TO_BINDING  (account + restart-config)
#   resolve_instance_role(persona) -> _PERSONA_TO_INSTANCE_ROLE (role-key)
# Both are manifest-derived and both follow a perturbed manifest (Guard F6 above
# drives them). EXCLUDED, with reason:
#   - account_persona / account_persona_map / fleet_accounts — the ACCOUNT axis,
#     owned by test_account_persona_map (its scan's _ACCESSORS). The two files
#     partition the axes; DRAIN_AGENT_CONFIG sits in BOTH tables on purpose.
#   - control_plane_consume_service — a PURE naming function (f"...@{persona}...")
#     that reads no manifest, so a snapshot of it carries nothing that can drift.
#   - live_session_project_slug / live_session_projects_dir — derived CONVENIENCE
#     wrappers keyed by ACCOUNT (they call account_persona()+resolve_instance_role
#     internally). The account-axis scan deliberately declines to chase derived
#     wrappers (they are imported, not module-scope defs, so `reaches` stops at
#     them); this scan mirrors that discipline. doc_traversal_report.AGENTS and
#     keep_going.AGENT_* snapshot those wrappers and are out of BOTH scans' scope.
#   - mailbox_hub / mailbox_group — read persona_installer.mailbox, a DIFFERENT
#     manifest stanza, not the roles bindings; not this axis.
_SIBLING_ACCESSORS = frozenset({"instance_binding", "resolve_instance_role"})

# The hand-maintained table: each known sibling snapshot site -> its perturbed-
# manifest guard test. GUARDED_SNAPSHOTS is DERIVED from it, so a row cannot be
# added without naming a test (asserted below). The T-2365 block enumerated TWO
# (context_fill_monitor.AGENT_CONFIG, restart_drain.DRAIN_AGENT_CONFIG); the scan
# below surfaced THREE more the enumeration missed — fleet_health.FLEET (a direct
# instance_binding() snapshot via _liveness_services), and channel_liveness_monitor
# + hook_allowlist_lint, which RE-EXPORT context_fill_monitor.AGENT_CONFIG (a
# snapshot of a snapshot, the same shape as the account axis's session_reset_reconcile).
SIBLING_PER_SITE_GUARDS: dict[tuple[str, str], str] = {
    ("context_fill_monitor", "AGENT_CONFIG"):
        "test_context_fill_monitor_agent_config_follows_the_manifest",
    ("restart_drain", "DRAIN_AGENT_CONFIG"):
        "test_restart_drain_agent_config_follows_the_manifest",
    ("fleet_health", "FLEET"):
        "test_fleet_health_fleet_follows_the_supervisor_marker",
    ("channel_liveness_monitor", "AGENT_CONFIG"):
        "test_channel_liveness_monitor_agent_config_reexports_the_manifest",
    ("hook_allowlist_lint", "AGENT_CONFIG"):
        "test_hook_allowlist_lint_agent_config_reexports_the_manifest",
}
SIBLING_GUARDED_SNAPSHOTS: tuple[tuple[str, str], ...] = tuple(sorted(SIBLING_PER_SITE_GUARDS))

# Classes carrying the per-site guards SIBLING_PER_SITE_GUARDS names.
_SIBLING_GUARD_CLASSES = (
    TestFleetMonitorConsumersFollowThePerturbedManifest,
    TestFleetHealthFleetFollowsTheSupervisorMarker,
)


class TestEverySiblingSnapshotSiteIsGuarded(unittest.TestCase):
    """The enumeration is the weak point, so it is re-derived from the source.

    SIBLING_PER_SITE_GUARDS is a hand-maintained table. A module that binds
    instance_binding() (or resolve_instance_role()) to a constant tomorrow gets no
    coverage and nothing says so — the account axis's exact failure, one axis over.
    This walks tools/*.py and re-derives the set; it is how fleet_health.FLEET and
    the two AGENT_CONFIG re-exports (channel_liveness_monitor, hook_allowlist_lint)
    came to be rows — the T-2365 block enumerated only two.

    The scan CORE is shared with the account axis via tools/_snapshot_site_scan.py
    (T-3573), parameterised here by _SIBLING_ACCESSORS + SIBLING_GUARDED_SNAPSHOTS +
    _PLANT_HEADER. See that module's docstring for exactly what the scan detects.

    WHAT IT DOES NOT DETECT — asserted, not asserted-complete: every blind-spot
    shape is planted in test_the_documented_blind_spots_are_really_blind, so
    widening the scan reddens that test and the list cannot silently go stale:
      1. a binding built ACROSS statements (an empty constant then a module-level
         loop / global-initialiser call). One statement is matched at a time.
      2. a snapshot inside a def or class body (those do not run at import).
      3. an accessor reached through more than two levels of module-scope helper.
      4. an accessor reached INDIRECTLY — getattr(arm, "instance_binding")(), or a
         module-level name bound to the accessor object then called.
      5. a walrus used as a bare expression statement, with no assignment target.
      6. a re-export of a constant SIBLING_PER_SITE_GUARDS does not already name.
      7. anything outside tools/*.py.
    """

    _ACCESSORS = _SIBLING_ACCESSORS
    _PLANT_HEADER = (
        "from tools.agent_role_map import instance_binding, resolve_instance_role\n"
    )
    _scanner = SnapshotSiteScanner(_SIBLING_ACCESSORS, SIBLING_GUARDED_SNAPSHOTS, _PLANT_HEADER)

    # SHAPES THE SCAN MUST FIND — the account axis's control shapes, re-pointed at
    # the sibling accessors. The control plants the shape you FEAR (contains-anywhere:
    # `X = dict(instance_binding(...))`), not just `X = instance_binding(...)`, because
    # the hole the account review measured was one `dict` wide.
    _MUST_FIND: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("bare call", "X = instance_binding('specialist')", ("X",)),
        ("wrapper call", "X = dict(instance_binding('specialist'))", ("X",)),
        ("nested wrappers", "X = tuple(sorted(resolve_instance_role('specialist')))", ("X",)),
        ("comprehension", "X = {k: v for k, v in instance_binding('specialist').items()}", ("X",)),
        ("binop", "X = resolve_instance_role('specialist') + '-x'", ("X",)),
        ("ternary", "X = instance_binding('specialist') if True else {}", ("X",)),
        ("annotated wrapper", "X: dict = dict(instance_binding('specialist'))", ("X",)),
        (
            "tuple unpack",
            "X, Y = instance_binding('specialist'), resolve_instance_role('specialist')",
            ("X", "Y"),
        ),
        (
            "module-level try",
            "try:\n    X = dict(instance_binding('specialist'))\nexcept Exception:\n    X = {}\n",
            ("X",),
        ),
        (
            "one-level helper",
            "def _b():\n    return dict(instance_binding('specialist'))\nX = _b()",
            ("X",),
        ),
        (
            "two-level helper",
            "def _i():\n    return instance_binding('specialist')\n"
            "def _b():\n    return dict(_i())\nX = _b()\n",
            ("X",),
        ),
    )

    # SHAPES THE SCAN IS DOCUMENTED NOT TO FIND, one per numbered blind spot above.
    _MUST_MISS: tuple[tuple[str, str], ...] = (
        (
            "1 across statements",
            "X = {}\nfor k, v in instance_binding('specialist').items():\n    X[k] = v\n",
        ),
        (
            "1 global initializer",
            "def _init():\n    global X\n    X = instance_binding('specialist')\n_init()\n",
        ),
        ("2 class body", "class C:\n    X = instance_binding('specialist')\n"),
        (
            "3 three-level helper",
            "def _a():\n    return instance_binding('specialist')\n"
            "def _b():\n    return _a()\ndef _c():\n    return _b()\nX = _c()\n",
        ),
        ("4 name alias", "f = instance_binding\nX = f('specialist')\n"),
        (
            "4 getattr",
            "import tools.agent_role_map as arm\nX = getattr(arm, 'instance_binding')('specialist')\n",
        ),
        ("5 bare walrus", "(X := instance_binding('specialist'))\n"),
    )

    def _scan_module(self, path: Path) -> set[tuple[str, str]]:
        return self._scanner.scan_module(path)

    def _scan_source(self, source: str, name: str) -> set[tuple[str, str]]:
        return self._scanner.scan_source(source, name, self.addCleanup)

    def test_the_scan_finds_every_shape_a_snapshot_can_take(self) -> None:
        """The control: a scan that matched nothing would report a clean sweep
        forever, so it is driven against each shape before its emptiness is believed."""
        for label, source, expected in self._MUST_FIND:
            with self.subTest(shape=label):
                self.assertEqual(
                    self._scan_source(source, "planted_site"),
                    {("planted_site", target) for target in expected},
                    f"the scan does not see a sibling snapshot written as {label!r}. A site "
                    "written this way would be absent from the drift check and could be "
                    "re-enumerated as a literal in total silence.",
                )

    def test_the_documented_blind_spots_are_really_blind(self) -> None:
        """The other direction: each shape the docstring calls out is planted and
        asserted MISSED. Widen the scan and this fails, forcing the list corrected."""
        for label, source in self._MUST_MISS:
            with self.subTest(shape=label):
                self.assertEqual(
                    self._scan_source(source, "blind_site"),
                    set(),
                    f"the scan now DETECTS {label!r}, which the class docstring lists as a "
                    "blind spot. Good news — remove it from that list.",
                )

    def test_every_row_names_a_per_site_guard_that_exists(self) -> None:
        """A row cannot silence the drift check without naming a real per-site test.
        GUARDED_SNAPSHOTS is DERIVED from SIBLING_PER_SITE_GUARDS, so a row always
        names a test; this asserts the name resolves on a guard class."""
        missing = sorted(
            f"{module}.{constant} -> {test_name}"
            for (module, constant), test_name in SIBLING_PER_SITE_GUARDS.items()
            if not any(
                callable(getattr(cls, test_name, None)) for cls in _SIBLING_GUARD_CLASSES
            )
        )
        self.assertEqual(
            missing,
            [],
            "SIBLING_PER_SITE_GUARDS names per-site test(s) that do not exist on any sibling "
            f"guard class: {missing}. A row without a test silences the drift check while "
            "asserting nothing about the site.",
        )

    def test_every_module_level_snapshot_in_tools_is_in_the_table(self) -> None:
        found: set[tuple[str, str]] = set()
        for path in sorted((REPO_ROOT / "tools").glob("*.py")):
            if path.name.startswith("test_"):
                continue
            found |= self._scan_module(path)
        self.assertEqual(
            found,
            set(SIBLING_GUARDED_SNAPSHOTS),
            "tools/ module-level snapshots of the SIBLING axis (instance_binding / "
            "resolve_instance_role) have drifted from SIBLING_PER_SITE_GUARDS. An entry FOUND "
            "BUT NOT LISTED is a site with NO perturbed-manifest coverage: re-enumerating it as "
            "a literal would be silent — add it to SIBLING_PER_SITE_GUARDS TOGETHER with the "
            "per-site test the row must name. An entry LISTED BUT NOT FOUND means the site moved "
            "or was removed and its guard is now asserting nothing.",
        )


if __name__ == "__main__":
    unittest.main()
