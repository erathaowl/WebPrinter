# WebPrinter

[English](README.md) | Italiano

Applicazione web Python basata su FastAPI per caricare documenti e inviarli alle stampanti configurate sul server. L'interfaccia, attualmente in italiano, usa HTML, AlpineJS e HTMX.

## Funzionalità

- Caricamento contemporaneo di più file (fino a tre alla volta) a blocchi, con avanzamento complessivo, anteprima ed eliminazione singola. Ogni lotto supporta fino a 50 file.
- Formati accettati: `.pdf`, `.txt`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.tif`, `.tiff`, `.webp`. La stampa effettiva dipende dai formati supportati dal backend e dai filtri installati.
- Anteprima dopo il caricamento, espandibile e ridimensionabile, per immagini, PDF e testo; la visualizzazione dipende dal supporto del browser.
- Eliminazione del file caricato prima dell'invio in stampa.
- Scelta della stampante quando ne è disponibile più di una.
- Opzioni di stampa: bianco/nero (predefinito) o colori, da 1 a 99 copie (predefinito: 1), fronte/retro sul lato lungo (disattivato per impostazione predefinita).
- Rilevamento dei PDF cifrati e password separata per ciascun documento protetto.
- Impostazioni di stampa comuni all'intero lotto. Per le immagini sono disponibili **Adatta** (predefinito, conserva tutto) e **Riempi** (ritaglio centrale).
- Elaborazione in background con stato, avanzamento, dettagli degli errori e ID di coda CUPS quando disponibile.
- Monitor della stampante con stato, numero di job in coda, segnalazioni e livelli toner quando disponibili.

## Prerequisiti

- Python 3.10 o successivo e `uv` per l'avvio locale.
- Un backend di stampa configurato:
  - **Linux/macOS:** CUPS con `lp` e `lpstat` disponibili nel `PATH` e almeno una stampante configurata. Per i livelli toner servono anche `ipptool`, il file standard `get-printer-attributes.test` di CUPS e una stampante che esponga gli attributi dei consumabili via IPP.
  - **Windows:** stampante configurata nel sistema, PowerShell con i comandi di gestione stampa e SumatraPDF. L'eseguibile viene cercato nei percorsi standard di installazione oppure nel percorso indicato da `SUMATRA_PDF_PATH`.
- Browser con JavaScript abilitato e accesso a `unpkg.com`: AlpineJS e HTMX vengono caricati da CDN.

Il backend CUPS ha la precedenza quando sono disponibili sia `lp` sia `lpstat`. Su Windows, i file `.txt` vengono convertiti in PDF con ReportLab prima dell'invio a SumatraPDF. I livelli toner non sono disponibili con il backend Windows.

## Avvio locale

Dalla directory del progetto:

```bash
uv sync
uv run uvicorn app:app --reload
```

Apri <http://127.0.0.1:8000>. `--reload` è pensato per lo sviluppo; i riavvii azzerano lo stato in memoria.

Il server viene avviato tramite `app:app`: `main.py` è soltanto uno script dimostrativo e non avvia il sito. Il file `.env` è usato da Docker Compose e non viene caricato automaticamente dall'avvio locale; imposta eventuali variabili, come `SUMATRA_PDF_PATH`, nell'ambiente del processo.

## Avvio con Docker e CUPS

L'immagine pronta all'uso è pubblicata su [GitHub Container Registry](https://github.com/erathaowl/WebPrinter/pkgs/container/webprinter):

```bash
docker pull ghcr.io/erathaowl/webprinter:latest
```

Puoi utilizzare una versione specifica, ad esempio `ghcr.io/erathaowl/webprinter:v1.1`. È necessaria una stampante IPP raggiungibile dalla rete del container, compatibile con il modello driverless `everywhere` di CUPS.

### Docker Compose (consigliato)

Serve Docker con il plugin Compose.

1. Copia `.env.example` in `.env`.
2. Configura le variabili:

   | Variabile | Descrizione |
   | --- | --- |
   | `PRINTER_NAME` | Nome della coda CUPS da creare, ad esempio `Office_IPP` (obbligatorio). |
   | `PRINTER_ADDRESS` | URI della stampante, ad esempio `ipp://192.168.1.50/ipp/print` (obbligatorio). |
   | `APP_PORT` | Porta HTTP pubblicata sull'host (predefinita: `8000`). |

3. Scarica l'immagine pubblicata e avvia il servizio:

   ```bash
   docker compose pull
   docker compose up -d
   ```

Il file `compose.yml` utilizza `ghcr.io/erathaowl/webprinter:latest`. Per compilare localmente, commenta `image:`, decommenta la sezione `build:` ed esegui `docker compose up -d --build`.

