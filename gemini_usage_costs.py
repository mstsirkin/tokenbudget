#!/usr/bin/env python3
"""Estimate Gemini CLI token usage and spend from local session files."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import dateparser


DEFAULT_GEMINI_ROOT = Path.home() / ".gemini"
DEFAULT_CACHE_PATH = Path.home() / ".cache" / "gemini-usage-costs" / "models.dev-api.json"
DEFAULT_PRICING_URL = "https://models.dev/api.json"
MILLION = Decimal("1000000")
LOCAL_TZINFO = datetime.now().astimezone().tzinfo or UTC
LOCAL_TZNAME = str(LOCAL_TZINFO)
ZERO = Decimal("0")
GRAPH_MODES: dict[str, dict[str, Any]] = {
    "hourly": {
        "count": 24,
        "title": "Last 24 hourly buckets",
        "unit_suffix": "h",
        "note": "Each point is one local-hour bucket. The newest bucket may be partial.",
        "selected_label": "This hour",
    },
    "daily": {
        "count": 14,
        "title": "Last 14 daily buckets",
        "unit_suffix": "day",
        "note": "Each point is one local-day bucket. Today's bucket may be partial.",
        "selected_label": "Today",
    },
    "weekly": {
        "count": 12,
        "title": "Last 12 weekly buckets",
        "unit_suffix": "week",
        "note": "Each point is one local-week bucket starting on Monday. This week's bucket may be partial.",
        "selected_label": "This week",
    },
    "monthly": {
        "count": 12,
        "title": "Last 12 monthly buckets",
        "unit_suffix": "month",
        "note": "Each point is one local-month bucket. This month's bucket may be partial.",
        "selected_label": "This month",
    },
}


@dataclass
class UsageEvent:
    file_path: str
    session_id: str | None
    timestamp: datetime | None
    message_id: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    response_output_tokens: int
    thought_tokens: int
    tool_tokens: int
    billed_output_tokens: int
    total_billed_tokens: int


@dataclass
class PriceBook:
    model_id: str
    input_cost_per_mtok: Decimal
    output_cost_per_mtok: Decimal
    cache_read_cost_per_mtok: Decimal
    long_input_cost_per_mtok: Decimal
    long_output_cost_per_mtok: Decimal
    long_cache_read_cost_per_mtok: Decimal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan local Gemini CLI sessions under ~/.gemini and estimate token "
            "usage and dollar cost using models.dev Google pricing."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_GEMINI_ROOT,
        help=f"Gemini data root directory (default: {DEFAULT_GEMINI_ROOT})",
    )
    parser.add_argument(
        "--since",
        type=str,
        help="Only include events on or after this ISO-8601 date/time or natural-language date expression. Date-only values start at 00:00:00 local time.",
    )
    parser.add_argument(
        "--until",
        type=str,
        help="Only include events before or at this ISO-8601 date/time or natural-language date expression. Date-only values include the full local day.",
    )
    parser.add_argument(
        "--pricing-url",
        default=DEFAULT_PRICING_URL,
        help=f"models.dev API URL (default: {DEFAULT_PRICING_URL})",
    )
    parser.add_argument(
        "--pricing-file",
        type=Path,
        help="Use a local models.dev api.json file instead of fetching it.",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=DEFAULT_CACHE_PATH,
        help=f"Path for the cached pricing JSON (default: {DEFAULT_CACHE_PATH})",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not fetch pricing; require --pricing-file or a cached file.",
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
    parser.add_argument(
        "--all",
        dest="all_modes",
        action="store_true",
        help="With --json, emit standard hourly/daily/weekly/monthly stats and buckets.",
    )
    args = parser.parse_args()
    if args.all_modes and not args.json:
        parser.error("--all requires --json")
    if args.all_modes and (args.since or args.until):
        parser.error("--all cannot be combined with --since/--until")
    return args


def parse_event_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value
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
            "TIMEZONE": LOCAL_TZNAME,
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
    day = (datetime.now(tz=LOCAL_TZINFO) + timedelta(days=offset)).date()
    return datetime.combine(
        day, time.max if end_of_day else time.min, tzinfo=LOCAL_TZINFO
    ).astimezone(UTC)


def parse_iso_value(text: str, *, end_of_day: bool) -> datetime:
    normalized = text
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    has_time = "T" in normalized or " " in normalized
    dt = datetime.fromisoformat(normalized)
    if not has_time:
        dt = dt.replace(
            hour=23 if end_of_day else 0,
            minute=59 if end_of_day else 0,
            second=59 if end_of_day else 0,
            microsecond=999999 if end_of_day else 0,
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZINFO)
    return dt.astimezone(UTC)


def start_of_bucket(dt: datetime, mode: str) -> datetime:
    if mode == "hourly":
        return dt.replace(minute=0, second=0, microsecond=0)
    if mode == "daily":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if mode == "weekly":
        day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return day_start - timedelta(days=day_start.weekday())
    if mode == "monthly":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unsupported graph mode {mode!r}")


def add_months(dt: datetime, months: int) -> datetime:
    month_index = (dt.month - 1) + months
    year = dt.year + month_index // 12
    month = (month_index % 12) + 1
    return dt.replace(year=year, month=month, day=1)


def shift_bucket(dt: datetime, mode: str, steps: int) -> datetime:
    if mode == "hourly":
        return dt + timedelta(hours=steps)
    if mode == "daily":
        return dt + timedelta(days=steps)
    if mode == "weekly":
        return dt + timedelta(weeks=steps)
    if mode == "monthly":
        return add_months(dt, steps)
    raise ValueError(f"unsupported graph mode {mode!r}")


def bucket_label(dt: datetime, mode: str) -> str:
    if mode == "hourly":
        return dt.strftime("%a %H:%M")
    if mode == "daily":
        return dt.strftime("%b %d")
    if mode == "weekly":
        return dt.strftime("wk %b %d")
    if mode == "monthly":
        return dt.strftime("%b %Y")
    raise ValueError(f"unsupported graph mode {mode!r}")


def bucket_range(now: datetime, mode: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config = GRAPH_MODES[mode]
    count = int(config["count"])
    current_bucket_start = start_of_bucket(now, mode)
    first_bucket_start = shift_bucket(current_bucket_start, mode, -(count - 1))
    buckets: list[dict[str, Any]] = []
    for index in range(count):
        bucket_start = shift_bucket(first_bucket_start, mode, index)
        bucket_end = min(shift_bucket(bucket_start, mode, 1), now)
        buckets.append(
            {
                "start": bucket_start,
                "end": bucket_end,
                "label": bucket_label(bucket_start, mode),
            }
        )
    return buckets, config


def selected_window(now: datetime, mode: str) -> tuple[str, datetime, datetime]:
    start = start_of_bucket(now, mode)
    return str(GRAPH_MODES[mode]["selected_label"]), start, now


def all_modes_start(now: datetime) -> datetime:
    monthly_count = int(GRAPH_MODES["monthly"]["count"])
    return shift_bucket(start_of_bucket(now, "monthly"), "monthly", -(monthly_count - 1))


def read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fetch_pricing_json(args: argparse.Namespace) -> dict[str, Any]:
    if args.pricing_file:
        return read_json_file(args.pricing_file)

    cache_path: Path = args.cache_file
    if args.offline:
        if cache_path.exists():
            return read_json_file(cache_path)
        raise RuntimeError(
            f"offline mode requested but cache file does not exist: {cache_path}"
        )

    request = urllib.request.Request(
        args.pricing_url,
        headers={
            "User-Agent": "gemini-usage-costs/1.0 (+https://models.dev)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if cache_path.exists():
            print(
                f"warning: could not fetch pricing ({exc}); using cached file {cache_path}",
                file=sys.stderr,
            )
            return read_json_file(cache_path)
        raise RuntimeError(f"failed to fetch pricing from {args.pricing_url}: {exc}") from exc

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(payload, encoding="utf-8")
    return json.loads(payload)


def decimal_from_json(value: Any, default: Decimal) -> Decimal:
    if value is None:
        return default
    return Decimal(str(value))


def resolve_price_book(pricing_data: dict[str, Any], model: str) -> PriceBook | None:
    google = pricing_data.get("google")
    if not isinstance(google, dict):
        return None
    models = google.get("models")
    if not isinstance(models, dict):
        return None
    entry = models.get(model)
    if not isinstance(entry, dict):
        return None
    cost = entry.get("cost")
    if not isinstance(cost, dict):
        return None
    long_cost = cost.get("context_over_200k")
    if not isinstance(long_cost, dict):
        long_cost = {}

    input_cost = decimal_from_json(cost.get("input"), ZERO)
    output_cost = decimal_from_json(cost.get("output"), ZERO)
    cache_read_cost = decimal_from_json(cost.get("cache_read"), input_cost * Decimal("0.1"))
    return PriceBook(
        model_id=model,
        input_cost_per_mtok=input_cost,
        output_cost_per_mtok=output_cost,
        cache_read_cost_per_mtok=cache_read_cost,
        long_input_cost_per_mtok=decimal_from_json(long_cost.get("input"), input_cost),
        long_output_cost_per_mtok=decimal_from_json(long_cost.get("output"), output_cost),
        long_cache_read_cost_per_mtok=decimal_from_json(
            long_cost.get("cache_read"), cache_read_cost
        ),
    )


def resolve_price_book_cached(
    pricing_data: dict[str, Any],
    model: str,
    cache: dict[str, PriceBook | None],
) -> PriceBook | None:
    if model not in cache:
        cache[model] = resolve_price_book(pricing_data, model)
    return cache[model]


def intish(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        return int(float(text))
    return 0


def decimalish(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text:
        return ZERO
    try:
        return Decimal(text)
    except InvalidOperation:
        return ZERO


def extract_usage_event(record: dict[str, Any], file_path: Path, session_id: str | None) -> UsageEvent | None:
    if record.get("type") != "gemini":
        return None
    message_id = record.get("id")
    if not isinstance(message_id, str) or not message_id:
        return None
    tokens = record.get("tokens")
    if not isinstance(tokens, dict):
        return None

    input_tokens = intish(tokens.get("input"))
    cached_input_tokens = intish(tokens.get("cached"))
    response_output_tokens = intish(tokens.get("output"))
    thought_tokens = intish(tokens.get("thoughts"))
    tool_tokens = intish(tokens.get("tool"))
    total_billed_tokens = intish(tokens.get("total"))
    if total_billed_tokens <= 0:
        total_billed_tokens = (
            input_tokens
            + cached_input_tokens
            + response_output_tokens
            + thought_tokens
            + tool_tokens
        )

    billed_output_tokens = total_billed_tokens - input_tokens - cached_input_tokens
    if billed_output_tokens < 0:
        billed_output_tokens = response_output_tokens + thought_tokens + tool_tokens
    if billed_output_tokens < 0:
        billed_output_tokens = 0

    model = record.get("model")
    if not isinstance(model, str) or not model:
        model = "<unknown>"

    return UsageEvent(
        file_path=str(file_path),
        session_id=session_id,
        timestamp=parse_event_timestamp(record.get("timestamp")),
        message_id=message_id,
        model=model,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        response_output_tokens=response_output_tokens,
        thought_tokens=thought_tokens,
        tool_tokens=tool_tokens,
        billed_output_tokens=billed_output_tokens,
        total_billed_tokens=total_billed_tokens,
    )


def iter_session_files(root: Path) -> list[Path]:
    tmp_root = root / "tmp"
    if not tmp_root.exists():
        raise RuntimeError(f"Gemini tmp root does not exist: {tmp_root}")
    return sorted(tmp_root.rglob("chats/session-*.json*"))


def iter_session_records(path: Path) -> tuple[str | None, list[dict[str, Any]], int]:
    parse_failures = 0
    if path.suffix == ".jsonl":
        session_id: str | None = None
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    parse_failures += 1
                    continue
                if session_id is None and isinstance(record.get("sessionId"), str):
                    session_id = record["sessionId"]
                if isinstance(record, dict):
                    records.append(record)
        return session_id, records, parse_failures

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, [], 1
    records: list[dict[str, Any]] = []
    session_id: str | None = None
    if isinstance(payload, dict):
        if isinstance(payload.get("sessionId"), str):
            session_id = payload["sessionId"]
        messages = payload.get("messages")
        if isinstance(messages, list):
            records = [record for record in messages if isinstance(record, dict)]
    elif isinstance(payload, list):
        records = [record for record in payload if isinstance(record, dict)]
        if records and isinstance(records[0].get("sessionId"), str):
            session_id = records[0]["sessionId"]
    return session_id, records, 0


def load_usage_events(
    root: Path,
    since: datetime | None,
    until: datetime | None,
) -> tuple[list[UsageEvent], int, int]:
    files = iter_session_files(root)
    events: list[UsageEvent] = []
    parse_failures = 0
    seen_message_ids: set[str] = set()

    for file_path in files:
        session_id, records, file_failures = iter_session_records(file_path)
        parse_failures += file_failures
        for record in records:
            event = extract_usage_event(record, file_path, session_id)
            if event is None:
                continue
            if event.message_id in seen_message_ids:
                continue
            seen_message_ids.add(event.message_id)
            if since and event.timestamp and event.timestamp < since:
                continue
            if until and event.timestamp and event.timestamp > until:
                continue
            events.append(event)

    return events, parse_failures, len(files)


def format_int(value: int) -> str:
    return f"{value:,}"


def format_decimal_usd(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.0001')):,}"


def compute_event_costs(event: UsageEvent, price_book: PriceBook) -> dict[str, Decimal]:
    context_tokens = event.input_tokens + event.cached_input_tokens
    long_context = context_tokens > 200_000
    input_rate = (
        price_book.long_input_cost_per_mtok if long_context else price_book.input_cost_per_mtok
    )
    output_rate = (
        price_book.long_output_cost_per_mtok if long_context else price_book.output_cost_per_mtok
    )
    cache_read_rate = (
        price_book.long_cache_read_cost_per_mtok
        if long_context
        else price_book.cache_read_cost_per_mtok
    )
    input_cost = Decimal(event.input_tokens) * input_rate / MILLION
    cache_read_cost = Decimal(event.cached_input_tokens) * cache_read_rate / MILLION
    output_cost = Decimal(event.billed_output_tokens) * output_rate / MILLION
    total_cost = input_cost + cache_read_cost + output_cost
    return {
        "input_cost_usd": input_cost,
        "cache_read_cost_usd": cache_read_cost,
        "output_cost_usd": output_cost,
        "total_cost_usd": total_cost,
    }


def summarize(events: list[UsageEvent], pricing_data: dict[str, Any]) -> dict[str, Any]:
    totals = {
        "responses": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "response_output_tokens": 0,
        "thought_tokens": 0,
        "tool_tokens": 0,
        "billed_output_tokens": 0,
        "total_billed_tokens": 0,
    }
    spend = {
        "input_cost_usd": ZERO,
        "cache_read_cost_usd": ZERO,
        "output_cost_usd": ZERO,
        "total_cost_usd": ZERO,
    }
    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "responses": 0,
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "response_output_tokens": 0,
            "thought_tokens": 0,
            "tool_tokens": 0,
            "billed_output_tokens": 0,
            "total_billed_tokens": 0,
            "total_cost_usd": ZERO,
            "priced_as": None,
        }
    )
    unknown_models: dict[str, int] = defaultdict(int)
    timestamps = [event.timestamp for event in events if event.timestamp is not None]
    price_book_cache: dict[str, PriceBook | None] = {}

    for event in events:
        totals["responses"] += 1
        totals["input_tokens"] += event.input_tokens
        totals["cache_read_tokens"] += event.cached_input_tokens
        totals["response_output_tokens"] += event.response_output_tokens
        totals["thought_tokens"] += event.thought_tokens
        totals["tool_tokens"] += event.tool_tokens
        totals["billed_output_tokens"] += event.billed_output_tokens
        totals["total_billed_tokens"] += event.total_billed_tokens

        model_row = by_model[event.model]
        model_row["responses"] += 1
        model_row["input_tokens"] += event.input_tokens
        model_row["cache_read_tokens"] += event.cached_input_tokens
        model_row["response_output_tokens"] += event.response_output_tokens
        model_row["thought_tokens"] += event.thought_tokens
        model_row["tool_tokens"] += event.tool_tokens
        model_row["billed_output_tokens"] += event.billed_output_tokens
        model_row["total_billed_tokens"] += event.total_billed_tokens

        price_book = resolve_price_book_cached(pricing_data, event.model, price_book_cache)
        if price_book is None:
            unknown_models[event.model] += event.total_billed_tokens
            continue

        model_row["priced_as"] = price_book.model_id
        costs = compute_event_costs(event, price_book)
        spend["input_cost_usd"] += costs["input_cost_usd"]
        spend["cache_read_cost_usd"] += costs["cache_read_cost_usd"]
        spend["output_cost_usd"] += costs["output_cost_usd"]
        spend["total_cost_usd"] += costs["total_cost_usd"]
        model_row["total_cost_usd"] += costs["total_cost_usd"]

    model_rows = [
        {
            "model": model,
            "priced_as": row["priced_as"],
            "responses": row["responses"],
            "input_tokens": row["input_tokens"],
            "cache_read_tokens": row["cache_read_tokens"],
            "response_output_tokens": row["response_output_tokens"],
            "thought_tokens": row["thought_tokens"],
            "tool_tokens": row["tool_tokens"],
            "billed_output_tokens": row["billed_output_tokens"],
            "total_billed_tokens": row["total_billed_tokens"],
            "total_cost_usd": str(row["total_cost_usd"]),
        }
        for model, row in by_model.items()
    ]
    model_rows.sort(key=lambda row: Decimal(row["total_cost_usd"]), reverse=True)

    return {
        "totals": totals,
        "spend": {key: str(value) for key, value in spend.items()},
        "by_model": model_rows,
        "unknown_models": dict(unknown_models),
        "date_range": {
            "first_event": min(timestamps).isoformat() if timestamps else None,
            "last_event": max(timestamps).isoformat() if timestamps else None,
        },
    }


def empty_mode_totals() -> dict[str, Any]:
    return {
        "responses": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "response_output_tokens": 0,
        "thought_tokens": 0,
        "tool_tokens": 0,
        "billed_output_tokens": 0,
        "total_billed_tokens": 0,
        "total_cost_usd": ZERO,
    }


def add_event_to_mode_totals(
    totals: dict[str, Any],
    event: UsageEvent,
    total_cost_usd: Decimal,
) -> None:
    totals["responses"] += 1
    totals["input_tokens"] += event.input_tokens
    totals["cache_read_tokens"] += event.cached_input_tokens
    totals["response_output_tokens"] += event.response_output_tokens
    totals["thought_tokens"] += event.thought_tokens
    totals["tool_tokens"] += event.tool_tokens
    totals["billed_output_tokens"] += event.billed_output_tokens
    totals["total_billed_tokens"] += event.total_billed_tokens
    totals["total_cost_usd"] += total_cost_usd


def selected_gemini_token_breakdown(totals: dict[str, Any]) -> dict[str, Any]:
    return {
        "input": int(totals["input_tokens"]),
        "output": int(totals["billed_output_tokens"]),
        "cache_read": int(totals["cache_read_tokens"]),
        "cache_write": None,
    }


def build_mode_payload(
    now: datetime,
    mode: str,
    buckets: list[dict[str, Any]],
    config: dict[str, Any],
    bucket_totals: list[dict[str, Any]],
) -> dict[str, Any]:
    selected_label, selected_start, selected_end = selected_window(now, mode)
    selected = bucket_totals[-1] if bucket_totals else empty_mode_totals()
    bucket_summaries = [
        {
            "start": bucket["start"].isoformat(),
            "end": bucket["end"].isoformat(),
            "label": bucket["label"],
            "cost_usd": str(bucket_totals[index]["total_cost_usd"]),
        }
        for index, bucket in enumerate(buckets)
    ]
    return {
        "selected": {
            "label": selected_label,
            "start": selected_start.isoformat(),
            "end": selected_end.isoformat(),
            "cost_usd": str(selected["total_cost_usd"]),
            "token_breakdown": selected_gemini_token_breakdown(selected),
            "responses": int(selected["responses"]),
        },
        "graph": {
            "mode": mode,
            "title": str(config["title"]),
            "unit_suffix": str(config["unit_suffix"]),
            "note": str(config["note"]),
        },
        "buckets": bucket_summaries,
    }


def build_all_modes_payload(
    events: list[UsageEvent],
    pricing_data: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    if now is None:
        now = datetime.now().astimezone()
    range_start = all_modes_start(now)
    relevant_events = [
        event
        for event in events
        if event.timestamp is not None and range_start <= event.timestamp <= now
    ]
    mode_state: dict[str, dict[str, Any]] = {}
    for mode in GRAPH_MODES:
        buckets, config = bucket_range(now, mode)
        mode_state[mode] = {
            "buckets": buckets,
            "config": config,
            "bucket_index": {bucket["start"]: index for index, bucket in enumerate(buckets)},
            "bucket_totals": [empty_mode_totals() for _ in buckets],
        }

    price_book_cache: dict[str, PriceBook | None] = {}
    for event in relevant_events:
        assert event.timestamp is not None
        total_cost_usd = ZERO
        price_book = resolve_price_book_cached(pricing_data, event.model, price_book_cache)
        if price_book is not None:
            total_cost_usd = compute_event_costs(event, price_book)["total_cost_usd"]

        event_local_timestamp = event.timestamp.astimezone(now.tzinfo)
        for mode, state in mode_state.items():
            bucket_start = start_of_bucket(event_local_timestamp, mode)
            bucket_index = state["bucket_index"].get(bucket_start)
            if bucket_index is None:
                continue
            add_event_to_mode_totals(
                state["bucket_totals"][bucket_index], event, total_cost_usd
            )

    return {
        "updated_at": int(now.timestamp()),
        "modes": {
            mode: build_mode_payload(
                now,
                mode,
                state["buckets"],
                state["config"],
                state["bucket_totals"],
            )
            for mode, state in mode_state.items()
        },
    }


def print_text_report(
    summary: dict[str, Any],
    root: Path,
    files_scanned: int,
    parse_failures: int,
    top: int,
) -> None:
    totals = summary["totals"]
    spend = {key: Decimal(value) for key, value in summary["spend"].items()}
    by_model = summary["by_model"]
    unknown_models = summary["unknown_models"]
    date_range = summary["date_range"]

    print(f"Gemini root: {root}")
    print(f"Session files scanned: {files_scanned:,}")
    print(f"Gemini responses counted: {totals['responses']:,}")
    print(f"Unreadable JSON lines skipped: {parse_failures:,}")
    print(
        "Spend is an estimate using models.dev Google pricing. "
        "Local Gemini session files expose cached token reads but not context-cache "
        "storage duration, so storage-duration charges are not included."
    )
    if date_range["first_event"] and date_range["last_event"]:
        print(f"Date range: {date_range['first_event']} .. {date_range['last_event']}")
    print()

    print("Tokens")
    print(f"  input:          {format_int(totals['input_tokens'])}")
    print(f"  cache read:     {format_int(totals['cache_read_tokens'])}")
    print(f"  response:       {format_int(totals['response_output_tokens'])}")
    print(f"  thoughts:       {format_int(totals['thought_tokens'])}")
    print(f"  tool:           {format_int(totals['tool_tokens'])}")
    print(f"  billed output:  {format_int(totals['billed_output_tokens'])}")
    print(f"  total billed:   {format_int(totals['total_billed_tokens'])}")
    print()

    print("Estimated spend (USD)")
    print(f"  input:          {format_decimal_usd(spend['input_cost_usd'])}")
    print(f"  cache read:     {format_decimal_usd(spend['cache_read_cost_usd'])}")
    print(f"  billed output:  {format_decimal_usd(spend['output_cost_usd'])}")
    print(f"  total:          {format_decimal_usd(spend['total_cost_usd'])}")
    print()

    print("By model")
    limit = max(0, min(top, len(by_model)))
    for row in by_model[:limit]:
        print(
            "  "
            f"{row['model']}: "
            f"{format_decimal_usd(Decimal(row['total_cost_usd']))} "
            f"(responses={row['responses']:,}, "
            f"input={format_int(row['input_tokens'])}, "
            f"cache_read={format_int(row['cache_read_tokens'])}, "
            f"billed_output={format_int(row['billed_output_tokens'])})"
        )

    if unknown_models:
        print()
        print("Unpriced models")
        for model, tokens in sorted(unknown_models.items(), key=lambda item: item[1], reverse=True):
            print(f"  {model}: {format_int(tokens)} billed tokens")


def main() -> int:
    args = parse_args()
    pricing_data = fetch_pricing_json(args)

    if args.all_modes:
        now = datetime.now().astimezone()
        since = all_modes_start(now)
        events, parse_failures, files_scanned = load_usage_events(
            root=args.root,
            since=since,
            until=now,
        )
        payload = {
            "root": str(args.root),
            "files_scanned": files_scanned,
            "parse_failures": parse_failures,
            **build_all_modes_payload(events, pricing_data, now),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    since = parse_when(args.since, end_of_day=False)
    until = parse_when(args.until, end_of_day=True)
    events, parse_failures, files_scanned = load_usage_events(
        root=args.root,
        since=since,
        until=until,
    )
    summary = summarize(events, pricing_data)

    if args.json:
        print(
            json.dumps(
                {
                    "root": str(args.root),
                    "files_scanned": files_scanned,
                    "parse_failures": parse_failures,
                    **summary,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print_text_report(summary, args.root, files_scanned, parse_failures, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
