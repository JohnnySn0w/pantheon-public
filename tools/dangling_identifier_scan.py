#!/usr/bin/env python3
"""tools/dangling_identifier_scan.py — find identifiers that ONLY exist in prose (T-2151).

THE DEFECT CLASS. A comment, docstring or doc names an identifier — an env var, a
function — that appears nowhere in the actual code. The doc is then confidently
wrong, and nothing catches it: the code compiles, the tests pass, and the reader
believes the comment. T-1988 closed six of these in one repo; four were exactly this
shape (two env vars that exist nowhere, and four cites of a symbol T-0508 removed).

WHY IT RECURS, and why this is a class fix rather than another round of manual
audits: renaming an identifier updates the CODE and leaves the PROSE naming it
untouched. Genericization does this systematically (rename in code, comments keep the
old name — T-2151's origin), but so does ordinary refactoring, and so does deleting a
subsystem while its explanatory comments survive elsewhere.

THE CHECK. For each candidate identifier mentioned anywhere in a comment/doc, ask:
does this token appear ANYWHERE in this tree outside a comment? If not, it is
DANGLING — the prose is the only thing that believes it exists.

DELIBERATELY NARROW. This reports only high-signal shapes, because a scanner that
cries wolf gets muted and a muted check is worse than none:
  * PREFIXED SCREAMING_SNAKE env vars (KINO_FOO, PANTHEON_BAR) — near-zero ambiguity.
  * Symbol cites written with call parens (loadRegistry()) — the parens are the
    author asserting "this is a function that exists".
It does NOT try to validate prose nouns, type names, or bare CamelCase; those are
where false positives live.

NOT A LEAK GATE. A dangling identifier is a CORRECTNESS defect, not an exposure, so
this reports separately from any leak scan and must never gate on leak semantics.
Exit 1 on findings so CI can use it, but prefer running it as a report first.

Usage:
    dangling_identifier_scan.py <root> [--prefix KINO_ --prefix PANTHEON_]
                                       [--include-calls] [--quiet]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Files whose content we read. Deliberately excludes lockfiles/binaries/vendor.
_CODE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".svelte", ".py", ".sh", ".bash",
                  ".toml", ".yaml", ".yml", ".json", ".md", ".service", ".timer", ".path"}
_SKIP_DIRS = {".git", "node_modules", "build", "dist", ".svelte-kit", "__pycache__",
              ".venv", "venv", "target", ".cache"}

# Comment forms, per family. (regex, group holding the comment body)
_LINE_COMMENT = re.compile(r"(?://|#)\s?(.*)$")
_BLOCK_OPEN = re.compile(r"/\*|\"\"\"|'''")

_ENVVAR = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
_CALL = re.compile(r"\b([a-z][A-Za-z0-9]*)\(\)")

# DELIBERATE HISTORICAL MENTIONS. Naming something that no longer exists is often
# exactly right — "T-0508 removed loadRegistry / AgentManager" is CORRECT prose, and
# a scanner that nags on it teaches people to ignore the scanner. So a comment may
# opt out explicitly. The marker is greppable on purpose: it makes deliberate
# historical references auditable rather than invisible, and it forces the author to
# assert the mention is intentional instead of the tool guessing from wording
# ("removed", "used to", "no longer") — which would be fragile and would silently
# suppress genuine defects phrased in the past tense.
_SUPPRESS = re.compile(r"\[dangling-ok(?::[^\]]*)?\]")


def _iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in _CODE_SUFFIXES:
            yield p


def _split_python(text: str) -> tuple[list[str], list[str]]:
    """Python via its OWN tokenizer, not regex.

    The regex form got this wrong and produced a false positive on a real file:
    naive triple-quote toggling desynchronised across 27 delimiters, classified the
    back half of the module as prose, and so reported env vars that ARE read in code
    as dangling. A scanner that cries wolf gets muted, so this path is exact.

    Classification: COMMENT tokens are prose. A triple-quoted STRING whose line
    begins with the quote is a docstring, so also prose. EVERY OTHER STRING IS CODE
    — that is deliberate and load-bearing, because os.environ.get("KINO_X") is how
    an env var is legitimately referenced; treating string literals as prose would
    hide exactly the usage that proves an identifier real.
    """
    import io
    import tokenize

    comments: list[str] = []
    code: list[str] = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Unparseable (partial file, py2, template) — fall back to line form rather
        # than claiming a clean result we did not earn.
        return _split_generic(text)
    lines = text.splitlines()
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            comments.append(tok.string)
        elif tok.type == tokenize.STRING:
            row = tok.start[0] - 1
            raw = lines[row] if 0 <= row < len(lines) else ""
            is_docstring = raw.lstrip().startswith(('"""', "'''", 'r"""', "r'''"))
            (comments if is_docstring else code).append(tok.string)
        else:
            code.append(tok.string)
    return comments, code


