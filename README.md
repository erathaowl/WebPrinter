# WebPrinter

English | [Italiano](README-it.md)

A Python web application built with FastAPI for uploading documents and sending them to printers configured on the server. The interface, currently in Italian, uses HTML, AlpineJS, and HTMX.

## Features

- Upload multiple files concurrently (up to three at a time) in chunks, with overall progress and independent removal or preview. A batch supports up to 50 staged files.
- Accepted formats: `.pdf`, `.txt`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.tif`, `.tiff`, `.webp`. Actual printing depends on the formats supported by the backend and installed filters.
- Expandable, resizable post-upload preview for images, PDFs, and text; rendering depends on browser support.
- Delete an uploaded file before submitting it for printing.
- Select a printer when more than one is available.
- Print options: black and white (default) or color, 1–99 copies (default: 1), and long-edge duplex printing (off by default).
- Encrypted PDF detection and a separate password field for each protected document.
- Shared print settings for all files in a batch. Images support **Fit** (default, retain the entire image) and **Fill** (center crop).
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

The ready-to-use image is published on [GitHub Container Registry](https://github.com/erathaowl/WebPrinter/pkgs/container/webprinter):

```bash
docker pull ghcr.io/erathaowl/webprinter:latest
```

You can pin a release using a version tag, for example `ghcr.io/erathaowl/webprinter:v1.0.0`. The container requires an IPP printer reachable from its network, compatible with the CUPS `everywhere` driverless model.

### Docker Compose (recommended)

You need Docker with the Compose plugin.

1. Copy `.env.example` to `.env`.
2. Configure the variables:

   | Variable | Description |
   | --- | --- |
   | `PRINTER_NAME` | Name of the CUPS queue to create, such as `Office_IPP` (required). |
   | `PRINTER_ADDRESS` | Printer URI, such as `ipp://192.168.1.50/ipp/print` (required). |
   | `APP_PORT` | HTTP port published on the host (default: `8000`). |

3. Pull the published image and start the service:

   ```bash
   docker compose pull
   docker compose up -d
   ```

The supplied `compose.yml` uses `ghcr.io/erathaowl/webprinter:latest`. To build locally instead, comment out `image:`, uncomment the `build:` section and run `docker compose up -d --build`.

### Docker CLI

Alternatively, start the published image without Compose:

```bash
docker run -d \
  --name webprinter \
  --restart unless-stopped \
  -p 8000:8000 \
  -e PRINTER_NAME=Office_IPP \
  -e PRINTER_ADDRESS=ipp://192.168.1.50/ipp/print \
  ghcr.io/erathaowl/webprinter:latest
```

Replace the example printer name and URI with your values. For Docker CLI, change `-p 8000:8000` if a different host port is needed.

Open <http://localhost:8000>, or use the configured host port.

The container installs CUPS tools and filters, starts `cupsd`, waits for it to become ready, and configures the printer with:

```bash
lpadmin -E -p "$PRINTER_NAME" -v "$PRINTER_ADDRESS" -m everywhere
```

It then attempts to enable the queue, accept jobs, and set it as the default, and starts Uvicorn on `0.0.0.0:8000`. In the current Compose configuration, `APP_PORT` in `.env` only changes the host port: the internal port remains `8000`. No persistent volumes are configured for uploads or CUPS configuration.

## Usage

1. Select one or more files and click **Carica file sul server** (upload files).
2. Check the previews and remove individual files if needed. You can add further files to the batch.
3. Enter a password for each encrypted PDF.
4. Choose a printer and shared print settings, including **Adatta / Riempi** for images, and submit the batch.
5. Follow each document's separate print job in the status panel.

Job status refreshes every **2 seconds** until completion or failure. The monitor loads when the page opens and when the printer changes, then refreshes every **30 seconds**; the button beside the last-update timestamp toggles automatic refresh off or on.

## Current behavior and limitations

- **“Completed” means the backend has finished submitting the document**, not that it has physically printed. CUPS checks whether the job remains in the local queue for approximately 12 seconds; SumatraPDF reports its own process outcome.
- Print progress represents submission stages, not the number of pages printed.
- Files are stored in `uploads/`, created in the working directory. Accepted jobs clean up their source and temporary files, including decrypted PDFs.
- Image layout applies only to image files. Images are rendered into temporary A4 PDFs, automatically rotated to match portrait or landscape orientation. Fit leaves a small white margin; Fill crops from the center. Physical edge-to-edge printing still depends on printer margins and driver support. Animated GIFs print their first frame; multi-page TIFFs print all frames.
- Abandoned or interrupted uploads have no expiration or automatic periodic cleanup. They may remain on disk, especially after the process stops.
- Jobs and upload metadata are stored only in memory. Use a single Uvicorn process, without multiple `--workers`: registries are not shared. The background pool has two processing threads.
- Authentication, authorization, and an explicit overall file-size limit are not implemented. The server accepts at most 5,000 chunks per upload; the interface uses 512 KiB, 1 MiB, or 2 MiB chunks depending on file size.
- Use the application only on a trusted network. Before exposing it to untrusted networks, provide access controls, HTTPS, and request limits, for example through a reverse proxy. Docker Compose publishes the HTTP port without restricting it to localhost.

## Troubleshooting

- **Backend not ready / no printers:** check the requirements and printers configured on the server; for CUPS, check `lpstat -r`, `lpstat -a`, and `lpstat -d`.
- **Toner unavailable:** check `ipptool` and the printer's IPP support. Missing toner levels do not prevent printing.
- **PDF rejected:** make sure it is not corrupted and the password is correct. After a failed submission, you may need to upload the file again.
- **Container issues:** inspect `docker compose logs webprinter` and check the printer name, URI, and reachability from the container.
- **`lpadmin: Success` followed by a warning:** CUPS can report a misleading error when the printer does not support IPP Everywhere or the configured IPP URI is incorrect. The web server starts even if printer setup fails. Inspect `docker compose exec webprinter sh -c 'lpstat -t; tail -n 50 /var/log/cups/error_log'` and check the printer URI and IPP Everywhere compatibility before printing.

## License

Distributed under the MIT license. See [LICENSE](LICENSE).
