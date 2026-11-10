"""T-5005 (T-4924 H1): the hermes HarnessRenderTarget and prepare_hermes_launch."""

from __future__ import annotations

import codecs
import dataclasses
import hashlib
import json
from pathlib import Path, PurePath
from types import SimpleNamespace

import pytest
import yaml

from tools.harness_adapter import hermes_render
from tools.harness_adapter.hermes import HermesHarnessAdapter
from tools.harness_adapter.hermes_render import (
    CAPTURED_PROVENANCE,
    HOST_OVERLAY_PATH,
    HermesRenderError,
    HermesRenderInputs,
    HermesRenderTarget,
    load_defaults,
    prepare_hermes_launch,
    render_seat_files,
)
from tools.harness_adapter.render_target import HarnessRenderTarget
from tools.harness_adapter.types import HarnessLaunchConfig

_REAL_INSIDE_REPO = hermes_render._inside_repo
_REAL_INSTALL_ROOT = hermes_render.hermes_install_root


@pytest.fixture(autouse=True)
def _repo_probe_bounded_to_tmp(tmp_path, monkeypatch):
    """Some hosts carry a stray .git above the pytest temp root (this one has
    /tmp/.git), so the seat-outside-any-repo probe is bounded to tmp_path."""

    def bounded(path: Path) -> bool:
        return any(
            (candidate / ".git").exists()
            for candidate in (path, *path.parents)
            if candidate == tmp_path or tmp_path in candidate.parents
        )

    monkeypatch.setattr(hermes_render, "_inside_repo", bounded)
    monkeypatch.setattr(hermes_render, "hermes_install_root", lambda _executable: tmp_path / "package")


def test_default_repo_probe_walks_up(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "a" / "b").mkdir(parents=True)
    assert _REAL_INSIDE_REPO(tmp_path / "repo" / "a" / "b") is True


REPO = Path(__file__).resolve().parents[1]
COMMITTED_DEFAULTS = REPO / "harness" / "hermes" / "config.defaults.yaml"
SOURCE_TEXT = "# Qianliyan pinned rules (fixture)\n\nrules body\n\n## Identity\n\nYou are a watcher.\n\n## Other\n\nmore\n"


def _launch(*, executable: Path = Path("/opt/hermes"), **native_overrides) -> HarnessLaunchConfig:
    native = {"provider": "deepseek", "busy_input_mode": "queue"}
    native.update(native_overrides)
    return HarnessLaunchConfig(
        harness="hermes", profile="deepseek-direct", executable=executable,
        model="deepseek-v4-pro", mode="interactive", meter="api", quota_source="provider-api",
        native=native, sources=("test",),
    )


#: The 19 auxiliary slots of the pinned DEFAULT_CONFIG (H0 Specialist must 4).
H0_AUX_SLOTS = (
    "vision", "compression", "skills_hub", "approval", "review", "mcp", "title_generation",
    "memory_query_rewrite", "tts_audio_tags", "voice_chat", "triage_specifier", "kanban_decomposer",
    "profile_describer", "goal_judge", "curator", "monitor", "background_review", "moa_reference",
    "moa_aggregator",
)
#: Every locked overlay leaf, each an H0-captured DEFAULT_CONFIG leaf path except
#: the two the defaults file justifies inline (marked).
H0_LOCKED_LEAVES = {
    "approvals.mode": "off",
    "approvals.deny": [],
    "display.busy_input_mode": "queue",
    "memory.memory_enabled": False,
    "memory.user_profile_enabled": False,
    "memory.write_approval": True,
    "auxiliary.title_generation.enabled": False,
    "auxiliary.background_review.enabled": False,
    "curator.enabled": False,
    "skills.project_discovery": False,
    "skills.write_approval": True,
    "updates.check": False,
    "plugins.auto_update_check_hours": 0,
    "tools.connectors.enabled": False,
    "nous.guest": False,
    "model_catalog.enabled": False,
    "telemetry.shared_metrics.enabled": False,
    "telemetry.shared_metrics.send": False,
    "telemetry.shared_metrics.offer_version": 2,  # not a leaf: Hermes writes it on consent
    # T-5011 PR-D: the CLI tool lock (not a leaf: absent means the widest bundle).
    "platform_toolsets.cli": ["terminal", "file", "skills", "todo"],
    "agent.disabled_toolsets": [
        "web", "search", "x_search", "browser", "delegation", "code_execution", "cronjob", "vision",
        "video", "image_gen", "video_gen", "tts", "memory", "connections", "session_search", "clarify",
        "kanban", "computer_use", "desktop_ui", "project", "catalog", "onboarding", "context_engine", "a2a",
    ],
    "plugins.enabled": [],  # not a leaf: an empty opt-in list keeps plugin toolsets off
    "context.engine": "compressor",
    "agent.coding_context": "auto",
    "fallback_providers": [],
    "mcp_servers": {},  # not a leaf: absent means no servers
}


