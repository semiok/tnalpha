#!/usr/bin/env python3
"""Import a sanitized tnalpha seed data package into a local SQLite database.

This is intentionally narrow: it imports knowledge/topic demo content and
uploaded assets, but does not touch LLM settings or API keys.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEED = ROOT / "seed_data" / "tnalpha_demo_seed.json"
CONTENT_TABLES = [
    "campaignpoolref",
    "topic",
    "campaigndoc",
    "branddoc",
    "campaign",
    "pooltopic",
    "brand",
    "appsetting",
]
INSERT_ORDER = [
    "appsetting",
    "brand",
    "pooltopic",
    "campaign",
    "branddoc",
    "campaigndoc",
    "campaignpoolref",
    "topic",
]


def _db_from_env() -> Path:
    url = os.environ.get("TNALPHA_DATABASE_URL", "").strip()
    if url.startswith("sqlite:///"):
        parsed = urlparse(url)
        if parsed.netloc:
            return Path(f"/{parsed.netloc}{parsed.path}")
        return Path(parsed.path)
    if (ROOT / "data" / "app.db").exists():
        return ROOT / "data" / "app.db"
    return ROOT / "tnalpha.db"


def _load_seed(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _backup(db_path: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.bak-seed-import-{ts}")
    shutil.copy2(db_path, backup)
    return backup


def _copy_asset(seed_dir: Path, data_dir: Path, relpath: str) -> Path:
    src = seed_dir / "assets" / relpath
    dest = data_dir / relpath
    if not src.exists():
        raise FileNotFoundError(f"missing seed asset: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest.resolve()


def _insert_rows(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ", ".join(["?"] * len(cols))
    quoted_cols = ", ".join(cols)
    sql = f"insert into {table} ({quoted_cols}) values ({placeholders})"
    conn.executemany(sql, [[row.get(col) for col in cols] for row in rows])


def import_seed(seed_path: Path, db_path: Path, data_dir: Path, yes: bool) -> None:
    seed_path = seed_path.resolve()
    db_path = db_path.resolve()
    data_dir = data_dir.resolve()

    if not seed_path.exists():
        raise FileNotFoundError(seed_path)
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    seed = _load_seed(seed_path)
    tables: dict[str, list[dict]] = seed["tables"]
    seed_dir = seed_path.parent

    for table in ("llmsetting",):
        if table in tables:
            raise ValueError("seed package must not contain llmsetting/API key rows")

    print(f"Seed: {seed_path}")
    print(f"DB: {db_path}")
    print(f"DATA_DIR: {data_dir}")
    print("Will replace knowledge/topic demo tables, but will not touch llmsetting.")
    if not yes:
        answer = input("Continue? Type 'yes': ").strip()
        if answer != "yes":
            print("Canceled.")
            return

    backup = _backup(db_path)
    print(f"Backed up DB: {backup}")

    # Rewrite uploaded-file paths for the target machine and copy assets.
    for table in ("branddoc", "campaigndoc", "pooltopic"):
        for row in tables.get(table, []):
            relpath = row.pop("_asset_relpath", "")
            if relpath:
                row["file_path"] = str(_copy_asset(seed_dir, data_dir, relpath))

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("pragma foreign_keys = off")
        with conn:
            for table in CONTENT_TABLES:
                conn.execute(f"delete from {table}")
            for table in INSERT_ORDER:
                _insert_rows(conn, table, tables.get(table, []))
        conn.execute("pragma foreign_keys = on")
    finally:
        conn.close()

    print("Seed import complete.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--db", type=Path, default=_db_from_env())
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("TNALPHA_DATA_DIR", "data")))
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    args = parser.parse_args()

    import_seed(args.seed, args.db, args.data_dir, args.yes)


if __name__ == "__main__":
    main()
