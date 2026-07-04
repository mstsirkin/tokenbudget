#!/usr/bin/env python3
"""Summarize Cursor Agent usage from Cursor's dashboard CSV export.

Unlike the Claude script, this one uses Cursor's own server-side CSV export,
which already includes per-event token columns and a reported cost field.
"""

from __future__ import annotations

import argparse
import base64
import csv
import dateparser
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

USAGE_CSV_URL = "https://cursor.com/api/dashboard/export-usage-events-csv?strategy=tokens"


@dataclass
class UsageRow:
    timestamp: datetime
    kind: str
    model: str
    max_mode: str
    input_with_cache_write: int
    input_without_cache_write: int
    cache_read: int
    output_tokens: int
    total_tokens: int
    cost_usd: Decimal


def get_default_auth_path() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        appdata = Path.home() / "AppData" / "Roaming"
        if "APPDATA" in os.environ:
            appdata = Path(os.environ["APPDATA"])
        return appdata / "Cursor" / "auth.json"
    if sys.platform == "darwin":
        return home / ".cursor" / "auth.json"
    config_home = Path.home() / ".config"
    if "XDG_CONFIG_HOME" in os.environ:
        config_home = Path(os.environ["XDG_CONFIG_HOME"])
    return config_home / "cursor" / "auth.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Cursor Agent usage CSV from cursor.com and summarize total "
            "tokens plus reported cost."
        )
    )
    parser.add_argument(
        "--auth-file",
        type=Path,
        default=get_default_auth_path(),
        help="Path to Cursor auth.json (default: platform-specific Cursor auth file)",
    )
    parser.add_argument(
        "--csv-file",
        type=Path,
        help="Read a previously saved usage CSV instead of fetching from cursor.com.",
    )
    parser.add_argument(
        "--save-csv",
        type=Path,
        help="Save the fetched CSV to this path.",
    )
    parser.add_argument(
        "--since",
        type=str,
        help="Only include rows on or after this ISO-8601 timestamp/date or natural-language date expression. Date-only values start at 00:00:00 UTC.",
    )
    parser.add_argument(
        "--until",
        type=str,
        help="Only include rows on or before this ISO-8601 timestamp/date or natural-language date expression. Date-only values include the full UTC day.",
    )
    parser.add_argument(
        "--kind",
        action="append",
        default=[],
        help="Only include matching Kind values. Can be passed multiple times.",
    )
    parser.add_argument(
        "--model-contains",
        action="append",
        default=[],
        help="Only include models containing this case-insensitive substring. Can be passed multiple times.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="How many models to show in the text report (default: 15).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a text report.",
    )
    return parser.parse_args()