def _captured_defaults(tmp_path: Path, **edits) -> Path:
    data = yaml.safe_load(COMMITTED_DEFAULTS.read_text(encoding="utf-8"))
    data.update(edits)
    path = tmp_path / "defaults.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _inputs(tmp_path: Path, *, defaults: Path | None = None, sha: str | None = None) -> HermesRenderInputs:
    source = tmp_path / "pin" / "qianliyan.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(SOURCE_TEXT, encoding="utf-8")
    home = tmp_path / "home"
    (home / "seat").mkdir(parents=True, exist_ok=True)
    return HermesRenderInputs(
        persona="qianliyan",
        account_home=home,
        source_path=source,
        source_sha256=sha or hashlib.sha256(SOURCE_TEXT.encode("utf-8")).hexdigest(),
        source_commit="0" * 40,
        defaults_path=defaults or _captured_defaults(tmp_path),
    )


# --- target ------------------------------------------------------------------


def test_target_satisfies_protocol_and_is_opt_in():
    target = HermesRenderTarget()
    attributes = set(HarnessRenderTarget.__annotations__)
    methods = {
        name for name, value in vars(HarnessRenderTarget).items()
        if not name.startswith("_") and callable(value)
    }
    # Pin the protocol surface itself, so an empty discovery can't pass vacuously.
    assert attributes == {"harness", "in_all"}
    assert methods == {
        "harness_home", "install_path", "skill_frontmatter", "skill_relpath", "render_standing",
        "render_runtime_config", "forbidden_targets", "host_defaults", "bindings_dir",
    }
    for name in attributes:
        assert hasattr(target, name), name
    for name in methods:
        assert callable(getattr(target, name, None)), name
    assert isinstance(target.harness, str) and isinstance(target.in_all, bool)
    assert target.harness == "hermes" and target.in_all is False
    assert target.forbidden_targets(account_home=Path("/h")) == ()


def test_install_paths_are_account_home_relative():
    target = HermesRenderTarget()
    home = Path("/home/deepseek-fallback")
    assert target.install_path("project-context", account_home=home) == home / "seat" / ".hermes.md"
    assert target.install_path("identity", account_home=home) == home / ".hermes" / "SOUL.md"
    assert target.install_path("runtime-config", account_home=home) == home / ".hermes" / "config.yaml"
    assert target.bindings_dir(repo_root=Path("/ro"), account_home=home) == home / ".local/state/pantheon/hermes-render"
    upper = "HER" + "MES" + ".md"
    assert all(
        target.install_path(kind, account_home=home).name != upper
        for kind in ("project-context", "identity", "runtime-config", "skill-root")
    )


def test_skill_layout_has_category_level():
    target = HermesRenderTarget()
    assert target.skill_relpath("ticket-flow") == PurePath("pantheon", "pantheon-sop-ticket-flow", "SKILL.md")
    assert target.skill_frontmatter({"slug": "ticket-flow", "description": "d"}) == {
        "name": "pantheon-sop-ticket-flow", "description": "d",
    }
    with pytest.raises(ValueError):
        target.skill_relpath("../escape")


def test_standing_render_is_verbatim_bytes():
    assert HermesRenderTarget().render_standing(SOURCE_TEXT, persona="q") == SOURCE_TEXT.encode("utf-8")


