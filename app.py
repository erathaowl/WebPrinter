"""FastAPI application for submitting and monitoring print jobs."""

from __future__ import annotations

import re
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from mimetypes import guess_type
from pathlib import Path
from threading import Lock
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pypdf import PdfReader, PdfWriter

from print_backend import (
    PrintBackendError,
    PrintOptions,
    PrinterStatusSnapshot,
    build_backend,
)


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
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
    status: str = "queued"
    progress: int = 5
    message: str = "File ricevuto, in coda."
    error: Optional[str] = None
    printer_job_id: Optional[str] = None


@dataclass
class UploadedFileEntry:
    """Represents a staged file upload before print submission."""

    id: str
    filename: str
    mime_type: str
    temp_path: Path
    total_chunks: int
    next_chunk_index: int = 0
    uploaded_bytes: int = 0
    stored_path: Optional[Path] = None
    completed: bool = False


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


class UploadRegistry:
    """Thread-safe in-memory storage for chunked file uploads."""

    def __init__(self) -> None:
        """Initialize upload map and lock."""
        self._uploads: dict[str, UploadedFileEntry] = {}
        self._lock = Lock()

    def get(self, upload_id: str) -> Optional[UploadedFileEntry]:
        """Return a snapshot copy of an upload entry, if available."""
        with self._lock:
            entry = self._uploads.get(upload_id)
            return replace(entry) if entry else None

    def create_or_replace(self, entry: UploadedFileEntry) -> None:
        """Create or replace an upload entry."""
        with self._lock:
            self._uploads[entry.id] = entry

    def update(self, upload_id: str, **changes: object) -> Optional[UploadedFileEntry]:
        """Apply field updates to an upload and return a snapshot."""
        with self._lock:
            entry = self._uploads.get(upload_id)
            if not entry:
                return None
            for key, value in changes.items():
                setattr(entry, key, value)
            return replace(entry)

    def pop(self, upload_id: str) -> Optional[UploadedFileEntry]:
        """Remove and return an upload entry."""
        with self._lock:
            entry = self._uploads.pop(upload_id, None)
            return replace(entry) if entry else None


app = FastAPI(title="WebPrinter")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

jobs = JobRegistry()
uploads = UploadRegistry()
executor = ThreadPoolExecutor(max_workers=2)
printer_backend, backend_boot_error = build_backend()


@app.on_event("shutdown")
def shutdown_executor() -> None:
    """Stop the background executor during application shutdown."""
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
        },
    )


@app.get("/printer-status", response_class=HTMLResponse)
def printer_status(request: Request, printer: Optional[str] = None) -> HTMLResponse:
    """Render the printer status panel for the selected printer."""
    snapshot: Optional[PrinterStatusSnapshot] = None
    error: Optional[str] = None

    selected_printer = _resolve_printer_name(printer)
    if printer_backend is None:
        error = backend_boot_error or "Backend di stampa non disponibile."
    elif not selected_printer:
        error = "Nessuna stampante disponibile."
    else:
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
    uploaded_file_id: str = Form(...),
    printer: str = Form(...),
    color_mode: str = Form("bw"),
    copies: int = Form(1),
    duplex: Optional[str] = Form(None),
    pdf_password: Optional[str] = Form(None),
) -> HTMLResponse:
    """Queue a print job using a previously uploaded file."""
    if printer_backend is None:
        error_job = _error_job(
            filename="sconosciuto",
            message="Backend di stampa non disponibile.",
            error=backend_boot_error
            or "Configura CUPS o SumatraPDF e riavvia l'applicazione.",
        )
        return _render_job(request, error_job)

    prepared_path: Optional[Path] = None
    upload_entry: Optional[UploadedFileEntry] = None
    try:
        upload_entry = _consume_uploaded_file(uploaded_file_id)
        filename = upload_entry.filename
        validated_color = _validate_color_mode(color_mode)
        validated_copies = _validate_copies(copies)
        validated_duplex = _as_bool(duplex)

        available_printers = printer_backend.list_printers()
        if printer not in available_printers:
            raise ValueError("Stampante selezionata non valida.")

        job_id = uuid.uuid4().hex
        prepared_path = _prepare_pdf_for_print(upload_entry.stored_path or Path(), pdf_password)
        queued_message = "File caricato, in attesa di stampa."
        if upload_entry.stored_path and prepared_path != upload_entry.stored_path:
            queued_message = "PDF protetto decriptato, in attesa di stampa."
        job = PrintJob(
            id=job_id,
            filename=filename,
            stored_path=prepared_path,
            printer=printer,
            color_mode=validated_color,
            copies=validated_copies,
            duplex=validated_duplex,
            status="queued",
            progress=8,
            message=queued_message,
        )
        jobs.create(job)
        executor.submit(process_print_job, job.id)
        return _render_job(request, job)

    except (ValueError, PrintBackendError) as exc:
        if prepared_path and prepared_path.exists() and upload_entry and prepared_path != upload_entry.stored_path:
            prepared_path.unlink(missing_ok=True)
        if upload_entry and upload_entry.stored_path and upload_entry.stored_path.exists():
            upload_entry.stored_path.unlink(missing_ok=True)
        error_job = _error_job(
            filename=upload_entry.filename if upload_entry else "sconosciuto",
            message="Richiesta non valida.",
            error=str(exc),
        )
        return _render_job(request, error_job)


