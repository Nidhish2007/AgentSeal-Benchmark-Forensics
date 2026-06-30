"""Minimal smoke test for source installs."""

import compileall
import importlib
import sys

mods = [
    "agentseal",
    "agentseal.cli",
    "agentseal.engine",
    "agentseal.report",
    "agentseal.tui",
]

ok = True
for mod in mods:
    try:
        importlib.import_module(mod)
        print(f"OK import {mod}")
    except Exception as exc:
        ok = False
        print(f"FAIL import {mod}: {exc}")

if not compileall.compile_dir("agentseal", quiet=1):
    ok = False
    print("FAIL compileall")
else:
    print("OK compileall")

sys.exit(0 if ok else 1)
