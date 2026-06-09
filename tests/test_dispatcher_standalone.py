"""Phase 3 A4: prove the dispatcher is a self-contained, stdlib-only package.

These tests are the contract that lets the dispatcher be lifted into its own
repo (NFS-247/ai-peer-review) without dragging StockTrader along. They fail
loudly if anyone ever:
  - imports from `backend`, `scripts.<other>`, or any sibling outside the
    dispatcher package, or
  - adds a third-party (non-stdlib, non-package) import.

If these fail, the dispatcher is no longer portable and the failing import
must be removed or vendored before extraction.
"""

import ast
import importlib
import pkgutil
import sys
from pathlib import Path

import scripts.dispatcher as dispatcher_pkg


PKG_DIR = Path(dispatcher_pkg.__file__).parent
PKG_NAME = "scripts.dispatcher"

# Modules in the Python standard library this package is allowed to use.
# (3.10+ exposes sys.stdlib_module_names; fall back to an explicit set.)
_STDLIB = set(getattr(sys, "stdlib_module_names", set())) | {
    "ast", "abc", "dataclasses", "datetime", "enum", "fnmatch", "hashlib",
    "hmac", "json", "os", "pathlib", "re", "sys", "textwrap", "typing",
    "urllib", "importlib", "pkgutil", "collections", "functools", "itertools",
}


def _dispatcher_module_names() -> list[str]:
    names = []
    for mod in pkgutil.iter_modules([str(PKG_DIR)]):
        if mod.name.startswith("__"):
            continue
        names.append(f"{PKG_NAME}.{mod.name}")
    return names


def _imports_in_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import within the package — always allowed.
                continue
            if node.module:
                out.append(node.module)
    return out


def test_every_module_imports_cleanly():
    # All dispatcher modules import without error and without pulling in
    # anything from backend/.
    for name in _dispatcher_module_names():
        importlib.import_module(name)


def test_no_imports_outside_stdlib_or_package():
    offenders = {}
    for py in sorted(PKG_DIR.glob("*.py")):
        for imported in _imports_in_file(py):
            top = imported.split(".", 1)[0]
            if top in _STDLIB:
                continue
            if imported.startswith(PKG_NAME) or top == "scripts":
                # Absolute self-reference is fine; relative handled separately.
                if imported.startswith(PKG_NAME):
                    continue
            # Anything else is an external dependency or a cross-package import.
            offenders.setdefault(py.name, []).append(imported)
    assert not offenders, (
        "Dispatcher must stay stdlib-only and self-contained for extraction. "
        f"Disallowed imports found: {offenders}"
    )


def test_no_backend_or_sibling_references_in_source():
    # Belt-and-suspenders: grep the source for forbidden import roots.
    forbidden = ("from backend", "import backend", "from scripts.app", "scripts.backend")
    hits = {}
    for py in sorted(PKG_DIR.glob("*.py")):
        text = py.read_text(encoding="utf-8")
        for f in forbidden:
            if f in text:
                hits.setdefault(py.name, []).append(f)
    assert not hits, f"Forbidden cross-package references: {hits}"
