# CB-RTB Repo Stabilization Instructions for Codex

## Mission
You are stabilizing the repository **without adding new features**.

Your job is to stop the repo from thrashing, remove inconsistent testing behavior, and make the current execution/reconciliation architecture trustworthy.

This document is intentionally strict because previous attempts drifted, filled gaps incorrectly, regressed working logic, or mixed cleanup with new feature work.

---

## Source of Truth
Base your work on the uploaded stabilization plan and the repo state discussed around commit `6894653`.

Primary planning reference: the uploaded repo stabilization plan. fileciteturn57file0

### Required baseline
- Treat commit **`6894653`** as the current recovery baseline.
- Do **not** continue from `fda0476` logic patterns.
- Do **not** redesign the architecture.
- Do **not** add new product features.

If the current branch has drifted beyond `6894653`, your job is still **cleanup and stabilization only**.

---

## Non-Negotiable Scope
You are allowed to do only the following kinds of work:

1. Remove tracked debug / artifact junk.
2. Standardize imports and package structure so tests run from repo root.
3. Centralize and isolate DB test fixtures.
4. Restore correct finalized-bar upsert semantics.
5. Make the full test suite collect and pass from repo root.
6. Add missing reconciliation tests that validate existing behavior.

You are **not allowed** to:

- add strategy features
- add new trading logic
- change risk model semantics
- invent new order lifecycle states unless explicitly required for an existing broken path
- rename major classes or modules unless absolutely necessary to restore consistency
- rewrite the execution layer from scratch
- change architecture from monolith to services
- add Redis, Celery, external migration tools, ORMs, or trading frameworks
- commit test output files, debug files, logs, notebooks, or scratch scripts
- “improve” things outside the phases below

If you encounter a tempting improvement outside scope, do **not** implement it. Mention it in the final notes only.

---

## Hard Behavioral Rules
When executing this plan:

- Do **one phase at a time**.
- Do **not** combine phases into one giant patch.
- After each phase, verify the acceptance criteria for that phase before moving on.
- After each phase, produce exactly:
  - files changed
  - summary of what changed
  - exact verification run
  - whether phase acceptance passed
- If a phase fails, fix that phase before moving on.
- Do **not** silently reinterpret a phase.
- Do **not** claim the suite is green unless it is green from repo root in a clean run.

---

## Required Branching / Safety Step
Before Phase 1:

1. Confirm current HEAD commit.
2. Record whether `6894653` is reachable in history.
3. Create or use a dedicated stabilization branch.
4. Do not squash unrelated changes into this effort.

Output required before Phase 1:

```text
Baseline commit in use: <sha>
Current branch: <branch>
Stabilization branch: <branch>
Working tree clean: yes/no
```

---

# Phase 1 — Remove committed artifacts

## Goal
Remove tracked junk files that should never have been committed.

## Files to delete if present
- `pytest_all.txt`
- `pytest_all2.txt`
- `pytest_output.txt`
- `pytest_output2.txt`
- `pytest_err.txt`
- `pytest_debug.txt`
- `pytest_two.txt`
- `pytest.log`
- `test_bug.py`
- `bot/tests/debug_sm.py`
- `bot/tests/debug_sm2.py`

If additional tracked files are clearly generated test/debug artifacts of the same kind, remove them too.

## Required `.gitignore` additions
Add these if missing:

```gitignore
pytest*.txt
pytest*.log
*.log
.pytest_cache/
```

Do **not** add broad ignores that hide source code accidentally.

## Forbidden moves in Phase 1
- no code logic changes
- no refactors
- no test rewriting

## Acceptance criteria
- junk/debug artifact files are removed from git
- `.gitignore` updated appropriately
- no source logic changed

## Required commit message
```text
chore: remove committed test artifacts and update .gitignore
```

---

# Phase 2 — Standardize imports and package layout

## Goal
Make tests import consistently from repo root without path hacks.

## Decision
Standardize on **package-style imports**:

```python
from bot.x import Y
```

## Required changes
1. Add empty files if missing:
   - `bot/__init__.py`
   - `bot/tests/__init__.py`

2. Add one repo-root pytest config. Prefer `pyproject.toml` if absent, otherwise use existing pytest config location.

