"""Permanent print archives and configurable retention policies."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from upload_storage import DATA_DIR, UploadedFileEntry


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean archive setting: {value!r}")


@dataclass(frozen=True)
class ArchiveConfig:
    mode: str = "disabled"
    directory: Path = DATA_DIR / "archive"
    expired_uploads: bool = False
    pdf_passwords: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"disabled", "opt_in", "opt_out", "always"}:
            raise ValueError(f"Invalid WEBPRINTER_ARCHIVE_MODE: {self.mode}")

    @classmethod
    def from_env(cls) -> "ArchiveConfig":
        return cls(
            mode=os.getenv("WEBPRINTER_ARCHIVE_MODE", "disabled").strip().lower(),
            directory=Path(os.getenv("WEBPRINTER_ARCHIVE_DIR", str(DATA_DIR / "archive"))),
            expired_uploads=parse_boolean(
                os.getenv("WEBPRINTER_ARCHIVE_EXPIRED_UPLOADS", "false")
            ),
            pdf_passwords=parse_boolean(
                os.getenv("WEBPRINTER_ARCHIVE_PDF_PASSWORDS", "false")
            ),
        )

    @property
    def user_can_choose(self) -> bool:
        return self.mode in {"opt_in", "opt_out"}

    @property
    def default_enabled(self) -> bool:
        return self.mode in {"opt_out", "always"}

    @property
    def archive_expired(self) -> bool:
        return self.expired_uploads and self.mode in {"opt_out", "always"}

    def enabled_for_job(self, requested: bool) -> bool:
        if self.mode == "disabled":
            return False
        if self.mode == "always":
            return True
        return requested


class ArchiveManager:
    """Write permanent archives outside the temporary upload directory."""

    def __init__(self, config: ArchiveConfig) -> None:
        self.config = config
        self._lock = threading.RLock()

    def _folder(self, category: str, identifier: str) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        location = self.config.directory / category / f"{timestamp}_{identifier}"
        location.mkdir(mode=0o700, parents=True, exist_ok=False)
        location.chmod(0o700)
        (location / "files").mkdir(mode=0o700)
        return location

    @staticmethod
    def _copy(source: Path, target: Path) -> dict:
        if not source.is_file():
            raise FileNotFoundError(source)
        hasher = hashlib.sha256()
        with source.open("rb") as original, target.open("xb") as saved:
            os.chmod(target, 0o600)
            for block in iter(lambda: original.read(1024 * 1024), b""):
                saved.write(block)
                hasher.update(block)
        return {"size_bytes": target.stat().st_size, "sha256": hasher.hexdigest()}

    @staticmethod
    def _safe_filename(filename: str, position: int) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)
        return f"{position:03d}_{cleaned[:180] or 'file'}"

    @staticmethod
    def _write_report(location: Path, report: dict) -> None:
        temp = location / f".report-{uuid.uuid4().hex}.tmp"
        try:
            with temp.open("x", encoding="utf-8") as output:
                os.chmod(temp, 0o600)
                json.dump(report, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, location / "report.json")
        finally:
            temp.unlink(missing_ok=True)

    def create_job_archive(
        self,
        entries: list[UploadedFileEntry],
        job_ids: list[str],
        batch_id: str,
        session_id: str,
        client_ip: str,
        printer: str,
        settings: dict,
        passwords: dict[str, str],
    ) -> Path:
        """Preserve originals before a batch is consumed for printing."""
        with self._lock:
            location = self._folder("jobs", batch_id)
            try:
                documents = []
                for position, (entry, job_id) in enumerate(zip(entries, job_ids), 1):
                    saved_name = self._safe_filename(entry.filename, position)
                    metadata = self._copy(entry.stored_path, location / "files" / saved_name)
                    document = {
                        "job_id": job_id,
                        "upload_id": entry.id,
                        "filename": entry.filename,
                        "saved_as": f"files/{saved_name}",
                        **metadata,
                        "status": "queued",
                        "printer_job_id": None,
                        "error": None,
                    }
                    if (
                        self.config.pdf_passwords
                        and entry.filename.lower().endswith(".pdf")
                        and passwords.get(entry.id)
                    ):
                        document["pdf_password"] = passwords[entry.id]
                    documents.append(document)

                report = {
                    "batch_id": batch_id,
                    "created_at": utc_timestamp(),
                    "completed_at": None,
                    "session_id": session_id,
                    "client_ip": client_ip,
                    "printer": printer,
                    "settings": settings,
                    "status": "queued",
                    "files": documents,
                }
                self._write_report(location, report)
                return location
            except Exception:
                shutil.rmtree(location, ignore_errors=True)
                raise

    def discard_archive(self, location: Path) -> None:
        """Remove an uncommitted archive when the batch was never accepted."""
        with self._lock:
            shutil.rmtree(location, ignore_errors=True)

    def update_job(self, location: Path, job_id: str, status: str,
                   printer_job_id: Optional[str], error: Optional[str]) -> None:
        """Atomically update an individual document and the batch outcome."""
        with self._lock:
            report_file = location / "report.json"
            report = json.loads(report_file.read_text(encoding="utf-8"))
            document = next(item for item in report["files"] if item["job_id"] == job_id)
            document["status"] = "submitted" if status == "completed" else "failed"
            document["printer_job_id"] = printer_job_id
            document["error"] = error

            statuses = {item["status"] for item in report["files"]}
            if statuses <= {"submitted", "failed"}:
                if statuses == {"submitted"}:
                    report["status"] = "submitted"
                elif statuses == {"failed"}:
                    report["status"] = "failed"
                else:
                    report["status"] = "partial_failed"
                report["completed_at"] = utc_timestamp()
            else:
                report["status"] = "processing"
            self._write_report(location, report)

    def reconcile_interrupted(self) -> None:
        """Mark incomplete reports interrupted after an application restart."""
        root = self.config.directory / "jobs"
        if not root.exists():
            return
        with self._lock:
            for report_file in root.glob("*/report.json"):
                try:
                    report = json.loads(report_file.read_text(encoding="utf-8"))
                    if report.get("status") not in {"queued", "processing"}:
                        continue
                    for item in report["files"]:
                        if item["status"] in {"queued", "printing"}:
                            item["status"] = "interrupted"
                    report["status"] = "interrupted"
                    report["completed_at"] = utc_timestamp()
                    self._write_report(report_file.parent, report)
                except (OSError, ValueError, KeyError):
                    # Preserve inconsistent reports for manual inspection.
                    continue

    def archive_expired(self, entries: list[UploadedFileEntry]) -> None:
        """Preserve completed expired uploads in a session-scoped archive."""
        if not entries:
            return
        with self._lock:
            location = self._folder("expired", uuid.uuid4().hex)
            try:
                files = []
                for index, entry in enumerate(entries, 1):
                    saved_name = self._safe_filename(entry.filename, index)
                    metadata = self._copy(entry.stored_path, location / "files" / saved_name)
                    files.append({
                        "upload_id": entry.id,
                        "filename": entry.filename,
                        "saved_as": f"files/{saved_name}",
                        "last_upload_at": datetime.fromtimestamp(
                            entry.updated_at, timezone.utc
                        ).isoformat(timespec="seconds").replace("+00:00", "Z"),
                        "client_ip": entry.client_ip,
                        **metadata,
                    })
                report = {
                    "archive_id": location.name,
                    "created_at": utc_timestamp(),
                    "session_id": entries[0].session_id,
                    "client_ips": sorted({entry.client_ip for entry in entries if entry.client_ip}),
                    "reason": "upload_expired",
                    "files": files,
                }
                self._write_report(location, report)
            except Exception:
                shutil.rmtree(location, ignore_errors=True)
                raise
