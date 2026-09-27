from __future__ import annotations

import datetime as dt
import json
import re
import sys
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

BASE = "https://www.cloudbet.com/sports-api/c/v6/sports"
TZ = ZoneInfo("Europe/Rome")

TARGET_COMPETITIONS = [
    "Premier League",
    "Serie A",
    "LaLiga",
    "Bundesliga",
    "Ligue 1",
    "Championship",
    "Serie B",
    "Eredivisie",
    "Primeira Liga",
    "Champions League",
    "Europa League",
]

MARKETS = [
    "soccer.match_odds",
    "soccer.asian_handicap",
    "soccer.total_goals",
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


def norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def choose_competitions(payload: dict):
    all_comps = []

    for cat in payload.get("categories", []) or []:
        for comp in cat.get("competitions", []) or []:
            name = str(comp.get("name") or "")
            key = str(comp.get("key") or "")
            if name and key:
                all_comps.append((name, key))

    chosen = []
    used = set()

    for wanted in TARGET_COMPETITIONS:
        nw = norm(wanted)
        best = None
        best_score = -1.0

        for name, key in all_comps:
            if key in used:
                continue

            nn = norm(name)

            if nn == nw:
                score = 10.0
            elif nw in nn or nn in nw:
                score = 5.0 + SequenceMatcher(None, nw, nn).ratio()
            else:
                score = SequenceMatcher(None, nw, nn).ratio()

            if score > best_score:
                best_score = score
                best = (name, key)

        if best and best_score >= 0.62:
            chosen.append(best)
            used.add(best[1])

    return chosen


def events_url(key: str):
    params = [
        ("include-pretrading", "true"),
        ("locale", "en"),
    ]

    for market in MARKETS:
        params.append(("markets", market))

    return (
        f"{BASE}/competitions/"
        + urllib.parse.quote(key, safe="")
        + "/events?"
        + urllib.parse.urlencode(params)
    )


def safe_name(event: dict):
    home = str((event.get("home") or {}).get("name") or "")
    away = str((event.get("away") or {}).get("name") or "")
    if home or away:
        return f"{home} - {away}"
    return str(event.get("name") or "(senza nome)")


def main():
    now = dt.datetime.now(dt.timezone.utc)
    print("=== DIAGNOSTICA CLOUDBET ===")
    print("Ora UTC:", now.isoformat())
    print("Ora Italia:", now.astimezone(TZ).isoformat())
    print()

    sport = get_json(f"{BASE}/soccer")
    competitions = choose_competitions(sport)

    print("Competizioni individuate:", len(competitions))
    for name, key in competitions:
        print(f"  - {name} [{key}]")

    print("\n=== EVENTI GREZZI ===")

    total_events = 0

    for comp_name, comp_key in competitions:
        print(f"\n### {comp_name} ###")

        try:
            payload = get_json(events_url(comp_key))
        except Exception as exc:
            print("ERRORE richiesta:", repr(exc))
            continue

        print("Chiavi risposta:", sorted(payload.keys()))
        events = payload.get("events", []) or []
        print("Numero eventi:", len(events))
        total_events += len(events)

        for i, event in enumerate(events[:8], start=1):
            print(f"\nEvento {i}")
            print("  nome:", safe_name(event))
            print("  id:", event.get("id"))
            print("  type:", event.get("type"))
            print("  cutoffTime:", event.get("cutoffTime"))
            print("  startTime:", event.get("startTime"))
            print("  status:", event.get("status"))

            markets = event.get("markets") or {}
            print("  market keys:", sorted(markets.keys()))

            # Mostra un esempio di submarket/selections per capire la struttura reale.
            for market_key, market_obj in list(markets.items())[:3]:
                print("   market:", market_key)

                submarkets = (market_obj or {}).get("submarkets")
                if isinstance(submarkets, dict):
                    print("    submarket keys:", list(submarkets.keys())[:6])

                    for sub_key, sub_obj in list(submarkets.items())[:2]:
                        sels = (sub_obj or {}).get("selections", []) or []
                        print(
                            "     ",
                            sub_key,
                            "selections:",
                            [
                                {
                                    "outcome": s.get("outcome"),
                                    "params": s.get("params"),
                                    "price": s.get("price"),
                                }
                                for s in sels[:4]
                            ],
                        )
                else:
                    print("    submarkets:", type(submarkets).__name__, submarkets)

        if not events:
            print("  Nessun evento restituito per questa competizione.")

    print("\n=== FINE DIAGNOSTICA ===")
    print("Eventi totali restituiti:", total_events)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERRORE FATALE:", repr(exc), file=sys.stderr)
        raise
