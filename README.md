# tokenbudget

Small command-line utilities for summarizing AI token usage and cost from
local Claude Code transcripts and Cursor dashboard exports.

## Scripts

### `claude_usage_costs.py`

Scans transcript files under `~/.claude/projects`, extracts per-response usage
records, de-duplicates repeated request entries, and estimates spend from
Anthropic pricing data fetched from `models.dev` or a local cache.

### `cursor_agent_usage_costs.py`

Fetches Cursor Agent usage as a CSV export from Cursor's dashboard API or reads
from a saved CSV file, then summarizes token totals and reported cost by event
kind and model.

Both scripts accept `--since` and `--until` filters using ISO-8601 timestamps
or natural-language expressions such as `2026-07-03`, `yesterday`, and
`1 hour ago`.

Natural-language parsing uses the Python `dateparser` package. On Fedora, install
it with `sudo dnf install python3-dateparser`.
