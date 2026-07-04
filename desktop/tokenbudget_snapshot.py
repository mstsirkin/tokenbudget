#!/usr/bin/env python3
"""Combine backend all-mode payloads for the Qt tokenbudget monitor."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import claude_usage_costs as claude
import cursor_agent_usage_costs as cursor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute a tokenbudget snapshot from backend all-mode summaries."
    )
    parser.add_argument(
        "--graph-mode",
        choices=tuple(cursor.GRAPH_MODES),
        default="hourly",
        help="Default mode to expose in the legacy top-level fields (default: hourly).",
    )
    parser.add_argument(
        "--exclude-subagents",
        action="store_true",
        help="Exclude Claude subagent transcript files.",
    )
    return parser.parse_args()


def decimal_text(value: Decimal) -> str:
    return str(value)


def empty_claude_payload(now: datetime) -> dict[str, Any]:
    return claude.build_all_modes_payload([], {}, now)


def empty_cursor_payload(now: datetime) -> dict[str, Any]:
    return cursor.build_all_modes_payload([], now)


def collect_claude_payload(now: datetime, exclude_subagents: bool) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
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
            since=claude.all_modes_start(now),
            until=now,
        )
        payload = claude.build_all_modes_payload(events, pricing_data, now)
        payload["parse_failures"] = parse_failures
        return payload, issues
    except Exception as exc:  # pragma: no cover - surfaced to UI
        issues.append(f"Claude: {exc}")
        return empty_claude_payload(now), issues


def collect_cursor_payload(now: datetime) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    try:
        csv_args = SimpleNamespace(
            auth_file=cursor.get_default_auth_path(),
            csv_file=None,
            save_csv=None,
        )
        rows = cursor.parse_csv_rows(cursor.load_csv_text(csv_args))
        return cursor.build_all_modes_payload(rows, now), issues
    except Exception as exc:  # pragma: no cover - surfaced to UI
        issues.append(f"Cursor: {exc}")
        return empty_cursor_payload(now), issues


def combine_mode_payload(
    mode: str,
    claude_mode: dict[str, Any],
    cursor_mode: dict[str, Any],
) -> dict[str, Any]:
    claude_selected = claude_mode.get("selected", {})
    cursor_selected = cursor_mode.get("selected", {})
    total_selected = Decimal(str(claude_selected.get("cost_usd", "0"))) + Decimal(
        str(cursor_selected.get("cost_usd", "0"))
    )
    graph_meta = claude_mode.get("graph") or cursor_mode.get("graph") or {
        "mode": mode,
        "title": cursor.GRAPH_MODES[mode]["title"],
        "unit_suffix": cursor.GRAPH_MODES[mode]["unit_suffix"],
        "note": cursor.GRAPH_MODES[mode]["note"],
    }
    label = (
        claude_selected.get("label")
        or cursor_selected.get("label")
        or str(cursor.GRAPH_MODES[mode]["selected_label"])
    )
    return {
        "selected": {
            "label": label,
            "claude_cost_usd": str(claude_selected.get("cost_usd", "0")),
            "cursor_cost_usd": str(cursor_selected.get("cost_usd", "0")),
            "total_cost_usd": decimal_text(total_selected),
            "claude_token_breakdown": claude_selected.get(
                "token_breakdown",
                {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
            ),
            "cursor_token_breakdown": cursor_selected.get(
                "token_breakdown",
                {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
            ),
        },
        "graph": graph_meta,
        "graphs": {
            "claude": claude_mode.get("buckets", []),
            "cursor": cursor_mode.get("buckets", []),
        },
    }


def main() -> int:
    args = parse_args()
    now = datetime.now().astimezone()
    with ThreadPoolExecutor(max_workers=2) as executor:
        claude_future = executor.submit(
            collect_claude_payload, now, args.exclude_subagents
        )
        cursor_future = executor.submit(collect_cursor_payload, now)
        claude_payload, claude_issues = claude_future.result()
        cursor_payload, cursor_issues = cursor_future.result()
    issues = claude_issues + cursor_issues

    modes = {
        mode: combine_mode_payload(
            mode,
            claude_payload.get("modes", {}).get(mode, {}),
            cursor_payload.get("modes", {}).get(mode, {}),
        )
        for mode in cursor.GRAPH_MODES
    }

    payload = {
        "updated_at": int(now.timestamp()),
        "status": "ok" if not issues else "degraded",
        "issues": issues,
        "modes": modes,
        **modes[args.graph_mode],
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
