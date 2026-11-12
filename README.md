# TelePort — Atlante

## Identità e pubblicazione

Il marchio approvato è **Dialogo**, integrato nella sidebar, nella hero pubblica, nelle favicon ICO (16/32/48/64 px), nell’icona Apple Touch e nelle icone del manifest. Gli asset di produzione sono in `frontend/public/`; il marchio WebP pesa circa 8 KB. `brand/social-preview.png` è l’anteprima pubblica dei link (1200 × 630).

Il dominio canonico configurato è **https://teleport.moris.dev/**. Titolo, descrizione, Open Graph, Twitter Card, robots e sitemap sono inclusi nella build. La sitemap contiene solo la home; API e documentazione hanno header `X-Robots-Tag: noindex, nofollow`. Le risorse mancanti restituiscono 404. I permessi sul server continuano a proteggere tutti i dati privati.

Dopo il deploy HTTPS sul VPS, controllare `/favicon.ico`, `/brand/social-preview.png`, `/robots.txt` e `/sitemap.xml`, quindi inviare la sitemap a Google Search Console. La configurazione DNS, il reverse proxy TLS e il deploy sul server restano operazioni di pubblicazione da eseguire sull’infrastruttura. Se cambia dominio, aggiornare `frontend/index.html`, `frontend/public/robots.txt` e `frontend/public/sitemap.xml` (e rigenerare l’immagine social).

Archivio privato di conversazioni Telegram, con React + Vite, FastAPI e analisi contestuale tramite OpenAI. Una sola installazione, un account Telegram, un amministratore. SQLite in modalità WAL conserva messaggi, sessioni, lavori e cache; i file originali non vengono modificati.

## Avvio locale

Python 3.12+ e Node 22+.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m backend.cli hash-password
```

Inserisci l'hash stampato in `ADMIN_PASSWORD_HASH` nel file `.env`, racchiuso tra apici singoli per conservare i caratteri `$` anche con Docker Compose. Se esiste già un `.env`, integralo con le nuove variabili senza sostituirlo. I vecchi `API_ID`, `API_HASH`, `PHONE_NUMBER` e `PASSWORD_USERNAME` sono riconosciuti durante configurazione e importazione. Non inserire credenziali nel frontend.

```sh
cd frontend
npm ci
npm run build
cd ..
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

Apri http://localhost:8000. Il pulsante Admin apre il login. Senza hash configurato il login è disabilitato e viene spiegato come configurarlo. In sviluppo puoi eseguire `npm run dev` nella cartella frontend: Vite inoltra le richieste `/api` al backend locale.

## Demo isolata

