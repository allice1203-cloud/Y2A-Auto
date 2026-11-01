"""Data-only path migration helpers; callers must stop all writers first."""
import json
import re
import sqlite3
from pathlib import Path


def rewrite_paths(value, mappings):
    if isinstance(value, dict):
        return {key: rewrite_paths(item, mappings) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_paths(item, mappings) for item in value]
    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
            updated = rewrite_paths(parsed, mappings)
            return value if parsed == updated else json.dumps(updated, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    for source, destination in sorted(mappings, key=lambda pair: len(pair[0]), reverse=True):
        value = re.sub(re.escape(source.rstrip('/')) + r'(?=$|/|[\s"\'])',
                       lambda _: destination.rstrip('/'), value)
    return value


def rewrite_database(database: Path, mappings) -> dict:
    changes = {}
    quote = lambda name: '"' + name.replace('"', '""') + '"'
    with sqlite3.connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for (table,) in tables:
            columns = db.execute(f"PRAGMA table_info({quote(table)})").fetchall()
            for column in columns:
                name = column[1]
                if str(column[2]).upper() not in {"TEXT", "VARCHAR", "JSON"}:
                    continue
                rows = db.execute(f"SELECT rowid,{quote(name)} FROM {quote(table)} WHERE typeof({quote(name)})='text'").fetchall()
                updates = [(rewrite_paths(value, mappings), rowid) for rowid, value in rows
                           if rewrite_paths(value, mappings) != value]
                if updates:
                    db.executemany(f"UPDATE {quote(table)} SET {quote(name)}=? WHERE rowid=?", updates)
                    changes[f"{table}.{name}"] = len(updates)
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed; changes rolled back")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Database foreign keys failed; changes rolled back")
    return {"database": database.name, "updated_columns": changes}


def backup_database(source: Path, destination: Path) -> dict:
    if destination.exists():
        raise ValueError("Backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original, sqlite3.connect(destination) as backup:
        original.backup(backup)
        if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Backup integrity check failed")
    destination.chmod(0o600)
    return {"name": source.name, "bytes": destination.stat().st_size, "quick_check": "ok"}
