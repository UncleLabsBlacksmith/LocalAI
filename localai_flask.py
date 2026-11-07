#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Local AI Chat • Ollama — Flask single-file edition
แปลงจาก LocalAI.py (tkinter) เป็นเว็บแอป Flask ไฟล์เดียว เปิดใช้ผ่านเบราว์เซอร์

ความสามารถ (เหมือนเวอร์ชัน tkinter):
  • แชตแบบ streaming กับ Ollama ในเครื่อง (/api/chat)
  • เลือก/โหลดรายชื่อโมเดล (/api/tags)
  • ประวัติแชต: ค้นหา เปลี่ยนชื่อ ลบ (ใช้ไฟล์ JSON เดิม ~/.local_ai_ollama_chat.json
    จึงเห็นประวัติร่วมกับเวอร์ชัน tkinter ได้)
  • แสดงผล Markdown (หัวข้อ, ลิสต์, โค้ดบล็อก, ตัวหนา/เอียง, ลิงก์, quote)
  • โหมด Thinking (field "thinking" และแท็ก <think>…</think>) แบบพับ/ขยายได้
  • ตอบใหม่, ส่งออก .md, คัดลอกคำตอบ/โค้ด, แสดง tokens และ tok/s
  • คีย์ลัด: Enter ส่ง • Shift+Enter ขึ้นบรรทัด • Esc หยุด • Alt+N แชตใหม่
  • แนบไฟล์ (ปุ่ม 📎 / ลากวาง / Ctrl+V วางรูป):
      - รูปภาพ → ส่งให้โมเดล vision (เช่น qwen2.5vl, llava, gemma3, llama3.2-vision)
      - เอกสาร → แปลงเป็นข้อความให้ AI วิเคราะห์: xlsx/xlsm, xls, csv/tsv, docx, pptx, pdf,
        txt/md/json/โค้ด/G-code

ติดตั้ง:  pip install flask
          pip install openpyxl python-docx python-pptx pypdf   (สำหรับอ่านเอกสาร; xls ใช้ xlrd)
รัน:      python localai_flask.py
          เครื่องนี้เปิด http://127.0.0.1:5000  •  เครื่องอื่นในวง LAN เปิด http://<IP เครื่องนี้>:5000
          (โปรแกรมจะพิมพ์ URL ให้ตอนเริ่ม; Ollama ยังรันอยู่บนเครื่องที่รัน Flask เครื่องเดียว)
"""

import csv
import io
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, Response, jsonify, request, stream_with_context

APP_TITLE = "Local AI Chat • Ollama"
DATA_FILE = Path(os.environ.get("LOCALAI_DATA") or Path.home() / ".local_ai_ollama_chat.json")
# 0.0.0.0 = รับการเชื่อมต่อจากทุกเครื่องในวง LAN  (ตั้ง LOCALAI_HOST=127.0.0.1 เพื่อเปิดเฉพาะเครื่องนี้)
LISTEN_HOST = os.environ.get("LOCALAI_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LOCALAI_PORT", "5050"))

DEFAULTS = {
    "host": "http://localhost:11434", "model": "", "systemPrompt": "",
    "temperature": 0.7, "numCtx": 8192, "maxHistory": 20, "think": False,
}
# อนุญาตเฉพาะ Ollama ในเครื่อง (กัน server ถูกใช้ยิงไปที่อื่น)
HOST_RE = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", re.I)

MAX_UPLOAD_MB = 30          # ขนาดไฟล์แนบสูงสุดต่อไฟล์
MAX_DOC_CHARS = 120_000     # ตัดข้อความที่ดึงจากเอกสารไม่ให้เกินนี้ (กัน context ล้น)
MAX_SHEET_ROWS = 3000       # จำนวนแถวสูงสุดต่อชีต

TEXT_EXT = {".txt", ".md", ".json", ".xml", ".html", ".htm", ".log", ".ini", ".cfg", ".yaml",
            ".yml", ".toml", ".py", ".js", ".ts", ".c", ".cpp", ".h", ".java", ".cs", ".sql",
            ".nc", ".gcode", ".tap", ".css", ".sh", ".bat"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


# ------------------------------------------------------------ document → text
def _need(module, pip_name):
    try:
        return __import__(module)
    except ImportError:
        raise RuntimeError(f"ต้องติดตั้งไลบรารีเพิ่มก่อน:  pip install {pip_name}")


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.10g}"
    if hasattr(v, "isoformat"):
        if hasattr(v, "hour") and hasattr(v, "date") and (v.hour, v.minute, v.second) == (0, 0, 0):
            return v.date().isoformat()      # วันที่ล้วนใน Excel ไม่ต้องมี 00:00:00
        return v.isoformat(sep=" ") if hasattr(v, "date") else v.isoformat()
    return str(v)


def _rows_to_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    n = 0
    for row in rows:
        cells = [_cell(v) for v in row]
        while cells and cells[-1] == "":
            cells.pop()
        if not cells:
            continue
        w.writerow(cells)
        n += 1
        if n >= MAX_SHEET_ROWS:
            buf.write(f"... (ตัดเหลือ {MAX_SHEET_ROWS} แถวแรก)\n")
            break
        if buf.tell() > MAX_DOC_CHARS:
            break
    return buf.getvalue()


def extract_xlsx(data):
    openpyxl = _need("openpyxl", "openpyxl")
    # data_only=True → ได้ค่าที่คำนวณแล้วของสูตร (ค่าที่ Excel บันทึกไว้ล่าสุด)
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        dim = ws.calculate_dimension() if hasattr(ws, "calculate_dimension") else ""
        parts.append(f"### ชีต: {ws.title}" + (f"  (ช่วง {dim})" if dim else "")
                     + "\n" + _rows_to_csv(ws.iter_rows(values_only=True)))
    wb.close()
    return "\n".join(parts)


def extract_xls(data):
    xlrd = _need("xlrd", "xlrd")
    book = xlrd.open_workbook(file_contents=data)
    return "\n".join(f"### ชีต: {sh.name}\n" + _rows_to_csv(sh.row_values(r) for r in range(sh.nrows))
                     for sh in book.sheets())


def extract_docx(data):
    docx = _need("docx", "python-docx")
    d = docx.Document(io.BytesIO(data))
    out = [p.text for p in d.paragraphs if p.text.strip()]
    for i, t in enumerate(d.tables, 1):
        out.append(f"\n### ตารางที่ {i}")
        out.append(_rows_to_csv([c.text for c in r.cells] for r in t.rows))
    return "\n".join(out)


def extract_pptx(data):
    pptx = _need("pptx", "python-pptx")
    prs = pptx.Presentation(io.BytesIO(data))
    out = []
    for i, slide in enumerate(prs.slides, 1):
        texts = [sh.text_frame.text for sh in slide.shapes
                 if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip()]
        out.append(f"### สไลด์ {i}\n" + "\n".join(texts))
    return "\n".join(out)


def extract_pdf(data):
    pypdf = _need("pypdf", "pypdf")
    reader = pypdf.PdfReader(io.BytesIO(data))
    out = []
    for i, page in enumerate(reader.pages, 1):
        out.append(f"### หน้า {i}\n" + (page.extract_text() or ""))
        if sum(map(len, out)) > MAX_DOC_CHARS:
            break
    text = "\n".join(out)
    if len(text.strip()) < 20 * max(1, len(reader.pages)) // 4:
        text += "\n(หมายเหตุ: PDF นี้แทบไม่มีข้อความ อาจเป็นไฟล์สแกน ลองแปลงหน้าเป็นรูปแล้วแนบแทน)"
    return text


def decode_text(data):
    for enc in ("utf-8-sig", "cp874", "latin-1"):   # cp874 = ภาษาไทยของ Windows
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def extract_text(filename, data):
    ext = os.path.splitext(filename.lower())[1]
    if ext in (".xlsx", ".xlsm"):
        return extract_xlsx(data)
    if ext == ".xls":
        return extract_xls(data)
    if ext == ".docx":
        return extract_docx(data)
    if ext == ".pptx":
        return extract_pptx(data)
    if ext == ".pdf":
        return extract_pdf(data)
    if ext in (".csv", ".tsv"):
        text = decode_text(data)
        delim = "\t" if ext == ".tsv" else ","
        return _rows_to_csv(csv.reader(io.StringIO(text), delimiter=delim))
    if ext in TEXT_EXT or b"\x00" not in data[:4096]:
        return decode_text(data)
    raise RuntimeError(f"ยังไม่รองรับไฟล์ชนิด {ext or 'นี้'}")


# ------------------------------------------------------------ data
def load_data():
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


def save_data(data):
    tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


def ollama_host():
    host = (load_data()["settings"].get("host") or DEFAULTS["host"]).rstrip("/")
    return host if HOST_RE.match(host) else DEFAULTS["host"]


def http_error_reason(e):
    try:
        return json.loads(e.read().decode("utf-8")).get("error", "") or f"HTTP {e.code}"
    except Exception:
        return f"HTTP {e.code}"


# ------------------------------------------------------------ routes
@app.get("/")
def index():
    return Response(PAGE.replace("__TITLE__", APP_TITLE)
                        .replace("__DATA_FILE__", str(DATA_FILE).replace("\\", "/")),
                    mimetype="text/html")


@app.get("/api/data")
def get_data():
    return jsonify(load_data())


@app.put("/api/data")
def put_data():
    d = request.get_json(silent=True)
    if not isinstance(d, dict) or not isinstance(d.get("chats"), list):
        return jsonify(error="รูปแบบข้อมูลไม่ถูกต้อง"), 400
    settings = dict(DEFAULTS)
    settings.update(d.get("settings") or {})
    settings["host"] = str(settings.get("host") or "").strip().rstrip("/")
    if not HOST_RE.match(settings["host"]):
        return jsonify(error="Host ต้องเป็น http://localhost:11434 หรือ http://127.0.0.1:11434"), 400
    for c in d["chats"]:
        for m in c.get("messages", []):
            m.pop("streaming", None)
    try:
        save_data({"settings": settings, "chats": d["chats"], "currentId": d.get("currentId")})
    except Exception as e:
        return jsonify(error="บันทึกไม่ได้: " + str(e)), 500
    return jsonify(ok=True)


@app.get("/api/models")
def models():
    try:
        with urllib.request.urlopen(ollama_host() + "/api/tags", timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        return jsonify(models=[x.get("name") for x in data.get("models", []) if x.get("name")])
    except Exception as e:
        return jsonify(error=str(e)), 502


@app.post("/api/extract")
def extract():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="ไม่พบไฟล์"), 400
    try:
        text = extract_text(f.filename, f.read()).strip()
    except Exception as e:
        return jsonify(error=f"อ่านไฟล์ {f.filename} ไม่ได้: {e}"), 422
    if not text:
        return jsonify(error=f"ไม่พบข้อความในไฟล์ {f.filename}"), 422
    truncated = len(text) > MAX_DOC_CHARS
    if truncated:
        text = text[:MAX_DOC_CHARS] + f"\n... (ตัดเหลือ {MAX_DOC_CHARS:,} ตัวอักษรแรก)"
    return jsonify(name=f.filename, text=text, chars=len(text), truncated=truncated)


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error=f"ไฟล์ใหญ่เกิน {MAX_UPLOAD_MB} MB"), 413


@app.post("/api/chat")
def chat():
    body = request.get_json(silent=True) or {}
    if not body.get("model") or not isinstance(body.get("messages"), list):
        return jsonify(error="ต้องระบุ model และ messages"), 400
    body["stream"] = True
    host = ollama_host()

    def post(b):
        req = urllib.request.Request(host + "/api/chat", data=json.dumps(b).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(req, timeout=600)

    def err(msg):
        return json.dumps({"error": msg}, ensure_ascii=False) + "\n"

    def generate():
        try:
            try:
                resp = post(body)
            except urllib.error.HTTPError as e:
                # โมเดลที่ไม่รองรับ think → ลองใหม่โดยไม่ส่ง think
                if body.get("think") and e.code in (400, 422):
                    body.pop("think", None)
                    resp = post(body)
                else:
                    raise
        except urllib.error.HTTPError as e:
            yield err(http_error_reason(e))
            return
        except Exception as e:
            yield err(str(e))
            return
        # ถ้าเบราว์เซอร์กดหยุด (ตัดการเชื่อมต่อ) generator จะถูกปิด → with ปิด resp ให้
        try:
            with resp:
                for raw in resp:
                    line = raw.strip()
                    if line:
                        yield line.decode("utf-8", "replace") + "\n"
        except Exception as e:
            yield err(str(e))

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------ page (HTML + CSS + JS)
PAGE = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --bg:#202123;--side:#151617;--panel:#2c2d30;--panel-hover:#3a3c3e;--line:#414348;
  --border:#4b4c50;--text:#f0f0f0;--muted:#a7a7aa;--accent:#21c482;--accent-hover:#35d793;
  --accent-text:#092b1d;--red:#eb747c;--stop-bg:#4a2d31;--user-bubble:#30353a;
  --code-bg:#111315;--inline-code:#38393c;--field:#28292b;--link:#8ad3ff;--user-name:#a2dafa;
  --active-row:#303134;--composer:#2e3032;--thought-bg:#26272a;--thought-fg:#b2b5b7;
  --quote:#c9c9ce;--select:#2f5545;
  --ui:"Leelawadee UI","Noto Sans Thai UI","Noto Sans Thai","Sarabun","Thonburi","Segoe UI",Tahoma,sans-serif;
  --mono:"Cascadia Mono",Consolas,Menlo,"JetBrains Mono","DejaVu Sans Mono","Courier New",monospace;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{display:flex;background:var(--bg);color:var(--text);font:15px/1.6 var(--ui)}
::selection{background:var(--select)}
button{font:inherit;color:inherit;cursor:pointer}
input,textarea,select{font:inherit;color:var(--text)}
::-webkit-scrollbar{width:10px}::-webkit-scrollbar-thumb{background:var(--panel);border-radius:5px}
::-webkit-scrollbar-thumb:hover{background:var(--line)}

/* sidebar */
#sidebar{width:272px;flex:none;background:var(--side);border-right:1px solid #2d2d2d;
  display:flex;flex-direction:column;padding:16px 12px}
body.no-side #sidebar{display:none}
.brand{font-weight:700;font-size:16px;padding:0 6px 12px;letter-spacing:.5px}
.brand b{color:var(--accent)}
.btn{background:var(--panel);border:1px solid #424348;border-radius:8px;padding:8px 12px}
.btn:hover{background:var(--panel-hover)}
.btn:disabled{color:var(--muted);cursor:default;background:var(--panel)}
.btn.full{width:100%}
.btn.small{padding:5px 10px;font-size:13px}
.btn.accent{background:var(--accent);color:var(--accent-text);border-color:var(--accent);font-weight:700}
.btn.accent:hover{background:var(--accent-hover)}
.btn.stop{background:var(--stop-bg);color:var(--red);border-color:var(--red);font-weight:700}
.btn.ghost{background:transparent;border-color:transparent;color:var(--muted);font-size:13px;padding:4px 6px}
.btn.ghost:hover{background:#2a2b2e;color:var(--text)}
.section{color:var(--muted);font-size:12px;padding:14px 4px 4px}
.field{width:100%;background:var(--field);border:1px solid var(--border);border-radius:8px;padding:7px 9px;outline:none}
.field:focus{border-color:var(--accent)}
#history{flex:1;overflow-y:auto;margin:12px 0 0;padding:0;list-style:none}
#history li{display:flex;align-items:center;gap:4px;padding:7px 8px;border-radius:7px;cursor:pointer;
  white-space:nowrap}
#history li:hover{background:#1f2022}
#history li.active{background:var(--active-row)}
#history li .t{flex:1;overflow:hidden;text-overflow:ellipsis}
#history li .ops{display:none}
#history li:hover .ops,#history li.active .ops{display:flex}
#history li .ops button{background:none;border:0;color:var(--muted);padding:0 4px;font-size:13px}
#history li .ops button:hover{color:var(--text)}
.footer{border-top:1px solid #303033;margin-top:8px;padding-top:10px;display:flex;align-items:center;gap:6px}
.dot{width:9px;height:9px;border-radius:50%;background:#777;flex:none;margin-left:4px}
.dot.ok{background:var(--accent)}.dot.bad{background:var(--red)}
#conn{flex:1;color:var(--muted);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* main */
#main{flex:1;display:flex;flex-direction:column;min-width:0}
.top{height:60px;flex:none;display:flex;align-items:center;gap:8px;padding:0 20px 0 12px;border-bottom:1px solid #36373b}
.top .menu{background:none;border:0;font-size:18px;padding:4px 10px;border-radius:6px}
.top .menu:hover{background:var(--panel)}
#title{flex:1;font-weight:700;font-size:17px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#messages{flex:1;overflow-y:auto;padding:20px 24px}
.inner{max-width:880px;margin:0 auto}
.empty{text-align:center;padding-top:14vh}
.empty h1{font-size:30px;margin:0 0 6px}.empty .sub{color:var(--muted)}
.msg{margin-bottom:22px}
.who{font-weight:700;font-size:13.5px;margin:4px 0 6px}
.who.user{color:var(--user-name)}.who.ai{color:var(--accent)}
.bubble{background:var(--user-bubble);padding:8px 12px;border-radius:10px;white-space:pre-wrap;word-wrap:break-word;display:inline-block;max-width:100%}
.content p{margin:0 0 6px;white-space:pre-wrap;word-wrap:break-word}
.content h2{font-size:21px;margin:12px 0 6px}.content h3{font-size:17px;margin:10px 0 4px}
.content ul,.content ol{margin:0 0 6px;padding-left:28px}
.content li{margin-bottom:3px}.content li.sub{margin-left:22px;list-style:circle}
.content blockquote{margin:0 0 6px;padding-left:12px;border-left:3px solid #61646a;color:var(--quote)}
.content hr{border:0;border-top:1px solid var(--line);margin:10px 0}
.content a{color:var(--link)}
.content code{font-family:var(--mono);font-size:13.5px;background:var(--inline-code);padding:1px 5px;border-radius:4px}
.code{background:var(--code-bg);border-radius:8px;margin:6px 0 10px;overflow:hidden}
.code-head{display:flex;justify-content:space-between;align-items:center;padding:5px 12px;color:var(--muted);font-size:12.5px;border-bottom:1px solid #222}
.code-head button{background:none;border:0;color:var(--muted);font-size:12.5px}
.code-head button:hover{color:var(--text)}
.code pre{margin:0;padding:12px 14px;overflow-x:auto}
.code pre code{background:none;padding:0;color:#e6e6e6;font-size:13.5px;white-space:pre}
details.thought{margin:0 0 8px}
details.thought summary{color:var(--muted);font-size:13px;cursor:pointer;user-select:none}
details.thought .tbody{background:var(--thought-bg);color:var(--thought-fg);font-size:13px;padding:8px 12px;border-radius:8px;margin-top:6px;white-space:pre-wrap}
.meta{color:var(--muted);font-size:12.5px;margin-top:6px}
.actions{margin-top:4px}
.actions button{background:none;border:0;color:var(--muted);font-size:12.5px;padding:0}
.actions button:hover{color:var(--text)}
.cursor{color:var(--muted);font-size:13px}

/* composer */
.composer-wrap{flex:none;border-top:1px solid #343538;padding:10px 18px 12px}
.composer{max-width:880px;margin:0 auto;display:flex;align-items:flex-end;gap:8px;background:var(--composer);
  border:1px solid #505257;border-radius:14px;padding:4px 9px 4px 4px}
.composer:focus-within{border-color:var(--accent)}
#prompt{flex:1;resize:none;border:0;outline:none;background:transparent;padding:10px;max-height:220px;min-height:48px;line-height:1.5}
#prompt::placeholder{color:#7d7f84}
#send{margin-bottom:5px;min-width:84px}
#status{text-align:center;color:var(--muted);font-size:12.5px;margin-top:7px}
#status.warn{color:#f1c27d}
.composer{flex-wrap:wrap}
#attach{background:none;border:0;font-size:20px;color:var(--muted);padding:6px 8px;margin-bottom:6px;border-radius:8px}
#attach:hover{color:var(--text);background:var(--panel-hover)}
#chips{flex-basis:100%;display:flex;flex-wrap:wrap;gap:8px;padding:8px 6px 0}
#chips:empty{display:none}
.chip{display:flex;align-items:center;gap:8px;background:var(--field);border:1px solid var(--border);border-radius:10px;
  padding:5px 8px;font-size:13px;max-width:260px}
.chip img{width:42px;height:42px;object-fit:cover;border-radius:6px}
.chip .ico{font-size:22px}
.chip .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip .sz{color:var(--muted);font-size:11.5px}
.chip.err{border-color:var(--red)}.chip.err .sz{color:var(--red)}
.chip .rm{background:none;border:0;color:var(--muted);font-size:15px;padding:0 2px}
.chip .rm:hover{color:var(--red)}
.composer.drag{border-color:var(--accent);border-style:dashed}
.att{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:6px}
.att img{max-width:220px;max-height:180px;border-radius:8px;cursor:zoom-in;border:1px solid var(--line)}
.att details{background:var(--field);border:1px solid var(--border);border-radius:10px;padding:6px 10px;font-size:13px;max-width:100%}
.att summary{cursor:pointer}
.att pre{max-height:300px;overflow:auto;font-family:var(--mono);font-size:12px;white-space:pre-wrap;margin:6px 0 0;color:var(--thought-fg)}
#viewer{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;z-index:20;cursor:zoom-out}
#viewer img{max-width:94vw;max-height:94vh}

/* settings */
dialog{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:12px;padding:18px 22px;width:min(520px,92vw)}
dialog::backdrop{background:rgba(0,0,0,.55)}
dialog h2{margin:0 0 4px;font-size:18px}
dialog label{display:block;color:#d0d0d2;font-size:13px;margin:12px 0 4px}
dialog .row{display:flex;gap:12px}dialog .row>div{flex:1}
dialog input[type=range]{width:100%;accent-color:var(--accent)}
dialog .check{display:flex;align-items:center;gap:8px;margin-top:14px;color:var(--text);font-size:14px}
dialog .check input{accent-color:var(--accent);width:16px;height:16px}
dialog .note{color:#c6c9cb;font-size:12.5px;margin-top:12px;word-break:break-all}
dialog .foot{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}
#hostErr{color:var(--red);font-size:12.5px;display:none}
/* จอแคบ (มือถือ/แท็บเล็ตแนวตั้ง): แถบข้างเป็นลิ้นชักเลื่อนออก ซ่อนไว้ก่อน กด ☰ เพื่อเปิด */
#backdrop{display:none}
@media (max-width:860px){
  html,body{height:100dvh}
  #sidebar,body.no-side #sidebar{display:flex;position:fixed;top:0;left:0;bottom:0;z-index:30;width:min(300px,85vw);
    transform:translateX(-100%);transition:transform .22s ease;box-shadow:none}
  body.side-open #sidebar{transform:none;box-shadow:4px 0 24px rgba(0,0,0,.5)}
  body.side-open #backdrop{display:block;position:fixed;inset:0;z-index:25;background:rgba(0,0,0,.5)}
  .top{height:52px;padding:0 10px 0 6px;gap:6px}
  #title{font-size:15px}
  .top .btn.small{padding:5px 8px;font-size:12.5px}
  #messages{padding:14px 12px}
  .composer-wrap{padding:8px 10px 10px}
  #prompt{font-size:16px}               /* กัน iOS ซูมหน้าจอเมื่อแตะช่องพิมพ์ */
  #send{min-width:64px;padding:8px 10px}
  #status{font-size:11.5px}
  .att img{max-width:150px;max-height:130px}
  #history li .ops{display:flex}        /* จอสัมผัสไม่มี hover → แสดงปุ่มตลอด */
}
@media (max-width:420px){
  #regen,#export{font-size:0;padding:6px 9px}
  #regen::before{content:"↻";font-size:15px}
  #export::before{content:"↓";font-size:15px}
}
</style>
</head>
<body>
<aside id="sidebar">
  <div class="brand">● LOCAL <b>AI</b> CHAT</div>
  <button class="btn full" id="newChat">＋&nbsp; แชตใหม่</button>
  <div class="section">โมเดลใน Ollama</div>
  <select class="field" id="model"><option>กำลังโหลด...</option></select>
  <button class="btn full" id="refresh" style="margin-top:8px">↻&nbsp; โหลดรายชื่อโมเดลใหม่</button>
  <div class="section">ค้นหาประวัติ</div>
  <input class="field" id="search" placeholder="พิมพ์เพื่อค้นหา...">
  <ul id="history"></ul>
  <div class="footer">
    <span class="dot" id="dot"></span><span id="conn">กำลังตรวจสอบ...</span>
    <button class="btn ghost" id="openSettings">⚙ ตั้งค่า</button>
  </div>
</aside>

<div id="backdrop"></div>
<main id="main">
  <div class="top">
    <button class="menu" id="toggleSide" title="ซ่อน/แสดงแถบข้าง">☰</button>
    <div id="title">แชตใหม่</div>
    <button class="btn small" id="regen">↻ ตอบใหม่</button>
    <button class="btn small" id="export">↓ .md</button>
  </div>
  <div id="messages"><div class="inner" id="list"></div></div>
  <div class="composer-wrap">
    <div class="composer" id="composer">
      <div id="chips"></div>
      <button id="attach" title="แนบรูปภาพหรือเอกสาร (xlsx, csv, docx, pdf, pptx, txt...) — ลากวางหรือวางรูป (Ctrl+V) ได้">📎</button>
      <input type="file" id="fileInput" multiple hidden
        accept="image/*,.xlsx,.xlsm,.xls,.csv,.tsv,.docx,.pptx,.pdf,.txt,.md,.json,.xml,.html,.log,.yaml,.yml,.py,.js,.sql,.nc,.gcode,.tap">
      <textarea id="prompt" rows="2" placeholder="ถามอะไรก็ได้... (Enter เพื่อส่ง • แนบไฟล์ด้วย 📎)"></textarea>
      <button class="btn accent" id="send">ส่ง ↑</button>
    </div>
    <div id="status">Enter ส่ง • Shift+Enter ขึ้นบรรทัด • Esc หยุด • ทุกอย่างประมวลผลด้วย Ollama ในเครื่อง</div>
  </div>
</main>

<div id="viewer"><img alt=""></div>

<dialog id="settings">
  <h2>⚙&nbsp; ตั้งค่า Local AI</h2>
  <label>Ollama Host</label>
  <input class="field" id="sHost">
  <div id="hostErr">เพื่อความปลอดภัย โปรดใช้ http://localhost:11434 หรือ http://127.0.0.1:11434</div>
  <label>System Prompt (กำหนดบทบาท AI)</label>
  <textarea class="field" id="sPrompt" rows="5"></textarea>
  <label id="sTempLbl">Temperature</label>
  <input type="range" id="sTemp" min="0" max="1.5" step="0.05">
  <div class="row">
    <div><label>Context Window (num_ctx)</label><input class="field" id="sCtx" type="number" min="512" max="131072" step="1024"></div>
    <div><label>ข้อความย้อนหลัง (0 = ทั้งหมด)</label><input class="field" id="sHist" type="number" min="0" max="1000" step="2"></div>
  </div>
  <label class="check"><input type="checkbox" id="sThink"> เปิดโหมด Thinking (เฉพาะโมเดลที่รองรับ)</label>
  <div class="note">ข้อมูลแชตบันทึกที่ __DATA_FILE__<br>ใช้กับ Ollama ที่คุณเปิดใช้งานในเครื่องเท่านั้น</div>
  <div class="foot">
    <button class="btn" id="sCancel">ยกเลิก</button>
    <button class="btn accent" id="sSave">บันทึก</button>
  </div>
</dialog>

<script>
"use strict";
const $ = id => document.getElementById(id);
const HOST_RE = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/i;
let S = {settings:{}, chats:[], currentId:null};
let active = null;                 // สถานะการตอบที่กำลัง stream อยู่
const openThoughts = new Set();
let saveTimer = null, rafPending = false;

// ------------------------------------------------------------ utils
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
  : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {const r=Math.random()*16|0; return (c=="x"?r:(r&3|8)).toString(16);}));
function setStatus(t, warn){ $("status").textContent = t; $("status").className = warn ? "warn" : ""; }
const fmtSize = n => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(0) + " KB" : (n / 1048576).toFixed(1) + " MB";
function setConn(ok, t){ $("dot").className = "dot " + (ok ? "ok" : "bad"); $("conn").textContent = t; }
async function copyText(text, msg){
  try { await navigator.clipboard.writeText(text); }
  catch { const ta=document.createElement("textarea"); ta.value=text; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); }
  setStatus("✓ " + msg);
}

// ------------------------------------------------------------ data
const current = () => S.chats.find(c => c.id === S.currentId) || null;
function save(now){
  clearTimeout(saveTimer);
  const doSave = () => fetch("/api/data", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(S)})
    .then(r => r.ok ? null : r.json().then(j => setStatus("บันทึกไม่ได้: " + (j.error || r.status))))
    .catch(e => setStatus("บันทึกไม่ได้: " + e.message));
  if (now) return doSave();
  saveTimer = setTimeout(doSave, 250);
}
function ensureChat(){
  if (!current()) { if (S.chats.length) S.currentId = S.chats[0].id; else newChat(); }
}
function newChat(){
  if (active) return;
  const chat = {id:uuid(), title:"แชตใหม่", messages:[], updated:Date.now()};
  S.chats.unshift(chat); S.currentId = chat.id;
  save(); renderAll(); $("prompt").focus();
}
function selectChat(id){
  if (active || id === S.currentId) return;
  S.currentId = id; save(); renderAll();
}
function renameChat(id){
  if (active) return;
  const chat = S.chats.find(c => c.id === id); if (!chat) return;
  const name = prompt("ชื่อใหม่:", chat.title);
  if (name && name.trim()) { chat.title = name.trim().slice(0, 80); chat.updated = Date.now(); save(); renderAll(); }
}
function deleteChat(id){
  if (active) return;
  const chat = S.chats.find(c => c.id === id);
  if (!chat || !confirm(`ลบการสนทนา “${chat.title}” หรือไม่?`)) return;
  S.chats = S.chats.filter(c => c.id !== id);
  if (S.currentId === id) S.currentId = S.chats.length ? S.chats[0].id : null;
  ensureChat(); save(); renderAll();
}

// ------------------------------------------------------------ markdown
function inline(text){
  return esc(text).split(/(`[^`\n]+`)/).map(p => {
    if (/^`[^`\n]+`$/.test(p)) return "<code>" + p.slice(1, -1) + "</code>";
    return p.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
            .replace(/~~([^~\n]+)~~/g, "<s>$1</s>")
            .replace(/\*([^*\n]+)\*/g, "<i>$1</i>")
            .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }).join("");
}
function markdown(text){
  const out = []; let inCode = false, code = [], lang = "", list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const flush = () => out.push(`<div class="code"><div class="code-head"><span>${esc(lang || "code")}</span>`
    + `<button data-act="ccopy">⧉ คัดลอกโค้ด</button></div><pre><code>${esc(code.join("\n"))}</code></pre></div>`);
  for (const line of text.split("\n")) {
    const f = line.match(/^\s*```\s*([\w.+-]*)/);
    if (f) { if (inCode) { flush(); code = []; inCode = false; } else { closeList(); inCode = true; lang = f[1]; } continue; }
    if (inCode) { code.push(line); continue; }
    const t = line.trim();
    if (!t) { closeList(); continue; }
    let m;
    if ((m = t.match(/^(#{1,6})\s+(.*)/))) { closeList(); const h = m[1].length === 1 ? "h2" : "h3"; out.push(`<${h}>${inline(m[2])}</${h}>`); continue; }
    if (t.startsWith("> ") || t === ">") { closeList(); out.push(`<blockquote>${inline(t.slice(2))}</blockquote>`); continue; }
    if ((m = t.match(/^([-*]|\d+[.)])\s+(.+)/))) {
      const kind = /\d/.test(m[1]) ? "ol" : "ul";
      if (list !== kind) { closeList(); out.push(`<${kind}>`); list = kind; }
      const indent = line.length - line.trimStart().length;
      out.push(`<li${indent >= 2 ? ' class="sub"' : ""}>${inline(m[2])}</li>`); continue;
    }
    if (/^[-*_]{3,}$/.test(t)) { closeList(); out.push("<hr>"); continue; }
    closeList(); out.push(`<p>${inline(line)}</p>`);
  }
  closeList(); if (inCode) flush();
  return out.join("");
}

// ------------------------------------------------------------ rendering
function renderAll(){ renderHistory(); renderMessages(); }

function renderHistory(){
  const q = $("search").value.trim().toLowerCase();
  const ul = $("history"); ul.innerHTML = "";
  [...S.chats].sort((a, b) => (b.updated || 0) - (a.updated || 0)).forEach(chat => {
    if (q) {
      const blob = [chat.title, ...chat.messages.map(m => (m.content || "") + " "
        + (m.attachments || []).map(a => a.name).join(" "))].join(" ").toLowerCase();
      if (!blob.includes(q)) return;
    }
    const li = document.createElement("li");
    if (chat.id === S.currentId) li.className = "active";
    li.innerHTML = `<span class="t">▤&nbsp; ${esc(chat.title)}</span><span class="ops">`
      + `<button title="เปลี่ยนชื่อ" data-op="rename">✎</button><button title="ส่งออก .md" data-op="export">↓</button>`
      + `<button title="ลบแชต" data-op="delete">×</button></span>`;
    li.onclick = e => {
      const op = e.target.dataset.op;
      if (op === "rename") renameChat(chat.id);
      else if (op === "delete") deleteChat(chat.id);
      else if (op === "export") exportMd(chat);
      else selectChat(chat.id);
    };
    li.ondblclick = e => { if (!e.target.dataset.op) renameChat(chat.id); };
    ul.appendChild(li);
  });
}

function nearBottom(){ const el = $("messages"); return el.scrollHeight - el.scrollTop - el.clientHeight < 40; }

function renderMessages(){
  const near = nearBottom();
  const chat = current();
  $("title").textContent = chat ? chat.title : "แชตใหม่";
  document.title = (chat && chat.messages.length ? chat.title + " · " : "") + "Local AI Chat";
  const list = $("list"); list.innerHTML = "";
  const msgs = chat ? chat.messages : [];
  if (!msgs.length) {
    list.innerHTML = `<div class="empty"><h1>สวัสดีครับ 👋</h1><div>Local AI ของคุณ พร้อมตอบด้วย Ollama บนเครื่องนี้</div>`
      + `<div class="sub" style="margin-top:12px">เลือกโมเดลด้านซ้าย แล้วเริ่มพิมพ์ได้เลย</div></div>`;
  } else {
    msgs.forEach((m, i) => list.appendChild(renderMessage(chat, i, m)));
  }
  if (near || (msgs.length && msgs[msgs.length - 1].streaming)) $("messages").scrollTop = 1e9;
}

function renderStream(){
  const chat = current(), list = $("list");
  if (!chat || !chat.messages.length || !list.lastElementChild) return renderMessages();
  const near = nearBottom();
  const i = chat.messages.length - 1;
  list.replaceChild(renderMessage(chat, i, chat.messages[i]), list.lastElementChild);
  if (near) $("messages").scrollTop = 1e9;
}
function scheduleStream(){
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => { rafPending = false; if (active) renderStream(); });
}

function renderMessage(chat, i, m){
  const div = document.createElement("div");
  div.className = "msg";
  if (m.role === "user") {
    div.innerHTML = `<div class="who user">คุณ</div>` + renderAttachments(m)
      + (m.content ? `<div class="bubble">${esc(m.content)}</div>` : "");
    return div;
  }
  const name = m.model || S.settings.model || "Local AI";
  let html = `<div class="who ai">✦ ${esc(name)}</div>`;
  const key = chat.id + ":" + i;
  if (m.thinking) {
    const opened = openThoughts.has(key);
    const label = (m.streaming && !m.content) ? "กำลังคิด... (คลิกเพื่อดู)" : (opened ? "ซ่อน Thinking" : "แสดง Thinking");
    html += `<details class="thought" data-key="${esc(key)}"${opened ? " open" : ""}><summary>${label}</summary>`
          + `<div class="tbody">${esc(m.thinking.trim())}</div></details>`;
  }
  if (m.content) html += `<div class="content">${markdown(m.content)}</div>`;
  else if (m.streaming && !m.thinking) html += `<div class="cursor">▍ กำลังสร้างคำตอบ...</div>`;
  const parts = [];
  if (m.stats) parts.push(`${m.stats.tokens || 0} tokens · ${(m.stats.tps || 0).toFixed(1)} tok/s`);
  if (m.interrupted) parts.push("หยุดกลางคัน");
  if (parts.length) html += `<div class="meta">${parts.join(" · ")}</div>`;
  if (!m.streaming) html += `<div class="actions"><button data-act="copy">⧉ คัดลอก</button></div>`;
  div.innerHTML = html;
  const det = div.querySelector("details");
  if (det) det.addEventListener("toggle", () => {
    if (det.open) openThoughts.add(key); else openThoughts.delete(key);
    if (!(m.streaming && !m.content)) det.querySelector("summary").textContent = det.open ? "ซ่อน Thinking" : "แสดง Thinking";
  });
  const cp = div.querySelector('[data-act="copy"]');
  if (cp) cp.onclick = () => copyText(m.content || "", "คัดลอกคำตอบแล้ว");
  return div;
}

// คัดลอกโค้ดในโค้ดบล็อก
$("list").addEventListener("click", e => {
  if (e.target.dataset.act === "ccopy")
    copyText(e.target.closest(".code").querySelector("pre").textContent, "คัดลอกโค้ดแล้ว");
  if (e.target.dataset.act === "zoom") {
    $("viewer").querySelector("img").src = e.target.src; $("viewer").style.display = "flex";
  }
});
$("viewer").onclick = () => { $("viewer").style.display = "none"; };

// ------------------------------------------------------------ attachments
// รูปภาพ → ย่อขนาดในเบราว์เซอร์แล้วส่งเป็น base64 ใน field "images" ของ Ollama (ต้องใช้โมเดล vision)
// เอกสาร → ส่งให้ Flask แปลงเป็นข้อความ แล้วแนบเข้าไปในข้อความถึงโมเดล
const IMG_MAX = 1600;
let pending = [];   // ไฟล์ที่แนบไว้ในช่องพิมพ์ ยังไม่ได้ส่ง

function loadImage(file){
  return new Promise((ok, fail) => {
    const url = URL.createObjectURL(file), img = new Image();
    img.onload = () => {
      const k = Math.min(1, IMG_MAX / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      const g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height);
      g.drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      ok(c.toDataURL("image/jpeg", 0.9));
    };
    img.onerror = () => { URL.revokeObjectURL(url); fail(new Error("เปิดรูปไม่ได้")); };
    img.src = url;
  });
}

function addFiles(files){
  for (const file of files) {
    const it = {id:uuid(), name:file.name || "pasted.png", size:file.size,
                kind:(file.type || "").startsWith("image/") ? "image" : "file", status:"loading"};
    if (it.kind === "image") {
      it.promise = loadImage(file).then(d => { it.data = d; it.status = "ok"; });
    } else {
      const fd = new FormData(); fd.append("file", file);
      it.promise = fetch("/api/extract", {method:"POST", body:fd})
        .then(async r => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || "HTTP " + r.status); return j; })
        .then(j => { it.text = j.text; it.chars = j.chars; it.truncated = j.truncated; it.status = "ok"; });
    }
    it.promise = it.promise.catch(e => { it.status = "error"; it.err = e.message; setStatus(e.message, true); })
                           .finally(renderChips);
    pending.push(it);
  }
  renderChips();
}

function renderChips(){
  const box = $("chips"); box.innerHTML = "";
  for (const it of pending) {
    const d = document.createElement("div");
    d.className = "chip" + (it.status === "error" ? " err" : "");
    const info = it.status === "loading" ? "กำลังอ่าน..." : it.status === "error" ? it.err
      : it.kind === "image" ? "รูปภาพ · " + fmtSize(it.size)
      : `${(it.chars || 0).toLocaleString()} ตัวอักษร${it.truncated ? " (ถูกตัด)" : ""}`;
    d.innerHTML = (it.kind === "image" && it.data ? `<img src="${it.data}" alt="">` : `<span class="ico">${it.kind === "image" ? "🖼" : "📄"}</span>`)
      + `<div style="min-width:0"><div class="nm" title="${esc(it.name)}">${esc(it.name)}</div><div class="sz" title="${esc(info)}">${esc(info)}</div></div>`
      + `<button class="rm" title="เอาออก">×</button>`;
    d.querySelector(".rm").onclick = () => { pending = pending.filter(x => x !== it); renderChips(); };
    box.appendChild(d);
  }
}

// ข้อความที่ส่งถึงโมเดลจริง = ข้อความผู้ใช้ + เนื้อหาเอกสารที่แนบ
function modelContent(m){
  let s = m.content || "";
  for (const a of m.attachments || [])
    if (a.kind === "file") s += `\n\n<file name="${a.name}">\n${a.text}\n</file>`;
  return s;
}
const modelImages = m => (m.attachments || []).filter(a => a.kind === "image").map(a => a.data.split(",")[1]);

function renderAttachments(m){
  const atts = m.attachments || [];
  if (!atts.length) return "";
  return `<div class="att">` + atts.map((a, j) => a.kind === "image"
    ? `<img src="${a.data}" alt="${esc(a.name)}" title="${esc(a.name)}" data-act="zoom">`
    : `<details><summary>📄 ${esc(a.name)} · ${(a.chars || 0).toLocaleString()} ตัวอักษร${a.truncated ? " (ถูกตัด)" : ""}</summary><pre>${esc(a.text)}</pre></details>`
  ).join("") + `</div>`;
}

// ------------------------------------------------------------ models
async function refreshModels(){
  const btn = $("refresh"); btn.disabled = true;
  const sel = $("model"), s = S.settings;
  const setOptions = (vals, label) => {
    sel.innerHTML = "";
    (vals.length ? vals : [label]).forEach(v => { const o = document.createElement("option"); o.textContent = v; o.value = v; sel.appendChild(o); });
  };
  try {
    const r = await fetch("/api/models"); const j = await r.json();
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    if (!j.models.length) { setOptions([], "ยังไม่มีโมเดล"); setConn(false, "ยังไม่มีโมเดล"); return; }
    setOptions(j.models);
    if (!j.models.includes(s.model)) { s.model = j.models[0]; save(); }
    sel.value = s.model;
    setConn(true, `เชื่อมต่อแล้ว · ${j.models.length} โมเดล`);
  } catch (e) {
    setOptions(s.model ? [s.model] : [], "ไม่พบ Ollama");
    setConn(false, "เชื่อมต่อไม่ได้");
    setStatus("เชื่อมต่อ Ollama ไม่ได้ ตรวจว่า Ollama เปิดอยู่: " + e.message);
  } finally { btn.disabled = false; }
}

// ------------------------------------------------------------ chat
function setBusy(b){
  const btn = $("send");
  btn.textContent = b ? "หยุด ■" : "ส่ง ↑";
  btn.className = "btn " + (b ? "stop" : "accent");
}

// แยกข้อความในแท็ก <think>…</think> ออกจากคำตอบ แม้แท็กจะขาดกลางระหว่าง chunk
function feedContent(a, chunk){
  const m = a.msg; a.buf += chunk;
  while (a.buf) {
    const tag = a.inThink ? "</think>" : "<think>";
    const key = a.inThink ? "thinking" : "content";
    const at = a.buf.indexOf(tag);
    if (at >= 0) { m[key] += a.buf.slice(0, at); a.buf = a.buf.slice(at + tag.length); a.inThink = !a.inThink; continue; }
    let hold = 0;
    for (let k = Math.min(tag.length - 1, a.buf.length); k > 0; k--) if (a.buf.endsWith(tag.slice(0, k))) { hold = k; break; }
    m[key] += a.buf.slice(0, a.buf.length - hold);
    a.buf = a.buf.slice(a.buf.length - hold);
    break;
  }
}
function flushBuf(a){ if (a && a.buf) { a.msg[a.inThink ? "thinking" : "content"] += a.buf; a.buf = ""; } }

async function sendMessage(regenerate){
  if (active) { stop(); return; }
  const chat = current(); if (!chat) return;
  const msgs = chat.messages, box = $("prompt");
  if (regenerate) {
    if (msgs.length && msgs[msgs.length - 1].role === "assistant") msgs.pop();
    if (!msgs.length || msgs[msgs.length - 1].role !== "user") { renderAll(); return; }
  } else {
    let text = box.value.trim();
    if (!text && !pending.length) return;
    if (pending.some(p => p.status === "loading")) {
      if (sendMessage.waiting) return;           // กันกดส่งซ้ำระหว่างรออ่านไฟล์
      sendMessage.waiting = true;
      setStatus("กำลังอ่านไฟล์แนบ รอสักครู่...");
      try { await Promise.all(pending.map(p => p.promise)); } finally { sendMessage.waiting = false; }
      if (active) return;
      text = box.value.trim();
    }
    const atts = pending.filter(p => p.status === "ok").map(p => p.kind === "image"
      ? {kind:"image", name:p.name, data:p.data}
      : {kind:"file", name:p.name, text:p.text, chars:p.chars, truncated:p.truncated});
    if (!text && !atts.length) { setStatus("ไม่มีไฟล์แนบที่อ่านได้", true); return; }
    if (!text) text = "ช่วยวิเคราะห์ไฟล์ที่แนบมา";
    const um = {role:"user", content:text};
    if (atts.length) um.attachments = atts;
    msgs.push(um);
    box.value = ""; autosize(); pending = []; renderChips();
    if (chat.title === "แชตใหม่") chat.title = text.replace(/\s+/g, " ").slice(0, 42);
  }
  const s = S.settings;
  if (!s.model) { setStatus("ยังไม่มีโมเดล กรุณาติดตั้งโมเดลใน Ollama ก่อน"); save(); renderAll(); return; }
  chat.updated = Date.now();
  const previous = msgs.filter(m => m.role === "user" || m.role === "assistant").map(m => {
    const o = {role:m.role, content:modelContent(m)};
    const imgs = modelImages(m); if (imgs.length) o.images = imgs;
    return o;
  });
  const a = {role:"assistant", model:s.model, content:"", thinking:"", streaming:true};
  msgs.push(a);
  const mx = parseInt(s.maxHistory) || 0;
  const history = mx > 0 ? previous.slice(-mx) : previous;
  const sp = (s.systemPrompt || "").trim();
  const body = {model:s.model, messages:(sp ? [{role:"system", content:sp}] : []).concat(history), stream:true,
                options:{temperature:+s.temperature, num_ctx:parseInt(s.numCtx)}};
  if (s.think) body.think = true;

  const me = active = {ctl:new AbortController(), msg:a, inThink:false, buf:"", seen:false};
  setBusy(true); save(); renderAll();
  // ประมาณ token คร่าวๆ (ไทย/ตัวเลขในตาราง ~3 ตัวอักษรต่อ token) — ถ้าเกิน num_ctx Ollama จะตัดส่วนต้นทิ้งเงียบๆ
  const estTok = Math.round(body.messages.reduce((n, m) => n + m.content.length, 0) / 3)
               + body.messages.reduce((n, m) => n + (m.images ? m.images.length * 800 : 0), 0);
  if (estTok > (parseInt(s.numCtx) || 8192) * 0.85)
    setStatus(`⚠ ข้อความ+ไฟล์แนบประมาณ ${estTok.toLocaleString()} tokens เกิน Context Window (${s.numCtx}) — `
            + `AI อาจเห็นข้อมูลไม่ครบ ควรเพิ่ม num_ctx ในตั้งค่า`, true);
  else
    setStatus("AI กำลังสร้างคำตอบ... กด Esc หรือปุ่มหยุดเพื่อยกเลิก");
  try {
    const r = await fetch("/api/chat", {method:"POST", headers:{"Content-Type":"application/json"},
                                        body:JSON.stringify(body), signal:me.ctl.signal});
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "HTTP " + r.status);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let rest = "";
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      rest += dec.decode(value, {stream:true});
      let nl;
      while ((nl = rest.indexOf("\n")) >= 0) {
        const line = rest.slice(0, nl).trim(); rest = rest.slice(nl + 1);
        if (!line) continue;
        const o = JSON.parse(line);
        if (o.error) throw new Error(o.error);
        const m = o.message || {};
        if (m.thinking) a.thinking += m.thinking;
        if (m.content) feedContent(me, m.content);
        if (o.done) {
          const n = o.eval_count || 0, d = o.eval_duration || 0;
          a.stats = {tokens:n, tps: d ? n / (d / 1e9) : 0}; me.seen = true;
        }
      }
      scheduleStream();
    }
    flushBuf(me);
    if (!me.seen && !a.content && !a.thinking) {
      a.content += "\n\n⚠ ข้อผิดพลาด: การเชื่อมต่อสิ้นสุดก่อนมีคำตอบ";
      setStatus("เกิดข้อผิดพลาด: การเชื่อมต่อสิ้นสุดก่อนมีคำตอบ");
    } else {
      setStatus("ตอบเสร็จแล้ว • Enter ส่ง • Shift+Enter ขึ้นบรรทัด • Esc หยุด");
    }
  } catch (e) {
    if (e.name !== "AbortError" && active === me) {
      a.content += "\n\n⚠ ข้อผิดพลาด: " + e.message;
      setStatus("เกิดข้อผิดพลาด: " + e.message);
    }
  } finally {
    if (active === me) finish();
  }
}

function stop(){
  const a = active; if (!a) return;
  a.msg.interrupted = true;
  a.ctl.abort();           // ตัดการเชื่อมต่อ → Flask ปิดการเชื่อมต่อกับ Ollama ตาม
  setStatus("หยุดการตอบแล้ว");
  finish();
}
function finish(){
  if (active) { flushBuf(active); active.msg.streaming = false; }
  active = null;
  setBusy(false); save(true); renderAll(); $("prompt").focus();
}

function exportMd(chat){
  const c = chat || current();
  if (!c || !c.messages.length) return;
  let txt = "# " + c.title + "\n\n";
  for (const m of c.messages) {
    txt += "## " + (m.role === "user" ? "คุณ" : (m.model || "AI")) + "\n\n";
    for (const a of m.attachments || []) txt += `> แนบไฟล์: ${a.name}\n\n`;
    txt += (m.content || "") + "\n\n";
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([txt], {type:"text/markdown;charset=utf-8"}));
  a.download = "ollama-chat-" + new Date().toISOString().slice(0, 10) + ".md";
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  setStatus("ส่งออกแล้ว: " + a.download);
}

// ------------------------------------------------------------ settings
function openSettings(){
  const s = S.settings;
  $("sHost").value = s.host; $("sPrompt").value = s.systemPrompt || "";
  $("sTemp").value = s.temperature; $("sCtx").value = s.numCtx; $("sHist").value = s.maxHistory;
  $("sThink").checked = !!s.think; $("hostErr").style.display = "none";
  updTemp(); $("settings").showModal();
}
function updTemp(){ $("sTempLbl").textContent = "Temperature: " + (+$("sTemp").value).toFixed(2); }
function saveSettings(){
  const host = $("sHost").value.trim().replace(/\/+$/, "");
  if (!HOST_RE.test(host)) { $("hostErr").style.display = "block"; return; }
  const toInt = (v, d) => { const n = parseInt(parseFloat(v)); return isNaN(n) ? d : n; };
  Object.assign(S.settings, {
    host, systemPrompt:$("sPrompt").value, temperature:+$("sTemp").value,
    numCtx:Math.max(512, toInt($("sCtx").value, 8192)), maxHistory:Math.max(0, toInt($("sHist").value, 0)),
    think:$("sThink").checked,
  });
  $("settings").close();
  save(true).then(refreshModels);
}

// ------------------------------------------------------------ events
function autosize(){ const p = $("prompt"); p.style.height = "auto"; p.style.height = Math.min(p.scrollHeight, 220) + "px"; }

$("newChat").onclick = newChat;
$("refresh").onclick = refreshModels;
$("search").oninput = renderHistory;
$("model").onchange = e => { S.settings.model = e.target.value; save(); };
$("openSettings").onclick = openSettings;
// จอกว้าง: ☰ ซ่อน/แสดงแถบข้าง  •  จอแคบ: ☰ เปิดลิ้นชัก แตะพื้นหลังหรือเลือกแชตเพื่อปิด
const narrow = matchMedia("(max-width:860px)");
const closeDrawer = () => document.body.classList.remove("side-open");
$("toggleSide").onclick = () => document.body.classList.toggle(narrow.matches ? "side-open" : "no-side");
$("backdrop").onclick = closeDrawer;
narrow.addEventListener("change", closeDrawer);
$("history").addEventListener("click", e => { if (narrow.matches && !e.target.dataset.op) closeDrawer(); });
$("newChat").addEventListener("click", () => { if (narrow.matches) closeDrawer(); });
$("regen").onclick = () => sendMessage(true);
$("export").onclick = () => exportMd();
$("send").onclick = () => sendMessage(false);
$("sTemp").oninput = updTemp;
$("sSave").onclick = saveSettings;
$("sCancel").onclick = () => $("settings").close();
$("prompt").addEventListener("input", autosize);
$("attach").onclick = () => $("fileInput").click();
$("fileInput").onchange = e => { addFiles([...e.target.files]); e.target.value = ""; };
$("prompt").addEventListener("paste", e => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); addFiles(files); }
});
const comp = $("composer");
["dragenter", "dragover"].forEach(t => comp.addEventListener(t, e => { e.preventDefault(); comp.classList.add("drag"); }));
["dragleave", "drop"].forEach(t => comp.addEventListener(t, e => { e.preventDefault(); comp.classList.remove("drag"); }));
comp.addEventListener("drop", e => addFiles([...e.dataTransfer.files]));
document.addEventListener("dragover", e => e.preventDefault());
document.addEventListener("drop", e => e.preventDefault());
$("prompt").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(false); }
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && $("viewer").style.display === "flex") { $("viewer").style.display = "none"; return; }
  if (e.key === "Escape" && document.body.classList.contains("side-open")) { closeDrawer(); return; }
  if (e.key === "Escape" && active && !$("settings").open) stop();
  if (e.altKey && (e.key === "n" || e.key === "N")) { e.preventDefault(); newChat(); }
});
window.addEventListener("beforeunload", () => {
  if (active) { active.msg.interrupted = true; active.msg.streaming = false; }
  clearTimeout(saveTimer);
  fetch("/api/data", {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(S), keepalive:true});
});

// ------------------------------------------------------------ start
(async () => {
  try { S = await (await fetch("/api/data")).json(); }
  catch (e) { setStatus("โหลดข้อมูลไม่ได้: " + e.message); }
  ensureChat(); renderAll(); autosize(); refreshModels(); $("prompt").focus();
})();
</script>
</body>
</html>
"""


def lan_ips():
    """หา IP ของเครื่องนี้ในวง LAN (ไว้บอกว่าเครื่องอื่นต้องเปิด URL ไหน)"""
    ips = set()
    try:  # วิธีนี้ไม่ได้ส่งข้อมูลออกไปจริง แค่ให้ OS เลือก interface ที่ใช้ออกเน็ต
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def main():
    print(f"{APP_TITLE}\n  ข้อมูลแชต: {DATA_FILE}")
    print(f"  เครื่องนี้:      http://127.0.0.1:{LISTEN_PORT}")
    if LISTEN_HOST in ("0.0.0.0", "::"):
        for ip in lan_ips():
            print(f"  เครื่องอื่นในวง: http://{ip}:{LISTEN_PORT}")
        print("  ⚠ ไม่มีระบบล็อกอิน — ทุกเครื่องในวง LAN เห็นและแก้ประวัติแชตเดียวกันได้\n"
              "    ถ้าต้องการเปิดเฉพาะเครื่องนี้ ให้ตั้ง LOCALAI_HOST=127.0.0.1")
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
