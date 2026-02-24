from __future__ import annotations

import json
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


@dataclass(frozen=True)
class TonerLevel:
    name: str
    percent: Optional[int]
    color: Optional[str] = None

    @property
    def color_key(self) -> str:
        text = f"{self.color or ''} {self.name}".lower()
        if "black" in text or "nero" in text:
            return "black"
        if "cyan" in text:
            return "cyan"
        if "magenta" in text:
            return "magenta"
        if "yellow" in text or "giallo" in text:
            return "yellow"
        return "generic"


@dataclass(frozen=True)
class PrinterStatusSnapshot:
    printer: str
    state: str
    message: str
    enabled: Optional[bool]
    accepting_jobs: Optional[bool]
    queue_length: int
    device_uri: Optional[str]
    reasons: list[str]
    toner_levels: list[TonerLevel]
    toner_note: Optional[str] = None


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

    def get_status(self, printer: str) -> PrinterStatusSnapshot:
        ...


class CupsPrinterBackend:
    REQUEST_ID_PATTERN = re.compile(r"request id is ([^\s]+)")
    IPP_ATTRIBUTE_PATTERN = re.compile(r"^([A-Za-z0-9-]+)\s+\([^)]+\)\s+=\s*(.+)$")

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

    def get_status(self, printer: str) -> PrinterStatusSnapshot:
        if not printer:
            raise PrintBackendError("Nessuna stampante selezionata.")

        printer_result = subprocess.run(
            ["lpstat", "-p", printer, "-l"],
            check=False,
            capture_output=True,
            text=True,
        )
        if printer_result.returncode != 0:
            detail = (
                printer_result.stderr.strip()
                or printer_result.stdout.strip()
                or "errore sconosciuto"
            )
            raise PrintBackendError(
                f"Impossibile leggere lo stato della stampante '{printer}': {detail}"
            )

        lines = [line.strip() for line in printer_result.stdout.splitlines() if line.strip()]
        summary = lines[0] if lines else f"Stato non disponibile per {printer}."
        state, enabled = self._map_state(summary)

        accepting_jobs: Optional[bool] = None
        accepting_result = subprocess.run(
            ["lpstat", "-a", printer],
            check=False,
            capture_output=True,
            text=True,
        )
        if accepting_result.returncode == 0:
            accepting_jobs = "accepting requests" in accepting_result.stdout.lower()

        queue_result = subprocess.run(
            ["lpstat", "-o", printer],
            check=False,
            capture_output=True,
            text=True,
        )
        queue_length = 0
        if queue_result.returncode == 0:
            queue_length = len(
                [line for line in queue_result.stdout.splitlines() if line.strip()]
            )

        uri_result = subprocess.run(
            ["lpstat", "-v", printer],
            check=False,
            capture_output=True,
            text=True,
        )
        device_uri = None
        if uri_result.returncode == 0:
            device_uri = self._parse_device_uri(uri_result.stdout, printer)

        reasons = self._extract_reasons(lines[1:])
        toner_levels, toner_note = self._load_toner_levels(printer, device_uri)

        return PrinterStatusSnapshot(
            printer=printer,
            state=state,
            message=summary,
            enabled=enabled,
            accepting_jobs=accepting_jobs,
            queue_length=queue_length,
            device_uri=device_uri,
            reasons=reasons,
            toner_levels=toner_levels,
            toner_note=toner_note,
        )

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

    def _map_state(self, summary: str) -> tuple[str, Optional[bool]]:
        text = summary.lower()
        enabled = "disabled" not in text
        if "printing" in text or "processing" in text:
            return "printing", enabled
        if "idle" in text or "ready" in text:
            return "idle", enabled
        if "disabled" in text or "stopped" in text:
            return "stopped", enabled
        return "unknown", enabled

    def _parse_device_uri(self, output: str, printer: str) -> Optional[str]:
        marker = f"device for {printer}:"
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.lower().startswith(marker.lower()):
                return line.split(":", 1)[1].strip()
            if line.lower().startswith("device for "):
                parts = line.split(":", 1)
                if len(parts) == 2:
                    return parts[1].strip()
        return None

    def _extract_reasons(self, lines: list[str]) -> list[str]:
        reasons: list[str] = []
        for line in lines:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            if not value:
                continue
            if key in {"alerts", "printer-state-reasons", "reasons"}:
                for reason in re.split(r",\s*", value):
                    reason = reason.strip()
                    if reason and reason.lower() not in {"none", "no alerts"}:
                        reasons.append(reason)
        return reasons

    def _load_toner_levels(
        self,
        printer: str,
        device_uri: Optional[str],
    ) -> tuple[list[TonerLevel], Optional[str]]:
        if not shutil.which("ipptool"):
            return [], "Livelli toner non disponibili (manca il comando ipptool)."

        test_path = self._find_ipptool_test()
        if test_path is None:
            return [], "Livelli toner non disponibili (test ipptool non trovato)."

        uris = [f"ipp://localhost/printers/{printer}"]
        if device_uri and device_uri.lower().startswith(("ipp://", "ipps://")):
            uris.append(device_uri)

        last_error: Optional[str] = None
        for uri in dict.fromkeys(uris):
            result = subprocess.run(
                ["ipptool", "-c", "-t", uri, str(test_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                last_error = result.stderr.strip() or result.stdout.strip() or "errore"
                continue

            attributes = self._parse_ipptool_output(result.stdout)
            toner_levels = self._extract_toner_levels(attributes)
            if toner_levels:
                return toner_levels, None
            return [], "La stampante non espone i livelli toner via IPP."

        if last_error:
            return [], f"Impossibile leggere i livelli toner: {last_error}"
        return [], "Livelli toner non disponibili."

    def _find_ipptool_test(self) -> Optional[Path]:
        candidates = [
            Path("/usr/share/cups/ipptool/get-printer-attributes.test"),
            Path("/usr/local/share/cups/ipptool/get-printer-attributes.test"),
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _parse_ipptool_output(self, output: str) -> dict[str, str]:
        attributes: dict[str, str] = {}
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = self.IPP_ATTRIBUTE_PATTERN.match(line)
            if match:
                key = match.group(1).lower()
                value = match.group(2).strip()
                attributes[key] = value
        return attributes

    def _extract_toner_levels(self, attributes: dict[str, str]) -> list[TonerLevel]:
        names = self._split_ipp_values(attributes.get("marker-names", ""))
        levels = self._split_ipp_values(attributes.get("marker-levels", ""))
        colors = self._split_ipp_values(attributes.get("marker-colors", ""))
        kinds = self._split_ipp_values(attributes.get("marker-types", ""))

        item_count = max(len(names), len(levels), len(colors), len(kinds))
        if item_count == 0:
            return []

        toner_levels: list[TonerLevel] = []
        for index in range(item_count):
            level_value = self._parse_int(levels[index]) if index < len(levels) else None
            normalized_level = self._normalize_toner_level(level_value)
            color = colors[index] if index < len(colors) else None
            name = ""
            if index < len(names):
                name = names[index]
            if not name and index < len(kinds):
                name = kinds[index]
            if not name:
                name = f"Toner {index + 1}"

            toner_levels.append(
                TonerLevel(
                    name=name,
                    percent=normalized_level,
                    color=color,
                )
            )
        return toner_levels

    def _split_ipp_values(self, raw: str) -> list[str]:
        if not raw:
            return []
        return [chunk.strip().strip('"') for chunk in raw.split(",") if chunk.strip()]

    def _parse_int(self, raw: str) -> Optional[int]:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _normalize_toner_level(self, level: Optional[int]) -> Optional[int]:
        if level is None or level < 0:
            return None
        if level <= 100:
            return level
        if level <= 10000:
            return max(0, min(100, round(level / 100)))
        return None


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

    def get_status(self, printer: str) -> PrinterStatusSnapshot:
        if not printer:
            raise PrintBackendError("Nessuna stampante selezionata.")

        safe_name = self._ps_escape(printer)
        script = (
            f"$name = '{safe_name}'; "
            "$printer = Get-Printer -Name $name -ErrorAction Stop; "
            "$jobs = @(Get-PrintJob -PrinterName $name -ErrorAction SilentlyContinue); "
            "[PSCustomObject]@{"
            "Name=$printer.Name; "
            "PrinterStatus=$printer.PrinterStatus; "
            "WorkOffline=$printer.WorkOffline; "
            "Comment=$printer.Comment; "
            "PortName=$printer.PortName; "
            "QueueLength=$jobs.Count"
            "} | ConvertTo-Json -Compress"
        )

        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "impossibile leggere lo stato stampante."
            raise PrintBackendError(detail)

        try:
            payload = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError as exc:
            raise PrintBackendError(f"Risposta stato stampante non valida: {exc}") from exc

        raw_state = str(payload.get("PrinterStatus", "unknown"))
        offline = bool(payload.get("WorkOffline"))
        state = self._map_windows_state(raw_state, offline)
        message = payload.get("Comment") or f"Stato Windows: {raw_state}"
        reasons = ["offline"] if offline else []

        queue_length = 0
        try:
            queue_length = int(payload.get("QueueLength") or 0)
        except (TypeError, ValueError):
            queue_length = 0

        return PrinterStatusSnapshot(
            printer=printer,
            state=state,
            message=message,
            enabled=True,
            accepting_jobs=None,
            queue_length=queue_length,
            device_uri=payload.get("PortName"),
            reasons=reasons,
            toner_levels=[],
            toner_note="Livelli toner non disponibili su backend Windows.",
        )

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

    def _ps_escape(self, value: str) -> str:
        return value.replace("'", "''")

    def _map_windows_state(self, raw_state: str, offline: bool) -> str:
        if offline:
            return "stopped"

        if raw_state.isdigit():
            code = int(raw_state)
            mapping = {
                3: "idle",
                4: "printing",
                5: "printing",
                6: "stopped",
                7: "stopped",
            }
            return mapping.get(code, "unknown")

        text = raw_state.lower()
        if "print" in text or "busy" in text:
            return "printing"
        if "idle" in text or "normal" in text:
            return "idle"
        if "offline" in text or "stop" in text or "error" in text:
            return "stopped"
        return "unknown"


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
