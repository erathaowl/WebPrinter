from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


class PrintBackendError(RuntimeError):
    """Errore sollevato dal backend di stampa."""


ProgressCallback = Callable[[int, str], None]


@dataclass(frozen=True)
class PrintOptions:
    printer: str
    copies: int = 1
    color: bool = False
    duplex: bool = False


class PrinterBackend(Protocol):
    def list_printers(self) -> list[str]:
        ...

    def default_printer(self) -> Optional[str]:
        ...

    def print_file(
        self,
        file_path: Path,
        options: PrintOptions,
        progress: ProgressCallback,
    ) -> Optional[str]:
        ...


class CupsPrinterBackend:
    REQUEST_ID_PATTERN = re.compile(r"request id is ([^\s]+)")

    def __init__(self) -> None:
        if not shutil.which("lp") or not shutil.which("lpstat"):
            raise PrintBackendError(
                "Comandi CUPS non trovati: servono 'lp' e 'lpstat'."
            )

    def list_printers(self) -> list[str]:
        result = subprocess.run(
            ["lpstat", "-a"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "impossibile leggere le stampanti."
            raise PrintBackendError(f"Errore nel caricamento stampanti: {detail}")

        printers: list[str] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            printers.append(line.split()[0])
        return sorted(set(printers))

    def default_printer(self) -> Optional[str]:
        result = subprocess.run(
            ["lpstat", "-d"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        output = result.stdout.strip()
        marker = "system default destination:"
        if marker in output:
            return output.split(marker, 1)[1].strip()
        return None

    def print_file(
        self,
        file_path: Path,
        options: PrintOptions,
        progress: ProgressCallback,
    ) -> Optional[str]:
        if not file_path.exists():
            raise PrintBackendError("Il file da stampare non esiste.")
        if options.copies < 1:
            raise PrintBackendError("Numero copie non valido.")

        command = [
            "lp",
            "-d",
            options.printer,
            "-n",
            str(options.copies),
            "-o",
            "sides=two-sided-long-edge" if options.duplex else "sides=one-sided",
        ]
        if options.color:
            command.extend(["-o", "print-color-mode=color", "-o", "ColorModel=RGB"])
        else:
            command.extend(
                ["-o", "print-color-mode=monochrome", "-o", "ColorModel=Gray"]
            )
        command.append(str(file_path))

        progress(40, "Invio del documento alla coda locale.")
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "errore sconosciuto"
            raise PrintBackendError(f"Comando lp fallito: {detail}")

        job_id = self._extract_job_id(result.stdout)
        if job_id:
            progress(65, f"Job {job_id} accodato, controllo avanzamento.")
            self._wait_for_release(options.printer, job_id, progress)
        else:
            progress(85, "Documento accodato.")
        return job_id

    def _extract_job_id(self, output: str) -> Optional[str]:
        match = self.REQUEST_ID_PATTERN.search(output or "")
        if match:
            return match.group(1)
        cleaned = (output or "").strip()
        if cleaned:
            return cleaned.split()[0]
        return None

    def _wait_for_release(
        self,
        printer: str,
        job_id: str,
        progress: ProgressCallback,
    ) -> None:
        for attempt in range(1, 13):
            time.sleep(1)
            result = subprocess.run(
                ["lpstat", "-o", printer],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return
            if job_id not in result.stdout:
                progress(92, "La coda ha accettato il job.")
                return
            progress(min(90, 68 + attempt * 2), "Job ancora in coda locale.")


class SumatraPrinterBackend:
    def __init__(self) -> None:
        self.executable = self._find_sumatra()
        if not self.executable:
            raise PrintBackendError(
                "Su Windows e richiesto SumatraPDF. "
                "Imposta SUMATRA_PDF_PATH o installa SumatraPDF."
            )

    def list_printers(self) -> list[str]:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Printer | Select-Object -ExpandProperty Name",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "impossibile leggere le stampanti."
            raise PrintBackendError(f"Errore nel caricamento stampanti: {detail}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def default_printer(self) -> Optional[str]:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Printer | "
                "Where-Object {$_.Default -eq $true} | "
                "Select-Object -First 1 -ExpandProperty Name)",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None

    def print_file(
        self,
        file_path: Path,
        options: PrintOptions,
        progress: ProgressCallback,
    ) -> Optional[str]:
        if not file_path.exists():
            raise PrintBackendError("Il file da stampare non esiste.")
        if options.copies < 1:
            raise PrintBackendError("Numero copie non valido.")

        source_path = file_path
        generated_pdf: Optional[Path] = None
        if file_path.suffix.lower() == ".txt":
            generated_pdf = self._text_to_pdf(file_path)
            source_path = generated_pdf

        settings = [f"{options.copies}x", "color" if options.color else "monochrome"]
        settings.append("duplexlong" if options.duplex else "simplex")
        settings_arg = ",".join(settings)

        command = [
            str(self.executable),
            "-silent",
            "-print-to",
            options.printer,
            "-print-settings",
            settings_arg,
            str(source_path),
        ]

        progress(45, "Invio del documento al sistema di stampa Windows.")
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if generated_pdf and generated_pdf.exists():
            generated_pdf.unlink(missing_ok=True)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "errore sconosciuto"
            raise PrintBackendError(f"SumatraPDF ha fallito la stampa: {detail}")

        progress(90, "Documento inviato al servizio spooler.")
        return None

    def _find_sumatra(self) -> Optional[Path]:
        candidates = [
            os.getenv("SUMATRA_PDF_PATH"),
            r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
            r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            candidate_path = Path(candidate)
            if candidate_path.exists():
                return candidate_path
        return None

    def _text_to_pdf(self, text_path: Path) -> Path:
        with text_path.open("r", encoding="utf-8", errors="replace") as source:
            lines = source.readlines()

        handle = tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            suffix=".pdf",
            prefix="webprinter_text_",
        )
        handle.close()
        output_path = Path(handle.name)

        document = canvas.Canvas(str(output_path), pagesize=A4)
        _, height = A4
        left_margin = 36
        top_margin = height - 36
        line_height = 13

        document.setFont("Courier", 10)
        y_position = top_margin
        for raw_line in lines:
            line = raw_line.rstrip("\n")
            chunks = [line[i : i + 110] for i in range(0, len(line), 110)] or [""]
            for chunk in chunks:
                if y_position < 36:
                    document.showPage()
                    document.setFont("Courier", 10)
                    y_position = top_margin
                document.drawString(left_margin, y_position, chunk)
                y_position -= line_height
        document.save()
        return output_path


def build_backend() -> tuple[Optional[PrinterBackend], Optional[str]]:
    if shutil.which("lp") and shutil.which("lpstat"):
        try:
            return CupsPrinterBackend(), None
        except PrintBackendError as exc:
            return None, str(exc)

    if os.name == "nt":
        try:
            return SumatraPrinterBackend(), None
        except PrintBackendError as exc:
            return None, str(exc)

    return (
        None,
        "Nessun backend disponibile. "
        "Installa CUPS (lp/lpstat) oppure usa Windows con SumatraPDF.",
    )
