"""
SAFETY BOUNDARY tests (the most important tests in this package).

Prove that research code cannot place orders or open/mutate a live journal:
  1. No module under research_pipeline imports `bot` or the `coinbase` brokerage SDK (static AST).
  2. No module contains a call to an order-submitting method (static AST call scan).
  3. Importing the package in a clean CWD creates no journal database and loads no bot module
     (subprocess — proves the import-time side effect described in ARCHITECTURE_REVIEW F-C1 is absent).
  4. ResearchStore refuses to open journal.db / live_journal.db / paper_journal.db.
"""
import ast
import json
import os
import subprocess
import sys

import pytest

import research_pipeline
from research_pipeline.storage import ResearchStore, AppendOnlyError

PKG_DIR = os.path.dirname(research_pipeline.__file__)
REPO_ROOT = os.path.abspath(os.path.join(PKG_DIR, ".."))

_FORBIDDEN_CALL_ATTRS = {
    "create_order", "submit_order_intent", "place_order", "cancel_orders",
}


def _py_files():
    for root, _dirs, files in os.walk(PKG_DIR):
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _trees():
    for path in _py_files():
        with open(path, "r", encoding="utf-8") as fh:
            yield path, ast.parse(fh.read(), filename=path)


def test_no_bot_or_brokerage_imports():
    offenders = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "bot" or a.name.startswith("bot."):
                        offenders.append((path, a.name))
                    if a.name == "coinbase" or a.name.startswith("coinbase."):
                        offenders.append((path, a.name))
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    if node.module == "bot" or node.module.startswith("bot."):
                        offenders.append((path, node.module))
                    if node.module == "coinbase" or node.module.startswith("coinbase."):
                        offenders.append((path, node.module))
    assert offenders == [], f"forbidden imports found: {offenders}"


def test_no_order_submitting_calls():
    offenders = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in _FORBIDDEN_CALL_ATTRS:
                    offenders.append((path, node.func.attr))
    assert offenders == [], f"order-submitting calls found: {offenders}"


def test_import_creates_no_journal_and_loads_no_bot(tmp_path):
    code = (
        "import os,sys,json;"
        "import research_pipeline.cli.smoke;"
        "import research_pipeline.cli.collect;"
        "import research_pipeline.governance.gates;"
        "import research_pipeline.context.base;"
        "bad=[m for m in sys.modules if m=='bot' or m.startswith('bot.')];"
        "print(json.dumps({'bot':bad,"
        "'journal':os.path.exists('journal.db'),"
        "'live':os.path.exists('live_journal.db'),"
        "'paper':os.path.exists('paper_journal.db')}))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path),
                          env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["bot"] == [], f"bot modules imported as a side effect: {out['bot']}"
    assert not out["journal"], "importing research_pipeline created journal.db"
    assert not out["live"], "importing research_pipeline created live_journal.db"
    assert not out["paper"], "importing research_pipeline created paper_journal.db"


@pytest.mark.parametrize("name", ["journal.db", "live_journal.db", "paper_journal.db"])
def test_store_refuses_journal_databases(tmp_path, name):
    with pytest.raises(AppendOnlyError):
        ResearchStore(str(tmp_path / name))
