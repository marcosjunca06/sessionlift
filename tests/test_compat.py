"""The code the server runs. The laptop keeps a copy of this package on the server and runs it with
the server's own python3, which may be older than the laptop's: the modules it imports must work on
Python 3.8 with the standard library alone."""

import ast
import re
import sys
import unittest
from pathlib import Path

import sessionlift

PACKAGE = Path(sessionlift.__file__).parent
# `python3 -m sessionlift.server <op>` imports the first five; the shim's `sessionlift hook` the rest.
SERVER_SIDE = {"__init__", "server", "gitsync", "locks", "transcripts",
               "cli", "paths", "hook", "desktop", "context", "config"}
NEWER_THAN_3_8 = re.compile(r"\.(removeprefix|removesuffix|is_relative_to|with_stem)\(|\bzoneinfo\b|"
                            r"functools\.cache\b|math\.lcm|\bmatch\s+\w+\s*:\s*$", re.M)


def top_level_imports(tree: ast.Module):
    """The import statements that run when the module loads (not those inside functions)."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, (ast.If, ast.Try)):
            stack += node.body + node.orelse + getattr(node, "finalbody", [])
            for handler in getattr(node, "handlers", []):
                stack += handler.body


class ServerCodeTest(unittest.TestCase):
    def source(self, name: str) -> str:
        return (PACKAGE / f"{name}.py").read_text(encoding="utf-8")

    def test_it_parses_as_python_3_8(self):
        for name in sorted(SERVER_SIDE):
            with self.subTest(name):
                ast.parse(self.source(name), filename=f"{name}.py", feature_version=(3, 8))
                self.assertIsNone(NEWER_THAN_3_8.search(self.source(name)))

    def test_it_needs_only_the_standard_library_and_itself(self):
        for name in sorted(SERVER_SIDE):
            for node in top_level_imports(ast.parse(self.source(name))):
                if isinstance(node, ast.ImportFrom) and node.level:  # from . import x / from .x import y
                    parts = [node.module] if node.module else [a.name for a in node.names]
                    siblings = {p.split(".")[0] for p in parts if (PACKAGE / f"{p.split('.')[0]}.py").exists()}
                    with self.subTest(name, imports=sorted(siblings)):
                        self.assertLessEqual(siblings, SERVER_SIDE, "a module the server does not get")
                else:
                    tops = [node.module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
                    for top in (t.split(".")[0] for t in tops):
                        with self.subTest(name, imports=top):
                            self.assertIn(top, sys.stdlib_module_names)


if __name__ == "__main__":
    unittest.main()
