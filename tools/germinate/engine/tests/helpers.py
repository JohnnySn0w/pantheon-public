"""tmp_path stand-ins for the engine tests: a checkout with a registry, contract
modules with real digests, planner modules, and a simulated host."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from tools.germinate.engine.backend import SimulatedHostBackend
from tools.germinate.engine.executor import Engine
from tools.germinate.engine.handlers_digest import compute as compute_handlers_digest
from tools.germinate.engine.invariant_digest import digest_of
from tools.germinate.registry.contract_digest import compute_contract_digest
from tools.germinate.registry.tests import fixtures as F

LIST_DOC = {"invariant_list": {"id": "org.pantheon.test/v1", "invariants": [{"id": "T1", "class": "presence-mode"}]}}
LIST_DIGEST = digest_of(LIST_DOC)


def file_op(op_id: str, target: str, content: str, **extra: Any) -> dict[str, Any]:
    op = {
        "operation_id": op_id,
        "layer": 1,
        "kind": "atomic-write",
        "target": target,
        "precondition": {},
        "desired": {"content": content, "sha256": __import__("hashlib").sha256(content.encode()).hexdigest()},
        "inverse": None,
        "privilege": "seat",
        "owner": "sim",
        "provenance": {"source": "test"},
        "crash_contract": "write-ahead-inverse-durable",
        "side_effect_free": False,
    }
    op.update(extra)
    return op


def dir_op(op_id: str, target: str, **extra: Any) -> dict[str, Any]:
    op = file_op(op_id, target, "", **extra)
    op.update(kind="mkdir", desired={"mode": "0755"}, crash_contract=extra.get("crash_contract", "stageable"))
    return op


def contract_body(cid: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": cid,
        "steward": "orchestrator",
        "version": 1,
        "planner_kind": "python",
        "contract_digest": "sha256:" + "0" * 64,
        "depends_on": [],
        "scopes": ["system", "seat"],
        "privilege": {"seat": ["mkdir", "atomic-write"]},
        "legs": {"install": "plan_install(manifest, registry, scope) -> [Operation]", "remove": "plan_remove"},
        "receipts": {"schema": "pantheon-receipt/v1"},
    }
    body.update(overrides)
    return body


def write_contract(repo: Path, cid: str, *, ops: dict[str, list] | None = None, planner_src: str | None = None, **overrides: Any) -> tuple[Path, str]:
    """Write tools/germinate/components/<cid>.yaml (+ .py) with its true digest."""

    directory = repo / "tools" / "germinate" / "components"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{cid}.yaml"
    body = contract_body(cid, **overrides)
    path.write_text(yaml.safe_dump({"component": body}, sort_keys=False), encoding="utf-8")
    if "invariant_list_digest" in body:
        (directory / f"{cid}.invariants.yaml").write_text(yaml.safe_dump(LIST_DOC, sort_keys=False), encoding="utf-8")
    digest = compute_contract_digest(path)
    body["contract_digest"] = digest
    path.write_text(yaml.safe_dump({"component": body}, sort_keys=False), encoding="utf-8")
    if planner_src is None:
        ops = ops if ops is not None else {"install": [], "remove": []}
        planner_src = "import json\n_OPS = json.loads(%r)\n" % json.dumps(ops)
        for leg in ("install", "configure", "update", "remove", "repair"):
            planner_src += f"\ndef plan_{leg}(request):\n    return _OPS.get({leg!r}, [])\n"
    (directory / f"{cid}.py").write_text(planner_src, encoding="utf-8")
    return path, digest


def repin_contract(repo: Path, cid: str) -> str:
    """Re-pin ``<cid>.yaml`` after its handlers changed (T-5477): handlers_digest, then contract_digest."""

    path = repo / "tools" / "germinate" / "components" / f"{cid}.yaml"
    body = yaml.safe_load(path.read_text(encoding="utf-8"))["component"]
    handlers = path.with_name(f"{cid}_handlers.py")
    if handlers.exists():
        body["handlers_digest"] = compute_handlers_digest(handlers).digest
    else:
        body.pop("handlers_digest", None)
    path.write_text(yaml.safe_dump({"component": body}, sort_keys=False), encoding="utf-8")
    body["contract_digest"] = compute_contract_digest(path)
    path.write_text(yaml.safe_dump({"component": body}, sort_keys=False), encoding="utf-8")
    return body["contract_digest"]


def write_handlers(repo: Path, cid: str, src: str) -> str:
    """Write ``<cid>_handlers.py`` and re-pin its contract; returns the new contract digest for the row."""

    (repo / "tools" / "germinate" / "components" / f"{cid}_handlers.py").write_text(src, encoding="utf-8")
    return repin_contract(repo, cid)


def row(cid: str, digest: str, **overrides: Any) -> dict[str, Any]:
    r = F.component(
        id=cid,
        contract={"repo": "pantheon-config", "module": f"tools/germinate/components/{cid}.yaml", "digest": digest},
    )
    r.update(overrides)
    return r


def make_checkout(root: Path, rows: list[dict[str, Any]]) -> Path:
    return F.make_repo(root, F.doc(*rows))


def sim(tmp_path: Path, name: str = "sim", **kwargs: Any) -> SimulatedHostBackend:
    backend = SimulatedHostBackend(tmp_path / name, **kwargs)
    backend.simulate_postboot()
    return backend


def engine(tmp_path: Path, repo: Path, backend: SimulatedHostBackend | None = None, **kwargs: Any) -> Engine:
    """The CLI's construction path (component handlers and kinds wired in), postboot simulated."""

    if backend is not None:
        return Engine(backend, repo_root=repo, **kwargs)
    eng = Engine.from_checkout(tmp_path / "sim", repo_root=repo, **kwargs)
    eng.backend.simulate_postboot()
    return eng


def tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every path under ``root`` with (size, mtime_ns): a write anywhere changes it."""

    return {str(p.relative_to(root)): (p.lstat().st_size, p.lstat().st_mtime_ns) for p in sorted(root.rglob("*"))}


def simple_setup(tmp_path: Path, ops: dict[str, list] | None = None, **contract_overrides: Any) -> tuple[Path, Engine]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _path, digest = write_contract(repo, "comp-a", ops=ops, **contract_overrides)
    make_checkout(repo, [row("comp-a", digest)])
    return repo, engine(tmp_path, repo)