```sh
TELEPORT_DEMO=true TELEPORT_DATA_DIR=/tmp/teleport-demo .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

La demo genera esclusivamente dati fittizi e non contatta Telegram o OpenAI. In assenza di una password/hash impostata, la password admin demo è `teleport-demo`. Se il tuo `.env` contiene già una password admin, usa quella. La modalità demo è dichiarata nell'interfaccia; non usare la cartella dati della demo per l'archivio reale.

## Recuperare l'archivio precedente

Ferma prima l'applicazione precedente. Dopo aver preparato `.env`:

```sh
.venv/bin/python -m backend.cli init
.venv/bin/python -m backend.cli import-legacy conversations
.venv/bin/python -m backend.cli copy-session chatty.session
.venv/bin/python -m backend.cli warm-reports
```

Il precalcolo `warm-reports` prepara le statistiche prima dell'apertura delle pagine e non contatta servizi esterni. Lo stesso precalcolo viene eseguito al termine dei job di aggiornamento e analisi. L'importazione è ripetibile, non duplica i messaggi e non sovrascrive successive modifiche recuperate da Telegram. Le password `PASSWORD_USERNAME` vengono cifrate nel nuovo archivio. Le vecchie cache pickle/XZ non vengono caricate. I vecchi sentiment non vengono riutilizzati come se fossero nuove analisi contestuali.

Il CSV precedente non conserva gli ID degli autori: gli autori importati hanno identità provvisorie distinte per nome. Al primo collegamento effettivo alla chat, TelePort recupera una volta lo storico accessibile per ricostruire le identità Telegram stabili. Gli aggiornamenti successivi usano il cursore ID. Il CSV non permette di recuperare a posteriori metadati già persi, messaggi cancellati o autori omonimi prima del riallineamento.

La copia della sessione usa il backup SQLite e lascia intatta la sessione originale; non sovrascrive una destinazione esistente. In alternativa usa Ricollega account nell'admin, con telefono, codice Telegram e, se richiesta, password 2FA. La password 2FA non viene salvata. La riconnessione prepara una sessione separata e verifica che l'account sia quello originale prima di attivarla.

## Aggiornamenti e accessi

- Admin: apre tutte le chat nella sessione, mostra/copia/cambia le password condivise, importa conversazioni selezionate e avvia gli aggiornamenti.
- Ospite: accede alle chat non protette o sblocca una singola chat con la sua password. La rotazione della password revoca gli sblocchi precedenti.
- Ogni pagina dati e ogni API verifica i permessi sul server. Sessioni revocabili e con scadenza, cookie HttpOnly, CSRF e limiti sui tentativi.
- Le password chat sono cifrate con Fernet perché devono essere recuperabili. Conserva un backup separato della chiave `data/encryption.key`, oppure imposta `TELEPORT_SECRET_KEY` con una chiave Fernet stabile. Senza la chiave originale non potrai decifrare le password.
- Un solo processo applicativo gestisce la sessione Telegram e la coda persistente. Non avviare Uvicorn con più worker. L'installazione ha un blocco esclusivo contro proprietari concorrenti della sessione.
- Gli aggiornamenti leggono gli ID successivi al cursore salvato atomicamente, non differenze di conteggio. Modifiche e cancellazioni vengono recepite dagli eventi mentre il client è connesso; gli ultimi messaggi ricevono anche una riconciliazione. Le cancellazioni storiche durante lunghi periodi offline non sono garantite dalla riconciliazione recente.
- I lavori sopravvivono ai riavvii, hanno avanzamento, lease e tentativi limitati. La cache del report dipende dalla versione dati, dall'intervallo temporale e dal fuso; un cambiamento invalida i risultati coinvolti.

## Sentiment OpenAI

Imposta `OPENAI_API_KEY` sul server. Modello predefinito: `gpt-5.4-mini`, configurabile con `OPENAI_MODEL`. Usa Responses API con Structured Outputs, ragionamento basso, `store=False` e validazione di etichette, autori e riferimenti ai messaggi.

L'analisi si avvia esplicitamente dal pannello admin. `TELEPORT_ANALYSIS_LIMIT` limita i momenti di un lavoro, `TELEPORT_ANALYSIS_REQUEST_LIMIT` il numero massimo di nuove richieste per esecuzione. Una conversazione molto lunga viene divisa in porzioni senza troncare il testo, con un breve contesto sovrapposto. Le porzioni già completate vengono conservate per riprendere un lavoro interrotto. Il pannello attività riporta richieste e token; non esiste una spesa automatica all'apertura dei report o all'importazione.

Un momento termina dopo una pausa di inattività, 30 minuti per impostazione predefinita e configurabile nel file .env. Non viene spezzato artificialmente a mezzanotte. Fingerprint di contenuto e metadati preservano le analisi immutate e invalidano quelle coinvolte in nuove risposte, modifiche o cancellazioni. Modello e versione del prompt partecipano alla cache.

Risultati: positivo, neutro, negativo, misto o incerto; emozioni testuali; tono di ciascun interlocutore; riferimenti ai messaggi di supporto. Non vengono prodotti punteggi sulla salute di una relazione o diagnosi. Gli autori inviati all'API sono pseudonimizzati, ma il testo dei messaggi può comunque contenere dati personali. `store=False` non equivale a Zero Data Retention.

Documentazione ufficiale consultata l'11 novembre 2026:
- https://developers.openai.com/api/docs/models/gpt-5.4-mini
- https://developers.openai.com/api/docs/guides/structured-outputs

## Statistiche e definizioni

Panoramica, Momenti e Statistiche coprono tutte le funzioni del report precedente:

- Messaggi totali/per autore, per anno/mese/giorno, ordinamento cronologico o per volume, periodi e tre giorni più attivi.
- Media per giorno di calendario (include i giorni vuoti) e per giorno attivo (solo giorni con messaggi), dichiarate separatamente.
- Tipi di messaggio, ripartizione per autore e possibilità di escludere il tipo predominante.
- Caratteri totali, media e deviazione standard delle lunghezze, valori per autore, dieci messaggi più lunghi globali e per autore con testo e data.
- Parole ricorrenti globali e per autore, frequenze superiori a 50 e ricerca con conteggio delle occorrenze effettive, non sottostringhe.
- Frasi di due e tre parole globali/per autore, costruite con token contigui entro lo stesso messaggio e la stessa frase. Le stopword non vengono eliminate all'interno degli n-grammi, evitando di inventare frasi.
- Emoji Unicode complete (incluse sequenze ZWJ e tonalità della pelle), frequenze globali/per autore e simboli usati una sola volta.
- Distribuzioni di sentiment ed emozioni globali/per autore, calcolate sui momenti analizzati; i momenti in attesa sono indicati separatamente.
- Esplorazione per giorno, ricerca, filtro autore e messaggi paginati in ordine cronologico.

Nuove misure: mappa giorno della settimana × ora locale; conteggio/durata mediana dei momenti; avvio degli scambi; sequenza più lunga di giorni attivi; tempo mediano tra un messaggio e il successivo di un autore diverso entro lo stesso momento. Quest'ultimo è un indicatore del ritmo dello scambio, non una misura di disponibilità personale; nei gruppi non identifica necessariamente una risposta diretta.

Date archiviate in UTC e visualizzate nel fuso `TELEPORT_TIMEZONE` (Europe/Rome). Le lunghezze usano i caratteri Python Unicode, non byte; gli allegati senza didascalia hanno lunghezza zero. Un messaggio con allegato e didascalia mantiene il tipo dell'allegato. Sono gestiti archivi vuoti e chat senza testo o emoji.

## VPS

### Migrazione dal vecchio servizio systemd e NGINX

Questa versione sostituisce Streamlit con FastAPI/Uvicorn. Puoi mantenere il servizio e il virtual host esistenti, cambiando il comando di avvio e la porta del proxy. Gli esempi usano `teleport.service`, `/srv/teleport` e la porta `8000`: sostituiscili con il nome e il percorso reali. Esegui installazione e migrazione come l’utente già configurato nel servizio (`systemctl show teleport.service -p User -p WorkingDirectory -p ExecStart`), per mantenere coerenti i permessi dei dati. Se il vecchio servizio usa `RootDirectory`, `ReadWritePaths`, `ProtectHome` o altri vincoli, verifica che consentano il nuovo percorso e `data/`.

1. Prima di aggiornare, annota la revisione (`git rev-parse HEAD`) e salva la configurazione (`sudo systemctl cat teleport.service`). Ferma il vecchio processo con `sudo systemctl stop teleport.service`. Crea un backup **fuori dalla cartella del progetto**, accessibile solo al proprietario, di `.env`, `conversations/`, `chatty.session` e gli eventuali file `chatty.session-*`. Conserva anche il vecchio virtualenv per il rollback. Se esiste già un archivio nuovo, salva anche tutta `data/` a processo fermo. Non eseguire `git clean`.

2. Nella cartella del progetto aggiorna il codice e prepara un ambiente separato dal precedente. Servono Python 3.12+ (con modulo venv) e Node 22+:

   ```sh
   cd /srv/teleport
   git pull --ff-only origin main
   python3.12 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   npm --prefix frontend ci
   npm --prefix frontend run build
   ```

   Se `.venv` è già l’ambiente del vecchio servizio, conserva prima una copia o usa un nuovo nome (per esempio `.venv-v2`), aggiornando tutti i comandi successivi. Non sostituire il vecchio `.env`: integra solo le variabili nuove mostrate in `.env.example`. Imposta `TELEPORT_COOKIE_SECURE=true`, `TELEPORT_DEMO=false`, `TELEPORT_DATA_DIR=/srv/teleport/data`, `TELEGRAM_SESSION_PATH=/srv/teleport/data/telegram`. Mantieni le credenziali Telegram esistenti e configura `OPENAI_API_KEY` se vuoi l’analisi AI. Genera la password amministratore con `.venv/bin/python -m backend.cli hash-password` e inserisci l’hash in `ADMIN_PASSWORD_HASH='...'`. Proteggi `.env` con `chmod 600 .env`. Un vecchio `EnvironmentFile` o `Environment=` nel servizio ha precedenza su `.env`: aggiorna anche eventuali valori duplicati.

3. Sempre con il servizio fermo e come il suo utente, importa l’archivio:

   ```sh
   .venv/bin/python -m backend.cli init
   .venv/bin/python -m backend.cli import-legacy conversations
   .venv/bin/python -m backend.cli copy-session chatty.session
   .venv/bin/python -m backend.cli warm-reports
   ```

   `copy-session` si esegue solo se la destinazione non esiste; usa il nome reale della sessione precedente. Se non hai più la sessione, salta la copia e ricollega Telegram dall’amministrazione. I CSV restano invariati e le password chat del vecchio `.env` vengono importate. Le chat senza password sono visibili solo all’amministratore. Conserva un backup di `data/encryption.key` insieme al database.

4. Crea un override con `sudo systemctl edit teleport.service`, mantenendo l’utente del servizio esistente:

   ```ini
   [Service]
   WorkingDirectory=/srv/teleport
   ExecStart=
   ExecStart=/srv/teleport/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1
   Restart=on-failure
   RestartSec=5
   ```

   La riga `ExecStart=` vuota rimuove il comando Streamlit precedente. Il processo deve poter scrivere in `data/` e leggere `.env`. Avvia una sola istanza con un solo worker. Se il vecchio servizio ha `ExecStartPre`, `ExecStartPost`, `ExecStop` o `ExecReload` specifici di Streamlit, rimuovili o adattali nell’override. Poi:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl restart teleport.service
   sudo systemctl status teleport.service --no-pager
   curl --fail http://127.0.0.1:8000/api/health
   ```

