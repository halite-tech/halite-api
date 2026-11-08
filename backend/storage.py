"""Durable storage for uploaded dataset files."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[1] / ".halite-data" / "datasets.sqlite3"
DATABASE_PATH = Path(os.getenv("HALITE_DATA_PATH", str(DEFAULT_PATH)))


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS datasets (
            dataset_id TEXT PRIMARY KEY,
            profile_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS dataset_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            payload BLOB NOT NULL
        )
        """
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        DELETE FROM dataset_files
        WHERE id NOT IN (
            SELECT MAX(id)
            FROM dataset_files
            GROUP BY dataset_id, filename
        )
        """
    )
    connection.commit()
    return connection


def create_dataset(dataset_id: str, profile: dict, files: list[tuple[str, bytes]]) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO datasets (dataset_id, profile_json) VALUES (?, ?)",
            (dataset_id, json.dumps(profile)),
        )
        connection.executemany(
            "INSERT INTO dataset_files (dataset_id, filename, payload) VALUES (?, ?, ?)",
            [(dataset_id, filename, payload) for filename, payload in files],
        )
        connection.commit()


def replace_files(dataset_id: str, files: list[tuple[str, bytes]]) -> None:
    with _connect() as connection:
        filenames = list(dict.fromkeys(filename for filename, _ in files))
        connection.executemany(
            "DELETE FROM dataset_files WHERE dataset_id = ? AND filename = ?",
            [(dataset_id, filename) for filename in filenames],
        )
        connection.executemany(
            "INSERT INTO dataset_files (dataset_id, filename, payload) VALUES (?, ?, ?)",
            [(dataset_id, filename, payload) for filename, payload in files],
        )
        connection.commit()


def delete_file(dataset_id: str, filename: str) -> bool:
    with _connect() as connection:
        row = connection.execute(
            "SELECT id FROM dataset_files WHERE dataset_id = ? AND filename = ? ORDER BY id LIMIT 1",
            (dataset_id, filename),
        ).fetchone()
        if row is None:
            return False
        connection.execute("DELETE FROM dataset_files WHERE id = ?", (row[0],))
        connection.commit()
        return True


def delete_files(dataset_id: str, filenames: list[str]) -> int:
    with _connect() as connection:
        cursor = connection.executemany(
            "DELETE FROM dataset_files WHERE dataset_id = ? AND filename = ?",
            [(dataset_id, filename) for filename in dict.fromkeys(filenames)],
        )
        connection.commit()
        return cursor.rowcount


def load_datasets() -> list[tuple[str, dict, list[tuple[str, bytes]]]]:
    with _connect() as connection:
        datasets = connection.execute(
            "SELECT dataset_id, profile_json FROM datasets ORDER BY rowid"
        ).fetchall()
        result = []
        for dataset_id, profile_json in datasets:
            files = connection.execute(
                "SELECT filename, payload FROM dataset_files WHERE dataset_id = ? ORDER BY id",
                (dataset_id,),
            ).fetchall()
            result.append((dataset_id, json.loads(profile_json), files))
        return result


def load_files(dataset_id: str) -> list[tuple[str, bytes]]:
    with _connect() as connection:
        return connection.execute(
            "SELECT filename, payload FROM dataset_files WHERE dataset_id = ? ORDER BY id",
            (dataset_id,),
        ).fetchall()
