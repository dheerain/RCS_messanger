"""
Bulk RCS Message Sender — Flet frontend for GoogleRCS (RCS_session.GoogleRCS).

Run: python -m venv .venv
     .venv\\Scripts\\pip install -r requirements.txt
     python rcs_bulk_sender_app.py

All Selenium / GoogleRCS calls run on one dedicated worker thread (WebDriver is not thread-safe).
UI updates from the worker are scheduled on the Flet event loop with call_soon_threadsafe (synchronous app code, no async/await).
"""

from __future__ import annotations

import csv
import io
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Tuple, Union, cast

import flet as ft
from flet.controls.material.icons import Icons

from RCS_session import GoogleRCS


# ---------------------------------------------------------------------------
# Extensibility: swap implementation for API-based send, retries, delivery API
# ---------------------------------------------------------------------------


class MessageSendBackend(Protocol):
    """Future: REST API client, delivery webhooks, retries."""

    def establish_session(self, browser: str, headless: bool) -> None:
        ...

    def send_rcs(
        self,
        recipient: str,
        text: str,
        image_paths: Tuple[str, ...],
    ) -> None:
        ...

    def close_session(self) -> None:
        ...


class GoogleRCSBackend:
    """Thin adapter around GoogleRCS; images reserved for future RCS_session support."""

    def __init__(self) -> None:
        self._client: Optional[GoogleRCS] = None

    @property
    def client(self) -> Optional[GoogleRCS]:
        return self._client

    @property
    def connected(self) -> bool:
        return bool(self._client and getattr(self._client, "session", False))

    def establish_session(self, browser: str, headless: bool) -> None:
        self._client = GoogleRCS(browser=browser, headless=headless)
        self._client.opensession()

    def send_rcs(
        self,
        recipient: str,
        text: str,
        image_paths: Tuple[str, ...],
    ) -> None:
        if not self._client:
            raise RuntimeError("No RCS client")
        # GoogleRCS.send_message does not yet support files; keep contract for later.
        if image_paths:
            pass  # noqa: WPS428 — intentional no-op until RCS_session supports media
        self._client.send_message(recipient, text)

    def close_session(self) -> None:
        if self._client:
            try:
                self._client.closeSession()
            except Exception:
                pass
            self._client = None


# ---------------------------------------------------------------------------
# Phone parsing & validation
# ---------------------------------------------------------------------------

_PHONE_DIGITS_RE = re.compile(r"\+?\d[\d\-\s().]{7,}\d")


def normalize_phone(raw: str) -> Optional[str]:
    s = raw.strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) < 10:
        return None
    if s.strip().startswith("+"):
        return "+" + digits
    return digits if len(digits) == 10 else "+" + digits if digits.startswith("1") and len(digits) == 11 else digits


def parse_phones_from_text(text: str) -> List[str]:
    seen = set()
    out: List[str] = []
    for part in re.split(r"[\s,;]+|\n+", text):
        n = normalize_phone(part)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def parse_phones_from_csv_bytes(data: bytes) -> List[str]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    seen = set()
    out: List[str] = []
    for row in reader:
        for cell in row:
            for m in _PHONE_DIGITS_RE.findall(cell):
                n = normalize_phone(m)
                if n and n not in seen:
                    seen.add(n)
                    out.append(n)
    return out


# ---------------------------------------------------------------------------
# Worker thread (commands)
# ---------------------------------------------------------------------------


class SendStatus(Enum):
    IDLE = auto()
    RUNNING = auto()
    PAUSED = auto()
    COMPLETED = auto()


@dataclass
class FailureLogEntry:
    phone: str
    message: str
    ts: float = field(default_factory=time.time)


class _WorkerCommand(Enum):
    QUIT = auto()
    ESTABLISH = auto()
    SEND_LOOP = auto()


@dataclass
class _EstablishPayload:
    browser: str
    headless: bool


@dataclass
class _SendPayload:
    contacts: List[str]
    message: str
    image_paths: Tuple[str, ...]
    run_id: str


