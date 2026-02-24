# WebPrinter

Mini sito web in Python che permette di:

- caricare un file (`.pdf`, `.txt`, immagini principali),
- scegliere opzioni di stampa:
  - bianco/nero o colori (default: bianco/nero),
  - numero copie (default: `1`),
  - fronte/retro (default: disattivo),
- stampare PDF protetti da password (campo password opzionale nel form),
- inviare il job alla stampante locale,
- vedere feedback su avanzamento, esito ed errori.

UI realizzata con HTML + AlpineJS + HTMX.

## Prerequisiti

- Python 3.10+
- `pip`
- Backend di stampa locale:
  - Linux/macOS: CUPS (`lp`, `lpstat`) installati e configurati
  - Windows: SumatraPDF installato (oppure variabile `SUMATRA_PDF_PATH`)

## Avvio

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --reload
```

Apri `http://127.0.0.1:8000`.

Su Linux/macOS usa:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

## Avvio con Docker + CUPS

1. Crea `.env` partendo da `.env.example` e imposta:
   - `PRINTER_NAME`
   - `PRINTER_ADDRESS` (es. `ipp://host/ipp/print`)
2. Avvia:

```bash
docker compose up --build
```

Il container:

- avvia CUPS,
- aggiunge la stampante con:
  `lpadmin -E -p {printername} -v {pronteraddress} -m everywhere`
  (usando le variabili del compose),
- avvia il sito su `http://localhost:${APP_PORT}` (default `8000`).

## Note implementative

- I file vengono salvati temporaneamente in `uploads/` e cancellati al termine del job.
- Lo stato del job viene aggiornato in polling HTMX ogni 2 secondi.
- In caso di errore, il dettaglio viene mostrato direttamente nel pannello del job.
- Con backend CUPS, se disponibile, viene letto anche l'ID di coda stampa.
