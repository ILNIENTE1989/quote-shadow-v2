from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://apiv3.apifootball.com/"
TZ = ZoneInfo("Europe/Rome")
BOOKMAKER = "Bet365"
WINDOW_START_MIN = 65
WINDOW_END_MIN = 0

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "apifootball"
SNAPDIR = DATA / "snapshots"
CHANGEDIR = DATA / "changes"
STATEDIR = DATA / "state"

META_FIELDS = {"match_id", "odd_bookmakers", "odd_date"}

SNAPSHOT_FIELDS = [
    "captured_at_local",
    "source_odd_date",
    "match_id",
    "home_team",
    "away_team",
    "kickoff_local",
    "minutes_to_kickoff",
    "bookmaker",
    "market_key",
    "odds",
]

CHANGE_FIELDS = [
    "captured_at_local",
    "source_odd_date",
    "match_id",
    "home_team",
    "away_team",
    "kickoff_local",
    "minutes_to_kickoff",
    "bookmaker",
    "market_key",
    "old_odds",
    "new_odds",
]


def api_get(api_key: str, **params):
    params["APIkey"] = api_key
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8")
    data = json.loads(body)

    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"]))

    return data


def parse_match_datetime(row: dict):
    date = row.get("match_date")
    clock = row.get("match_time")
    if not date or not clock:
        return None

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = dt.datetime.strptime(f"{date} {clock}", fmt)
            return naive.replace(tzinfo=TZ)
        except ValueError:
            pass

    return None


def is_odds_field(key, value):
    if key in META_FIELDS or value in (None, ""):
        return False

    # Escludiamo BTS. Manteniamo 1X2, DC, AH e O/U.
    if key.startswith("bts_"):
        return False

    return (
        key in {"odd_1", "odd_x", "odd_2", "odd_1x", "odd_12", "odd_x2"}
        or key.startswith("ah")
        or key.startswith("o+")
        or key.startswith("u+")
    )


def normalized_payload(row: dict):
    return {k: str(v) for k, v in row.items() if is_odds_field(k, v)}


def read_state(path: Path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_csv(path: Path, fields: list[str], rows: list[dict]):
    if not rows:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()

    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerows(rows)


def main():
    api_key = os.environ.get("APIFOOTBALL_KEY", "").strip()
    if not api_key:
        raise SystemExit("ERRORE: secret APIFOOTBALL_KEY mancante.")

    now = dt.datetime.now(TZ)
    today = now.date().isoformat()

    print(f"Rilevazione: {now.isoformat()}")
    print(f"Bookmaker: {BOOKMAKER}")
    print(f"Finestra: T-{WINDOW_START_MIN} -> T0")

    fixtures = api_get(
        api_key,
        action="get_events",
        **{
            "from": today,
            "to": today,
            "timezone": "Europe/Rome",
        },
    )

    odds = api_get(
        api_key,
        action="get_odds",
        **{
            "from": today,
            "to": today,
        },
    )

    fixture_by_id = {}
    for f in fixtures if isinstance(fixtures, list) else []:
        mid = str(f.get("match_id") or "")
        if mid:
            fixture_by_id[mid] = f

    active = {}
    for mid, f in fixture_by_id.items():
        kickoff = parse_match_datetime(f)
        if kickoff is None:
            continue

        mins = (kickoff - now).total_seconds() / 60.0
        if WINDOW_END_MIN <= mins <= WINDOW_START_MIN:
            active[mid] = (f, kickoff, mins)

    print(f"Match nella finestra: {len(active)}")

    odds_by_match = {}
    available_bookies = set()

    for row in odds if isinstance(odds, list) else []:
        book = str(row.get("odd_bookmakers") or "").strip()
        if book:
            available_bookies.add(book)

        if book.lower() != BOOKMAKER.lower():
            continue

        mid = str(row.get("match_id") or "")
        if mid in active:
            odds_by_match[mid] = row

    snap_path = SNAPDIR / f"{today}.csv"
    change_path = CHANGEDIR / f"{today}.csv"
    state_path = STATEDIR / f"{today}.json"

    state = read_state(state_path)
    snapshot_rows = []
    change_rows = []

    for mid, (f, kickoff, mins) in sorted(
        active.items(),
        key=lambda item: item[1][1]
    ):
        home = str(f.get("match_hometeam_name") or "")
        away = str(f.get("match_awayteam_name") or "")

        row = odds_by_match.get(mid)
        if row is None:
            print(
                f"{home} - {away} | T-{mins:.1f} | "
                f"nessuna quota {BOOKMAKER}"
            )
            continue

        payload = normalized_payload(row)
        source_date = str(row.get("odd_date") or "")

        if not payload:
            print(f"{home} - {away} | T-{mins:.1f} | mercati vuoti")
            continue

        print(
            f"{home} - {away} | T-{mins:.1f} | "
            f"{len(payload)} quote | source={source_date}"
        )

        for market_key, odd in sorted(payload.items()):
            base = {
                "captured_at_local": now.isoformat(),
                "source_odd_date": source_date,
                "match_id": mid,
                "home_team": home,
                "away_team": away,
                "kickoff_local": kickoff.isoformat(),
                "minutes_to_kickoff": round(mins, 3),
                "bookmaker": BOOKMAKER,
                "market_key": market_key,
            }

            snapshot_rows.append({
                **base,
                "odds": odd,
            })

            state_key = f"{mid}|{BOOKMAKER}|{market_key}"
            old = state.get(state_key)

            if old is not None and old != odd:
                change_rows.append({
                    **base,
                    "old_odds": old,
                    "new_odds": odd,
                })

            state[state_key] = odd

    append_csv(snap_path, SNAPSHOT_FIELDS, snapshot_rows)
    append_csv(change_path, CHANGE_FIELDS, change_rows)
    write_state(state_path, state)

    print(f"Snapshot salvati: {len(snapshot_rows)}")
    print(f"Cambi effettivi: {len(change_rows)}")

    if active and not odds_by_match:
        print(
            "ATTENZIONE: ci sono match nella finestra ma nessuna quota Bet365."
        )
        if available_bookies:
            sample = sorted(available_bookies)[:25]
            print("Bookmaker disponibili nell'endpoint:", ", ".join(sample))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERRORE: {exc}", file=sys.stderr)
        raise