5. Nel virtual host HTTPS già esistente per `teleport.moris.dev`, mantieni certificati e redirect HTTP→HTTPS. Sostituisci la vecchia `location /` con:

   ```nginx
   location / {
       proxy_pass http://127.0.0.1:8000;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-Proto $scheme;
       proxy_set_header X-Forwarded-For $remote_addr;
       proxy_set_header X-Real-IP $remote_addr;
       client_max_body_size 3m;
   }
   ```

   L’esempio assume NGINX direttamente esposto a Internet sullo stesso VPS: sovrascrive gli header inoltrati dal client e Uvicorn si fida solo di `127.0.0.1`. Se c’è un proxy/CDN davanti, configura prima la provenienza fidata dell’IP reale. Rimuovi eventuali vecchie location Streamlit e regole di cache per HTML/API; tutto il traffico del portale deve arrivare al nuovo backend. Non esporre la porta 8000 su Internet.

   ```sh
   sudo nginx -t
   sudo systemctl reload nginx
   curl --fail https://teleport.moris.dev/api/health
   ```

6. Verifica home, favicon e login admin via HTTPS; poi una password ospite e le chat importate. Avvia gli aggiornamenti Telegram e l’analisi solo quando sei pronto. Per diagnosticare l’avvio: `sudo journalctl -u teleport.service -n 100 --no-pager` (i log vanno tenuti privati). Per tornare alla versione precedente, ferma prima il nuovo servizio, ripristina revisione/virtualenv, configurazione systemd e upstream NGINX annotati, quindi riavvia. Non far girare le due versioni contemporaneamente sulla stessa sessione Telegram. I CSV e la sessione originale restano disponibili per il rollback.

