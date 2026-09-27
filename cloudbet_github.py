from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://www.cloudbet.com/sports-api/c/v6/sports"
TZ = ZoneInfo("Europe/Rome")
WINDOW_MIN = 65

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "cloudbet"
SNAPDIR = DATA / "snapshots"
CHANGEDIR = DATA / "changes"
STATEDIR = DATA / "state"

# Cernita tecnica iniziale, indipendente dalle quote.
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


def norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def choose_competitions(sport_payload: dict):
    all_comps = []
    for cat in sport_payload.get("categories", []) or []:
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
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerows(rows)


def competition_events_url(key: str):
    q = [
        ("include-pretrading", "true"),
        ("locale", "en"),
    ]
    for market in MARKETS:
        q.append(("markets", market))
    return (
        f"{BASE}/competitions/{urllib.parse.quote(key, safe='')}/events?"
        + urllib.parse.urlencode(q)
    )


def main():
    now = dt.datetime.now(dt.timezone.utc)
    local_now = now.astimezone(TZ)
    today = local_now.date().isoformat()

    print(f"Rilevazione Cloudbet: {local_now.isoformat()}")
    print("Mercati:", ", ".join(MARKETS))

    sport = get_json(f"{BASE}/soccer")
    comps = choose_competitions(sport)

    print(f"Competizioni individuate: {len(comps)}")
    for name, key in comps:
        print(f"  - {name} [{key}]")

    state_path = STATEDIR / f"{today}.json"
    snap_path = SNAPDIR / f"{today}.csv"
    change_path = CHANGEDIR / f"{today}.csv"

    state = load_state(state_path)
    snapshots = []
    changes = []
    active_events = 0

    for comp_name, comp_key in comps:
        try:
            payload = get_json(competition_events_url(comp_key))
        except Exception as exc:
            print(f"SKIP {comp_name}: {exc}")
            continue

        for event in payload.get("events", []) or []:
            if str(event.get("type") or "") != "EVENT_TYPE_EVENT":
                continue

            kickoff = parse_iso(str(event.get("cutoffTime") or ""))
            if kickoff is None:
                continue

            mins = (kickoff - now).total_seconds() / 60.0
            if not (0 <= mins <= WINDOW_MIN):
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
                        outcome = str(sel.get("outcome") or "")
                        params = str(sel.get("params") or "")
                        price = sel.get("price")
                        if price is None:
                            continue

                        try:
                            price = float(price)
                        except Exception:
                            continue

                        row_base = {
                            "captured_at_local": local_now.isoformat(),
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

                        state_key = "|".join([
                            event_id,
                            market_key,
                            str(sub_key),
                            outcome,
                            params,
                        ])
                        old = state.get(state_key)

                        if old is not None and float(old) != price:
                            changes.append({
                                **row_base,
                                "old_price": old,
                                "new_price": price,
                            })

                        state[state_key] = price

    append_csv(snap_path, SNAPSHOT_FIELDS, snapshots)
    append_csv(change_path, CHANGE_FIELDS, changes)
    save_state(state_path, state)

    print(f"Eventi nella finestra T-{WINDOW_MIN}->T0: {active_events}")
    print(f"Quote salvate: {len(snapshots)}")
    print(f"Cambi effettivi: {len(changes)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRORE: {exc}", file=sys.stderr)
        raise
