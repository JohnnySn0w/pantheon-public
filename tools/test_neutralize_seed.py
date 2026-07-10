"""Tests for neutralize_seed.py — persona->role neutralization + seed selection.

Covers: word-boundary-safe token substitution, the two residual gaps closed in
T-1676 (claude-prime, Janus), privacy scrub, the KEEP/CLEAR/JUDGMENT/MANIFEST-
CLASS file-selection allowlist, manifest-class banner injection, basename
renames, and carry-forward-not-regenerated files (README.md, .gitignore).
"""
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

import neutralize_seed as ns


# ---------------------------------------------------------------------------
# Token substitution
# ---------------------------------------------------------------------------

def test_word_boundary_safe_ward_variants():
    text = "forward toward award wardrobe steward"
    new, n = ns.substitute_content(text)
    assert new == text
    assert n == 0


def test_ward_token_converts_case_variants():
    text = "WARD Ward ward"
    new, n = ns.substitute_content(text)
    assert new == "RECOVERY Recovery recovery"
    assert n == 3


def test_thoth_and_argus_token_convert():
    # "the user" (not "Michael") deliberately — Michael is a separate scrub
    # target (see the round-2 operator-name-scrub tests below) and this test
    # is scoped to the persona-token conversion only.
    text = "Thoth talks to the user. argus runs the watch loop."
    new, n = ns.substitute_content(text)
    assert new == "Orchestrator talks to the user. specialist runs the watch loop."
    assert n == 2


def test_role_id_and_instance_key_vocab_converge():
    text = "head_orchestrator science_team recovery-fallback recovery_fallback"
    new, n = ns.substitute_content(text)
    assert new == "orchestrator specialist recovery recovery"
    assert n == 4


def test_claude_prime_gap_closed():
    """T-1676 residual gap #1: claude-prime leaked through the original tool."""
    text = "Earlier identifiers — claude-prime, briefly Janus — refer to the same entity."
    new, n = ns.substitute_content(text)
    assert "claude-prime" not in new
    assert "Janus" not in new
    assert n == 2


def test_claude_prime_word_boundary_safe():
    text = "not-claude-primeval and claude-primer are untouched"
    new, n = ns.substitute_content(text)
    assert new == text
    assert n == 0


def test_janus_word_boundary_safe():
    text = "Januses and Janushead are untouched"
    new, n = ns.substitute_content(text)
    assert new == text
    assert n == 0


def test_privacy_scrub():
    text = "JohnnySn0w owns mahan.io; the money-engine project already pays $20/mo for Plus."
    new, n = ns.substitute_content(text)
    assert "JohnnySn0w" not in new
    assert "mahan.io" not in new
    assert "money-engine" not in new
    assert "$20/mo" not in new
    assert n >= 3


# ---------------------------------------------------------------------------
# ROUND 2 (Michael reversed the Q2 keep-precedent ruling, TG 5147): the
# operator's bare first name must not appear in the seed.
# ---------------------------------------------------------------------------

def test_operator_name_scrub_mid_sentence():
    text = "the ticket was filed by Michael, and Michael's call stands."
    new, n = ns.substitute_content(text)
    assert "Michael" not in new
    assert new == "the ticket was filed by the operator, and the operator's call stands."
    assert n == 2


def test_operator_name_scrub_word_boundary_safe():
    text = "Michaelson and MichaelJordan are untouched"
    new, n = ns.substitute_content(text)
    assert new == text
    assert n == 0


def test_operator_name_scrub_sentence_start_capitalized():
    text = "Ratified 2026-06-05. Michael approved it.\nMichael also filed the ticket.\n- Michael reviewed the draft\n* Michael closed it"
    new, n = ns.substitute_content(text)
    assert "Michael" not in new
    assert "The operator approved it" in new
    assert "\nThe operator also filed" in new
    assert "- The operator reviewed" in new
    assert "* The operator closed" in new


def test_operator_name_scrub_start_of_string():
    text = "Michael ratified this ADR."
    new, n = ns.substitute_content(text)
    assert new == "The operator ratified this ADR."


def test_studio_mahan_scrub():
    text = "Studio Mahan LLC company materials (revenue-project entity layer)"
    new, n = ns.substitute_content(text)
    assert "Mahan" not in new
    assert "the operator's studio" in new


def test_bare_mahan_fallback_scrub():
    text = "a stray reference to Mahan with no Studio prefix"
    new, n = ns.substitute_content(text)
    assert "Mahan" not in new