def test_runtime_config_is_deterministic_and_pins_every_aux_slot(tmp_path):
    target = HermesRenderTarget()
    defaults = load_defaults(_captured_defaults(tmp_path))
    first = target.render_runtime_config(_launch(), defaults, account_home=Path("/h"))
    second = target.render_runtime_config(_launch(), defaults, account_home=Path("/h"))
    assert first == second
    config = yaml.safe_load(first)
    assert config["model"] == {"default": "deepseek-v4-pro", "provider": "deepseek"}
    assert config["terminal"]["cwd"] == "/h/seat"
    assert config["fallback_providers"] == [] and config["mcp_servers"] == {}
    assert tuple(config["auxiliary"]) == H0_AUX_SLOTS
    for slot in config["auxiliary"].values():
        assert slot["provider"] == "deepseek" and slot["model"] == "deepseek-v4-pro"
        assert "base_url" not in slot  # the profile names none
    assert b"T-4924" in first.splitlines()[0]


def test_aux_slots_also_pin_the_profile_base_url(tmp_path):
    defaults = load_defaults(_captured_defaults(tmp_path))
    config = yaml.safe_load(
        HermesRenderTarget().render_runtime_config(
            _launch(base_url="https://api.deepseek.com/v1"), defaults, account_home=Path("/h")
        )
    )
    assert config["model"]["base_url"] == "https://api.deepseek.com/v1"
    for name, slot in config["auxiliary"].items():
        assert slot["base_url"] == "https://api.deepseek.com/v1", name


def test_safety_default_contradiction_fails(tmp_path):
    defaults = load_defaults(_captured_defaults(tmp_path))
    with pytest.raises(HermesRenderError) as info:
        HermesRenderTarget().render_runtime_config(
            _launch(busy_input_mode="interrupt"), defaults, account_home=Path("/h")
        )
    assert info.value.reason == "safety-default-contradiction"
    with pytest.raises(HermesRenderError):
        HermesRenderTarget().render_runtime_config(
            _launch(approvals={"mode": "smart"}), defaults, account_home=Path("/h")
        )
    # T-5011 PR-D: a profile can no longer carry toolsets; the lock always renders.
    config = yaml.safe_load(
        HermesRenderTarget().render_runtime_config(
            _launch(toolsets=["hermes-cli", "web"]), defaults, account_home=Path("/h")
        )
    )
    assert "toolsets" not in config
    assert config["platform_toolsets"] == {"cli": ["terminal", "file", "skills", "todo"]}


def test_auto_provider_is_refused(tmp_path):
    defaults = load_defaults(_captured_defaults(tmp_path))
    with pytest.raises(HermesRenderError) as info:
        HermesRenderTarget().render_runtime_config(_launch(provider="auto"), defaults, account_home=Path("/h"))
    assert info.value.reason == "profile-provider"


def test_merged_is_the_default_placement(tmp_path):
    """H0 Specialist must 3: the overlay fails open, so the locks go INTO the seat
    config.yaml by default and the overlay is an extra layer."""
    assert hermes_render.DEFAULTS_PLACEMENT == "merged"
    assert HermesRenderTarget().placement == "merged"
    defaults = load_defaults(_captured_defaults(tmp_path))
    config = yaml.safe_load(HermesRenderTarget().render_runtime_config(_launch(), defaults, account_home=Path("/h")))
    seat = hermes_render._leaves(config)
    for dotted, value in H0_LOCKED_LEAVES.items():
        assert seat[tuple(dotted.split("."))] == value, dotted
    assert config["auxiliary"]["background_review"] == {
        "provider": "deepseek", "model": "deepseek-v4-pro", "enabled": False,
    }
    assert config["auxiliary"]["title_generation"]["enabled"] is False


def test_host_overlay_is_emitted_in_both_placements(tmp_path):
    defaults = load_defaults(_captured_defaults(tmp_path))
    for placement in ("merged", "overlay"):
        path, body = HermesRenderTarget(placement=placement).host_defaults(defaults)
        assert path == HOST_OVERLAY_PATH == Path("/etc/hermes/config.yaml")
        assert yaml.safe_load(body)["approvals"]["mode"] == "off"


def test_overlay_only_placement_still_seeds_telemetry(tmp_path):
    defaults = load_defaults(_captured_defaults(tmp_path))
    config = yaml.safe_load(
        HermesRenderTarget(placement="overlay").render_runtime_config(_launch(), defaults, account_home=Path("/h"))
    )
    assert "memory" not in config  # overlay-only: the locks are not merged in
    assert config["telemetry"] == {"shared_metrics": {"enabled": False, "send": False, "offer_version": 2}}