def _split_generic(text: str) -> tuple[list[str], list[str]]:
    """Line-oriented fallback for non-Python sources."""
    comments, code, in_block = [], [], False
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if in_block:
            comments.append(line)
            if "*/" in line:
                in_block = False
            continue
        stripped = line.lstrip()
        if "/*" in line and "*/" not in line:
            comments.append(line)
            in_block = True
            continue
        if stripped.startswith(("//", "#", "*", "/*")):
            comments.append(line)
            continue
        m = _LINE_COMMENT.search(line)
        if m:
            comments.append(m.group(1))
            code.append(line[: m.start()])
        else:
            code.append(line)
    return comments, code


def _split_comment_code(path: Path, text: str) -> tuple[list[str], list[str]]:
    """Return (comment_lines, code_lines). Markdown is entirely prose.

    Over-classifying a line as prose is the SAFE direction for the generic path — it
    can only suppress a finding, never invent one. Python gets exact treatment via
    its own tokenizer (see _split_python) because the approximate form produced a
    real false positive there.
    """
    suffix = path.suffix.lower()
    if suffix == ".md":
        return text.splitlines(), []
    if suffix == ".py":
        return _split_python(text)
    return _split_generic(text)


def scan(root: Path, prefixes: tuple[str, ...], include_calls: bool) -> list[tuple[str, str, int, str]]:
    """Return [(kind, identifier, line_no, 'path:line')] for dangling identifiers."""
    mentioned: dict[tuple[str, str], tuple[Path, int]] = {}
    code_tokens: set[str] = set()

    for path in _iter_files(root):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        comments, code = _split_comment_code(path, text)
        code_blob = "\n".join(code)
        code_tokens.update(_ENVVAR.findall(code_blob))
        if include_calls:
            # a definition/usage in code need not have () — capture bare names too
            code_tokens.update(re.findall(r"\b([a-z][A-Za-z0-9]*)\b", code_blob))
        for i, cline in enumerate(comments, start=1):
            if _SUPPRESS.search(cline):
                continue  # author asserted this mention is deliberately historical
            for ident in _ENVVAR.findall(cline):
                if prefixes and not ident.startswith(prefixes):
                    continue
                mentioned.setdefault(("env", ident), (path, i))
            if include_calls:
                for ident in _CALL.findall(cline):
                    mentioned.setdefault(("call", ident), (path, i))

    findings = []
    for (kind, ident), (path, line) in sorted(mentioned.items()):
        if ident not in code_tokens:
            findings.append((kind, ident, line, str(path)))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("--prefix", action="append", default=[],
                    help="only report SCREAMING_SNAKE idents with this prefix (repeatable). "
                         "Omit to report all prefixed-looking env vars.")
    ap.add_argument("--include-calls", action="store_true",
                    help="also report symbol() cites whose bare name is absent from code")
    ap.add_argument("--quiet", action="store_true", help="print only findings")
    args = ap.parse_args(argv)

    if not args.root.is_dir():
        print(f"error: {args.root} is not a directory", file=sys.stderr)
        return 2

    findings = scan(args.root, tuple(args.prefix), args.include_calls)
    if not args.quiet:
        print(f"[dangling-scan] root={args.root} prefixes={args.prefix or '(any)'} "
              f"calls={'on' if args.include_calls else 'off'}")
    if not findings:
        if not args.quiet:
            print("[dangling-scan] no dangling identifiers found")
        return 0
    print(f"[dangling-scan] {len(findings)} dangling identifier(s) — named in prose, absent from code:")
    for kind, ident, line, path in findings:
        print(f"  {kind:5} {ident:38} {path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
