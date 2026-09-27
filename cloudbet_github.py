from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = "https://www.cloudbet.com/sports-api/c/v6/sports"
TZ = ZoneInfo("Europe/Rome")

# Formal v2 collection window. We only need the final pre-match phase,
# with a buffer beyond the T-65 research window.
CAPTURE_WINDOW_MIN = 90
DISCOVERY_HORIZON_MIN = 720
DISCOVERY_CACHE_TTL_MIN = 120
DISCOVERY_WORKERS = 18
REQUEST_TIMEOUT = 20

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "cloudbet"
SNAPDIR = DATA / "snapshots"
CHANGEDIR = DATA / "changes"
STATEDIR = DATA / "state"
DISCOVERYDIR = DATA / "discovery"

MARKETS = [
    "soccer.match_odds",
    "soccer.double_chance",
    "soccer.asian_handicap",
    "soccer.total_goals",
]

# Discovery only needs an event list, not the full market payload.
DISCOVERY_MARKETS = ["soccer.match_odds"]

SNAPSHOT_FIELDS = [
    "captured_at_local",
    "event_id",
    "competition",
    "category",
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
    "category",
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


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


# Explicitly excluded by project rules: women, friendlies, youth, reserves.
BLOCK_TERMS = {
    # women
    "women", "womens", "woman", "ladies", "female", "feminine",
    "feminino", "feminina", "femenino", "femenina", "femminile",
    "frauen", "dames", "femmes",
    # friendlies
    "friendly", "friendlies", "amistoso", "amistosos", "amichevole",
    "amichevoli", "freundschaft",
    # youth
    "youth", "junior", "juniors", "academy", "primavera",
    "under 17", "under 18", "under 19", "under 20", "under 21",
    "under 22", "under 23", "olympic",
    # reserves
    "reserve", "reserves", "reserva", "reservas", "second team", "2nd team",
}

AGE_RE = re.compile(r"(?:^|\s)u\s*[- ]?\s*(17|18|19|20|21|22|23)(?:\s|$)", re.I)
RESERVE_SUFFIX_RE = re.compile(r"\s(?:b|ii)$", re.I)


def blocked_text(*parts: str) -> bool:
    text = normalize(" ".join(str(p or "") for p in parts))
    if any(term in text for term in BLOCK_TERMS):
        return True
    if AGE_RE.search(text):
        return True
    return False


def competition_allowed(category: str, name: str, key: str) -> bool:
    return not blocked_text(category, name, key)


def event_allowed(home: str, away: str, competition: str, category: str) -> bool:
    if blocked_text(home, away, competition, category):
        return False
    # Common explicit reserve-team naming: "Team B" / "Team II".
    if RESERVE_SUFFIX_RE.search(str(home or "").strip()):
        return False
    if RESERVE_SUFFIX_RE.search(str(away or "").strip()):
        return False
    return True


def get_json(url: str):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_iso(value: str):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


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


def events_url(comp_key: str, markets):
    params = [
        ("include-pretrading", "true"),
        ("locale", "en"),
    ]
    for market in markets:
        params.append(("markets", market))
    return (
        f"{BASE}/competitions/"
        + urllib.parse.quote(comp_key, safe="")
        + "/events?"
        + urllib.parse.urlencode(params)
    )


def all_soccer_competitions():
    payload = get_json(f"{BASE}/soccer")
    result = []
    seen = set()

    for cat in payload.get("categories", []) or []:
        category = str(cat.get("name") or cat.get("key") or "")
        for comp in cat.get("competitions", []) or []:
            name = str(comp.get("name") or "")
            key = str(comp.get("key") or "")
            if not name or not key or key in seen:
                continue
            seen.add(key)
            if competition_allowed(category, name, key):
                result.append({"category": category, "name": name, "key": key})
    return result


def discover_one(comp: dict, now_utc: dt.datetime):
    try:
        payload = get_json(events_url(comp["key"], DISCOVERY_MARKETS))
    except Exception:
        return None

    events_out = []
    for event in payload.get("events", []) or []:
        if str(event.get("type") or "") != "EVENT_TYPE_EVENT":
            continue

        kickoff = parse_iso(str(event.get("cutoffTime") or event.get("startTime") or ""))
        if kickoff is None:
            continue

        mins = (kickoff - now_utc).total_seconds() / 60.0
        if not (0 <= mins <= DISCOVERY_HORIZON_MIN):
            continue

        home = str((event.get("home") or {}).get("name") or "")
        away = str((event.get("away") or {}).get("name") or "")
        if not event_allowed(home, away, comp["name"], comp["category"]):
            continue

        events_out.append({
            "event_id": str(event.get("id") or ""),
            "home": home,
            "away": away,
            "kickoff_utc": kickoff.isoformat(),
        })

    if not events_out:
        return None

    return {
        **comp,
        "events": events_out,
    }


def refresh_discovery(now_utc: dt.datetime, cache_path: Path):
    comps = all_soccer_competitions()
    print(f"Competizioni senior/ufficiali candidate: {len(comps)}")

    active = []
    with ThreadPoolExecutor(max_workers=DISCOVERY_WORKERS) as ex:
        futures = [ex.submit(discover_one, comp, now_utc) for comp in comps]
        for fut in as_completed(futures):
            try:
                item = fut.result()
            except Exception:
                item = None
            if item:
                active.append(item)

    active.sort(key=lambda x: (x.get("category", ""), x.get("name", "")))
    cache = {
        "generated_at_utc": now_utc.isoformat(),
        "horizon_minutes": DISCOVERY_HORIZON_MIN,
        "competitions": active,
    }
    save_json(cache_path, cache)
    print(f"Competizioni con eventi nelle prossime {DISCOVERY_HORIZON_MIN} min: {len(active)}")
    print(f"Eventi scoperti: {sum(len(c.get('events', [])) for c in active)}")
    return cache


def get_discovery(now_utc: dt.datetime):
    cache_path = DISCOVERYDIR / "schedule.json"
    cache = load_json(cache_path, {})
    generated = parse_iso(str(cache.get("generated_at_utc") or ""))

    stale = generated is None or (
        (now_utc - generated).total_seconds() / 60.0 >= DISCOVERY_CACHE_TTL_MIN
    )

    if stale:
        try:
            return refresh_discovery(now_utc, cache_path)
        except Exception as exc:
            print("Discovery refresh fallita, uso eventuale cache precedente:", repr(exc))
            if cache:
                return cache
            raise
    return cache


def competitions_due_for_poll(cache: dict, now_utc: dt.datetime):
    due = []
    for comp in cache.get("competitions", []) or []:
        should_poll = False
        for event in comp.get("events", []) or []:
            kickoff = parse_iso(str(event.get("kickoff_utc") or ""))
            if kickoff is None:
                continue
            mins = (kickoff - now_utc).total_seconds() / 60.0
            if -2 <= mins <= CAPTURE_WINDOW_MIN + 4:
                should_poll = True
                break
        if should_poll:
            due.append(comp)
    return due


def fetch_competition(comp: dict):
    try:
        return comp, get_json(events_url(comp["key"], MARKETS)), None
    except Exception as exc:
        return comp, None, exc


def main():
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone(TZ)
    today = now_local.date().isoformat()

    print("=== CLOUDBET COLLECTOR ALL SENIOR OFFICIAL SOCCER ===")
    print("Rilevazione:", now_local.isoformat())
    print("Finestra raccolta:", f"T-{CAPTURE_WINDOW_MIN} -> T0")
    print("Esclusi: donne, amichevoli, giovanili, riserve")
    print("Mercati:", ", ".join(MARKETS))

    cache = get_discovery(now_utc)
    due = competitions_due_for_poll(cache, now_utc)
    print("Competizioni da interrogare ora:", len(due))

    state_path = STATEDIR / f"{today}.json"
    snap_path = SNAPDIR / f"{today}.csv"
    change_path = CHANGEDIR / f"{today}.csv"

    state = load_json(state_path, {})
    snapshots = []
    changes = []
    active_events = 0
    market_counts = {}
    event_seen = set()

    results = []
    if due:
        with ThreadPoolExecutor(max_workers=min(DISCOVERY_WORKERS, len(due))) as ex:
            futures = [ex.submit(fetch_competition, comp) for comp in due]
            for fut in as_completed(futures):
                results.append(fut.result())

    for comp, payload, error in results:
        if error is not None:
            print(f"SKIP {comp['name']}: {repr(error)}")
            continue

        for event in payload.get("events", []) or []:
            if str(event.get("type") or "") != "EVENT_TYPE_EVENT":
                continue

            status = str(event.get("status") or "")
            if status and status not in ("TRADING", "TRADING_LIVE"):
                continue

            kickoff = parse_iso(str(event.get("cutoffTime") or event.get("startTime") or ""))
            if kickoff is None:
                continue

            mins = (kickoff - now_utc).total_seconds() / 60.0
            if not (0 <= mins <= CAPTURE_WINDOW_MIN):
                continue

            event_id = str(event.get("id") or "")
            home = str((event.get("home") or {}).get("name") or "")
            away = str((event.get("away") or {}).get("name") or "")

            if not event_allowed(home, away, comp["name"], comp["category"]):
                continue
            if event_id in event_seen:
                continue
            event_seen.add(event_id)

            active_events += 1
            print(
                f"{home} - {away} | {comp['category']} / {comp['name']} | "
                f"T-{mins:.2f}"
            )

            markets = event.get("markets") or {}
            for market_key, market_obj in markets.items():
                if market_key not in MARKETS:
                    continue

                submarkets = (market_obj or {}).get("submarkets") or {}
                for sub_key, sub_obj in submarkets.items():
                    # Full-time only. If Cloudbet adds another period, we do not mix it
                    # into the same 1X2/DC/AH/O-U pre-match stream.
                    if "period=ft" not in str(sub_key):
                        continue

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
                            "competition": comp["name"],
                            "category": comp["category"],
                            "home_team": home,
                            "away_team": away,
                            "kickoff_utc": kickoff.isoformat(),
                            "minutes_to_kickoff": round(mins, 4),
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
    save_json(state_path, state)

    print("Eventi eleggibili nella finestra:", active_events)
    print("Quote salvate:", len(snapshots))
    print("Cambi effettivi:", len(changes))
    print("Quote per mercato:", json.dumps(market_counts, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERRORE FATALE:", repr(exc), file=sys.stderr)
        raise