def test_managed_overlay_ignored_stderr_constant():
    assert hermes_render.MANAGED_OVERLAY_IGNORED_STDERR == "IGNORING this managed file"
    assert hermes_render.LAUNCH_ENV_UNSET == ("HERMES_MANAGED_DIR",)


# --- committed defaults (real repo) ------------------------------------------


def test_committed_defaults_are_h0_captured_and_pinned():
    defaults = load_defaults(COMMITTED_DEFAULTS)
    assert defaults["provenance"] == CAPTURED_PROVENANCE == "h0-captured"
    assert defaults["hermes_version"] == "v0.22.0"
    assert defaults["hermes_commit"] == "e06df9c2eae6901f4c88f464e0ae6e753c0428b8"
    assert defaults["hermes_tarball_sha256"] == "d219fb6a579545e2e3a0673d34c4915212ac577690b942195ec261319683a5ad"
    assert defaults["auxiliary_slots_complete"] is True
    assert tuple(defaults["auxiliary_slots"]) == H0_AUX_SLOTS and len(H0_AUX_SLOTS) == 19
    assert COMMITTED_DEFAULTS.read_bytes().count(b"HER" + b"MES" + b".md") == 0


def test_committed_overlay_locks_exactly_the_h0_leaves():
    overlay = load_defaults(COMMITTED_DEFAULTS)["overlay"]
    locked = {".".join(path): value for path, value in hermes_render._leaves(overlay).items()}
    assert locked == H0_LOCKED_LEAVES


def test_committed_overlay_drops_the_guessed_keys():
    overlay = load_defaults(COMMITTED_DEFAULTS)["overlay"]
    assert "gateway" not in overlay  # no gateway on/off key exists at the pin
    assert "user_profile" not in overlay
    assert "enabled" not in overlay["memory"]
    assert overlay["telemetry"]["shared_metrics"] == {"enabled": False, "send": False, "offer_version": 2}


def test_committed_defaults_render_deterministically():
    defaults = load_defaults(COMMITTED_DEFAULTS)
    target = HermesRenderTarget()
    assert target.render_runtime_config(_launch(), defaults, account_home=Path("/h")) == \
        target.render_runtime_config(_launch(), defaults, account_home=Path("/h"))


def test_committed_h0_defaults_allow_a_launch(tmp_path):
    result = prepare_hermes_launch(_inputs(tmp_path, defaults=COMMITTED_DEFAULTS), _launch())
    assert result.status == "rendered"


def test_pre_h0_defaults_refuse_a_launch(tmp_path):
    with pytest.raises(HermesRenderError) as info:
        render_seat_files(
            _inputs(tmp_path, defaults=_captured_defaults(tmp_path, provenance="spec-derived-pre-H0")), _launch()
        )
    assert info.value.reason == "defaults-not-captured"


def test_incomplete_aux_slot_list_refuses_a_launch(tmp_path):
    with pytest.raises(HermesRenderError) as info:
        render_seat_files(
            _inputs(tmp_path, defaults=_captured_defaults(tmp_path, auxiliary_slots_complete=False)), _launch()
        )
    assert info.value.reason == "defaults-not-captured"


# --- prepare_hermes_launch ---------------------------------------------------


def test_prepare_writes_soul_first_then_marker_and_stamp(tmp_path):
    inputs = _inputs(tmp_path)
    order: list[Path] = []

    def recording(path: Path, data: bytes) -> None:
        order.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    result = prepare_hermes_launch(inputs, _launch(), writer=recording)
    home = inputs.account_home
    assert order[0] == home / ".hermes" / "SOUL.md"
    assert order[1:5] == [
        home / ".hermes" / "config.yaml", home / "seat" / ".hermes.md", home / ".hermes" / ".managed",
        home / ".hermes" / ".no-bundled-skills",
    ]
    assert result.status == "rendered" and result.stamp_written
    assert (home / ".hermes" / "SOUL.md").read_text(encoding="utf-8") == "## Identity\n\nYou are a watcher.\n"
    assert (home / "seat" / ".hermes.md").read_text(encoding="utf-8") == SOURCE_TEXT
    assert (home / ".hermes" / ".managed").exists()
    stamp = json.loads((home / ".local/state/pantheon/hermes-render/bindings.json").read_text(encoding="utf-8"))
    assert stamp["source_sha256"] == inputs.source_sha256
    assert stamp["placement"] == "merged"
    seat_config = yaml.safe_load((home / ".hermes" / "config.yaml").read_text(encoding="utf-8"))
    assert seat_config["telemetry"]["shared_metrics"]["offer_version"] == 2  # no consent modal
    assert seat_config["memory"]["memory_enabled"] is False
    assert set(stamp["outputs"]) == {
        ".hermes/SOUL.md", ".hermes/config.yaml", "seat/.hermes.md", ".hermes/.managed", ".hermes/.no-bundled-skills",
    }
    assert "written_at" not in json.dumps(stamp)


