"""
research_pipeline — read-only / shadow-only Coinbase microstructure research spine.

SAFETY BOUNDARY (enforced by tests/test_boundary.py):
  * This package imports NO module from `bot/` and never instantiates `bot.db.db`.
    Importing `bot.db` (or anything that imports it) creates/initialises a journal
    database in the CWD as a side effect — see docs/research_pipeline/ARCHITECTURE_REVIEW.md
    finding F-C1. This package must never trigger that.
  * It uses its OWN SQLite database (never journal.db / live_journal.db / paper_journal.db).
  * It contains NO brokerage/order adapter and no path to rest.create_order.

See docs/research_pipeline/IMPLEMENTATION_CONTRACT.md for the frozen data contract.
"""

__version__ = "0.2.0"