def parse_csv_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_when(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    keyword_dt = parse_relative_day_keyword(text, end_of_day=end_of_day)
    if keyword_dt is not None:
        return keyword_dt

    try:
        return parse_iso_value(text, end_of_day=end_of_day)
    except ValueError:
        pass

    dt = dateparser.parse(
        text,
        settings={
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": "UTC",
            "TO_TIMEZONE": "UTC",
        },
    )
    if dt is None:
        raise ValueError(f"could not parse {value!r} as a date/time")
    return dt.astimezone(UTC)


def parse_relative_day_keyword(text: str, *, end_of_day: bool) -> datetime | None:
    offsets = {
        "today": 0,
        "yesterday": -1,
        "tomorrow": 1,
    }
    offset = offsets.get(" ".join(text.lower().split()))
    if offset is None:
        return None
    day = (datetime.now(tz=UTC) + timedelta(days=offset)).date()
    return datetime.combine(day, time.max if end_of_day else time.min, tzinfo=UTC)


def parse_iso_value(text: str, *, end_of_day: bool) -> datetime:
    normalized = text
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if "T" not in normalized and " " not in normalized:
        normalized = normalized + (
            "T23:59:59.999999+00:00" if end_of_day else "T00:00:00+00:00"
        )
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def intish(value: str | None) -> int:
    if value is None:
        return 0
    text = value.strip().replace(",", "")
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return 0


def decimalish(value: str | None) -> Decimal:
    if value is None:
        return Decimal("0")
    text = value.strip().replace(",", "").replace("$", "")
    if not text:
        return Decimal("0")
    lowered = text.lower()
    if lowered in {"free", "n/a", "na", "none", "null"}:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def decode_jwt_payload(jwt: str) -> dict[str, Any] | None:
    parts = jwt.split(".")
    if len(parts) != 3 or not parts[1]:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
        parsed = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def build_session_cookie(access_token: str) -> str:
    payload = decode_jwt_payload(access_token)
    if not payload or not isinstance(payload.get("sub"), str) or not payload["sub"]:
        raise RuntimeError("failed to decode Cursor access token subject")
    sub = payload["sub"]
    return f"WorkosCursorSessionToken={urllib.parse.quote(sub)}%3A%3A{access_token}"


def read_access_token(auth_file: Path) -> str:
    try:
        data = json.loads(auth_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Cursor auth file not found: {auth_file}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cursor auth file is not valid JSON: {auth_file}") from exc
    token = data.get("accessToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"Cursor auth file does not contain a usable accessToken: {auth_file}")
    return token


def fetch_csv_text(auth_file: Path) -> str:
    access_token = read_access_token(auth_file)
    cookie = build_session_cookie(access_token)
    request = urllib.request.Request(
        USAGE_CSV_URL,
        headers={
            "Cookie": cookie,
            "User-Agent": "cursor-agent-usage-costs/1.0",
            "Accept": "text/csv,*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Cursor CSV export failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"failed to fetch Cursor CSV export: {exc}") from exc


def load_csv_text(args: argparse.Namespace) -> str:
    if args.csv_file:
        return args.csv_file.read_text(encoding="utf-8")
    text = fetch_csv_text(args.auth_file)
    if args.save_csv:
        args.save_csv.parent.mkdir(parents=True, exist_ok=True)
        args.save_csv.write_text(text, encoding="utf-8")
    return text


def parse_csv_rows(csv_text: str) -> list[UsageRow]:
    rows: list[UsageRow] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for raw in reader:
        timestamp = parse_csv_timestamp(raw.get("Date", ""))
        if timestamp is None:
            continue
        row = UsageRow(
            timestamp=timestamp,
            kind=(raw.get("Kind") or "").strip() or "<unknown>",
            model=(raw.get("Model") or "").strip() or "<unknown>",
            max_mode=(raw.get("Max Mode") or "").strip(),
            input_with_cache_write=intish(raw.get("Input (w/ Cache Write)")),
            input_without_cache_write=intish(raw.get("Input (w/o Cache Write)")),
            cache_read=intish(raw.get("Cache Read")),
            output_tokens=intish(raw.get("Output Tokens")),
            total_tokens=intish(raw.get("Total Tokens")),
            cost_usd=decimalish(raw.get("Cost")),
        )
        rows.append(row)
    rows.sort(key=lambda row: row.timestamp)
    return rows


def filter_rows(rows: list[UsageRow], args: argparse.Namespace) -> list[UsageRow]:
    since = parse_when(args.since, end_of_day=False)
    until = parse_when(args.until, end_of_day=True)
    kinds = {kind.strip().lower() for kind in args.kind if kind.strip()}
    model_contains = [needle.strip().lower() for needle in args.model_contains if needle.strip()]

    result: list[UsageRow] = []
    for row in rows:
        if since and row.timestamp < since:
            continue
        if until and row.timestamp > until:
            continue
        if kinds and row.kind.lower() not in kinds:
            continue
        if model_contains and not any(needle in row.model.lower() for needle in model_contains):
            continue
        result.append(row)
    return result


def format_int(value: int) -> str:
    return f"{value:,}"


def format_decimal_usd(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.0001')):,}"


def normalized_token_breakdown(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_tokens": data["input_without_cache_write"],
        "cache_write_tokens": data["input_with_cache_write"],
        "cache_read_tokens": data["cache_read"],
        "output_tokens": data["output_tokens"],
        "total_tokens": data["total_tokens"],
    }


def summarize_rows(rows: list[UsageRow]) -> dict[str, Any]:
    totals = {
        "events": len(rows),
        "input_with_cache_write": 0,
        "input_without_cache_write": 0,
        "cache_read": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "reported_cost_usd": Decimal("0"),
    }
    by_kind: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"events": 0, "total_tokens": 0, "reported_cost_usd": Decimal("0")}
    )
    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "events": 0,
            "input_with_cache_write": 0,
            "input_without_cache_write": 0,
            "cache_read": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "reported_cost_usd": Decimal("0"),
        }
    )

    for row in rows:
        totals["input_with_cache_write"] += row.input_with_cache_write
        totals["input_without_cache_write"] += row.input_without_cache_write
        totals["cache_read"] += row.cache_read
        totals["output_tokens"] += row.output_tokens
        totals["total_tokens"] += row.total_tokens
        totals["reported_cost_usd"] += row.cost_usd

        kind_row = by_kind[row.kind]
        kind_row["events"] += 1
        kind_row["total_tokens"] += row.total_tokens
        kind_row["reported_cost_usd"] += row.cost_usd

        model_row = by_model[row.model]
        model_row["events"] += 1
        model_row["input_with_cache_write"] += row.input_with_cache_write
        model_row["input_without_cache_write"] += row.input_without_cache_write
        model_row["cache_read"] += row.cache_read
        model_row["output_tokens"] += row.output_tokens
        model_row["total_tokens"] += row.total_tokens
        model_row["reported_cost_usd"] += row.cost_usd

    model_rows = [
        {
            "model": model,
            "events": data["events"],
            "input_with_cache_write": data["input_with_cache_write"],
            "input_without_cache_write": data["input_without_cache_write"],
            "cache_read": data["cache_read"],
            "output_tokens": data["output_tokens"],
            "total_tokens": data["total_tokens"],
            "reported_cost_usd": str(data["reported_cost_usd"]),
            **normalized_token_breakdown(data),
        }
        for model, data in by_model.items()
    ]
    model_rows.sort(
        key=lambda row: (Decimal(row["reported_cost_usd"]), row["total_tokens"]),
        reverse=True,
    )

    kind_rows = [
        {
            "kind": kind,
            "events": data["events"],
            "total_tokens": data["total_tokens"],
            "reported_cost_usd": str(data["reported_cost_usd"]),
        }
        for kind, data in by_kind.items()
    ]
    kind_rows.sort(
        key=lambda row: (Decimal(row["reported_cost_usd"]), row["events"]),
        reverse=True,
    )

    totals_json = {
        **totals,
        "reported_cost_usd": str(totals["reported_cost_usd"]),
        **normalized_token_breakdown(totals),
    }

    return {
        "totals": totals_json,
        "by_kind": kind_rows,
        "by_model": model_rows,
        "date_range": {
            "first_event": rows[0].timestamp.isoformat() if rows else None,
            "last_event": rows[-1].timestamp.isoformat() if rows else None,
        },
    }


def print_text_report(
    summary: dict[str, Any],
    *,
    total_rows: int,
    matched_rows: int,
    top: int,
    source: str,
) -> None:
    totals = summary["totals"]
    kinds = summary["by_kind"]
    models = summary["by_model"]
    date_range = summary["date_range"]

    print(f"Source: {source}")
    print(f"Rows matched: {matched_rows:,} / {total_rows:,}")
    print("Cost comes from Cursor's exported CSV `Cost` column.")
    if date_range["first_event"] and date_range["last_event"]:
        print(f"Date range: {date_range['first_event']} .. {date_range['last_event']}")
    print()

    print("Totals")
    print(f"  input:                   {format_int(totals['input_tokens'])}")
    print(f"  cache write:             {format_int(totals['cache_write_tokens'])}")
    print(f"  cache read:              {format_int(totals['cache_read_tokens'])}")
    print(f"  output:                  {format_int(totals['output_tokens'])}")
    print(f"  total tokens:            {format_int(totals['total_tokens'])}")
    print(f"  reported cost:           {format_decimal_usd(Decimal(totals['reported_cost_usd']))}")
    print()

    print("By kind")
    for row in kinds:
        print(
            f"  {row['kind']}: "
            f"{format_decimal_usd(Decimal(row['reported_cost_usd']))} "
            f"(events={row['events']:,}, total_tokens={format_int(row['total_tokens'])})"
        )

    print()
    print("Top models")
    limit = len(models) if top <= 0 else min(top, len(models))
    for row in models[:limit]:
        print(
            f"  {row['model']}: "
            f"{format_decimal_usd(Decimal(row['reported_cost_usd']))} "
            f"(events={row['events']:,}, total_tokens={format_int(row['total_tokens'])})"
        )


def main() -> int:
    args = parse_args()
    csv_text = load_csv_text(args)
    all_rows = parse_csv_rows(csv_text)
    rows = filter_rows(all_rows, args)
    summary = summarize_rows(rows)

    source = str(args.csv_file) if args.csv_file else USAGE_CSV_URL

    if args.json:
        print(
            json.dumps(
                {
                    "source": source,
                    "rows_total": len(all_rows),
                    "rows_matched": len(rows),
                    **summary,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print_text_report(
            summary,
            total_rows=len(all_rows),
            matched_rows=len(rows),
            top=args.top,
            source=source,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
