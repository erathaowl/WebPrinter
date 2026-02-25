"""FastAPI application for submitting and monitoring print jobs."""

from __future__ import annotations

import re
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
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

jobs = JobRegistry()
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
    file: UploadFile = File(...),
    printer: str = Form(...),
    color_mode: str = Form("bw"),
    copies: int = Form(1),
    duplex: Optional[str] = Form(None),
    pdf_password: Optional[str] = Form(None),
) -> HTMLResponse:
    """Validate input, queue a print job, and return its initial status panel."""
    if printer_backend is None:
        error_job = _error_job(
            filename=file.filename or "sconosciuto",
            message="Backend di stampa non disponibile.",
            error=backend_boot_error
            or "Configura CUPS o SumatraPDF e riavvia l'applicazione.",
        )
        return _render_job(request, error_job)

    saved_path: Optional[Path] = None
    prepared_path: Optional[Path] = None
    try:
        filename = _validate_upload(file)
        validated_color = _validate_color_mode(color_mode)
        validated_copies = _validate_copies(copies)
        validated_duplex = _as_bool(duplex)

        available_printers = printer_backend.list_printers()
        if printer not in available_printers:
            raise ValueError("Stampante selezionata non valida.")

        job_id = uuid.uuid4().hex
        saved_path = _store_uploaded_file(file=file, original_name=filename, job_id=job_id)
        prepared_path = _prepare_pdf_for_print(saved_path, pdf_password)
        queued_message = "File caricato, in attesa di stampa."
        if prepared_path != saved_path:
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
        if prepared_path and prepared_path.exists():
            prepared_path.unlink(missing_ok=True)
        if saved_path and saved_path.exists():
            saved_path.unlink(missing_ok=True)
        error_job = _error_job(
            filename=file.filename or "sconosciuto",
            message="Richiesta non valida.",
            error=str(exc),
        )
        return _render_job(request, error_job)


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
