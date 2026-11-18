"""FastAPI application for submitting and monitoring print jobs."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from mimetypes import guess_type
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pypdf import PdfReader, PdfWriter

from archive_storage import ArchiveConfig, ArchiveManager
from upload_storage import UPLOAD_DIR, UploadRegistry, UploadedFileEntry

from print_backend import (
    PrintBackendError,
    PrintOptions,
    PrinterStatusSnapshot,
    build_backend,
    prepare_image_pdf,
)


BASE_DIR = Path(__file__).resolve().parent
PRINTER_STATUS_FILE = Path(os.getenv("WEBPRINTER_PRINTER_STATUS_FILE", "/run/webprinter/printer-error"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
ALLOWED_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}
MAX_BATCH_FILES = 50
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tif', '.tiff', '.webp'}
UPLOAD_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")
TERMINAL_STATUSES = {"completed", "failed"}
STATUS_LABELS = {
    "queued": "In coda",
    "preparing": "Preparazione",
    "printing": "In stampa",
    "completed": "Completato",
    "failed": "Errore",
}
PRINTER_STATE_LABELS = {
    "idle": "Pronta",
    "printing": "In stampa",
    "stopped": "Fermata",
    "unknown": "Sconosciuto",
}


@dataclass
class PrintJob:
    """Represents a print job tracked by the application."""

    id: str
    filename: str
    stored_path: Optional[Path]
    printer: str
    color_mode: str
    copies: int
    duplex: bool
    image_mode: str = "fit"
    session_id: str = ""
    archive_path: Optional[Path] = None
    status: str = "queued"
    progress: int = 5
    message: str = "File ricevuto, in coda."
    error: Optional[str] = None
    printer_job_id: Optional[str] = None


class JobRegistry:
    """Thread-safe in-memory storage for print jobs."""

    def __init__(self) -> None:
        """Initialize the internal job map and lock."""
        self._jobs: dict[str, PrintJob] = {}
        self._lock = Lock()

    def create(self, job: PrintJob) -> None:
        """Store a new job by its identifier."""
        with self._lock:
            self._jobs[job.id] = job

    def get(self, job_id: str) -> Optional[PrintJob]:
        """Return a snapshot copy of a job, if available."""
        with self._lock:
            job = self._jobs.get(job_id)
            return replace(job) if job else None

    def update(self, job_id: str, **changes: object) -> Optional[PrintJob]:
        """Apply field updates to a job and return the updated snapshot."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            for key, value in changes.items():
                setattr(job, key, value)
            return replace(job)


app = FastAPI(title="WebPrinter")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

COOKIE_NAME = "webprinter_session"
COOKIE_MAX_AGE = 30 * 24 * 60 * 60
logger = logging.getLogger("webprinter")
jobs = JobRegistry()
uploads = UploadRegistry()
archive_config = ArchiveConfig.from_env()
archives = ArchiveManager(archive_config)
executor = ThreadPoolExecutor(max_workers=2)
printer_backend, backend_boot_error = build_backend()
cleanup_stop = Event()
cleanup_thread: Optional[Thread] = None


@app.middleware("http")
async def session_cookie(request: Request, call_next):
    """Associate each browser with an unguessable, HttpOnly session cookie."""
    token = request.cookies.get(COOKIE_NAME, "")
    created = not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    if created:
        token = secrets.token_urlsafe(32)
    request.state.session_id = hashlib.sha256(token.encode("ascii")).hexdigest()
    response = await call_next(request)
    if created:
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
            path="/",
        )
    return response


def cleanup_uploads() -> None:
    """Remove expired uploads, or archive completed ones when configured."""
    callback = archives.archive_expired if archive_config.archive_expired else None
    uploads.cleanup_expired(archive_expired=callback)


def cleanup_loop() -> None:
    """Process abandoned uploads independently of active browsers."""
    while not cleanup_stop.wait(300):
        try:
            cleanup_uploads()
        except Exception:
            logger.exception("Upload cleanup failed")


@app.on_event("startup")
def start_upload_cleanup() -> None:
    global cleanup_thread
    if archive_config.mode != "disabled":
        archives.reconcile_interrupted()
    cleanup_uploads()
    cleanup_stop.clear()
    cleanup_thread = Thread(target=cleanup_loop, name="upload-cleanup", daemon=True)
    cleanup_thread.start()



