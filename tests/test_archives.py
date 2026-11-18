"""Permanent archive policy, job reports, and expired upload tests."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

import app as application
from archive_storage import ArchiveConfig, ArchiveManager
from upload_storage import UploadRegistry, UploadedFileEntry


class FakePrinter:
    def list_printers(self):
        return ["TestPrinter"]

    def default_printer(self):
        return "TestPrinter"

    def print_file(self, file_path, options, progress):
        if file_path.name.endswith("bad.txt"):
            raise RuntimeError("Mock printer failure")
        return "TestPrinter-123"


class FakeExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, callback, job_id):
        self.jobs.append(job_id)


@pytest.fixture
def archive_service(tmp_path, monkeypatch):
    registry = UploadRegistry(tmp_path / "uploads.sqlite3", tmp_path / "uploads")
    executor = FakeExecutor()
    monkeypatch.setattr(application, "uploads", registry)
    monkeypatch.setattr(application, "UPLOAD_DIR", registry.upload_dir)
    monkeypatch.setattr(application, "jobs", application.JobRegistry())
    monkeypatch.setattr(application, "executor", executor)
    monkeypatch.setattr(application, "printer_backend", FakePrinter())

    def configure(mode="disabled", expired=False, passwords=False):
        config = ArchiveConfig(
            mode=mode,
            directory=tmp_path / "archive",
            expired_uploads=expired,
            pdf_passwords=passwords,
        )
        manager = ArchiveManager(config)
        monkeypatch.setattr(application, "archive_config", config)
        monkeypatch.setattr(application, "archives", manager)
        return manager

    return TestClient(application.app), registry, executor, configure


def stage(client, name, content, upload_id):
    result = client.post(
        "/uploads/chunk",
        data={
            "upload_id": upload_id,
            "chunk_index": "0",
            "total_chunks": "1",
            "original_name": name,
        },
        files={"chunk": (name, content)},
    )
    assert result.status_code == 200, result.text
    return result.json()


def submit(client, file_ids, enabled=False, passwords=None):
    fields = [("uploaded_file_id", file_id) for file_id in file_ids]
    fields.append(("printer", "TestPrinter"))
    fields.append(("copies", "2"))
    fields.append(("image_mode", "fill"))
    fields.append(("color_mode", "color"))
    if enabled:
        fields.append(("archive_job", "true"))
    if passwords is not None:
        fields.append(("pdf_passwords", json.dumps(passwords)))
    return client.post(
        "/jobs",
        content=urlencode(fields),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


@pytest.mark.parametrize(
    ("mode", "selected", "should_archive"),
    [
        ("disabled", False, False),
        ("disabled", True, False),
        ("opt_in", False, False),
        ("opt_in", True, True),
        ("opt_out", False, False),
        ("opt_out", True, True),
        ("always", False, True),
    ],
)
def test_job_policy_enforced_server_side(
    archive_service, mode, selected, should_archive
):
    client, _, queue, configure = archive_service
    manager = configure(mode)
    stage(client, "document.txt", b"original", "archivefile00001")

    result = submit(client, ["archivefile00001"], enabled=selected)

    assert result.headers["HX-Trigger"] == "print-batch-accepted"
    assert len(queue.jobs) == 1
    job = application.jobs.get(queue.jobs[0])
    assert bool(job.archive_path) is should_archive
    reports = list((manager.config.directory / "jobs").glob("*/report.json"))
    assert len(reports) == int(should_archive)


@pytest.mark.parametrize(
    ("mode", "visible", "checked"),
    [
        ("disabled", False, False),
        ("opt_in", True, False),
        ("opt_out", True, True),
        ("always", False, False),
    ],
)
def test_ui_shows_archive_checkbox_only_when_configurable(
    archive_service, mode, visible, checked
):
    client, _, _, configure = archive_service
    configure(mode)
    html = client.get("/").text

    assert ('name="archive_job"' in html) is visible
    if visible:
        segment = html.split('name="archive_job"', 1)[1].split("</label>", 1)[0]
        assert ("checked" in segment) is checked


def test_batch_archive_keeps_originals_and_tracks_mixed_results(archive_service):
    client, _, queue, configure = archive_service
    manager = configure("always")
    stage(client, "good.txt", b"first content", "archivegood0001")
    stage(client, "bad.txt", b"second content", "archivebad00001")

    response = submit(client, ["archivegood0001", "archivebad00001"])
    assert response.headers["HX-Trigger"] == "print-batch-accepted"
    assert len(queue.jobs) == 2
    for job_id in queue.jobs:
        application.process_print_job(job_id)

    folders = list((manager.config.directory / "jobs").iterdir())
    assert len(folders) == 1
    folder = folders[0]
    report = json.loads((folder / "report.json").read_text())
    assert report["status"] == "partial_failed"
    assert report["completed_at"]
    assert report["printer"] == "TestPrinter"
    assert report["settings"] == {
        "color_mode": "color", "copies": 2, "duplex": False, "image_mode": "fill"
    }
    assert len(report["session_id"]) == 64
    assert report["client_ip"] == "testclient"
    assert [file["status"] for file in report["files"]] == ["submitted", "failed"]
    assert report["files"][0]["printer_job_id"] == "TestPrinter-123"
    assert "Mock printer failure" in report["files"][1]["error"]
    assert [(folder / file["saved_as"]).read_bytes() for file in report["files"]] == [
        b"first content", b"second content"
    ]
    assert all(len(file["sha256"]) == 64 for file in report["files"])


def encrypted_pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.encrypt("example-password")
    content = io.BytesIO()
    writer.write(content)
    return content.getvalue()


@pytest.mark.parametrize("password_storage", [False, True])
def test_pdf_password_is_only_saved_with_explicit_opt_in(archive_service, password_storage):
    client, _, queue, configure = archive_service
    manager = configure("always", passwords=password_storage)
    original = encrypted_pdf()
    stage(client, "secret.pdf", original, "archivepdf00001")

    response = submit(
        client,
        ["archivepdf00001"],
        passwords={"archivepdf00001": "example-password"},
    )
    assert response.headers["HX-Trigger"] == "print-batch-accepted"
    application.process_print_job(queue.jobs[0])
    report_file = next((manager.config.directory / "jobs").glob("*/report.json"))
    report = json.loads(report_file.read_text())
    archived = report_file.parent / report["files"][0]["saved_as"]
    assert archived.read_bytes() == original
    assert report["files"][0]["status"] == "submitted"
    assert ("pdf_password" in report["files"][0]) is password_storage
    if password_storage:
        assert report["files"][0]["pdf_password"] == "example-password"
    else:
        assert "example-password" not in report_file.read_text()


@pytest.mark.parametrize(
    ("mode", "expired_enabled", "expected"),
    [
        ("disabled", True, False),
        ("opt_in", True, False),
        ("opt_out", False, False),
        ("opt_out", True, True),
        ("always", True, True),
    ],
)
def test_expired_upload_archive_policy(
    archive_service, mode, expired_enabled, expected
):
    _, registry, _, configure = archive_service
    manager = configure(mode, expired=expired_enabled)
    timestamp = time.time() - 7200
    source = registry.upload_dir / "expired.txt"
    source.write_bytes(b"abandoned")
    registry.create_or_replace(
        UploadedFileEntry(
            id="expiredarchive01",
            session_id="known-hashed-session",
            filename="expired.txt",
            mime_type="text/plain",
            temp_path=registry.upload_dir / "expired.part",
            stored_path=source,
            total_chunks=1,
            completed=True,
            updated_at=timestamp,
            client_ip="192.0.2.5",
        )
    )
    callback = manager.archive_expired if manager.config.archive_expired else None
    registry.cleanup_expired(archive_expired=callback)
    assert not source.exists()
    reports = list((manager.config.directory / "expired").glob("*/report.json"))
    assert len(reports) == int(expected)
    if expected:
        report = json.loads(reports[0].read_text())
        assert report["session_id"] == "known-hashed-session"
        assert report["client_ips"] == ["192.0.2.5"]
        assert (reports[0].parent / report["files"][0]["saved_as"]).read_bytes() == b"abandoned"


def test_archive_failure_prevents_print_and_preserves_staged_files(
    archive_service, monkeypatch
):
    client, registry, queue, configure = archive_service
    manager = configure("always")
    stage(client, "failed.txt", b"keep", "archiveerror0001")

    def fail_copy(*args):
        raise OSError("Archive disk full")

    monkeypatch.setattr(manager, "_copy", fail_copy)
    result = submit(client, ["archiveerror0001"])
    assert "Archive disk full" in result.text
    assert "HX-Trigger" not in result.headers
    assert not queue.jobs
    assert registry.get("archiveerror0001").stored_path.read_bytes() == b"keep"
    assert not list((manager.config.directory / "jobs").iterdir())


def test_archive_failure_during_expiry_retains_upload_for_retry(
    archive_service, monkeypatch
):
    _, registry, _, configure = archive_service
    manager = configure("always", expired=True)
    source = registry.upload_dir / "retry.txt"
    source.write_bytes(b"retained")
    registry.create_or_replace(
        UploadedFileEntry(
            id="expiredretry0001",
            session_id="owner",
            filename="retry.txt",
            mime_type="text/plain",
            temp_path=registry.upload_dir / "retry.part",
            stored_path=source,
            total_chunks=1,
            completed=True,
            updated_at=time.time() - 7200,
        )
    )
    def fail_archive(entries):
        raise OSError("Unavailable storage")

    registry.cleanup_expired(archive_expired=fail_archive)
    assert source.exists()
    assert "expiredretry0001" in registry._uploads
    registry.cleanup_expired(archive_expired=manager.archive_expired)
    assert not source.exists()
    assert len(list((manager.config.directory / "expired").glob("*/report.json"))) == 1


def test_incomplete_batch_is_marked_interrupted_after_restart(archive_service):
    client, _, queue, configure = archive_service
    manager = configure("always")
    stage(client, "queued.txt", b"pending", "archivequeue0001")
    submit(client, ["archivequeue0001"])
    report_file = next((manager.config.directory / "jobs").glob("*/report.json"))
    assert json.loads(report_file.read_text())["status"] == "queued"
    ArchiveManager(manager.config).reconcile_interrupted()
    report = json.loads(report_file.read_text())
    assert report["status"] == "interrupted"
    assert report["files"][0]["status"] == "interrupted"
    assert report["completed_at"]


def test_pdf_password_flag_defaults_off():
    assert ArchiveConfig().pdf_passwords is False
    assert ArchiveConfig.from_env().mode in {"disabled", "opt_in", "opt_out", "always"}
