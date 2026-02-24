# WebPrinter

Mini sito web in Python che permette di:

- caricare un file (`.pdf`, `.txt`, immagini principali),
- scegliere opzioni di stampa:
  - bianco/nero o colori (default: bianco/nero),
  - numero copie (default: `1`),
  - fronte/retro (default: disattivo),
- stampare PDF protetti da password (campo password opzionale nel form),
- inviare il job alla stampante locale,
- monitorare stato stampante e livelli toner in un pannello dedicato asincrono,
- vedere feedback su avanzamento, esito ed errori.

UI realizzata con HTML + AlpineJS + HTMX.

## Prerequisiti

- Python 3.10+
- `uv`
- Backend di stampa locale:
  - Linux/macOS: CUPS (`lp`, `lpstat`) installati e configurati
    (`ipptool` consigliato per mostrare i livelli toner)
  - Windows: SumatraPDF installato (oppure variabile `SUMATRA_PDF_PATH`)

## Avvio

```bash
uv sync
uv run uvicorn app:app --reload
```

Apri `http://127.0.0.1:8000`.

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
- Il pannello monitor stampante viene aggiornato in polling HTMX ogni 8 secondi.
- I livelli toner sono mostrati quando la stampante li espone via IPP/CUPS.
- In caso di errore, il dettaglio viene mostrato direttamente nel pannello del job.
- Con backend CUPS, se disponibile, viene letto anche l'ID di coda stampa.
