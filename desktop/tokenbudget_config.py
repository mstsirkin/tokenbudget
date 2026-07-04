#!/usr/bin/env python3
"""Shared config loading for the tokenbudget Qt monitor."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys
from typing import Any


RC_PATH = Path.home() / ".config" / "tokenbudget" / "qt-monitor.rc.py"
SUPPORTED_PROVIDERS = ("claude", "cursor", "gemini")
PROVIDER_LABELS = {
    "claude": "Claude",
    "cursor": "Cursor CLI",
    "gemini": "Gemini",
}
DEFAULT_WINDOW_SIZE = (440, 690)
# Example `~/.config/tokenbudget/qt-monitor.rc.py`:
# SCALE = 2  # correct token double counting
# WINDOW_SIZE = (440, 690)
# DISABLED_PROVIDERS = {"cursor"}


@dataclass(frozen=True)
class TokenbudgetConfig:
    scale: Decimal | None = None
    window_size: tuple[int, int] = DEFAULT_WINDOW_SIZE
    disabled_providers: frozenset[str] = frozenset()

    def provider_enabled(self, provider: str) -> bool:
        return provider in SUPPORTED_PROVIDERS and provider not in self.disabled_providers

    def enabled_providers(self) -> tuple[str, ...]:
        return tuple(
            provider
            for provider in SUPPORTED_PROVIDERS
            if self.provider_enabled(provider)
        )


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _normalize_window_size(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    try:
        width = int(value[0])
        height = int(value[1])
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _normalize_disabled_providers(value: Any) -> frozenset[str] | None:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (tuple, list, set, frozenset)):
        items = list(value)
    else:
        return None

    disabled: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            return None
        provider = item.strip().lower()
        if not provider:
            continue
        if provider not in SUPPORTED_PROVIDERS:
            _warn(
                f"ignoring unknown provider {provider!r} in {RC_PATH}; "
                f"known providers: {', '.join(SUPPORTED_PROVIDERS)}"
            )
            continue
        disabled.add(provider)
    return frozenset(disabled)


def load_rc_config() -> TokenbudgetConfig:
    if not RC_PATH.exists():
        return TokenbudgetConfig()

    namespace: dict[str, Any] = {"Decimal": Decimal}
    try:
        exec(compile(RC_PATH.read_text(encoding="utf-8"), str(RC_PATH), "exec"), namespace)
    except Exception as exc:  # pragma: no cover - visible in launcher stderr
        _warn(f"failed to load {RC_PATH}: {exc}")
        return TokenbudgetConfig()

    scale: Decimal | None = None
    scale_value = namespace.get("SCALE")
    if scale_value is not None:
        try:
            scale = Decimal(str(scale_value))
        except (InvalidOperation, TypeError, ValueError):
            _warn(f"ignoring invalid SCALE in {RC_PATH}")

    window_size = DEFAULT_WINDOW_SIZE
    window_size_value = namespace.get("WINDOW_SIZE")
    if window_size_value is not None:
        normalized_window_size = _normalize_window_size(window_size_value)
        if normalized_window_size is None:
            _warn(f"ignoring invalid WINDOW_SIZE in {RC_PATH}")
        else:
            window_size = normalized_window_size

    disabled_providers = frozenset()
    disabled_providers_value = namespace.get("DISABLED_PROVIDERS")
    if disabled_providers_value is not None:
        normalized_disabled = _normalize_disabled_providers(disabled_providers_value)
        if normalized_disabled is None:
            _warn(
                f"ignoring invalid DISABLED_PROVIDERS in {RC_PATH}; "
                "use a list, tuple, or set of provider names"
            )
        else:
            disabled_providers = normalized_disabled

    return TokenbudgetConfig(
        scale=scale,
        window_size=window_size,
        disabled_providers=disabled_providers,
    )


CONFIG = load_rc_config()