@app.on_event("shutdown")
def shutdown_executor() -> None:
    """Stop the background executor during application shutdown."""
    cleanup_stop.set()
    if cleanup_thread:
        cleanup_thread.join(timeout=1)
    executor.shutdown(wait=False, cancel_futures=True)


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Render the main page with printer availability information."""
    printers: list[str] = []
    default_printer: Optional[str] = None
    backend_error = backend_boot_error

    if printer_backend:
        try:
            printers = printer_backend.list_printers()
            default_printer = printer_backend.default_printer()
        except PrintBackendError as exc:
            backend_error = str(exc)

    return TEMPLATES.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "printers": printers,
            "default_printer": default_printer,
            "backend_ready": bool(printer_backend),
            "backend_error": backend_error,
            "archive_mode": archive_config.mode,
            "archive_default": archive_config.default_enabled,
            "archive_expired_notice": archive_config.archive_expired,
        },
    )


@app.get("/printer-status", response_class=HTMLResponse)
def printer_status(request: Request, printer: Optional[str] = None) -> HTMLResponse:
    """Render the printer status panel for the selected printer."""
    snapshot: Optional[PrinterStatusSnapshot] = None
    error: Optional[str] = None

    selected_printer = _resolve_printer_name(printer)
    configured_printer = os.getenv("PRINTER_NAME", "").strip()
    if printer_backend is None:
        error = backend_boot_error or "Backend di stampa non disponibile."
    elif not selected_printer:
        error = "Nessuna stampante disponibile."
    else:
        if selected_printer == configured_printer:
            try:
                error = PRINTER_STATUS_FILE.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()[:500] or None
            except OSError:
                pass
        if error is None:
            try:
                snapshot = printer_backend.get_status(selected_printer)
            except PrintBackendError as exc:
                error = str(exc)

    return TEMPLATES.TemplateResponse(
        request=request,
        name="_printer_status.html",
        context={
            "status_snapshot": snapshot,
            "status_error": error,
            "printer_state_labels": PRINTER_STATE_LABELS,
        },
    )


@app.post("/jobs", response_class=HTMLResponse)
def create_job(
    request: Request,
    uploaded_file_id: list[str] = Form(...),
    printer: str = Form(...),
    color_mode: str = Form("bw"),
    copies: int = Form(1),
    duplex: Optional[str] = Form(None),
    image_mode: str = Form("fit"),
    pdf_passwords: str = Form("{}"),
    archive_job: Optional[str] = Form(None),
) -> HTMLResponse:
    """Validate and queue an entire batch with shared print settings."""
    if printer_backend is None:
        return _render_job(
            request,
            _error_job("sconosciuto", "Backend di stampa non disponibile.", backend_boot_error),
        )

    prepared_paths: list[tuple[Path, Path]] = []
    consumed = False
    archive_path: Optional[Path] = None
    try:
        if not uploaded_file_id or len(uploaded_file_id) > MAX_BATCH_FILES:
            raise ValueError(f"Seleziona da 1 a {MAX_BATCH_FILES} file.")
        ids = [_validate_upload_id(upload_id) for upload_id in uploaded_file_id]
        if len(set(ids)) != len(ids):
            raise ValueError("Sono presenti file duplicati nella richiesta.")

        validated_color = _validate_color_mode(color_mode)
        validated_copies = _validate_copies(copies)
        validated_image_mode = _validate_image_mode(image_mode)
        if printer not in printer_backend.list_printers():
            raise ValueError("Stampante selezionata non valida.")

        passwords = json.loads(pdf_passwords or "{}")
        if not isinstance(passwords, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in passwords.items()
        ):
            raise ValueError("Password PDF non valide.")

        try:
            entries = [_require_completed_upload(upload_id, request.state.session_id) for upload_id in ids]
        except HTTPException as exc:
            raise ValueError(str(exc.detail)) from exc

        for entry in entries:
            source = entry.stored_path
            assert source is not None
            prepared = _prepare_pdf_for_print(source, passwords.get(entry.id))
            prepared_paths.append((source, prepared))

        job_ids = [uuid.uuid4().hex for _ in entries]
        if archive_config.enabled_for_job(_as_bool(archive_job)):
            batch_id = uuid.uuid4().hex
            try:
                archive_path = archives.create_job_archive(
                    entries=entries,
                    job_ids=job_ids,
                    batch_id=batch_id,
                    session_id=request.state.session_id,
                    client_ip=request.client.host if request.client else "",
                    printer=printer,
                    settings={
                        "color_mode": validated_color,
                        "copies": validated_copies,
                        "duplex": _as_bool(duplex),
                        "image_mode": validated_image_mode,
                    },
                    passwords=passwords,
                )
            except OSError as exc:
                raise ValueError(f"Impossibile archiviare i documenti: {exc}") from exc

        uploads.pop_many(ids, request.state.session_id)
        consumed = True

        queued_jobs = []
        for entry, job_id, (source, prepared) in zip(entries, job_ids, prepared_paths):
            if prepared != source:
                uploads.transfer_path(source, prepared)
                source.unlink(missing_ok=True)
            job = PrintJob(
                id=job_id,
                filename=entry.filename,
                stored_path=prepared,
                printer=printer,
                color_mode=validated_color,
                copies=validated_copies,
                duplex=_as_bool(duplex),
                image_mode=validated_image_mode,
                session_id=request.state.session_id,
                archive_path=archive_path,
                status="queued",
                progress=8,
                message="File in attesa di stampa.",
            )
            jobs.create(job)
            executor.submit(process_print_job, job.id)
            queued_jobs.append(job)

        html = "".join(_render_job(request, job).body.decode("utf-8") for job in queued_jobs)
        return HTMLResponse(html, headers={"HX-Trigger": "print-batch-accepted"})

    except (ValueError, PrintBackendError, HTTPException, json.JSONDecodeError, OSError) as exc:
        if not consumed:
            if archive_path:
                archives.discard_archive(archive_path)
            for source, prepared in prepared_paths:
                if prepared != source:
                    _cleanup_file_paths(prepared)
        return _render_job(
            request,
            _error_job("Lotto di stampa", "Richiesta non valida.", str(exc)),
        )


@app.get("/uploads")
def list_uploaded_files(request: Request) -> JSONResponse:
    """Restore the current session's completed uploads after a page refresh."""
    files = [
        {
            "id": entry.id,
            "name": entry.filename,
            "preview_url": f"/uploads/{entry.id}/preview",
            "type": _preview_kind(entry.filename),
            "requires_password": _pdf_requires_password(entry.stored_path)
            if entry.filename.lower().endswith(".pdf") else False,
        }
        for entry in uploads.list_for_session(request.state.session_id)
    ]
    return JSONResponse({"files": files}, headers={"Cache-Control": "no-store"})