def test_mahan_word_boundary_safe():
    text = "Mahanoy and Mahanaquatic are untouched"
    new, n = ns.substitute_content(text)
    assert new == text
    assert n == 0


def test_operator_name_scrub_filename():
    assert ns.rename_basename("ho-absent-michael-keep-working.md") == "ho-absent-operator-keep-working.md"


def test_operator_name_scrub_escape_sequence_adjacent():
    """Verified real leak: tools/test_ticket_lint.py contains fixture text
    like `"...\\nMichael will drive this..."` — a Python SOURCE FILE's raw
    bytes hold the literal 2 characters backslash+n (an unrendered escape
    sequence), not an actual newline byte. The plain letter-boundary check
    sees 'n' (a letter) immediately before "Michael" and refuses to match,
    same as it correctly refuses to touch "MichaelJordan" — but here that's
    wrong: this is a real word boundary in the source, not a compound word."""
    text = "some markdown\\n\\nMichael will drive this when he has time.\\n"
    new, n = ns.substitute_content(text)
    assert "Michael" not in new
    assert "the operator will drive this" in new or "The operator will drive this" in new


def test_camelcase_embedded_operator_name_class_name():
    """Verified real leak: tools/test_activity_monitor.py has a Python class
    named `TestMichaelLastInboundAge` — "Michael" is embedded in a camelCase
    identifier with no separator, so the general letter-boundary rule
    correctly (by design) leaves it alone, same as it must leave
    "MichaelJordan" alone. A targeted literal-identifier rule closes this
    specific instance without loosening the general boundary safety."""
    text = "class TestMichaelLastInboundAge(unittest.TestCase):"
    new, n = ns.substitute_content(text)
    assert new == "class TestOperatorLastInboundAge(unittest.TestCase):"


def test_operator_name_scrub_hyphen_adjacent_uses_bare_token():
    """Verified real breakage from the prose-integrity spot-read: "Michael"
    immediately adjacent to a hyphen (a compound title/identifier, not
    ordinary prose) must substitute to bare "operator" — the two-word "the
    operator" breaks the compound. Three real instances found:
    - title "HO-absent-Michael-keep-working" -> broken
      "HO-absent-the operator-keep-working" if the prose rule fires.
    - "(Michael-absent-with-pullable-work)" -> broken
      "(the operator-absent-with-pullable-work)".
    - "no Michael-shelving is pending" -> ungrammatical
      "no the operator-shelving is pending"."""
    cases = [
        ("HO-absent-Michael-keep-working", "HO-absent-operator-keep-working"),
        ("(Michael-absent-with-pullable-work)", "(operator-absent-with-pullable-work)"),
        ("no Michael-shelving is pending", "no operator-shelving is pending"),
        ("orchestrator-drafted under T-0022; Michael-ratified.", "orchestrator-drafted under T-0022; operator-ratified."),
    ]
    for text, expected in cases:
        new, n = ns.substitute_content(text)
        assert new == expected, f"{text!r} -> {new!r}, expected {expected!r}"


def test_operator_name_scrub_non_hyphen_adjacent_still_gets_article():
    """Sanity: ordinary prose usage (not hyphen-adjacent) must still get the
    natural "the operator" form, not the bare compound-only token."""
    text = "ratified by Michael on that date."
    new, n = ns.substitute_content(text)
    assert new == "ratified by the operator on that date."


def test_a_an_agreement_operator():
    """REBOUND-2: substituting a consonant-initial token (Michael) with a
    vowel-initial one (operator) leaves the preceding "a" article wrong —
    "a Michael-ratified" -> "a operator-ratified" instead of "an
    operator-ratified". These are all hyphen-adjacent compound-modifier
    instances (bare "operator", no article inserted by the Michael rule
    itself — see the hyphen-adjacency rule above) — the real evidenced
    pattern (19 instances, e.g. rulebook/directives.md, adr/ORG-037). Must
    run AFTER the token substitutions (it corrects their output), scoped to
    just these two known vowel-initial outputs — not a general prose a/an
    corrector."""
    cases = [
        ("a Michael-ratified decision", "an operator-ratified decision"),
        ("A Michael-ratified decision", "An operator-ratified decision"),
    ]
    for text, expected in cases:
        new, n = ns.substitute_content(text)
        assert new == expected, f"{text!r} -> {new!r}, expected {expected!r}"


