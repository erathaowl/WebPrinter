"""Regression tests for multi-file printing and image layout."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader, PdfWriter

import app as application
from print_backend import PrintBackendError, prepare_image_pdf


class FakePrinterBackend:
    def __init__(self) -> None:
        self.printed = []

    def list_printers(self) -> list[str]:
        return ["TestPrinter"]

    def print_file(self, file_path, options, progress):
        self.printed.append((file_path.suffix, PdfReader(file_path) if file_path.suffix == ".pdf" else None, options))
        progress(85, "Queued")
        return "TestPrinter-42"


class PendingExecutor:
    def __init__(self) -> None:
        self.job_ids = []

    def submit(self, callback, job_id) -> None:
        self.job_ids.append(job_id)


@pytest.fixture
def service(monkeypatch, tmp_path):
    backend = FakePrinterBackend()
    queue = PendingExecutor()
    monkeypatch.setattr(application, "printer_backend", backend)
    monkeypatch.setattr(application, "backend_boot_error", None)
    monkeypatch.setattr(application, "executor", queue)
    monkeypatch.setattr(application, "jobs", application.JobRegistry())
    monkeypatch.setattr(application, "uploads", application.UploadRegistry())
    monkeypatch.setattr(application, "UPLOAD_DIR", tmp_path)
    return TestClient(application.app), backend, queue


def upload(client, name: str, contents: bytes, identifier: str):
    response = client.post(
        "/uploads/chunk",
        data={
            "upload_id": identifier,
            "chunk_index": "0",
            "total_chunks": "1",
            "original_name": name,
            "mime_type": "application/octet-stream",
        },
        files={"chunk": (name, contents)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["completed"]
    return response.json()


def submit(client, ids, **options):
    fields = [("uploaded_file_id", identifier) for identifier in ids]
    fields += [("printer", "TestPrinter")]
    fields += list(options.items())
    return client.post(
        "/jobs",
        content=urlencode(fields),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def png_bytes(size=(120, 60)):
    stream = io.BytesIO()
    Image.new("RGB", size, "red").save(stream, format="PNG")
    return stream.getvalue()


def encrypted_pdf(password="correct"):
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.encrypt(password)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_batch_queues_each_file_with_shared_settings(service):
    client, backend, queue = service
    upload(client, "one.png", png_bytes(), "uploadtest00001")
    upload(client, "two.txt", b"hello", "uploadtest00002")

    response = submit(
        client,
        ["uploadtest00001", "uploadtest00002"],
        color_mode="color",
        copies="2",
        duplex="true",
        image_mode="fill",
    )

    assert response.status_code == 200
    assert response.headers["HX-Trigger"] == "print-batch-accepted"
    assert response.text.count('class="job-card') == 2
    assert len(queue.job_ids) == 2
    assert application.uploads.get("uploadtest00001") is None
    assert application.uploads.get("uploadtest00002") is None

    for job_id in queue.job_ids:
        job = application.jobs.get(job_id)
        assert job.copies == 2
        assert job.color_mode == "color"
        assert job.duplex is True
        assert job.image_mode == "fill"
        application.process_print_job(job_id)
        assert application.jobs.get(job_id).status == "completed"

    assert backend.printed[0][0] == ".pdf"
    assert backend.printed[0][1] is not None
    assert backend.printed[1][0] == ".txt"
    assert all(options.copies == 2 for _, _, options in backend.printed)


def test_wrong_pdf_password_preserves_all_staged_files(service):
    client, _, queue = service
    upload(client, "secret.pdf", encrypted_pdf(), "uploadtest00003")
    upload(client, "ordinary.txt", b"hello", "uploadtest00004")

    response = submit(
        client,
        ["uploadtest00003", "uploadtest00004"],
        pdf_passwords=json.dumps({"uploadtest00003": "incorrect"}),
    )

    assert response.status_code == 200
    assert "HX-Trigger" not in response.headers
    assert "Password PDF non valida" in response.text
    assert not queue.job_ids
    assert application.uploads.get("uploadtest00003").completed
    assert application.uploads.get("uploadtest00004").completed


def test_batch_rejects_duplicate_or_invalid_layout(service):
    client, _, queue = service
    upload(client, "one.txt", b"hello", "uploadtest00005")
    assert "duplicati" in submit(client, ["uploadtest00005"] * 2).text
    assert "Modalita" in submit(client, ["uploadtest00005"], image_mode="invalid").text
    assert application.uploads.get("uploadtest00005").completed
    assert not queue.job_ids


def test_accepted_encrypted_pdf_is_printable(service):
    client, backend, queue = service
    upload(client, "secure.pdf", encrypted_pdf(), "uploadtest00006")
    response = submit(
        client,
        ["uploadtest00006"],
        pdf_passwords=json.dumps({"uploadtest00006": "correct"}),
    )
    assert response.headers["HX-Trigger"] == "print-batch-accepted"
    application.process_print_job(queue.job_ids[0])
    assert backend.printed[0][1].is_encrypted is False


def test_image_layout_fit_and_fill(tmp_path):
    image_path = tmp_path / "wide.png"
    Image.new("RGB", (800, 200), "blue").save(image_path)
    fit = prepare_image_pdf(image_path, "fit")
    fill = prepare_image_pdf(image_path, "fill")
    try:
        fit_page = PdfReader(fit).pages[0]
        fill_page = PdfReader(fill).pages[0]
        assert float(fit_page.mediabox.width) > float(fit_page.mediabox.height)
        assert abs(float(fill_page.mediabox.width) - float(fit_page.mediabox.width)) < 1
        assert b"W*" not in fit_page.get_contents().get_data()
        assert b"W*" in fill_page.get_contents().get_data()
    finally:
        fit.unlink()
        fill.unlink()


def test_multiframe_tiff_and_gif(tmp_path):
    first = Image.new("RGB", (40, 60), "red")
    second = Image.new("RGB", (40, 60), "blue")
    tiff = tmp_path / "pages.tiff"
    gif = tmp_path / "animated.gif"
    first.save(tiff, save_all=True, append_images=[second])
    first.save(gif, save_all=True, append_images=[second], duration=100)
    for file, expected_pages in [(tiff, 2), (gif, 1)]:
        output = prepare_image_pdf(file)
        try:
            assert len(PdfReader(output).pages) == expected_pages
        finally:
            output.unlink()


def test_rejects_unknown_image_layout(tmp_path):
    with pytest.raises(PrintBackendError):
        prepare_image_pdf(tmp_path / "unused.png", "stretch")


def test_chunked_upload_preview_and_deletion(service):
    client, _, _ = service
    upload_id = "uploadchunk00001"
    payload = {
        "upload_id": upload_id,
        "total_chunks": "2",
        "original_name": "notes.txt",
        "mime_type": "text/plain",
    }
    first = client.post(
        "/uploads/chunk",
        data={**payload, "chunk_index": "0"},
        files={"chunk": ("notes.txt", b"hello ")},
    )
    assert first.status_code == 200
    assert first.json()["completed"] is False

    out_of_order = client.post(
        "/uploads/chunk",
        data={**payload, "chunk_index": "2"},
        files={"chunk": ("notes.txt", b"ignored")},
    )
    assert out_of_order.status_code == 409

    final = client.post(
        "/uploads/chunk",
        data={**payload, "chunk_index": "1"},
        files={"chunk": ("notes.txt", b"world")},
    )
    assert final.status_code == 200
    assert final.json()["completed"] is True
    preview = client.get(final.json()["preview_url"])
    assert preview.status_code == 200
    assert preview.content == b"hello world"

    entry = application.uploads.get(upload_id)
    assert entry.stored_path.exists()
    deleted = client.delete(f"/uploads/{upload_id}")
    assert deleted.status_code == 200
    assert not entry.stored_path.exists()
    assert client.get(final.json()["preview_url"]).status_code == 404


def test_missing_batch_file_preserves_remaining_uploads(service):
    client, _, queue = service
    upload(client, "keep.txt", b"keep", "uploadbatch00001")
    upload(client, "remove.txt", b"remove", "uploadbatch00002")
    assert client.delete("/uploads/uploadbatch00002").status_code == 200

    response = submit(client, ["uploadbatch00001", "uploadbatch00002"])

    assert response.status_code == 200
    assert "HX-Trigger" not in response.headers
    assert not queue.job_ids
    assert application.uploads.get("uploadbatch00001").completed
    assert application.uploads.get("uploadbatch00001").stored_path.exists()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"copies": "0"}, "copie"),
        ({"color_mode": "unexpected"}, "colore"),
        ({"pdf_passwords": "not-json"}, "Expecting value"),
    ],
)
def test_invalid_batch_options_preserve_uploads(service, options, message):
    client, _, queue = service
    upload(client, "keep.txt", b"hello", "uploadoption00001")

    response = submit(client, ["uploadoption00001"], **options)

    assert response.status_code == 200
    assert message.lower() in response.text.lower()
    assert "HX-Trigger" not in response.headers
    assert not queue.job_ids
    assert application.uploads.get("uploadoption00001").completed


def test_batch_limit_prevents_queueing(service):
    client, _, queue = service
    response = submit(client, [f"uploadlimit{i:05d}" for i in range(51)])

    assert response.status_code == 200
    assert "50 file" in response.text
    assert not queue.job_ids


def test_failed_print_cleans_source_and_generated_pdf(service, monkeypatch):
    client, backend, queue = service
    upload(client, "photo.png", png_bytes(), "uploaderror00001")
    response = submit(client, ["uploaderror00001"])
    assert response.headers["HX-Trigger"] == "print-batch-accepted"
    job = application.jobs.get(queue.job_ids[0])
    assert job.image_mode == "fit"
    source = job.stored_path

    generated = []
    actual_prepare = application.prepare_image_pdf

    def track_pdf(file_path, mode):
        result = actual_prepare(file_path, mode)
        generated.append(result)
        return result

    def fail_print(file_path, options, progress):
        raise PrintBackendError("Simulated spooler error")

    monkeypatch.setattr(application, "prepare_image_pdf", track_pdf)
    monkeypatch.setattr(backend, "print_file", fail_print)

    application.process_print_job(job.id)

    result = application.jobs.get(job.id)
    assert result.status == "failed"
    assert "Simulated spooler error" in result.error
    assert not source.exists()
    assert len(generated) == 1
    assert not generated[0].exists()


def test_corrupt_image_fails_safely_without_temporary_files(service, monkeypatch):
    client, backend, queue = service
    upload(client, "broken.png", b"invalid image data", "uploadbroken0001")
    response = submit(client, ["uploadbroken0001"])
    assert response.headers["HX-Trigger"] == "print-batch-accepted"
    job = application.jobs.get(queue.job_ids[0])
    source = job.stored_path

    application.process_print_job(job.id)

    assert application.jobs.get(job.id).status == "failed"
    assert not source.exists()
    assert not backend.printed


@pytest.mark.parametrize("lpadmin_exit", [0, 1])
def test_entrypoint_starts_web_app_after_printer_setup(tmp_path, lpadmin_exit):
    """Printer configuration errors must not trigger a container restart loop."""
    commands = {
        "mkdir": "exit 0",
        "cupsd": "exit 0",
        "lpstat": f'if [ "$1" = "-r" ]; then exit 0; fi\nexit {lpadmin_exit}',
        "lpadmin": f'echo "lpadmin: Success" >&2\nexit {lpadmin_exit}',
        "cupsenable": "exit 0",
        "cupsaccept": "exit 0",
        "lpoptions": "exit 0",
        "uvicorn": 'echo "UVICORN_MOCK_STARTED $*"',
    }
    for name, body in commands.items():
        executable = tmp_path / name
        executable.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        executable.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{tmp_path}:{environment['PATH']}",
            "PRINTER_NAME": "TestPrinter",
            "PRINTER_ADDRESS": "ipp://127.0.0.1/ipp/print",
            "APP_HOST": "127.0.0.1",
            "APP_PORT": "8080",
            "WEBPRINTER_PRINTER_STATUS_FILE": str(tmp_path / "printer-error"),
            "WEBPRINTER_RETRY_INTERVAL": "0",
        }
    )
    script = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"
    result = subprocess.run(
        ["sh", str(script)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert "UVICORN_MOCK_STARTED app:app --host 127.0.0.1 --port 8080" in result.stdout
    status_file = tmp_path / "printer-error"
    if lpadmin_exit:
        assert "Printer setup failed (exit code 1)" in result.stderr
        assert status_file.exists()
        assert "lpadmin: Success" in status_file.read_text(encoding="utf-8")
    else:
        assert "CUPS printer 'TestPrinter' configured." in result.stdout
        assert not status_file.exists()


def test_printer_monitor_reports_configuration_failure_and_recovers(
    service, monkeypatch, tmp_path
):
    client, backend, _ = service
    status_file = tmp_path / "printer-error"
    status_file.write_text(
        "Printer setup failed (exit code 1). Check power and IPP address.",
        encoding="utf-8",
    )
    monkeypatch.setattr(application, "PRINTER_STATUS_FILE", status_file)
    monkeypatch.setenv("PRINTER_NAME", "TestPrinter")
    monkeypatch.setattr(backend, "default_printer", lambda: None, raising=False)
    monkeypatch.setattr(backend, "list_printers", lambda: [])

    unavailable = client.get("/printer-status")
    assert unavailable.status_code == 200
    assert "Printer setup failed (exit code 1)" in unavailable.text

    status_file.unlink()
    monkeypatch.setattr(
        backend,
        "get_status",
        lambda name: application.PrinterStatusSnapshot(
            printer=name,
            state="idle",
            message="Printer is ready.",
            enabled=True,
            accepting_jobs=True,
            queue_length=0,
            device_uri="ipp://127.0.0.1/ipp/print",
            reasons=[],
            toner_levels=[],
        ),
        raising=False,
    )
    recovered = client.get("/printer-status")
    assert recovered.status_code == 200
    assert "Printer is ready." in recovered.text
    assert "Printer setup failed" not in recovered.text


def test_printer_monitor_escapes_error_output(service, monkeypatch, tmp_path):
    client, _, _ = service
    status_file = tmp_path / "printer-error"
    status_file.write_text("<script>alert(1)</script>", encoding="utf-8")
    monkeypatch.setattr(application, "PRINTER_STATUS_FILE", status_file)
    monkeypatch.setenv("PRINTER_NAME", "TestPrinter")

    response = client.get("/printer-status?printer=TestPrinter")

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