@app.post("/uploads/chunk")
async def upload_chunk(
    request: Request,
    upload_id: str = Form(...),
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    original_name: str = Form(...),
    mime_type: str = Form("application/octet-stream"),
    chunk: UploadFile = File(...),
) -> JSONResponse:
    """Receive one chunk of a file and assemble it on disk."""
    normalized_upload_id = _validate_upload_id(upload_id)
    filename = _validate_filename(original_name)
    total_chunks = _validate_total_chunks(total_chunks)
    if chunk_index < 0:
        raise HTTPException(status_code=400, detail="Indice chunk non valido.")

    part_path = UPLOAD_DIR / f"{normalized_upload_id}.part"
    session_id = request.state.session_id
    entry = uploads.get(normalized_upload_id, session_id)
    if chunk_index == 0:
        if uploads.get(normalized_upload_id) and entry is None:
            raise HTTPException(status_code=409, detail="Upload ID gia in uso.")
        if entry:
            _cleanup_file_paths(entry.temp_path, entry.stored_path)
        _cleanup_file_paths(part_path)
        fresh_entry = UploadedFileEntry(
            id=normalized_upload_id,
            session_id=session_id,
            filename=filename,
            mime_type=mime_type or "application/octet-stream",
            client_ip=request.client.host if request.client else "",
            temp_path=part_path,
            total_chunks=total_chunks,
        )
        try:
            uploads.create_or_replace(fresh_entry)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        entry = fresh_entry

    if entry is None:
        raise HTTPException(status_code=404, detail="Upload non inizializzato.")
    if entry.completed:
        raise HTTPException(status_code=409, detail="Upload gia completato.")
    if entry.total_chunks != total_chunks:
        raise HTTPException(status_code=409, detail="Numero chunk incoerente.")
    if entry.next_chunk_index != chunk_index:
        raise HTTPException(status_code=409, detail="Chunk fuori sequenza.")

    data = await chunk.read()
    mode = "wb" if chunk_index == 0 else "ab"
    with entry.temp_path.open(mode) as destination:
        destination.write(data)

    uploaded_bytes = entry.uploaded_bytes + len(data)
    is_last = chunk_index + 1 >= total_chunks

    if is_last:
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", entry.filename)
        final_path = UPLOAD_DIR / f"{normalized_upload_id}_{safe_name}"
        _cleanup_file_paths(final_path)
        entry.temp_path.replace(final_path)
        file_type = _preview_kind(entry.filename)
        pdf_requires_password = _pdf_requires_password(final_path) if file_type == "pdf" else False
        uploads.update(
            normalized_upload_id,
            session_id=session_id,
            next_chunk_index=chunk_index + 1,
            uploaded_bytes=uploaded_bytes,
            stored_path=final_path,
            completed=True,
        )
        return JSONResponse(
            {
                "completed": True,
                "upload_id": normalized_upload_id,
                "filename": entry.filename,
                "preview_url": f"/uploads/{normalized_upload_id}/preview",
                "file_type": file_type,
                "pdf_requires_password": pdf_requires_password,
            }
        )

    uploads.update(
        normalized_upload_id,
        session_id=session_id,
        next_chunk_index=chunk_index + 1,
        uploaded_bytes=uploaded_bytes,
    )
    return JSONResponse(
        {
            "completed": False,
            "upload_id": normalized_upload_id,
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
        }
    )