def test_prepare_default_writer_is_idempotent(tmp_path):
    inputs = _inputs(tmp_path)
    first = prepare_hermes_launch(inputs, _launch())
    second = prepare_hermes_launch(inputs, _launch())
    assert first.outputs == second.outputs


def test_deepseek_fallback_launch_requires_local_key_holding_proxy_base(tmp_path):
    original = _inputs(tmp_path)
    home = tmp_path / "deepseek-fallback"
    (home / "seat").mkdir(parents=True)
    inputs = dataclasses.replace(original, account_home=home)
    for launch in (_launch(), _launch(base_url="https://api.deepseek.com/v1")):
        with pytest.raises(HermesRenderError) as info:
            prepare_hermes_launch(inputs, launch)
        assert info.value.reason == "egress-proxy-required"
    prepare_hermes_launch(inputs, _launch(base_url="https://deepseek-proxy.invalid:8448/v1"))
    config = yaml.safe_load((home / ".hermes/config.yaml").read_text())
    assert config["model"]["base_url"] == "https://deepseek-proxy.invalid:8448/v1"
    assert all(slot["base_url"] == "https://deepseek-proxy.invalid:8448/v1" for slot in config["auxiliary"].values())


def test_source_hash_mismatch_fails_closed(tmp_path):
    inputs = _inputs(tmp_path, sha="f" * 64)
    with pytest.raises(HermesRenderError) as info:
        prepare_hermes_launch(inputs, _launch())
    assert info.value.reason == "source-hash-mismatch"
    assert not (inputs.account_home / ".hermes").exists()


def test_seat_inside_a_repo_fails_closed(tmp_path):
    inputs = _inputs(tmp_path)
    (inputs.account_home / ".git").mkdir()
    with pytest.raises(HermesRenderError) as info:
        prepare_hermes_launch(inputs, _launch())
    assert info.value.reason == "seat-inside-repo"


def test_missing_identity_heading_fails(tmp_path):
    inputs = _inputs(tmp_path)
    text = "no identity here\n"
    inputs.source_path.write_text(text, encoding="utf-8")
    inputs = HermesRenderInputs(**{**inputs.__dict__, "source_sha256": hashlib.sha256(text.encode()).hexdigest()})
    with pytest.raises(HermesRenderError) as info:
        prepare_hermes_launch(inputs, _launch())
    assert info.value.reason == "identity-heading-missing"


def test_transient_fault_launches_only_on_byte_equal_disk(tmp_path):
    inputs = _inputs(tmp_path)
    prepare_hermes_launch(inputs, _launch())  # good state on disk

    def failing(path: Path, data: bytes) -> None:
        raise OSError(28, "No space left on device")

    result = prepare_hermes_launch(inputs, _launch(), writer=failing)
    assert result.status == "on-disk-equal"
    assert result.write_errors and not result.stamp_written

    (inputs.account_home / ".hermes" / "config.yaml").write_text("tampered: true\n", encoding="utf-8")
    with pytest.raises(HermesRenderError) as info:
        prepare_hermes_launch(inputs, _launch(), writer=failing)
    assert info.value.reason == "transient-write-failure"
    assert "config.yaml" in info.value.detail


def test_adapter_prepare_launch_uses_binding_inputs(tmp_path):
    inputs = _inputs(tmp_path)
    adapter = HermesHarnessAdapter("system-orchestrator", account_home=inputs.account_home, launch=_launch())
    binding = SimpleNamespace(hermes_render=inputs)
    result = adapter.prepare_launch(binding, cwd=inputs.account_home / "seat")
    assert result.status == "rendered"
    with pytest.raises(HermesRenderError) as info:
        adapter.prepare_launch(binding, cwd=tmp_path)
    assert info.value.reason == "cwd-not-seat"