@app.post("/uploads/chunk")
async def upload_chunk(
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
    entry = uploads.get(normalized_upload_id)
    if chunk_index == 0:
        _cleanup_file_paths(part_path)
        fresh_entry = UploadedFileEntry(
            id=normalized_upload_id,
            filename=filename,
            mime_type=mime_type or "application/octet-stream",
            temp_path=part_path,
            total_chunks=total_chunks,
        )
        uploads.create_or_replace(fresh_entry)
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
def delete_uploaded_file(upload_id: str) -> JSONResponse:
    """Delete a staged upload (temporary or fully assembled)."""
    normalized_upload_id = _validate_upload_id(upload_id)
    entry = uploads.pop(normalized_upload_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Upload non trovato.")

    _cleanup_file_paths(entry.temp_path, entry.stored_path)
    return JSONResponse({"deleted": True, "upload_id": normalized_upload_id})


@app.get("/uploads/{upload_id}/preview")
def preview_uploaded_file(upload_id: str) -> FileResponse:
    """Serve the uploaded file for post-upload preview."""
    entry = _require_completed_upload(upload_id)
    media_type = entry.mime_type or guess_type(entry.filename)[0] or "application/octet-stream"
    return FileResponse(
        path=entry.stored_path,
        media_type=media_type,
        content_disposition_type="inline",
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_status(request: Request, job_id: str) -> HTMLResponse:
    """Render the latest status for a single print job."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    return _render_job(request, job)


def process_print_job(job_id: str) -> None:
    """Execute printing in the background and update job progress."""
    snapshot = jobs.get(job_id)
    if not snapshot:
        return
    if not printer_backend:
        jobs.update(
            job_id,
            status="failed",
            progress=100,
            message="Backend non disponibile.",
            error=backend_boot_error or "Configura un backend di stampa valido.",
        )
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

    try:
        options = PrintOptions(
            printer=snapshot.printer,
            copies=snapshot.copies,
            color=snapshot.color_mode == "color",
            duplex=snapshot.duplex,
        )
        printer_job_id = printer_backend.print_file(
            file_path=snapshot.stored_path or Path(),
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
        current = jobs.get(job_id)
        if current and current.stored_path and current.stored_path.exists():
            try:
                current.stored_path.unlink()
            except OSError:
                jobs.update(
                    job_id,
                    message="Stampa completata, ma non riesco a eliminare il file temporaneo.",
                )


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

    file_path.unlink(missing_ok=True)
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


def _require_completed_upload(upload_id: str) -> UploadedFileEntry:
    """Load a completed upload entry or raise HTTP errors."""
    normalized = _validate_upload_id(upload_id)
    entry = uploads.get(normalized)
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
        return None
    return None


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
