#!/usr/bin/env python3
"""Estimate total Claude Code token usage and spend from local transcripts.

The script scans Claude Code session logs under ~/.claude/projects, extracts
per-response usage blocks, de-duplicates repeated records for the same API
request, fetches Anthropic pricing from models.dev, and prints a spend summary.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import dateparser


DEFAULT_TRANSCRIPTS_ROOT = Path.home() / ".claude" / "projects"
DEFAULT_CACHE_PATH = Path.home() / ".cache" / "claude-usage-costs" / "models.dev-api.json"
DEFAULT_PRICING_URL = "https://models.dev/api.json"
MILLION = Decimal("1000000")


@dataclass
class UsageEvent:
    file_path: str
    session_id: str | None
    timestamp: datetime | None
    request_key: str
    model: str
    speed: str | None
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_write_5m_input_tokens: int
    cache_write_1h_input_tokens: int


@dataclass
class PriceBook:
    model_id: str
    input_cost_per_mtok: Decimal
    output_cost_per_mtok: Decimal
    cache_read_cost_per_mtok: Decimal
    cache_write_5m_cost_per_mtok: Decimal
    cache_write_1h_cost_per_mtok: Decimal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan Claude Code transcripts under ~/.claude/projects and estimate "
            "total token usage and dollar cost using models.dev pricing."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_TRANSCRIPTS_ROOT,
        help=f"Transcript root directory (default: {DEFAULT_TRANSCRIPTS_ROOT})",
    )
    parser.add_argument(
        "--exclude-subagents",
        action="store_true",
        help="Exclude transcript files under subagents/.",
    )
    parser.add_argument(
        "--since",
        type=str,
        help="Only include events on or after this ISO-8601 date/time or natural-language date expression. Date-only values start at 00:00:00 UTC.",
    )
    parser.add_argument(
        "--until",
        type=str,
        help="Only include events before or at this ISO-8601 date/time or natural-language date expression. Date-only values include the full UTC day.",
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
        help=f"Cache path for downloaded pricing data (default: {DEFAULT_CACHE_PATH})",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not fetch pricing; require --pricing-file or a cached file.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a text report.",
    )
    return parser.parse_args()


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
            "User-Agent": "claude-usage-costs/1.0 (+https://models.dev)",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
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


def normalize_model_id(model: str) -> str:
    normalized = model.strip()
    if normalized.startswith("anthropic/"):
        normalized = normalized.split("/", 1)[1]
    normalized = normalized.replace(".", "-")
    normalized = normalized.replace("@", "-")
    return normalized


def resolve_price_book(pricing_data: dict[str, Any], model: str, speed: str | None) -> PriceBook | None:
    anthropic = pricing_data.get("anthropic")
    if not isinstance(anthropic, dict):
        return None
    models = anthropic.get("models")
    if not isinstance(models, dict):
        return None

    candidate_ids = []
    model_id = normalize_model_id(model)
    candidate_ids.append(model_id)

    if model_id.endswith("-latest"):
        candidate_ids.append(model_id.removesuffix("-latest"))

    # For inputs like claude-sonnet-4-5@20250929.
    candidate_ids.append(model.strip())

    entry = None
    matched_id = None
    for candidate in candidate_ids:
        candidate = normalize_model_id(candidate)
        if candidate in models:
            entry = models[candidate]
            matched_id = candidate
            break
    if not isinstance(entry, dict):
        return None

    cost = entry.get("cost")
    if not isinstance(cost, dict):
        return None

    mode_cost = None
    if speed and speed != "standard":
        experimental = entry.get("experimental")
        if isinstance(experimental, dict):
            modes = experimental.get("modes")
            if isinstance(modes, dict):
                speed_entry = modes.get(speed)
                if isinstance(speed_entry, dict) and isinstance(speed_entry.get("cost"), dict):
                    mode_cost = speed_entry["cost"]

    active_cost = mode_cost if mode_cost is not None else cost
    input_cost = decimal_from_json(active_cost.get("input"), Decimal("0"))
    output_cost = decimal_from_json(active_cost.get("output"), Decimal("0"))
    cache_read_cost = decimal_from_json(
        active_cost.get("cache_read"),
        input_cost * Decimal("0.1"),
    )
    cache_write_5m_cost = decimal_from_json(
        active_cost.get("cache_write"),
        input_cost * Decimal("1.25"),
    )
    cache_write_1h_cost = decimal_from_json(
        active_cost.get("cache_write_1h"),
        input_cost * Decimal("2"),
    )

    return PriceBook(
        model_id=matched_id or model_id,
        input_cost_per_mtok=input_cost,
        output_cost_per_mtok=output_cost,
        cache_read_cost_per_mtok=cache_read_cost,
        cache_write_5m_cost_per_mtok=cache_write_5m_cost,
        cache_write_1h_cost_per_mtok=cache_write_1h_cost,
    )


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


def extract_usage_event(record: dict[str, Any], file_path: Path) -> UsageEvent | None:
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    request_key = (
        record.get("requestId")
        or message.get("id")
        or record.get("uuid")
    )
    if not isinstance(request_key, str) or not request_key:
        return None

    input_tokens = intish(usage.get("input_tokens"))
    output_tokens = intish(usage.get("output_tokens"))
    cache_read_input_tokens = intish(usage.get("cache_read_input_tokens"))
    cache_creation_input_tokens = intish(usage.get("cache_creation_input_tokens"))

    cache_creation = usage.get("cache_creation")
    cache_write_5m_input_tokens = 0
    cache_write_1h_input_tokens = 0
    if isinstance(cache_creation, dict):
        cache_write_5m_input_tokens = intish(cache_creation.get("ephemeral_5m_input_tokens"))
        cache_write_1h_input_tokens = intish(cache_creation.get("ephemeral_1h_input_tokens"))

    accounted_cache_writes = cache_write_5m_input_tokens + cache_write_1h_input_tokens
    if cache_creation_input_tokens > accounted_cache_writes:
        cache_write_5m_input_tokens += cache_creation_input_tokens - accounted_cache_writes

    model = message.get("model")
    if not isinstance(model, str) or not model:
        model = "<unknown>"

    return UsageEvent(
        file_path=str(file_path),
        session_id=record.get("sessionId") if isinstance(record.get("sessionId"), str) else None,
        timestamp=parse_event_timestamp(record.get("timestamp")),
        request_key=request_key,
        model=model,
        speed=usage.get("speed") if isinstance(usage.get("speed"), str) else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_write_5m_input_tokens=cache_write_5m_input_tokens,
        cache_write_1h_input_tokens=cache_write_1h_input_tokens,
    )


def iter_transcript_files(root: Path, exclude_subagents: bool) -> list[Path]:
    if not root.exists():
        raise RuntimeError(f"transcript root does not exist: {root}")

    files = sorted(root.rglob("*.jsonl"))
    if exclude_subagents:
        files = [path for path in files if "subagents" not in path.parts]
    return files


def load_usage_events(
    root: Path,
    exclude_subagents: bool,
    since: datetime | None,
    until: datetime | None,
) -> tuple[list[UsageEvent], int]:
    files = iter_transcript_files(root, exclude_subagents)
    events: list[UsageEvent] = []
    parse_failures = 0

    for file_path in files:
        seen_keys: set[str] = set()
        with file_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    parse_failures += 1
                    continue

                event = extract_usage_event(record, file_path)
                if event is None:
                    continue

                if event.request_key in seen_keys:
                    continue
                seen_keys.add(event.request_key)

                if since and event.timestamp and event.timestamp < since:
                    continue
                if until and event.timestamp and event.timestamp > until:
                    continue

                events.append(event)

    return events, parse_failures


def format_int(value: int) -> str:
    return f"{value:,}"


def format_decimal_usd(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.0001')):,}"


def summarize(
    events: list[UsageEvent],
    pricing_data: dict[str, Any],
) -> dict[str, Any]:
    totals = {
        "responses": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_write_5m_input_tokens": 0,
        "cache_write_1h_input_tokens": 0,
        "total_billed_tokens": 0,
    }
    spend = {
        "input_cost_usd": Decimal("0"),
        "output_cost_usd": Decimal("0"),
        "cache_read_cost_usd": Decimal("0"),
        "cache_write_5m_cost_usd": Decimal("0"),
        "cache_write_1h_cost_usd": Decimal("0"),
        "total_cost_usd": Decimal("0"),
    }
    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "responses": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_write_5m_input_tokens": 0,
            "cache_write_1h_input_tokens": 0,
            "total_cost_usd": Decimal("0"),
            "priced_as": None,
        }
    )
    unknown_models = Counter()
    timestamps = [event.timestamp for event in events if event.timestamp is not None]

    for event in events:
        totals["responses"] += 1
        totals["input_tokens"] += event.input_tokens
        totals["output_tokens"] += event.output_tokens
        totals["cache_read_input_tokens"] += event.cache_read_input_tokens
        totals["cache_write_5m_input_tokens"] += event.cache_write_5m_input_tokens
        totals["cache_write_1h_input_tokens"] += event.cache_write_1h_input_tokens
        totals["total_billed_tokens"] += (
            event.input_tokens
            + event.output_tokens
            + event.cache_read_input_tokens
            + event.cache_write_5m_input_tokens
            + event.cache_write_1h_input_tokens
        )

        model_row = by_model[event.model]
        model_row["responses"] += 1
        model_row["input_tokens"] += event.input_tokens
        model_row["output_tokens"] += event.output_tokens
        model_row["cache_read_input_tokens"] += event.cache_read_input_tokens
        model_row["cache_write_5m_input_tokens"] += event.cache_write_5m_input_tokens
        model_row["cache_write_1h_input_tokens"] += event.cache_write_1h_input_tokens

        if event.model == "<synthetic>":
            continue

        price_book = resolve_price_book(pricing_data, event.model, speed=event.speed)
        if price_book is None:
            token_total = (
                event.input_tokens
                + event.output_tokens
                + event.cache_read_input_tokens
                + event.cache_write_5m_input_tokens
                + event.cache_write_1h_input_tokens
            )
            if token_total:
                unknown_models[event.model] += token_total
            continue

        model_row["priced_as"] = price_book.model_id

        input_cost = Decimal(event.input_tokens) * price_book.input_cost_per_mtok / MILLION
        output_cost = Decimal(event.output_tokens) * price_book.output_cost_per_mtok / MILLION
        cache_read_cost = (
            Decimal(event.cache_read_input_tokens) * price_book.cache_read_cost_per_mtok / MILLION
        )
        cache_write_5m_cost = (
            Decimal(event.cache_write_5m_input_tokens)
            * price_book.cache_write_5m_cost_per_mtok
            / MILLION
        )
        cache_write_1h_cost = (
            Decimal(event.cache_write_1h_input_tokens)
            * price_book.cache_write_1h_cost_per_mtok
            / MILLION
        )
        total_cost = (
            input_cost
            + output_cost
            + cache_read_cost
            + cache_write_5m_cost
            + cache_write_1h_cost
        )

        spend["input_cost_usd"] += input_cost
        spend["output_cost_usd"] += output_cost
        spend["cache_read_cost_usd"] += cache_read_cost
        spend["cache_write_5m_cost_usd"] += cache_write_5m_cost
        spend["cache_write_1h_cost_usd"] += cache_write_1h_cost
        spend["total_cost_usd"] += total_cost
        model_row["total_cost_usd"] += total_cost

    model_rows = []
    for model, row in by_model.items():
        model_rows.append(
            {
                "model": model,
                "priced_as": row["priced_as"],
                "responses": row["responses"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_read_input_tokens": row["cache_read_input_tokens"],
                "cache_write_5m_input_tokens": row["cache_write_5m_input_tokens"],
                "cache_write_1h_input_tokens": row["cache_write_1h_input_tokens"],
                "total_cost_usd": str(row["total_cost_usd"]),
            }
        )
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


def print_text_report(
    summary: dict[str, Any],
    root: Path,
    files_scanned: int,
    parse_failures: int,
) -> None:
    totals = summary["totals"]
    spend = {key: Decimal(value) for key, value in summary["spend"].items()}
    by_model = summary["by_model"]
    unknown_models = summary["unknown_models"]
    date_range = summary["date_range"]

    print(f"Transcript root: {root}")
    print(f"Files scanned: {files_scanned:,}")
    print(f"Unique assistant responses counted: {totals['responses']:,}")
    print(f"Unreadable JSONL lines skipped: {parse_failures:,}")
    print("Spend is an API-style estimate using models.dev Anthropic pricing.")
    if date_range["first_event"] and date_range["last_event"]:
        print(f"Date range: {date_range['first_event']} .. {date_range['last_event']}")
    print()

    print("Tokens")
    print(f"  input:            {format_int(totals['input_tokens'])}")
    print(f"  cache write (5m): {format_int(totals['cache_write_5m_input_tokens'])}")
    print(f"  cache write (1h): {format_int(totals['cache_write_1h_input_tokens'])}")
    print(f"  cache read:       {format_int(totals['cache_read_input_tokens'])}")
    print(f"  output:           {format_int(totals['output_tokens'])}")
    print(f"  total billed:     {format_int(totals['total_billed_tokens'])}")
    print()

    print("Estimated spend (USD)")
    print(f"  input:            {format_decimal_usd(spend['input_cost_usd'])}")
    print(f"  cache write (5m): {format_decimal_usd(spend['cache_write_5m_cost_usd'])}")
    print(f"  cache write (1h): {format_decimal_usd(spend['cache_write_1h_cost_usd'])}")
    print(f"  cache read:       {format_decimal_usd(spend['cache_read_cost_usd'])}")
    print(f"  output:           {format_decimal_usd(spend['output_cost_usd'])}")
    print(f"  total:            {format_decimal_usd(spend['total_cost_usd'])}")
    print()

    print("By model")
    for row in by_model:
        print(
            "  "
            f"{row['model']}: "
            f"{format_decimal_usd(Decimal(row['total_cost_usd']))} "
            f"(responses={row['responses']:,}, "
            f"input={format_int(row['input_tokens'])}, "
            f"cache_write_5m={format_int(row['cache_write_5m_input_tokens'])}, "
            f"cache_read={format_int(row['cache_read_input_tokens'])}, "
            f"output={format_int(row['output_tokens'])})"
        )

    if unknown_models:
        print()
        print("Unpriced models")
        for model, tokens in sorted(unknown_models.items(), key=lambda item: item[1], reverse=True):
            print(f"  {model}: {format_int(tokens)} tokens")


def main() -> int:
    args = parse_args()
    since = parse_when(args.since, end_of_day=False)
    until = parse_when(args.until, end_of_day=True)
    pricing_data = fetch_pricing_json(args)
    events, parse_failures = load_usage_events(
        root=args.root,
        exclude_subagents=args.exclude_subagents,
        since=since,
        until=until,
    )
    summary = summarize(events, pricing_data)
    files_scanned = len(iter_transcript_files(args.root, args.exclude_subagents))

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
        print_text_report(summary, args.root, files_scanned, parse_failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
