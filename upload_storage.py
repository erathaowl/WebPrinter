"""Persistent upload metadata and expiry management."""

from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import Callable, Optional


DATA_DIR = Path(os.getenv("WEBPRINTER_DATA_DIR", str(Path.cwd() / "data")))
UPLOAD_DIR = DATA_DIR / "uploads"
DATABASE_PATH = DATA_DIR / "uploads.sqlite3"
UPLOAD_TTL_MINUTES = max(1, int(os.getenv("WEBPRINTER_UPLOAD_TTL_MINUTES", "60")))


@dataclass
class UploadedFileEntry:
    """A file staged for printing within one browser session."""

    id: str
    session_id: str
    filename: str
    mime_type: str
    temp_path: Path
    total_chunks: int
    next_chunk_index: int = 0
    uploaded_bytes: int = 0
    stored_path: Optional[Path] = None
    completed: bool = False
    updated_at: float = field(default_factory=time.time)
    client_ip: str = ""


class UploadRegistry:
    """SQLite-backed upload registry with synchronized file expiry."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        upload_dir: Optional[Path] = None,
        ttl_minutes: Optional[int] = None,
    ) -> None:
        self.db_path = Path(db_path or DATABASE_PATH)
        self.upload_dir = Path(upload_dir or UPLOAD_DIR)
        self.ttl_seconds = 60 * (ttl_minutes or UPLOAD_TTL_MINUTES)
        self._lock = RLock()
        self._busy_paths: set[Path] = set()
        self._uploads: dict[str, UploadedFileEntry] = {}
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as database:
            database.execute(
                """CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    temp_path TEXT NOT NULL,
                    total_chunks INTEGER NOT NULL,
                    next_chunk_index INTEGER NOT NULL,
                    uploaded_bytes INTEGER NOT NULL,
                    stored_path TEXT,
                    completed INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    client_ip TEXT NOT NULL DEFAULT ''
                )"""
            )
            columns = {row[1] for row in database.execute("PRAGMA table_info(uploads)")}
            if "client_ip" not in columns:
                database.execute("ALTER TABLE uploads ADD COLUMN client_ip TEXT NOT NULL DEFAULT ''")
            rows = database.execute("SELECT * FROM uploads").fetchall()
        for row in rows:
            entry = UploadedFileEntry(
                id=row[0],
                session_id=row[1],
                filename=row[2],
                mime_type=row[3],
                temp_path=Path(row[4]),
                total_chunks=row[5],
                next_chunk_index=row[6],
                uploaded_bytes=row[7],
                stored_path=Path(row[8]) if row[8] else None,
                completed=bool(row[9]),
                updated_at=row[10],
                client_ip=row[11],
            )
            self._uploads[entry.id] = entry

    def _save(self, entry: UploadedFileEntry) -> None:
        with sqlite3.connect(self.db_path) as database:
            database.execute(
                """INSERT OR REPLACE INTO uploads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.id,
                    entry.session_id,
                    entry.filename,
                    entry.mime_type,
                    str(entry.temp_path),
                    entry.total_chunks,
                    entry.next_chunk_index,
                    entry.uploaded_bytes,
                    str(entry.stored_path) if entry.stored_path else None,
                    int(entry.completed),
                    entry.updated_at,
                    entry.client_ip,
                ),
            )

    def _remove(self, ids: list[str]) -> None:
        with sqlite3.connect(self.db_path) as database:
            database.executemany("DELETE FROM uploads WHERE id = ?", [(id_,) for id_ in ids])

    def get(self, upload_id: str, session_id: Optional[str] = None) -> Optional[UploadedFileEntry]:
        """Return an unexpired upload, enforcing session ownership if provided."""
        with self._lock:
            entry = self._uploads.get(upload_id)
            if not entry or (session_id is not None and entry.session_id != session_id):
                return None
            if entry.updated_at <= time.time() - self.ttl_seconds:
                return None
            return replace(entry)

    def list_for_session(self, session_id: str) -> list[UploadedFileEntry]:
        """List only completed, unexpired files owned by this browser."""
        with self._lock:
            cutoff = time.time() - self.ttl_seconds
            return [
                replace(entry)
                for entry in sorted(self._uploads.values(), key=lambda item: item.updated_at)
                if entry.session_id == session_id
                and entry.completed
                and entry.stored_path is not None
                and entry.stored_path.is_file()
                and entry.updated_at > cutoff
            ]

    def create_or_replace(self, entry: UploadedFileEntry) -> None:
        with self._lock:
            current = self._uploads.get(entry.id)
            if current and current.session_id != entry.session_id:
                raise ValueError("Upload belongs to a different session.")
            self._save(entry)
            self._uploads[entry.id] = replace(entry)

    def update(self, upload_id: str, session_id: Optional[str] = None, **changes: object) -> Optional[UploadedFileEntry]:
        """Update a file and reset its timeout after receiving a chunk."""
        with self._lock:
            entry = self.get(upload_id, session_id)
            if not entry:
                return None
            for key, value in changes.items():
                setattr(entry, key, value)
            entry.updated_at = time.time()
            self._save(entry)
            self._uploads[upload_id] = entry
            return replace(entry)

    def pop(self, upload_id: str, session_id: Optional[str] = None) -> Optional[UploadedFileEntry]:
        with self._lock:
            entry = self.get(upload_id, session_id)
            if entry:
                self._remove([upload_id])
                del self._uploads[upload_id]
            return entry

    def pop_many(self, upload_ids: list[str], session_id: Optional[str] = None) -> list[UploadedFileEntry]:
        """Consume a batch atomically, reserving its files for the print workers."""
        with self._lock:
            entries = [self.get(id_, session_id) for id_ in upload_ids]
            if any(
                entry is None or not entry.completed or not entry.stored_path
                or not entry.stored_path.is_file()
                for entry in entries
            ):
                raise ValueError("Uno o piu file non sono piu disponibili.")
            self._remove(upload_ids)
            for id_, entry in zip(upload_ids, entries):
                del self._uploads[id_]
                self._busy_paths.add(entry.stored_path)
            return entries

    def transfer_path(self, original: Path, prepared: Path) -> None:
        """Track a decrypted document after replacing its original source."""
        with self._lock:
            self._busy_paths.discard(original)
            self._busy_paths.add(prepared)

    def release_path(self, path: Path) -> None:
        """Permit orphan cleanup after a print worker finishes."""
        with self._lock:
            self._busy_paths.discard(path)

    def cleanup_expired(
        self,
        now: Optional[float] = None,
        archive_expired: Optional[Callable[[list[UploadedFileEntry]], None]] = None,
    ) -> int:
        """Expire staged files; retain complete files if archival fails."""
        current = time.time() if now is None else now
        cutoff = current - self.ttl_seconds
        removed = 0
        with self._lock:
            expired = [
                entry for entry in self._uploads.values()
                if entry.updated_at <= cutoff
            ]
            archive_groups: dict[str, list[UploadedFileEntry]] = defaultdict(list)
            if archive_expired:
                for entry in expired:
                    if entry.completed and entry.stored_path and entry.stored_path.is_file():
                        archive_groups[entry.session_id].append(entry)

            preserved = set()
            for group in archive_groups.values():
                try:
                    archive_expired([replace(entry) for entry in group])
                except Exception:
                    preserved.update(entry.id for entry in group)
                    import logging
                    logging.getLogger("webprinter").exception("Failed to archive expired uploads")

            expired = [entry for entry in expired if entry.id not in preserved]
            if expired:
                self._remove([entry.id for entry in expired])
            for entry in expired:
                del self._uploads[entry.id]
                for path in (entry.temp_path, entry.stored_path):
                    if path:
                        path.unlink(missing_ok=True)
                removed += 1

            protected = set(self._busy_paths)
            for entry in self._uploads.values():
                protected.add(entry.temp_path)
                if entry.stored_path:
                    protected.add(entry.stored_path)
            for path in self.upload_dir.iterdir():
                if path.is_file() and path not in protected and path.stat().st_mtime <= cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed
