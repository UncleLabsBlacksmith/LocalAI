#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Local AI Chat • Ollama — tkinter edition
แปลงจาก local_ollama_chat.html เป็นโปรแกรม Desktop ด้วย tkinter

ความสามารถ:
  • แชตแบบ streaming กับ Ollama ในเครื่อง (/api/chat)
  • เลือก/โหลดรายชื่อโมเดล (/api/tags)
  • ประวัติแชต: ค้นหา เปลี่ยนชื่อ ลบ (บันทึกเป็นไฟล์ JSON ในโฟลเดอร์ home)
  • แสดงผล Markdown (หัวข้อ, ลิสต์, โค้ดบล็อก, ตัวหนา/เอียง, ลิงก์, quote)
  • โหมด Thinking (field "thinking" และแท็ก <think>…</think>) แบบพับ/ขยายได้
  • ตอบใหม่, ส่งออก .md, คัดลอกคำตอบ/โค้ด, แสดง tokens และ tok/s
  • คีย์ลัด: Enter ส่ง • Shift+Enter ขึ้นบรรทัด • Esc หยุด • Ctrl+N แชตใหม่

ใช้เฉพาะ Python standard library (ไม่ต้อง pip install อะไรเพิ่ม)
"""

import datetime
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_TITLE = "Local AI Chat • Ollama"
DATA_FILE = Path.home() / ".local_ai_ollama_chat.json"

C = {
    "bg": "#202123", "side": "#151617", "panel": "#2c2d30", "panel_hover": "#3a3c3e",
    "line": "#414348", "border": "#4b4c50", "text": "#f0f0f0", "muted": "#a7a7aa",
    "accent": "#21c482", "accent_hover": "#35d793", "accent_text": "#092b1d",
    "red": "#eb747c", "stop_bg": "#4a2d31", "user_bubble": "#30353a",
    "code_bg": "#111315", "inline_code": "#38393c", "field": "#28292b",
    "link": "#8ad3ff", "user_name": "#a2dafa", "active_row": "#303134",
    "composer": "#2e3032", "thought_bg": "#26272a", "thought_fg": "#b2b5b7",
    "quote": "#c9c9ce", "select": "#2f5545",
}

DEFAULTS = {
    "host": "http://localhost:11434", "model": "", "systemPrompt": "",
    "temperature": 0.7, "numCtx": 8192, "maxHistory": 20, "think": False,
}

INLINE_RE = re.compile(
    r"(`[^`\n]+`|\*\*[^*\n]+\*\*|~~[^~\n]+~~|\*[^*\n]+\*|\[[^\]]+\]\(https?://[^\s)]+\))"
)
FENCE_RE = re.compile(r"^\s*```\s*([\w.+-]*)")
LIST_RE = re.compile(r"^([-*]|\d+[.)])\s+(.+)")
HOST_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", re.I)


def now_ms():
    return int(time.time() * 1000)


def pick_font(candidates, fallback):
    families = set(tkfont.families())
    for name in candidates:
        if name in families:
            return name
    return fallback


class App:
    # ------------------------------------------------------------ setup
    def __init__(self, root):
        self.root = root
        self.data = self.load_data()
        self.active = None          # สถานะการตอบที่กำลัง stream อยู่
        self.gen = 0                # เลขรุ่นของงาน ใช้ทิ้งข้อความค้างจากงานที่ถูกหยุด
        self.q = queue.Queue()
        self.open_thoughts = set()
        self.hist_ids = []
        self._dyn_tags, self._stream_tags = [], []
        self._tag_target = self._dyn_tags
        self._tagn = 0
        self._pad = None

        root.title(APP_TITLE)
        root.geometry("1180x760")
        root.minsize(760, 480)
        root.configure(bg=C["bg"])

        self.setup_fonts()
        self.setup_style()
        self.build_ui()
        self.bind_keys()

        self.ensure_chat()
        self.render_all()
        self.resize_prompt()
        self.refresh_models()
        root.after(40, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def setup_fonts(self):
        ui = pick_font(["Leelawadee UI", "Noto Sans Thai UI", "Noto Sans Thai", "Sarabun",
                        "Thonburi", "Segoe UI", "Tahoma"], "TkDefaultFont")
        mono = pick_font(["Cascadia Mono", "Consolas", "Menlo", "JetBrains Mono",
                          "DejaVu Sans Mono", "Courier New"], "TkFixedFont")
        F = tkfont.Font
        self.f_base = F(family=ui, size=11)
        self.f_bold = F(family=ui, size=11, weight="bold")
        self.f_italic = F(family=ui, size=11, slant="italic")
        self.f_small = F(family=ui, size=9)
        self.f_label = F(family=ui, size=9)
        self.f_header = F(family=ui, size=10, weight="bold")
        self.f_title = F(family=ui, size=13, weight="bold")
        self.f_brand = F(family=ui, size=12, weight="bold")
        self.f_h2 = F(family=ui, size=16, weight="bold")
        self.f_h3 = F(family=ui, size=13, weight="bold")
        self.f_empty = F(family=ui, size=22, weight="bold")
        self.f_mono = F(family=mono, size=10)
        self.f_gap = F(family=ui, size=6)

    def setup_style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=C["bg"], foreground=C["text"], font=self.f_base,
                    bordercolor=C["line"], darkcolor=C["panel"], lightcolor=C["panel"],
                    troughcolor=C["field"], focuscolor=C["accent"])
        s.configure("TButton", background=C["panel"], foreground=C["text"], padding=(12, 8),
                    borderwidth=1, bordercolor="#424348", relief="flat")
        s.map("TButton", background=[("disabled", C["panel"]), ("active", C["panel_hover"])],
              foreground=[("disabled", C["muted"])])
        s.configure("Accent.TButton", background=C["accent"], foreground=C["accent_text"],
                    bordercolor=C["accent"], font=self.f_bold)
        s.map("Accent.TButton", background=[("disabled", "#1a7a54"), ("active", C["accent_hover"])])
        s.configure("Stop.TButton", background=C["stop_bg"], foreground=C["red"],
                    bordercolor=C["red"], font=self.f_bold)
        s.map("Stop.TButton", background=[("active", "#5c3439")])
        s.configure("Small.TButton", padding=(10, 5), font=self.f_small)
        s.configure("Ghost.TButton", background=C["side"], foreground=C["muted"],
                    bordercolor=C["side"], padding=(6, 4), font=self.f_small)
        s.map("Ghost.TButton", background=[("active", "#2a2b2e")], foreground=[("active", C["text"])])
        s.configure("Top.TButton", background=C["bg"], foreground=C["text"],
                    bordercolor=C["bg"], padding=(8, 5))
        s.map("Top.TButton", background=[("active", C["panel"])])

        for w in ("TEntry", "TSpinbox", "TCombobox"):
            s.configure(w, fieldbackground=C["field"], foreground=C["text"], insertcolor=C["text"],
                        bordercolor=C["border"], lightcolor=C["field"], darkcolor=C["field"],
                        background=C["panel"], arrowcolor=C["text"], padding=6)
            s.map(w, bordercolor=[("focus", C["accent"])], lightcolor=[("focus", C["accent"])])
        s.map("TCombobox", fieldbackground=[("readonly", C["field"])],
              foreground=[("readonly", C["text"])], selectbackground=[("readonly", C["field"])],
              selectforeground=[("readonly", C["text"])], bordercolor=[("focus", C["accent"])])
        self.root.option_add("*TCombobox*Listbox.background", C["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", C["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", C["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", C["accent_text"])
        self.root.option_add("*TCombobox*Listbox.font", self.f_base)

        s.configure("Vertical.TScrollbar", background=C["panel"], troughcolor=C["bg"],
                    bordercolor=C["bg"], lightcolor=C["panel"], darkcolor=C["panel"],
                    arrowcolor=C["muted"], gripcount=0, arrowsize=12)
        s.map("Vertical.TScrollbar", background=[("active", C["line"])])
        s.configure("TCheckbutton", background=C["panel"], foreground=C["text"],
                    indicatorbackground=C["field"], indicatorforeground=C["accent_text"])
        s.map("TCheckbutton", indicatorbackground=[("selected", C["accent"])],
              background=[("active", C["panel"])])
        s.configure("Horizontal.TScale", background=C["accent"], troughcolor=C["field"],
                    bordercolor=C["border"], lightcolor=C["accent"], darkcolor=C["accent"])

    # ------------------------------------------------------------ UI
    def build_ui(self):
        # ---------- sidebar
        self.sidebar = sb = tk.Frame(self.root, bg=C["side"], width=272)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)
        tk.Frame(self.root, bg="#2d2d2d", width=1).pack(side="left", fill="y")
        self.sep = self.root.pack_slaves()[-1]

        inner = tk.Frame(sb, bg=C["side"])
        inner.pack(fill="both", expand=True, padx=12, pady=16)

        brand = tk.Frame(inner, bg=C["side"])
        brand.pack(fill="x", padx=6, pady=(0, 12))
        for txt, col in (("● LOCAL ", C["text"]), ("AI", C["accent"]), (" CHAT", C["text"])):
            tk.Label(brand, text=txt, fg=col, bg=C["side"], font=self.f_brand).pack(side="left")

        ttk.Button(inner, text="＋  แชตใหม่", command=self.new_chat).pack(fill="x")

        self._section(inner, "โมเดลใน Ollama")
        self.model_var = tk.StringVar(value=self.data["settings"].get("model") or "กำลังโหลด...")
        self.model_box = ttk.Combobox(inner, textvariable=self.model_var, state="readonly")
        self.model_box.pack(fill="x")
        self.model_box.bind("<<ComboboxSelected>>", self.on_model_change)
        self.refresh_btn = ttk.Button(inner, text="↻  โหลดรายชื่อโมเดลใหม่", command=self.refresh_models)
        self.refresh_btn.pack(fill="x", pady=(8, 0))

        self._section(inner, "ค้นหาประวัติ")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.render_history())
        ttk.Entry(inner, textvariable=self.search_var).pack(fill="x")

        # footer (pack ก่อน list เพื่อให้อยู่ล่างสุดเสมอ)
        footer = tk.Frame(inner, bg=C["side"])
        footer.pack(side="bottom", fill="x", pady=(10, 0))
        tk.Frame(inner, bg="#303033", height=1).pack(side="bottom", fill="x", pady=(8, 0))
        self.dot = tk.Canvas(footer, width=10, height=10, bg=C["side"], highlightthickness=0)
        self.dot.pack(side="left", padx=(4, 6))
        self.dot_id = self.dot.create_oval(1, 1, 9, 9, fill="#777", outline="")
        self.conn_label = tk.Label(footer, text="กำลังตรวจสอบ...", fg=C["muted"], bg=C["side"],
                                   font=self.f_small, anchor="w")
        self.conn_label.pack(side="left", fill="x", expand=True)
        ttk.Button(footer, text="⚙ ตั้งค่า", style="Ghost.TButton",
                   command=self.open_settings).pack(side="right")

        actions = tk.Frame(inner, bg=C["side"])
        actions.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(actions, text="✎ เปลี่ยนชื่อ", style="Ghost.TButton",
                   command=self.rename_chat).pack(side="left")
        ttk.Button(actions, text="× ลบแชต", style="Ghost.TButton",
                   command=self.delete_chat).pack(side="left", padx=4)

        histf = tk.Frame(inner, bg=C["side"])
        histf.pack(fill="both", expand=True, pady=(12, 0))
        self.hist = tk.Listbox(histf, bg=C["side"], fg=C["text"], font=self.f_base, bd=0,
                               highlightthickness=0, activestyle="none", relief="flat",
                               selectbackground=C["active_row"], selectforeground=C["text"],
                               exportselection=False)
        hs = ttk.Scrollbar(histf, orient="vertical", command=self.hist.yview)
        self.hist.configure(yscrollcommand=hs.set)
        hs.pack(side="right", fill="y")
        self.hist.pack(side="left", fill="both", expand=True)
        self.hist.bind("<<ListboxSelect>>", self.on_hist_select)
        self.hist.bind("<Double-Button-1>", lambda e: self.rename_chat())
        self.hist.bind("<Delete>", lambda e: self.delete_chat())
        self.hist.bind("<Button-3>", self.on_hist_menu)
        self.hist.bind("<Button-2>", self.on_hist_menu)  # macOS
        self.hist_menu = tk.Menu(self.root, tearoff=0, bg=C["panel"], fg=C["text"],
                                 activebackground=C["accent"], activeforeground=C["accent_text"],
                                 bd=0, font=self.f_base)
        self.hist_menu.add_command(label="✎  เปลี่ยนชื่อ", command=self.rename_chat)
        self.hist_menu.add_command(label="↓  ส่งออก .md", command=self.export_md)
        self.hist_menu.add_separator()
        self.hist_menu.add_command(label="×  ลบแชต", command=self.delete_chat)

        # ---------- main
        self.main = main = tk.Frame(self.root, bg=C["bg"])
        main.pack(side="left", fill="both", expand=True)

        top = tk.Frame(main, bg=C["bg"], height=60)
        top.pack(fill="x")
        top.pack_propagate(False)
        ttk.Button(top, text="☰", style="Top.TButton", width=3,
                   command=self.toggle_sidebar).pack(side="left", padx=(12, 6))
        self.title_label = tk.Label(top, text="แชตใหม่", fg=C["text"], bg=C["bg"],
                                    font=self.f_title, anchor="w")
        self.title_label.pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="↓ .md", style="Small.TButton",
                   command=self.export_md).pack(side="right", padx=(6, 20))
        ttk.Button(top, text="↻ ตอบใหม่", style="Small.TButton",
                   command=lambda: self.send_message(True)).pack(side="right")
        tk.Frame(main, bg="#36373b", height=1).pack(fill="x")

        # composer (pack ก่อน เพื่อให้ติดขอบล่าง)
        self.composer_wrap = cw = tk.Frame(main, bg=C["bg"])
        cw.pack(side="bottom", fill="x", padx=18, pady=(10, 12))
        tk.Frame(main, bg="#343538", height=1).pack(side="bottom", fill="x")

        comp = tk.Frame(cw, bg=C["composer"], highlightthickness=1,
                        highlightbackground="#505257", highlightcolor=C["accent"])
        comp.pack(fill="x")
        self.comp = comp
        self.prompt = tk.Text(comp, height=2, wrap="word", bg=C["composer"], fg=C["text"],
                              insertbackground=C["text"], bd=0, highlightthickness=0,
                              font=self.f_base, padx=10, pady=10, undo=True,
                              selectbackground=C["select"])
        self.prompt.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        self.send_btn = ttk.Button(comp, text="ส่ง ↑", style="Accent.TButton", width=8,
                                   command=self.send_message)
        self.send_btn.pack(side="right", anchor="s", padx=9, pady=9)
        self.placeholder = tk.Label(comp, text="ถามอะไรก็ได้... (Enter เพื่อส่ง)",
                                    fg="#7d7f84", bg=C["composer"], font=self.f_base, cursor="xterm")
        self.placeholder.place(x=19, y=14)
        self.placeholder.bind("<Button-1>", lambda e: self.prompt.focus_set())
        self.prompt.bind("<FocusIn>", lambda e: comp.configure(highlightbackground=C["accent"]))
        self.prompt.bind("<FocusOut>", lambda e: comp.configure(highlightbackground="#505257"))

        self.status = tk.Label(cw, fg=C["muted"], bg=C["bg"], font=self.f_small,
                               text="Enter ส่ง • Shift+Enter ขึ้นบรรทัด • Esc หยุด • "
                                    "ทุกอย่างประมวลผลด้วย Ollama ในเครื่อง")
        self.status.pack(pady=(7, 0))

        # messages
        mf = tk.Frame(main, bg=C["bg"])
        mf.pack(fill="both", expand=True)
        self.msg = tk.Text(mf, bg=C["bg"], fg=C["text"], font=self.f_base, wrap="word", bd=0,
                           highlightthickness=0, padx=24, pady=20, cursor="arrow",
                           insertwidth=0, selectbackground=C["select"],
                           selectforeground=C["text"], spacing1=1, spacing3=1,
                           state="disabled", takefocus=0)
        ms = ttk.Scrollbar(mf, orient="vertical", command=self.msg.yview)
        self.msg.configure(yscrollcommand=ms.set)
        ms.pack(side="right", fill="y")
        self.msg.pack(side="left", fill="both", expand=True)
        self.msg.bind("<Configure>", self.on_msg_resize)
        self.setup_tags()

    def _section(self, parent, text):
        tk.Label(parent, text=text, fg=C["muted"], bg=C["side"], font=self.f_label,
                 anchor="w").pack(fill="x", padx=4, pady=(14, 4))

    def setup_tags(self):
        t = self.msg
        t.tag_configure("ai_header", font=self.f_header, foreground=C["accent"], spacing1=4, spacing3=6)
        t.tag_configure("user_header", font=self.f_header, foreground=C["user_name"], spacing1=4, spacing3=6)
        t.tag_configure("user", background=C["user_bubble"], lmargin1=0, lmargin2=0,
                        spacing1=3, spacing3=3)
        t.tag_configure("p", spacing3=5)
        t.tag_configure("h2", font=self.f_h2, spacing1=10, spacing3=6)
        t.tag_configure("h3", font=self.f_h3, spacing1=8, spacing3=4)
        t.tag_configure("li", lmargin1=14, lmargin2=34, spacing3=3)
        t.tag_configure("quote", foreground=C["quote"], lmargin1=6, lmargin2=22, spacing3=4)
        t.tag_configure("quote_bar", foreground="#61646a")
        t.tag_configure("hr", foreground=C["line"], spacing1=6, spacing3=6)
        t.tag_configure("code_block", font=self.f_mono, background=C["code_bg"],
                        foreground="#e6e6e6", lmargin1=14, lmargin2=14, rmargin=14)
        t.tag_configure("code_lang", font=self.f_small, foreground=C["muted"], spacing1=6)
        t.tag_configure("code_end", spacing3=4)
        t.tag_configure("inline_code", font=self.f_mono, background=C["inline_code"])
        t.tag_configure("bold", font=self.f_bold)
        t.tag_configure("italic", font=self.f_italic)
        t.tag_configure("strike", overstrike=1)
        t.tag_configure("link", foreground=C["link"], underline=1)
        t.tag_configure("thought_toggle", font=self.f_small, foreground=C["muted"], spacing3=4)
        t.tag_configure("thought", font=self.f_small, foreground=C["thought_fg"],
                        background=C["thought_bg"], lmargin1=12, lmargin2=12, rmargin=12)
        t.tag_configure("meta", font=self.f_small, foreground=C["muted"], spacing1=6)
        t.tag_configure("action", font=self.f_small, foreground=C["muted"], spacing1=4)
        t.tag_configure("gap", font=self.f_gap, spacing3=14)
        t.tag_configure("center", justify="center")
        t.tag_configure("empty_title", font=self.f_empty, spacing3=6)
        t.tag_configure("empty_sub", foreground=C["muted"], spacing3=4)
        for tag in ("inline_code", "bold", "italic", "strike", "link"):
            t.tag_raise(tag)

    def bind_keys(self):
        self.prompt.bind("<Return>", self.on_enter)
        self.prompt.bind("<KP_Enter>", self.on_enter)
        self.prompt.bind("<KeyRelease>", lambda e: (self.resize_prompt(), self.update_placeholder()))
        self.prompt.bind("<<Paste>>", lambda e: self.root.after(1, self.resize_prompt))
        self.root.bind_all("<Escape>", self.on_escape)
        self.root.bind_all("<Control-n>", lambda e: (self.new_chat(), "break")[1])
        self.root.bind_all("<Control-N>", lambda e: (self.new_chat(), "break")[1])

    # ------------------------------------------------------------ data
    def load_data(self):
        st = {"settings": dict(DEFAULTS), "chats": [], "currentId": None}
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                st["settings"].update(saved.get("settings") or {})
                if isinstance(saved.get("chats"), list):
                    st["chats"] = saved["chats"]
                st["currentId"] = saved.get("currentId")
        except FileNotFoundError:
            pass
        except Exception as e:
            print("โหลดข้อมูลแชตไม่ได้:", e, file=sys.stderr)
        for c in st["chats"]:
            for m in c.get("messages", []):
                m.pop("streaming", None)
        return st

    def save(self):
        try:
            tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, DATA_FILE)
        except Exception as e:
            self.set_status("บันทึกไม่ได้: " + str(e))

    def current(self):
        cid = self.data.get("currentId")
        return next((c for c in self.data["chats"] if c["id"] == cid), None)

    def ensure_chat(self):
        if not self.current():
            if self.data["chats"]:
                self.data["currentId"] = self.data["chats"][0]["id"]
            else:
                self.new_chat()

    def new_chat(self):
        if self.active:
            return
        chat = {"id": str(uuid.uuid4()), "title": "แชตใหม่", "messages": [], "updated": now_ms()}
        self.data["chats"].insert(0, chat)
        self.data["currentId"] = chat["id"]
        self.save()
        self.render_all()
        self.prompt.focus_set()

    def _target_chat(self):
        sel = self.hist.curselection()
        if sel and sel[0] < len(self.hist_ids):
            cid = self.hist_ids[sel[0]]
            return next((c for c in self.data["chats"] if c["id"] == cid), None)
        return self.current()

    def rename_chat(self):
        if self.active:
            return
        chat = self._target_chat()
        if not chat:
            return
        name = simpledialog.askstring("เปลี่ยนชื่อแชต", "ชื่อใหม่:", initialvalue=chat["title"],
                                      parent=self.root)
        if name and name.strip():
            chat["title"] = name.strip()[:80]
            chat["updated"] = now_ms()
            self.save()
            self.render_all()

    def delete_chat(self):
        if self.active:
            return
        chat = self._target_chat()
        if not chat or not messagebox.askyesno("ลบแชต", f"ลบการสนทนา “{chat['title']}” หรือไม่?",
                                               parent=self.root):
            return
        self.data["chats"] = [c for c in self.data["chats"] if c["id"] != chat["id"]]
        if self.data["currentId"] == chat["id"]:
            self.data["currentId"] = self.data["chats"][0]["id"] if self.data["chats"] else None
        self.ensure_chat()
        self.save()
        self.render_all()

    # ------------------------------------------------------------ history
    def render_history(self):
        q = self.search_var.get().strip().lower()
        lb = self.hist
        lb.delete(0, "end")
        self.hist_ids = []
        sel = None
        for chat in sorted(self.data["chats"], key=lambda c: c.get("updated", 0), reverse=True):
            if q:
                blob = " ".join([chat["title"]] + [m.get("content") or "" for m in chat["messages"]])
                if q not in blob.lower():
                    continue
            lb.insert("end", "  ▤  " + chat["title"])
            self.hist_ids.append(chat["id"])
            if chat["id"] == self.data.get("currentId"):
                sel = len(self.hist_ids) - 1
        if sel is not None:
            lb.selection_set(sel)
            lb.see(sel)

    def on_hist_select(self, _e=None):
        sel = self.hist.curselection()
        if not sel or sel[0] >= len(self.hist_ids):
            return
        if self.active:
            self.render_history()
            return
        cid = self.hist_ids[sel[0]]
        if cid != self.data.get("currentId"):
            self.data["currentId"] = cid
            self.save()
            self.render_all()

    def on_hist_menu(self, e):
        idx = self.hist.nearest(e.y)
        if idx < 0 or idx >= len(self.hist_ids):
            return
        if not self.active:
            self.hist.selection_clear(0, "end")
            self.hist.selection_set(idx)
            self.on_hist_select()
        self.hist_menu.tk_popup(e.x_root, e.y_root)

    # ------------------------------------------------------------ rendering
    def render_all(self):
        self.render_history()
        self.render_messages()

    def _near_bottom(self):
        return self.msg.yview()[1] > 0.985

    def ins(self, text, tags=()):
        self.msg.insert("end", text, tags)

    def dyn_tag(self, prefix, callback):
        name = f"{prefix}{self._tagn}"
        self._tagn += 1
        t = self.msg
        t.tag_bind(name, "<Button-1>", lambda e: callback())
        t.tag_bind(name, "<Enter>", lambda e: t.configure(cursor="hand2"))
        t.tag_bind(name, "<Leave>", lambda e: t.configure(cursor="arrow"))
        self._tag_target.append(name)
        return name

    def _clear_tags(self, lst):
        for name in lst:
            try:
                self.msg.tag_delete(name)
            except tk.TclError:
                pass
        lst.clear()

    def render_messages(self):
        near = self._near_bottom()
        t = self.msg
        t.configure(state="normal", cursor="arrow")
        t.delete("1.0", "end")
        self._clear_tags(self._dyn_tags)
        self._clear_tags(self._stream_tags)
        t.mark_unset("stream") if "stream" in t.mark_names() else None
        chat = self.current()
        self.title_label.configure(text=(chat or {}).get("title", "แชตใหม่"))
        msgs = (chat or {}).get("messages", [])
        if not msgs:
            self.ins("\n\n\n\n")
            self.ins("สวัสดีครับ 👋\n", ("center", "empty_title"))
            self.ins("Local AI ของคุณ พร้อมตอบด้วย Ollama บนเครื่องนี้\n", ("center",))
            self.ins("\nเลือกโมเดลด้านซ้าย แล้วเริ่มพิมพ์ได้เลย\n", ("center", "empty_sub"))
        else:
            for i, m in enumerate(msgs):
                if i == len(msgs) - 1 and m.get("streaming"):
                    t.mark_set("stream", "end-1c")
                    t.mark_gravity("stream", "left")
                    self._tag_target = self._stream_tags
                self.render_message(chat, i, m)
            self._tag_target = self._dyn_tags
        t.configure(state="disabled")
        if near or (msgs and msgs[-1].get("streaming")):
            t.see("end")

    def render_stream(self):
        t = self.msg
        chat = self.current()
        if "stream" not in t.mark_names() or not chat or not chat["messages"]:
            self.render_messages()
            return
        near = self._near_bottom()
        t.configure(state="normal")
        t.delete("stream", "end")
        self._clear_tags(self._stream_tags)
        self._tag_target = self._stream_tags
        self.render_message(chat, len(chat["messages"]) - 1, chat["messages"][-1])
        self._tag_target = self._dyn_tags
        t.configure(state="disabled")
        if near:
            t.see("end")

    def render_message(self, chat, i, m):
        is_user = m.get("role") == "user"
        if is_user:
            self.ins("คุณ\n", ("user_header",))
            self.ins(" " + (m.get("content") or "").replace("\n", " \n ") + " \n", ("user",))
        else:
            name = m.get("model") or self.data["settings"].get("model") or "Local AI"
            self.ins("✦ " + name + "\n", ("ai_header",))
            streaming = m.get("streaming")
            if m.get("thinking"):
                key = (chat["id"], i)
                opened = key in self.open_thoughts
                if streaming and not m.get("content"):
                    label = ("▾ " if opened else "▸ ") + "กำลังคิด... (คลิกเพื่อดู)"
                else:
                    label = "▾ ซ่อน Thinking" if opened else "▸ แสดง Thinking"

                def toggle(k=key):
                    self.open_thoughts.symmetric_difference_update({k})
                    self.render_messages()
                self.ins(label + "\n", ("thought_toggle", self.dyn_tag("think", toggle)))
                if opened:
                    self.ins(m["thinking"].strip() + "\n", ("thought",))
                    self.ins("\n", ("gap",))
            content = m.get("content") or ""
            if content:
                self.render_markdown(content)
            elif streaming and not m.get("thinking"):
                self.ins("▍ กำลังสร้างคำตอบ...\n", ("meta",))
            stats = m.get("stats")
            if stats or m.get("interrupted"):
                parts = []
                if stats:
                    parts.append(f"{stats.get('tokens', 0)} tokens · {stats.get('tps', 0):.1f} tok/s")
                if m.get("interrupted"):
                    parts.append("หยุดกลางคัน")
                self.ins(" · ".join(parts) + "\n", ("meta",))
            if not streaming:
                tag = self.dyn_tag("copy", lambda c=content: self.copy_text(c, "คัดลอกคำตอบแล้ว"))
                self.ins("⧉ คัดลอก", ("action", tag))
                self.ins("\n", ("action",))
        self.ins("\n", ("gap",))

    def render_inline(self, text, base=()):
        base = tuple(base)
        for part in INLINE_RE.split(text):
            if not part:
                continue
            if part.startswith("`") and part.endswith("`") and len(part) > 2:
                self.ins(part[1:-1], base + ("inline_code",))
            elif part.startswith("**") and part.endswith("**") and len(part) > 4:
                self.ins(part[2:-2], base + ("bold",))
            elif part.startswith("~~") and part.endswith("~~") and len(part) > 4:
                self.ins(part[2:-2], base + ("strike",))
            elif part.startswith("*") and part.endswith("*") and len(part) > 2:
                self.ins(part[1:-1], base + ("italic",))
            elif part.startswith("[") and "](" in part and part.endswith(")"):
                label, url = part[1:-1].split("](", 1)
                tag = self.dyn_tag("link", lambda u=url: webbrowser.open(u))
                self.ins(label, base + ("link", tag))
            else:
                self.ins(part, base)

    def render_markdown(self, text):
        in_code, code, lang = False, [], ""

        def flush_code():
            if lang:
                self.ins(lang + "\n", ("code_block", "code_lang"))
            self.ins("\n".join(code) + "\n", ("code_block",))
            src = "\n".join(code)
            tag = self.dyn_tag("ccopy", lambda s=src: self.copy_text(s, "คัดลอกโค้ดแล้ว"))
            self.ins("⧉ คัดลอกโค้ด", ("action", tag))
            self.ins("\n", ("code_end",))

        for line in text.split("\n"):
            fence = FENCE_RE.match(line)
            if fence:
                if in_code:
                    flush_code()
                    code, in_code = [], False
                else:
                    in_code, lang = True, fence.group(1)
                continue
            if in_code:
                code.append(line)
                continue
            trim = line.strip()
            if not trim:
                continue
            h = re.match(r"^(#{1,6})\s+(.*)", trim)
            if h:
                self.render_inline(h.group(2), ("h2" if len(h.group(1)) == 1 else "h3",))
                self.ins("\n", ("h2" if len(h.group(1)) == 1 else "h3",))
                continue
            if trim.startswith("> ") or trim == ">":
                self.ins("▎ ", ("quote", "quote_bar"))
                self.render_inline(trim[2:], ("quote",))
                self.ins("\n", ("quote",))
                continue
            item = LIST_RE.match(trim)
            if item:
                marker = "•  " if item.group(1) in "-*" else item.group(1).rstrip(")").rstrip(".") + ".  "
                indent = len(line) - len(line.lstrip())
                if indent >= 2:
                    marker = "    " + ("◦  " if marker == "•  " else marker)
                self.ins(marker, ("li",))
                self.render_inline(item.group(2), ("li",))
                self.ins("\n", ("li",))
                continue
            if re.fullmatch(r"[-*_]{3,}", trim):
                self.ins("─" * 48 + "\n", ("hr",))
                continue
            self.render_inline(line, ("p",))
            self.ins("\n", ("p",))
        if in_code:
            flush_code()

    def on_msg_resize(self, e):
        pad = max(20, (e.width - 880) // 2)
        if pad != self._pad:
            self._pad = pad
            self.msg.configure(padx=pad)
            self.composer_wrap.pack_configure(padx=max(18, pad - 4))

    # ------------------------------------------------------------ misc UI
    def set_status(self, text):
        self.status.configure(text=text)

    def set_connection(self, ok, text):
        self.dot.itemconfigure(self.dot_id, fill=C["accent"] if ok else C["red"])
        self.conn_label.configure(text=text)

    def copy_text(self, text, msg):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("✓ " + msg)

    def toggle_sidebar(self):
        if self.sidebar.winfo_ismapped():
            self.sidebar.pack_forget()
            self.sep.pack_forget()
        else:
            self.sidebar.pack(side="left", fill="y", before=self.main)
            self.sep.pack(side="left", fill="y", before=self.main)

    def resize_prompt(self):
        try:
            r = self.prompt.count("1.0", "end", "displaylines")
            n = r[0] if isinstance(r, tuple) else int(r or 1)
        except Exception:
            n = int(self.prompt.index("end-1c").split(".")[0])
        self.prompt.configure(height=min(max(n, 2), 8))

    def update_placeholder(self):
        if self.prompt.get("1.0", "end-1c"):
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=19, y=14)

    def on_enter(self, e):
        if e.state & 0x0001:  # Shift → ขึ้นบรรทัดใหม่ตามปกติ
            self.root.after(1, self.resize_prompt)
            return None
        self.send_message()
        return "break"

    def on_escape(self, _e=None):
        if self.active:
            self.stop()

    def on_model_change(self, _e=None):
        self.data["settings"]["model"] = self.model_var.get()
        self.save()

    def on_close(self):
        if self.active:
            self.stop()
        self.save()
        self.root.destroy()

    def export_md(self):
        c = self._target_chat() if self.hist.focus_get() == self.hist else self.current()
        if not c or not c["messages"]:
            return
        path = filedialog.asksaveasfilename(
            parent=self.root, defaultextension=".md", filetypes=[("Markdown", "*.md")],
            initialfile="ollama-chat-" + datetime.date.today().isoformat() + ".md")
        if not path:
            return
        txt = "# " + c["title"] + "\n\n"
        for m in c["messages"]:
            who = "คุณ" if m["role"] == "user" else (m.get("model") or "AI")
            txt += "## " + who + "\n\n" + (m.get("content") or "") + "\n\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)
        self.set_status("ส่งออกแล้ว: " + path)

    # ------------------------------------------------------------ models
    def host(self):
        return self.data["settings"]["host"].rstrip("/")

    def refresh_models(self):
        self.refresh_btn.state(["disabled"])
        host = self.host()

        def work():
            try:
                with urllib.request.urlopen(host + "/api/tags", timeout=8) as r:
                    data = json.loads(r.read().decode("utf-8"))
                models = [x.get("name") for x in data.get("models", []) if x.get("name")]
                self.root.after(0, self._apply_models, models, None)
            except Exception as e:
                self.root.after(0, self._apply_models, None, e)
        threading.Thread(target=work, daemon=True).start()

    def _apply_models(self, models, err):
        self.refresh_btn.state(["!disabled"])
        s = self.data["settings"]
        if err is not None:
            self.set_connection(False, "เชื่อมต่อไม่ได้")
            self.model_box.configure(values=[s["model"]] if s.get("model") else [])
            self.model_var.set(s.get("model") or "ไม่พบ Ollama")
            self.set_status("เชื่อมต่อ Ollama ไม่ได้ ตรวจว่า Ollama เปิดอยู่: " + str(err))
            return
        if not models:
            self.model_box.configure(values=[])
            self.model_var.set("ยังไม่มีโมเดล")
            self.set_connection(False, "ยังไม่มีโมเดล")
            return
        self.model_box.configure(values=models)
        if s.get("model") not in models:
            s["model"] = models[0]
        self.model_var.set(s["model"])
        self.set_connection(True, f"เชื่อมต่อแล้ว · {len(models)} โมเดล")
        self.save()

    # ------------------------------------------------------------ chat
    def send_message(self, regenerate=False):
        if self.active:
            self.stop()
            return
        chat = self.current()
        if not chat:
            return
        msgs = chat["messages"]
        if regenerate:
            if msgs and msgs[-1]["role"] == "assistant":
                msgs.pop()
            if not msgs or msgs[-1]["role"] != "user":
                self.render_all()
                return
        else:
            text = self.prompt.get("1.0", "end-1c").strip()
            if not text:
                return
            msgs.append({"role": "user", "content": text})
            self.prompt.delete("1.0", "end")
            self.resize_prompt()
            self.update_placeholder()
            if chat["title"] == "แชตใหม่":
                chat["title"] = re.sub(r"\s+", " ", text)[:42]
        s = self.data["settings"]
        if not s.get("model"):
            self.set_status("ยังไม่มีโมเดล กรุณาติดตั้งโมเดลใน Ollama ก่อน")
            self.save()
            self.render_all()
            return
        chat["updated"] = now_ms()
        assistant = {"role": "assistant", "model": s["model"], "content": "",
                     "thinking": "", "streaming": True}
        previous = [{"role": m["role"], "content": m.get("content") or ""}
                    for m in msgs if m.get("role") in ("user", "assistant")]
        msgs.append(assistant)
        mx = int(s.get("maxHistory") or 0)
        history = previous[-mx:] if mx > 0 else previous
        sp = (s.get("systemPrompt") or "").strip()
        body = {"model": s["model"],
                "messages": ([{"role": "system", "content": sp}] if sp else []) + history,
                "stream": True,
                "options": {"temperature": float(s["temperature"]), "num_ctx": int(s["numCtx"])}}
        if s.get("think"):
            body["think"] = True

        self.gen += 1
        self.active = {"gen": self.gen, "stop": threading.Event(), "resp": None,
                       "msg": assistant, "in_think": False, "tagbuf": "", "seen": False}
        self.send_btn.configure(text="หยุด ■", style="Stop.TButton")
        self.save()
        self.render_all()
        self.set_status("AI กำลังสร้างคำตอบ... กด Esc หรือปุ่มหยุดเพื่อยกเลิก")
        threading.Thread(target=self._worker, args=(self.active, self.host(), body),
                         daemon=True).start()

    def _worker(self, act, host, body):
        gen, stop, q = act["gen"], act["stop"], self.q

        def post(b):
            req = urllib.request.Request(host + "/api/chat", data=json.dumps(b).encode("utf-8"),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            return urllib.request.urlopen(req, timeout=600)
        try:
            try:
                resp = post(body)
            except urllib.error.HTTPError as e:
                if body.get("think") and e.code in (400, 422):
                    body.pop("think", None)
                    resp = post(body)
                else:
                    raise
            act["resp"] = resp
            with resp:
                for raw in resp:
                    if stop.is_set():
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("error"):
                        raise RuntimeError(obj["error"])
                    msg = obj.get("message") or {}
                    q.put((gen, "delta", msg.get("thinking") or "", msg.get("content") or ""))
                    if obj.get("done"):
                        n, d = obj.get("eval_count") or 0, obj.get("eval_duration") or 0
                        q.put((gen, "stats", {"tokens": n, "tps": n / (d / 1e9) if d else 0}))
            q.put((gen, "end"))
        except urllib.error.HTTPError as e:
            reason = ""
            try:
                reason = json.loads(e.read().decode("utf-8")).get("error", "")
            except Exception:
                pass
            q.put((gen, "error", reason or f"HTTP {e.code}"))
        except Exception as e:
            if not stop.is_set():
                q.put((gen, "error", str(e)))

    def feed_content(self, chunk):
        """แยกข้อความในแท็ก <think>…</think> ออกจากคำตอบ แม้แท็กจะขาดกลาง chunk"""
        a, m = self.active, self.active["msg"]
        a["tagbuf"] += chunk
        while a["tagbuf"]:
            tag = "</think>" if a["in_think"] else "<think>"
            key = "thinking" if a["in_think"] else "content"
            buf = a["tagbuf"]
            at = buf.find(tag)
            if at >= 0:
                m[key] += buf[:at]
                a["tagbuf"] = buf[at + len(tag):]
                a["in_think"] = not a["in_think"]
                continue
            hold = 0
            for k in range(min(len(tag) - 1, len(buf)), 0, -1):
                if buf.endswith(tag[:k]):
                    hold = k
                    break
            m[key] += buf[:len(buf) - hold]
            a["tagbuf"] = buf[len(buf) - hold:]
            break

    def flush_tagbuf(self):
        a = self.active
        if a and a["tagbuf"]:
            a["msg"]["thinking" if a["in_think"] else "content"] += a["tagbuf"]
            a["tagbuf"] = ""

    def poll(self):
        changed = False
        try:
            while True:
                item = self.q.get_nowait()
                a = self.active
                if not a or a["gen"] != item[0]:
                    continue
                kind, m = item[1], a["msg"]
                if kind == "delta":
                    m["thinking"] = (m.get("thinking") or "") + item[2]
                    self.feed_content(item[3])
                    changed = True
                elif kind == "stats":
                    m["stats"] = item[2]
                    a["seen"] = True
                elif kind == "end":
                    self.flush_tagbuf()
                    if not a["seen"] and not (m["content"] or m["thinking"]):
                        m["content"] += "\n\n⚠ ข้อผิดพลาด: การเชื่อมต่อสิ้นสุดก่อนมีคำตอบ"
                        self.set_status("เกิดข้อผิดพลาด: การเชื่อมต่อสิ้นสุดก่อนมีคำตอบ")
                    else:
                        self.set_status("ตอบเสร็จแล้ว • Enter ส่ง • Shift+Enter ขึ้นบรรทัด • Esc หยุด")
                    self.finish()
                    changed = False
                elif kind == "error":
                    m["content"] += "\n\n⚠ ข้อผิดพลาด: " + item[2]
                    self.set_status("เกิดข้อผิดพลาด: " + item[2])
                    self.finish()
                    changed = False
        except queue.Empty:
            pass
        if changed and self.active:
            self.render_stream()
        self.root.after(40, self.poll)

    def stop(self):
        a = self.active
        if not a:
            return
        a["stop"].set()
        a["msg"]["interrupted"] = True
        resp = a.get("resp")
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        self.set_status("หยุดการตอบแล้ว")
        self.finish()

    def finish(self):
        if self.active:
            self.flush_tagbuf()
            self.active["msg"]["streaming"] = False
        self.active = None
        self.send_btn.configure(text="ส่ง ↑", style="Accent.TButton")
        self.save()
        self.render_all()
        self.prompt.focus_set()

    # ------------------------------------------------------------ settings
    def open_settings(self):
        s = self.data["settings"]
        win = tk.Toplevel(self.root, bg=C["panel"])
        win.title("ตั้งค่า Local AI")
        win.transient(self.root)
        win.resizable(False, False)
        body = tk.Frame(win, bg=C["panel"])
        body.pack(fill="both", expand=True, padx=22, pady=18)

        def label(text):
            tk.Label(body, text=text, fg="#d0d0d2", bg=C["panel"], font=self.f_small,
                     anchor="w").pack(fill="x", pady=(12, 4))

        tk.Label(body, text="⚙  ตั้งค่า Local AI", fg=C["text"], bg=C["panel"],
                 font=self.f_title, anchor="w").pack(fill="x")

        label("Ollama Host")
        host_var = tk.StringVar(value=s["host"])
        ttk.Entry(body, textvariable=host_var, width=52).pack(fill="x")

        label("System Prompt (กำหนดบทบาท AI)")
        sp = tk.Text(body, height=5, wrap="word", bg=C["field"], fg=C["text"],
                     insertbackground=C["text"], bd=0, highlightthickness=1,
                     highlightbackground=C["border"], highlightcolor=C["accent"],
                     font=self.f_base, padx=8, pady=6)
        sp.insert("1.0", s.get("systemPrompt") or "")
        sp.pack(fill="x")

        temp_var = tk.DoubleVar(value=float(s["temperature"]))
        temp_lbl = tk.Label(body, fg="#d0d0d2", bg=C["panel"], font=self.f_small, anchor="w")
        temp_lbl.pack(fill="x", pady=(12, 4))

        def upd_temp(*_):
            v = round(temp_var.get() / 0.05) * 0.05
            temp_var.set(round(v, 2))
            temp_lbl.configure(text=f"Temperature: {temp_var.get():.2f}")
        ttk.Scale(body, from_=0, to=1.5, variable=temp_var, command=upd_temp).pack(fill="x")
        upd_temp()

        row = tk.Frame(body, bg=C["panel"])
        row.pack(fill="x")
        c1, c2 = tk.Frame(row, bg=C["panel"]), tk.Frame(row, bg=C["panel"])
        c1.pack(side="left", fill="x", expand=True, padx=(0, 6))
        c2.pack(side="left", fill="x", expand=True, padx=(6, 0))
        tk.Label(c1, text="Context Window (num_ctx)", fg="#d0d0d2", bg=C["panel"],
                 font=self.f_small, anchor="w").pack(fill="x", pady=(12, 4))
        ctx_var = tk.StringVar(value=str(s["numCtx"]))
        ttk.Spinbox(c1, from_=512, to=131072, increment=1024, textvariable=ctx_var).pack(fill="x")
        tk.Label(c2, text="ข้อความย้อนหลัง (0 = ทั้งหมด)", fg="#d0d0d2", bg=C["panel"],
                 font=self.f_small, anchor="w").pack(fill="x", pady=(12, 4))
        hist_var = tk.StringVar(value=str(s["maxHistory"]))
        ttk.Spinbox(c2, from_=0, to=1000, increment=2, textvariable=hist_var).pack(fill="x")

        think_var = tk.BooleanVar(value=bool(s.get("think")))
        ttk.Checkbutton(body, text=" เปิดโหมด Thinking (เฉพาะโมเดลที่รองรับ)",
                        variable=think_var).pack(fill="x", pady=(14, 0))

        tk.Label(body, text=f"ข้อมูลแชตบันทึกที่ {DATA_FILE}\nใช้กับ Ollama ที่คุณเปิดใช้งานเองในเครื่อง",
                 fg="#c6c9cb", bg=C["panel"], font=self.f_small, justify="left",
                 anchor="w").pack(fill="x", pady=(12, 0))

        def do_save():
            host = host_var.get().strip().rstrip("/")
            if not HOST_RE.match(host):
                messagebox.showwarning("Host ไม่ถูกต้อง", "เพื่อความปลอดภัย โปรดใช้ "
                                       "http://localhost:11434 หรือ http://127.0.0.1:11434",
                                       parent=win)
                return

            def to_int(v, default):
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    return default
            s.update(host=host, systemPrompt=sp.get("1.0", "end-1c"),
                     temperature=float(temp_var.get()),
                     numCtx=max(512, to_int(ctx_var.get(), 8192)),
                     maxHistory=max(0, to_int(hist_var.get(), 0)),
                     think=bool(think_var.get()))
            self.save()
            win.destroy()
            self.refresh_models()

        foot = tk.Frame(body, bg=C["panel"])
        foot.pack(fill="x", pady=(20, 0))
        ttk.Button(foot, text="บันทึก", style="Accent.TButton", command=do_save).pack(side="right")
        ttk.Button(foot, text="ยกเลิก", command=win.destroy).pack(side="right", padx=8)
        win.bind("<Escape>", lambda e: (win.destroy(), "break")[1])

        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        win.focus_set()


def main():
    if sys.platform == "win32":
        try:  # ให้ตัวอักษรคมชัดบนจอ High-DPI
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