Required pytest config behavior:
- running `pytest` from repo root should discover `bot/tests`
- no `PYTHONPATH` hacks required

Example acceptable config:

```toml
[tool.pytest.ini_options]
testpaths = ["bot/tests"]
```

3. Remove `sys.path.insert(...)` hacks from tests.

4. Convert all test imports to package style, including but not limited to:
- `from models import ...` -> `from bot.models import ...`
- `from db import db` -> `from bot.db import db`
- `from journal import Journal` -> `from bot.journal import Journal`
- `from execution import ExecutionService` -> `from bot.execution import ExecutionService`
- `from risk import RiskManager` -> `from bot.risk import RiskManager`
- `import db as global_db_module` -> `import bot.db as global_db_module`
- `import journal` -> `import bot.journal as journal`
- `import strategy` -> `import bot.strategy as strategy`
- `import state_machine` -> `import bot.state_machine as state_machine`

## Important rule
If adding `bot/__init__.py` breaks source-internal bare imports inside `bot/`, fix them cleanly.
Preferred order:
1. relative imports like `from .db import db`
2. absolute package imports like `from bot.db import db`

Do **not** leave the repo in a mixed half-working state.

## Forbidden moves in Phase 2
- no behavior changes to strategy or execution logic unless required to fix import breakage
- no DB fixture redesign yet

## Acceptance criteria
- `pytest --collect-only` from repo root succeeds
- no `sys.path` hacks remain in tests
- package imports are consistent across tests

## Required commit message
```text
refactor(tests): standardize on package-style imports and remove path hacks
```

---

# Phase 3 — Centralize DB test fixtures

## Goal
Stop DB test contamination, file locking, singleton leakage, and inconsistent setup patterns.

## Required approach
Create `bot/tests/conftest.py` with shared fixtures using `tmp_path` and monkeypatch.

## Core fixture requirement
You must provide a fresh DB per test function for tests that need isolation.

Use the global `bot.db.db` object but patch its `db_path` to a temp file, initialize it, and allow pytest temp cleanup to handle file deletion.

## Required outcomes
- eliminate ad hoc `os.remove()` teardown blocks where possible
- eliminate fixed shared DB filenames in tests
- eliminate `setup_module` / `teardown_module` DB orchestration where unnecessary
- eliminate repeated hand-written temp DB fixture logic across files

## Per-file expectations
- `test_db.py`: move to shared fixture
- `test_state_machine.py`: move to shared fixture
- `test_reconcile.py`: stop using module-level fixed DB file setup
- `test_execution.py`: if unittest style complicates fixture usage, convert it to pytest-style tests
- `test_db_migration.py`: leave it alone unless required, because it already uses a proper temp path pattern

## Strong preference
Convert `test_execution.py` to pytest-style functions if that is the cleanest route. Do not preserve unittest style out of habit if it keeps the fixture story messy.

## Forbidden moves in Phase 3
- no strategy/execution feature changes
- no architecture changes
- no hidden state carried across tests

## Acceptance criteria
- tests no longer depend on fixed DB filenames
- teardown file-lock issues are gone
- DB setup logic is centralized instead of duplicated

## Required commit message
```text
refactor(tests): centralize DB fixtures in conftest.py using tmp_path
```

---

# Phase 4 — Restore correct finalized-bar upsert semantics

## Goal
Fix the finalized-bar volume regression.

## Correct rule
For duplicate upserts of the **same finalized bar**, use **last-write-wins** for volume.
Do **not** accumulate volume during finalized-bar replay/upsert.

## Required code change
In `bot/journal.py`, change finalized bar upsert behavior so volume becomes:

```sql
volume = excluded.volume
```

and **not**:

```sql
volume = bars.volume + excluded.volume
```

## Required test update
Update the relevant DB test so it verifies overwrite semantics.
The exact asserted value should reflect the second write in that test case, because the behavior is overwrite, not accumulation.

## Forbidden moves in Phase 4
- do not change live intra-bar accumulation logic in the bar builder
- do not redesign persistence beyond this bug

## Acceptance criteria
- finalized-bar replay does not double-count volume
- test reflects overwrite semantics clearly

