from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent

CONFIG = {
    "bookmaker": "bet365",
    "markets": ["1x2", "double_chance", "asian_handicap", "over_under"],
    "max_matches_per_day": 12,
    "timezone": "Europe/Rome",
    "locale": "it-IT",
    "request_delay_seconds": 2.0,
    "concurrency": 1,
    "base_url": "https://www.centroquote.it",
}

META_KEYS = {
    "bookmaker_name",
    "period",
    "odds_history_data",
    "submarket_name",
    "blocked_outcomes",
}


def run(cmd: list[str]) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def local_date(offset_days: int = 0) -> str:
    tz = ZoneInfo(CONFIG["timezone"])
    return (datetime.now(tz) + timedelta(days=offset_days)).date().isoformat()


def odds_date(date_iso: str) -> str:
    return date_iso.replace("-", "")


def find_output(base: Path, suffix: str) -> Path:
    if base.exists():
        return base
    candidate = base.with_suffix(suffix)
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"Output non trovato: {base} / {candidate}")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_kickoff_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=UTC)
    except ValueError:
        return None


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-")
    return text[-100:] or "match"


def naive_local_to_utc(value: str) -> datetime | None:
    try:
        naive = datetime.fromisoformat(value)
        return naive.replace(tzinfo=ZoneInfo(CONFIG["timezone"])).astimezone(UTC)
    except Exception:
        return None


def plan(date_iso: str) -> None:
    tmp_dir = ROOT / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp_base = tmp_dir / f"links-{date_iso}"

    run([
        "oddsharvester", "upcoming",
        "-s", "football",
        "-d", odds_date(date_iso),
        "--links-only",
        "-f", "csv",
        "-o", str(tmp_base),
        "--headless",
        "--timezone", CONFIG["timezone"],
        "--locale", CONFIG["locale"],
        "--base-url", CONFIG["base_url"],
    ])

    source = find_output(tmp_base, ".csv")
    rows = read_csv(source)

    # Selezione preregistrata: kickoff, poi URL.
    # Nessuna selezione in base ai movimenti di quota.
    rows.sort(key=lambda r: (r.get("kickoff_utc") or "9999", r.get("match_link") or ""))
    rows = rows[: int(CONFIG["max_matches_per_day"])]

    out = ROOT / "data" / "plans" / f"{date_iso}.csv"
    fields = ["match_link", "sport", "league", "season", "date", "kickoff_utc"]
    normalized = [{k: r.get(k, "") for k in fields} for r in rows]
    write_csv(out, normalized, fields)
    print(f"Piano congelato: {len(normalized)} partite -> {out}")


def flatten_match(match: dict) -> list[dict[str, object]]:
    kickoff = parse_kickoff_utc(match.get("match_date"))
    if kickoff is None:
        return []

    rows: list[dict[str, object]] = []
    home = match.get("home_team") or ""
    away = match.get("away_team") or ""
    result = match.get("result") or match.get("final_score") or match.get("score") or ""

    for market_key, entries in match.items():
        if not (
            isinstance(market_key, str)
            and market_key.endswith("_market")
            and isinstance(entries, list)
        ):
            continue

        for entry in entries:
            if not isinstance(entry, dict):
                continue

            bookmaker = str(entry.get("bookmaker_name") or "")
            if bookmaker.lower() != CONFIG["bookmaker"].lower():
                continue

            outcome_labels = [k for k in entry.keys() if k not in META_KEYS]
            history_blocks = entry.get("odds_history_data") or []
            submarket = entry.get("submarket_name") or ""

            for idx, label in enumerate(outcome_labels):
                block = (
                    history_blocks[idx]
                    if idx < len(history_blocks) and isinstance(history_blocks[idx], dict)
                    else {}
                )

                points = []
                opening = block.get("opening_odds")
                if isinstance(opening, dict):
                    points.append(opening)
                points.extend(
                    p for p in (block.get("odds_history") or [])
                    if isinstance(p, dict)
                )

                seen = set()
                for p in points:
                    ts_raw = p.get("timestamp")
                    odds = p.get("odds")
                    if ts_raw is None or odds is None:
                        continue

                    ts_utc = naive_local_to_utc(str(ts_raw))
                    if ts_utc is None:
                        continue

                    minutes = (kickoff - ts_utc).total_seconds() / 60
                    if minutes < 0 or minutes > 60:
                        continue

                    key = (ts_raw, odds)
                    if key in seen:
                        continue
                    seen.add(key)

                    rows.append({
                        "kickoff_utc": kickoff.isoformat(),
                        "home_team": home,
                        "away_team": away,
                        "result": result,
                        "bookmaker": bookmaker,
                        "market_key": market_key,
                        "submarket": submarket,
                        "outcome": label,
                        "timestamp_local": ts_raw,
                        "timestamp_utc": ts_utc.isoformat(),
                        "minutes_to_kickoff": round(minutes, 3),
                        "odds": odds,
                    })

    return rows


def collect(date_iso: str) -> None:
    plan_path = ROOT / "data" / "plans" / f"{date_iso}.csv"
    if not plan_path.exists():
        raise SystemExit(f"Piano non trovato: {plan_path}")

    matches = read_csv(plan_path)
    raw_dir = ROOT / "data" / "raw" / date_iso
    raw_dir.mkdir(parents=True, exist_ok=True)
    tick_rows: list[dict[str, object]] = []

    markets = ",".join(CONFIG["markets"])

    for i, row in enumerate(matches, start=1):
        url = row.get("match_link")
        if not url:
            continue

        tmp_base = raw_dir / f"{i:02d}-{slugify(url)}"

        cmd = [
            "oddsharvester", "upcoming",
            "-s", "football",
            "--match-link", url,
            "-m", markets,
            "--period", "full_time",
            "--target-bookmaker", CONFIG["bookmaker"],
            "--odds-history",
            "--include-started",
            "--headless",
            "--timezone", CONFIG["timezone"],
            "--locale", CONFIG["locale"],
            "--base-url", CONFIG["base_url"],
            "--request-delay", str(CONFIG["request_delay_seconds"]),
            "--concurrency", str(CONFIG["concurrency"]),
            "-f", "json",
            "-o", str(tmp_base),
        ]

        try:
            run(cmd)
            out = find_output(tmp_base, ".json")
            payload = json.loads(out.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                for match in payload:
                    if isinstance(match, dict):
                        tick_rows.extend(flatten_match(match))
        except Exception as exc:
            print(f"SKIP {url}: {exc}")

    ticks_path = ROOT / "data" / "ticks" / f"{date_iso}.csv"
    fields = [
        "kickoff_utc",
        "home_team",
        "away_team",
        "result",
        "bookmaker",
        "market_key",
        "submarket",
        "outcome",
        "timestamp_local",
        "timestamp_utc",
        "minutes_to_kickoff",
        "odds",
    ]

    tick_rows.sort(
        key=lambda r: (
            r["kickoff_utc"],
            r["market_key"],
            r["submarket"],
            r["outcome"],
            r["timestamp_utc"],
        )
    )
    write_csv(ticks_path, tick_rows, fields)
    print(f"Tick T-60->T-0 salvati: {len(tick_rows)} -> {ticks_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["plan", "collect"])
    parser.add_argument("--date", help="YYYY-MM-DD")
    args = parser.parse_args()

    date_iso = args.date or local_date(0 if args.mode == "plan" else -1)

    if args.mode == "plan":
        plan(date_iso)
    else:
        collect(date_iso)


if __name__ == "__main__":
    main()
