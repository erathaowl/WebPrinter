"""Session recovery, access isolation, and automatic upload expiry."""

from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

import app as application
from upload_storage import UploadRegistry, UploadedFileEntry


class FakePrinter:
    def list_printers(self):
        return ["TestPrinter"]

    def default_printer(self):
        return "TestPrinter"


class FakeExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, callback, job_id):
        self.jobs.append(job_id)


@pytest.fixture
def browser_service(monkeypatch, tmp_path):
    registry = UploadRegistry(tmp_path / "uploads.sqlite3", tmp_path / "uploads", ttl_minutes=60)
    monkeypatch.setattr(application, "uploads", registry)
    monkeypatch.setattr(application, "UPLOAD_DIR", registry.upload_dir)
    monkeypatch.setattr(application, "printer_backend", FakePrinter())
    monkeypatch.setattr(application, "executor", FakeExecutor())
    monkeypatch.setattr(application, "jobs", application.JobRegistry())
    return TestClient(application.app), registry


def stage_file(client, upload_id="sessionfile00001"):
    result = client.post(
        "/uploads/chunk",
        data={
            "upload_id": upload_id,
            "chunk_index": "0",
            "total_chunks": "1",
            "original_name": "report.txt",
            "mime_type": "text/plain",
        },
        files={"chunk": ("report.txt", b"private report")},
    )
    assert result.status_code == 200, result.text
    return result.json()


def print_file(client, upload_id):
    return client.post(
        "/jobs",
        content=urlencode([("uploaded_file_id", upload_id), ("printer", "TestPrinter")]),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def test_refresh_restores_files_and_session_cookie(browser_service):
    browser, registry = browser_service
    uploaded = stage_file(browser)
    token = browser.cookies.get(application.COOKIE_NAME)
    assert token and len(token) == 43
    assert browser.get("/uploads").json()["files"][0]["name"] == "report.txt"

    refreshed = TestClient(application.app)
    refreshed.cookies.set(application.COOKIE_NAME, token)
    files = refreshed.get("/uploads").json()["files"]
    assert [item["id"] for item in files] == [uploaded["upload_id"]]
    assert refreshed.get(files[0]["preview_url"]).content == b"private report"

    restarted_registry = UploadRegistry(registry.db_path, registry.upload_dir)
    session_id = application.hashlib.sha256(token.encode("ascii")).hexdigest()
    assert len(restarted_registry.list_for_session(session_id)) == 1


def test_session_separation_covers_listing_preview_delete_and_print(browser_service):
    owner, registry = browser_service
    uploaded = stage_file(owner)
    another = TestClient(application.app)

    assert another.get("/uploads").json()["files"] == []
    assert another.get(uploaded["preview_url"]).status_code == 404
    assert another.delete("/uploads/" + uploaded["upload_id"]).status_code == 404
    attempt = print_file(another, uploaded["upload_id"])
    assert "HX-Trigger" not in attempt.headers
    assert registry.get(uploaded["upload_id"]) is not None
    assert len(owner.get("/uploads").json()["files"]) == 1

    accepted = print_file(owner, uploaded["upload_id"])
    assert accepted.headers["HX-Trigger"] == "print-batch-accepted"
    assert owner.get("/uploads").json()["files"] == []


def test_cookie_is_httponly_and_invalid_cookie_gets_replaced(browser_service):
    client, _ = browser_service
    result = client.get("/")
    cookie = result.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie

    invalid = TestClient(application.app)
    invalid.cookies.set(application.COOKIE_NAME, "invalid")
    response = invalid.get("/uploads")
    assert response.status_code == 200
    assert "set-cookie" in response.headers
    assert len(invalid.cookies.get(application.COOKIE_NAME)) == 43


def test_expired_file_and_abandoned_chunk_are_cleaned(tmp_path):
    registry = UploadRegistry(tmp_path / "uploads.sqlite3", tmp_path / "uploads", ttl_minutes=1)
    timestamp = time.time() - 120
    part = registry.upload_dir / "orphan.part"
    part.write_bytes(b"unfinished")
    complete = registry.upload_dir / "old_report.txt"
    complete.write_bytes(b"report")
    registry.create_or_replace(
        UploadedFileEntry(
            id="expiredfile00001",
            session_id="owned-session",
            filename="old_report.txt",
            mime_type="text/plain",
            temp_path=part,
            stored_path=complete,
            total_chunks=1,
            completed=True,
            updated_at=timestamp,
        )
    )
    assert registry.list_for_session("owned-session") == []
    assert registry.cleanup_expired() >= 1
    assert not complete.exists()
    assert not part.exists()
    assert registry.get("expiredfile00001") is None


def test_orphan_cleanup_respects_active_print_jobs(tmp_path):
    registry = UploadRegistry(tmp_path / "uploads.sqlite3", tmp_path / "uploads", ttl_minutes=1)
    file = registry.upload_dir / "active.txt"
    file.write_bytes(b"job")
    registry.create_or_replace(
        UploadedFileEntry(
            id="activejob00001",
            session_id="owner",
            filename="active.txt",
            mime_type="text/plain",
            temp_path=registry.upload_dir / "active.part",
            stored_path=file,
            total_chunks=1,
            completed=True,
        )
    )
    registry.pop_many(["activejob00001"], "owner")
    previous = time.time() - 120
    os.utime(file, (previous, previous))
    registry.cleanup_expired()
    assert file.exists()
    registry.release_path(file)
    registry.cleanup_expired()
    assert not file.exists()


def test_other_session_cannot_overwrite_an_active_upload(browser_service):
    owner, registry = browser_service
    identifier = "sharedchunk00001"
    first = owner.post(
        "/uploads/chunk",
        data={
            "upload_id": identifier,
            "chunk_index": "0",
            "total_chunks": "2",
            "original_name": "original.txt",
        },
        files={"chunk": ("original.txt", b"first")},
    )
    assert first.status_code == 200
    assert first.json()["completed"] is False

    outsider = TestClient(application.app)
    takeover = outsider.post(
        "/uploads/chunk",
        data={
            "upload_id": identifier,
            "chunk_index": "0",
            "total_chunks": "1",
            "original_name": "replacement.txt",
        },
        files={"chunk": ("replacement.txt", b"attacker")},
    )
    assert takeover.status_code == 409
    assert registry.get(identifier).filename == "original.txt"

    final = owner.post(
        "/uploads/chunk",
        data={
            "upload_id": identifier,
            "chunk_index": "1",
            "total_chunks": "2",
            "original_name": "original.txt",
        },
        files={"chunk": ("original.txt", b" second")},
    )
    assert final.status_code == 200
    assert owner.get(final.json()["preview_url"]).content == b"first second"
