#!/usr/bin/env python3
"""
Local AI Chat — Ollama desktop client (Tkinter, no external dependencies)

โครงสร้าง
    Config        : ค่าตั้งค่าที่บันทึกลงดิสก์ (host / model / system prompt / options)
    OllamaClient  : ชั้นเชื่อมต่อ HTTP อย่างเดียว ยกเลิกกลางคันได้จริง
    ThinkSplitter : แยก <think>...</think> ออกจากคำตอบแบบ streaming
    Markdown      : เรนเดอร์ code block / bullet / heading / bold / inline code ลง Text widget
    ChatStore     : โหลด-บันทึกประวัติแบบ atomic (ไฟล์ไม่พังถ้าปิดโปรแกรมกลางทาง)
    ChatApp       : ชั้น UI ล้วน ๆ แตะ widget เฉพาะใน main thread ผ่าน event queue
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import urllib.error
import urllib.request
import webbrowser
import tkinter as tk
from dataclasses import dataclass, asdict, fields as dc_fields
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont
from tkinter.scrolledtext import ScrolledText

APP_NAME = "Local AI Chat"
DATA_DIR = Path(os.getenv("LOCALAPPDATA") or Path.home()) / "OllamaChatDesktop"
HISTORY_FILE = DATA_DIR / "history.json"
CONFIG_FILE = DATA_DIR / "config.json"

# ---------------------------------------------------------------- theme ----
BG = "#202123"
SIDEBAR = "#171717"
PANEL = "#2f3033"
INPUT_BG = "#2b2c2f"
CODE_BG = "#1b1c1e"
TEXT = "#ececec"
MUTED = "#9a9a9a"
GREEN = "#19c37d"
RED = "#ef6b73"
BLUE = "#9ad7ff"


# --------------------------------------------------------------- config ----
@dataclass
class Config:
    host: str = "http://127.0.0.1:11434"
    model: str = "qwen3:8b"
    system_prompt: str = ""
    temperature: float = 0.7
    num_ctx: int = 8192
    think: bool = True                 # ขอ reasoning แยก field (Ollama รุ่นใหม่)
    max_history_messages: int = 20     # 0 = ส่งทั้งหมด
    request_timeout: int = 600
    font_size: int = 12

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            known = {f.name for f in dc_fields(cls)}
            for key, value in data.items():
                if key in known:
                    setattr(cfg, key, value)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        return cfg

    def save(self) -> None:
        atomic_write(CONFIG_FILE, json.dumps(asdict(self), ensure_ascii=False, indent=2))


def atomic_write(path: Path, text: str) -> None:
    """เขียนผ่านไฟล์ชั่วคราวแล้ว replace — ไฟล์เดิมไม่พังถ้าไฟดับกลางคัน"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# --------------------------------------------------------------- client ----
class OllamaError(Exception):
    pass


class OllamaClient:
    """เชื่อมต่อ Ollama อย่างเดียว ไม่รู้จัก Tkinter — เทสต์แยกได้"""

    def __init__(self, host: str):
        self.host = host.rstrip("/")
        self._lock = threading.Lock()
        self._response = None

    def _open(self, path: str, payload=None, method="GET", timeout=15):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.host + path, data=data,
            headers={"Content-Type": "application/json"}, method=method)
        return urllib.request.urlopen(request, timeout=timeout)

    def list_models(self, timeout=8) -> list[str]:
        with self._open("/api/tags", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return sorted(m["name"] for m in data.get("models", []) if m.get("name"))

    def chat_stream(self, messages, model, options, think=None, timeout=600):
        """yield dict ทีละ chunk; ถ้า server ไม่รู้จัก think จะ retry ให้อัตโนมัติ"""
        payload = {"model": model, "messages": messages, "stream": True, "options": options}
        if think is not None:
            payload["think"] = think

        started = False
        try:
            for obj in self._stream(payload, timeout):
                started = True
                yield obj
        except urllib.error.HTTPError as exc:
            if started or think is None:
                raise OllamaError(self._http_message(exc)) from exc
            payload.pop("think", None)          # โมเดล/เซิร์ฟเวอร์ไม่รองรับ think
            yield from self._stream(payload, timeout)

    def _stream(self, payload, timeout):
        response = self._open("/api/chat", payload, "POST", timeout)
        with self._lock:
            self._response = response
        try:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("error"):
                    raise OllamaError(str(obj["error"]))
                yield obj
                if obj.get("done"):
                    break
        finally:
            with self._lock:
                self._response = None
            try:
                response.close()
            except Exception:
                pass

    def cancel(self) -> None:
        """ปิด socket ทันที — ไม่ต้องรอ token ถัดไปเหมือนโค้ดเดิม"""
        with self._lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    @staticmethod
    def _http_message(exc: urllib.error.HTTPError) -> str:
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            return str(body.get("error") or body)
        except Exception:
            return f"HTTP {exc.code}"


# ------------------------------------------------------- think splitter ----
class ThinkSplitter:
    """แยก <think>...</think> ระหว่าง stream โดยรองรับแท็กที่ถูกตัดครึ่ง chunk"""

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self):
        self.buffer = ""
        self.in_think = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self.buffer += chunk
        out: list[tuple[str, str]] = []
        while True:
            tag = self.CLOSE if self.in_think else self.OPEN
            index = self.buffer.find(tag)
            if index == -1:
                hold = self._partial_tail(self.buffer, tag)
                emit = self.buffer[:len(self.buffer) - hold] if hold else self.buffer
                self.buffer = self.buffer[len(self.buffer) - hold:] if hold else ""
                if emit:
                    out.append(("think" if self.in_think else "content", emit))
                break
            emit = self.buffer[:index]
            if emit:
                out.append(("think" if self.in_think else "content", emit))
            self.buffer = self.buffer[index + len(tag):]
            self.in_think = not self.in_think
        return out

    def flush(self) -> list[tuple[str, str]]:
        rest, self.buffer = self.buffer, ""
        return [("think" if self.in_think else "content", rest)] if rest else []

    @staticmethod
    def _partial_tail(text: str, tag: str) -> int:
        for size in range(min(len(tag) - 1, len(text)), 0, -1):
            if text.endswith(tag[:size]):
                return size
        return 0


