"""Durable storage for uploaded dataset files."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[1] / ".halite-data" / "datasets.sqlite3"
DATABASE_PATH = Path(os.getenv("HALITE_DATA_PATH", str(DEFAULT_PATH)))
_schema_lock = threading.Lock()
_schema_ready = False


def _connect() -> sqlite3.Connection:
    global _schema_ready
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    if _schema_ready:
        return connection
    with _schema_lock:
        if _schema_ready:
            return connection
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS datasets (
                dataset_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT 'legacy',
                profile_json TEXT NOT NULL
            )
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(datasets)")}
        if "user_id" not in columns:
            connection.execute("ALTER TABLE datasets ADD COLUMN user_id TEXT NOT NULL DEFAULT 'legacy'")
        if not os.getenv("CLERK_JWT_KEY"):
            connection.execute("UPDATE datasets SET user_id = 'local' WHERE user_id = 'legacy'")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                payload BLOB NOT NULL,
                uploaded_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        file_columns = {row[1] for row in connection.execute("PRAGMA table_info(dataset_files)")}
        if "uploaded_at" not in file_columns:
            connection.execute("ALTER TABLE dataset_files ADD COLUMN uploaded_at REAL NOT NULL DEFAULT 0")
            connection.execute("UPDATE dataset_files SET uploaded_at = ? WHERE uploaded_at = 0", (time.time() * 1000,))
        connection.execute("PRAGMA foreign_keys = ON")
        connection.commit()
        _schema_ready = True
    return connection


def create_dataset(user_id: str, dataset_id: str, profile: dict, files: list[tuple[str, bytes]]) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO datasets (dataset_id, user_id, profile_json) VALUES (?, ?, ?)",
            (dataset_id, user_id, json.dumps(profile)),
        )
        connection.executemany(
            "INSERT INTO dataset_files (dataset_id, filename, payload, uploaded_at) VALUES (?, ?, ?, ?)",
            [(dataset_id, filename, payload, time.time() * 1000) for filename, payload in files],
        )
        connection.commit()


def replace_files(user_id: str, dataset_id: str, files: list[tuple[str, bytes]]) -> None:
    with _connect() as connection:
        _require_dataset(connection, user_id, dataset_id)
        filenames = list(dict.fromkeys(filename for filename, _ in files))
        connection.executemany(
            "DELETE FROM dataset_files WHERE dataset_id = ? AND filename = ?",
            [(dataset_id, filename) for filename in filenames],
        )
        connection.executemany(
            "INSERT INTO dataset_files (dataset_id, filename, payload, uploaded_at) VALUES (?, ?, ?, ?)",
            [(dataset_id, filename, payload, time.time() * 1000) for filename, payload in files],
        )
        connection.commit()


def delete_file(user_id: str, dataset_id: str, filename: str) -> bool:
    with _connect() as connection:
        _require_dataset(connection, user_id, dataset_id)
        row = connection.execute(
            "SELECT id FROM dataset_files WHERE dataset_id = ? AND filename = ? ORDER BY id LIMIT 1",
            (dataset_id, filename),
        ).fetchone()
        if row is None:
            return False
        connection.execute("DELETE FROM dataset_files WHERE id = ?", (row[0],))
        connection.commit()
        return True


def delete_files(user_id: str, dataset_id: str, filenames: list[str]) -> int:
    with _connect() as connection:
        _require_dataset(connection, user_id, dataset_id)
        cursor = connection.executemany(
            "DELETE FROM dataset_files WHERE dataset_id = ? AND filename = ?",
            [(dataset_id, filename) for filename in dict.fromkeys(filenames)],
        )
        connection.commit()
        return cursor.rowcount


def _require_dataset(connection: sqlite3.Connection, user_id: str, dataset_id: str) -> None:
    if connection.execute(
        "SELECT 1 FROM datasets WHERE dataset_id = ? AND user_id = ?", (dataset_id, user_id)
    ).fetchone() is None:
        raise KeyError(dataset_id)


def load_datasets() -> list[tuple[str, str, dict, list[tuple[str, bytes]]]]:
    with _connect() as connection:
        datasets = connection.execute(
            "SELECT dataset_id, user_id, profile_json FROM datasets ORDER BY rowid"
        ).fetchall()
        result = []
        for dataset_id, user_id, profile_json in datasets:
            files = connection.execute(
                "SELECT filename, payload FROM dataset_files WHERE dataset_id = ? ORDER BY id",
                (dataset_id,),
            ).fetchall()
            result.append((dataset_id, user_id, json.loads(profile_json), files))
        return result


def load_files(user_id: str, dataset_id: str) -> list[tuple[str, bytes]]:
    with _connect() as connection:
        _require_dataset(connection, user_id, dataset_id)
        return connection.execute(
            "SELECT filename, payload FROM dataset_files WHERE dataset_id = ? ORDER BY id",
            (dataset_id,),
        ).fetchall()


def load_dataset(user_id: str, dataset_id: str) -> tuple[dict, list[tuple[str, bytes]]] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT profile_json FROM datasets WHERE dataset_id = ? AND user_id = ?",
            (dataset_id, user_id),
        ).fetchone()
        if row is None:
            return None
        files = connection.execute(
            "SELECT filename, payload FROM dataset_files WHERE dataset_id = ? ORDER BY id",
            (dataset_id,),
        ).fetchall()
        return json.loads(row[0]), files


def load_user_files(user_id: str) -> tuple[dict, list[tuple[str, bytes]]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT d.profile_json, f.filename, f.payload
            FROM datasets d
            LEFT JOIN dataset_files f ON f.dataset_id = d.dataset_id
            WHERE d.user_id = ?
            ORDER BY d.rowid, f.id
            """,
            (user_id,),
        ).fetchall()
        if not rows:
            return {}, []
        profile = {}
        files_by_name: dict[str, bytes] = {}
        for profile_json, filename, payload in rows:
            profile = json.loads(profile_json)
            if filename is not None:
                files_by_name[filename] = payload
        return profile, list(files_by_name.items())