class RCSWorker:
    """Single thread owns GoogleRCSBackend / WebDriver."""

    def __init__(
        self,
        notify: Callable[[Callable[[], None]], None],
        on_established: Callable[[], None],
        on_establish_failed: Callable[[str], None],
        on_send_progress: Callable[[int, int, SendStatus], None],
        on_send_error: Callable[[str, str], None],
        on_send_finished: Callable[[], None],
    ) -> None:
        self._notify = notify
        self._backend = GoogleRCSBackend()
        self._q: "queue.Queue[Tuple[_WorkerCommand, Union[_EstablishPayload, _SendPayload, None]]]" = (
            queue.Queue()
        )
        self._pause = threading.Event()
        self._pause.set()  # set = not paused (worker runs)
        self._cancel_send = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._on_established = on_established
        self._on_establish_failed = on_establish_failed
        self._on_send_progress = on_send_progress
        self._on_send_error = on_send_error
        self._on_send_finished = on_send_finished

    @property
    def backend(self) -> GoogleRCSBackend:
        return self._backend

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="RCSWorker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._q.put((_WorkerCommand.QUIT, None))

    def request_establish(self, browser: str, headless: bool) -> None:
        self.start()
        self._q.put((_WorkerCommand.ESTABLISH, _EstablishPayload(browser, headless)))

    def request_send(self, contacts: List[str], message: str, image_paths: Tuple[str, ...]) -> None:
        self._cancel_send.clear()
        self._pause.set()
        self._q.put(
            (_WorkerCommand.SEND_LOOP, _SendPayload(contacts, message, image_paths, str(uuid.uuid4())))
        )

    def pause_sending(self) -> None:
        self._pause.clear()

    def resume_sending(self) -> None:
        self._pause.set()

    def cancel_sending(self) -> None:
        self._cancel_send.set()
        self._pause.set()

    def _loop(self) -> None:
        while True:
            cmd, payload = self._q.get()
            if cmd == _WorkerCommand.QUIT:
                try:
                    self._backend.close_session()
                except Exception:
                    pass
                break
            if cmd == _WorkerCommand.ESTABLISH:
                p = cast(_EstablishPayload, payload)
                try:
                    self._backend.close_session()
                except Exception:
                    pass
                try:
                    self._backend.establish_session(p.browser, p.headless)
                    self._notify(lambda: self._on_established())
                except Exception as e:
                    err = str(e)
                    self._notify(lambda: self._on_establish_failed(err))
            elif cmd == _WorkerCommand.SEND_LOOP:
                sp = cast(_SendPayload, payload)
                total = len(sp.contacts)
                if total == 0:
                    self._notify(lambda: self._on_send_progress(0, 0, SendStatus.COMPLETED))
                    self._notify(lambda: self._on_send_finished())
                    continue
                self._notify(lambda: self._on_send_progress(0, total, SendStatus.RUNNING))
                for idx, phone in enumerate(sp.contacts):
                    if self._cancel_send.is_set():
                        self._notify(lambda i=idx, t=total: self._on_send_progress(i, t, SendStatus.IDLE))
                        self._notify(lambda: self._on_send_finished())
                        break
                    pause_announced = False
                    while not self._pause.is_set():
                        if not pause_announced:
                            self._notify(
                                lambda i=idx, t=total: self._on_send_progress(i, t, SendStatus.PAUSED)
                            )
                            pause_announced = True
                        time.sleep(0.15)
                    self._notify(lambda i=idx, t=total: self._on_send_progress(i, t, SendStatus.RUNNING))
                    try:
                        self._backend.send_rcs(phone, sp.message, sp.image_paths)
                    except Exception as e:
                        err = str(e)
                        self._notify(lambda p=phone, er=err: self._on_send_error(p, er))
                    self._notify(
                        lambda d=idx + 1, t=total: self._on_send_progress(d, t, SendStatus.RUNNING)
                    )
                else:
                    self._notify(lambda t=total: self._on_send_progress(t, t, SendStatus.COMPLETED))
                    self._notify(lambda: self._on_send_finished())


# ---------------------------------------------------------------------------
# Flet UI
# ---------------------------------------------------------------------------

# Visual design tokens (dark messaging-style shell)
_UI_BG = "#0B1220"
_UI_SURFACE = "#151F32"
_UI_SURFACE_ELEV = "#1B2740"
_UI_BORDER = "#2D3F5C"
_UI_ACCENT = "#22D3EE"
_UI_ACCENT_DIM = "#0891B2"
_UI_MUTED = "#94A3B8"
_UI_TEXT = "#F1F5F9"