# ------------------------------------------------------------- markdown ----
INLINE_RE = re.compile(
    r"(\*\*[^*]+\*\*|~~[^~]+~~|`[^`]+`|\[[^\]]+\]\([^)]+\)|\*[^*\n]+\*)")
LINK_RE = re.compile(r"^\[([^\]]+)\]\(([^)]+)\)$")
ORDERED_RE = re.compile(r"^(\d+)[.)]\s+(.*)$")
HR_RE = re.compile(r"^([-*_])\1{2,}$")
TABLE_SEP_RE = re.compile(r"^\|?[\s:|-]+\|?$")


def _open_link(url: str) -> None:
    if url.startswith(("http://", "https://")):
        webbrowser.open(url)


def _insert_link(widget, label: str, url: str) -> None:
    count = getattr(widget, "_link_count", 0)
    widget._link_count = count + 1
    tag = f"link{count}"
    widget.insert("end", label, ("link", tag))
    widget.tag_bind(tag, "<Button-1>", lambda e, u=url: _open_link(u))
    widget.tag_bind(tag, "<Enter>", lambda e: widget.configure(cursor="hand2"))
    widget.tag_bind(tag, "<Leave>", lambda e: widget.configure(cursor=""))


def insert_inline(widget, text: str, base="body") -> None:
    for part in INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            widget.insert("end", part[2:-2], "bold")
        elif part.startswith("~~") and part.endswith("~~"):
            widget.insert("end", part[2:-2], "strike")
        elif part.startswith("`") and part.endswith("`"):
            widget.insert("end", part[1:-1], "icode")
        elif part.startswith("["):
            match = LINK_RE.match(part)
            if match:
                _insert_link(widget, match.group(1), match.group(2))
            else:
                widget.insert("end", part, base)
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            widget.insert("end", part[1:-1], "italic")
        else:
            widget.insert("end", part, base)


def _insert_table(widget, rows_raw: list[str]) -> None:
    rows = [[c.strip() for c in r.strip("|").split("|")] for r in rows_raw]
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    widths = [max(len(r[c]) for r in rows) for c in range(ncols)]
    for row_index, row in enumerate(rows):
        line = " │ ".join(cell.ljust(widths[col]) for col, cell in enumerate(row))
        widget.insert("end", line + "\n", "table_head" if row_index == 0 else "table")
    widget.insert("end", "\n", "body")


def insert_markdown(widget, text: str) -> None:
    lines = text.split("\n")
    in_code = False
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            index += 1
            continue
        if in_code:
            widget.insert("end", line + "\n", "code")
            index += 1
            continue
        if not stripped:
            widget.insert("end", "\n", "body")
            index += 1
            continue
        if HR_RE.match(stripped.replace(" ", "")):
            widget.insert("end", "─" * 42 + "\n", "hr")
            index += 1
            continue
        if (stripped.startswith("|") and index + 1 < len(lines)
                and TABLE_SEP_RE.match(lines[index + 1].strip())
                and "-" in lines[index + 1]):
            table_lines = [stripped]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            _insert_table(widget, table_lines)
            continue
        if stripped.startswith(">"):
            widget.insert("end", "▏ ", "quote")
            insert_inline(widget, stripped.lstrip(">").strip(), base="quote")
            widget.insert("end", "\n", "quote")
            index += 1
            continue
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            widget.insert("end", stripped.lstrip("# ").strip() + "\n",
                          "h1" if level <= 2 else "h3")
            index += 1
            continue
        if stripped.startswith(("- ", "* ")):
            indent = (len(line) - len(line.lstrip(" "))) // 2
            widget.insert("end", "   " * (indent + 1) + "•  ", "body")
            insert_inline(widget, stripped[2:])
            widget.insert("end", "\n", "body")
            index += 1
            continue
        match = ORDERED_RE.match(stripped)
        if match:
            widget.insert("end", f"   {match.group(1)}.  ", "body")
            insert_inline(widget, match.group(2))
            widget.insert("end", "\n", "body")
            index += 1
            continue
        insert_inline(widget, line)
        widget.insert("end", "\n", "body")
        index += 1


# ---------------------------------------------------------------- store ----
class ChatStore:
    def __init__(self):
        self.chats: list[dict] = self._load()

    @staticmethod
    def _load() -> list[dict]:
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [c for c in data if isinstance(c, dict)]
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def save(self) -> None:
        keep = [c for c in self.chats if c.get("messages")]
        atomic_write(HISTORY_FILE, json.dumps(keep, ensure_ascii=False, indent=2))

    @staticmethod
    def new_chat() -> dict:
        now = datetime.now().isoformat(timespec="seconds")
        return {"id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
                "title": "แชตใหม่", "messages": [], "created_at": now, "updated_at": now}