### Docker CLI

In alternativa, puoi avviare l'immagine pubblicata senza Compose:

```bash
docker run -d \
  --name webprinter \
  --restart unless-stopped \
  -p 8000:8000 \
  -e PRINTER_NAME=Office_IPP \
  -e PRINTER_ADDRESS=ipp://192.168.1.50/ipp/print \
  ghcr.io/erathaowl/webprinter:latest
```

Sostituisci il nome e l'indirizzo della stampante di esempio con i tuoi valori. Con Docker CLI, modifica `-p 8000:8000` se desideri una porta diversa sull'host.

Apri <http://localhost:8000>, oppure usa la porta configurata sull'host.

Il container installa gli strumenti CUPS e i filtri, avvia `cupsd`, attende che sia pronto e configura la stampante con:

```bash
lpadmin -E -p "$PRINTER_NAME" -v "$PRINTER_ADDRESS" -m everywhere
```

Quindi tenta di abilitare la coda, accettare i job e impostarla come predefinita, e avvia Uvicorn su `0.0.0.0:8000`. Nel Compose attuale, `APP_PORT` nel file `.env` cambia solo la porta dell'host: la porta interna resta `8000`. Non sono configurati volumi persistenti per upload o configurazione CUPS.

## Utilizzo

1. Seleziona uno o più file e premi **Carica file sul server**.
2. Controlla le anteprime, elimina i singoli file se necessario oppure aggiungine altri.
3. Inserisci la password per ciascun PDF protetto.
4. Scegli la stampante e le impostazioni comuni, inclusa **Adatta / Riempi** per le immagini, quindi invia il lotto in stampa.
5. Controlla l'esito di ciascun job nel pannello di stato.

Lo stato del job viene aggiornato ogni **2 secondi**, fino al completamento o all'errore. Il monitor viene caricato all'apertura della pagina e al cambio stampante, poi aggiornato ogni **30 secondi**; il pulsante accanto alla data dell'ultimo aggiornamento consente di disattivare o riattivare il refresh automatico.

## Comportamento e limiti attuali

- **“Completato” indica che il backend ha terminato l'invio**, non che il documento sia stato fisicamente stampato. CUPS controlla la presenza del job nella coda locale per circa 12 secondi; SumatraPDF segnala l'esito del proprio processo.
- L'avanzamento di stampa rappresenta le fasi dell'invio, non il numero di pagine stampate.
- I file sono salvati in `uploads/`, creata nella directory di lavoro. Al termine dell'elaborazione dei job vengono eliminati i sorgenti e i file temporanei, inclusi i PDF decriptati.
- L'adattamento si applica solo alle immagini: vengono convertite in PDF A4 temporanei, con orientamento verticale/orizzontale automatico. **Adatta** lascia un piccolo margine bianco; **Riempi** ritaglia al centro. La stampa senza bordi fisici dipende dalla stampante e dal driver. Per i GIF animati si stampa il primo fotogramma; per i TIFF multipagina si stampano tutte le pagine.
- Gli upload abbandonati o interrotti non hanno una scadenza né una pulizia periodica automatica. Possono rimanere su disco, specialmente dopo un arresto del processo.
- Job e metadati degli upload sono conservati solo in memoria. Usa un solo processo Uvicorn, senza `--workers` multipli: i registri non sono condivisi. Il pool in background ha due thread di elaborazione.
- Non sono implementati autenticazione, autorizzazione o un limite complessivo esplicito alla dimensione dei file. Il server accetta al massimo 5.000 blocchi per upload; l'interfaccia usa blocchi da 512 KiB, 1 MiB o 2 MiB in base alla dimensione del file.
- Usa l'applicazione solo in una rete fidata. Prima di esporla su reti non fidate, predisponi controlli di accesso, HTTPS e limiti alle richieste, ad esempio tramite reverse proxy. Docker Compose pubblica la porta HTTP senza limitarla a localhost.

## Risoluzione dei problemi

- **Backend non pronto / nessuna stampante:** verifica i prerequisiti e le stampanti configurate sul server; su CUPS controlla `lpstat -r`, `lpstat -a` e `lpstat -d`.
- **Toner non disponibile:** verifica `ipptool` e il supporto IPP della stampante. L'assenza dei livelli non impedisce la stampa.
- **PDF rifiutato:** verifica che non sia corrotto e che la password sia corretta. Dopo un invio fallito può essere necessario caricare nuovamente il file.
- **Problemi nel container:** consulta `docker compose logs webprinter` e verifica nome, URI e raggiungibilità della stampante dal container.

## Licenza

Distribuito sotto licenza MIT. Vedi [LICENSE](LICENSE).
