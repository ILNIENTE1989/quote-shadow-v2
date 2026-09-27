from __future__ import annotations

import csv
import json
import math
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Rome")
BASE = "https://api.sofascore.com/api/v1"
PROVIDER_ID = 1  # Bet365
MAX_MATCHES_PER_DAY = 24
WINDOW_MINUTES = 65

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.sofascore.com/",
}

KEEP_GROUPS = {
    "1x2",
    "double chance",
    "draw no bet",
    "asian handicap",
    "match goals",
}


def get_json(url: str, retries: int = 3) -> dict:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET fallita: {url}: {last}")


def fractional_to_decimal(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        if "/" in s:
            a, b = s.split("/", 1)
            return round(1.0 + float(a) / float(b), 6)
        return float(s)
    except Exception:
        return None


def current_decimal(choice: dict):
    for key in ("decimalValue", "decimal_value"):
        if key in choice:
            try:
                return float(choice[key])
            except Exception:
                pass

    value = choice.get("value")
    if isinstance(value, dict):
        for key in ("decimal", "decimalValue"):
            if key in value:
                try:
                    return float(value[key])
                except Exception:
                    pass

    for key in ("fractionalValue", "fractional_value"):
        if key in choice:
            return fractional_to_decimal(choice[key])

    return None


def initial_decimal(choice: dict):
    for key in ("initialDecimalValue", "initial_decimal_value"):
        if key in choice:
            try:
                return float(choice[key])
            except Exception:
                pass

    value = choice.get("initialValue")
    if isinstance(value, dict):
        for key in ("decimal", "decimalValue"):
            if key in value:
                try:
                    return float(value[key])
                except Exception:
                    pass

    for key in ("initialFractionalValue", "initial_fractional_value"):
        if key in choice:
            return fractional_to_decimal(choice[key])

    return None


def market_group(market: dict) -> str:
    return str(
        market.get("group")
        or market.get("marketGroup")
        or market.get("name")
        or market.get("marketName")
        or ""
    ).strip()


def market_name(market: dict) -> str:
    return str(market.get("name") or market.get("marketName") or market_group(market)).strip()


def choice_group(market: dict):
    for key in ("choiceGroup", "choice_group"):
        if key in market and market[key] not in (None, ""):
            return str(market[key])
    return ""


def infer_line(group: str, market: dict, choices: list[dict]) -> str:
    cg = choice_group(market)
    if cg:
        return cg

    if group.lower() == "asian handicap":
        nums = []
        for c in choices:
            name = str(c.get("name") or "")
            m = re.search(r"\(([+-]?\d+(?:\.\d+)?)\)", name)
            if m:
                nums.append(m.group(1))
        if nums:
            return "/".join(nums)
    return ""


def event_popularity(event: dict):
    tournament = event.get("tournament") or {}
    unique = tournament.get("uniqueTournament") or {}
    return (
        int(unique.get("userCount") or 0),
        int(event.get("homeTeam", {}).get("userCount") or 0)
        + int(event.get("awayTeam", {}).get("userCount") or 0),
    )


def ensure_plan(date_str: str) -> list[dict]:
    path = ROOT / "data" / "sofa" / "plans" / f"{date_str}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    payload = get_json(f"{BASE}/sport/football/scheduled-events/{date_str}")
    events = payload.get("events") or []

    candidates = []
    for e in events:
        if not isinstance(e, dict) or not e.get("id") or not e.get("startTimestamp"):
            continue
        status = str((e.get("status") or {}).get("type") or "").lower()
        if status in {"canceled", "cancelled", "postponed"}:
            continue
        candidates.append(e)

    # Cernita PRE-QUOTE: popolarità SofaScore, poi kickoff.
    # Non guarda quote o movimenti.
    candidates.sort(
        key=lambda e: (
            -event_popularity(e)[0],
            -event_popularity(e)[1],
            int(e.get("startTimestamp") or 0),
            int(e.get("id") or 0),
        )
    )
    candidates = candidates[:MAX_MATCHES_PER_DAY]

    plan = []
    for e in candidates:
        tournament = e.get("tournament") or {}
        unique = tournament.get("uniqueTournament") or {}
        category = tournament.get("category") or {}
        plan.append({
            "event_id": int(e["id"]),
            "kickoff_utc": datetime.fromtimestamp(
                int(e["startTimestamp"]), tz=timezone.utc
            ).isoformat(),
            "home_team": (e.get("homeTeam") or {}).get("name", ""),
            "away_team": (e.get("awayTeam") or {}).get("name", ""),
            "tournament": unique.get("name") or tournament.get("name") or "",
            "country": category.get("name") or "",
            "popularity": event_popularity(e)[0],
        })

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Piano congelato: {len(plan)} match -> {path}")
    return plan


def rows_from_odds(event: dict, captured: datetime, payload) -> list[dict]:
    markets = payload.get("markets") if isinstance(payload, dict) else payload
    if not isinstance(markets, list):
        return []

    rows = []
    for market in markets:
        if not isinstance(market, dict):
            continue

        group = market_group(market)
        if group.lower() not in KEEP_GROUPS:
            continue

        period = str(market.get("period") or "")
        if period and period.lower() not in {"full-time", "full time", "full_time"}:
            continue

        if bool(market.get("isLive") or market.get("is_live")):
            continue

        choices = market.get("choices") or []
        if not isinstance(choices, list):
            continue

        line = infer_line(group, market, choices)

        for choice in choices:
            if not isinstance(choice, dict):
                continue

            dec = current_decimal(choice)
            if dec is None:
                continue

            rows.append({
                "captured_utc": captured.astimezone(timezone.utc).isoformat(),
                "captured_local": captured.astimezone(TZ).isoformat(),
                "event_id": event["event_id"],
                "kickoff_utc": event["kickoff_utc"],
                "minutes_to_kickoff": round(
                    (
                        datetime.fromisoformat(event["kickoff_utc"]) -
                        captured.astimezone(timezone.utc)
                    ).total_seconds() / 60.0,
                    3,
                ),
                "home_team": event["home_team"],
                "away_team": event["away_team"],
                "tournament": event["tournament"],
                "provider": "bet365",
                "provider_id": PROVIDER_ID,
                "market_group": group,
                "market_name": market_name(market),
                "line": line,
                "choice": str(choice.get("name") or ""),
                "odds_decimal": dec,
                "initial_odds_decimal": initial_decimal(choice),
                "change": choice.get("change", ""),
                "suspended": bool(market.get("suspended") or market.get("isSuspended")),
            })

    return rows


FIELDS = [
    "captured_utc",
    "captured_local",
    "event_id",
    "kickoff_utc",
    "minutes_to_kickoff",
    "home_team",
    "away_team",
    "tournament",
    "provider",
    "provider_id",
    "market_group",
    "market_name",
    "line",
    "choice",
    "odds_decimal",
    "initial_odds_decimal",
    "change",
    "suspended",
]


def append_rows(date_str: str, rows: list[dict]):
    path = ROOT / "data" / "sofa" / "snapshots" / f"{date_str}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = set()
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                existing.add(
                    (
                        r.get("captured_utc"),
                        r.get("event_id"),
                        r.get("market_group"),
                        r.get("line"),
                        r.get("choice"),
                    )
                )

    fresh = []
    for r in rows:
        key = (
            str(r["captured_utc"]),
            str(r["event_id"]),
            str(r["market_group"]),
            str(r["line"]),
            str(r["choice"]),
        )
        if key not in existing:
            fresh.append(r)
            existing.add(key)

    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(fresh)

    print(f"Snapshot: {len(fresh)} righe nuove -> {path}")


def snapshot():
    now = datetime.now(timezone.utc)
    local_date = now.astimezone(TZ).date().isoformat()
    plan = ensure_plan(local_date)

    active = []
    for event in plan:
        kickoff = datetime.fromisoformat(event["kickoff_utc"])
        mins = (kickoff - now).total_seconds() / 60.0
        if 0 <= mins <= WINDOW_MINUTES:
            active.append(event)

    print(f"Match nella finestra T-{WINDOW_MINUTES}->T0: {len(active)}")
    all_rows = []

    for event in active:
        event_id = event["event_id"]
        try:
            payload = get_json(f"{BASE}/event/{event_id}/odds/{PROVIDER_ID}/all")
            rows = rows_from_odds(event, now, payload)
            print(
                f"{event['home_team']} - {event['away_team']}: "
                f"{len(rows)} righe quote"
            )
            all_rows.extend(rows)
        except Exception as exc:
            print(
                f"SKIP {event['home_team']} - {event['away_team']} "
                f"({event_id}): {exc}"
            )

    append_rows(local_date, all_rows)


if __name__ == "__main__":
    snapshot()