# ------------------------------------------------------------------ app ----
# (attr, offset จาก font_size, ขนาดต่ำสุด) — ต่ำสุดกันไม่ให้ UI เล็กจนอ่านไม่ออกตอนซูมออกสุด
FONT_SPEC = (
    ("f_body", 0, 8), ("f_bold", 0, 8), ("f_name", -1, 8),
    ("f_h1", 4, 10), ("f_h3", 1, 9), ("f_italic", -1, 8), ("f_mono", -1, 8),
    ("f_ui", -2, 8), ("f_ui_bold", -2, 8), ("f_small", -4, 8), ("f_title", 1, 10),
)
FONT_BOLD = {"f_bold", "f_name", "f_h1", "f_h3", "f_ui_bold", "f_title"}
FONT_ITALIC = {"f_italic"}


class ChatApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = Config.load()
        self.client = OllamaClient(self.cfg.host)
        self.store = ChatStore()

        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.generation = 0
        self.expanded_think: set[int] = set()
        self.stream_think_text = ""
        self.stream_content_text = ""
        self.current_chat: dict | None = None

        self._init_fonts()
        self._build_ui()
        self._bind_keys()

        self.new_chat()
        self.root.after(40, self._process_events)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self._refresh_models, daemon=True).start()

    # ------------------------------------------------------------ fonts --
    def _init_fonts(self):
        families = set(tkfont.families())
        ui = next((f for f in ("Sarabun", "Leelawadee UI", "Noto Sans Thai",
                               "Tahoma", "Segoe UI", "Helvetica") if f in families), "TkDefaultFont")
        mono = next((f for f in ("JetBrains Mono", "Cascadia Mono", "Consolas",
                                 "DejaVu Sans Mono", "Courier New") if f in families), "TkFixedFont")
        self.cfg.font_size = max(9, min(24, self.cfg.font_size))  # กันค่าที่บันทึกไว้ผิดพลาด/สุดโต่ง
        size = self.cfg.font_size
        for name, offset, floor in FONT_SPEC:
            kwargs = {"family": mono if name == "f_mono" else ui,
                      "size": max(floor, size + offset)}
            if name in FONT_BOLD:
                kwargs["weight"] = "bold"
            if name in FONT_ITALIC:
                kwargs["slant"] = "italic"
            setattr(self, name, tkfont.Font(**kwargs))

    def _apply_zoom(self, delta: int):
        self.cfg.font_size = max(9, min(24, self.cfg.font_size + delta))
        size = self.cfg.font_size
        for name, offset, floor in FONT_SPEC:
            getattr(self, name).configure(size=max(floor, size + offset))
        self.cfg.save()

    # --------------------------------------------------------------- ui --
    def _build_ui(self):
        self.root.title(APP_NAME)
        self.root.geometry("1180x780")
        self.root.minsize(880, 600)
        self.root.configure(bg=BG)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Dark.TCombobox", fieldbackground=PANEL, background=PANEL,
                        foreground=TEXT, arrowcolor=TEXT, bordercolor=PANEL,
                        lightcolor=PANEL, darkcolor=PANEL, selectbackground=PANEL,
                        selectforeground=TEXT, insertcolor=TEXT)
        # ttk ใช้ style.map สำหรับสีตามสถานะ (readonly/disabled/focus) — configure() อย่างเดียวไม่พอ
        style.map("Dark.TCombobox",
                  fieldbackground=[("readonly", PANEL), ("disabled", PANEL), ("focus", PANEL)],
                  selectbackground=[("readonly", PANEL)],
                  selectforeground=[("readonly", TEXT)],
                  foreground=[("readonly", TEXT), ("disabled", MUTED)],
                  background=[("readonly", PANEL), ("active", PANEL)],
                  arrowcolor=[("readonly", TEXT), ("disabled", MUTED)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", GREEN)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#082b1d")

        self._build_sidebar()
        self._build_main()

    def _flat_button(self, parent, text, command, **kw):
        options = dict(bg=PANEL, fg=TEXT, activebackground="#414246", activeforeground=TEXT,
                       relief="flat", font=self.f_ui, cursor="hand2", padx=10, pady=6)
        options.update(kw)
        return tk.Button(parent, text=text, command=command, **options)

    def _build_sidebar(self):
        sidebar = tk.Frame(self.root, bg=SIDEBAR, width=260)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        tk.Label(sidebar, text="LOCAL AI", bg=SIDEBAR, fg=TEXT,
                 font=self.f_title).pack(anchor="w", padx=18, pady=(18, 10))

        self._flat_button(sidebar, "＋  แชตใหม่", self.new_chat, anchor="w",
                          padx=12, pady=9).pack(fill="x", padx=12)

        tk.Label(sidebar, text="โมเดล", bg=SIDEBAR, fg=MUTED,
                 font=self.f_small).pack(anchor="w", padx=18, pady=(16, 4))
        self.model_var = tk.StringVar(value=self.cfg.model)
        self.model_box = ttk.Combobox(sidebar, textvariable=self.model_var,
                                      style="Dark.TCombobox", state="readonly",
                                      values=[self.cfg.model], font=self.f_ui)
        self.model_box.pack(fill="x", padx=12)
        self.model_box.bind("<<ComboboxSelected>>", self.on_model_change)

        tk.Label(sidebar, text="ค้นหาในประวัติ", bg=SIDEBAR, fg=MUTED,
                 font=self.f_small).pack(anchor="w", padx=18, pady=(16, 4))
        self.search_var = tk.StringVar()
        search = tk.Entry(sidebar, textvariable=self.search_var, bg=PANEL, fg=TEXT,
                          insertbackground=TEXT, relief="flat", font=self.f_ui)
        search.pack(fill="x", padx=12, pady=(0, 6), ipady=5)
        self.search_var.trace_add("write", lambda *_: self.refresh_history())

        self.history_list = tk.Listbox(sidebar, bg=SIDEBAR, fg=TEXT,
                                       selectbackground=PANEL, selectforeground=TEXT,
                                       relief="flat", borderwidth=0, highlightthickness=0,
                                       font=self.f_ui, activestyle="none")
        self.history_list.pack(fill="both", expand=True, padx=10)
        self.history_list.bind("<<ListboxSelect>>", self.open_selected_chat)
        self.history_list.bind("<Double-Button-1>", self.rename_chat)
        self.history_list.bind("<Delete>", lambda e: self.delete_chat())

        bottom = tk.Frame(sidebar, bg=SIDEBAR)
        bottom.pack(fill="x", padx=12, pady=10)
        self.conn_label = tk.Label(bottom, text="●  กำลังเชื่อมต่อ", bg=SIDEBAR,
                                   fg=MUTED, font=self.f_small)
        self.conn_label.pack(side="left")
        self._flat_button(bottom, "⚙", self.open_settings, bg=SIDEBAR, fg=MUTED,
                          activebackground=SIDEBAR, padx=6, pady=2).pack(side="right")
        self._flat_button(bottom, "ลบแชต", self.delete_chat, bg=SIDEBAR, fg=MUTED,
                          activebackground=SIDEBAR, padx=6, pady=2).pack(side="right")

    def _build_main(self):
        main = tk.Frame(self.root, bg=BG)
        main.pack(side="left", fill="both", expand=True)

        header = tk.Frame(main, bg=BG, height=56)
        header.pack(fill="x")
        header.pack_propagate(False)
        self.title_label = tk.Label(header, text="แชตใหม่", bg=BG, fg=TEXT, font=self.f_title)
        self.title_label.pack(side="left", padx=24)

        self.status_label = tk.Label(header, text="พร้อมใช้งาน", bg=BG, fg=MUTED, font=self.f_small)
        self.status_label.pack(side="right", padx=(8, 24))
        self._flat_button(header, "ส่งออก .md", self.export_markdown, bg=BG, fg=MUTED,
                          activebackground=BG).pack(side="right")
        self._flat_button(header, "↻ ตอบใหม่", self.regenerate, bg=BG, fg=MUTED,
                          activebackground=BG).pack(side="right")

        # composer ต้อง pack ก่อน (side="bottom") เพื่อจองพื้นที่ของตัวเองไว้เสมอ —
        # ถ้า pack กล่องแชท (expand=True) ก่อน แล้วค่อย pack composer ทีหลัง
        # เมื่อย่อหน้าต่างเล็กลง Tk จะให้กล่องแชทกินพื้นที่จนไม่เหลือให้ composer เห็น
        composer_outer = tk.Frame(main, bg=BG)
        composer_outer.pack(side="bottom", fill="x", padx=28, pady=(6, 16))
        composer = tk.Frame(composer_outer, bg=INPUT_BG, highlightbackground="#505157",
                            highlightthickness=1)
        composer.pack(fill="x")

        # ปุ่มส่งต้อง pack ก่อน (side="right") เพื่อจองความกว้างของตัวเองไว้เสมอ —
        # เหตุผลเดียวกับ composer_outer ด้านบน (มิฉะนั้นช่อง input ที่ expand=True จะแย่งพื้นที่จนปุ่มหาย)
        self.send_button = tk.Button(composer, text="ส่ง  ↑", command=self.send_message,
                                     bg=GREEN, fg="#082b1d", activebackground="#23d58c",
                                     activeforeground="#082b1d", relief="flat",
                                     font=self.f_ui_bold, padx=16, pady=8, cursor="hand2")
        self.send_button.pack(side="right", padx=10, pady=10)

        self.input = tk.Text(composer, height=3, wrap="word", bg=INPUT_BG, fg=TEXT,
                             insertbackground=TEXT, relief="flat", padx=14, pady=10,
                             font=self.f_body, undo=True)
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Return>", self.on_enter)
        self.input.bind("<KeyRelease>", self._autosize_input)

        self.hint_label = tk.Label(
            composer_outer,
            text="Enter ส่ง • Shift+Enter ขึ้นบรรทัด • Esc หยุด • Ctrl+N แชตใหม่ • "
                 "Ctrl+±/ล้อเลื่อน ย่อ-ขยาย UI • Ctrl+0 รีเซ็ตขนาด",
            bg=BG, fg=MUTED, font=self.f_small)
        self.hint_label.pack(pady=(6, 0))

        self.chat = ScrolledText(main, wrap="word", bg=BG, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", borderwidth=0, padx=34, pady=16,
                                 spacing1=2, spacing3=6, font=self.f_body, state="disabled")
        self.chat.pack(fill="both", expand=True)
        self._configure_tags()

    def _configure_tags(self):
        c = self.chat
        c.tag_configure("user_name", foreground=BLUE, font=self.f_name, spacing1=10)
        c.tag_configure("ai_name", foreground=GREEN, font=self.f_name, spacing1=10)
        c.tag_configure("body", foreground=TEXT, font=self.f_body, lmargin1=4, lmargin2=4)
        c.tag_configure("bold", foreground=TEXT, font=self.f_bold)
        c.tag_configure("h1", foreground=TEXT, font=self.f_h1, spacing1=8, spacing3=4)
        c.tag_configure("h3", foreground=TEXT, font=self.f_h3, spacing1=6, spacing3=2)
        c.tag_configure("icode", foreground="#ffd580", font=self.f_mono, background=CODE_BG)
        c.tag_configure("code", foreground="#d7e6ff", font=self.f_mono, background=CODE_BG,
                        lmargin1=18, lmargin2=18, rmargin=18, spacing1=1, spacing3=1)
        c.tag_configure("thinking", foreground=MUTED, font=self.f_italic,
                        lmargin1=18, lmargin2=18)
        c.tag_configure("muted", foreground=MUTED, font=self.f_small)
        c.tag_configure("error", foreground=RED, font=self.f_body)
        c.tag_configure("toggle", foreground="#8ab4f8", font=self.f_small)
        c.tag_configure("italic", foreground=TEXT, font=self.f_italic)
        c.tag_configure("strike", foreground=MUTED, font=self.f_body, overstrike=1)
        c.tag_configure("link", foreground=BLUE, font=self.f_body, underline=1)
        c.tag_configure("quote", foreground=MUTED, font=self.f_italic,
                        lmargin1=22, lmargin2=22)
        c.tag_configure("hr", foreground=MUTED, font=self.f_small)
        c.tag_configure("table", foreground=TEXT, font=self.f_mono,
                        lmargin1=18, lmargin2=18)
        c.tag_configure("table_head", foreground=GREEN, font=self.f_mono,
                        lmargin1=18, lmargin2=18)

    def _bind_keys(self):
        self.root.bind("<Control-n>", lambda e: self.new_chat())
        self.root.bind("<Escape>", lambda e: self.stop_generation())
        self.root.bind("<Control-Return>", lambda e: (self.send_message(), "break")[1])
        self.root.bind("<Control-plus>", lambda e: self._zoom(1))
        self.root.bind("<Control-equal>", lambda e: self._zoom(1))
        self.root.bind("<Control-minus>", lambda e: self._zoom(-1))
        self.root.bind("<Control-0>", lambda e: self._zoom_reset())
        self.root.bind("<Control-MouseWheel>", self._on_ctrl_wheel)   # Windows / macOS
        self.root.bind("<Control-Button-4>", lambda e: self._zoom(1))  # Linux scroll up
        self.root.bind("<Control-Button-5>", lambda e: self._zoom(-1))  # Linux scroll down

    def _on_ctrl_wheel(self, event):
        self._zoom(1 if event.delta > 0 else -1)
        return "break"

    def _zoom(self, delta):
        self._apply_zoom(delta)
        self.render_chat()

    def _zoom_reset(self):
        self.cfg.font_size = 12
        self._apply_zoom(0)
        self.render_chat()

    def _autosize_input(self, _event=None):
        lines = int(self.input.index("end-1c").split(".")[0])
        self.input.configure(height=max(3, min(8, lines)))

    # ---------------------------------------------------------- history --
    def refresh_history(self):
        keyword = self.search_var.get().strip().lower()
        self.filtered = []
        for chat in reversed(self.store.chats):
            if not keyword:
                self.filtered.append(chat)
                continue
            haystack = chat.get("title", "") + " " + " ".join(
                m.get("content", "") for m in chat.get("messages", []))
            if keyword in haystack.lower():
                self.filtered.append(chat)

        self.history_list.delete(0, "end")
        for chat in self.filtered:
            marker = "…" if len(chat.get("title", "")) > 32 else ""
            self.history_list.insert("end", "  " + (chat.get("title") or "แชตใหม่")[:32] + marker)
        for index, chat in enumerate(self.filtered):
            if chat is self.current_chat:
                self.history_list.selection_clear(0, "end")
                self.history_list.selection_set(index)
                break

    def new_chat(self):
        if self.running:
            return
        self.store.chats = [c for c in self.store.chats if c.get("messages")]
        chat = self.store.new_chat()
        self.store.chats.append(chat)
        self.current_chat = chat
        self.expanded_think.clear()
        self.refresh_history()
        self.render_chat()
        self.input.focus_set()

    def open_selected_chat(self, _event=None):
        if self.running:
            return
        selected = self.history_list.curselection()
        if not selected:
            return
        chat = self.filtered[selected[0]]
        if chat is not self.current_chat:
            self.current_chat = chat
            self.expanded_think.clear()
            self.render_chat()

    def rename_chat(self, _event=None):
        if not self.current_chat:
            return
        name = simpledialog.askstring("เปลี่ยนชื่อแชต", "ชื่อใหม่:",
                                      initialvalue=self.current_chat.get("title", ""),
                                      parent=self.root)
        if name:
            self.current_chat["title"] = name.strip()[:60]
            self.title_label.configure(text=self.current_chat["title"])
            self.store.save()
            self.refresh_history()

    def delete_chat(self):
        if self.running or not self.current_chat:
            return
        if not messagebox.askyesno("ลบแชต", "ต้องการลบการสนทนานี้หรือไม่?"):
            return
        self.store.chats.remove(self.current_chat)
        self.current_chat = None
        self.store.save()
        if self.store.chats:
            self.current_chat = self.store.chats[-1]
            self.refresh_history()
            self.render_chat()
        else:
            self.new_chat()

    def export_markdown(self):
        if not self.current_chat or not self.current_chat.get("messages"):
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".md", initialfile=f"{self.current_chat.get('title', 'chat')}.md",
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt")])
        if not path:
            return
        lines = [f"# {self.current_chat.get('title', 'Chat')}", ""]
        for message in self.current_chat["messages"]:
            who = "คุณ" if message["role"] == "user" else message.get("model", "AI")
            lines += [f"**{who}**", "", message.get("content", ""), ""]
        try:
            Path(path).write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc))

    # ----------------------------------------------------------- render --
    def render_chat(self):
        chat = self.chat
        chat.configure(state="normal")
        chat.delete("1.0", "end")
        self.title_label.configure(text=self.current_chat.get("title", "แชตใหม่"))

        messages = self.current_chat.get("messages", [])
        if not messages:
            chat.insert("end", "สวัสดีครับ 👋\n", "ai_name")
            chat.insert("end", f"กำลังใช้ {self.cfg.model} บนเครื่องของคุณ ถามอะไรได้เลย\n", "body")
        for index, message in enumerate(messages):
            if message["role"] == "user":
                chat.insert("end", "คุณ\n", "user_name")
                insert_inline(chat, message.get("content", ""))
                chat.insert("end", "\n", "body")
            else:
                chat.insert("end", message.get("model", self.cfg.model) + "\n", "ai_name")
                thinking = (message.get("thinking") or "").strip()
                if thinking:
                    self._insert_think_block(index, thinking)
                insert_markdown(chat, message.get("content", ""))
                footer = []
                if message.get("interrupted"):
                    footer.append("หยุดกลางคัน")
                stats = message.get("stats") or {}
                if stats.get("tokens"):
                    footer.append(f"{stats['tokens']} tokens · {stats.get('tps', 0):.1f} tok/s")
                if footer:
                    chat.insert("end", "  ".join(footer) + "\n", "muted")
        chat.configure(state="disabled")
        chat.see("end")

    def _insert_think_block(self, index: int, thinking: str):
        expanded = index in self.expanded_think
        tag = f"think{index}"
        label = "▾ ซ่อนกระบวนการคิด" if expanded else f"▸ แสดงกระบวนการคิด ({len(thinking)} ตัวอักษร)"
        self.chat.insert("end", label + "\n", ("toggle", tag))
        self.chat.tag_bind(tag, "<Button-1>", lambda e, i=index: self._toggle_think(i))
        self.chat.tag_bind(tag, "<Enter>", lambda e: self.chat.configure(cursor="hand2"))
        self.chat.tag_bind(tag, "<Leave>", lambda e: self.chat.configure(cursor=""))
        if expanded:
            self.chat.insert("end", thinking + "\n", "thinking")

    def _toggle_think(self, index: int):
        self.expanded_think.symmetric_difference_update({index})
        self.render_chat()

    def _at_bottom(self) -> bool:
        return self.chat.yview()[1] > 0.995

    def append_stream(self, text: str, tag="body"):
        stick = self._at_bottom()
        self.chat.configure(state="normal")
        self.chat.insert("end", text, tag)
        self.chat.configure(state="disabled")
        if stick:
            self.chat.see("end")

    def _redraw_stream(self):
        """เรนเดอร์ข้อความที่กำลัง stream ใหม่ทั้งก้อนเป็น markdown แบบสด"""
        stick = self._at_bottom()
        chat = self.chat
        chat.configure(state="normal")
        chat.delete("stream_start", "end-1c")
        if self.stream_think_text:
            chat.insert("end", "กำลังคิด...\n", "muted")
            chat.insert("end", self.stream_think_text, "thinking")
            chat.insert("end", "\n", "body")
        if self.stream_content_text:
            insert_markdown(chat, self.stream_content_text)
        chat.configure(state="disabled")
        if stick:
            chat.see("end")

    # ------------------------------------------------------------- send --
    def on_enter(self, event):
        if event.state & 0x0001:            # Shift
            return None
        self.send_message()
        return "break"

    def send_message(self):
        if self.running:
            self.stop_generation()
            return
        prompt = self.input.get("1.0", "end-1c").strip()
        if not prompt:
            return
        self.input.delete("1.0", "end")
        self._autosize_input()

        if not self.current_chat["messages"]:
            self.current_chat["title"] = prompt.replace("\n", " ").strip()[:40]
        self.current_chat["messages"].append({"role": "user", "content": prompt})
        self._start_generation()

    def regenerate(self):
        if self.running or not self.current_chat:
            return
        messages = self.current_chat["messages"]
        while messages and messages[-1]["role"] == "assistant":
            messages.pop()
        if not messages:
            return
        self.expanded_think.clear()
        self._start_generation()

    def _start_generation(self):
        self.current_chat["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.render_chat()
        self.append_stream(self.cfg.model + "\n", "ai_name")

        self.running = True
        self.generation += 1
        self.stop_event.clear()
        self.stream_think_text = ""
        self.stream_content_text = ""
        self.chat.mark_set("stream_start", "end-1c")
        self.chat.mark_gravity("stream_start", "left")
        self.send_button.configure(text="หยุด  ■", bg=RED, fg="#3b0d10")
        self.status_label.configure(text="กำลังคิด...")
        self.refresh_history()
        self.store.save()

        payload = self._payload_messages()
        options = {"temperature": float(self.cfg.temperature), "num_ctx": int(self.cfg.num_ctx)}
        threading.Thread(target=self._worker,
                         args=(payload, options, self.generation, self.cfg.model),
                         daemon=True).start()

    def _payload_messages(self) -> list[dict]:
        history = [m for m in self.current_chat["messages"] if m["role"] in ("user", "assistant")]
        if self.cfg.max_history_messages > 0:
            history = history[-self.cfg.max_history_messages:]
        messages = []
        if self.cfg.system_prompt.strip():
            messages.append({"role": "system", "content": self.cfg.system_prompt.strip()})
        # ไม่ส่ง thinking กลับไป — เปลืองบริบทและ Ollama ไม่ต้องการ
        messages += [{"role": m["role"], "content": m.get("content", "")} for m in history]
        return messages

    def stop_generation(self):
        if not self.running:
            return
        self.stop_event.set()
        self.status_label.configure(text="กำลังหยุด...")
        threading.Thread(target=self.client.cancel, daemon=True).start()

    # ----------------------------------------------------------- worker --
    def _worker(self, messages, options, generation, model):
        splitter = ThinkSplitter()
        content: list[str] = []
        thinking: list[str] = []
        stats: dict = {}

        def emit(event):
            self.events.put((generation, event))

        try:
            think = self.cfg.think if self.cfg.think else None
            for obj in self.client.chat_stream(messages, model, options, think,
                                               self.cfg.request_timeout):
                if self.stop_event.is_set():
                    break
                message = obj.get("message") or {}
                reasoning = message.get("thinking") or ""
                if reasoning:
                    thinking.append(reasoning)
                    emit(("think", reasoning))
                piece = message.get("content") or ""
                if piece:
                    for kind, text in splitter.feed(piece):
                        (thinking if kind == "think" else content).append(text)
                        emit((kind, text))
                if obj.get("done"):
                    duration = obj.get("eval_duration") or 0
                    tokens = obj.get("eval_count") or 0
                    stats = {"tokens": tokens,
                             "tps": (tokens / (duration / 1e9)) if duration else 0.0}
            for kind, text in splitter.flush():
                (thinking if kind == "think" else content).append(text)
                emit((kind, text))
            emit(("done", {"content": "".join(content), "thinking": "".join(thinking),
                           "interrupted": self.stop_event.is_set(), "stats": stats,
                           "model": model}))
        except Exception as exc:                                   # noqa: BLE001
            if self.stop_event.is_set():
                emit(("done", {"content": "".join(content), "thinking": "".join(thinking),
                               "interrupted": True, "stats": stats, "model": model}))
            else:
                emit(("error", self._friendly_error(exc)))

    def _friendly_error(self, exc: Exception) -> str:
        if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
            return (f"เชื่อมต่อ Ollama ไม่สำเร็จ ({self.cfg.host})\n"
                    f"{exc}\nตรวจว่ารัน `ollama serve` อยู่ และ host ในหน้าตั้งค่าถูกต้อง")
        if isinstance(exc, OllamaError):
            return f"Ollama ตอบกลับข้อผิดพลาด: {exc}\nลองรัน `ollama pull {self.cfg.model}` ก่อน"
        return f"เกิดข้อผิดพลาด: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------ event pump --
    def _process_events(self):
        batch = []
        try:
            while True:
                batch.append(self.events.get_nowait())
        except queue.Empty:
            pass

        merged: list[tuple[str, object]] = []
        for generation, (kind, payload) in batch:
            if generation != self.generation:
                continue                                    # ผลลัพธ์ค้างจากรอบก่อน
            if kind in ("think", "content") and merged and merged[-1][0] == kind:
                merged[-1] = (kind, merged[-1][1] + payload)
            else:
                merged.append((kind, payload))

        changed = False
        for kind, payload in merged:
            if kind == "think":
                self.stream_think_text += payload
                changed = True
            elif kind == "content":
                self.stream_content_text += payload
                changed = True
            elif kind == "done":
                if changed:
                    self._redraw_stream()
                    changed = False
                self._on_done(payload)
            elif kind == "error":
                if changed:
                    self._redraw_stream()
                    changed = False
                self.append_stream("\n" + payload + "\n", "error")
                self._finish()
        if changed:
            self._redraw_stream()
        self.root.after(40, self._process_events)

    def _on_done(self, payload: dict):
        if payload["content"] or payload["thinking"]:
            self.current_chat["messages"].append({
                "role": "assistant",
                "content": payload["content"].strip(),
                "thinking": payload["thinking"].strip(),
                "model": payload["model"],
                "stats": payload["stats"],
                "interrupted": payload["interrupted"],
            })
        self.current_chat["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._finish()
        self.render_chat()          # เรนเดอร์ซ้ำเพื่อจัดรูปแบบ Markdown ให้สวย

    def _finish(self):
        self.running = False
        self.stop_event.clear()
        self.send_button.configure(text="ส่ง  ↑", bg=GREEN, fg="#082b1d")
        self.status_label.configure(text="พร้อมใช้งาน")
        self.store.save()
        self.refresh_history()
        self.input.focus_set()

    # ----------------------------------------------------- models/config --
    def _refresh_models(self):
        try:
            models = self.client.list_models()
            self.root.after(0, lambda: self._on_models(models))
        except Exception:                                           # noqa: BLE001
            self.root.after(0, lambda: self.conn_label.configure(
                text="●  ต่อ Ollama ไม่ได้", fg=RED))

    def _on_models(self, models: list[str]):
        if not models:
            self.conn_label.configure(text="●  ยังไม่มีโมเดล", fg=RED)
            return
        self.model_box.configure(values=models)
        if self.cfg.model not in models:
            self.cfg.model = models[0]
            self.cfg.save()
            self.model_var.set(self.cfg.model)
        self.conn_label.configure(text=f"●  ออนไลน์ · {len(models)} โมเดล", fg=GREEN)

    def on_model_change(self, _event=None):
        self.cfg.model = self.model_var.get()
        self.cfg.save()
        self.status_label.configure(text=f"เปลี่ยนเป็น {self.cfg.model}")

    def open_settings(self):
        SettingsDialog(self.root, self)

    def apply_settings(self, new_cfg: Config):
        host_changed = new_cfg.host != self.cfg.host
        self.cfg = new_cfg
        self.cfg.save()
        self.model_var.set(self.cfg.model)
        if host_changed:
            self.client = OllamaClient(self.cfg.host)
        threading.Thread(target=self._refresh_models, daemon=True).start()
        self.render_chat()

    def on_close(self):
        self.stop_event.set()
        self.client.cancel()
        self.store.save()
        self.cfg.save()
        self.root.destroy()


# ------------------------------------------------------------ settings ----
class SettingsDialog(tk.Toplevel):
    def __init__(self, master, app: ChatApp):
        super().__init__(master, bg=BG)
        self.app = app
        self.title("ตั้งค่า")
        self.geometry("520x560")
        self.transient(master)
        self.resizable(False, False)

        cfg = app.cfg
        pad = dict(padx=20, sticky="w")

        def label(text, row):
            tk.Label(self, text=text, bg=BG, fg=MUTED, font=app.f_small).grid(
                row=row, column=0, pady=(12, 2), **pad)

        def entry(value, row, width=48):
            widget = tk.Entry(self, bg=INPUT_BG, fg=TEXT, insertbackground=TEXT,
                              relief="flat", font=app.f_ui, width=width)
            widget.insert(0, str(value))
            widget.grid(row=row, column=0, padx=20, sticky="we", ipady=5)
            return widget

        label("Ollama host", 0)
        self.host = entry(cfg.host, 1)

        label("System prompt (บทบาทของ AI)", 2)
        self.system = tk.Text(self, height=6, bg=INPUT_BG, fg=TEXT, insertbackground=TEXT,
                              relief="flat", font=app.f_ui, wrap="word", padx=8, pady=6)
        self.system.insert("1.0", cfg.system_prompt)
        self.system.grid(row=3, column=0, padx=20, sticky="we")

        label("Temperature (ความสร้างสรรค์)", 4)
        self.temperature = tk.DoubleVar(value=cfg.temperature)
        tk.Scale(self, from_=0.0, to=1.5, resolution=0.05, orient="horizontal",
                 variable=self.temperature, bg=BG, fg=TEXT, troughcolor=PANEL,
                 highlightthickness=0, relief="flat", font=app.f_small,
                 activebackground=GREEN).grid(row=5, column=0, padx=16, sticky="we")

        label("Context window (num_ctx)", 6)
        self.num_ctx = entry(cfg.num_ctx, 7, width=12)

        label("จำนวนข้อความย้อนหลังที่ส่งให้โมเดล (0 = ทั้งหมด)", 8)
        self.max_history = entry(cfg.max_history_messages, 9, width=12)

        label("Timeout ต่อคำตอบ (วินาที)", 10)
        self.timeout = entry(cfg.request_timeout, 11, width=12)

        self.think = tk.BooleanVar(value=cfg.think)
        tk.Checkbutton(self, text="เปิดโหมดคิด (thinking) สำหรับโมเดลที่รองรับ",
                       variable=self.think, bg=BG, fg=TEXT, selectcolor=PANEL,
                       activebackground=BG, activeforeground=TEXT, font=app.f_ui,
                       highlightthickness=0).grid(row=12, column=0, padx=16, pady=14, sticky="w")

        buttons = tk.Frame(self, bg=BG)
        buttons.grid(row=13, column=0, pady=10, sticky="e", padx=20)
        app._flat_button(buttons, "ยกเลิก", self.destroy).pack(side="right", padx=6)
        tk.Button(buttons, text="บันทึก", command=self.save, bg=GREEN, fg="#082b1d",
                  relief="flat", font=app.f_ui_bold, padx=16, pady=6,
                  cursor="hand2").pack(side="right")

        self.columnconfigure(0, weight=1)
        self.grab_set()

    def save(self):
        cfg = Config(**asdict(self.app.cfg))
        cfg.host = self.host.get().strip() or cfg.host
        cfg.system_prompt = self.system.get("1.0", "end-1c")
        cfg.temperature = float(self.temperature.get())
        cfg.model = self.app.model_var.get()
        for attr, widget, cast in (("num_ctx", self.num_ctx, int),
                                   ("max_history_messages", self.max_history, int),
                                   ("request_timeout", self.timeout, int)):
            try:
                setattr(cfg, attr, cast(widget.get()))
            except ValueError:
                pass
        cfg.think = bool(self.think.get())
        self.app.apply_settings(cfg)
        self.destroy()


def main():
    root = tk.Tk()
    ChatApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
