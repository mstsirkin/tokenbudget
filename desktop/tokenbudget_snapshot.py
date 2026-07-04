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
import gemini_usage_costs as gemini
from desktop.tokenbudget_config import CONFIG, PROVIDER_LABELS, SUPPORTED_PROVIDERS


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


def empty_gemini_payload(now: datetime) -> dict[str, Any]:
    return gemini.build_all_modes_payload([], {}, now)


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


def collect_gemini_payload(now: datetime) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    try:
        pricing_args = SimpleNamespace(
            pricing_file=None,
            cache_file=gemini.DEFAULT_CACHE_PATH,
            offline=gemini.DEFAULT_CACHE_PATH.exists(),
            pricing_url=gemini.DEFAULT_PRICING_URL,
        )
        pricing_data = gemini.fetch_pricing_json(pricing_args)
        events, parse_failures, files_scanned = gemini.load_usage_events(
            root=gemini.DEFAULT_GEMINI_ROOT,
            since=gemini.all_modes_start(now),
            until=now,
        )
        payload = gemini.build_all_modes_payload(events, pricing_data, now)
        payload["parse_failures"] = parse_failures
        payload["files_scanned"] = files_scanned
        return payload, issues
    except Exception as exc:  # pragma: no cover - surfaced to UI
        issues.append(f"Gemini: {exc}")
        return empty_gemini_payload(now), issues


def combine_mode_payload(
    mode: str,
    provider_modes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    total_selected = Decimal("0")
    graph_meta: dict[str, Any] | None = None
    label: str | None = None
    selected_payload: dict[str, Any] = {}
    graphs_payload: dict[str, Any] = {}

    for provider in SUPPORTED_PROVIDERS:
        provider_mode = provider_modes.get(provider, {})
        selected = provider_mode.get("selected", {})
        if not graph_meta:
            candidate_graph_meta = provider_mode.get("graph")
            if isinstance(candidate_graph_meta, dict):
                graph_meta = candidate_graph_meta
        if label is None:
            candidate_label = selected.get("label")
            if isinstance(candidate_label, str) and candidate_label:
                label = candidate_label
        cost_usd = Decimal(str(selected.get("cost_usd", "0")))
        total_selected += cost_usd
        selected_payload[f"{provider}_cost_usd"] = str(selected.get("cost_usd", "0"))
        selected_payload[f"{provider}_token_breakdown"] = selected.get(
            "token_breakdown",
            {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        )
        graphs_payload[provider] = provider_mode.get("buckets", [])

    if graph_meta is None:
        graph_meta = {
        "mode": mode,
        "title": cursor.GRAPH_MODES[mode]["title"],
        "unit_suffix": cursor.GRAPH_MODES[mode]["unit_suffix"],
        "note": cursor.GRAPH_MODES[mode]["note"],
    }

    return {
        "selected": {
            "label": label or str(cursor.GRAPH_MODES[mode]["selected_label"]),
            "total_cost_usd": decimal_text(total_selected),
            **selected_payload,
        },
        "graph": graph_meta,
        "graphs": graphs_payload,
    }


def main() -> int:
    args = parse_args()
    now = datetime.now().astimezone()
    enabled_providers = set(CONFIG.enabled_providers())
    provider_collectors = {
        "claude": lambda: collect_claude_payload(now, args.exclude_subagents),
        "cursor": lambda: collect_cursor_payload(now),
        "gemini": lambda: collect_gemini_payload(now),
    }
    empty_payloads = {
        "claude": empty_claude_payload(now),
        "cursor": empty_cursor_payload(now),
        "gemini": empty_gemini_payload(now),
    }

    with ThreadPoolExecutor(max_workers=max(1, len(enabled_providers))) as executor:
        futures = {
            provider: executor.submit(collector)
            for provider, collector in provider_collectors.items()
            if provider in enabled_providers
        }
        provider_payloads: dict[str, dict[str, Any]] = {}
        issues: list[str] = []
        for provider in SUPPORTED_PROVIDERS:
            future = futures.get(provider)
            if future is None:
                provider_payloads[provider] = empty_payloads[provider]
                continue
            payload, provider_issues = future.result()
            provider_payloads[provider] = payload
            issues.extend(provider_issues)

    modes = {
        mode: combine_mode_payload(
            mode,
            {
                provider: provider_payloads[provider].get("modes", {}).get(mode, {})
                for provider in SUPPORTED_PROVIDERS
            },
        )
        for mode in cursor.GRAPH_MODES
    }

    payload = {
        "updated_at": int(now.timestamp()),
        "status": "ok" if not issues else "degraded",
        "issues": issues,
        "providers": {
            "enabled": [provider for provider in SUPPORTED_PROVIDERS if provider in enabled_providers],
            "disabled": [
                provider
                for provider in SUPPORTED_PROVIDERS
                if provider not in enabled_providers
            ],
            "labels": PROVIDER_LABELS,
        },
        "modes": modes,
        **modes[args.graph_mode],
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
