"""`germinate registry {show,render,validate,repin,lint-adrs}` (ORG-080 §1, ORG-081 §5)."""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path
from typing import TextIO

import yaml

from tools.germinate.registry import adr_link, render
from tools.germinate.registry.load import RegistryLoadError, load_file
from tools.germinate.registry.validate import REGISTRY_REL, Finding, errors, validate_repo
from tools.remote_resolve import RemoteResolveError, load_production_manifest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    root = Path(args.repo_root).resolve() if args.repo_root else REPO_ROOT
    registry = Path(args.registry) if args.registry else root / REGISTRY_REL
    return root, registry


def _report(findings: list[Finding], out: TextIO) -> int:
    for finding in findings:
        print(str(finding), file=out)
    bad = errors(findings)
    if bad:
        print(f"{len(bad)} error(s)", file=out)
        return 1
    return 0


def _load(registry: Path) -> dict | None:
    try:
        doc = load_file(registry)
    except RegistryLoadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    if not isinstance(doc, dict):
        print(f"error: {registry}: top level must be a mapping", file=sys.stderr)
        return None
    return doc


def _git_resolver(root: Path):
    """The opt-in ORG-081 §7 resolver: URLs come through ``resolve_remote`` (seat pin, host binding)."""
    from tools.germinate.registry.git_resolver import GitExternalResolver
    from tools.remote_resolve import resolve_remote

    manifest = load_production_manifest(repo_root=root)
    return GitExternalResolver(lambda repo: resolve_remote(repo, manifest=manifest).url)


def _cmd_validate(args: argparse.Namespace) -> int:
    root, registry = _paths(args)
    today = _dt.date.fromisoformat(args.today) if args.today else None
    if getattr(args, "resolve_external", False):
        try:
            resolver = _git_resolver(root)
        except (RemoteResolveError, OSError, yaml.YAMLError) as exc:
            print(f"error: --resolve-external: cannot load the repos.yaml manifest: {exc}", file=sys.stderr)
            return 1
        with resolver:
            findings = validate_repo(root, registry=registry, today=today, resolver=resolver)
    else:
        findings = validate_repo(root, registry=registry, today=today, resolver=None)
    rc = _report(findings, sys.stderr)
    if rc == 0:
        print(f"ok: {registry} is valid ({len(findings)} warning(s))")
    return rc


def _cmd_render(args: argparse.Namespace) -> int:
    root, registry = _paths(args)
    doc = _load(registry)
    if doc is None:
        return 1
    text = render.render_matrix(doc)
    if not args.check:
        sys.stdout.write(text)
        return 0
    target = Path(args.rendered) if args.rendered else root / render.RENDERED_REL
    try:
        current = target.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != text:
        print(
            f"error: {target} is stale; regenerate with "
            "`python3 -m tools.germinate registry render > registry/system-coverage.md`",
            file=sys.stderr,
        )
        return 1
    print(f"ok: {target} matches the registry")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    _root, registry = _paths(args)
    doc = _load(registry)
    if doc is None:
        return 1
    sys.stdout.write(render.render_show(doc))
    return 0


def _cmd_lint_adrs(args: argparse.Namespace) -> int:
    root, registry = _paths(args)
    doc = _load(registry)
    if doc is None:
        return 1
    if args.diff is not None:
        if args.diff == "-":
            buf = getattr(sys.stdin, "buffer", None)
            data = buf.read() if buf is not None else sys.stdin.read()
        else:
            try:
                data = Path(args.diff).read_bytes()
            except OSError as exc:
                print(f"error: cannot read diff {args.diff} ({exc.strerror})", file=sys.stderr)
                return 2
        findings = adr_link.scan_diff(data, doc)
    else:
        findings = adr_link.scan_tree(root, doc)
    rc = _report(findings, sys.stderr)
    if rc == 0:
        print(f"ok: ORG-081 D-10 ADR link clean ({len(findings)} warning(s))")
    return rc


def _cmd_repin(args: argparse.Namespace) -> int:
    from tools.germinate.registry import repin as R

    root, registry = _paths(args)
    if not args.component and not args.check:
        print("error: name a component id to re-pin (only --check may run over every pinned component)", file=sys.stderr)
        return 2
    out = sys.stderr if args.check else sys.stdout
    drifted: list[str] = []
    try:
        ids = list(dict.fromkeys(args.component)) or R.pinned_components(registry)
        for cid in ids:  # sequential: each repin reads the registry the previous one wrote
            result = R.repin(root, registry, cid)
            for line in result.drift:
                print(f"{cid}: {line}", file=out)
            if result.drift:
                drifted.append(cid)
                if not args.check:
                    result.write()
    except (R.RepinError, OSError, RegistryLoadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.check:
        if drifted:
            print(
                f"error: {len(drifted)} component(s) need re-pinning; run "
                f"`python3 -m tools.germinate registry repin {' '.join(drifted)}`",
                file=sys.stderr,
            )
            return 1
        print(f"ok: {len(ids)} component(s) pinned ({', '.join(ids)})")
        return 0
    print(f"ok: re-pinned {', '.join(drifted)}" if drifted else f"ok: {', '.join(ids)} already pinned")
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    reg = subparsers.add_parser("registry", help="the ORG-081 system registry")
    sub = reg.add_subparsers(dest="registry_cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo-root", help="checkout root (default: this repo)")
        p.add_argument("--registry", help="registry file (default: <repo-root>/registry/systems.yaml)")

    p = sub.add_parser("validate", help="schema, enum, evidence, reference and alias checks")
    common(p)
    p.add_argument("--today", help="YYYY-MM-DD used for alias expiry (default: today)")
    p.add_argument(
        "--resolve-external",
        action="store_true",
        help="fetch each external contract at its repos.yaml revision pin (network; needs the seat manifest pin, "
        "ORG-081 §7). Default off: every external row stays fail-closed",
    )
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("render", help="print the ORG-068 coverage matrix as markdown")
    common(p)
    p.add_argument("--check", action="store_true", help="exit 1 if the committed render is stale")
    p.add_argument("--rendered", help="rendered file for --check (default: <repo-root>/registry/system-coverage.md)")
    p.set_defaults(func=_cmd_render)

    p = sub.add_parser("show", help="one line per registry row")
    common(p)
    p.set_defaults(func=_cmd_show)

    p = sub.add_parser(
        "repin",
        help="recompute a component's handlers_digest, contract_digest and registry digest in place (T-5477)",
    )
    common(p)
    p.add_argument("component", nargs="*", help="component id(s); with --check and none named, every pinned component")
    p.add_argument("--check", action="store_true", help="write nothing; exit 1 if any pin would change")
    p.set_defaults(func=_cmd_repin)

    p = sub.add_parser("lint-adrs", help="ORG-081 D-10 ADR frontmatter link lint")
    common(p)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--tree", action="store_true", help="state check over every ORG ADR (the default)")
    mode.add_argument("--diff", metavar="FILE", help="creation check over a unified diff (FILE, or - for stdin)")
    p.set_defaults(func=_cmd_lint_adrs)


if __name__ == "__main__":  # T-5989: the module form runs the `registry` group, never a silent exit 0
    from tools.germinate.cli import main as _germinate_main

    sys.exit(_germinate_main(["registry", *sys.argv[1:]]))
