#!/usr/bin/env python3
"""Check upstream bus timetable sources and update local timetable JSON.

The script is intentionally dependency-free so it can run in GitHub Actions
without package installation. It updates files only when timetable values
change, keeping scheduled runs quiet when upstream data is unchanged.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import re
import sys
import urllib.request
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
TIMETABLE_PATH = ROOT / "data" / "timetable.json"
FINGERPRINT_PATH = ROOT / "data" / "source-fingerprints.json"

JST = timezone(timedelta(hours=9))

DAY_TYPES = ("weekday", "saturday", "holiday")

JHB_SOURCES = (
    {
        "key": "jhb_atsubetsu_to_shinsapporo_weekday",
        "route_id": "jr-atsubetsu-shinsapporo",
        "day_types": ("weekday",),
        "url": "http://www.busdia.com/mobileTimeTable/timeOfPassage/?tid=0823012201",
    },
    {
        "key": "jhb_atsubetsu_to_shinsapporo_holiday",
        "route_id": "jr-atsubetsu-shinsapporo",
        "day_types": ("saturday", "holiday"),
        "url": "http://www.busdia.com/mobileTimeTable/timeOfPassage/?tid=0823012202",
    },
    {
        "key": "jhb_onopporo_to_shinsapporo_weekday",
        "route_id": "jr-onopporo-shinsapporo",
        "day_types": ("weekday",),
        "url": "http://www.busdia.com/mobileTimeTable/timeOfPassage/?tid=0592012201",
    },
    {
        "key": "jhb_onopporo_to_shinsapporo_holiday",
        "route_id": "jr-onopporo-shinsapporo",
        "day_types": ("saturday", "holiday"),
        "url": "http://www.busdia.com/mobileTimeTable/timeOfPassage/?tid=0592012202",
    },
)

YUTETSU_GTFS_URL = (
    "https://ckan.hoda.jp/dataset/24d1dd70-5395-4d6b-b41f-0d83e8eabdb9/"
    "resource/594c9dd3-d356-4445-98e2-c480d49c9d65/download/yutetsu_bus.zip"
)
YUTETSU_ROUTE_ID = "yutetsu-atsubetsu-shinsapporo"
YUTETSU_ORIGIN_STOP = "\u539a\u5225\u6771\uff14\u6761\uff14\u4e01\u76ee"
YUTETSU_DESTINATION_STOP = "\u65b0\u3055\u3063\u307d\u308d\u99c5\u524d"
YUTETSU_ROUTE_NAMES = {
    "\u672d\u5e4c\u7dda",
    "\u672d\u5e4c\u4ee3\u884c\u7dda",
}


@dataclass(frozen=True)
class SourceResult:
    key: str
    url: str
    content_hash: str
    times_by_route: dict[str, dict[str, list[str]]]
    metadata: dict


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fetch_bytes(url: str, timeout: int = 45) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; timetable-update-checker/1.0; "
                "+https://github.com/)"
            )
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def decode_source(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp932", "shift_jis", "euc_jp"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def normalize_digits(text: str) -> str:
    return text.translate(str.maketrans("０１２３４５６７８９：", "0123456789:"))


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", fragment, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_digits(html.unescape(text))


def normalize_time(hour: str, minute: str) -> str | None:
    h = int(hour)
    m = int(minute)
    if not (0 <= h <= 29 and 0 <= m <= 59):
        return None
    if h >= 24:
        return None
    return f"{h:02d}:{m:02d}"


def sort_times(times: Iterable[str]) -> list[str]:
    return sorted(set(times), key=lambda value: int(value[:2]) * 60 + int(value[3:]))


def parse_jhb_times(page: str) -> list[str]:
    times: list[str] = []

    # Common table shape: hour cell followed by minute cells.
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page, flags=re.IGNORECASE | re.DOTALL):
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, flags=re.IGNORECASE | re.DOTALL)
        if len(cells) < 2:
            continue
        hour_text = strip_tags(cells[0]).strip()
        hour_match = re.match(r"^([0-2]?\d)\s*(?:\u6642)?$", hour_text)
        if not hour_match:
            continue
        hour = hour_match.group(1)
        minute_text = " ".join(strip_tags(cell) for cell in cells[1:])
        for minute in re.findall(r"(?<!\d)([0-5]\d)(?!\d)", minute_text):
            normalized = normalize_time(hour, minute)
            if normalized:
                times.append(normalized)

    # JR Busdia mobile pages commonly render rows as:
    #   06時<span> 43</span><span> 57</span><br>
    if len(times) < 3:
        normalized_page = normalize_digits(page)
        for hour, minute_fragment in re.findall(
            r"(?m)^\s*([0-2]?\d)\s*\u6642(.*?)(?:<br\s*/?>)",
            normalized_page,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            minute_text = strip_tags(minute_fragment)
            for minute in re.findall(r"(?<!\d)([0-5]\d)(?!\d)", minute_text):
                normalized = normalize_time(hour, minute)
                if normalized:
                    times.append(normalized)

    # Fallback for pages that render complete HH:MM strings.
    if len(times) < 3:
        text = normalize_digits(strip_tags(page))
        for hour, minute in re.findall(r"(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)", text):
            normalized = normalize_time(hour, minute)
            if normalized:
                times.append(normalized)

    result = sort_times(times)
    if len(result) < 3:
        raise ValueError("Could not parse enough JR timetable times from the source page.")
    return result


def collect_jhb_sources() -> list[SourceResult]:
    results: list[SourceResult] = []
    for source in JHB_SOURCES:
        payload = fetch_bytes(source["url"])
        page = decode_source(payload)
        times = parse_jhb_times(page)
        by_day = {day_type: times for day_type in source["day_types"]}
        results.append(
            SourceResult(
                key=source["key"],
                url=source["url"],
                content_hash=sha256_bytes(payload),
                times_by_route={source["route_id"]: by_day},
                metadata={"timesHash": stable_hash(by_day), "times": by_day},
            )
        )
    return results


def read_gtfs_csv(zip_file: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    with zip_file.open(name) as handle:
        rows = (line.decode("utf-8-sig") for line in handle)
        return list(csv.DictReader(rows))


def parse_gtfs_time(value: str) -> tuple[int, str] | None:
    match = re.match(r"^(\d{1,2}):([0-5]\d):[0-5]\d$", value)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour >= 24:
        return None
    return hour * 60 + minute, f"{hour:02d}:{minute:02d}"


def service_matches_day(calendar_row: dict[str, str], day_type: str) -> bool:
    if day_type == "weekday":
        return any(calendar_row.get(day) == "1" for day in ("monday", "tuesday", "wednesday", "thursday", "friday"))
    if day_type == "saturday":
        return calendar_row.get("saturday") == "1"
    if day_type == "holiday":
        return calendar_row.get("sunday") == "1"
    return False


def parse_yutetsu_gtfs(payload: bytes) -> tuple[dict[str, list[str]], dict]:
    with zipfile.ZipFile(io.BytesIO(payload)) as zip_file:
        stops = {row["stop_id"]: row["stop_name"] for row in read_gtfs_csv(zip_file, "stops.txt")}
        routes = {row["route_id"]: row for row in read_gtfs_csv(zip_file, "routes.txt")}
        trips = {row["trip_id"]: row for row in read_gtfs_csv(zip_file, "trips.txt")}
        calendars = {row["service_id"]: row for row in read_gtfs_csv(zip_file, "calendar.txt")}
        feed_info_rows = read_gtfs_csv(zip_file, "feed_info.txt") if "feed_info.txt" in zip_file.namelist() else []

        origin_stop_ids = {stop_id for stop_id, name in stops.items() if name == YUTETSU_ORIGIN_STOP}
        destination_stop_ids = {stop_id for stop_id, name in stops.items() if name == YUTETSU_DESTINATION_STOP}
        if not origin_stop_ids:
            raise ValueError("Could not find Yutetsu origin stop in GTFS.")
        if not destination_stop_ids:
            raise ValueError("Could not find Yutetsu destination stop in GTFS.")

        stop_times_by_trip: dict[str, list[dict[str, str]]] = {}
        for row in read_gtfs_csv(zip_file, "stop_times.txt"):
            stop_times_by_trip.setdefault(row["trip_id"], []).append(row)

    result: dict[str, list[str]] = {day_type: [] for day_type in DAY_TYPES}
    for trip_id, stop_times in stop_times_by_trip.items():
        trip = trips.get(trip_id)
        if not trip:
            continue
        route = routes.get(trip.get("route_id", ""))
        if not route or route.get("route_long_name") not in YUTETSU_ROUTE_NAMES:
            continue
        service = calendars.get(trip.get("service_id", ""))
        if not service:
            continue

        ordered = sorted(stop_times, key=lambda row: int(row["stop_sequence"]))
        origin_candidates = [
            (index, row)
            for index, row in enumerate(ordered)
            if row["stop_id"] in origin_stop_ids
        ]
        destination_candidates = [
            (index, row)
            for index, row in enumerate(ordered)
            if row["stop_id"] in destination_stop_ids
        ]
        if not origin_candidates or not destination_candidates:
            continue

        origin_index, origin_row = origin_candidates[0]
        has_destination_after_origin = any(index > origin_index for index, _ in destination_candidates)
        if not has_destination_after_origin:
            continue

        parsed_time = parse_gtfs_time(origin_row["departure_time"])
        if not parsed_time:
            continue
        _, time_text = parsed_time
        for day_type in DAY_TYPES:
            if service_matches_day(service, day_type):
                result[day_type].append(time_text)

    final = {day_type: sort_times(result[day_type]) for day_type in DAY_TYPES}
    if not all(final[day_type] for day_type in DAY_TYPES):
        raise ValueError("Could not parse complete Yutetsu timetable from GTFS.")
    return final, (feed_info_rows[0] if feed_info_rows else {})


def collect_yutetsu_source(gtfs_zip: Path | None = None) -> SourceResult:
    if gtfs_zip:
        payload = gtfs_zip.read_bytes()
        url = str(gtfs_zip)
    else:
        payload = fetch_bytes(YUTETSU_GTFS_URL)
        url = YUTETSU_GTFS_URL
    times, feed_info = parse_yutetsu_gtfs(payload)
    return SourceResult(
        key="yutetsu_gtfs",
        url=url,
        content_hash=sha256_bytes(payload),
        times_by_route={YUTETSU_ROUTE_ID: times},
        metadata={
            "feedInfo": feed_info,
            "timesHash": stable_hash(times),
            "times": times,
        },
    )


def route_index(timetable: dict) -> dict[str, dict]:
    return {route["id"]: route for route in timetable.get("routes", [])}


def apply_results(timetable: dict, results: list[SourceResult]) -> tuple[dict, list[str]]:
    updated = deepcopy(timetable)
    routes = route_index(updated)
    changes: list[str] = []

    for result in results:
        for route_id, by_day in result.times_by_route.items():
            if route_id not in routes:
                raise KeyError(f"Route {route_id} was not found in timetable.json")
            route = routes[route_id]
            route.setdefault("timetables", {})
            for day_type, new_times in by_day.items():
                old_times = route["timetables"].get(day_type, [])
                if old_times != new_times:
                    route["timetables"][day_type] = new_times
                    changes.append(
                        f"{route_id}:{day_type} {len(old_times)} -> {len(new_times)} departures"
                    )

    return updated, changes


def update_metadata(timetable: dict, today: str) -> None:
    metadata = timetable.setdefault("metadata", {})
    metadata["updatedAt"] = today
    metadata["version"] = f"auto-{today}-shinsapporo-bound"


def update_fingerprints(fingerprints: dict, results: list[SourceResult], checked_at: str) -> dict:
    updated = deepcopy(fingerprints)
    updated["checkedAt"] = checked_at
    sources = updated.setdefault("sources", {})
    for result in results:
        previous = sources.get(result.key, {})
        merged = {
            **previous,
            "url": result.url,
            "contentHash": result.content_hash,
            "checkedAt": checked_at,
            **result.metadata,
        }
        sources[result.key] = merged
    return updated


def format_report(changes: list[str], checked_at: str, results: list[SourceResult]) -> str:
    lines = [
        "# Timetable update check",
        "",
        f"- Checked at: {checked_at}",
        f"- Sources checked: {len(results)}",
        "",
    ]
    if changes:
        lines.append("## Detected timetable changes")
        lines.extend(f"- {change}" for change in changes)
    else:
        lines.append("No timetable changes detected.")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check and update bus timetable data.")
    parser.add_argument("--write", action="store_true", help="Write updated JSON files when times changed.")
    parser.add_argument("--skip-jhb", action="store_true", help="Skip JR Hokkaido Bus sources.")
    parser.add_argument("--skip-yutetsu", action="store_true", help="Skip Yutetsu GTFS source.")
    parser.add_argument("--gtfs-zip", type=Path, help="Use a local Yutetsu GTFS zip instead of downloading it.")
    parser.add_argument("--today", help="Override today's date in YYYY-MM-DD form.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    checked_at = datetime.now(JST).isoformat(timespec="seconds")
    today = args.today or datetime.now(JST).date().isoformat()

    timetable = read_json(TIMETABLE_PATH)
    fingerprints = read_json(FINGERPRINT_PATH)

    results: list[SourceResult] = []
    if not args.skip_jhb:
        results.extend(collect_jhb_sources())
    if not args.skip_yutetsu:
        results.append(collect_yutetsu_source(args.gtfs_zip))
    if not results:
        raise SystemExit("No sources selected.")

    updated_timetable, changes = apply_results(timetable, results)
    if changes and args.write:
        update_metadata(updated_timetable, today)
        write_json(TIMETABLE_PATH, updated_timetable)
        updated_fingerprints = update_fingerprints(fingerprints, results, checked_at)
        write_json(FINGERPRINT_PATH, updated_fingerprints)

    sys.stdout.write(format_report(changes, checked_at, results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
