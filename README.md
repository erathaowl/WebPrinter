# WebPrinter

English | [Italiano](README-it.md)

A Python web application built with FastAPI for uploading documents and sending them to printers configured on the server. The interface, currently in Italian, uses HTML, AlpineJS, and HTMX.

## Features

- Upload one file at a time in chunks, with a progress indicator.
- Accepted formats: `.pdf`, `.txt`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.tif`, `.tiff`, `.webp`. Actual printing depends on the formats supported by the backend and installed filters.
- Expandable, resizable post-upload preview for images, PDFs, and text; rendering depends on browser support.
- Delete an uploaded file before submitting it for printing.
- Select a printer when more than one is available.
- Print options: black and white (default) or color, 1–99 copies (default: 1), and long-edge duplex printing (off by default).
- Encrypted PDF detection and password prompt to decrypt the document before printing.
- Background processing with status, progress, error details, and a CUPS queue ID when available.
- Printer monitor showing status, queued job count, alerts, and toner levels when available.

## Requirements

- Python 3.10 or later and `uv` for local execution.
- A configured printing backend:
  - **Linux/macOS:** CUPS with `lp` and `lpstat` available in `PATH` and at least one configured printer. Toner reporting also requires `ipptool`, the standard CUPS `get-printer-attributes.test` file, and a printer exposing consumable attributes over IPP.
  - **Windows:** a printer configured in the system, PowerShell with printer management commands, and SumatraPDF. The executable is searched for in standard installation locations or at the path specified by `SUMATRA_PDF_PATH`.
- A JavaScript-enabled browser with access to `unpkg.com`: AlpineJS and HTMX are loaded from a CDN.

The CUPS backend takes precedence when both `lp` and `lpstat` are available. On Windows, `.txt` files are converted to PDF with ReportLab before being passed to SumatraPDF. Toner levels are not available with the Windows backend.

## Run locally

From the project directory:

```bash
uv sync
uv run uvicorn app:app --reload
```

Open <http://127.0.0.1:8000>. `--reload` is intended for development; restarts reset in-memory state.

Start the server using `app:app`: `main.py` is only a demonstration script and does not start the website. The `.env` file is used by Docker Compose and is not loaded automatically when running locally; set any variables, such as `SUMATRA_PDF_PATH`, in the process environment.

## Run with Docker and CUPS

You need Docker with the Compose plugin and an IPP printer reachable from the container, compatible with the CUPS `everywhere` driverless model.

1. Copy `.env.example` to `.env`.
2. Configure the variables:

   | Variable | Description |
   | --- | --- |
   | `PRINTER_NAME` | Name of the CUPS queue to create, such as `Office_IPP` (required). |
   | `PRINTER_ADDRESS` | Printer URI, such as `ipp://192.168.1.50/ipp/print` (required). |
   | `APP_PORT` | HTTP port published on the host (default: `8000`). |

3. Start the service:

   ```bash
   docker compose up --build
   ```

Open <http://localhost:8000>, or use the port configured in `APP_PORT`.

The container installs CUPS tools and filters, starts `cupsd`, waits for it to become ready, and configures the printer with:

```bash
lpadmin -E -p "$PRINTER_NAME" -v "$PRINTER_ADDRESS" -m everywhere
```

It then attempts to enable the queue, accept jobs, and set it as the default, and starts Uvicorn on `0.0.0.0:8000`. In the current Compose configuration, `APP_PORT` in `.env` only changes the host port: the internal port remains `8000`. No persistent volumes are configured for uploads or CUPS configuration.

## Usage

1. Select a file and click **Carica file sul server** (upload file to server).
2. Once the upload finishes, check the preview if needed. You can delete the file before printing it.
3. If the PDF is encrypted, enter its password in the field that appears.
4. Choose the printer and options, then click **Invia in stampa** (send to printer).
5. Check the job panel for the outcome and the monitor for printer status.

Job status refreshes every **2 seconds** until completion or failure. The monitor loads when the page opens and when the printer changes, then refreshes every **30 seconds**; the button beside the last-update timestamp toggles automatic refresh off or on.

## Current behavior and limitations

- **“Completed” means the backend has finished submitting the document**, not that it has physically printed. CUPS checks whether the job remains in the local queue for approximately 12 seconds; SumatraPDF reports its own process outcome.
- Print progress represents submission stages, not the number of pages printed.
- Files are stored in `uploads/`, which is created automatically. After job processing, the application attempts to delete them even on failure; encrypted PDFs are replaced with a temporary decrypted copy.
- Abandoned or interrupted uploads have no expiration or automatic periodic cleanup. They may remain on disk, especially after the process stops.
- Jobs and upload metadata are stored only in memory. Use a single Uvicorn process, without multiple `--workers`: registries are not shared. The background pool has two processing threads.
- Authentication, authorization, and an explicit overall file-size limit are not implemented. The server accepts at most 5,000 chunks per upload; the interface uses 512 KiB, 1 MiB, or 2 MiB chunks depending on file size.
- Use the application only on a trusted network. Before exposing it to untrusted networks, provide access controls, HTTPS, and request limits, for example through a reverse proxy. Docker Compose publishes the HTTP port without restricting it to localhost.

## Troubleshooting

- **Backend not ready / no printers:** check the requirements and printers configured on the server; for CUPS, check `lpstat -r`, `lpstat -a`, and `lpstat -d`.
- **Toner unavailable:** check `ipptool` and the printer's IPP support. Missing toner levels do not prevent printing.
- **PDF rejected:** make sure it is not corrupted and the password is correct. After a failed submission, you may need to upload the file again.
- **Container issues:** inspect `docker compose logs webprinter` and check the printer name, URI, and reachability from the container.

## License

Distributed under the MIT license. See [LICENSE](LICENSE).
