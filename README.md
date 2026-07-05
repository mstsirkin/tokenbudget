# tokenbudget

`tokenbudget` is a desktop widget for tracking token usage across local AI agents:
Claude Code, Cursor CLI, and Gemini CLI.

What it does: shows recent usage, token breakdowns, and spend/cost across the local agents.

What it does not do: cloud spend/tracking across systems.


![tokenbudget Qt monitor](desktop/tokenbudget-qt-screenshot.png)

## What It Does

The app is a transparent Qt desktop monitor that:

- shows separate totals for Claude, Cursor CLI, and Gemini
- graphs recent usage in `hourly`, `daily`, `weekly`, and `monthly` views
- displays token breakdowns alongside spend/cost
- can stay pinned on top, live in the system tray, and refresh in the background
- remembers its window position and selected graph mode

## Supported Agents

### Claude Code

- reads local transcripts under `~/.claude/projects`
- estimates spend using `models.dev` Anthropic pricing data

### Cursor CLI

- uses your local Cursor auth plus Cursor's usage export
- shows token totals and the reported cost from Cursor's export data

### Gemini CLI

- reads local session data under `~/.gemini`
- estimates spend using `models.dev` Google pricing data

## Usage

If you just want to use `tokenbudget`, download the latest release from the
repository's Releases page and run the packaged app.

## Optional Config

The GUI reads optional settings from
`~/.config/tokenbudget/qt-monitor.rc.py`.

Example:

```python
WINDOW_SIZE = (440, 690)
DISABLED_PROVIDERS = {"cursor"}
```

Notes:

- all providers are enabled by default
- `DISABLED_PROVIDERS` can include any of `claude`, `cursor`, and `gemini`

## Running From Source

If you are developing or testing from source instead of using a release:

```bash
dnf install -y python3-pyside6 python3-dateparser
./desktop/run-tokenbudget-qt.sh
```

Useful options:

- `./desktop/run-tokenbudget-qt.sh --poll-seconds 60`
- `./desktop/run-tokenbudget-qt.sh --graph-mode hourly`
- `./desktop/run-tokenbudget-qt.sh --exclude-subagents`

## Backend Scripts

The GUI is powered by three backend scripts that can also be run directly:

- `claude_usage_costs.py`
- `cursor_agent_usage_costs.py`
- `gemini_usage_costs.py`

These scripts support `--since` and `--until` filters with ISO-8601 timestamps
or natural-language expressions such as `2026-07-03`, `yesterday`, and
`1 hour ago`.

## Packaging

To build a standalone executable from source:

```bash
make build-deps
make onefile
```

The output binary is written to `dist/tokenbudget`.
