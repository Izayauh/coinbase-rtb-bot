# Repo Stabilization Plan

**Baseline:** commit `6894653` ("fix(execution): Restore execution layer and implement robust exchange reconciliation")
**Scope:** cleanup and stabilize only -- no new features.
**Goal:** make the execution/reconcile architecture trustworthy with a clean, reproducible test suite.

---

## Phase 1: Remove committed artifacts

**Problem:**
Seven pytest output files, a standalone debug script, two debug helpers, and a log file are tracked in git. They add noise and have no value in version control.

**Files to delete (git rm):**

| File | Type |
|------|------|
| `pytest_all.txt` | captured output |
| `pytest_all2.txt` | captured output |
| `pytest_output.txt` | captured output |
| `pytest_output2.txt` | captured output |
| `pytest_err.txt` | captured output |
| `pytest_debug.txt` | captured output |
| `pytest_two.txt` | captured output |
| `pytest.log` | log file |
| `test_bug.py` | standalone debug script |
| `bot/tests/debug_sm.py` | manual debug script |
| `bot/tests/debug_sm2.py` | manual debug script |

**gitignore additions:**

```
pytest*.txt
pytest*.log
*.log
.pytest_cache/
```

`*.db` is already present; no change needed there.

**Single commit** after this phase.

---

## Phase 2: Fix test import consistency

**Problem:**
The test suite uses three incompatible import strategies:

| File | Strategy | sys.path hack? |
|------|----------|----------------|
| `test_execution.py` | bare `from models import ...` | no (relies on cwd) |
| `test_reconcile.py` | bare `from db import db` | no (relies on cwd) |
| `test_state_machine.py` | bare after `sys.path.insert(0, '..')` | **yes** |
| `test_db.py` | bare after `sys.path.insert(0, '..')` | **yes** |
| `test_db_migration.py` | package `from bot.db import Database` | no |

Running `pytest` from the repo root fails unless you also set `PYTHONPATH=bot`, and the sys.path hacks in two files fight with that.

**Decision: standardize on package-style imports (`from bot.X import Y`).**

Rationale: `test_db_migration.py` already uses this style, and it works from any working directory without hacks. The bare-import style only works when cwd happens to be `bot/`.

**Steps:**

1. Remove `sys.path.insert(...)` blocks from `test_state_machine.py` and `test_db.py`.
2. Convert all test imports from bare style to package style:
   - `from models import ...` --> `from bot.models import ...`
   - `from db import db` --> `from bot.db import db`
   - `from journal import Journal` --> `from bot.journal import Journal`
   - `from execution import ExecutionService` --> `from bot.execution import ExecutionService`
   - `from risk import RiskManager` --> `from bot.risk import RiskManager`
   - `import db as global_db_module` --> `import bot.db as global_db_module`
   - `import journal` --> `import bot.journal as journal` (or `from bot import journal`)
   - `import strategy` --> `import bot.strategy as strategy`
   - `import state_machine` --> `import bot.state_machine as state_machine`
3. Add `bot/__init__.py` (empty) so `bot` is an importable package.
4. Add `bot/tests/__init__.py` (empty) so pytest discovers tests correctly.
5. Add a `pyproject.toml` (or `pytest.ini` / `setup.cfg`) at the repo root with:
   ```toml
   [tool.pytest.ini_options]
   testpaths = ["bot/tests"]
   ```
   This lets `pytest` be run from the repo root with no `PYTHONPATH` needed.
6. Verify: `pytest` from repo root collects all 5 test files and resolves all imports.

**Also fix internal module imports within `bot/` source:**
The source files (`journal.py`, `execution.py`, `state_machine.py`, etc.) use bare imports like `from db import db`. These work today because python resolves sibling imports within the `bot/` directory, but once `bot/__init__.py` exists, relative or explicit package imports are cleaner. However, changing source imports is a larger refactor. For this stabilization:
- Leave source-internal imports alone for now (they work with the current flat structure).
- Only fix test imports to use the `from bot.X` package style.
- If the source imports break after adding `__init__.py`, convert them to relative imports (`from .db import db`) or absolute package imports.

**Single commit** after this phase.

---

## Phase 3: Fix DB test fixture consistency

**Problem:**
Four different DB setup patterns exist across five test files:

| Pattern | Used by | Issues |
|---------|---------|--------|
| unittest setUp/tearDown + uuid path | `test_execution.py` | Orphaned files if test crashes; mutates global singleton |
| `setup_module` + fixed filename | `test_reconcile.py` | Shared file across tests; races in parallel; `teardown_module` is a no-op (just `pass`) |
| pytest fixture + uuid path + global mutation | `test_state_machine.py`, `test_db.py` | Mutates global `db.db_path` singleton; files may linger |
| pytest `tmp_path` | `test_db_migration.py` | Correct pattern -- pytest cleans up automatically |