# --- r1 LOW-3: one defaults read; durable rename ------------------------------


def test_defaults_are_read_once_and_stamp_hashes_those_bytes(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    reads: list[Path] = []
    real = hermes_render._read_bytes

    def counting(path: Path):
        if path == inputs.defaults_path:
            reads.append(path)
        return real(path)

    monkeypatch.setattr(hermes_render, "_read_bytes", counting)
    prepare_hermes_launch(inputs, _launch())
    assert len(reads) == 1
    stamp = json.loads(
        (inputs.account_home / ".local/state/pantheon/hermes-render/bindings.json").read_text(encoding="utf-8")
    )
    assert stamp["defaults_sha256"] == hashlib.sha256(inputs.defaults_path.read_bytes()).hexdigest()


def test_atomic_write_fsyncs_the_parent_directory(tmp_path, monkeypatch):
    synced: list[Path] = []
    monkeypatch.setattr(hermes_render, "_fsync_dir", lambda directory: synced.append(directory))
    target = tmp_path / "d" / "f.txt"
    hermes_render.atomic_write(target, b"x")
    assert target.read_bytes() == b"x"
    assert synced == [tmp_path / "d"]
    assert [p.name for p in (tmp_path / "d").iterdir()] == ["f.txt"]


def test_fsync_dir_runs_on_a_real_directory(tmp_path):
    hermes_render._fsync_dir(tmp_path)


# --- r1 MED-2: per-seat paths nested under a locked leaf ---------------------


@pytest.mark.parametrize(
    "per_seat",
    [
        {"mcp_servers": {"evil": {"command": "curl", "args": ["x"]}}},  # under an empty-mapping leaf
        {"nous": {"guest": {"nested": True}}},  # under a scalar leaf
        {"approvals": {"deny": {"glob": "x"}}},  # under a list leaf
    ],
)
def test_nested_under_locked_leaf_is_a_contradiction(tmp_path, per_seat):
    overlay = load_defaults(_captured_defaults(tmp_path))["overlay"]
    with pytest.raises(HermesRenderError) as info:
        hermes_render.check_no_contradiction(per_seat, overlay)
    assert info.value.reason == "safety-default-contradiction"
    assert "nested under locked" in info.value.detail


def test_equal_empty_mapping_leaf_is_not_a_contradiction(tmp_path):
    overlay = load_defaults(_captured_defaults(tmp_path))["overlay"]
    hermes_render.check_no_contradiction({"mcp_servers": {}, "fallback_providers": []}, overlay)


@pytest.mark.parametrize("placement", ["overlay", "merged"])
def test_nested_per_seat_key_is_refused_in_both_placements(tmp_path, placement):
    defaults = load_defaults(_captured_defaults(tmp_path))
    target = HermesRenderTarget(placement=placement)
    with pytest.raises(HermesRenderError) as info:
        target.render_runtime_config(
            _launch(approvals={"mode": "off", "deny": {"smuggled": "x"}}), defaults, account_home=Path("/h")
        )
    assert info.value.reason == "safety-default-contradiction"


# --- r1 MED-1: a seat .env must not carry scope/home/config controls ---------


def _editable_install_layout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "hermes-source"
    venv = root / ".venv"
    executable = venv / "bin" / "hermes"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("version_info = 3.14.8\n", encoding="utf-8")
    metadata = venv / "lib" / "python3.14" / "site-packages" / "hermes_agent-0.0.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "direct_url.json").write_text(
        json.dumps({"url": root.as_uri(), "dir_info": {"editable": True}}), encoding="utf-8"
    )
    (root / "hermes_cli").mkdir()
    (root / "hermes_cli" / "_startup_fast.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='hermes-agent'\n", encoding="utf-8")
    return executable, root


def test_install_root_follows_pinned_editable_package_metadata(tmp_path):
    executable, root = _editable_install_layout(tmp_path)
    assert _REAL_INSTALL_ROOT(executable) == root


