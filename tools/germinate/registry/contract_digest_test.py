"""Golden vectors for the sole contract_digest oracle (ORG-080 §2 "Golden vectors").

Any change to ``_canonical_yaml_emit`` that alters the bytes for the golden
fixture fails here. The expected value was produced under PyYAML 6.0.1; CI
(PyYAML 6.0.3) is the proof for the other pinned version.
"""

from __future__ import annotations

import pytest
import yaml

from tools.germinate.registry import contract_digest as CD

GOLDEN_MODULE = """\
component:
  id: harness-manager
  steward: orchestrator
  version: 1
  planner_kind: python
  contract_digest: sha256:0000000000000000000000000000000000000000000000000000000000000000
  depends_on: [host-substrate, mailbox]
  scopes: [system, seat]
  desired_schema: {harness: {enum: [claude, codex]}}
  note: "naïve — unicode passes through"
  privilege:
    seat: [mkdir, atomic-write]
"""
GOLDEN_DIGEST = "sha256:a45cf0ed8d57ca147b9856ebbd1586c0983a07310cdd89031d1e084abc8b0421"


def _write(tmp_path, text, name="c.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_golden_vector(tmp_path):
    assert CD.compute_contract_digest(_write(tmp_path, GOLDEN_MODULE)) == GOLDEN_DIGEST


def test_written_digest_value_is_not_part_of_the_digest(tmp_path):
    other = GOLDEN_MODULE.replace("sha256:" + "0" * 64, "anything-at-all")
    assert CD.compute_contract_digest(_write(tmp_path, other)) == GOLDEN_DIGEST


def test_key_order_and_flow_style_do_not_matter(tmp_path):
    doc = yaml.safe_load(GOLDEN_MODULE)
    reordered = {"component": dict(reversed(list(doc["component"].items())))}
    text = yaml.safe_dump(reordered, sort_keys=False, default_flow_style=True, allow_unicode=False)
    assert CD.compute_contract_digest(_write(tmp_path, text)) == GOLDEN_DIGEST


def test_any_content_change_changes_the_digest(tmp_path):
    changed = GOLDEN_MODULE.replace("steward: orchestrator", "steward: recovery")
    assert CD.compute_contract_digest(_write(tmp_path, changed)) != GOLDEN_DIGEST


def test_sentinel_is_the_adr_constant():
    assert CD.SENTINEL == "SHA256_DIGEST_PLACEHOLDER" and len(CD.SENTINEL) == 25


@pytest.mark.parametrize(
    "text",
    [
        "component:\n  id: a\n  contract_digest: x\n  nested:\n    contract_digest: y\n",
        "component:\n  id: a\n  items:\n  - contract_digest: y\n",
        "component:\n  id: a\n",
        "component:\n  id: a\n  contract_digest: x\nextra: 1\n",
        "- component\n",
        "component: [1, 2]\n",
        "component:\n  id: a\n  contract_digest: x\n  contract_digest: y\n",
    ],
)
def test_ambiguous_or_malformed_modules_refuse(tmp_path, text):
    with pytest.raises(CD.ContractDigestError):
        CD.compute_contract_digest(_write(tmp_path, text))


def test_unreadable_module_refuses(tmp_path):
    with pytest.raises(CD.ContractDigestError):
        CD.compute_contract_digest(tmp_path / "absent.yaml")


def test_unpinned_emitter_version_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(yaml, "__version__", "7.0.0")
    with pytest.raises(CD.ContractDigestError, match="not a pinned"):
        CD.compute_contract_digest(_write(tmp_path, GOLDEN_MODULE))


def test_canonical_bytes_are_lf_terminated_without_trailing_space():
    data = CD._canonical_yaml_emit({"component": {"b": "x ", "a": [1, 2]}})
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    assert all(line == line.rstrip() for line in data.decode().split("\n"))
