#!/usr/bin/env python3
"""Direct on-demand snapshot for the Qt tokenbudget monitor."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import claude_usage_costs as claude
import cursor_agent_usage_costs as cursor


ZERO = Decimal("0")

GRAPH_MODES: dict[str, dict[str, Any]] = {
    "hourly": {
        "count": 24,
        "title": "Last 24 hourly buckets",
        "unit_suffix": "h",
        "note": "Each point is one local-hour bucket. The newest bucket may be partial.",
    },
    "daily": {
        "count": 14,
        "title": "Last 14 daily buckets",
        "unit_suffix": "day",
        "note": "Each point is one local-day bucket. Today's bucket may be partial.",
    },
    "weekly": {
        "count": 12,
        "title": "Last 12 weekly buckets",
        "unit_suffix": "week",
        "note": "Each point is one local-week bucket starting on Monday. This week's bucket may be partial.",
    },
    "monthly": {
        "count": 12,
        "title": "Last 12 monthly buckets",
        "unit_suffix": "month",
        "note": "Each point is one local-month bucket. This month's bucket may be partial.",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute a direct tokenbudget snapshot with hourly buckets."
    )
    parser.add_argument(
        "--graph-mode",
        choices=tuple(GRAPH_MODES),
        default="hourly",
        help="Bucket mode to compute for the graphs (default: hourly).",
    )
    parser.add_argument(
        "--exclude-subagents",
        action="store_true",
        help="Exclude Claude subagent transcript files.",
    )
    return parser.parse_args()


def decimal_text(value: Decimal) -> str:
    return str(value)


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
    labels = {
        "hourly": "This hour",
        "daily": "Today",
        "weekly": "This week",
        "monthly": "This month",
    }
    return labels[mode], start, now


def in_window(timestamp: datetime | None, start: datetime, end: datetime, *, inclusive_end: bool) -> bool:
    if timestamp is None:
        return False
    if timestamp < start:
        return False
    if inclusive_end:
        return timestamp <= end
    return timestamp < end


def filter_claude_events(
    events: list[claude.UsageEvent],
    start: datetime,
    end: datetime,
    *,
    inclusive_end: bool,
) -> list[claude.UsageEvent]:
    return [
        event
        for event in events
        if in_window(event.timestamp, start, end, inclusive_end=inclusive_end)
    ]


def filter_cursor_rows(
    rows: list[cursor.UsageRow],
    start: datetime,
    end: datetime,
    *,
    inclusive_end: bool,
) -> list[cursor.UsageRow]:
    return [
        row for row in rows if in_window(row.timestamp, start, end, inclusive_end=inclusive_end)
    ]


def summarize_claude_window(
    events: list[claude.UsageEvent],
    pricing_data: dict[str, Any],
    start: datetime,
    end: datetime,
    *,
    inclusive_end: bool,
) -> dict[str, Any]:
    window_events = filter_claude_events(events, start, end, inclusive_end=inclusive_end)
    return claude.summarize(window_events, pricing_data)


def summarize_cursor_window(
    rows: list[cursor.UsageRow],
    start: datetime,
    end: datetime,
    *,
    inclusive_end: bool,
) -> dict[str, Any]:
    window_rows = filter_cursor_rows(rows, start, end, inclusive_end=inclusive_end)
    return cursor.summarize_rows(window_rows)


def collect_claude_data(now: datetime, graph_mode: str, exclude_subagents: bool) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    buckets, _ = bucket_range(now, graph_mode)
    _, selected_start, selected_end = selected_window(now, graph_mode)
    since = min(selected_start, buckets[0]["start"])

    try:
        pricing_args = SimpleNamespace(
            pricing_file=None,
            cache_file=claude.DEFAULT_CACHE_PATH,
            offline=claude.DEFAULT_CACHE_PATH.exists(),
            pricing_url=claude.DEFAULT_PRICING_URL,
        )
        pricing_data = claude.fetch_pricing_json(pricing_args)
        events, parse_failures = claude.load_usage_events(
            root=claude.DEFAULT_TRANSCRIPTS_ROOT,
            exclude_subagents=exclude_subagents,
            since=since,
            until=now,
        )
        selected = summarize_claude_window(
            events, pricing_data, selected_start, selected_end, inclusive_end=True
        )
        buckets_summary = [
            {
                "start": bucket["start"].isoformat(),
                "end": bucket["end"].isoformat(),
                "label": bucket["label"],
                "cost_usd": bucket_summary["spend"]["total_cost_usd"],
            }
            for bucket in buckets
            for bucket_summary in [
                summarize_claude_window(
                    events,
                    pricing_data,
                    bucket["start"],
                    bucket["end"],
                    inclusive_end=(bucket["end"] == now),
                )
            ]
        ]
        return (
            {
                "selected_cost_usd": selected["spend"]["total_cost_usd"],
                "selected_tokens": selected["totals"]["total_billed_tokens"],
                "selected_responses": selected["totals"]["responses"],
                "buckets": buckets_summary,
                "parse_failures": parse_failures,
            },
            issues,
        )
    except Exception as exc:  # pragma: no cover - surfaced to UI
        issues.append(f"Claude: {exc}")
        return (
            {
                "selected_cost_usd": "0",
                "selected_tokens": 0,
                "selected_responses": 0,
                "buckets": [
                    {
                        "start": bucket["start"].isoformat(),
                        "end": bucket["end"].isoformat(),
                        "label": bucket["label"],
                        "cost_usd": "0",
                    }
                    for bucket in buckets
                ],
                "parse_failures": 0,
            },
            issues,
        )


def collect_cursor_data(now: datetime, graph_mode: str) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    buckets, _ = bucket_range(now, graph_mode)
    _, selected_start, selected_end = selected_window(now, graph_mode)

    try:
        csv_args = SimpleNamespace(
            auth_file=cursor.get_default_auth_path(),
            csv_file=None,
            save_csv=None,
        )
        csv_text = cursor.load_csv_text(csv_args)
        rows = cursor.parse_csv_rows(csv_text)
        selected = summarize_cursor_window(rows, selected_start, selected_end, inclusive_end=True)
        buckets_summary = [
            {
                "start": bucket["start"].isoformat(),
                "end": bucket["end"].isoformat(),
                "label": bucket["label"],
                "cost_usd": bucket_summary["totals"]["reported_cost_usd"],
            }
            for bucket in buckets
            for bucket_summary in [
                summarize_cursor_window(
                    rows,
                    bucket["start"],
                    bucket["end"],
                    inclusive_end=(bucket["end"] == now),
                )
            ]
        ]
        return (
            {
                "selected_cost_usd": selected["totals"]["reported_cost_usd"],
                "selected_tokens": selected["totals"]["total_tokens"],
                "selected_events": selected["totals"]["events"],
                "buckets": buckets_summary,
            },
            issues,
        )
    except Exception as exc:  # pragma: no cover - surfaced to UI
        issues.append(f"Cursor: {exc}")
        return (
            {
                "selected_cost_usd": "0",
                "selected_tokens": 0,
                "selected_events": 0,
                "buckets": [
                    {
                        "start": bucket["start"].isoformat(),
                        "end": bucket["end"].isoformat(),
                        "label": bucket["label"],
                        "cost_usd": "0",
                    }
                    for bucket in buckets
                ],
            },
            issues,
        )


def main() -> int:
    args = parse_args()
    now = datetime.now().astimezone()
    graph_meta = GRAPH_MODES[args.graph_mode]
    selected_label, _, _ = selected_window(now, args.graph_mode)
    claude_data, claude_issues = collect_claude_data(
        now, graph_mode=args.graph_mode, exclude_subagents=args.exclude_subagents
    )
    cursor_data, cursor_issues = collect_cursor_data(now, graph_mode=args.graph_mode)

    total_selected = Decimal(str(claude_data["selected_cost_usd"])) + Decimal(
        str(cursor_data["selected_cost_usd"])
    )

    issues = claude_issues + cursor_issues
    payload = {
        "updated_at": int(now.timestamp()),
        "status": "ok" if not issues else "degraded",
        "issues": issues,
        "selected": {
            "label": selected_label,
            "claude_cost_usd": claude_data["selected_cost_usd"],
            "cursor_cost_usd": cursor_data["selected_cost_usd"],
            "total_cost_usd": decimal_text(total_selected),
            "claude_tokens": claude_data["selected_tokens"],
            "cursor_tokens": cursor_data["selected_tokens"],
            "claude_responses": claude_data["selected_responses"],
            "cursor_events": cursor_data["selected_events"],
        },
        "graph": {
            "mode": args.graph_mode,
            "title": graph_meta["title"],
            "unit_suffix": graph_meta["unit_suffix"],
            "note": graph_meta["note"],
        },
        "graphs": {
            "claude": claude_data["buckets"],
            "cursor": cursor_data["buckets"],
        },
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