All patterns share the same root problem: the global singleton at `bot/db.py:144` (`db = Database()`) is mutated in-place. When test A changes `db.db_path`, that change leaks into test B if import caching isn't accounted for.

**Solution: centralize fixtures in a `conftest.py` using `tmp_path` + monkeypatch.**

1. Create `bot/tests/conftest.py` with a shared `test_db` fixture:
   ```python
   @pytest.fixture
   def test_db(tmp_path, monkeypatch):
       db_file = str(tmp_path / "test.db")
       from bot.db import db
       monkeypatch.setattr(db, "db_path", db_file)
       db._init_db()
       yield db
       # tmp_path auto-cleanup handles the file
   ```
2. Replace per-file DB setup in every test file:
   - `test_execution.py`: convert from unittest.TestCase to pytest functions (or at minimum use the shared fixture via `@pytest.fixture(autouse=True)` adaptation). The unittest class can stay if desired, but the setUp/tearDown must delegate to the conftest fixture.
   - `test_reconcile.py`: remove `setup_module` / `teardown_module` / `clean_tables` fixture, use `test_db` fixture instead (each test gets a fresh empty DB -- no need to DELETE rows).
   - `test_state_machine.py`: replace inline `temp_db` fixture with `test_db`.
   - `test_db.py`: replace inline `temp_db` fixture with `test_db`.
   - `test_db_migration.py`: already correct (uses `tmp_path` directly). No change needed.
3. Because `test_db` gives every test function a fresh, empty database:
   - Eliminate all `DELETE FROM ...` cleanup queries.
   - Eliminate all `os.path.exists() / os.remove()` teardown blocks.
   - Eliminate all `uuid` filename generation in test fixtures.
4. For `test_execution.py` specifically, the unittest.TestCase style could be preserved, but consider converting to pytest functions for consistency with the rest of the suite. If keeping unittest, the `test_db` fixture can be accessed via a module-level `conftest` autouse fixture that patches `db.db_path` before each test method.

**Important detail -- the `from db import db` in `journal.py` and `execution.py`:**
When test code does `monkeypatch.setattr(db, "db_path", ...)`, it patches the *same object* that `journal.py` and `execution.py` imported (since Python caches module-level objects). `monkeypatch` restores the original value after each test. This is the correct isolation pattern.

**Single commit** after this phase.

---

## Phase 4: Revert journal bar-volume merge regression

**Problem:**
`bot/journal.py:21` reads:
```sql
volume=bars.volume + excluded.volume
```

This **adds** the incoming volume to the existing row. For a finalized bar upsert (bar already closed, replay or restart pushes the same bar again), the correct behavior is to **overwrite** with the final volume, not accumulate it.

The comment on line 23 even says "Exact overwrite" but the SQL does the opposite.

The test in `test_db.py:52` (`assert row["volume"] == 15.0`) validates the *additive* behavior, which means the test itself enshrines the bug.

**Context from `bot/bars.py`:**
`BarBuilder.process_trade()` accumulates volume within a bar via `current.volume += size`. When the bar is finalized and upserted, that bar object already contains the total accumulated volume. If the same finalized bar is upserted again (e.g., after restart), the additive SQL doubles the volume.

**Fix:**

1. Change `bot/journal.py:21` from:
   ```sql
   volume=bars.volume + excluded.volume
   ```
   to:
   ```sql
   volume=excluded.volume
   ```