@app.delete("/uploads/{upload_id}")
def delete_uploaded_file(request: Request, upload_id: str) -> JSONResponse:
    """Delete a staged upload (temporary or fully assembled)."""
    normalized_upload_id = _validate_upload_id(upload_id)
    entry = uploads.pop(normalized_upload_id, request.state.session_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Upload non trovato.")

    _cleanup_file_paths(entry.temp_path, entry.stored_path)
    return JSONResponse({"deleted": True, "upload_id": normalized_upload_id})


@app.get("/uploads/{upload_id}/preview")
def preview_uploaded_file(request: Request, upload_id: str) -> FileResponse:
    """Serve the uploaded file for post-upload preview."""
    entry = _require_completed_upload(upload_id, request.state.session_id)
    media_type = guess_type(entry.filename)[0] or "application/octet-stream"
    return FileResponse(
        path=entry.stored_path,
        media_type=media_type,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_status(request: Request, job_id: str) -> HTMLResponse:
    """Render the latest status for a single print job."""
    job = jobs.get(job_id)
    if not job or (job.session_id and job.session_id != request.state.session_id):
        raise HTTPException(status_code=404, detail="Job non trovato")
    return _render_job(request, job)


def process_print_job(job_id: str) -> None:
    """Execute printing in the background and update job progress."""
    snapshot = jobs.get(job_id)
    if not snapshot:
        return
    if not printer_backend:
        _cleanup_file_paths(snapshot.stored_path)
        if snapshot.stored_path:
            uploads.release_path(snapshot.stored_path)
        jobs.update(
            job_id,
            status="failed",
            progress=100,
            message="Backend non disponibile.",
            error=backend_boot_error or "Configura un backend di stampa valido.",
        )
        update_archived_job(job_id)
        return

    jobs.update(job_id, status="preparing", progress=20, message="Preparazione del file.")

    def progress(percent: int, message: str) -> None:
        """Persist progressive feedback while the backend is printing."""
        jobs.update(
            job_id,
            status="printing",
            progress=max(0, min(99, percent)),
            message=message,
        )

    image_pdf: Optional[Path] = None
    try:
        source_path = snapshot.stored_path or Path()
        if source_path.suffix.lower() in IMAGE_EXTENSIONS:
            image_pdf = prepare_image_pdf(source_path, snapshot.image_mode)
            source_path = image_pdf

        options = PrintOptions(
            printer=snapshot.printer,
            copies=snapshot.copies,
            color=snapshot.color_mode == "color",
            duplex=snapshot.duplex,
        )
        printer_job_id = printer_backend.print_file(
            file_path=source_path,
            options=options,
            progress=progress,
        )
        jobs.update(
            job_id,
            status="completed",
            progress=100,
            message="Stampa inviata con successo.",
            printer_job_id=printer_job_id,
            error=None,
        )
    except Exception as exc:
        jobs.update(
            job_id,
            status="failed",
            progress=100,
            message="Errore durante il processo di stampa.",
            error=str(exc),
        )
    finally:
        _cleanup_file_paths(image_pdf)
        current = jobs.get(job_id)
        if current and current.stored_path and current.stored_path.exists():
            try:
                current.stored_path.unlink()
            except OSError:
                jobs.update(
                    job_id,
                    message="Stampa completata, ma non riesco a eliminare il file temporaneo.",
                )
        if current and current.stored_path:
            uploads.release_path(current.stored_path)
        update_archived_job(job_id)


def update_archived_job(job_id: str) -> None:
    """Store a final spooler outcome without interrupting cleanup."""
    job = jobs.get(job_id)
    if not job or not job.archive_path:
        return
    try:
        archives.update_job(
            job.archive_path, job.id, job.status, job.printer_job_id, job.error
        )
    except (OSError, ValueError, KeyError) as exc:
        logger.exception("Failed to update job archive %s: %s", job.id, exc)


def _render_job(request: Request, job: PrintJob) -> HTMLResponse:
    """Render the reusable job status fragment."""
    return TEMPLATES.TemplateResponse(
        request=request,
        name="_job_status.html",
        context={
            "job": job,
            "terminal_statuses": TERMINAL_STATUSES,
            "status_labels": STATUS_LABELS,
        },
    )


def _validate_upload(file: UploadFile) -> str:
    """Validate file name and extension and return a safe base filename."""
    filename = (file.filename or "").strip()
    if not filename:
        raise ValueError("Seleziona un file.")
    clean_name = Path(filename).name
    extension = Path(clean_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise ValueError(f"Formato non supportato. Usa: {allowed}")
    return clean_name


def _validate_filename(filename: str) -> str:
    """Validate a plain filename and allowed extension."""
    clean_name = Path((filename or "").strip()).name
    if not clean_name:
        raise HTTPException(status_code=400, detail="Nome file non valido.")
    extension = Path(clean_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise HTTPException(status_code=400, detail=f"Formato non supportato. Usa: {allowed}")
    return clean_name


def _validate_upload_id(upload_id: str) -> str:
    """Validate upload identifier format."""
    normalized = (upload_id or "").strip()
    if not UPLOAD_ID_PATTERN.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="Upload ID non valido.")
    return normalized


def _validate_total_chunks(total_chunks: int) -> int:
    """Validate chunk count constraints."""
    if total_chunks < 1 or total_chunks > 5000:
        raise HTTPException(status_code=400, detail="Numero chunk non valido.")
    return total_chunks


def _validate_color_mode(color_mode: str) -> str:
    """Validate and normalize the selected color mode."""
    mode = (color_mode or "").lower()
    if mode not in {"bw", "color"}:
        raise ValueError("Modalita colore non valida.")
    return mode


def _validate_copies(copies: int) -> int:
    """Ensure the number of copies is within accepted bounds."""
    if copies < 1 or copies > 99:
        raise ValueError("Il numero di copie deve essere tra 1 e 99.")
    return copies


def _validate_image_mode(image_mode: str) -> str:
    """Normalize the image layout preference."""
    mode = (image_mode or "").lower()
    if mode not in {"fit", "fill"}:
        raise ValueError("Modalita di adattamento immagine non valida.")
    return mode


def _store_uploaded_file(file: UploadFile, original_name: str, job_id: str) -> Path:
    """Store the uploaded file on disk using a sanitized unique name."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", original_name)
    target_path = UPLOAD_DIR / f"{job_id}_{safe_name}"
    with target_path.open("wb") as destination:
        shutil.copyfileobj(file.file, destination)
    return target_path


def _prepare_pdf_for_print(file_path: Path, pdf_password: Optional[str]) -> Path:
    """Decrypt encrypted PDFs when needed and return the printable path."""
    if file_path.suffix.lower() != ".pdf":
        return file_path

    try:
        reader = PdfReader(str(file_path))
    except Exception as exc:
        raise ValueError(f"PDF non valido o corrotto: {exc}") from exc

    if not reader.is_encrypted:
        return file_path

    if not pdf_password:
        raise ValueError("Il PDF e protetto da password. Inserisci la password PDF.")

    try:
        decrypted = reader.decrypt(pdf_password)
    except Exception as exc:
        raise ValueError(f"Impossibile decriptare il PDF: {exc}") from exc
    if not decrypted:
        raise ValueError("Password PDF non valida.")

    output_path = file_path.with_name(f"{file_path.stem}_decrypted.pdf")
    writer = PdfWriter()
    try:
        for page in reader.pages:
            writer.add_page(page)
        with output_path.open("wb") as output_file:
            writer.write(output_file)
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        raise ValueError(f"Errore durante la preparazione del PDF: {exc}") from exc

    return output_path


def _consume_uploaded_file(uploaded_file_id: str) -> UploadedFileEntry:
    """Remove an uploaded file from staging and return its metadata."""
    upload_id = _validate_upload_id(uploaded_file_id)
    entry = uploads.pop(upload_id)
    if not entry or not entry.completed or not entry.stored_path:
        raise ValueError("File non caricato o upload incompleto.")
    if not entry.stored_path.exists():
        raise ValueError("File caricato non disponibile sul server.")
    return entry


def _require_completed_upload(upload_id: str, session_id: str) -> UploadedFileEntry:
    """Load a completed upload entry or raise HTTP errors."""
    normalized = _validate_upload_id(upload_id)
    entry = uploads.get(normalized, session_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Upload non trovato.")
    if not entry.completed or not entry.stored_path:
        raise HTTPException(status_code=409, detail="Upload non ancora completato.")
    if not entry.stored_path.exists():
        raise HTTPException(status_code=404, detail="File caricato non disponibile.")
    return entry


def _preview_kind(filename: str) -> str:
    """Classify the uploaded file for frontend preview rendering."""
    extension = Path(filename).suffix.lower()
    if extension in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}:
        return "image"
    if extension == ".pdf":
        return "pdf"
    if extension == ".txt":
        return "text"
    return "other"


def _pdf_requires_password(file_path: Path) -> bool:
    """Return True only when the uploaded PDF is encrypted."""
    if file_path.suffix.lower() != ".pdf":
        return False
    try:
        reader = PdfReader(str(file_path))
    except Exception:
        return False
    return bool(reader.is_encrypted)


def _cleanup_file_paths(*paths: Optional[Path]) -> None:
    """Best-effort cleanup helper for uploaded file paths."""
    for path in paths:
        if path and path.exists():
            path.unlink(missing_ok=True)


def _resolve_printer_name(printer: Optional[str]) -> Optional[str]:
    """Resolve explicit, default, or first available printer name."""
    requested = (printer or "").strip()
    if requested:
        return requested
    if not printer_backend:
        return None
    try:
        default_printer = printer_backend.default_printer()
        if default_printer:
            return default_printer
        printers = printer_backend.list_printers()
        if printers:
            return printers[0]
    except PrintBackendError:
        pass
    return os.getenv("PRINTER_NAME", "").strip() or None


def _as_bool(value: Optional[str]) -> bool:
    """Parse common HTML form truthy values into a boolean."""
    if value is None:
        return False
    return value.lower() in {"1", "true", "yes", "on"}


def _error_job(filename: str, message: str, error: Optional[str]) -> PrintJob:
    """Build a failed job payload used for immediate UI feedback."""
    return PrintJob(
        id=uuid.uuid4().hex,
        filename=Path(filename).name or "sconosciuto",
        stored_path=None,
        printer="-",
        color_mode="bw",
        copies=1,
        duplex=False,
        status="failed",
        progress=100,
        message=message,
        error=error,
    )