class BulkRCSApp:
    def _notify(self, thunk: Callable[[], None]) -> None:
        """Run UI updates on the Flet event loop (safe from the RCS worker thread)."""
        loop = self.page.session.connection.loop

        def _apply() -> None:
            thunk()
            self.page.update()

        loop.call_soon_threadsafe(_apply)

    def _ui_established(self) -> None:
        self._session_ok = True
        self._establish_btn.disabled = False
        self._apply_session_ui(True)

    def _ui_establish_failed(self, msg: str) -> None:
        self._session_ok = False
        self._establish_btn.disabled = False
        self._apply_session_ui(False)
        self._show_error_dialog("Session failed", msg, None)

    def _ui_send_progress(self, done: int, total: int, status: SendStatus) -> None:
        self._send_status = status
        self._progress_bar.value = 1.0 if total == 0 else min(1.0, done / total)
        pct = int(self._progress_bar.value * 100)
        self._progress_label.value = f"{pct}% — {done} / {total}"
        self._status_text.value = self._status_label_for(status)
        dot = {
            SendStatus.IDLE: "#64748B",
            SendStatus.RUNNING: _UI_ACCENT,
            SendStatus.PAUSED: "#FBBF24",
            SendStatus.COMPLETED: "#34D399",
        }.get(status, "#64748B")
        self._status_indicator.bgcolor = dot
        self._pause_resume_btn.disabled = (
            not self._session_ok or total == 0 or status in (SendStatus.COMPLETED, SendStatus.IDLE)
        )
        if status == SendStatus.COMPLETED:
            self._start_btn.disabled = False
            self._pause_resume_btn.content = "Pause"
            self._pause_resume_btn.icon = Icons.PAUSE
        if status == SendStatus.IDLE and total > 0:
            self._pause_resume_btn.content = "Pause"
            self._pause_resume_btn.icon = Icons.PAUSE

    def _ui_send_error(self, phone: str, err: str) -> None:
        self._failure_log.append(FailureLogEntry(phone=phone, message=err))
        self._refresh_failure_list()
        self._show_error_dialog("Send failed", err, phone)

    def _ui_send_finished(self) -> None:
        self._start_btn.disabled = False
        if self._send_status != SendStatus.PAUSED:
            self._pause_resume_btn.content = "Pause"
            self._pause_resume_btn.icon = Icons.PAUSE

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self._contacts: List[str] = []
        self._image_paths: List[str] = []
        self._session_ok = False
        self._send_status = SendStatus.IDLE
        self._failure_log: List[FailureLogEntry] = []

        # —— Controls ——
        self._pill = ft.RoundedRectangleBorder(radius=12)
        self._establish_btn = ft.ElevatedButton(
            content="Establish Session",
            icon=Icons.LINK,
            on_click=self._on_establish,
            style=ft.ButtonStyle(
                bgcolor=_UI_ACCENT_DIM,
                color=ft.Colors.WHITE,
                padding=ft.padding.symmetric(horizontal=20, vertical=14),
                shape=self._pill,
            ),
        )
        self._browser_dd = ft.Dropdown(
            label="Browser",
            width=160,
            dense=True,
            filled=True,
            border_color=_UI_BORDER,
            focused_border_color=_UI_ACCENT,
            value="chrome",
            options=[
                ft.dropdown.Option("chrome"),
                ft.dropdown.Option("edge"),
                ft.dropdown.Option("firefox"),
            ],
        )
        self._headless_sw = ft.Switch(
            label="Headless",
            value=False,
            active_color=_UI_ACCENT,
            inactive_thumb_color=ft.Colors.BLUE_GREY_400,
        )

        self._contacts_field = ft.TextField(
            label="Phone numbers",
            hint_text="Comma or newline separated (e.g. +15551234567, 5551234567)",
            multiline=True,
            min_lines=4,
            max_lines=10,
            disabled=True,
            filled=True,
            border_color=_UI_BORDER,
            focused_border_color=_UI_ACCENT,
            on_change=self._on_contacts_changed,
        )
        self._contact_stat_inner = ft.Text(
            "0 valid numbers",
            size=14,
            weight=ft.FontWeight.W_600,
            color=_UI_TEXT,
        )
        self._contact_count = ft.Container(
            padding=ft.padding.symmetric(horizontal=14, vertical=8),
            border_radius=24,
            bgcolor=ft.Colors.with_opacity(0.18, _UI_ACCENT),
            border=ft.border.all(1, ft.Colors.with_opacity(0.35, _UI_ACCENT)),
            content=ft.Row(
                [
                    ft.Icon(Icons.NUMBERS, size=18, color=_UI_ACCENT),
                    self._contact_stat_inner,
                ],
                spacing=8,
                tight=True,
            ),
        )
        self._csv_btn = ft.OutlinedButton(
            content="Upload CSV",
            icon=Icons.UPLOAD_FILE,
            disabled=True,
            on_click=self._pick_csv,
            style=ft.ButtonStyle(
                shape=self._pill,
                side=ft.BorderSide(width=1, color=_UI_ACCENT),
            ),
        )

        self._message_field = ft.TextField(
            label="RCS message",
            hint_text="Message body",
            multiline=True,
            min_lines=4,
            max_lines=12,
            disabled=True,
            filled=True,
            border_color=_UI_BORDER,
            focused_border_color=_UI_ACCENT,
        )
        self._attach_btn = ft.OutlinedButton(
            content="Attach images",
            icon=Icons.IMAGE,
            disabled=True,
            on_click=self._pick_images,
            style=ft.ButtonStyle(
                shape=self._pill,
                side=ft.BorderSide(width=1, color=_UI_ACCENT),
            ),
        )
        self._clear_images_btn = ft.TextButton(
            content="Clear images",
            disabled=True,
            on_click=self._clear_images,
            style=ft.ButtonStyle(color=_UI_MUTED, shape=self._pill),
        )
        self._preview_row = ft.Row(wrap=True, spacing=8, run_spacing=8)

        self._preview_msg_btn = ft.ElevatedButton(
            content="Preview Message",
            icon=Icons.REMOVE_RED_EYE,
            disabled=True,
            on_click=self._open_preview_dialog,
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.with_opacity(0.2, _UI_SURFACE_ELEV),
                color=_UI_TEXT,
                elevation=0,
                padding=ft.padding.symmetric(horizontal=18, vertical=12),
                shape=self._pill,
            ),
        )
        self._start_btn = ft.ElevatedButton(
            content="Start Sending",
            icon=Icons.SEND,
            disabled=True,
            on_click=self._on_start_send,
            style=ft.ButtonStyle(
                bgcolor="#059669",
                color=ft.Colors.WHITE,
                padding=ft.padding.symmetric(horizontal=22, vertical=14),
                shape=self._pill,
            ),
        )
        self._pause_resume_btn = ft.ElevatedButton(
            content="Pause",
            icon=Icons.PAUSE,
            disabled=True,
            on_click=self._on_pause_resume,
            style=ft.ButtonStyle(
                bgcolor="#D97706",
                color=ft.Colors.WHITE,
                padding=ft.padding.symmetric(horizontal=18, vertical=12),
                shape=self._pill,
            ),
        )

        self._progress_bar = ft.ProgressBar(
            value=0,
            width=float("inf"),
            bar_height=10,
            color=_UI_ACCENT,
            bgcolor=ft.Colors.with_opacity(0.25, _UI_BORDER),
            border_radius=8,
        )
        self._progress_label = ft.Text(
            "0% — 0 / 0",
            size=14,
            weight=ft.FontWeight.W_500,
            color=_UI_MUTED,
        )
        self._status_indicator = ft.Container(
            width=10,
            height=10,
            border_radius=5,
            bgcolor=ft.Colors.BLUE_GREY_500,
        )
        self._status_text = ft.Text(
            "Idle",
            size=15,
            weight=ft.FontWeight.W_600,
            color=_UI_TEXT,
        )

        self._failure_list = ft.ListView(expand=True, spacing=4, padding=8, auto_scroll=True)

        self._worker = RCSWorker(
            self._notify,
            self._ui_established,
            self._ui_establish_failed,
            self._ui_send_progress,
            self._ui_send_error,
            self._ui_send_finished,
        )

        self._build_layout()
        self._apply_session_ui(False)

    def _status_label_for(self, s: SendStatus) -> str:
        return {
            SendStatus.IDLE: "Idle",
            SendStatus.RUNNING: "Sending...",
            SendStatus.PAUSED: "Paused",
            SendStatus.COMPLETED: "Completed",
        }.get(s, "Unknown")

    def _section_header(self, icon, title: str, subtitle: Optional[str] = None) -> ft.Row:
        titles: List[ft.Control] = [
            ft.Text(title, size=18, weight=ft.FontWeight.W_600, color=_UI_TEXT),
        ]
        if subtitle:
            titles.append(ft.Text(subtitle, size=12, color=_UI_MUTED))
        return ft.Row(
            [
                ft.Container(
                    padding=10,
                    border_radius=12,
                    bgcolor=ft.Colors.with_opacity(0.2, _UI_ACCENT),
                    content=ft.Icon(icon, size=22, color=_UI_ACCENT),
                ),
                ft.Column(titles, spacing=2, tight=True),
            ],
            spacing=14,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _build_layout(self) -> None:
        hero = ft.Container(
            padding=ft.padding.all(28),
            border_radius=20,
            gradient=ft.LinearGradient(
                colors=["#0E7490", "#1D4ED8", "#4338CA"],
                begin=ft.Alignment(-1, -0.2),
                end=ft.Alignment(1, 0.4),
            ),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(Icons.FORUM, size=36, color=ft.Colors.WHITE),
                            ft.Column(
                                [
                                    ft.Text(
                                        "Bulk RCS",
                                        size=32,
                                        weight=ft.FontWeight.BOLD,
                                        color=ft.Colors.WHITE,
                                    ),
                                    ft.Text(
                                        "Google Messages · session-based sending",
                                        size=14,
                                        color=ft.Colors.with_opacity(0.9, ft.Colors.WHITE),
                                    ),
                                ],
                                spacing=4,
                                tight=True,
                                expand=True,
                            ),
                        ],
                        spacing=16,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                ],
                spacing=8,
            ),
        )

        session_card = ft.Card(
            elevation=6,
            shadow_color="#000000",
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=_UI_SURFACE_ELEV,
            content=ft.Container(
                padding=20,
                content=ft.Column(
                    [
                        self._section_header(
                            Icons.LINK,
                            "Session",
                            "Connect the browser, then load contacts and send.",
                        ),
                        ft.Row(
                            [
                                self._establish_btn,
                                ft.VerticalDivider(width=1, color=_UI_BORDER),
                                self._browser_dd,
                                self._headless_sw,
                            ],
                            alignment=ft.MainAxisAlignment.START,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=16,
                            wrap=True,
                            run_spacing=12,
                        ),
                    ],
                    spacing=16,
                ),
            ),
        )

        input_card = ft.Card(
            elevation=4,
            shadow_color="#000000",
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=_UI_SURFACE,
            content=ft.Container(
                padding=22,
                content=ft.Column(
                    cast(
                        List[ft.Control],
                        [
                            self._section_header(
                                Icons.PEOPLE,
                                "Recipients & message",
                                "Paste numbers, import CSV, compose your RCS body and media.",
                            ),
                            self._contacts_field,
                            ft.Row(
                                [self._contact_count, self._csv_btn],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            ft.Divider(color=_UI_BORDER, height=1),
                            self._message_field,
                            ft.Row(
                                [self._attach_btn, self._clear_images_btn],
                                spacing=12,
                            ),
                            ft.Text("Image preview", size=12, weight=ft.FontWeight.W_500, color=_UI_MUTED),
                            ft.Container(
                                self._preview_row,
                                height=128,
                                padding=10,
                                bgcolor=ft.Colors.with_opacity(0.35, _UI_BG),
                                border=ft.border.all(1, _UI_BORDER),
                                border_radius=14,
                            ),
                        ],
                    ),
                    spacing=14,
                ),
            ),
        )

        actions = ft.Card(
            elevation=2,
            bgcolor=_UI_SURFACE_ELEV,
            content=ft.Container(
                padding=18,
                content=ft.Row(
                    [self._preview_msg_btn, self._start_btn, self._pause_resume_btn],
                    alignment=ft.MainAxisAlignment.START,
                    spacing=14,
                    wrap=True,
                    run_spacing=12,
                ),
            ),
        )

        self._status_row = ft.Row(
            [self._status_indicator, self._status_text],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        progress = ft.Card(
            elevation=4,
            shadow_color="#000000",
            bgcolor=_UI_SURFACE,
            content=ft.Container(
                padding=22,
                content=ft.Column(
                    cast(
                        List[ft.Control],
                        [
                            self._section_header(
                                Icons.TRENDING_UP,
                                "Progress",
                                "Live status while the worker sends each RCS message.",
                            ),
                            self._progress_bar,
                            self._progress_label,
                            ft.Container(
                                padding=ft.padding.symmetric(horizontal=14, vertical=10),
                                border_radius=12,
                                bgcolor=ft.Colors.with_opacity(0.25, _UI_BG),
                                border=ft.border.all(1, _UI_BORDER),
                                content=self._status_row,
                            ),
                        ],
                    ),
                    spacing=14,
                ),
            ),
        )

        failures = ft.Card(
            elevation=3,
            bgcolor=ft.Colors.with_opacity(0.95, "#1A1008"),
            content=ft.Container(
                padding=22,
                content=ft.Column(
                    cast(
                        List[ft.Control],
                        [
                            self._section_header(
                                Icons.WARNING_AMBER_ROUNDED,
                                "Failure log",
                                "Each error is shown in a dialog and listed here.",
                            ),
                            ft.Container(
                                self._failure_list,
                                height=180,
                                border_radius=12,
                                bgcolor=ft.Colors.with_opacity(0.4, _UI_BG),
                                border=ft.border.all(1, ft.Colors.with_opacity(0.45, "#F59E0B")),
                            ),
                        ],
                    ),
                    spacing=12,
                ),
            ),
        )

        self.page.add(
            ft.SafeArea(
                ft.Container(
                    bgcolor=_UI_BG,
                    expand=True,
                    padding=ft.padding.symmetric(horizontal=20, vertical=16),
                    content=ft.Column(
                        cast(
                            List[ft.Control],
                            [
                                hero,
                                session_card,
                                input_card,
                                actions,
                                progress,
                                failures,
                            ],
                        ),
                        spacing=18,
                        expand=True,
                        scroll=ft.ScrollMode.AUTO,
                    ),
                )
            )
        )

    def _apply_session_ui(self, connected: bool) -> None:
        if connected:
            self._establish_btn.content = "Session active"
            self._establish_btn.icon = Icons.CHECK_CIRCLE
            self._establish_btn.style = ft.ButtonStyle(
                bgcolor="#059669",
                color=ft.Colors.WHITE,
                padding=ft.padding.symmetric(horizontal=20, vertical=14),
                shape=self._pill,
            )
        else:
            self._establish_btn.content = "Establish Session"
            self._establish_btn.icon = Icons.LINK
            self._establish_btn.style = ft.ButtonStyle(
                bgcolor=_UI_ACCENT_DIM,
                color=ft.Colors.WHITE,
                padding=ft.padding.symmetric(horizontal=20, vertical=14),
                shape=self._pill,
            )
        for c in (
            self._contacts_field,
            self._csv_btn,
            self._message_field,
            self._attach_btn,
            self._preview_msg_btn,
            self._start_btn,
        ):
            c.disabled = not connected
        self._browser_dd.disabled = connected
        self._headless_sw.disabled = connected
        self._clear_images_btn.disabled = not connected or not self._image_paths
        self._pause_resume_btn.disabled = (
            not connected or self._send_status not in (SendStatus.RUNNING, SendStatus.PAUSED)
        )

    def _on_establish(self, _e) -> None:
        self._establish_btn.disabled = True
        self.page.update()
        self._worker.request_establish(self._browser_dd.value or "chrome", self._headless_sw.value)

    def _on_contacts_changed(self, e: ft.Event[ft.TextField]) -> None:
        self._contacts = parse_phones_from_text(e.control.value or "")
        n = len(self._contacts)
        self._contact_stat_inner.value = f"{n} valid number{'s' if n != 1 else ''}"
        self.page.update()

    def _pick_csv(self, _e: ft.Event[ft.OutlinedButton]) -> None:
        if self.page.web:
            self._show_error_dialog(
                "Desktop only",
                "CSV file selection uses a native dialog and requires the desktop app, not web.",
                None,
            )
            return
        path = self._ask_open_filename(
            title="Select contacts CSV",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            phones = parse_phones_from_csv_bytes(data)
            self._contacts = phones
            self._contacts_field.value = "\n".join(phones)
            n = len(self._contacts)
            self._contact_stat_inner.value = f"{n} valid number{'s' if n != 1 else ''}"
        except Exception as ex:
            self._show_error_dialog("CSV error", str(ex), None)
        self.page.update()

    def _pick_images(self, _e: ft.Event[ft.OutlinedButton]) -> None:
        if self.page.web:
            self._show_error_dialog(
                "Desktop only",
                "Image file selection uses a native dialog and requires the desktop app, not web.",
                None,
            )
            return
        paths = self._ask_open_filenames(
            title="Attach images",
            filetypes=[
                ("Image files", "*.png;*.jpg;*.jpeg;*.gif;*.webp"),
                ("All files", "*.*"),
            ],
        )
        if not paths:
            return
        for p in paths:
            self._image_paths.append(p)
        self._rebuild_image_preview()
        self._clear_images_btn.disabled = not self._session_ok or not self._image_paths
        self.page.update()

    def _ask_open_filename(self, title: str, filetypes: List[Tuple[str, str]]) -> Optional[str]:
        try:
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except tk.TclError:
                pass
            path = filedialog.askopenfilename(parent=root, title=title, filetypes=filetypes)
            root.destroy()
            return str(path) if path else None
        except Exception as ex:
            self._show_error_dialog("File dialog", str(ex), None)
            return None

    def _ask_open_filenames(
        self,
        title: str,
        filetypes: List[Tuple[str, str]],
    ) -> List[str]:
        try:
            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except tk.TclError:
                pass
            paths = filedialog.askopenfilenames(parent=root, title=title, filetypes=filetypes)
            root.destroy()
            return [str(p) for p in paths] if paths else []
        except Exception as ex:
            self._show_error_dialog("File dialog", str(ex), None)
            return []

    def _clear_images(self, _e) -> None:
        self._image_paths.clear()
        self._preview_row.controls.clear()
        self._clear_images_btn.disabled = True
        self.page.update()

    def _rebuild_image_preview(self) -> None:
        self._preview_row.controls.clear()
        for p in self._image_paths[-12:]:
            self._preview_row.controls.append(
                ft.Container(
                    content=ft.Image(src=p, width=72, height=72, fit=ft.BoxFit.COVER),
                    border_radius=6,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                )
            )

    def _open_preview_dialog(self, _e) -> None:
        body = self._message_field.value or "(empty)"
        imgs: List[ft.Control] = [
            ft.Image(src=p, width=200, fit=ft.BoxFit.CONTAIN) for p in self._image_paths
        ]
        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=_UI_SURFACE_ELEV,
            title=ft.Text("Message preview", weight=ft.FontWeight.W_600, color=_UI_TEXT),
            content=ft.Container(
                width=480,
                height=420,
                padding=8,
                content=ft.Column(
                    cast(
                        List[ft.Control],
                        [
                            ft.Text("Text", weight=ft.FontWeight.BOLD, color=_UI_MUTED),
                            ft.Text(body, color=_UI_TEXT),
                            ft.Divider(color=_UI_BORDER),
                            ft.Text("Images", weight=ft.FontWeight.BOLD, color=_UI_MUTED),
                            ft.Row(imgs, wrap=True, spacing=8, run_spacing=8),
                        ],
                    ),
                    scroll=ft.ScrollMode.AUTO,
                ),
            ),
            actions=[
                ft.FilledButton(
                    content="Close",
                    on_click=lambda e: self._dismiss_dialog(dlg),
                    style=ft.ButtonStyle(
                        bgcolor=_UI_ACCENT_DIM,
                        color=ft.Colors.WHITE,
                        shape=self._pill,
                        padding=ft.padding.symmetric(horizontal=24, vertical=12),
                    ),
                )
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _on_start_send(self, _e) -> None:
        if not self._contacts:
            self._show_error_dialog("Validation", "No valid phone numbers.", None)
            return
        msg = (self._message_field.value or "").strip()
        if not msg:
            self._show_error_dialog("Validation", "Message body is empty.", None)
            return
        self._send_status = SendStatus.RUNNING
        self._start_btn.disabled = True
        self._pause_resume_btn.disabled = False
        self._pause_resume_btn.content = "Pause"
        self._pause_resume_btn.icon = Icons.PAUSE
        self._progress_bar.value = 0
        self._status_text.value = "Sending..."
        self.page.update()
        self._worker.request_send(list(self._contacts), msg, tuple(self._image_paths))

    def _on_pause_resume(self, _e) -> None:
        if self._send_status == SendStatus.PAUSED:
            self._worker.resume_sending()
            self._pause_resume_btn.content = "Pause"
            self._pause_resume_btn.icon = Icons.PAUSE
            self._status_text.value = "Sending..."
        else:
            self._worker.pause_sending()
            self._pause_resume_btn.content = "Resume"
            self._pause_resume_btn.icon = Icons.PLAY_ARROW
            self._status_text.value = "Paused"
        self.page.update()

    def _dismiss_dialog(self, dlg: ft.AlertDialog) -> None:
        dlg.open = False
        self.page.update()

    def _show_error_dialog(self, title: str, message: str, phone: Optional[str]) -> None:
        parts: List[ft.Control] = [ft.Text(message, color=_UI_TEXT)]
        if phone:
            parts.append(ft.Text(f"Number: {phone}", selectable=True, color=_UI_MUTED))
        dlg = ft.AlertDialog(
            modal=True,
            bgcolor=_UI_SURFACE_ELEV,
            title=ft.Text(title, weight=ft.FontWeight.W_600, color=_UI_TEXT),
            content=ft.Column(parts, tight=True),
            actions=[
                ft.FilledButton(
                    content="OK",
                    on_click=lambda e: self._dismiss_dialog(dlg),
                    style=ft.ButtonStyle(
                        bgcolor=_UI_ACCENT_DIM,
                        color=ft.Colors.WHITE,
                        shape=self._pill,
                        padding=ft.padding.symmetric(horizontal=24, vertical=12),
                    ),
                )
            ],
        )
        self.page.show_dialog(dlg)

    def _refresh_failure_list(self) -> None:
        self._failure_list.controls.clear()
        for entry in reversed(self._failure_log[-200:]):
            self._failure_list.controls.append(
                ft.Container(
                    margin=ft.margin.only(bottom=6),
                    border_radius=10,
                    bgcolor=ft.Colors.with_opacity(0.35, _UI_BG),
                    border=ft.border.all(1, ft.Colors.with_opacity(0.25, "#FB923C")),
                    content=ft.ListTile(
                        leading=ft.Icon(Icons.ERROR_OUTLINE, color="#FB923C"),
                        title=ft.Text(entry.phone, size=13, weight=ft.FontWeight.W_500, color=_UI_TEXT),
                        subtitle=ft.Text(entry.message, size=12, color=_UI_MUTED),
                    ),
                )
            )


def main(page: ft.Page) -> None:
    page.title = "Bulk RCS Sender"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = _UI_BG
    page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
    seed = "#22D3EE"
    page.theme = ft.Theme(color_scheme_seed=seed, use_material3=True)
    page.dark_theme = ft.Theme(color_scheme_seed=seed, use_material3=True)
    page.window.min_width = 760
    page.window.min_height = 680
    page.window.full_screen = False
    page.window.maximized = True
    page.window.resizable = False
    page.scroll = ft.ScrollMode.AUTO

    app = BulkRCSApp(page)

    def cleanup() -> None:
        app._worker.cancel_sending()
        app._worker.stop()

    def on_window_event(e: ft.WindowEvent) -> None:
        if e.type == ft.WindowEventType.CLOSE:
            cleanup()
            loop = page.session.connection.loop
            loop.create_task(page.window.destroy())

    page.window.prevent_close = True
    page.window.on_event = on_window_event
    page.on_disconnect = lambda _: cleanup()


if __name__ == "__main__":
    ft.app(target=main)