def test_a_an_agreement_orchestrator():
    """The pre-existing round-1 case (thoth -> orchestrator) folded in per
    the reviewer's REBOUND-2 finding — same defect class, one fix. Thoth's
    substitution is always Title-cased regardless of sentence position (a
    proper-noun-style replacement, unlike Michael's position-aware
    capitalization) — "a Thoth would" -> "a Orchestrator would" (bare token,
    no article inserted), so the a/an fix must preserve that Title-case
    form too, not just the "orchestrator" prose form the Michael rule uses."""
    cases = [
        ("Thoth talks like a Thoth would.", "Orchestrator talks like an Orchestrator would."),
        ("A Thoth-drafted ADR", "An Orchestrator-drafted ADR"),
    ]
    for text, expected in cases:
        new, n = ns.substitute_content(text)
        assert new == expected, f"{text!r} -> {new!r}, expected {expected!r}"


def test_a_an_agreement_collapses_double_article():
    """Verified real instance (rulebook/goals.md): "a Michael roadmap call"
    (Michael used as a bare adjectival modifier, not hyphenated) falls
    through to the general Michael->"the operator" rule (which inserts its
    own article), producing "a the operator roadmap call" — a double
    article, worse than the simple a/an mismatch. Collapse "a the
    operator"/"a the orchestrator" (any case) straight to "an
    operator"/"an orchestrator"."""
    cases = [
        ("itself a the operator roadmap call", "itself an operator roadmap call"),
        ("A the operator decision", "An operator decision"),
        ("a the orchestrator session", "an orchestrator session"),
    ]
    for text, expected in cases:
        new, n = ns.substitute_content(text)
        assert new == expected, f"{text!r} -> {new!r}, expected {expected!r}"


def test_a_an_agreement_does_not_touch_already_correct_an():
    text = "an operator reviewed it; an orchestrator drafted it."
    new, n = ns.substitute_content(text)
    assert new == text


def test_a_an_agreement_tree_wide_zero(tmp_path=None):
    """Gate-level assertion: across the tool's own test corpus of known
    substitution outputs, `\\ba operator\\b` and `\\ba orchestrator\\b` never
    survive."""
    samples = [
        "a Michael-ratified ADR", "A Michael-ratified ADR",
        "a Thoth-drafted plan", "A Thoth-drafted plan",
        "talks like a Thoth would", "needs a Thoth session",
    ]
    for s in samples:
        new, n = ns.substitute_content(s)
        assert not re.search(r'\ba operator\b', new), (s, new)
        assert not re.search(r'\ba orchestrator\b', new), (s, new)
        assert not re.search(r'\bA operator\b', new), (s, new)
        assert not re.search(r'\bA orchestrator\b', new), (s, new)


def test_markdown_anchor_slug_compound_scrub():
    """Verified real leak: a GFM-slugified markdown anchor concatenates words
    with no separator when punctuation is stripped, defeating letter-boundary
    safety for BOTH "ward" and "michael" in the same anchor fragment."""
    text = "[link](file.md#failure-alert-routing-othersward-ward-selfmichael)"
    new, n = ns.substitute_content(text)
    assert new == "[link](file.md#failure-alert-routing-othersrecovery-recovery-selfthe-operator)"


def test_operator_name_scrub_all_caps_constant():
    """Verified real leak: MICHAEL_CHAT_ID, MICHAEL_MOOT_RECENCY_DAYS,
    P1B_MICHAEL_ABSENT_THRESHOLD_SECONDS — the other 3 tokens (thoth/argus/
    ward) all have THOTH/Thoth/thoth-style full case coverage; Michael only
    had Title-case + lowercase. This was the gap."""
    text = 'MICHAEL_CHAT_ID = "456474197"\nP1B_MICHAEL_ABSENT_THRESHOLD_SECONDS = 900'
    new, n = ns.substitute_content(text)
    assert "MICHAEL" not in new
    assert new == 'OPERATOR_CHAT_ID = "456474197"\nP1B_OPERATOR_ABSENT_THRESHOLD_SECONDS = 900'


def test_operator_name_scrub_lowercase_structured_token():
    """Verified real leak, confirmed at scale: bare lowercase `michael` is a
    STRUCTURED-DATA VALUE throughout tools/ticket.py & co — `target: michael`,
    `requester: michael`, `target == "michael"` — the ticket system's role
    vocabulary treats "michael" as a value exactly like "thoth"/"argus"/"ward"
    are values elsewhere. 67 selected files carry this (simulation-verified).
    Maps to bare "operator" (no article — a structured value, not prose),
    matching tools/seed_genericize.py's own established precedent for the
    identical case (`\\bmichael\\b` -> `operator`, no "the")."""
    text = 'target: michael\nrequester: michael\ngate_applies = target == "michael"'
    new, n = ns.substitute_content(text)
    assert "michael" not in new
    assert new == 'target: operator\nrequester: operator\ngate_applies = target == "operator"'