2. Update `test_db.py` test `test_bar_upsert_duplicates`: the expected volume after two upserts of the same bar should be `5.0` (the second upsert's volume), not `15.0`.
3. Verify this doesn't break any other tests.

**Single commit** after this phase.

---

## Phase 5: Run and stabilize the full suite

**Problem:**
The test suite has never been proven to collect and pass in a single `pytest` invocation from the repo root. Phases 1-4 change imports, fixtures, and behavior -- the suite must be verified end to end.

**Steps:**

1. Run `pytest bot/tests/ -v` from the repo root. Fix any remaining import errors, fixture resolution issues, or assertion failures.
2. Common issues to watch for:
   - `test_execution.py` uses `unittest.TestCase` -- pytest collects these, but fixture injection works differently. If the `test_db` conftest fixture doesn't integrate cleanly with unittest, convert the class to pytest-style functions.
   - `test_reconcile.py:164` references `process_consumer_tick` with a mock adapter -- verify the fixture provides a clean DB before each test so adapter metadata assertions don't collide.
   - `test_state_machine.py` monkeypatches `state_machine.is_bullish_regime` and `Indicators` methods -- verify these patches compose correctly with the new `test_db` fixture.
   - `test_db_migration.py` constructs its own `Database(db_path=...)` instance -- this is fine and should not be affected by conftest changes.
3. Run with `pytest -x -v` (stop on first failure) to iterate quickly.
4. Once green: run `pytest --tb=short` one final time for a clean pass.
5. **Do not commit any test output files.** The `.gitignore` updates from Phase 1 should prevent this, but verify.
6. Only commit the code changes that were needed to make the suite pass.

**Single commit** if any additional fixes were needed beyond Phases 1-4.

---

## Phase 6: Re-verify reconcile behavior

**Problem:**
The reconciliation loop in `bot/execution.py:158-227` is the most critical code path. After all cleanup, its behavior must be re-verified against the four key scenarios.

**Test coverage check -- what exists today (`test_reconcile.py`):**

| Scenario | Test | Status |
|----------|------|--------|
| Pending order submission (adapter boundary) | `test_exchange_metadata_persists_on_submit` | exists |
| Fill reconciliation | `test_fill_application_updates_order_and_position` | exists |
| FAILED_EXCHANGE path | **none** | **missing** |
| Timeout path | `test_stale_pending_order_timeout` | exists |

**Required additions:**

1. **FAILED_EXCHANGE test:** Create a `MockAdapter` that returns a CANCELLED/FAILED status from `sync_get_order()`, then verify:
   - Order status transitions to FAILED
   - Signal status transitions to FAILED_EXCHANGE
   - No position is opened
2. **Fill reconciliation via adapter:** The existing fill test (`test_fill_application_updates_order_and_position`) calls `handle_fill()` directly. Add a test that exercises fills *through the reconcile loop* via a MockAdapter that returns fills from `sync_get_fills()`, verifying:
   - `reconcile_pending_orders()` fetches fills and calls `handle_fill`
   - Order progresses from PENDING to FILLED
   - Position is created with correct avg_entry and stop_price

**Observations to document (not fix yet) for future work:**

- `reconcile_pending_orders` line 180: if `adapter.submit_order_intent()` throws, the order stays PENDING with no `exchange_order_id` and will be retried forever. Should consider a max-retry or backoff mechanism.
- Line 201: `if order.status != "PENDING"` checks the *in-memory* Order object, but `handle_fill()` only modifies it in-place -- this works but is fragile if `Order` becomes immutable or DB-only.
- Lines 220-226: timeout uses `insert_order` (upsert) rather than `update_order_status`, which is an inconsistency with the FAILED path (line 156 uses `update_order_status`). Both work due to the `ON CONFLICT` clause but the intent is different.

**Single commit** for the new tests.

---

## Execution order and commit sequence

| # | Phase | Commit message pattern |
|---|-------|----------------------|
| 1 | Remove artifacts, update .gitignore | `chore: remove committed test artifacts and update .gitignore` |
| 2 | Standardize imports | `refactor(tests): standardize on package-style imports, remove sys.path hacks` |
| 3 | Centralize DB fixtures | `refactor(tests): centralize DB fixtures in conftest.py using tmp_path` |
| 4 | Fix bar volume upsert | `fix(journal): bar upsert must overwrite volume, not accumulate` |
| 5 | Stabilize full suite | `test: fix remaining failures for clean full-suite run` |
| 6 | Add missing reconcile tests | `test(reconcile): add FAILED_EXCHANGE and adapter-driven fill tests` |

Each phase produces exactly one commit. If a phase requires no changes (e.g., Phase 5 passes immediately after Phases 1-4), skip the commit.

---

## Risks and edge cases

- **Adding `bot/__init__.py` may change Python's import resolution** for the source modules themselves. If `journal.py`'s `from db import db` breaks, it must be changed to `from .db import db` (relative import). Test this immediately after Phase 2.
- **`test_execution.py` uses `unittest.TestCase`** which doesn't natively support pytest fixtures via function arguments. Options: (a) convert to pytest functions, (b) use `@pytest.fixture(autouse=True)` at module level, or (c) keep unittest and call setup logic explicitly. Option (a) is cleanest.
- **`test_reconcile.py` line 17** has a `teardown_module` that does nothing (`pass`). This was likely meant to clean up `test_reconcile.db` but was left incomplete. Phase 3 eliminates this entirely.
- **The global `db = Database()` at `bot/db.py:144`** runs `_init_db()` at import time, which creates `journal.db` in whatever the current working directory is. After adding `__init__.py`, importing `bot.db` from the repo root will create `journal.db` at the repo root. This is existing behavior but worth noting -- the `.gitignore` already covers `*.db`.