Riferimenti: [Uvicorn dietro un reverse proxy](https://uvicorn.dev/deployment/), [override e comando ExecStart di systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html).

### Alternativa Docker Compose

```sh
docker compose up --build -d
```

Il servizio ascolta su `127.0.0.1:8000`; usa un reverse proxy HTTPS e imposta `TELEPORT_COOKIE_SECURE=true`. Inoltra Host e schema originali al processo, consentendo proxy headers solo dal proxy fidato. Non esporre Vite in produzione. Il volume `teleport-data` conserva database, chiave e sessione. Il container gira come utente non privilegiato. Non avviare una seconda copia sullo stesso archivio/sessione.

Prima di migrare sul server esegui il backup di CSV, sessione originale e `.env`. Per backup del nuovo database usa l'API backup di SQLite oppure ferma il container prima di copiare il volume; una copia del solo file SQLite durante scritture WAL può essere incompleta. Conserva database, chiave e sessione in un backup protetto. La pubblicazione sul server è un'operazione separata dall'implementazione locale.

## Verifiche

```sh
.venv/bin/python -m pytest -q
cd frontend
npm run build
```

I test usano dati sintetici e provider simulati: controllano sincronizzazione, ripresa, deduplicazione, cache, permessi, password, cronologia, statistiche e analisi contestuale. Le chiamate reali richiedono le credenziali del proprietario. L'implementazione Streamlit originale e le sue dipendenze sono conservate in `legacy/` come riferimento storico, non sono necessarie per l'app nuova.

## Personalizzazioni Atlante

In Amministrazione puoi impostare nome e immagine del proprietario. Con Gestisci, accanto a ogni chat, scegli il tuo partecipante, i colori delle voci e l’immagine della conversazione. Nelle chat singole questa immagine accompagna l’altra persona; nei gruppi resta la copertina. Le immagini accettate sono PNG, JPEG o WebP fino a 2 MB, conservate nel database e protette insieme all’archivio.

Gestisci mostra una stima indicativa dei token necessari per i momenti ancora da analizzare con il modello configurato. Include istruzioni e contesto ripetuto, separa input e output stimato e non avvia richieste OpenAI. Il modello predefinito è GPT-5.4 mini. Le conversazioni eliminate dall’interfaccia restano ripristinabili nella sezione Conversazioni rimosse; Telegram non viene modificato.

## Accesso riservato

La home pubblica non elenca conversazioni né partecipanti. Una password valida sblocca solo le conversazioni associate nella sessione corrente; le chat senza password sono riservate all’amministratore. Il limite di cinque tentativi errati in quindici minuti è condiviso per indirizzo IP tra sessioni e tra gli endpoint di sblocco; un accesso riuscito non azzera gli errori precedenti. Dietro un reverse proxy configura i proxy headers fidati: il client IP deve provenire soltanto dal proxy controllato.

I messaggi Markdown supportano formattazione, liste, link, codice e tabelle. HTML non viene eseguito e immagini remote non vengono caricate automaticamente. Amministrazione è suddivisa in Profilo, Connessioni (Telegram e analisi AI), Attività e log e Conversazioni; le impostazioni della chat si aprono in un dialog.

Le analisi elaborano i momenti dal più recente al più vecchio, mantenendo i messaggi di ogni momento in ordine cronologico. Ogni nuova analisi include un breve contesto fattuale, ricercabile nel tab Momenti insieme al testo originale. I contesti delle analisi precedenti vengono creati al successivo rilancio: il cambio di prompt li rende nuovamente eleggibili, senza inviare richieste automaticamente. I messaggi non analizzati non mostrano descrizioni sostitutive.

Le risposte mostrano autore e anteprima del messaggio originale quando disponibile nella stessa chat; riferimenti mancanti o cancellati sono indicati senza consultare altre conversazioni. Poemi presenta la top 10 con tab Tutti/partecipante e messaggi espandibili. L’eliminazione dal tab Conversazioni richiede un dialog di conferma e resta ripristinabile.

La home admin mostra i totali dell’archivio e le conversazioni recenti. La sidebar si può compattare in una colonna di immagini e icone; la preferenza resta nel browser. La ricerca dei testi e del contesto è integrata nei Momenti; il tab Messaggi è stato eliminato. L’importazione si apre in un dialog.

La pausa tra momenti è configurata soltanto dal server con `TELEPORT_MOMENT_GAP_MINUTES=30` (5–240) nel file `.env`. Dopo il riavvio il valore si applica a tutte le conversazioni e alle nuove importazioni, invalidando le statistiche delle chat che hanno cambiato soglia. I momenti vengono ricostruiti durante la preparazione delle statistiche o dell’analisi, conservando le analisi dei blocchi invariati. L’avvio dell’analisi rimane nella gestione della singola chat; Connessioni indica modello AI e configurazione.

La gestione di una chat mantiene Salva visibile e lo abilita solo con modifiche non salvate; l’accodamento dell’analisi viene confermato con una notifica di successo. La ricerca nei Momenti evidenzia la frase cercata nelle descrizioni, nei messaggi e nelle citazioni, mantenendo il Markdown e trattando i caratteri speciali come testo letterale.

La ricerca nell’importazione filtra istantaneamente nome e username nell’elenco già caricato, mantenendo le selezioni anche quando sono nascoste dal filtro. Le righe riportano ultimo messaggio, non letti, membri e stato già importata quando disponibili; questi metadati arrivano dalla stessa richiesta Telegram, senza lettura del testo dei messaggi o richieste per singola chat.
