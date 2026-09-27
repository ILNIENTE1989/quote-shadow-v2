from __future__ import annotations

import csv
import datetime as dt
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://www.cloudbet.com/sports-api/c/v6/sports"
TZ = ZoneInfo("Europe/Rome")

# Per il test tecnico raccogliamo fino a 8 ore prima.
# Nell'analisi formale useremo solo le righe con minutes_to_kickoff <= 65.
CAPTURE_WINDOW_MIN = 480

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "cloudbet"
SNAPDIR = DATA / "snapshots"
CHANGEDIR = DATA / "changes"
STATEDIR = DATA / "state"

# Chiavi esatte, niente fuzzy matching.
COMPETITIONS = [
    ("Premier League", "soccer-england-premier-league"),
    ("Serie A", "soccer-italy-serie-a"),
    ("La Liga", "soccer-spain-laliga"),
    ("Bundesliga", "soccer-germany-bundesliga"),
    ("Ligue 1", "soccer-france-ligue-1"),
    ("Championship", "soccer-england-championship"),
    ("Serie B", "soccer-italy-serie-b"),
    ("Eredivisie", "soccer-netherlands-eredivisie"),
    ("UEFA Champions League", "soccer-international-clubs-uefa-champions-league"),
    ("UEFA Europa League", "soccer-international-clubs-uefa-europa-league"),

    # Aggiunta solo per il collaudo di oggi: ha eventi in giornata.
    ("Argentina Primera B", "soccer-argentina-primera-b"),
]

MARKETS = [
    "soccer.match_odds",
    "soccer.double_chance",
    "soccer.asian_handicap",
    "soccer.total_goals",
]

SNAPSHOT_FIELDS = [
    "captured_at_local",
    "event_id",
    "competition",
    "home_team",
    "away_team",
    "kickoff_utc",
    "minutes_to_kickoff",
    "market",
    "submarket",
    "outcome",
    "params",
    "price",
]

CHANGE_FIELDS = [
    "captured_at_local",
    "event_id",
    "competition",
    "home_team",
    "away_team",
    "kickoff_utc",
    "minutes_to_kickoff",
    "market",
    "submarket",
    "outcome",
    "params",
    "old_price",
    "new_price",
]


def get_json(url: str):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_iso(value: str):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def load_state(path: Path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_csv(path: Path, fields, rows):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()

    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def events_url(comp_key: str):
    params = [
        ("include-pretrading", "true"),
        ("locale", "en"),
    ]
    for market in MARKETS:
        params.append(("markets", market))

    return (
        f"{BASE}/competitions/"
        + urllib.parse.quote(comp_key, safe="")
        + "/events?"
        + urllib.parse.urlencode(params)
    )


def main():
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone(TZ)
    today = now_local.date().isoformat()

    print("=== CLOUDBET COLLECTOR ===")
    print("Rilevazione:", now_local.isoformat())
    print("Finestra tecnica:", f"T-{CAPTURE_WINDOW_MIN} -> T0")
    print("Mercati richiesti:", ", ".join(MARKETS))

    state_path = STATEDIR / f"{today}.json"
    snap_path = SNAPDIR / f"{today}.csv"
    change_path = CHANGEDIR / f"{today}.csv"

    state = load_state(state_path)
    snapshots = []
    changes = []
    active_events = 0
    market_counts = {}

    for comp_name, comp_key in COMPETITIONS:
        try:
            payload = get_json(events_url(comp_key))
        except Exception as exc:
            print(f"SKIP {comp_name}: {repr(exc)}")
            continue

        events = payload.get("events", []) or []

        for event in events:
            if str(event.get("type") or "") != "EVENT_TYPE_EVENT":
                continue
            if str(event.get("status") or "") not in ("TRADING", "TRADING_LIVE"):
                continue

            kickoff = parse_iso(str(event.get("cutoffTime") or ""))
            if kickoff is None:
                continue

            mins = (kickoff - now_utc).total_seconds() / 60.0
            if not (0 <= mins <= CAPTURE_WINDOW_MIN):
                continue

            active_events += 1

            event_id = str(event.get("id") or "")
            home = str((event.get("home") or {}).get("name") or "")
            away = str((event.get("away") or {}).get("name") or "")

            print(f"{home} - {away} | {comp_name} | T-{mins:.1f}")

            markets = event.get("markets") or {}

            for market_key, market_obj in markets.items():
                if market_key not in MARKETS:
                    continue

                submarkets = (market_obj or {}).get("submarkets") or {}

                for sub_key, sub_obj in submarkets.items():
                    for sel in (sub_obj or {}).get("selections", []) or []:
                        price = sel.get("price")
                        if price is None:
                            continue

                        try:
                            price = float(price)
                        except Exception:
                            continue

                        outcome = str(sel.get("outcome") or "")
                        params = str(sel.get("params") or "")

                        row_base = {
                            "captured_at_local": now_local.isoformat(),
                            "event_id": event_id,
                            "competition": comp_name,
                            "home_team": home,
                            "away_team": away,
                            "kickoff_utc": kickoff.isoformat(),
                            "minutes_to_kickoff": round(mins, 3),
                            "market": market_key,
                            "submarket": str(sub_key),
                            "outcome": outcome,
                            "params": params,
                        }

                        snapshots.append({**row_base, "price": price})
                        market_counts[market_key] = market_counts.get(market_key, 0) + 1

                        state_key = "|".join([
                            event_id,
                            market_key,
                            str(sub_key),
                            outcome,
                            params,
                        ])

                        old = state.get(state_key)

                        if old is not None:
                            try:
                                old_float = float(old)
                            except Exception:
                                old_float = None

                            if old_float is not None and old_float != price:
                                changes.append({
                                    **row_base,
                                    "old_price": old_float,
                                    "new_price": price,
                                })

                        state[state_key] = price

    append_csv(snap_path, SNAPSHOT_FIELDS, snapshots)
    append_csv(change_path, CHANGE_FIELDS, changes)
    save_state(state_path, state)

    print("Eventi nella finestra:", active_events)
    print("Quote salvate:", len(snapshots))
    print("Cambi effettivi:", len(changes))
    print("Quote per mercato:", json.dumps(market_counts, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERRORE FATALE:", repr(exc), file=sys.stderr)
        raise
