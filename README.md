# Quote Shadow v2 — raccolta gratuita T-60 → T-0

Esperimento parallelo al protocollo v1.0. **Non modifica il campione ufficiale dei 50 match.**

## Obiettivo

Per un campione giornaliero congelato prima di osservare i movimenti, raccogliere da OddsPortal tramite OddsHarvester lo storico quote di un solo bookmaker (`bet365`) per:

- 1X2
- Double Chance
- Asian Handicap (tutte le linee disponibili)
- Over/Under (tutte le linee disponibili)

Il file finale `data/ticks/YYYY-MM-DD.csv` conserva solo i cambiamenti che cadono tra **T-60 e T-0**.

## Perché due fasi

1. `plan.py` gira al mattino e salva fino a 12 match in ordine di kickoff. La selezione avviene **prima** di vedere le quote, evitando selection bias.
2. `collect.py` gira il giorno successivo. Usa `--odds-history`, quindi non deve interrogare il sito ogni 5 minuti: ricostruisce i movimenti già registrati da OddsPortal con timestamp.

## Struttura dati

- `data/plans/`: campione preregistrato del giorno.
- `data/raw/`: output JSON grezzo per audit.
- `data/ticks/`: dataset normalizzato dei tick T-60→T-0.

Campi principali del dataset tick:

`kickoff_utc, home_team, away_team, result, bookmaker, market_key, submarket, outcome, timestamp_local, timestamp_utc, minutes_to_kickoff, odds`

## Main line AH e O/U

Non viene imposta durante lo scraping. Si conservano **tutte le linee disponibili**, così la main line può essere determinata successivamente per ogni timestamp con una regola unica e verificabile, invece di indovinarla durante la raccolta.

## GitHub Actions

Il workflow `.github/workflows/odds-shadow.yml`:

- 05:00 UTC: congela il palinsesto del giorno;
- 07:30 UTC: raccoglie lo storico del giorno precedente;
- può anche essere avviato manualmente con `workflow_dispatch`.

Per mantenere il costo a zero, un repository pubblico usa i normali runner standard gratuiti di GitHub Actions. Se si usa un repository privato, valgono i minuti inclusi nel proprio piano GitHub.

## Primo test manuale

Dopo avere caricato il repository:

1. Vai in **Actions → Quote shadow v2 → Run workflow**.
2. Esegui `plan` per la data odierna.
3. Dopo che le partite sono concluse, esegui `collect` indicando la stessa data.
4. Controlla `data/ticks/YYYY-MM-DD.csv`.

## Vincoli

- Nessun CAPTCHA o protezione anti-bot viene aggirato.
- Se OddsPortal blocca il runner o cambia DOM, il workflow può fallire e va diagnosticato.
- I dati assenti non vengono inventati.
- `bet365` resta il bookmaker di riferimento; una partita senza dati bet365 rimane incompleta e non viene sostituita con un altro bookmaker.