## Required commit message
```text
fix(journal): finalized bar upsert must overwrite volume, not accumulate
```

---

# Phase 5 — Make the full suite pass cleanly from repo root

## Goal
Prove the repo is stable in one clean run.

## Required run conditions
Run from **repo root**.
Do not use:
- manual `PYTHONPATH`
- cwd tricks
- one-off shell hacks not encoded in repo config

## Required commands
Use these, in order:

```bash
pytest --collect-only
pytest -x -v
pytest --tb=short
```

If failures occur, fix them.

## Critical rule
Do **not** commit output logs from test runs.
Do **not** use committed text dumps as proof.
The proof is the code and the clean run result you report.

## Acceptance criteria
- full suite collects from repo root
- full suite passes from repo root
- no debug artifacts/log files are added to git

## Required commit message
Use only if code changes were needed beyond phases 1-4:

```text
test: fix remaining failures for clean full-suite run
```

If no code changes were needed here, skip the commit and say so explicitly.

---

# Phase 6 — Add missing reconciliation tests only

## Goal
Validate the current reconciliation path more completely, without adding new product features.

## Existing scenarios already expected
- pending order submission / metadata persistence
- timeout path
- some fill behavior

## Required additional tests
### A. FAILED_EXCHANGE path
Create a mock adapter that yields a remote order state like `CANCELLED`, `FAILED`, or `EXPIRED`.
Verify:
- local order status becomes failed/expired appropriately
- linked signal status becomes `FAILED_EXCHANGE`
- no position is opened

### B. Adapter-driven fill reconciliation
Do **not** only call `handle_fill()` directly.
Add a test where reconciliation pulls fills via adapter methods and causes local state transitions through the real reconcile path.
Verify:
- pending order becomes filled
- signal status updates correctly
- position is created correctly
- stop data is persisted correctly

### C. Duplicate fill protection
Add a test where the adapter returns the same fill twice (same trade/execution id).
Verify that execution credit is not double-applied.

## Forbidden moves in Phase 6
- no new exchange features
- no new order types
- no new execution architecture

## Acceptance criteria
- missing reconcile coverage is added
- tests validate current intended behavior instead of bypassing it

## Required commit message
```text
test(reconcile): add FAILED_EXCHANGE and adapter-driven reconciliation coverage
```

---

# Final Acceptance Gate
You are done only if all of the following are true:

1. Repo root `pytest --collect-only` succeeds.
2. Repo root full suite passes.
3. No tracked debug artifacts remain.
4. No `sys.path` hacks remain in tests.
5. DB fixtures are centralized and isolated.
6. Finalized-bar upsert uses overwrite semantics for finalized bar replay.
7. Reconciliation has coverage for:
   - pending submission
   - timeout
   - exchange failure
   - adapter-driven fills
   - duplicate fill defense
8. No new product features were added.

If any of the above is false, do **not** claim stabilization is complete.

---

# Required Output Format After Each Phase
After each phase, output exactly this structure:

```text
PHASE: <number and name>
STATUS: PASS / FAIL

Files changed:
- <file>
- <file>

What changed:
- <bullet>
- <bullet>

Verification run:
- <exact command>
- <result>

Notes:
- <only if needed>
```

Do not pad with motivational language.
Do not speculate.
Do not say “should be fixed.”
State what was changed and what passed.

---

# Required Final Output Format
When all phases are complete, output exactly:

```text
STABILIZATION COMPLETE

Baseline used:
<sha>

Commits created:
1. <sha> <message>
2. <sha> <message>
...

Validation:
- pytest --collect-only: PASS/FAIL
- pytest -x -v: PASS/FAIL
- pytest --tb=short: PASS/FAIL

Remaining known issues outside scope:
- <issue>
- <issue>
```

If stabilization is **not** complete, output instead:

```text
STABILIZATION INCOMPLETE

Blocked in phase:
<phase>

Reason:
<precise reason>

Last verification output:
<concise factual summary>

Next required action:
<single next action>
```

---

# Summary
This is a **cleanup and stabilization pass only**.
Not a redesign pass.
Not a feature pass.
Not a cleverness pass.

If you are uncertain whether a change is allowed, do not make it unless it is required to satisfy one of the explicit phases above.