def test_operator_name_scrub_lowercase_does_not_touch_uppercase_form():
    """Sanity: the lowercase structured-token rule must not fire on the
    capitalized prose form (that's the earlier rules' job) or vice versa."""
    text = "Michael ratified this; target: michael"
    new, n = ns.substitute_content(text)
    assert new == "The operator ratified this; target: operator"


def test_generator_source_exempt_covers_seed_genericize_too():
    """tools/seed_genericize.py (T-1327, a separate pre-existing generator)
    has its own Michael/michael pattern table for the same reason mine does
    — it must ship unmodified, not partially corrupted by this tool's own
    substitution pass."""
    assert "tools/seed_genericize.py" in ns.SELF_EXEMPT_FROM_SUBSTITUTION
    assert "tools/test_seed_genericize.py" in ns.SELF_EXEMPT_FROM_SUBSTITUTION


# ---------------------------------------------------------------------------
# Basename renames
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("thoth-pane-unsolicited-typed-text-mystery.md", "orchestrator-pane-unsolicited-typed-text-mystery.md"),
    ("argus.md", "specialist.md"),
    ("ward.md", "recovery.md"),
    ("ward-bot-supervise.sh", "recovery-bot-supervise.sh"),
    ("README.md", "README.md"),
    # REBOUND 1: privacy-scrub tokens must also apply to basenames, not just
    # content. Verified real leak: adr/ORG-032-money-engine-orchestration-shape.md
    # shipped with the scrubbed-in-content-but-not-in-filename project name.
    ("ORG-032-money-engine-orchestration-shape.md", "ORG-032-revenue-project-orchestration-shape.md"),
    ("money_engine_notes.md", "revenue_project_notes.md"),
])
def test_rename_basename(name, expected):
    assert ns.rename_basename(name) == expected


# ---------------------------------------------------------------------------
# File selection allowlist
# ---------------------------------------------------------------------------

def _make_fake_source(root: Path):
    # Wholesale-KEEP dirs
    for d in ("rulebook", "sop", "adr", "tools", "tests", "skills", ".githooks"):
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / "a.py").write_text("thoth\n")
    # Wholesale-CLEAR dirs
    for d in ("tickets", "ledger", "notes", "incidents", "reports", "finances"):
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / "b.md").write_text("operator-secret@example.com\n")
    # Judgment-include dirs
    for d in ("bugs", "lessons"):
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / "c.md").write_text("ward\n")
    # knowledge/ split
    (root / "knowledge").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "README.md").write_text("generic\n")
    (root / "knowledge" / "pantheon-glossary.md").write_text("glossary\n")
    (root / "knowledge" / "infra-abstractions").mkdir(parents=True, exist_ok=True)
    (root / "knowledge" / "infra-abstractions" / "x.md").write_text("generic\n")
    for d in ("episodic", "watch", "company", "external-audits", "state"):
        (root / "knowledge" / d).mkdir(parents=True, exist_ok=True)
        (root / "knowledge" / d / "y.md").write_text("instance-specific\n")
    # manifest-class
    (root / "repos.yaml").write_text("schema_version: 1\n")
    (root / "instance.yaml").write_text("instance:\n  id: x\n")
    (root / "pinned-rules").mkdir(parents=True, exist_ok=True)
    (root / "pinned-rules" / "argus.md").write_text("Argus rules\n")
    (root / "pinned-rules" / "ward.md").write_text("Ward rules\n")
    (root / "pinned-rules" / "README.md").write_text("index\n")
    # excluded-by-default top-level cruft
    (root / "BACKLOG.md").write_text("not for seed\n")
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "z.png").write_bytes(b"\x89PNG")
    # root-level carry-forward-only files (present in source too, but must NOT be sourced from here)
    (root / "README.md").write_text("SOURCE readme, not the seed's\n")
    (root / ".gitignore").write_text("SOURCE gitignore\n")


def test_select_paths_keep_dirs_wholesale(tmp_path):
    _make_fake_source(tmp_path)
    selected = {str(p) for p in ns.select_paths(tmp_path)}
    for d in ("rulebook", "sop", "adr", "tools", "tests", "skills", ".githooks"):
        assert f"{d}/a.py" in selected


