import sqlite3
from contextlib import contextmanager
from pathlib import Path
from app import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, source BLOB NOT NULL, book BLOB NOT NULL,
  result TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  run_id TEXT NOT NULL REFERENCES runs(id), version INTEGER NOT NULL,
  decision TEXT NOT NULL, note TEXT NOT NULL, changed TEXT NOT NULL,
  PRIMARY KEY(run_id,version)
);
CREATE TABLE IF NOT EXISTS transfer_lists (
  id TEXT PRIMARY KEY, week TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES runs(id), capacity INTEGER NOT NULL,
  result TEXT NOT NULL, status TEXT NOT NULL,
  created TEXT NOT NULL, changed TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS transfer_live_week
  ON transfer_lists(week) WHERE status != '已失效';
CREATE TABLE IF NOT EXISTS transfer_reviews (
  list_id TEXT NOT NULL REFERENCES transfer_lists(id), version INTEGER NOT NULL,
  decision TEXT NOT NULL, note TEXT NOT NULL, changed TEXT NOT NULL,
  PRIMARY KEY(list_id,version)
);
"""


@contextmanager
def connection():
    path = Path(settings.DB)
    path.parent.mkdir(parents=True,exist_ok=True)
    db = sqlite3.connect(path,timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA foreign_keys = ON')
        db.executescript(SCHEMA)
        with db:
            yield db
    finally:
        db.close()
