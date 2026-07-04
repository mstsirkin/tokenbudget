from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from functools import lru_cache


def parse_when(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    try:
        return _parse_iso_value(text, end_of_day=end_of_day)
    except ValueError:
        pass

    try:
        return _parse_date_expression(text)
    except ValueError as exc:
        raise ValueError(
            f"could not parse {value!r} as ISO-8601 or GNU date expression"
        ) from exc


def _parse_iso_value(text: str, *, end_of_day: bool) -> datetime:
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


def _parse_date_expression(text: str) -> datetime:
    last_error: str | None = None
    for binary in _candidate_date_binaries():
        try:
            result = subprocess.run(
                [binary, "--date", text, "+%Y-%m-%dT%H:%M:%S%z"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            last_error = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            continue

        output = result.stdout.strip()
        try:
            dt = datetime.strptime(output, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError as exc:
            last_error = f"{binary} returned an unexpected timestamp {output!r}"
            continue
        return dt.astimezone(UTC)

    if last_error is None:
        raise ValueError("no GNU date-compatible command found")
    raise ValueError(last_error)


@lru_cache(maxsize=1)
def _candidate_date_binaries() -> tuple[str, ...]:
    paths: list[str] = []
    for name in ("gdate", "date"):
        path = shutil.which(name)
        if path and path not in paths:
            paths.append(path)
    return tuple(paths)
