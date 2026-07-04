#!/usr/bin/env python3
"""Native draggable desktop monitor for tokenbudget."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QProcess, QRectF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
try:
    from desktop.tokenbudget_config import CONFIG, PROVIDER_LABELS, RC_PATH
except ModuleNotFoundError:
    from tokenbudget_config import CONFIG, PROVIDER_LABELS, RC_PATH

SNAPSHOT_HELPER = REPO_ROOT / "desktop" / "tokenbudget_snapshot.py"
SETTINGS_GROUP = "qt-monitor"
WINDOW_SIZE = QSize(*CONFIG.window_size)
MONEY_QUANTUM = Decimal("0.1")
# Quick display-only workaround for suspected inflation.
SCALE: Decimal | None = CONFIG.scale
PROVIDER_ORDER = tuple(PROVIDER_LABELS)
GRAPH_MODE_LABELS = {
    "hourly": "Hourly",
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
}


def scaled_decimal(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if SCALE is None or SCALE == 0:
        return amount
    return amount / SCALE


def scaled_float(value: Any) -> float:
    return float(scaled_decimal(value))


class SpendHistoryGraph(QWidget):
    """Single-series graph for direct buckets."""

    def __init__(self, title: str, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title
        self.color = QColor(color)
        self.unit_suffix = "h"
        self._series: list[tuple[str, float]] = []
        self.setMinimumHeight(135)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_series(self, series: list[tuple[str, float]], *, unit_suffix: str) -> None:
        self._series = series
        self.unit_suffix = unit_suffix
        self.update()

    def paintEvent(self, event: QEvent) -> None:  # type: ignore[override]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        base_font = painter.font()
        label_font = painter.font()
        label_font.setPixelSize(11)
        title_font = painter.font()
        title_font.setPixelSize(11)
        title_font.setBold(True)

        plot_rect = QRectF(14, 12, self.width() - 28, self.height() - 26)
        label_width = 68
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 18))
        painter.drawRoundedRect(plot_rect, 12, 12)

        painter.setFont(title_font)
        painter.setPen(QColor("#c0c8d6"))
        painter.drawText(
            QRectF(plot_rect.left() + 12, plot_rect.top() + 8, plot_rect.width() - 24, 18),
            Qt.AlignmentFlag.AlignLeft,
            self.title,
        )

        if not self._series:
            painter.setFont(label_font)
            painter.drawText(plot_rect, Qt.AlignmentFlag.AlignCenter, "No data")
            return

        values = [value for _, value in self._series]
        latest_value = values[-1]
        max_value = self._axis_ceiling(max(values))
        bottom = plot_rect.bottom() - 22
        top = plot_rect.top() + 28
        left = plot_rect.left() + label_width
        right = plot_rect.right() - 10
        width = max(1.0, right - left)
        height = max(1.0, bottom - top)

        grid_pen = QPen(QColor(255, 255, 255, 28))
        grid_pen.setWidthF(1.0)
        painter.setPen(grid_pen)
        for step in range(5):
            ratio = step / 4.0
            y = top + (height * ratio)
            tick_value = max_value * (1.0 - ratio)
            painter.setFont(label_font)
            painter.setPen(QColor("#9aa7b8"))
            painter.drawText(
                QRectF(plot_rect.left() + 2, y - 8, label_width - 8, 16),
                Qt.AlignmentFlag.AlignRight,
                self._axis_label(tick_value),
            )
            painter.setPen(grid_pen)
            painter.drawLine(left, y, right, y)

        label_pen = QPen(QColor("#9aa7b8"))
        painter.setFont(label_font)
        painter.setPen(label_pen)
        painter.drawText(
            QRectF(left, plot_rect.top() + 8, width, 18),
            Qt.AlignmentFlag.AlignRight,
            f"{self._format_money_float(latest_value)}/{self.unit_suffix}",
        )
        painter.drawText(QRectF(left, bottom + 6, width / 2, 16), self._series[0][0])
        painter.drawText(
            QRectF(left + width / 2, bottom + 6, width / 2, 16),
            Qt.AlignmentFlag.AlignRight,
            self._series[-1][0],
        )

        path = QPainterPath()
        count = len(self._series)
        for index, (_, value) in enumerate(self._series):
            x = left + (width / 2.0 if count == 1 else width * index / (count - 1))
            y = top + height - ((value / max_value) * height)
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        fill_path = QPainterPath(path)
        fill_path.lineTo(left + width, top + height)
        fill_path.lineTo(left, top + height)
        fill_path.closeSubpath()
        fill_color = QColor(self.color)
        fill_color.setAlpha(44)
        painter.fillPath(fill_path, fill_color)

        pen = QPen(self.color)
        pen.setWidthF(2.5)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.setFont(base_font)

    @staticmethod
    def _axis_label(value: float) -> str:
        if value >= 1000:
            return f"${value / 1000:.1f}k"
        if value >= 100:
            return f"${value:.0f}"
        return SpendHistoryGraph._format_money_float(value)

    @staticmethod
    def _format_money_float(value: float) -> str:
        amount = Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        return f"${amount:,}"

    @staticmethod
    def _axis_ceiling(value: float) -> float:
        if value <= 0:
            return 1.0
        magnitude = 10 ** math.floor(math.log10(value))
        increment = magnitude / 2.0
        return math.ceil((value - 1e-12) / increment) * increment


class TokenbudgetWindow(QWidget):
    def __init__(self, *, poll_seconds: int, graph_mode: str) -> None:
        super().__init__()
        self.poll_seconds = poll_seconds
        self.settings = QSettings("tokenbudget", SETTINGS_GROUP)
        saved_mode = self.settings.value("graph_mode", graph_mode, type=str)
        self.graph_mode = saved_mode if saved_mode in GRAPH_MODE_LABELS else graph_mode
        self._drag_offset: QPoint | None = None
        self._pinned = self.settings.value("pinned", True, type=bool)
        self._quit_requested = False
        self.tray_icon: QSystemTrayIcon | None = None
        self._snapshot_cache: dict[str, Any] | None = None

        self.process = QProcess(self)
        self.process.finished.connect(self._handle_snapshot_finished)

        self.setWindowTitle("tokenbudget")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._apply_window_flags()
        self.resize(WINDOW_SIZE)
        self.setMinimumWidth(WINDOW_SIZE.width())
        self.setMaximumWidth(WINDOW_SIZE.width())

        self._build_ui()
        self._apply_provider_visibility(set(PROVIDER_ORDER))
        self._restore_position()
        self._setup_tray()

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(max(10, self.poll_seconds) * 1000)
        self.poll_timer.timeout.connect(self.request_snapshot)
        self.poll_timer.start()
        self.request_snapshot(force=True)

    def _apply_window_flags(self) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._pinned:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)

    def _apply_pinned_state(self) -> None:
        old_pos = self.pos()
        self.setUpdatesEnabled(False)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self._pinned)
        self.show()
        self.move(old_pos)
        self.raise_()
        self.setUpdatesEnabled(True)
        self.update()

    def _create_tray_icon(self) -> QIcon:
        size = 32
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        card_rect = QRectF(3, 3, size - 6, size - 6)
        painter.setPen(QPen(QColor(255, 255, 255, 60), 1.2))
        painter.setBrush(QColor(18, 22, 30, 220))
        painter.drawRoundedRect(card_rect, 8, 8)

        painter.setPen(QPen(QColor("#66c2ff"), 2.4))
        painter.drawLine(8, 22, 13, 15)
        painter.drawLine(13, 15, 18, 18)
        painter.drawLine(18, 18, 24, 10)

        painter.setPen(QPen(QColor("#7fdc8a"), 2.0))
        painter.drawLine(8, 25, 24, 25)
        painter.end()
        return QIcon(pixmap)

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self.tray_icon = QSystemTrayIcon(self._create_tray_icon(), self)
        self.tray_icon.setToolTip("tokenbudget")
        self.tray_icon.activated.connect(self._tray_activated)
        self.close_button.setToolTip("Hide to tray")

        menu = QMenu(self)
        self.tray_show_action = QAction("Show/Raise", self)
        self.tray_show_action.triggered.connect(self.show_and_raise)
        menu.addAction(self.tray_show_action)

        self.tray_hide_action = QAction("Hide", self)
        self.tray_hide_action.triggered.connect(self.hide_to_tray)
        menu.addAction(self.tray_hide_action)

        self.tray_pin_action = QAction(self)
        self.tray_pin_action.triggered.connect(self.toggle_pinned)
        menu.addAction(self.tray_pin_action)

        self.tray_refresh_action = QAction("Refresh now", self)
        self.tray_refresh_action.triggered.connect(lambda: self.request_snapshot(force=True))
        menu.addAction(self.tray_refresh_action)

        self.tray_restart_action = QAction("Restart", self)
        self.tray_restart_action.triggered.connect(self.restart_application)
        menu.addAction(self.tray_restart_action)

        menu.addSeparator()
        self.tray_quit_action = QAction("Quit", self)
        self.tray_quit_action.triggered.connect(self.quit_application)
        menu.addAction(self.tray_quit_action)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.show()
        self._sync_tray_actions()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self.show_and_raise()

    def _sync_tray_actions(self) -> None:
        if self.tray_icon is None:
            return
        self.tray_show_action.setText("Raise" if self.isVisible() else "Show/Raise")
        self.tray_hide_action.setEnabled(self.isVisible())
        self.tray_pin_action.setText("Unpin from top" if self._pinned else "Pin on top")

    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self._sync_tray_actions()

    def hide_to_tray(self) -> None:
        self.hide()
        self._sync_tray_actions()

    def quit_application(self) -> None:
        self._quit_requested = True
        if self.tray_icon is not None:
            self.tray_icon.hide()
        self.close()
        QApplication.instance().quit()

    def restart_application(self) -> None:
        result = QProcess.startDetached(
            sys.executable,
            [str(Path(__file__).resolve()), *sys.argv[1:]],
            str(REPO_ROOT),
        )
        started = bool(result[0]) if isinstance(result, tuple) else bool(result)
        if not started:
            self.status_label.setText("Restart failed")
            self.status_label.show()
            return
        self.quit_application()

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        self.card = QFrame(self)
        self.card.setObjectName("card")
        self.card.setStyleSheet(
            """
            #card {
                background-color: rgba(18, 22, 30, 212);
                border: 1px solid rgba(255, 255, 255, 36);
                border-radius: 18px;
            }
            QLabel {
                color: #e5e9f0;
            }
            QToolButton {
                color: #d8dee9;
                background-color: rgba(255, 255, 255, 28);
                border: 1px solid rgba(255, 255, 255, 40);
                border-radius: 9px;
                padding: 4px 8px;
            }
            QToolButton:hover {
                background-color: rgba(255, 255, 255, 42);
            }
            QToolButton#pinButton {
                min-width: 38px;
                max-width: 38px;
                min-height: 32px;
                max-height: 32px;
                font-size: 18px;
                padding: 0px 0px 8px 0px;
            }
            QToolButton#pinButton:checked {
                background-color: rgba(255, 255, 255, 28);
                border: 1px solid rgba(255, 255, 255, 40);
                color: #d8dee9;
                padding-top: 10px;
                padding-bottom: 0px;
                font-size: 18px;
            }
            QToolButton#closeButton {
                min-width: 18px;
                max-width: 18px;
                min-height: 18px;
                max-height: 18px;
                font-size: 8px;
                padding: 0px;
            }
            """
        )
        root_layout.addWidget(self.card)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(14, 12, 14, 14)
        card_layout.setSpacing(10)

        self.header = QFrame(self.card)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        title_column = QVBoxLayout()
        title_column.setContentsMargins(0, 0, 0, 0)
        title_column.setSpacing(0)

        self.title_label = QLabel("tokenbudget", self.header)
        self.title_label.setStyleSheet("font-size: 18px; font-weight: 700;")
        title_column.addWidget(self.title_label)

        self.subtitle_label = QLabel("Starting...", self.header)
        self.subtitle_label.setStyleSheet("color: #9aa7b8; font-size: 11px;")
        title_column.addWidget(self.subtitle_label)
        header_layout.addLayout(title_column)
        header_layout.addStretch(1)

        self.graph_mode_combo = QComboBox(self.header)
        for key, label in GRAPH_MODE_LABELS.items():
            self.graph_mode_combo.addItem(label, key)
        current_index = max(0, self.graph_mode_combo.findData(self.graph_mode))
        self.graph_mode_combo.setCurrentIndex(current_index)
        self.graph_mode_combo.currentIndexChanged.connect(self._graph_mode_changed)
        header_layout.addWidget(self.graph_mode_combo)

        self.pin_button = QToolButton(self.header)
        self.pin_button.setObjectName("pinButton")
        self.pin_button.setCheckable(True)
        self.pin_button.setToolTip("Toggle always-on-top")
        self.pin_button.clicked.connect(self.toggle_pinned)
        self._update_pin_button()
        header_layout.addWidget(self.pin_button)

        self.refresh_button = QToolButton(self.header)
        self.refresh_button.setText("↻")
        self.refresh_button.setToolTip("Refresh now")
        self.refresh_button.clicked.connect(lambda: self.request_snapshot(force=True))
        header_layout.addWidget(self.refresh_button)

        self.close_button = QToolButton(self.header)
        self.close_button.setObjectName("closeButton")
        self.close_button.setText("❌")
        self.close_button.setToolTip("Close monitor")
        self.close_button.clicked.connect(self.close)
        header_layout.addWidget(self.close_button)

        card_layout.addWidget(self.header)
        for widget in (self.header, self.title_label, self.subtitle_label):
            widget.installEventFilter(self)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(12)
        metrics.setVerticalSpacing(4)
        self.period_total_label, self.period_total_value = self._add_metric(metrics, 0, "Period total")
        self.claude_spend_label, self.claude_spend_value = self._add_metric(metrics, 1, "Claude spend")
        self.cursor_spend_label, self.cursor_spend_value = self._add_metric(metrics, 2, "Cursor spend")
        self.claude_tokens_label, self.claude_tokens_value = self._add_metric(metrics, 3, "Claude tokens")
        self.cursor_tokens_label, self.cursor_tokens_value = self._add_metric(metrics, 4, "Cursor tokens")
        card_layout.addLayout(metrics)

        text_block_width = WINDOW_SIZE.width() - 40

        self.graph_title = QLabel("Last 24 hourly buckets", self.card)
        self.graph_title.setStyleSheet("font-size: 12px; font-weight: 600; color: #c0c8d6;")
        self.graph_title.setMaximumWidth(text_block_width)
        self.graph_title.setFixedHeight(18)
        card_layout.addWidget(self.graph_title)

        self.graph_note = QLabel("Each point is one hourly bucket, computed directly on refresh.", self.card)
        self.graph_note.setStyleSheet("color: #8f9db0; font-size: 11px;")
        self.graph_note.setWordWrap(True)
        self.graph_note.setMaximumWidth(text_block_width)
        self.graph_note.setFixedHeight(32)
        card_layout.addWidget(self.graph_note)

        self.claude_graph = SpendHistoryGraph("Claude", "#66c2ff", self.card)
        card_layout.addWidget(self.claude_graph)
        self.cursor_graph = SpendHistoryGraph("Cursor", "#7fdc8a", self.card)
        card_layout.addWidget(self.cursor_graph)
        self._provider_metric_widgets = {
            "claude": (
                self.claude_spend_label,
                self.claude_spend_value,
                self.claude_tokens_label,
                self.claude_tokens_value,
            ),
            "cursor": (
                self.cursor_spend_label,
                self.cursor_spend_value,
                self.cursor_tokens_label,
                self.cursor_tokens_value,
            ),
        }
        self._provider_graph_widgets = {
            "claude": self.claude_graph,
            "cursor": self.cursor_graph,
        }

        self.status_label = QLabel("", self.card)
        self.status_label.setStyleSheet("color: #8f9db0; font-size: 11px;")
        self.status_label.hide()
        card_layout.addWidget(self.status_label)

    def _add_metric(self, layout: QGridLayout, row: int, label_text: str) -> tuple[QLabel, QLabel]:
        label = QLabel(label_text, self.card)
        label.setStyleSheet("color: #aeb8c7; font-size: 12px;")
        value = QLabel("--", self.card)
        value.setStyleSheet("font-size: 13px; font-weight: 600;")
        layout.addWidget(label, row, 0)
        layout.addWidget(value, row, 1)
        return label, value

    @staticmethod
    def _enabled_providers_from_snapshot(snapshot: dict[str, Any]) -> set[str]:
        providers = snapshot.get("providers")
        if isinstance(providers, dict):
            enabled = providers.get("enabled")
            if isinstance(enabled, list):
                return {
                    provider
                    for provider in enabled
                    if isinstance(provider, str) and provider in PROVIDER_ORDER
                }
        return set(PROVIDER_ORDER)

    def _apply_provider_visibility(self, enabled_providers: set[str]) -> None:
        for provider in PROVIDER_ORDER:
            visible = provider in enabled_providers
            for widget in self._provider_metric_widgets.get(provider, ()):
                widget.setVisible(visible)
            graph_widget = self._provider_graph_widgets.get(provider)
            if graph_widget is not None:
                graph_widget.setVisible(visible)

    def toggle_pinned(self) -> None:
        self._pinned = not self._pinned
        self._apply_pinned_state()
        self.settings.setValue("pinned", self._pinned)
        self._update_pin_button()
        self._sync_tray_actions()

    def _update_pin_button(self) -> None:
        self.pin_button.setChecked(self._pinned)
        self.pin_button.setText("📍")
        self.pin_button.setToolTip(
            "Pinned on top" if self._pinned else "Not pinned on top"
        )

    def request_snapshot(self, *, force: bool = False) -> None:
        if self.process.state() != QProcess.ProcessState.NotRunning:
            return
        del force
        self.process.start(
            sys.executable,
            [str(SNAPSHOT_HELPER), "--graph-mode", self.graph_mode],
        )
        self.subtitle_label.setText("Refreshing...")

    def _handle_snapshot_finished(self) -> None:
        if self.process.exitStatus() != QProcess.ExitStatus.NormalExit or self.process.exitCode() != 0:
            stderr = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace").strip()
            self.status_label.setText(stderr or "Snapshot refresh failed")
            self.subtitle_label.setText("Refresh error")
            self.status_label.show()
            return

        stdout = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        try:
            snapshot = json.loads(stdout)
        except json.JSONDecodeError:
            self.status_label.setText("Snapshot helper returned invalid JSON")
            self.subtitle_label.setText("Refresh error")
            self.status_label.show()
            return
        if not isinstance(snapshot, dict):
            self.status_label.setText("Snapshot helper returned the wrong data shape")
            self.subtitle_label.setText("Refresh error")
            self.status_label.show()
            return
        self._snapshot_cache = snapshot
        self._apply_snapshot(snapshot)

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        enabled_providers = self._enabled_providers_from_snapshot(snapshot)
        self._apply_provider_visibility(enabled_providers)
        modes = snapshot.get("modes")
        if isinstance(modes, dict):
            mode_payload = modes.get(self.graph_mode)
            if isinstance(mode_payload, dict):
                selected = mode_payload.get("selected", {})
                graph_meta = mode_payload.get("graph", {})
                graphs = mode_payload.get("graphs", {})
            else:
                selected = {}
                graph_meta = {}
                graphs = {}
        else:
            selected = snapshot.get("selected", {})
            graph_meta = snapshot.get("graph", {})
            graphs = snapshot.get("graphs", {})

        selected_label = str(selected.get("label", "Selected period"))
        self.period_total_label.setText(f"{selected_label} total")
        self.claude_spend_label.setText(f"{PROVIDER_LABELS['claude']} spend")
        self.cursor_spend_label.setText(f"{PROVIDER_LABELS['cursor']} spend")
        self.claude_tokens_label.setText(f"{PROVIDER_LABELS['claude']} tokens")
        self.cursor_tokens_label.setText(f"{PROVIDER_LABELS['cursor']} tokens")

        self.period_total_value.setText(self._format_money(selected.get("total_cost_usd", 0)))
        self.claude_spend_value.setText(self._format_money(selected.get("claude_cost_usd", 0)))
        self.cursor_spend_value.setText(self._format_money(selected.get("cursor_cost_usd", 0)))
        self.claude_tokens_value.setText(
            self._format_token_breakdown(selected.get("claude_token_breakdown"))
        )
        self.cursor_tokens_value.setText(
            self._format_token_breakdown(selected.get("cursor_token_breakdown"))
        )

        unit_suffix = str(graph_meta.get("unit_suffix", "h"))
        self.graph_title.setText(str(graph_meta.get("title", "Buckets")))
        self.graph_note.setText(str(graph_meta.get("note", "")))
        self.claude_graph.set_series(
            self._series_from_graph(graphs.get("claude", [])),
            unit_suffix=unit_suffix,
        )
        self.cursor_graph.set_series(
            self._series_from_graph(graphs.get("cursor", [])),
            unit_suffix=unit_suffix,
        )

        updated_at = int(snapshot.get("updated_at", 0) or 0)
        self.subtitle_label.setText(f"Updated {self._human_age(updated_at)}")
        issues = snapshot.get("issues") or []
        if not enabled_providers:
            self.status_label.setText(f"All providers disabled in {RC_PATH}")
            self.status_label.show()
        elif issues:
            self.status_label.setText(str(issues[0]))
            self.status_label.show()
        else:
            self.status_label.clear()
            self.status_label.hide()

    @staticmethod
    def _series_from_graph(items: Any) -> list[tuple[str, float]]:
        if not isinstance(items, list):
            return []
        series: list[tuple[str, float]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            label = item.get("label")
            if not isinstance(label, str):
                continue
            try:
                value = scaled_float(item.get("cost_usd", 0) or 0)
            except (InvalidOperation, TypeError, ValueError):
                continue
            series.append((label, value))
        return series

    def _graph_mode_changed(self) -> None:
        mode = self.graph_mode_combo.currentData()
        if not isinstance(mode, str) or mode == self.graph_mode:
            return
        self.graph_mode = mode
        self.settings.setValue("graph_mode", mode)
        if self._snapshot_cache is not None and isinstance(self._snapshot_cache.get("modes"), dict):
            self._apply_snapshot(self._snapshot_cache)
            return
        self.request_snapshot(force=True)

    def _restore_position(self) -> None:
        saved_pos = self.settings.value("position")
        if isinstance(saved_pos, QPoint):
            self.move(saved_pos)
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.move(available.right() - self.width() - 24, available.top() + 24)

    def closeEvent(self, event: QEvent) -> None:  # type: ignore[override]
        self.settings.setValue("position", self.pos())
        self.settings.setValue("pinned", self._pinned)
        if self.tray_icon is not None and not self._quit_requested:
            event.ignore()
            self.hide_to_tray()
            return
        super().closeEvent(event)  # type: ignore[arg-type]

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if watched in {self.header, self.title_label, self.subtitle_label}:
            if event.type() == QEvent.Type.MouseButtonPress:
                mouse_event = event  # type: ignore[assignment]
                if isinstance(mouse_event, QMouseEvent) and mouse_event.button() == Qt.MouseButton.LeftButton:
                    self._drag_offset = mouse_event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                    return True
            if event.type() == QEvent.Type.MouseMove and self._drag_offset is not None:
                mouse_event = event  # type: ignore[assignment]
                if isinstance(mouse_event, QMouseEvent):
                    self.move(mouse_event.globalPosition().toPoint() - self._drag_offset)
                    return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._drag_offset = None
                self.settings.setValue("position", self.pos())
                return True
        return super().eventFilter(watched, event)

    def contextMenuEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        menu = QMenu(self)
        refresh_action = QAction("Refresh now", self)
        refresh_action.triggered.connect(lambda: self.request_snapshot(force=True))
        menu.addAction(refresh_action)

        pin_text = "Disable always-on-top" if self._pinned else "Enable always-on-top"
        pin_action = QAction(pin_text, self)
        pin_action.triggered.connect(self.toggle_pinned)
        menu.addAction(pin_action)

        reset_action = QAction("Reset position", self)
        reset_action.triggered.connect(self._reset_position)
        menu.addAction(reset_action)

        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit_application)
        menu.addAction(quit_action)
        menu.exec(event.globalPos())

    def _reset_position(self) -> None:
        self.settings.remove("position")
        self._restore_position()

    @staticmethod
    def _format_money(value: Any) -> str:
        amount = scaled_decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        return f"${amount:,}"

    @staticmethod
    def _format_int(value: Any) -> str:
        return f"{int(value):,}"

    @staticmethod
    def _format_compact_int(value: Any) -> str:
        amount = scaled_decimal(value)
        sign = "-" if amount < 0 else ""
        amount = abs(amount)
        suffixes = [
            (Decimal("1000000000000"), "T"),
            (Decimal("1000000000"), "B"),
            (Decimal("1000000"), "M"),
            (Decimal("1000"), "k"),
        ]
        for threshold, suffix in suffixes:
            if amount >= threshold:
                compact = amount / threshold
                if compact >= 100:
                    text = f"{compact.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,}"
                else:
                    text = f"{compact.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):,}"
                    text = text.rstrip("0").rstrip(".")
                return f"{sign}{text}{suffix}"
        if amount == amount.to_integral_value(rounding=ROUND_HALF_UP):
            return f"{sign}{int(amount):,}"
        quantum = Decimal("0.01") if amount < 10 else Decimal("0.1")
        text = f"{amount.quantize(quantum, rounding=ROUND_HALF_UP):,}"
        text = text.rstrip("0").rstrip(".")
        return f"{sign}{text}"

    @classmethod
    def _format_token_breakdown(cls, value: Any) -> str:
        if not isinstance(value, dict):
            return "--"
        parts = [
            ("I", value.get("input", 0)),
            ("O", value.get("output", 0)),
            ("CR", value.get("cache_read", 0)),
            ("CW", value.get("cache_write", 0)),
        ]
        return "  ".join(
            f"{label} {'-' if amount is None else cls._format_compact_int(amount)}"
            for label, amount in parts
        )

    @staticmethod
    def _human_age(updated_at: int) -> str:
        if updated_at <= 0:
            return "never"
        age = max(0, int(datetime.now(tz=UTC).timestamp() - updated_at))
        if age < 60:
            return f"{age}s ago"
        if age < 3600:
            return f"{age // 60}m ago"
        if age < 86400:
            return f"{age // 3600}h ago"
        return f"{age // 86400}d ago"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a draggable transparent Qt monitor for tokenbudget."
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=60,
        help="How often to recompute the direct snapshot (default: 60).",
    )
    parser.add_argument(
        "--graph-mode",
        choices=tuple(GRAPH_MODE_LABELS),
        default="hourly",
        help="Initial graph mode (default: hourly).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("tokenbudget")
    app.setOrganizationName("tokenbudget")
    app.setQuitOnLastWindowClosed(False)

    window = TokenbudgetWindow(
        poll_seconds=args.poll_seconds,
        graph_mode=args.graph_mode,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