def test_select_paths_clear_dirs_excluded(tmp_path):
    _make_fake_source(tmp_path)
    selected = {str(p) for p in ns.select_paths(tmp_path)}
    for d in ("tickets", "ledger", "notes", "incidents", "reports", "finances"):
        assert f"{d}/b.md" not in selected


def test_select_paths_judgment_dirs_included(tmp_path):
    _make_fake_source(tmp_path)
    selected = {str(p) for p in ns.select_paths(tmp_path)}
    assert "bugs/c.md" in selected
    assert "lessons/c.md" in selected


def test_select_paths_knowledge_split(tmp_path):
    _make_fake_source(tmp_path)
    selected = {str(p) for p in ns.select_paths(tmp_path)}
    assert "knowledge/README.md" in selected
    assert "knowledge/pantheon-glossary.md" in selected
    assert "knowledge/infra-abstractions/x.md" in selected
    for d in ("episodic", "watch", "company", "external-audits", "state"):
        assert f"knowledge/{d}/y.md" not in selected


def test_select_paths_manifest_class_included(tmp_path):
    _make_fake_source(tmp_path)
    selected = {str(p) for p in ns.select_paths(tmp_path)}
    assert "repos.yaml" in selected
    assert "instance.yaml" in selected
    assert "pinned-rules/argus.md" in selected
    assert "pinned-rules/ward.md" in selected
    assert "pinned-rules/README.md" in selected


def test_select_paths_default_excludes_unlisted_toplevel(tmp_path):
    _make_fake_source(tmp_path)
    selected = {str(p) for p in ns.select_paths(tmp_path)}
    assert "BACKLOG.md" not in selected
    assert "assets/z.png" not in selected


def test_select_paths_tests_dir_missing_is_graceful(tmp_path):
    _make_fake_source(tmp_path)
    import shutil
    shutil.rmtree(tmp_path / "tests")
    selected = {str(p) for p in ns.select_paths(tmp_path)}  # must not raise
    assert not any(str(p).startswith("tests/") for p in selected)


def test_select_paths_skips_pytest_cache(tmp_path):
    """Verified real leak: tools/.pytest_cache/v/cache/nodeids survived into
    the build — same class of bug as the __pycache__ fix in REBOUND 1, just a
    different build-artifact directory this tool's own repeated test runs
    left behind in the KEEP-wholesale source tree."""
    _make_fake_source(tmp_path)
    cache_dir = tmp_path / "tools" / ".pytest_cache" / "v" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "nodeids").write_text("[]")

    selected = [str(p) for p in ns.select_paths(tmp_path)]

    assert not any(".pytest_cache" in p for p in selected)


def test_select_paths_skips_pycache_and_bytecode(tmp_path):
    """REBOUND 1 root cause: stale __pycache__/*.pyc left in the SOURCE tree
    by running the test suite there got swept into KEEP-wholesale selection,
    inflating the build's file count and copying build artifacts into the
    seed (harmless only because the seed's own .gitignore then excluded them
    from `git add -A` — the tool should never have selected them)."""
    _make_fake_source(tmp_path)
    cache_dir = tmp_path / "tools" / "__pycache__"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "a.cpython-312.pyc").write_bytes(b"\x00\x01")
    (tmp_path / "tools" / "a.pyc").write_bytes(b"\x00\x01")
    (tmp_path / "tools" / "a.pyo").write_bytes(b"\x00\x01")

    selected = [str(p) for p in ns.select_paths(tmp_path)]

    assert not any("__pycache__" in p for p in selected)
    assert not any(p.endswith((".pyc", ".pyo")) for p in selected)


# ---------------------------------------------------------------------------
# Manifest-class banner injection
# ---------------------------------------------------------------------------

def test_manifest_class_banner_prepended():
    text = "schema_version: 1\n"
    out = ns.apply_manifest_banner("repos.yaml", text)
    assert out.startswith("# " + "=" * 76 + "\n")
    assert "REFERENCE-INSTANCE EXAMPLE" in out
    assert out.endswith(text)


def test_manifest_class_banner_idempotent():
    text = "schema_version: 1\n"
    once = ns.apply_manifest_banner("repos.yaml", text)
    twice = ns.apply_manifest_banner("repos.yaml", once)
    assert once == twice


def test_manifest_class_banner_not_applied_to_other_files():
    text = "hello\n"
    assert ns.apply_manifest_banner("pinned-rules/argus.md", text) == text


# ---------------------------------------------------------------------------
# Full build pipeline (end-to-end, small fixture)
# ---------------------------------------------------------------------------