def test_dotenv_audit_wires_exactly_three_clean_paths(tmp_path):
    executable, root = _editable_install_layout(tmp_path)
    home = tmp_path / "home"
    harness_home = home / ".hermes"
    harness_home.mkdir(parents=True)
    for path in (harness_home / ".env", harness_home / ".op.env", root / ".env"):
        path.write_text("DEEPSEEK_API_KEY=fixture\n", encoding="utf-8")
    assert hermes_render.dotenv_control_findings(
        home, executable, install_root_resolver=_REAL_INSTALL_ROOT,
    ) == {
        "harness-env": (),
        "harness-op-env": (),
        "install-root-env": (),
    }


@pytest.mark.parametrize("path_class", hermes_render.DOTENV_PATH_CLASSES)
def test_prepare_refuses_forbidden_control_in_every_loaded_dotenv(tmp_path, path_class):
    inputs = _inputs(tmp_path)
    executable, root = _editable_install_layout(tmp_path)
    paths = {
        "harness-env": inputs.account_home / ".hermes" / ".env",
        "harness-op-env": inputs.account_home / ".hermes" / ".op.env",
        "install-root-env": root / ".env",
    }
    paths[path_class].parent.mkdir(parents=True, exist_ok=True)
    paths[path_class].write_text("HERMES_MANAGED_DIR=/tmp/repoint\n", encoding="utf-8")
    with pytest.raises(HermesRenderError) as info:
        prepare_hermes_launch(
            inputs, _launch(executable=executable), install_root_resolver=_REAL_INSTALL_ROOT,
        )
    assert info.value.reason == "env-file-control"
    assert path_class in info.value.detail
    assert not (inputs.account_home / ".hermes" / "config.yaml").exists()


def test_forbidden_env_file_keys():
    assert hermes_render.FORBIDDEN_ENV_FILE_KEYS == {
        "HERMES_MANAGED_DIR", "HERMES_MANAGED", "HERMES_HOME", "HERMES_IGNORE_USER_CONFIG", "HERMES_IGNORE_RULES",
        "HERMES_YOLO_MODE", "HERMES_ACCEPT_HOOKS",
    }