def load_user_file_metadata(user_id: str) -> tuple[dict, list[tuple[str, int, float]]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT d.profile_json, f.filename, length(f.payload), f.uploaded_at
            FROM datasets d
            LEFT JOIN dataset_files f ON f.dataset_id = d.dataset_id
            WHERE d.user_id = ?
            ORDER BY d.rowid, f.id
            """,
            (user_id,),
        ).fetchall()
        if not rows:
            return {}, []
        profile = {}
        files_by_name: dict[str, tuple[int, float]] = {}
        for profile_json, filename, size, uploaded_at in rows:
            profile = json.loads(profile_json)
            if filename is not None:
                files_by_name[filename] = (int(size or 0), float(uploaded_at or 0))
        return profile, [(filename, size, uploaded_at) for filename, (size, uploaded_at) in files_by_name.items()]


def upsert_user_files(user_id: str, profile: dict, files: list[tuple[str, bytes]]) -> None:
    with _connect() as connection:
        datasets = connection.execute(
            "SELECT dataset_id, profile_json FROM datasets WHERE user_id = ? ORDER BY rowid",
            (user_id,),
        ).fetchall()
        if len(datasets) == 1:
            dataset_id, current_profile = datasets[0]
            connection.execute(
                "UPDATE datasets SET profile_json = ? WHERE dataset_id = ?",
                (json.dumps(profile or json.loads(current_profile)), dataset_id),
            )
            unique_files = {filename: payload for filename, payload in files}
            connection.executemany(
                "DELETE FROM dataset_files WHERE dataset_id = ? AND filename = ?",
                [(dataset_id, filename) for filename in unique_files],
            )
            connection.executemany(
                "INSERT INTO dataset_files (dataset_id, filename, payload, uploaded_at) VALUES (?, ?, ?, ?)",
                [(dataset_id, filename, payload, time.time() * 1000) for filename, payload in unique_files.items()],
            )
            connection.commit()
            return
        existing = connection.execute(
            """
            SELECT f.filename, f.payload
            FROM dataset_files f
            JOIN datasets d ON d.dataset_id = f.dataset_id
            WHERE d.user_id = ?
            ORDER BY f.id
            """,
            (user_id,),
        ).fetchall()
        merged: dict[str, bytes] = {filename: payload for filename, payload in existing}
        merged.update(files)
        current_profile = connection.execute(
            "SELECT profile_json FROM datasets WHERE user_id = ? ORDER BY rowid DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        stored_profile = profile or (json.loads(current_profile[0]) if current_profile else {})
        connection.execute(
            "DELETE FROM dataset_files WHERE dataset_id IN (SELECT dataset_id FROM datasets WHERE user_id = ?)",
            (user_id,),
        )
        connection.execute("DELETE FROM datasets WHERE user_id = ?", (user_id,))
        dataset_id = uuid.uuid4().hex[:12]
        connection.execute(
            "INSERT INTO datasets (dataset_id, user_id, profile_json) VALUES (?, ?, ?)",
            (dataset_id, user_id, json.dumps(stored_profile)),
        )
        connection.executemany(
            "INSERT INTO dataset_files (dataset_id, filename, payload, uploaded_at) VALUES (?, ?, ?, ?)",
            [(dataset_id, filename, payload, time.time() * 1000) for filename, payload in merged.items()],
        )
        connection.commit()


def delete_user_files(user_id: str, filenames: list[str]) -> int:
    with _connect() as connection:
        cursor = connection.executemany(
            "DELETE FROM dataset_files WHERE dataset_id IN (SELECT dataset_id FROM datasets WHERE user_id = ?) AND filename = ?",
            [(user_id, filename) for filename in dict.fromkeys(filenames)],
        )
        connection.commit()
        return cursor.rowcount