def test_build_seed_preserves_executable_bit(tmp_path):
    """Text files (e.g. .githooks/pre-commit, extensionless) go through
    read_text/write_text for substitution, which does NOT preserve the source
    file's mode bits the way shutil.copy2 does — verified failure mode: the
    refreshed seed's .githooks/pre-commit lost +x, silently disabling the
    HERMES-discipline guard (3 downstream test failures in the seed's own
    suite: test_hooks_are_executable + both TestGitSmoke refuse-cases)."""
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    carry = tmp_path / "carry"
    src.mkdir()
    dest.mkdir()
    carry.mkdir()
    (src / ".githooks").mkdir(parents=True, exist_ok=True)
    hook = src / ".githooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho ward\n")
    hook.chmod(0o755)
    (carry / "README.md").write_text("r\n")
    (carry / ".gitignore").write_text("g\n")

    ns.build_seed(src, dest, carry_forward_root=carry)

    out = dest / ".githooks" / "pre-commit"
    assert out.read_text() == "#!/bin/sh\necho recovery\n"
    assert out.stat().st_mode & 0o111, "executable bit must survive substitution"


def test_build_seed_end_to_end(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    carry = tmp_path / "carry"
    src.mkdir()
    dest.mkdir()
    carry.mkdir()
    _make_fake_source(src)
    (carry / "README.md").write_text("EXISTING seed readme\n")
    (carry / ".gitignore").write_text("EXISTING seed gitignore\n")

    stats = ns.build_seed(src, dest, carry_forward_root=carry)

    # renamed + substituted content
    assert (dest / "bugs" / "c.md").read_text() == "recovery\n"
    # pinned-rules renamed
    assert (dest / "pinned-rules" / "specialist.md").exists()
    assert (dest / "pinned-rules" / "recovery.md").exists()
    assert not (dest / "pinned-rules" / "argus.md").exists()
    # manifest-class banner present
    repos_out = (dest / "repos.yaml").read_text()
    assert "REFERENCE-INSTANCE EXAMPLE" in repos_out
    # carry-forward files come from carry root, not src
    assert (dest / "README.md").read_text() == "EXISTING seed readme\n"
    assert (dest / ".gitignore").read_text() == "EXISTING seed gitignore\n"
    # CLEAR dirs absent
    assert not (dest / "tickets").exists()
    assert not (dest / "knowledge" / "company").exists()
    assert stats["files_written"] > 0


def test_generator_source_exempt_from_self_substitution(tmp_path):
    """The generator ships with the arc (T-1676 process lesson) and therefore
    becomes part of its own KEEP-wholesale input on every future refresh. Its
    own source contains persona tokens as REGEX PATTERN LITERALS, not prose —
    if the pipeline neutralized itself, it would corrupt its own SUBS map
    (verified failure mode: a self-build turned `(L('thoth'), 'orchestrator')`
    into `(L('orchestrator'), 'orchestrator')`, a silent no-op map)."""
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    carry = tmp_path / "carry"
    src.mkdir()
    dest.mkdir()
    carry.mkdir()
    (src / "tools").mkdir(parents=True, exist_ok=True)
    marker_line = "    (L('thoth'), 'orchestrator'),  # must survive verbatim\n"
    (src / "tools" / "neutralize_seed.py").write_text(marker_line)
    (carry / "README.md").write_text("r\n")
    (carry / ".gitignore").write_text("g\n")

    ns.build_seed(src, dest, carry_forward_root=carry)

    assert (dest / "tools" / "neutralize_seed.py").read_text() == marker_line


def test_build_seed_is_deterministic(tmp_path):
    src = tmp_path / "src"
    carry = tmp_path / "carry"
    src.mkdir()
    carry.mkdir()
    _make_fake_source(src)
    (carry / "README.md").write_text("r\n")
    (carry / ".gitignore").write_text("g\n")

    dest1 = tmp_path / "dest1"
    dest2 = tmp_path / "dest2"
    dest1.mkdir()
    dest2.mkdir()
    ns.build_seed(src, dest1, carry_forward_root=carry)
    ns.build_seed(src, dest2, carry_forward_root=carry)

    files1 = sorted(p.relative_to(dest1) for p in dest1.rglob("*") if p.is_file())
    files2 = sorted(p.relative_to(dest2) for p in dest2.rglob("*") if p.is_file())
    assert files1 == files2
    for f in files1:
        assert (dest1 / f).read_bytes() == (dest2 / f).read_bytes()