def test_clean_env_file_has_no_controls(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# seat credentials\n\nDEEPSEEK_API_KEY=sk-test\n", encoding="utf-8")
    assert hermes_render.check_env_file_controls(env) == []
    assert hermes_render.check_env_file_controls(tmp_path / "missing.env") == []


def test_bounded_reader_consumes_short_reads_before_classifying(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_bytes(b"DEEPSEEK_API_KEY=x\nHERMES_MANAGED_DIR=/x\n")
    real_read = hermes_render.os.read

    def short_read(fd, count):
        return real_read(fd, min(count, 3))

    monkeypatch.setattr(hermes_render.os, "read", short_read)
    assert hermes_render.check_env_file_controls(env) == ["HERMES_MANAGED_DIR"]


def test_env_control_check_rejects_symlink_and_oversize(tmp_path):
    target = tmp_path / "target"
    target.write_text("DEEPSEEK_API_KEY=x\n", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert hermes_render.check_env_file_controls(link) == [hermes_render.ENV_FILE_SYMLINK]
    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * (hermes_render.MAX_ENV_FILE_BYTES + 1))
    assert hermes_render.check_env_file_controls(oversized) == [hermes_render.ENV_FILE_OVERSIZE]


def test_api_key_alone_is_not_flagged(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DEEPSEEK_API_KEY=sk-test", encoding="utf-8")
    assert hermes_render.check_env_file_controls(env) == []


@pytest.mark.parametrize(
    "line",
    [
        "HERMES_MANAGED_DIR=/home/seat-account/x",
        'HERMES_MANAGED_DIR="/home/seat-account/x"',
        "HERMES_MANAGED_DIR='/home/seat-account/x'  # repoint",
        "export HERMES_MANAGED_DIR=/home/seat-account/x",
        "   export   HERMES_MANAGED_DIR = /home/seat-account/x",
        "HERMES_MANAGED_DIR=",
    ],
)
def test_managed_dir_repoint_is_flagged(tmp_path, line):
    env = tmp_path / ".env"
    env.write_text(f"DEEPSEEK_API_KEY=sk-test\n{line}\n", encoding="utf-8")
    assert hermes_render.check_env_file_controls(env) == ["HERMES_MANAGED_DIR"]


def test_every_forbidden_key_is_flagged_and_comments_are_not(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# HERMES_HOME=/commented/out\n"
        "HERMES_MANAGED=1\nHERMES_HOME=/tmp/h\nHERMES_IGNORE_USER_CONFIG=1\nHERMES_IGNORE_RULES=1\n"
        "HERMES_MANAGED_DIRX=1\nXHERMES_HOME=1\n",
        encoding="utf-8",
    )
    assert hermes_render.check_env_file_controls(env) == [
        "HERMES_HOME", "HERMES_IGNORE_RULES", "HERMES_IGNORE_USER_CONFIG", "HERMES_MANAGED",
    ]


# --- r2: decode as Hermes does (env_loader.py ~:384-452), match fail-closed ---


def _env_bytes(tmp_path: Path, data: bytes) -> Path:
    env = tmp_path / ".env"
    env.write_bytes(data)
    return env


@pytest.mark.parametrize(
    "data",
    [
        b"\xef\xbb\xbfHERMES_MANAGED_DIR=/x\n",  # (i) UTF-8 BOM on the first line
        b"'HERMES_MANAGED_DIR'=/x\n",  # (ii) single-quoted key
        b'"HERMES_MANAGED_DIR"=/x\n',  # double-quoted key
        b"export 'HERMES_MANAGED_DIR'=/x\n",
        b"HERMES_MANAGED_DIR\x00=/x\n",  # (iii) embedded NUL, stripped by Hermes
        b"HERMES_\x00MANAGED_DIR=/x\n",
        "HERMES_MANAGED_DIR=/x\n".encode("utf-16"),  # (iv) UTF-16 with a BOM (native order)
        codecs.BOM_UTF16_LE + "HERMES_MANAGED_DIR=/x\r\n".encode("utf-16-le"),
        codecs.BOM_UTF16_BE + "HERMES_MANAGED_DIR=/x\n".encode("utf-16-be"),
        b"DEEPSEEK_API_KEY=a\rHERMES_MANAGED_DIR=/x",  # bare CR line ending
    ],
)
def test_hermes_loaded_forms_are_flagged(tmp_path, data):
    assert hermes_render.check_env_file_controls(_env_bytes(tmp_path, data)) == ["HERMES_MANAGED_DIR"]


def test_backstop_flags_a_key_word_anywhere_before_the_first_equals(tmp_path):
    env = _env_bytes(tmp_path, b"weird HERMES_MANAGED_DIR junk=/x\nDEEPSEEK_API_KEY=HERMES_HOME\n")
    assert hermes_render.check_env_file_controls(env) == ["HERMES_MANAGED_DIR"]  # values are not keys


@pytest.mark.parametrize(
    "data",
    [
        b"HERMES_MANAGED_DIR=\xff\xfe\xfa\n",  # invalid UTF-8
        codecs.BOM_UTF32_LE + "HERMES_MANAGED_DIR=/x".encode("utf-32-le"),  # Hermes leaves UTF-32 untouched
        codecs.BOM_UTF32_BE + "DEEPSEEK_API_KEY=a".encode("utf-32-be"),
        codecs.BOM_UTF16_LE + b"\x00\xd8",  # a lone UTF-16 surrogate
    ],
)
def test_undecodable_file_fails_closed(tmp_path, data):
    assert hermes_render.check_env_file_controls(_env_bytes(tmp_path, data)) == [hermes_render.ENV_FILE_UNDECODABLE]


@pytest.mark.parametrize("key", ["HERMES_YOLO_MODE", "HERMES_ACCEPT_HOOKS"])
def test_lock_defeat_keys_are_flagged(tmp_path, key):
    env = _env_bytes(tmp_path, f"DEEPSEEK_API_KEY=a\nexport {key}=1\n".encode())
    assert hermes_render.check_env_file_controls(env) == [key]


def test_bom_and_nul_clean_file_is_still_clean(tmp_path):
    env = _env_bytes(tmp_path, b"\xef\xbb\xbf# creds\nDEEPSEEK_API_KEY=sk\x00-test\n")
    assert hermes_render.check_env_file_controls(env) == []
    utf16 = _env_bytes(tmp_path, "DEEPSEEK_API_KEY=sk-test\n".encode("utf-16"))
    assert hermes_render.check_env_file_controls(utf16) == []
