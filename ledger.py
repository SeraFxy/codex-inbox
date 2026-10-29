#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codex-inbox：AI 任务台账

把 Claude Code 和 Codex 的会话汇总到一个本地看板：
- 读每条回复最后的状态行（📌 / 🎯），按「现在等谁」分组；
- 「等时间点」到点弹 Mac 通知；
- `ledger schedule` 让 cmux 里的 Claude 会话到点被唤醒（会话关了会自动重开）；
- 每天定时把汇总私信到飞书。

只用 Python 标准库，兼容 macOS 自带的 /usr/bin/python3（3.9）。
"""
import argparse
import base64
import binascii
import bisect
import datetime as dt
import glob
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

HOME = os.path.expanduser("~")
BASE = os.path.dirname(os.path.realpath(__file__))
DATA_DIR = os.path.join(BASE, "data")
LOG_DIR = os.path.join(BASE, "logs")
DB_PATH = os.path.join(DATA_DIR, "ledger.db")
TOKEN_PATH = os.path.join(DATA_DIR, "token")
CONFIG_PATH = os.path.join(BASE, "config.json")
BOARD_HTML = os.path.join(BASE, "board.html")
IMG_DIR = os.path.join(DATA_DIR, "images")
CMUX_STATE = os.path.join(HOME, "Library", "Application Support", "cmux", "session-com.cmuxterm.app.json")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_HOME = os.path.join(HOME, ".codex")
CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"
SUB_ENV = {
    "HOME": HOME,
    "LANG": "zh_CN.UTF-8",
    "PATH": ":".join([
        "/opt/homebrew/bin", "/usr/local/bin",
        os.path.join(HOME, ".npm-global", "bin"), os.path.join(HOME, ".local", "bin"),
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]),
}
LARK = shutil.which("lark-cli", path=SUB_ENV["PATH"]) or "lark-cli"   # 飞书汇总用，没装就不发

DEFAULTS = {
    "port": 47800,
    "scan_interval_min": 15,   # 后台扫描对话记录的间隔
    "window_days": 14,         # 只看最近多少天有动静的会话
    "stale_hours": 48,         # 等你超过多久算「可能被搁置」
    "interrupted_min": 30,     # 停在半路超过多久算「可能已中断」
    "digest_time": "09:00",    # 每日飞书汇总时间
    "feishu_open_id": "",
    "feishu_as": "bot",
    "wake_expire_hours": 3,    # 定时唤醒晚了多久就放弃（如 Mac 一直在睡）
    "utc_offset_hours": None,  # 看板按哪个时区算时间；空 = 本机时区。Mac 时区和工作时区不一样时填，如北京时间填 8
    "project_parents": [],     # 这些目录下的每个子目录算一个项目，如 ["~/code"]；其余按 ~/Desktop/<项目>、~/<项目> 推断
}

HEAD_BYTES = 512 * 1024
TAIL_BYTES = 2 * 1024 * 1024
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
WEEK = "一二三四五六日"

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, session_key TEXT, size INTEGER, mtime REAL, offset INTEGER);
CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, last_activity REAL, data TEXT);
CREATE TABLE IF NOT EXISTS overrides (key TEXT PRIMARY KEY, action TEXT, until_ts REAL, at REAL);
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_key TEXT, session_id TEXT, tool TEXT, cwd TEXT,
  workspace TEXT, surface TEXT, at REAL, expire REAL, prompt TEXT, note TEXT, status TEXT,
  created REAL, attempts INTEGER DEFAULT 0, last_error TEXT, done_at REAL);
CREATE TABLE IF NOT EXISTS notified (key TEXT PRIMARY KEY, at REAL);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS prompts (id TEXT PRIMARY KEY, session_key TEXT, tool TEXT, ts REAL, text TEXT,
  project TEXT, cwd TEXT, title TEXT);
CREATE INDEX IF NOT EXISTS prompts_ts ON prompts(ts);
CREATE INDEX IF NOT EXISTS prompts_session ON prompts(session_key, ts);
CREATE TABLE IF NOT EXISTS names (session_key TEXT PRIMARY KEY, title TEXT, at REAL);
CREATE TABLE IF NOT EXISTS tags (project TEXT PRIMARY KEY, label TEXT, color TEXT);
CREATE TABLE IF NOT EXISTS goals (session_key TEXT PRIMARY KEY, goal TEXT, criteria TEXT, status TEXT, at REAL);
CREATE TABLE IF NOT EXISTS links (session_key TEXT, url TEXT, label TEXT, ts REAL, PRIMARY KEY (session_key, url));
CREATE TABLE IF NOT EXISTS images (session_key TEXT, sha TEXT, ts REAL, file TEXT, bytes INTEGER, PRIMARY KEY (session_key, sha));
CREATE INDEX IF NOT EXISTS images_session ON images(session_key, ts);
"""


# ---------------------------------------------------------------- 基础工具

def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, "ledger.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 1000000:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(clock().strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def clock(ts=None):
    """按看板的工作时区取时间（WORK_TZ 为 None 时就是本机时区）。"""
    return dt.datetime.fromtimestamp(time.time() if ts is None else ts, WORK_TZ)


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def work_tz(cfg):
    h = cfg.get("utc_offset_hours")
    return dt.timezone(dt.timedelta(hours=float(h))) if isinstance(h, (int, float)) else None


def tz_text(cfg):
    h = cfg.get("utc_offset_hours")
    if not isinstance(h, (int, float)):
        return ""
    return "北京时间" if float(h) == 8 else "UTC%+g" % float(h)


_START_CFG = load_config()
WORK_TZ = work_tz(_START_CFG)   # 改配置后要重启服务
PROJECT_PARENTS = [os.path.expanduser(x).rstrip("/") for x in (_START_CFG.get("project_parents") or []) if isinstance(x, str)]


def open_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)   # 对话记录和图片只给自己的账号读
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    os.chmod(DB_PATH, 0o600)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def meta_get(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, str(v)))


def load_token():
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            tok = f.read().strip()
            if tok:
                return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(tok)
    os.chmod(TOKEN_PATH, 0o600)
    return tok


def run(args, timeout=15):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=SUB_ENV)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$")


def parse_iso(ts):
    m = _ISO_RE.match(ts or "") if isinstance(ts, str) else None
    if not m:
        return None
    base, frac, tz = m.groups()
    s = base + ("." + (frac + "000000")[:6] if frac else "") + ("+00:00" if tz in (None, "Z") else tz)
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def fmt_time(ts, now=None):
    d = clock(ts)
    n = clock(now)
    days = (d.date() - n.date()).days
    hm = d.strftime("%H:%M")
    if days == 0:
        return "今天 " + hm
    if days == -1:
        return "昨天 " + hm
    if days == 1:
        return "明天 " + hm
    return "%d月%d日 %s" % (d.month, d.day, hm)


def fmt_ago(sec):
    sec = max(0, sec)
    if sec < 3600:
        return "%d 分钟" % max(1, int(sec // 60))
    if sec < 86400:
        return "%d 小时" % int(sec // 3600)
    return "%d 天" % int(sec // 86400)


# ---------------------------------------------------------------- 状态行解析

STATUS_RE = re.compile(r"📌\s*(.+)")
GOAL_RE = re.compile(r"🎯\s*(.+)")
ASK_RE = re.compile(r"^\s*(?:[-*]\s+)?❓️?\s*(.+)")
SEP_RE = re.compile(r"\s*[｜|]\s*")
CLEAN_RE = re.compile(r"[`*]+")
TIME_FULL_RE = re.compile(r"(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2})[:：](\d{2})")
TIME_HM_RE = re.compile(r"(?<!\d)(\d{1,2})[:：](\d{2})(?!\d)")


def last_match(regex, text):
    found = None
    for line in (text or "").splitlines():
        m = regex.search(line)
        if m:
            found = m.group(1)
    return found


def split_parts(raw):
    parts = [CLEAN_RE.sub("", p).strip() for p in SEP_RE.split(raw.strip())]
    return [p for p in parts if p]


def parse_when(s, ref_ts=None):
    ref = clock(ref_ts)
    m = TIME_FULL_RE.search(s or "")
    if m:
        year = int(m.group(1)) if m.group(1) else ref.year
        try:
            return dt.datetime(year, int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)), tzinfo=WORK_TZ).timestamp()
        except ValueError:
            return None
    m = TIME_HM_RE.search(s or "")
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return None
        d = ref.replace(hour=h, minute=mi, second=0, microsecond=0)
        if any(w in s for w in ("明天", "明早", "明晚")):
            d += dt.timedelta(days=1)
        elif d.timestamp() < ref.timestamp() - 600:
            d += dt.timedelta(days=1)
        return d.timestamp()
    return None


def classify(label):
    l = label.replace(" ", "")
    m = re.search(r"[（(]([^）)]+)[）)]", label)
    who = m.group(1).strip() if m else ""
    if l.startswith(("等你", "等我")):
        return "me", who
    if l.startswith(("等同事", "等别人", "等回复", "等对方")):
        return "peer", who
    if l.startswith("等时间"):
        return "time", who
    if l.startswith(("已定时", "定时")):
        return "sched", who
    if l.upper().startswith("AI") or l.startswith(("进行中", "继续")):
        return "ai", who
    if "确认完成" in l or l.startswith(("已完成", "完成")):
        return "confirm", who
    return "me", who


def parse_status(text, ts):
    raw = last_match(STATUS_RE, text)
    if not raw:
        return None
    parts = split_parts(raw)
    if not parts:
        return None
    label, rest = parts[0], parts[1:]
    kind, who = classify(label)
    at = None
    if rest:
        at = parse_when(rest[-1], ts)
        if at is not None and len(rest) >= 2:
            rest = rest[:-1]
    if at is None and kind in ("time", "sched"):
        at = parse_when(label, ts)
    return {"kind": kind, "label": label, "who": who, "desc": "｜".join(rest), "at": at, "ts": ts}


def parse_goal(text):
    raw = last_match(GOAL_RE, text)
    if not raw:
        return None
    goal, criteria = "", ""
    for p in split_parts(raw):
        sep = "：" if "：" in p else ":"
        k, _, v = p.partition(sep)
        k = k.strip()
        if v and k.startswith("目标"):
            goal = v.strip()
        elif v and "完成标准" in k:
            criteria = v.strip()
        elif not goal:
            goal = p
    return {"goal": goal, "criteria": criteria}


def parse_decisions(text):
    """「❓ 事情｜选项｜选项」：每行一件等你拍板的事；代码块里的是举例，不算。"""
    items, fence = [], False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        m = None if fence else ASK_RE.match(line)
        parts = split_parts(m.group(1)) if m else None
        if parts:
            items.append({"q": parts[0][:200], "opts": [o[:40] for o in parts[1:7]]})
    return items[:12]


# ---------------------------------------------------------------- 产出链接与图片

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'`，。；、）」』|]+")
MD_LINK_RE = re.compile(r"\[([^\]\n]{1,80})\]\((https?://[^\s)]+)\)")
SKIP_LINK_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|0\.0\.0\.0)([:/]|$)")
IMG_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


CODEISH_RE = re.compile(r"""^\s*([$!>#]|curl\b|wget\b|open\b|"[\w.-]+"\s*:|[\w.-]+=)""")
LINK_KINDS = {"docx": "飞书文档", "docs": "飞书文档", "doc": "飞书文档", "wiki": "飞书知识库", "base": "飞书多维表格",
              "sheets": "飞书表格", "minutes": "飞书妙记", "drive": "飞书云盘", "mindnotes": "飞书思维笔记"}


def link_name(url):
    u = urlparse(url)
    host, path = u.netloc.lower(), u.path
    m = re.search(r"/([^/]+)/-/merge_requests/(\d+)", path)
    if m:
        return "MR !%s · %s" % (m.group(2), m.group(1))
    m = re.search(r"/([^/]+)/-/(pipelines|jobs)/(\d+)", path)
    if m:
        return "%s #%s · %s" % ("流水线" if m.group(2) == "pipelines" else "任务", m.group(3), m.group(1))
    if host.endswith("feishu.cn") or "larksuite" in host:
        return LINK_KINDS.get((path.strip("/").split("/") or [""])[0], "飞书")
    if host.endswith("claude.ai") and "artifact" in path:
        return "Claude 页面"
    if "/-/" in path or host.startswith(("git.", "gitlab.")):
        return "GitLab · " + "/".join(path.strip("/").split("/")[:2])
    if host == "github.com":
        return "GitHub · " + "/".join(path.strip("/").split("/")[:2])
    return host[4:] if host.startswith("www.") else host


def link_label(before):
    """链接前面那句话里最后一小句，去掉「请到」「见」这类引导词，当作链接说明。"""
    t = clean_line(re.split(r"[，。；;！？!?]|[,.](?=\s)", before)[-1])
    if re.search(r"[{}\"=$]|\B--\w", t):
        return ""
    t = re.sub(r"[\s:：（(【\[<|-]+$", "", t)
    t = re.sub(r"(请现在|请|现在|可以)?(到|在|见|打开|点开|访问|看|参考|地址|链接|网址|位置|入口)$", "", t)
    t = re.sub(r"[\s:：（(【\[<|-]+$", "", t).strip()
    return t[-30:] if len(t) >= 2 else ""


def extract_links(text):
    """AI 回复里出现的链接（飞书文档、MR、产物地址等），带上一小段说明文字；代码块和命令行里的不算。"""
    text = re.sub(r"```.*?(```|$)", "", text or "", flags=re.S)
    out, seen = [], set()
    for m in MD_LINK_RE.finditer(text or ""):
        url = m.group(2).rstrip(".,;:!?")
        if url not in seen:
            seen.add(url)
            out.append((url, clean_line(m.group(1))[:60]))
    for ln in text.splitlines():
        if CODEISH_RE.match(ln):
            continue
        for m in URL_RE.finditer(ln):
            url = m.group(0).rstrip(".,;:!?*_")
            if url in seen or len(url) < 12:
                continue
            seen.add(url)
            out.append((url, link_label(ln[:m.start()])))
    return [(u, l) for u, l in out if not SKIP_LINK_RE.match(u)]


def note_links(st, ts, text):
    if text and "http" in text:
        found = extract_links(text)
        if found:
            st.setdefault("new_links", []).extend([ts, u, l] for u, l in found)


def save_image(b64, mime):
    """按内容去重存一份原图，返回 (sha1, 文件名, 字节数)。"""
    try:
        raw = base64.b64decode(b64 or "")
    except (ValueError, binascii.Error):
        return None
    if len(raw) < 200:
        return None
    sha = hashlib.sha1(raw).hexdigest()
    name = "%s.%s" % (sha, IMG_EXT.get(mime, "png"))
    path = os.path.join(IMG_DIR, sha[:2], name)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(raw)
        os.replace(path + ".tmp", path)
    return sha, name, len(raw)


def note_images(st, ts, pairs):
    for b64, mime in pairs:
        got = save_image(b64, mime)
        if got:
            st.setdefault("new_images", []).append([ts, got[0], got[1], got[2]])


def data_url_pairs(blocks):
    out = []
    for b in blocks:
        url = b.get("image_url") if isinstance(b, dict) and b.get("type") == "input_image" else None
        if isinstance(url, str) and url.startswith("data:image/") and ";base64," in url:
            head, _, data = url.partition(";base64,")
            out.append((data, head[5:]))
    return out


def flush_media(conn, key, st):
    for ts, url, label in st.pop("new_links", []):
        conn.execute("INSERT INTO links (session_key, url, label, ts) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(session_key, url) DO UPDATE SET ts=MAX(links.ts, excluded.ts), "
                     "label=CASE WHEN excluded.label != '' THEN excluded.label ELSE links.label END",
                     (key, url[:1000], label, ts or 0))
    for ts, sha, name, size in st.pop("new_images", []):
        conn.execute("INSERT OR IGNORE INTO images (session_key, sha, ts, file, bytes) VALUES (?, ?, ?, ?, ?)",
                     (key, sha, ts or 0, name, size))


def save_goal(conn, key, goal, criteria="", status=""):
    if not goal:
        return
    conn.execute("INSERT INTO goals (session_key, goal, criteria, status, at) VALUES (?, ?, ?, ?, ?) "
                 "ON CONFLICT(session_key) DO UPDATE SET goal=excluded.goal, criteria=excluded.criteria, "
                 "status=excluded.status, at=excluded.at WHERE goals.goal IS NOT excluded.goal "
                 "OR goals.criteria IS NOT excluded.criteria OR goals.status IS NOT excluded.status",
                 (key, goal, criteria or "", status or "", time.time()))


# ---------------------------------------------------------------- 对话记录解析

def new_state(tool, sid, path):
    return {
        "tool": tool, "id": sid, "path": path, "cwd": "", "branch": "", "entrypoint": "",
        "thread_source": "", "ai_title": "", "custom_title": "", "first_prompt": "", "last_prompt": "",
        "n_prompts": 0, "last_text": "", "progress_text": "", "turn_text": "", "turn": "ended",
        "last_kind": "", "status": None, "goal": None, "final_has_status": False,
        "last_ts": 0, "final_ts": 0,
    }


def take_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text"))
    return ""


def is_injected(text):
    t = (text or "").lstrip()
    return (not t) or t.startswith(("<", "[Request interrupted", "Caveat:", "# AGENTS.md",
                                    "This session is being continued", "【定时唤醒"))


def on_final(st, text, ts):
    st["turn"] = "ended"
    st["last_kind"] = "final"
    st["final_ts"] = ts or st["final_ts"]
    if text.strip():
        st["last_text"] = text[-6000:]
    s = parse_status(text, ts or time.time())
    st["final_has_status"] = bool(s)
    if s:
        st["status"] = s
    g = parse_goal(text)
    if g:
        st["goal"] = g


CLAUDE_KEYS = ('"type":"assistant"', '"type":"user"', '"type":"system"', '"type":"ai-title"',
               '"type":"custom-title"', '"type":"last-prompt"')


def claude_line(st, line):
    if not any(k in line for k in CLAUDE_KEYS):
        return
    try:
        rec = json.loads(line)
    except ValueError:
        return
    typ = rec.get("type")
    ts = parse_iso(rec.get("timestamp")) or 0
    if ts > st["last_ts"]:
        st["last_ts"] = ts
    if rec.get("cwd"):
        st["cwd"] = rec["cwd"]
    if rec.get("gitBranch"):
        st["branch"] = rec["gitBranch"]
    if rec.get("entrypoint"):
        st["entrypoint"] = rec["entrypoint"]
    if typ == "ai-title":
        st["ai_title"] = rec.get("aiTitle") or st["ai_title"]
        return
    if typ == "custom-title":
        st["custom_title"] = rec.get("customTitle") or rec.get("title") or st["custom_title"]
        return
    if typ == "last-prompt":
        lp = rec.get("lastPrompt")
        if isinstance(lp, str) and not is_injected(lp):
            st["last_prompt"] = lp[:500]
        return
    if typ == "system":
        if rec.get("subtype") == "turn_duration" and st["turn"] != "ended":
            on_final(st, st["turn_text"], ts)
        return
    if rec.get("isSidechain"):
        return
    msg = rec.get("message") or {}
    content = msg.get("content")
    if typ == "user":
        if rec.get("isMeta"):
            return
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            st["turn"] = "running"
            st["last_kind"] = "tool"
            return
        text = take_text(content)
        if isinstance(content, list) and not text.lstrip().startswith(("<", "[Request interrupted")):
            note_images(st, ts, [((b.get("source") or {}).get("data"), (b.get("source") or {}).get("media_type"))
                                 for b in content if isinstance(b, dict) and b.get("type") == "image"
                                 and (b.get("source") or {}).get("type") == "base64"])
        if text.lstrip().startswith("[Request interrupted"):
            st["turn"] = "ended"
            st["last_kind"] = "final"
            return
        if text.lstrip().startswith("【定时唤醒"):
            st["turn"] = "running"
            st["last_kind"] = "user"
            st["turn_text"] = ""
            return
        if is_injected(text):
            return
        st["n_prompts"] += 1
        if not st["first_prompt"]:
            st["first_prompt"] = text[:500]
        st["last_prompt"] = text[:500]
        st["last_user_ts"] = ts
        st["turn"] = "running"
        st["last_kind"] = "user"
        st["turn_text"] = ""
        return
    if typ == "assistant":
        blocks = content if isinstance(content, list) else []
        text = take_text(content)
        tools = [b.get("name", "") for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        note_links(st, ts, text)
        if tools:
            st["turn"] = "running"
            st["last_kind"] = "tool_use"
            st["pending_tool"] = tools[-1]
            st["turn_text"] = ""
            if text.strip():
                st["progress_text"] = text[-1500:]
            return
        if text.strip():
            st["turn_text"] = (st["turn_text"] + "\n" + text)[-8000:]
            st["last_kind"] = "assistant_text"
            if msg.get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens"):
                on_final(st, st["turn_text"], ts)


CODEX_KEYS = ('"session_meta"', '"task_started"', '"task_complete"', '"turn_aborted"', '"turn_context"',
              '"role":"user"', '"role":"assistant"')


def codex_line(st, line):
    if not any(k in line for k in CODEX_KEYS):
        return
    try:
        rec = json.loads(line)
    except ValueError:
        return
    typ = rec.get("type")
    p = rec.get("payload") or {}
    ts = parse_iso(rec.get("timestamp")) or 0
    if ts > st["last_ts"]:
        st["last_ts"] = ts
    if typ == "session_meta":
        st["id"] = p.get("id") or st["id"]
        st["cwd"] = p.get("cwd") or st["cwd"]
        st["thread_source"] = str(p.get("thread_source"))
        git = p.get("git")
        if isinstance(git, dict) and git.get("branch"):
            st["branch"] = git["branch"]
        return
    if typ == "turn_context":
        if p.get("cwd"):
            st["cwd"] = p["cwd"]
        return
    if typ == "event_msg":
        pt = p.get("type")
        if pt == "task_started":
            st["turn"] = "running"
            st["last_user_ts"] = ts
            st["last_kind"] = "tool"
        elif pt == "task_complete":
            on_final(st, p.get("last_agent_message") or "", ts)
        elif pt == "turn_aborted":
            st["turn"] = "ended"
            st["last_kind"] = "final"
        return
    if typ == "response_item" and p.get("type") == "message":
        text = take_text(p.get("content"))
        if p.get("role") == "user" and isinstance(p.get("content"), list):
            note_images(st, ts, data_url_pairs(p["content"]))
        if p.get("role") == "user" and not is_injected(text):
            st["n_prompts"] += 1
            if not st["first_prompt"]:
                st["first_prompt"] = text[:500]
            st["last_prompt"] = text[:500]
        elif p.get("role") == "assistant" and text.strip():
            st["progress_text"] = text[-1500:]
            note_links(st, ts, text)


def parse_file(tool, path, st, offset, size):
    """增量读取：新文件读开头 + 末尾，旧文件只读新增部分（最多末尾 2MB）。返回 (读了多少字节, 新 offset)。"""
    handler = claude_line if tool == "claude" else codex_line
    segments = []
    if offset == 0:
        if size <= HEAD_BYTES + TAIL_BYTES:
            segments.append((0, size, False))
        else:
            segments.append((0, HEAD_BYTES, False))
            segments.append((size - TAIL_BYTES, size, True))
    else:
        start = max(offset, size - TAIL_BYTES)
        segments.append((start, size, start != offset))
    total, new_offset = 0, offset
    with open(path, "rb") as f:
        for a, b, skip_head in segments:
            f.seek(a)
            data = f.read(b - a)
            total += len(data)
            if skip_head:
                nl = data.find(b"\n")
                if nl < 0:
                    continue
                data = data[nl + 1:]
            end = data.rfind(b"\n")
            complete = data[:end + 1] if end >= 0 else b""
            if b == size:
                new_offset = size - (len(data) - len(complete))
            for raw in complete.split(b"\n"):
                if raw:
                    handler(st, raw.decode("utf-8", "replace"))
    return total, new_offset


def session_id_from_path(tool, path):
    stem = os.path.basename(path)[:-len(".jsonl")]
    return stem if tool == "claude" else stem[-36:]


def scan(conn, cfg):
    migrate_tz(conn)
    t0 = time.time()
    horizon = t0 - max(cfg["window_days"], 30) * 86400
    files = [("claude", p) for p in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl"))]
    files += [("codex", p) for p in glob.glob(os.path.join(CODEX_HOME, "sessions", "*", "*", "*", "*.jsonl"))]
    known = {r["path"]: r for r in conn.execute("SELECT * FROM files")}
    parsed, nbytes = 0, 0
    for tool, path in files:
        try:
            stt = os.stat(path)
        except OSError:
            continue
        if stt.st_mtime < horizon:
            continue
        row = known.get(path)
        if row and row["size"] == stt.st_size and row["mtime"] == stt.st_mtime:
            continue
        st, offset = None, 0
        if row and row["session_key"] and stt.st_size >= (row["offset"] or 0):
            got = conn.execute("SELECT data FROM sessions WHERE key=?", (row["session_key"],)).fetchone()
            if got:
                st, offset = json.loads(got["data"]), row["offset"] or 0
        if st is None:
            st, offset = new_state(tool, session_id_from_path(tool, path), path), 0
        try:
            n, offset = parse_file(tool, path, st, offset, stt.st_size)
        except OSError as e:
            log("读取失败 %s: %s" % (path, e))
            continue
        nbytes += n
        parsed += 1
        key = "%s:%s" % (tool, st["id"])
        flush_media(conn, key, st)
        if tool == "claude" and st.get("goal"):
            save_goal(conn, key, st["goal"].get("goal"), st["goal"].get("criteria"))
        conn.execute("INSERT OR REPLACE INTO sessions (key, last_activity, data) VALUES (?, ?, ?)",
                     (key, st["last_ts"] or stt.st_mtime, json.dumps(st, ensure_ascii=False)))
        conn.execute("INSERT OR REPLACE INTO files (path, session_key, size, mtime, offset) VALUES (?, ?, ?, ?, ?)",
                     (path, key, stt.st_size, stt.st_mtime, offset))
    for step in (sync_prompts, sync_codex_goals, sync_cmux_names):
        try:
            step(conn)
        except Exception:
            log("%s 出错：%s" % (step.__name__, traceback.format_exc()))
    ms = int((time.time() - t0) * 1000)
    meta_set(conn, "last_scan", time.time())
    meta_set(conn, "last_scan_ms", ms)
    conn.commit()
    return {"files": len(files), "parsed": parsed, "bytes": nbytes, "ms": ms}


_codex_cache = {"mtime": None, "titles": {}, "goals": {}, "goals_at": 0}


def codex_extras():
    path = os.path.join(CODEX_HOME, "session_index.jsonl")
    try:
        mt = os.stat(path).st_mtime
    except OSError:
        mt = None
    if mt != _codex_cache["mtime"]:
        titles = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("id") and r.get("thread_name"):
                        titles[r["id"]] = r["thread_name"]
        except OSError:
            pass
        _codex_cache.update(mtime=mt, titles=titles)
    if time.time() - _codex_cache["goals_at"] > 300:
        goals = {}
        try:
            c = sqlite3.connect("file:%s?mode=ro" % os.path.join(CODEX_HOME, "goals_1.sqlite"), uri=True, timeout=2)
            for tid, obj, status in c.execute("SELECT thread_id, objective, status FROM thread_goals"):
                goals[tid] = {"objective": obj, "status": status}
            c.close()
        except sqlite3.Error:
            pass
        _codex_cache.update(goals=goals, goals_at=time.time())
    return _codex_cache["titles"], _codex_cache["goals"]


# ---------------------------------------------------------------- 历史消息（你发给 agent 的每一句话）

CLAUDE_HISTORY = os.path.join(HOME, ".claude", "history.jsonl")
_threads_cache = {"at": 0, "map": {}}


def codex_threads():
    """Codex 线程元信息：{id: (source, cwd, title)}，5 分钟缓存。"""
    if time.time() - _threads_cache["at"] > 300:
        found = {}
        try:
            c = sqlite3.connect("file:%s?mode=ro" % os.path.join(CODEX_HOME, "state_5.sqlite"), uri=True, timeout=2)
            for tid, source, cwd, title in c.execute("SELECT id, source, cwd, title FROM threads"):
                found[tid] = (source or "", cwd or "", title or "")
            c.close()
        except sqlite3.Error as e:
            log("读 Codex 线程表失败：%s" % e)
        _threads_cache.update(at=time.time(), map=found)
    return _threads_cache["map"]


def sync_prompts(conn):
    """增量收集你发出的消息。Claude 默认 30 天后删对话记录，这里各存一份纯文本，几 MB 一年。"""
    added = 0
    # Claude：~/.claude/history.jsonl（输入框历史，含粘贴的文字）
    try:
        size = os.path.getsize(CLAUDE_HISTORY)
    except OSError:
        size = 0
    off = int(float(meta_get(conn, "hist_claude_off", 0) or 0))
    if size < off:
        off = 0
    if size > off:
        with open(CLAUDE_HISTORY, "rb") as f:
            f.seek(off)
            data = f.read(size - off)
        end = data.rfind(b"\n")
        data = data[:end + 1] if end >= 0 else b""
        titles = {}
        for row in conn.execute("SELECT key, data FROM sessions WHERE key LIKE 'claude:%'"):
            st = json.loads(row["data"])
            titles[row["key"]] = st.get("custom_title") or st.get("ai_title") or ""
        for raw in data.split(b"\n"):
            if not raw:
                continue
            try:
                r = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            text = r.get("display") or ""
            pasted = r.get("pastedContents") or {}
            extra = [v.get("content", "")[:4000] for v in (pasted.values() if isinstance(pasted, dict) else [])
                     if isinstance(v, dict) and v.get("type") == "text" and v.get("content")]
            if text.startswith("/") and len(text) < 40 and not extra:
                continue
            if text.startswith("!"):  # 终端里直接跑的命令，不是对 AI 说的话
                continue
            if not text.strip() and not extra:
                continue
            sid = r.get("sessionId") or ""
            key = "claude:" + sid
            ts = (r.get("timestamp") or 0) / 1000.0
            full = "\n".join([text] + extra)[:8000]
            cur = conn.execute("INSERT OR IGNORE INTO prompts VALUES (?, ?, 'claude', ?, ?, ?, ?, ?)",
                               ("claude:%s:%d:%s" % (sid, r.get("timestamp") or 0, hashlib.sha1(full.encode("utf-8")).hexdigest()[:8]), key, ts, full,
                                project_of(r.get("project")), r.get("project") or "", titles.get(key, "")))
            added += cur.rowcount
        meta_set(conn, "hist_claude_off", off + len(data))
    # Codex：thread_history 里的 userMessage，只收你自己开的线程
    last_ms = int(float(meta_get(conn, "hist_codex_ms", 0) or 0))
    threads, autos = codex_threads(), automation_threads()
    try:
        c = sqlite3.connect("file:%s?mode=ro" % os.path.join(CODEX_HOME, "thread_history_1.sqlite"), uri=True, timeout=5)
        rows = c.execute("SELECT thread_id, item_id, created_at_ms, item_json FROM thread_items "
                         "WHERE item_type='userMessage' AND created_at_ms > ? ORDER BY created_at_ms LIMIT 20000",
                         (last_ms,)).fetchall()
        c.close()
    except sqlite3.Error as e:
        log("读 Codex 历史失败：%s" % e)
        rows = []
    for tid, item_id, cms, item_json in rows:
        last_ms = max(last_ms, cms or 0)
        source, cwd, title = threads.get(tid, ("", "", ""))
        if source not in ("vscode", "cli") or tid in autos:
            continue
        try:
            item = json.loads(item_json)
        except ValueError:
            continue
        text = take_text(item.get("content"))
        if is_injected(text):
            continue
        cur = conn.execute("INSERT OR IGNORE INTO prompts VALUES (?, ?, 'codex', ?, ?, ?, ?, ?)",
                           ("codex:" + item_id, "codex:" + tid, (cms or 0) / 1000.0, text[:8000],
                            project_of(cwd), cwd, title))
        added += cur.rowcount
    meta_set(conn, "hist_codex_ms", last_ms)
    if added:
        log("历史消息新增 %d 条" % added)
    return added


def sync_codex_links(conn):
    """Codex 回复里的链接：第一次运行时从 thread_history 的 agentMessage 回填全部历史（约 15 秒）。"""
    last_ms = int(float(meta_get(conn, "links_codex_ms", 0) or 0))
    threads, autos = codex_threads(), automation_threads()
    try:
        c = sqlite3.connect("file:%s?mode=ro" % os.path.join(CODEX_HOME, "thread_history_1.sqlite"), uri=True, timeout=5)
        newest = c.execute("SELECT MAX(created_at_ms) FROM thread_items WHERE item_type='agentMessage'").fetchone()[0] or 0
        rows = c.execute("SELECT thread_id, created_at_ms, item_json FROM thread_items WHERE item_type='agentMessage' "
                         "AND created_at_ms > ? AND created_at_ms <= ? AND item_json LIKE '%http%' ORDER BY created_at_ms LIMIT 50000",
                         (last_ms, newest)).fetchall()
        c.close()
    except sqlite3.Error as e:
        log("读 Codex 回复失败：%s" % e)
        return
    for tid, cms, item_json in rows:
        if threads.get(tid, ("",))[0] not in ("vscode", "cli") or tid in autos:
            continue
        try:
            text = json.loads(item_json).get("text") or ""
        except ValueError:
            continue
        st = {}
        note_links(st, (cms or 0) / 1000.0, text)
        flush_media(conn, "codex:" + tid, st)
    meta_set(conn, "links_codex_ms", max(last_ms, newest) if len(rows) < 50000 else rows[-1][1])


def sync_codex_goals(conn):
    if not meta_get(conn, "claude_goals_v1"):
        # 目标表是 10-29 才加的，之前解析过的 Claude 会话补存一次 🎯
        for r in conn.execute("SELECT key, data FROM sessions WHERE key LIKE 'claude:%'").fetchall():
            g = json.loads(r["data"]).get("goal") or {}
            save_goal(conn, r["key"], g.get("goal"), g.get("criteria"))
        meta_set(conn, "claude_goals_v1", time.time())
    _, goals = codex_extras()
    for tid, g in goals.items():
        save_goal(conn, "codex:" + tid, g.get("objective"), "", g.get("status"))


_cmux_names = {"mtime": None}


def sync_cmux_names(conn):
    """cmux 侧边栏里你手动改的名字（只认单个 AI 会话的工作区，分组标题不算）。"""
    try:
        mt = os.stat(CMUX_STATE).st_mtime
    except OSError:
        return
    if mt == _cmux_names["mtime"]:
        return
    with open(CMUX_STATE, encoding="utf-8") as f:
        data = json.load(f)
    _cmux_names["mtime"] = mt
    now = time.time()
    for win in data.get("windows") or []:
        tm = win.get("tabManager") or {}
        anchors = {g.get("anchorWorkspaceId") for g in tm.get("workspaceGroups") or []}
        for w in tm.get("workspaces") or []:
            if w.get("workspaceId") in anchors:
                continue
            agents = [((p.get("terminal") or {}).get("agent") or {}) for p in w.get("panels") or []]
            agents = [a for a in agents if a.get("sessionId") and a.get("kind") in ("claude", "codex")]
            if len(agents) != 1:
                continue
            key = "%s:%s" % (agents[0]["kind"], agents[0]["sessionId"])
            title = (w.get("customTitle") or "").strip()
            if w.get("customTitleSource") == "user" and title:
                conn.execute("INSERT INTO names (session_key, title, at) VALUES (?, ?, ?) ON CONFLICT(session_key) "
                             "DO UPDATE SET title=excluded.title, at=excluded.at WHERE names.title != excluded.title",
                             (key, title, now))
            else:
                conn.execute("DELETE FROM names WHERE session_key=?", (key,))


def name_map(conn):
    return {r["session_key"]: r["title"] for r in conn.execute("SELECT session_key, title FROM names")}


def tag_map(conn):
    return {r["project"]: (r["label"], r["color"]) for r in conn.execute("SELECT * FROM tags")}


def tag_of(tags, project):
    label, color = tags.get(project, (None, None))
    return label or project, color or color_of(project)


def attach_images(conn, items):
    """每张图归到它之前最近的那句话（容差 5 秒）。"""
    for key in {it["key"] for it in items}:
        imgs = conn.execute("SELECT file, ts FROM images WHERE session_key=? ORDER BY ts", (key,)).fetchall()
        if not imgs:
            continue
        tss = [r[0] for r in conn.execute("SELECT ts FROM prompts WHERE session_key=? ORDER BY ts", (key,))]
        if not tss:
            continue
        owner = {}
        for im in imgs:
            i = max(bisect.bisect_right(tss, im["ts"] + 5) - 1, 0)
            owner.setdefault(tss[i], []).append(im["file"])
        for it in items:
            if it["key"] == key:
                it["images"] = owner.get(it["ts"], [])


def merge_batches(items, gap=3):
    """同一会话里几乎同一时刻进来的几段（排队发出、或一句话被拆开）合成一条显示。"""
    out = []
    for it in items:
        prev = out[-1] if out else None
        if prev and prev["key"] == it["key"] and abs(prev["ts"] - it["ts"]) <= gap:
            a, b = (prev, it) if prev["ts"] <= it["ts"] else (it, prev)
            prev["text"] = a["text"] + "\n" + b["text"]
            prev["images"] = a.get("images", []) + b.get("images", [])
            continue
        out.append(it)
    return out


def session_extras(conn, key, limit=50):
    g = conn.execute("SELECT goal, criteria, status FROM goals WHERE session_key=?", (key,)).fetchone()
    links = conn.execute("SELECT url, label, ts FROM links WHERE session_key=? ORDER BY ts DESC LIMIT ?", (key, limit)).fetchall()
    n = conn.execute("SELECT COUNT(*) FROM links WHERE session_key=?", (key,)).fetchone()[0]
    return {
        "goal": g["goal"] if g else "", "criteria": g["criteria"] if g else "",
        "goalStatus": GOAL_STATUS.get(g["status"], g["status"]) if g and g["status"] else "",
        "links": [{"url": r["url"], "label": r["label"], "name": link_name(r["url"]),
                   "timeText": fmt_time(r["ts"], time.time()) if r["ts"] else ""} for r in links],
        "linksTotal": n,
    }


GOAL_STATUS = {"active": "进行中", "paused": "已暂停", "blocked": "卡住了", "usage_limited": "额度用完暂停",
               "budget_limited": "预算用完暂停", "complete": "已完成"}


def prompt_rows_to_items(conn, rows, now):
    titles, _ = codex_extras()
    threads = codex_threads()
    names, tags = name_map(conn), tag_map(conn)
    out = []
    for r in rows:
        tool, sid = r["session_key"].split(":", 1)
        title = names.get(r["session_key"]) or r["title"]
        if names.get(r["session_key"]):
            pass
        elif tool == "codex":
            title = titles.get(sid) or threads.get(sid, ("", "", ""))[2] or title
        elif not title:
            got = conn.execute("SELECT data FROM sessions WHERE key=?", (r["session_key"],)).fetchone()
            if got:
                st = json.loads(got["data"])
                title = st.get("custom_title") or st.get("ai_title") or ""
        if not title:
            fp = conn.execute("SELECT text FROM prompts WHERE session_key=? ORDER BY ts LIMIT 1", (r["session_key"],)).fetchone()
            if fp:
                title = clean_line(fp["text"].strip().splitlines()[0] if fp["text"].strip() else "")[:30]
        label, color = tag_of(tags, r["project"])
        out.append({
            "key": r["session_key"], "tool": tool, "toolLabel": "Claude" if tool == "claude" else "Codex",
            "ts": r["ts"], "timeText": fmt_time(r["ts"], now), "text": r["text"], "project": label,
            "projectKey": r["project"], "color": color, "title": title or "（未命名会话）", "images": [],
            "resume": ("cd %s && claude --resume %s" % (shell_quote(r["cwd"] or HOME), sid)) if tool == "claude"
                      else "codex resume %s" % sid,
        })
    attach_images(conn, out)
    return merge_batches(out)


def _like_where(q):
    words = [w for w in (q or "").split() if w][:6]
    conds, args = [], []
    for w in words:
        like = "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds.append("(text LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR project LIKE ? ESCAPE '\\' OR session_key IN "
                     "(SELECT session_key FROM names WHERE title LIKE ? ESCAPE '\\') OR project IN "
                     "(SELECT project FROM tags WHERE label LIKE ? ESCAPE '\\'))")
        args += [like, like, like, like, like]
    return conds, args


def search_prompts(conn, q, limit=100, offset=0):
    now = time.time()
    conds, args = _like_where(q)
    sql_where = (" WHERE " + " AND ".join(conds)) if conds else ""
    total = conn.execute("SELECT COUNT(*) FROM prompts" + sql_where, args).fetchone()[0]
    rows = conn.execute("SELECT * FROM prompts" + sql_where + " ORDER BY ts DESC LIMIT ? OFFSET ?",
                        args + [limit, offset]).fetchall()
    first = conn.execute("SELECT MIN(ts), COUNT(*) FROM prompts").fetchone()
    return {"q": q, "total": total, "items": prompt_rows_to_items(conn, rows, now),
            "all": first[1], "since": fmt_time(first[0], now) if first[0] else ""}


def search_sessions(conn, q, limit=50, offset=0):
    """按会话汇总：有命中的会话按最近一次命中排序，每个会话带最多 3 条命中预览。"""
    now = time.time()
    conds, args = _like_where(q)
    sql_where = (" WHERE " + " AND ".join(conds)) if conds else ""
    total = conn.execute("SELECT COUNT(DISTINCT session_key) FROM prompts" + sql_where, args).fetchone()[0]
    rows = conn.execute("SELECT session_key, COUNT(*) AS hits, MAX(ts) AS last_ts FROM prompts" + sql_where +
                        " GROUP BY session_key ORDER BY last_ts DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    out = []
    for r in rows:
        key = r["session_key"]
        n = conn.execute("SELECT COUNT(*) FROM prompts WHERE session_key=?", (key,)).fetchone()[0]
        extra = (" AND " + " AND ".join(conds)) if conds else ""
        prev = conn.execute("SELECT * FROM prompts WHERE session_key=?" + extra + " ORDER BY ts DESC LIMIT ?",
                            [key] + args + [8 if conds else 4]).fetchall()
        items = prompt_rows_to_items(conn, prev, now)[:3 if conds else 1]
        head = items[0]
        ex = session_extras(conn, key, limit=0)
        out.append({"key": key, "title": head["title"], "project": head["project"], "projectKey": head["projectKey"],
                    "color": head["color"], "tool": head["tool"], "toolLabel": head["toolLabel"], "count": n,
                    "hits": r["hits"], "lastText": fmt_time(r["last_ts"], now), "resume": head["resume"],
                    "goal": ex["goal"], "linksTotal": ex["linksTotal"],
                    "preview": [{"timeText": it["timeText"], "text": it["text"], "images": it["images"]} for it in items]})
    first = conn.execute("SELECT MIN(ts), COUNT(*), COUNT(DISTINCT session_key) FROM prompts").fetchone()
    return {"q": q, "total": total, "sessions": out, "all": first[1], "allSessions": first[2],
            "since": fmt_time(first[0], now) if first[0] else ""}


def session_prompts(conn, key):
    rows = conn.execute("SELECT * FROM prompts WHERE session_key=? ORDER BY ts", (key,)).fetchall()
    res = {"key": key, "items": prompt_rows_to_items(conn, rows, time.time())}
    res.update(session_extras(conn, key, limit=200))
    return res


# ---------------------------------------------------------------- 看板

GROUPS = [
    ("need", "🔔 现在需要你", "AI 停下来等你、到点了、或卡住了；等得最久的排最前"),
    ("ai", "🏃 AI 在做", "正在跑，不用管；跑完会自动挪到上面"),
    ("confirm", "✅ 待你确认完成", "AI 认为做完了，你点「完成」才算数"),
    ("time", "⏰ 等时间点", "到点会提醒你，或自动唤醒会话"),
    ("peer", "👥 等同事", "到了约定再跟的时间会挪到最上面"),
    ("backlog", "🗂 待整理", "状态不明（规则生效前开的会话）或 2 天没动静；看一眼，没用了就点「完成」"),
]
PALETTE = ["#3370ff", "#7f3bf5", "#0fb5a8", "#f77234", "#d83931", "#2ea121", "#c45ec0", "#8f959e"]


def project_of(cwd):
    p = (cwd or "").rstrip("/")
    for parent in sorted(PROJECT_PARENTS, key=len, reverse=True):
        if p.startswith(parent + "/"):
            name = re.sub(r"-wt-.*$", "", p[len(parent) + 1:].split("/")[0])
            return "其他" if name.startswith(".") else name
    if not p.startswith(HOME):
        return os.path.basename(p) or "其他"
    parts = [x for x in p[len(HOME):].split("/") if x]
    if not parts:
        return "主目录"
    if parts[0] == "Desktop":
        if len(parts) == 1:
            return "桌面"
        name = parts[1]
    else:
        name = parts[0]
    name = re.sub(r"-wt-.*$", "", name)
    return "其他" if name.startswith(".") else name


def color_of(name):
    fixed = {"桌面": "#8f959e", "主目录": "#8f959e"}   # 其余按名字取色，可在看板上点标签改
    if name in fixed:
        return fixed[name]
    return PALETTE[sum(ord(c) for c in name) % len(PALETTE)]


def clean_line(s):
    s = re.sub(r"\[Image #\d+\]", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^\s*(#+|[-*>]|\d+[.、)])\s*", "", s)
    s = CLEAN_RE.sub("", s).strip()
    return s


def excerpt(text, limit=100):
    lines = [l for l in (text or "").splitlines() if l.strip()]
    for l in reversed(lines):
        t = l.strip()
        if t.startswith(("|", "```", "📌", "🎯", "---", "<")):
            continue
        t = clean_line(t)
        if re.fullmatch(r"[0-9a-f-]{20,}", t):
            continue
        if len(t) >= 6:
            return t if len(t) <= limit else t[:limit] + "…"
    return ""


def tail_text(text, limit=600):
    t = "\n".join(l.replace("**", "") for l in (text or "").splitlines() if not l.strip().startswith(("📌", "🎯")))
    t = t.strip()
    return t if len(t) <= limit else "…" + t[-limit:]


_auto_cache = {"at": 0, "ids": set()}


def automation_threads():
    """Codex 自动任务（📅 每日提醒之类）绑定的线程，不算你的任务。"""
    if time.time() - _auto_cache["at"] > 300:
        ids = set()
        for path in glob.glob(os.path.join(CODEX_HOME, "automations", "*", "automation.toml")):
            try:
                with open(path, encoding="utf-8") as f:
                    m = re.search(r'^target_thread_id\s*=\s*"([^"]+)"', f.read(), re.M)
                    if m:
                        ids.add(m.group(1))
            except OSError:
                pass
        _auto_cache.update(at=time.time(), ids=ids)
    return _auto_cache["ids"]


def include_session(st):
    if st["tool"] == "codex" and st["id"] in automation_threads():
        return False
    if st["tool"] == "claude":
        if (st.get("entrypoint") or "").startswith("sdk"):
            return False
    elif st.get("thread_source") != "user":
        return False
    return st.get("n_prompts", 0) > 0


def make_item(st, now, cfg, ov, sched, titles, goals, ctx):
    key = "%s:%s" % (st["tool"], st["id"])
    last = st.get("last_ts") or 0
    idle = now - last
    if idle > cfg["window_days"] * 86400 and not sched:
        status = st.get("status") or {}
        if status.get("kind") not in ("peer", "time", "sched"):
            return None
    turn = st.get("turn")
    status = st.get("status")
    has_status = st.get("final_has_status")
    decisions = []
    if turn == "running" and st.get("last_kind") == "assistant_text" and idle > 120:
        turn = "ended"
        status = parse_status(st.get("turn_text", ""), last) or None
        has_status = bool(status)
        decisions = parse_decisions(st.get("turn_text"))
    elif turn == "ended" and (st.get("final_ts") or 0) >= (st.get("last_user_ts") or 0):
        # 中断的那一轮不更新 final_ts，last_text 还是上一轮的，里面的选项已经过时
        decisions = parse_decisions(st.get("last_text"))
    goal_obj = st.get("goal") or {}
    codex_goal = goals.get(st["id"], {}) if st["tool"] == "codex" else {}
    title = (ctx["names"].get(key) or st.get("custom_title") or titles.get(st["id"]) or st.get("ai_title") or goal_obj.get("goal")
             or clean_line((st.get("first_prompt") or "").splitlines()[0] if st.get("first_prompt") else "")[:40]
             or "（无标题）")
    project = project_of(st.get("cwd"))
    project_label, project_color = tag_of(ctx["tags"], project)
    saved = ctx["goals"].get(key) or {}
    reply_hint = excerpt(st.get("last_text")) or excerpt(st.get("progress_text"))
    badges = []
    group, kind, headline, sort = "need", "me", "", 0
    stale_sec = cfg["stale_hours"] * 3600

    if ov and ov["action"] == "untrack":
        group, headline = "hidden", "不再跟踪"
    elif ov and ov["action"] == "done" and last <= (ov["at"] or 0) + 120:
        group, headline = "hidden", "已完成"
    elif turn == "running" and st.get("last_kind") == "tool_use" and (
            st.get("pending_tool") in ("AskUserQuestion", "ExitPlanMode") or idle > 600):
        group, kind, sort = "need", "approval", 0
        tool = st.get("pending_tool") or "操作"
        if tool == "AskUserQuestion":
            headline = "它问了你一个问题，在等你回答"
        elif tool == "ExitPlanMode":
            headline = "计划写好了，在等你确认"
        else:
            headline = "可能在等你批准一个操作（%s），也可能命令还在跑（已 %s）" % (tool, fmt_ago(idle))
    elif turn == "running":
        if idle > cfg["interrupted_min"] * 60:
            group, kind, sort = "need", "interrupted", 0
            if st.get("last_kind") == "user":
                ask = clean_line((st.get("last_prompt") or "").splitlines()[0] if st.get("last_prompt") else "")[:40]
                headline = "你%s 发的消息它没回，可能卡住了：「%s」" % (fmt_time(last, now), ask)
            else:
                headline = "%s前停在半路，可能已中断，点「打开会话」接着做" % fmt_ago(idle)
        else:
            group, kind = "ai", "ai"
            ask = clean_line((st.get("last_prompt") or "").splitlines()[0] if st.get("last_prompt") else "")[:40]
            since = st.get("last_user_ts") or 0
            dur = "（已跑 %s）" % fmt_ago(now - since) if since and now - since < 86400 else ""
            prog = excerpt(st.get("progress_text"), 60)
            headline = ("在做你交代的：「%s」%s" % (ask, dur)) if ask else ("在做：" + prog if prog else "AI 在做")
    elif sched and sched["status"] == "pending":
        group, kind = "time", "sched"
        headline = "%s 自动唤醒会话：%s" % (fmt_time(sched["at"], now), sched["note"] or sched["prompt"][:60])
        sort = sched["at"]
    elif sched and sched["status"] in ("failed", "missed") and (sched["done_at"] or 0) >= last - 60:
        group, kind, sort = "need", "sched_failed", 0
        headline = "定时唤醒没执行：%s" % (sched["last_error"] or "原因不明")
    elif not status or not has_status:
        if idle > stale_sec:
            group, kind = "stale", "nostatus"
            headline = "%s没动静 · 上次停在：%s" % (fmt_ago(idle), reply_hint or "（没有内容）")
        else:
            group, kind = "unknown", "nostatus"
            headline = "上次停在：" + (reply_hint or "（没有内容）")
    else:
        k, desc, at = status["kind"], status.get("desc") or "", status.get("at")
        who = status.get("who") or "同事"
        if k == "me" and at and at > (status.get("ts") or 0) + 300:
            # 「等你」后面写了将来的时间：先放「等时间点」，到点挪到「现在需要你」并弹通知
            kind = "time"
            if at > now:
                group, sort = "time", at
                headline = "%s 到点后：%s" % (fmt_time(at, now), desc)
            else:
                group, sort = "need", 0
                headline = "已到点（%s）：%s" % (fmt_time(at, now), desc)
        elif k == "me":
            group = "stale" if idle > stale_sec else "need"
            kind, sort = "me", 2
            headline = ("%s没动静 · 下一步：%s" % (fmt_ago(idle), desc)) if group == "stale" else "下一步：" + desc
        elif k == "peer":
            kind = "peer"
            if at and at <= now:
                group, sort = "need", 1
                headline = "该跟进了：等%s · %s" % (who, desc)
            else:
                group, sort = "peer", at or (now + 1e9)
                headline = "等%s：%s" % (who, desc) + (" · %s 再跟" % fmt_time(at, now) if at else "")
        elif k == "time":
            kind = "time"
            if at and at <= now:
                group, sort = "need", 0
                headline = "已到点（%s）：%s" % (fmt_time(at, now), desc)
            elif at:
                group, sort = "time", at
                headline = "%s：%s" % (fmt_time(at, now), desc)
            else:
                group, sort = "need", 2
                headline = "下一步：" + desc
        elif k == "sched":
            kind = "sched"
            if at and at > now:
                group, sort = "time", at
                headline = "%s 会话会自己继续：%s" % (fmt_time(at, now), desc)
            elif at and now - at > 3600 and last < at:
                group, kind, sort = "need", "sched_failed", 0
                headline = "定时时间已过，会话没动静：%s" % desc
            else:
                group, sort = "time", at or now
                headline = "定时任务：%s" % desc
        elif k == "ai":
            group, kind = ("stale" if idle > 6 * 3600 else "ai"), "ai"
            headline = ("%s没动静 · AI 说在做：%s" % (fmt_ago(idle), desc)) if group == "stale" else "AI 在做：" + desc
        elif k == "confirm":
            group, kind = "confirm", "confirm"
            headline = "已达到完成标准：" + desc
        if status and status.get("label") and status["label"] not in ("等你", "等同事", "等时间点", "已定时", "AI在做", "待你确认完成"):
            badges.append(status["label"])

    if group in ("unknown", "stale"):
        badges.insert(0, "状态不明" if group == "unknown" else "%s没动静" % fmt_ago(idle))
        group = "backlog"
    snoozed = (ov and ov["action"] == "snooze" and (ov["until_ts"] or 0) > now and last <= (ov["at"] or 0) + 120)
    if snoozed and group in ("need", "peer", "confirm", "backlog"):
        group, sort = "time", ov["until_ts"]
        headline = "推迟到 %s · %s" % (fmt_time(ov["until_ts"], now), headline)
    if decisions and group != "hidden":
        badges.insert(0, "%d 件待你决定" % len(decisions))
    else:
        decisions = []

    if st["tool"] == "claude":
        resume = "cd %s && claude --resume %s" % (shell_quote(st.get("cwd") or HOME), st["id"])
    else:
        resume = "codex resume %s" % st["id"]
    return {
        "key": key, "tool": st["tool"], "toolLabel": "Claude" if st["tool"] == "claude" else "Codex",
        "title": title, "project": project_label, "projectKey": project, "color": project_color, "group": group, "kind": kind,
        "headline": headline, "badges": badges, "sort": sort, "idle": idle, "last": last,
        "meta": "最后动静 %s（%s前）" % (fmt_time(last, now), fmt_ago(idle)) if last else "",
        "scheduleId": sched["id"] if sched and sched["status"] == "pending" else None,
        "statusKey": "%s|%s" % ((status or {}).get("ts"), (status or {}).get("at")),
        "details": {
            "goal": goal_obj.get("goal") or codex_goal.get("objective") or saved.get("goal") or "",
            "criteria": goal_obj.get("criteria") or saved.get("criteria") or "",
            "goalStatus": GOAL_STATUS.get(codex_goal.get("status") or saved.get("status") or "", ""),
            "originalTitle": (st.get("custom_title") or titles.get(st["id"]) or st.get("ai_title") or "")
                             if ctx["names"].get(key) else "",
            "links": ctx["links"].get(key, [])[:8], "linksTotal": len(ctx["links"].get(key, [])),
            "firstPrompt": (st.get("first_prompt") or "")[:300],
            "lastPrompt": (st.get("last_prompt") or "")[:300] if st.get("last_prompt") != st.get("first_prompt") else "",
            "lastReply": tail_text(st.get("last_text") or st.get("progress_text")),
            "status": ("📌 " + status["label"] + ("｜" + status["desc"] if status.get("desc") else "")) if status else "",
            "decisions": decisions,
            "cwd": st.get("cwd") or "", "branch": st.get("branch") or "", "id": st["id"], "resume": resume,
        },
    }


def shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def build_board(conn, cfg, now=None):
    now = now or time.time()
    titles, goals = codex_extras()
    overrides = {r["key"]: r for r in conn.execute("SELECT * FROM overrides")}
    scheds = {}
    for r in conn.execute("SELECT * FROM schedules WHERE status='pending' OR (status IN ('failed','missed') AND done_at > ?) ORDER BY at",
                          (now - 3 * 86400,)):
        cur = scheds.get(r["session_key"])
        if cur is None or (r["status"] == "pending" and cur["status"] != "pending"):
            scheds[r["session_key"]] = r
    items, hidden = [], []
    ctx = {"names": name_map(conn), "tags": tag_map(conn), "links": {},
           "goals": {r["session_key"]: dict(r) for r in conn.execute("SELECT * FROM goals")}}
    for r in conn.execute("SELECT session_key, url, label, ts FROM links WHERE ts >= ? ORDER BY ts DESC", (now - 30 * 86400,)):
        ctx["links"].setdefault(r["session_key"], []).append({"url": r["url"], "label": r["label"], "name": link_name(r["url"]),
                                                             "timeText": fmt_time(r["ts"], now)})
    for r in conn.execute("SELECT data FROM sessions WHERE last_activity >= ?", (now - 30 * 86400,)):
        st = json.loads(r["data"])
        if not include_session(st):
            continue
        key = "%s:%s" % (st["tool"], st["id"])
        it = make_item(st, now, cfg, overrides.get(key), scheds.get(key), titles, goals, ctx)
        if it is None:
            continue
        if it["group"] == "hidden":
            if now - it["last"] < cfg["window_days"] * 86400:
                hidden.append(it)
        else:
            items.append(it)
    groups = []
    for gid, title, hint in GROUPS:
        g = [it for it in items if it["group"] == gid]
        if gid == "need":
            g.sort(key=lambda it: (it["sort"] if it["sort"] < 2 else 2, -it["idle"]))
        elif gid in ("time", "peer"):
            g.sort(key=lambda it: it["sort"])
        else:
            g.sort(key=lambda it: it["idle"])
        groups.append({"id": gid, "title": title, "hint": hint, "items": g})
    hidden.sort(key=lambda it: it["idle"])
    last_scan = float(meta_get(conn, "last_scan", 0) or 0)
    return {
        "now": now, "nowText": "%d月%d日 周%s %s" % (clock(now).month, clock(now).day, WEEK[clock(now).weekday()], clock(now).strftime("%H:%M")),
        "lastScan": last_scan, "lastScanText": fmt_time(last_scan, now) if last_scan else "从未",
        "scanMs": int(meta_get(conn, "last_scan_ms", 0) or 0), "scanIntervalMin": cfg["scan_interval_min"],
        "groups": groups, "hidden": hidden, "digestTime": cfg["digest_time"], "tzText": tz_text(cfg),
    }


# ---------------------------------------------------------------- cmux 会话定位、打开、唤醒

def cmux(*args, timeout=10):
    return run([CMUX] + list(args), timeout=timeout)


_surface_cache = {"at": 0, "map": {}}
CMUX_DENIED = "cmux 不允许看板控制它，没法替你跳转：到 cmux 设置 → 自动化，把「套接字控制模式」改成「密码模式」并设一个套接字密码"


def cmux_surfaces():
    """返回 {会话 ID: (workspace, surface)}，30 秒缓存；cmux 不让控制时返回 None。"""
    if time.time() - _surface_cache["at"] < 30:
        return _surface_cache["map"]
    tree = cmux("tree", "--all")
    if tree.returncode != 0:
        log("cmux 拒绝访问：%s" % (tree.stderr or tree.stdout).strip()[:200])
        return None
    found, ws = {}, None
    for line in tree.stdout.splitlines():
        m = re.search(r"workspace (workspace:\d+)", line)
        if m:
            ws = m.group(1)
            continue
        m = re.search(r"surface (surface:\d+) \[terminal\]", line)
        if m and ws:
            sid = surface_checkpoint(ws, m.group(1))
            if sid:
                found[sid] = (ws, m.group(1))
    _surface_cache.update(at=time.time(), map=found)
    return found


def surface_checkpoint(ws, sf):
    r = cmux("surface", "resume", "show", "--json", "--workspace", ws, "--surface", sf)
    try:
        rec = (json.loads(r.stdout) or {}).get("restore_record") or {}
        return rec.get("checkpoint_id")
    except ValueError:
        return None


def resume_in_new_workspace(st, title):
    sid, cwd = st["id"], st.get("cwd") or HOME
    if not UUID_RE.match(sid):
        return None
    name = re.sub(r"[\r\n]+", " ", title)[:40]
    r = cmux("new-workspace", "--name", name, "--cwd", cwd, "--command", "claude --resume " + sid, "--focus", "true")
    _surface_cache["at"] = 0
    m = re.search(r"workspace:\d+", r.stdout or "")
    return m.group(0) if m else ("" if r.returncode == 0 else None)


def open_session(conn, key):
    row = conn.execute("SELECT data FROM sessions WHERE key=?", (key,)).fetchone()
    if row:
        st = json.loads(row["data"])
    else:
        p = conn.execute("SELECT cwd FROM prompts WHERE session_key=? LIMIT 1", (key,)).fetchone()
        if not p or ":" not in key:
            return False, "找不到这个会话"
        tool, sid = key.split(":", 1)
        st = {"tool": tool, "id": sid, "cwd": p["cwd"], "turn": "ended", "last_ts": 0}
    if st["tool"] == "codex":
        if not UUID_RE.match(st["id"]):
            return False, "会话 ID 不对"
        run(["open", "codex://threads/" + st["id"]])
        return True, "已在 Codex 里打开"
    surfaces = cmux_surfaces()
    if surfaces is None:
        return False, CMUX_DENIED
    loc = surfaces.get(st["id"])
    if loc:
        cmux("select-workspace", "--workspace", loc[0])
        run(["open", "-a", "cmux"])
        return True, "已切到 cmux 里的这个会话"
    if st.get("turn") == "running" and time.time() - (st.get("last_ts") or 0) < 300:
        return False, "这个会话正在运行，但不在 cmux 里（可能在别的终端）"
    ws = resume_in_new_workspace(st, name_map(conn).get(key) or st.get("custom_title") or st.get("ai_title") or "恢复会话")
    if ws is None:
        return False, "没能在 cmux 里重新打开（目录不存在或 cmux 没响应）"
    run(["open", "-a", "cmux"])
    return True, "已在 cmux 新开一个工作区恢复这个会话"


def wake_text(s, now):
    created = fmt_time(s["created"], now)
    planned = fmt_time(s["at"], now)
    prompt = re.sub(r"\s*\n\s*", "；", s["prompt"].strip()).replace("\\", "/")
    return ("【定时唤醒｜%s 由本会话安排，计划 %s，现在 %s】%s"
            "（先核对现在是否仍适合执行；不适合就不做，写明原因并更新状态行。）") % (
        created, planned, clock().strftime("%H:%M"), prompt)


def wake_session(conn, s, now):
    if s["tool"] != "claude":
        return False, "只支持 cmux 里的 Claude 会话"
    sid = s["session_id"]
    if cmux("workspace", "list").returncode != 0:
        run(["open", "-a", "cmux"])
        time.sleep(10)
    loc = None
    if s["workspace"] and s["surface"] and surface_checkpoint(s["workspace"], s["surface"]) == sid:
        loc = (s["workspace"], s["surface"])
    if not loc:
        _surface_cache["at"] = 0
        surfaces = cmux_surfaces()
        if surfaces is None:
            return False, CMUX_DENIED
        loc = surfaces.get(sid)
    if not loc:
        row = conn.execute("SELECT data FROM sessions WHERE key=?", (s["session_key"],)).fetchone()
        st = json.loads(row["data"]) if row else {"id": sid, "cwd": s["cwd"], "tool": "claude"}
        if resume_in_new_workspace(st, "定时唤醒") is None:
            return False, "会话已关闭，且没能在 cmux 里重新打开"
        for _ in range(10):
            time.sleep(3)
            _surface_cache["at"] = 0
            loc = (cmux_surfaces() or {}).get(sid)
            if loc:
                time.sleep(5)
                break
        if not loc:
            return False, "重新打开了会话，但没找到它的窗口"
    r = cmux("send", "--workspace", loc[0], "--surface", loc[1], wake_text(s, now) + "\\n")
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "cmux 发送失败").strip()[:200]
    return True, ""


# ---------------------------------------------------------------- 通知与飞书汇总

NOTIFY_FILE = os.path.join(DATA_DIR, "notify.txt")


def notifier_app():
    apps = glob.glob(os.path.join(BASE, "notifier", "*.app"))
    return apps[0] if apps else None


def mac_notify(title, body):
    """经 notifier 小程序弹通知：点通知会打开看板。直接用 osascript 弹的通知，点了会打开「脚本编辑器」。"""
    title, body = re.sub(r"\s+", " ", title)[:80], re.sub(r"\s+", " ", body)[:200]
    app = notifier_app()
    if app:
        with open(NOTIFY_FILE, "w", encoding="utf-8") as f:
            f.write(title + "\n" + body)
        if run(["open", "-g", app]).returncode == 0:
            return
    def q(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    run(["osascript", "-e", "display notification %s with title %s sound name \"Glass\"" % (q(body), q(title))])


def md_safe(s):
    return (s or "").replace("<", "＜").replace(">", "＞").replace("*", "＊")


def build_digest(board, sample=False):
    g = {x["id"]: x["items"] for x in board["groups"]}
    now = board["now"]
    today_end = clock(now).replace(hour=23, minute=59, second=59).timestamp()
    today_time = [it for it in g["time"] if it["sort"] and it["sort"] <= today_end]
    peers = [it for it in g["peer"] if it["idle"] > 86400]

    def lines(items, limit=8, ago=True):
        out = []
        for it in items[:limit]:
            tail = "%s · %s" % (it["project"], it["toolLabel"]) + (" · 已等 %s" % fmt_ago(it["idle"]) if ago else "")
            out.append("- **%s**　%s\n  <font color='grey'>%s</font>" % (md_safe(it["title"]), md_safe(it["headline"]), tail))
        if len(items) > limit:
            out.append("<font color='grey'>还有 %d 个，见看板</font>" % (len(items) - limit))
        return "\n".join(out)

    elements = []

    def section(title, items, **kw):
        if items:
            if elements:
                elements.append({"tag": "hr"})
            elements.append({"tag": "markdown", "content": "**%s（%d）**\n%s" % (title, len(items), lines(items, **kw))})

    section("🔔 需要你处理", g["need"])
    section("⏰ 今天的时间点", today_time, ago=False)
    section("✅ 待你确认完成", g["confirm"], ago=False)
    section("👥 等同事超过 1 天", peers)
    if g["ai"]:
        if elements:
            elements.append({"tag": "hr"})
        names = "、".join(md_safe(it["title"]) for it in g["ai"][:6])
        elements.append({"tag": "markdown", "content": "**🏃 AI 在做（%d）**\n<font color='grey'>%s。不用管，跑完会进「需要你」。</font>" % (len(g["ai"]), names)})
    if g["backlog"]:
        if elements:
            elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": "**🗂 待整理（%d）**\n<font color='grey'>状态不明或 2 天没动静的旧会话，空了在看板上扫一眼，没用的点「完成」。</font>" % len(g["backlog"])})
    if not elements:
        elements.append({"tag": "markdown", "content": "今天没有需要你处理的事 🎉"})
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": "<font color='grey'>看板（在 Mac 上打开）：http://127.0.0.1:%s　·　每天 %s 发一次</font>" % (
        load_config()["port"], board["digestTime"])})
    d = clock(now)
    sub = "需要你 %d · 今天到点 %d · 待确认 %d · 等同事 %d · AI 在做 %d" % (
        len(g["need"]), len(today_time), len(g["confirm"]), len(g["peer"]), len(g["ai"]))
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "%sAI 任务台账 · %d月%d日 周%s" % ("【示意】" if sample else "", d.month, d.day, WEEK[d.weekday()])},
            "subtitle": {"tag": "plain_text", "content": sub},
            "template": "orange" if g["need"] else "blue",
        },
        "body": {"elements": elements},
    }


def send_digest(conn, cfg, sample=False):
    if not cfg.get("feishu_open_id"):
        return False, "config.json 里没有 feishu_open_id"
    card = build_digest(build_board(conn, cfg), sample=sample)
    key = "ledger-%s-%s" % ("sample" if sample else "digest", clock().strftime("%Y%m%d%H%M" if sample else "%Y%m%d"))
    r = run([LARK, "im", "+messages-send", "--as", cfg.get("feishu_as", "bot"), "--user-id", cfg["feishu_open_id"],
             "--msg-type", "interactive", "--content", json.dumps(card, ensure_ascii=False), "--idempotency-key", key], timeout=40)
    ok = r.returncode == 0 and '"ok":false' not in (r.stdout or "").replace(" ", "")
    if not ok:
        log("飞书汇总发送失败：%s %s" % (r.stdout[-400:], r.stderr[-400:]))
        return False, (r.stderr or r.stdout).strip()[-300:]
    return True, "已发送"


# ---------------------------------------------------------------- 常驻服务

class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = open_db()
        self.lock = threading.RLock()
        self.token = load_token()
        self.event = threading.Event()

    def maybe_scan(self, min_age):
        with self.lock:
            last = float(meta_get(self.conn, "last_scan", 0) or 0)
            if time.time() - last >= min_age:
                stats = scan(self.conn, self.cfg)
                if stats["parsed"]:
                    log("扫描：%(files)d 个文件，解析 %(parsed)d 个，读 %(bytes)d 字节，用时 %(ms)d ms" % stats)

    def board(self):
        with self.lock:
            return build_board(self.conn, self.cfg)

    def action(self, body):
        key, action, arg = body.get("key", ""), body.get("action", ""), body.get("arg")
        now = time.time()
        with self.lock:
            c = self.conn
            if action in ("done", "untrack"):
                keys = arg if (action == "untrack" and isinstance(arg, list)) else [key]
                for k in keys:
                    c.execute("INSERT OR REPLACE INTO overrides (key, action, until_ts, at) VALUES (?, ?, NULL, ?)", (k, action, now))
                c.commit()
                return True, ("已标为完成" if action == "done" else "已不再跟踪 %d 个" % len(keys) if len(keys) > 1 else "已不再跟踪")
            if action == "snooze":
                until = snooze_until(arg, now)
                c.execute("INSERT OR REPLACE INTO overrides (key, action, until_ts, at) VALUES (?, 'snooze', ?, ?)", (key, until, now))
                c.commit()
                return True, "已推迟到 " + fmt_time(until, now)
            if action == "tag" and isinstance(arg, dict) and arg.get("project"):
                label = re.sub(r"\s+", " ", str(arg.get("label") or "")).strip()[:24]
                color = str(arg.get("color") or "")
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    color = ""
                if not label and not color:
                    c.execute("DELETE FROM tags WHERE project=?", (arg["project"],))
                else:
                    c.execute("INSERT OR REPLACE INTO tags (project, label, color) VALUES (?, ?, ?)", (arg["project"], label, color))
                c.commit()
                return True, "标签已更新"
            if action == "restore":
                c.execute("DELETE FROM overrides WHERE key=?", (key,))
                c.commit()
                return True, "已恢复跟踪"
            if action == "cancel_schedule":
                c.execute("UPDATE schedules SET status='cancelled', done_at=? WHERE session_key=? AND status='pending'", (now, key))
                c.commit()
                return True, "已取消定时唤醒"
        if action == "open":
            with self.lock:
                return open_session(self.conn, key)
        return False, "未知操作"

    def tick(self):
        now = time.time()
        self.maybe_scan(self.cfg["scan_interval_min"] * 60)
        self.run_schedules(now)
        self.notify_due(now)
        self.maybe_digest(now)

    def run_schedules(self, now):
        with self.lock:
            due = self.conn.execute("SELECT * FROM schedules WHERE status='pending' AND at <= ?", (now,)).fetchall()
        for s in due:
            if now > s["expire"]:
                status, err = "missed", "到点时 Mac 在睡眠或服务没运行，已晚 %s，没有执行" % fmt_ago(now - s["at"])
            else:
                ok, err = wake_session(self.conn, s, now)
                status = "sent" if ok else ("failed" if s["attempts"] + 1 >= 15 else "pending")
            with self.lock:
                self.conn.execute("UPDATE schedules SET status=?, attempts=attempts+1, last_error=?, done_at=? WHERE id=?",
                                  (status, err, now if status != "pending" else None, s["id"]))
                self.conn.commit()
            log("定时唤醒 #%d → %s %s" % (s["id"], status, err or ""))

    def notify_due(self, now):
        board = self.board()
        due = [it for it in board["groups"][0]["items"] if it["kind"] == "time"]
        fresh = []
        with self.lock:
            for it in due:
                nk = "%s|%s" % (it["key"], it["statusKey"])
                if not self.conn.execute("SELECT 1 FROM notified WHERE key=?", (nk,)).fetchone():
                    self.conn.execute("INSERT OR REPLACE INTO notified (key, at) VALUES (?, ?)", (nk, now))
                    fresh.append(it)
            self.conn.commit()
        if len(fresh) == 1:
            mac_notify("⏰ 到点了：" + fresh[0]["title"], fresh[0]["headline"])
        elif fresh:
            mac_notify("⏰ %d 件事到点了" % len(fresh), "；".join(it["title"] for it in fresh))

    def maybe_digest(self, now):
        if not self.cfg.get("feishu_open_id"):
            return   # 没配飞书就不发汇总
        today = clock(now).strftime("%Y-%m-%d")
        hh, mm = [int(x) for x in self.cfg["digest_time"].split(":")]
        due_at = clock(now).replace(hour=hh, minute=mm, second=0).timestamp()
        with self.lock:
            if now < due_at or meta_get(self.conn, "digest_last_date") == today:
                return
            if now - float(meta_get(self.conn, "digest_last_try", 0) or 0) < 1800:
                return
            meta_set(self.conn, "digest_last_try", now)
            self.conn.commit()
            self.maybe_scan(60)
            ok, msg = send_digest(self.conn, self.cfg)
            if ok:
                meta_set(self.conn, "digest_last_date", today)
                self.conn.commit()
        log("每日汇总：%s" % msg)

    def loop(self):
        while True:
            try:
                self.tick()
            except Exception:
                log("tick 出错：" + traceback.format_exc())
            # 最多睡 60 秒再看一眼时钟：Mac 睡眠时单调时钟不走，长时间等待会醒得太晚
            self.event.wait(60)
            self.event.clear()


def snooze_until(opt, now):
    d = clock(now)
    if opt == "15m":
        return now + 900
    if opt == "1h":
        return now + 3600
    if opt == "3h":
        return now + 3 * 3600
    if opt == "tonight":
        t = d.replace(hour=21, minute=0, second=0, microsecond=0)
        return (t if t.timestamp() > now else t + dt.timedelta(days=1)).timestamp()
    if opt == "monday":
        days = (7 - d.weekday()) % 7 or 7
        return (d.replace(hour=9, minute=0, second=0, microsecond=0) + dt.timedelta(days=days)).timestamp()
    return (d.replace(hour=9, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)).timestamp()


APP = None


class Handler(BaseHTTPRequestHandler):
    server_version = "ledger"

    def log_message(self, *args):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        origin = self.headers.get("Origin")
        port = str(APP.cfg["port"])
        if origin and origin not in ("http://127.0.0.1:" + port, "http://localhost:" + port):
            return False
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, '{"error":"forbidden"}')
        u = urlparse(self.path)
        if u.path == "/":
            with open(BOARD_HTML, encoding="utf-8") as f:
                html = f.read().replace("__LEDGER_TOKEN__", APP.token)
            return self._send(200, html, "text/html; charset=utf-8")
        if u.path == "/favicon.png":
            with open(os.path.join(BASE, "favicon.png"), "rb") as f:
                return self._send(200, f.read(), "image/png")
        if u.path.startswith("/img/"):
            name = u.path[5:]
            if not re.fullmatch(r"[0-9a-f]{40}\.(png|jpg|webp|gif)", name):
                return self._send(404, '{"error":"not found"}')
            try:
                with open(os.path.join(IMG_DIR, name[:2], name), "rb") as f:
                    data = f.read()
            except OSError:
                return self._send(404, '{"error":"not found"}')
            ext = name.rsplit(".", 1)[1]
            return self._send(200, data, "image/" + ("jpeg" if ext == "jpg" else ext))
        if u.path == "/api/search":
            q = parse_qs(u.query)
            with APP.lock:
                res = search_prompts(APP.conn, (q.get("q") or [""])[0], 100, int((q.get("offset") or ["0"])[0]))
            return self._send(200, json.dumps(res, ensure_ascii=False))
        if u.path == "/api/sessions":
            q = parse_qs(u.query)
            with APP.lock:
                res = search_sessions(APP.conn, (q.get("q") or [""])[0], 50, int((q.get("offset") or ["0"])[0]))
            return self._send(200, json.dumps(res, ensure_ascii=False))
        if u.path == "/api/session":
            q = parse_qs(u.query)
            with APP.lock:
                res = session_prompts(APP.conn, (q.get("key") or [""])[0])
            return self._send(200, json.dumps(res, ensure_ascii=False))
        if u.path == "/api/state":
            q = parse_qs(u.query)
            if q.get("refresh"):
                APP.maybe_scan(0 if q["refresh"][0] == "force" else 60)
            return self._send(200, json.dumps(APP.board(), ensure_ascii=False))
        return self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Ledger-Token") != APP.token:
            return self._send(403, '{"error":"forbidden"}')
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, '{"error":"bad json"}')
        u = urlparse(self.path)
        if u.path == "/api/action":
            ok, msg = APP.action(body)
            return self._send(200, json.dumps({"ok": ok, "message": msg}, ensure_ascii=False))
        if u.path == "/api/poke":
            APP.event.set()
            return self._send(200, '{"ok":true}')
        if u.path == "/api/selftest":
            r = run([LARK, "contact", "+get-user", "--as", "user"], timeout=30)
            c = cmux("workspace", "list")
            return self._send(200, json.dumps({"lark": r.returncode == 0, "cmux": c.returncode == 0,
                                               "python": sys.version.split()[0]}, ensure_ascii=False))
        return self._send(404, '{"error":"not found"}')


def migrate_tz(conn):
    """早期版本一律按本机时区解析状态行时间；配置了 utc_offset_hours 后，把已存的时间按它重算一次。"""
    if meta_get(conn, "tz_bj_v1"):
        return
    def fix(ts):
        return dt.datetime.fromtimestamp(ts).replace(tzinfo=WORK_TZ).timestamp() if ts else ts
    for r in conn.execute("SELECT key, data FROM sessions").fetchall():
        st = json.loads(r["data"])
        s = st.get("status")
        if s and s.get("at"):
            s["at"] = fix(s["at"])
            conn.execute("UPDATE sessions SET data=? WHERE key=?", (json.dumps(st, ensure_ascii=False), r["key"]))
    for r in conn.execute("SELECT id, at, expire FROM schedules WHERE status='pending'").fetchall():
        conn.execute("UPDATE schedules SET at=?, expire=? WHERE id=?", (fix(r["at"]), fix(r["expire"]), r["id"]))
    meta_set(conn, "tz_bj_v1", time.time())
    conn.commit()
    log("已把状态行时间改按配置的时区计算")


def backfill_media():
    """第一次运行时回填：Claude 对话记录里你发过的图和 AI 给过的链接、Codex 记录里你发过的图。"""
    conn = open_db()
    if not meta_get(conn, "links_codex_ms"):
        # Codex 的 thread_history 有 3GB 且回复没有索引，只在第一次全量读一遍；之后从对话记录增量收
        sync_codex_links(conn)
        conn.commit()
    if meta_get(conn, "media_backfill_v1"):
        return
    t0, n_files = time.time(), 0
    for path in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
        key = "claude:" + session_id_from_path("claude", path)
        st = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    is_user = '"type":"user"' in line and '"type":"image"' in line
                    is_ai = '"type":"assistant"' in line and "http" in line
                    if not (is_user or is_ai):
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("isSidechain") or rec.get("isMeta"):
                        continue
                    ts = parse_iso(rec.get("timestamp")) or 0
                    content = (rec.get("message") or {}).get("content")
                    if not isinstance(content, list):
                        continue
                    if rec.get("type") == "user" and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                        note_images(st, ts, [((b.get("source") or {}).get("data"), (b.get("source") or {}).get("media_type"))
                                             for b in content if isinstance(b, dict) and b.get("type") == "image"
                                             and (b.get("source") or {}).get("type") == "base64"])
                    elif rec.get("type") == "assistant":
                        note_links(st, ts, take_text(content))
        except OSError:
            continue
        flush_media(conn, key, st)
        conn.commit()
        n_files += 1
    # Codex 的对话记录很大（含截图），只读含图片的文件，且只看你自己发的消息
    threads, autos = codex_threads(), automation_threads()
    r = run(["grep", "-l", "-r", "--include=*.jsonl", '"input_image"', os.path.join(CODEX_HOME, "sessions")], timeout=900)
    for path in (r.stdout or "").splitlines():
        tid = session_id_from_path("codex", path)
        if threads.get(tid, ("",))[0] not in ("vscode", "cli") or tid in autos:
            continue
        st = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"input_image"' not in line or '"role":"user"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    p = rec.get("payload") or {}
                    if rec.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                        note_images(st, parse_iso(rec.get("timestamp")) or 0, data_url_pairs(p.get("content") or []))
        except OSError:
            continue
        flush_media(conn, "codex:" + tid, st)
        conn.commit()
        n_files += 1
    meta_set(conn, "media_backfill_v1", time.time())
    conn.commit()
    n_img, size = conn.execute("SELECT COUNT(DISTINCT sha), COALESCE(SUM(bytes), 0) FROM images").fetchone()
    log("回填完成：%d 个文件，图片 %d 张（约 %d MB），用时 %d 秒" % (n_files, n_img, size // 1048576, time.time() - t0))


def cmd_serve(args):
    global APP
    os.umask(0o077)   # 服务新建的数据、图片、日志都只给自己的账号读
    os.makedirs(LOG_DIR, exist_ok=True)
    os.chmod(LOG_DIR, 0o700)
    cfg = load_config()
    APP = App(cfg)
    migrate_tz(APP.conn)
    with open(os.path.join(DATA_DIR, "board_url"), "w", encoding="utf-8") as f:
        f.write("http://127.0.0.1:%s" % cfg["port"])   # 通知小程序点开时读它
    # 先占端口再启动定时线程：端口被占说明已有一个实例在跑，直接退出，避免定时唤醒被发两次
    srv = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), Handler)
    APP.maybe_scan(0)
    threading.Thread(target=APP.loop, daemon=True).start()
    threading.Thread(target=lambda: _guard(backfill_media), daemon=True).start()
    log("服务启动，端口 %s" % cfg["port"])
    srv.serve_forever()


def _guard(fn):
    try:
        fn()
    except Exception:
        log("%s 出错：%s" % (fn.__name__, traceback.format_exc()))


# ---------------------------------------------------------------- 安装辅助：状态行规则、体检、飞书和 cmux 设置
# 给 INSTALL.md 里的安装步骤用。doctor 只读：不建 data/、不写日志、不改任何配置。

RULES_FILE = os.path.join(BASE, "status-line.md")
RULES_START = "<!-- codex-inbox:status-line:start -->"
RULES_END = "<!-- codex-inbox:status-line:end -->"
HANDWRITTEN_RULE = "📌 <状态>"   # 没有标记但有这句，说明用户自己手写过规则，不再重复加
CMUX_CONFIG = os.path.join(HOME, ".config", "cmux", "cmux.json")
BLOCKED_DIRS = {"Desktop": "桌面", "Documents": "文稿", "Downloads": "下载"}


def home_rel(path):
    """给人看、也能直接粘贴进终端的路径：家目录写成 ~，有空格等特殊字符时加引号。"""
    p = "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + "/") else path
    return p if re.match(r"^[\w./~+@%-]+$", p) else shell_quote(path)


def ledger_cmd(*args):
    return " ".join([home_rel(os.path.join(BASE, "ledger"))] + list(args))


def rule_targets():
    """[(工具名, 规则文件)]：工具的目录在（装过）才往里加。"""
    return [("Codex", os.path.join(CODEX_HOME, "AGENTS.md")), ("Claude Code", os.path.join(HOME, ".claude", "CLAUDE.md"))]


def rules_block():
    """status-line.md 去掉开头的说明，把 {{LEDGER}} 换成本机 ledger 的完整路径，再用标记包起来。"""
    with open(RULES_FILE, encoding="utf-8") as f:
        text = re.sub(r"\A\s*<!--.*?-->\s*", "", f.read(), flags=re.S).strip()
    ledger = os.path.join(BASE, "ledger")
    text = text.replace("{{LEDGER}}", ledger if re.match(r"^[\w./+@%-]+$", ledger) else shell_quote(ledger))
    return "%s\n%s\n%s" % (RULES_START, text, RULES_END)


def read_rules_file(path):
    """返回 (状态, 内容)。状态：missing 没有文件 / marked 有成对标记 / broken 标记不成对 / handwritten 手写过 / none 没有规则。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return "missing", ""
    s = text.find(RULES_START)
    if s >= 0 and text.find(RULES_END, s) > s:
        return "marked", text
    if s >= 0 or RULES_END in text:
        return "broken", text
    return ("handwritten" if HANDWRITTEN_RULE in text else "none"), text


def splice_rules(text, block):
    """把标记之间（含标记）换成 block；block 为空就删掉，并收拾多出来的空行。"""
    s = text.find(RULES_START)
    e = text.find(RULES_END, s) + len(RULES_END)
    if block:
        return text[:s] + block + text[e:]
    before, after = text[:s].rstrip("\n"), text[e:].strip("\n")
    return "\n\n".join(x for x in (before, after) if x) + ("\n" if before or after else "")


def rules_status(path, block):
    """(代号, 给人看的一句话)，doctor 和 rules show 共用。"""
    state, text = read_rules_file(path)
    if state == "marked":
        s = text.find(RULES_START)
        if text[s:text.find(RULES_END, s) + len(RULES_END)] != block:
            return "old", "已加，但和 status-line.md 不一致（代码更新过）"
        if HANDWRITTEN_RULE in splice_rules(text, ""):
            return "dup", "已加，但文件里另有一段手写的规则，两段重复了"
        return "ok", "已加（codex-inbox 标记）"
    return {"handwritten": ("hand", "有你手写的状态行规则"), "broken": ("broken", "codex-inbox 标记不成对"),
            "missing": ("none", "还没加（文件还不存在）"), "none": ("none", "还没加")}[state]


def write_text(path, text):
    # 原地写：规则文件可能是指向别处的软链接，不能换成新文件
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def apply_rules(path, action, block):
    """对一个文件执行 install / remove，返回做了什么。"""
    state, text = read_rules_file(path)
    if state == "broken":
        return "跳过：codex-inbox 标记不成对（只找到开始或结束那一行），请手动删掉残缺的标记再运行"
    if action == "remove":
        if state != "marked":
            return "没有 codex-inbox 加的规则，不用删" + ("（你手写的规则没动）" if state == "handwritten" else "")
        write_text(path, splice_rules(text, ""))
        return "已删掉标记之间的规则，文件其他内容没动"
    if state == "handwritten":
        return "跳过：文件里已经有你手写的状态行规则（含「%s」）。想换成自动维护的版本，先删掉手写的那段再运行" % HANDWRITTEN_RULE
    if state == "marked":
        new = splice_rules(text, block)
        if new == text:
            return "已是最新，没改"
        write_text(path, new)
        return "已原地更新标记之间的规则"
    write_text(path, (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n")
    return "已新建文件并加上规则" if state == "missing" else "已在文件末尾加上规则"


def board_up(port):
    try:
        urlopen("http://127.0.0.1:%s/" % port, timeout=3).read(64)
        return True
    except OSError:
        return False


def ledger_services():
    """~/Library/LaunchAgents 里跑 ledger.py serve 的服务：[(label, ledger.py 所在目录)]。"""
    out = []
    for f in sorted(glob.glob(os.path.join(HOME, "Library", "LaunchAgents", "*.plist"))):
        try:
            with open(f, "rb") as fp:
                pl = plistlib.load(fp)
            args, label = pl.get("ProgramArguments") or [], pl.get("Label") or os.path.basename(f)[:-6]
        except Exception:
            continue
        for a in args:
            if isinstance(a, str) and a.endswith("/ledger.py") and "serve" in args:
                out.append((label, os.path.realpath(os.path.dirname(a))))
    return out


def read_token():
    """只读已有的令牌，不像 load_token 那样没有就新建。"""
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def board_selftest(cfg):
    """请本目录的看板服务从开机自启的环境里试调 cmux（和 lark-cli）。服务没在跑、或端口上是别的安装时返回 None。"""
    tok = read_token()
    if not tok:
        return None
    try:
        req = Request("http://127.0.0.1:%s/api/selftest" % cfg["port"], data=b"{}", method="POST",
                      headers={"X-Ledger-Token": tok, "Content-Type": "application/json"})
        return json.loads(urlopen(req, timeout=60).read())
    except (OSError, ValueError):
        return None


def cmux_mode():
    """cmux 实际在用的套接字控制模式（cmux.json 里的，没有就是设置里的或默认值）；读不到返回空串。"""
    r = cmux("config", "get", "automation.socketControlMode")
    m = re.search(r'=\s*"?([\w-]+)', r.stdout or "")
    return m.group(1) if r.returncode == 0 and m else ""


def lark_status():
    """飞书这边走到哪一步：(代号, 当前登录的人 {userName, openId}, 下一步)。
    代号：missing 没装 / unconfigured 没建应用 / login 没登录 / error 出错 / ready 可以发。"""
    if not (os.path.isabs(LARK) and os.access(LARK, os.X_OK)):
        npm = shutil.which("npm", path=SUB_ENV["PATH"])
        return "missing", {}, "npm install -g @larksuite/cli" + ("" if npm else "（要先装 Node.js：brew install node，或到 nodejs.org 下载安装包）")
    r = run([LARK, "whoami"], timeout=30)
    who = None
    for out in (r.stdout or "", r.stderr or ""):   # 出错时 JSON 写在 stderr 里
        try:
            who = json.loads(out[out.index("{"):])
            break
        except ValueError:
            continue
    if not isinstance(who, dict):
        return "error", {}, "lark-cli whoami 没输出 JSON（%s）：运行 lark-cli doctor 看看" % (r.stderr or r.stdout or "").strip()[:100]
    err = who.get("error") or {}
    if who.get("ok") is False or err:
        if err.get("subtype") == "not_configured":
            return "unconfigured", {}, "lark-cli config init --new（会一直等你在浏览器里建好应用；AI 代理请放到后台跑，从输出里取链接给用户）"
        return "error", {}, "lark-cli 报错：%s。%s" % (err.get("message") or "未知错误", err.get("hint") or "运行 lark-cli doctor 看看")
    me = who.get("onBehalfOf") or {}
    if not me.get("openId"):
        return "login", {}, "lark-cli auth login（在浏览器里授权；AI 代理可以加 --no-wait --json 先拿到授权链接）"
    return "ready", me, ""


def read_config_file():
    """config.json 原样读出（不合并默认值），改完写回时保留用户的其他配置；没有文件返回 {}，不是合法 JSON 抛 ValueError。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("config.json 最外层不是 {}")
    return data


def utc_text(h):
    return "UTC%+g" % h


def doctor_items():
    """逐项体检。level：必做 / 可选 / 提示；state：ok / fail / off（可选，没启用）/ warn（提示，看情况）。"""
    items = []

    def add(level, state, name, text, nxt="", note=""):
        items.append({"level": level, "state": state, "name": name, "text": text, "next": nxt, "note": note})

    cfg = load_config()
    try:
        read_config_file()
    except ValueError:
        add("必做", "fail", "config.json", "不是合法 JSON，里面的设置都没生效",
            "对照 config.example.json 修好 %s，再运行 %s" % (home_rel(CONFIG_PATH), home_rel(os.path.join(BASE, "install.sh"))))

    # 安装目录：后台服务读不了桌面、文稿、下载
    bad = next((zh for d, zh in BLOCKED_DIRS.items()
                if BASE == os.path.join(HOME, d) or BASE.startswith(os.path.join(HOME, d) + "/")), None)
    if bad:
        target = os.path.join(HOME, "codex-inbox")
        if os.path.exists(os.path.join(target, "ledger.py")):
            nxt = "~/codex-inbox 里已经有一份：以后都用那一份，运行 ~/codex-inbox/install.sh，再运行 ~/codex-inbox/ledger doctor"
        elif os.path.exists(target):
            nxt = "~/codex-inbox 被别的东西占了，换个目录名：cp -R %s ~/codex-inbox-app && ~/codex-inbox-app/install.sh" % home_rel(BASE)
        else:
            nxt = "cp -R %s ~/codex-inbox && ~/codex-inbox/install.sh（之后都用 ~/codex-inbox 里的那一份）" % home_rel(BASE)
        add("必做", "fail", "安装目录", "在「%s」里，macOS 不让后台服务读这里" % bad, nxt)
    else:
        add("必做", "ok", "安装目录", home_rel(BASE))

    # 看板服务：端口通不通，launchd 里是不是这个目录装的
    port = cfg["port"]
    up = board_up(port)
    svcs = ledger_services()
    mine = [label for label, d in svcs if d == BASE]
    others = [d for label, d in svcs if d != BASE]
    install = "运行 " + home_rel(os.path.join(BASE, "install.sh"))
    if bad:
        install = "先按上一项换到别的目录，再运行那边的 install.sh"
    if up and mine:
        add("必做", "ok", "看板服务", "在跑：http://127.0.0.1:%s（launchd 服务 %s）" % (port, mine[0]))
    elif up and others:
        add("必做", "fail", "看板服务", "端口 %s 上跑的是另一份安装：%s" % (port, home_rel(others[0])),
            "要用那一份：运行 %s/ledger doctor。要换成这一份：%s（会替换掉那个服务，那边 data/ 里的记录不会搬过来）"
            % (home_rel(others[0]), install))
    elif up:
        add("必做", "fail", "看板服务", "端口 %s 有程序在用，但 launchd 里没有指向这个目录的服务（可能是手动运行的 ledger serve 或别的程序）" % port,
            "%s，装成开机自启；端口被别的程序占了就在 config.json 里换个 port 再装" % install)
    elif mine:
        add("必做", "fail", "看板服务", "launchd 里有服务 %s，但端口 %s 不通" % (mine[0], port),
            "看 %s 最后几行，再%s重启" % (home_rel(os.path.join(LOG_DIR, "launchd.err")), install))
    else:
        add("必做", "fail", "看板服务", "没装（launchd 里没有指向这个目录的服务）", install)

    # 状态行规则
    try:
        block = rules_block()
    except OSError:
        block = None
        add("必做", "fail", "状态行规则", "找不到 %s" % home_rel(RULES_FILE), "重新下载完整的 codex-inbox")
    seen = 0
    for tool, path in (rule_targets() if block else []):
        name = "%s 的状态行规则" % tool
        if not os.path.isdir(os.path.dirname(path)):
            add("可选", "off", name, "没装 %s（没有 %s），不用加" % (tool, home_rel(os.path.dirname(path))))
            continue
        seen += 1
        code, text = rules_status(path, block)
        where = "%s：%s" % (home_rel(path), text)
        if code in ("ok", "hand"):
            add("必做", "ok", name, where)
        elif code == "dup":
            add("必做", "fail", name, where, "打开 %s 删掉 codex-inbox 标记之外手写的那段；想只留手写的，就运行 %s" % (home_rel(path), ledger_cmd("rules", "remove")))
        elif code == "broken":
            add("必做", "fail", name, where, "打开 %s 删掉残缺的 codex-inbox 标记，再运行 %s" % (home_rel(path), ledger_cmd("rules", "install")))
        else:
            add("必做", "fail", name, where, "运行 " + ledger_cmd("rules", "install"))
    if block and not seen:
        add("必做", "fail", "Codex / Claude Code", "没找到 ~/.codex 和 ~/.claude，看板没有会话可读", "先装 Codex 或 Claude Code 并至少用一次")

    # 通知小程序
    app = notifier_app()
    perm = "通知权限没法自动检测：没收到通知时，去「系统设置 → 通知 → AI 任务台账」打开「允许通知」"
    if app:
        add("必做", "ok", "通知小程序", "已建好（%s）" % home_rel(app), note=perm)
    else:
        add("必做", "fail", "通知小程序", "没建好：通知会改用系统脚本弹出，点了会打开「脚本编辑器」",
            "%s（会重建它）；还不行就看 %s" % (install, home_rel(os.path.join(LOG_DIR, "notifier-build.log"))))

    # 飞书每日汇总（可选）
    want = cfg.get("feishu_open_id") or ""
    code, me, nxt = lark_status()
    name = "飞书每日汇总"
    if code == "missing":
        add("可选", "fail" if want else "off", name, "没装 lark-cli（飞书命令行工具）", "要用的话：" + nxt + "，再运行 " + ledger_cmd("feishu-setup"))
    elif code != "ready":
        text = {"unconfigured": "lark-cli 还没建飞书应用", "login": "lark-cli 还没登录你的飞书账号"}.get(code, "lark-cli 出错了")
        add("可选", "fail" if want else "off", name, text, nxt)
    elif not want:
        add("可选", "off", name, "lark-cli 已登录（%s），但 config.json 里没有 feishu_open_id" % (me.get("userName") or "?"),
            "要用的话：运行 " + ledger_cmd("feishu-setup"))
    elif want != me["openId"]:
        add("可选", "fail", name, "config.json 的 feishu_open_id 和 lark-cli 当前登录的人（%s）不一样" % (me.get("userName") or "?"),
            "运行 %s 改成当前登录的人" % ledger_cmd("feishu-setup"))
    else:
        add("可选", "ok", name, "每天 %s 用%s身份私信给 %s" % (cfg["digest_time"], "机器人" if cfg.get("feishu_as", "bot") == "bot" else "你自己的",
                                                     me.get("userName") or "你"),
            note="改过 config.json 后要运行 install.sh 重启服务才生效")

    # cmux 定时唤醒（可选）
    name = "cmux 定时唤醒"
    setup = "要用的话：先看安全说明，再运行 " + ledger_cmd("cmux-setup")
    if not os.path.exists(CMUX):
        add("可选", "off", name, "没装 cmux：定时唤醒和「打开会话」跳到 cmux 用不了，其他功能不受影响")
    else:
        mode = cmux_mode()
        test = board_selftest(cfg) if up and mine else None
        if test is not None and test.get("cmux"):
            add("可选", "ok", name, "看板服务能控制 cmux（%s 模式）" % (mode or "?"))
        elif test is not None and mode == "password":
            add("可选", "fail", name, "已是密码模式，但看板服务控制不了 cmux",
                "确认 cmux 开着，运行 %s reload-config，再运行 %s" % (home_rel(CMUX), ledger_cmd("doctor")))
        elif mode == "password":
            add("可选", "ok", name, "cmux 已是密码模式（看板服务没在跑或不是这份安装，没法实测）")
        else:
            add("可选", "off", name, "cmux 是 %s 模式，开机自启的看板服务控制不了它" % (mode or "默认的 cmuxOnly"), setup)

    # 时区（提示）
    local = dt.datetime.now().astimezone().utcoffset().total_seconds() / 3600
    h = cfg.get("utc_offset_hours")
    if h is None:
        add("提示", "ok", "时区", "看板按本机时区（%s）算时间" % utc_text(local))
    elif not isinstance(h, (int, float)) or isinstance(h, bool):
        add("提示", "warn", "时区", "config.json 的 utc_offset_hours 不是数字，被忽略了，看板按本机时区（%s）算" % utc_text(local),
            "改成数字（如北京时间填 8）或 null，再运行 install.sh")
    elif float(h) == local:
        add("提示", "ok", "时区", "看板按 %s 算时间，和本机一致" % utc_text(h))
    else:
        add("提示", "warn", "时区", "看板按 %s 算时间，本机是 %s" % (utc_text(h), utc_text(local)),
            note="状态行和 ledger schedule 里的时间都按 %s 理解。你确实按它安排工作就不用改；否则把 config.json 的 utc_offset_hours 改成 null，再运行 install.sh" % utc_text(h))

    # 项目目录（提示）
    parents = [p for p in (cfg.get("project_parents") or []) if isinstance(p, str)]
    if not parents:
        add("提示", "off", "项目目录", "project_parents 没填：项目按 ~/Desktop/<项目>、~/<项目> 推断",
            note="代码放在别处（如 ~/code/<项目>）时，把父目录填进 config.json 的 project_parents，如 [\"~/code\"]，再运行 install.sh")
    else:
        missing = [p for p in parents if not os.path.isdir(os.path.expanduser(p))]
        if missing:
            add("提示", "warn", "项目目录", "project_parents 里这些目录不存在：" + "、".join(missing), "改 config.json 的 project_parents，再运行 install.sh")
        else:
            add("提示", "ok", "项目目录", "project_parents：" + "、".join(parents))
    return items


# ---------------------------------------------------------------- 命令行

def poke(cfg):
    try:
        req = Request("http://127.0.0.1:%s/api/poke" % cfg["port"], data=b"{}", method="POST",
                      headers={"X-Ledger-Token": load_token(), "Content-Type": "application/json"})
        urlopen(req, timeout=3).read()
    except OSError:
        pass


def cmd_schedule(args):
    cfg = load_config()
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not UUID_RE.match(sid):
        sys.exit("只能在 Claude Code 会话里运行（找不到 CLAUDE_CODE_SESSION_ID）。Codex 会话请用状态行「等时间点」。")
    now = time.time()
    at = parse_when(args.at, now)
    if not at or not TIME_FULL_RE.search(args.at):
        sys.exit('时间格式要写成 "YYYY-MM-DD HH:MM"（按看板的时区）')
    if at < now + 60:
        sys.exit("时间已经过去或太近了：" + fmt_time(at, now))
    conn = open_db()
    conn.execute(
        "INSERT INTO schedules (session_key, session_id, tool, cwd, workspace, surface, at, expire, prompt, note, status, created) "
        "VALUES (?, ?, 'claude', ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        ("claude:" + sid, sid, os.getcwd(), os.environ.get("CMUX_WORKSPACE_ID", ""), os.environ.get("CMUX_SURFACE_ID", ""),
         at, at + cfg["wake_expire_hours"] * 3600, args.prompt, args.note or "", now))
    conn.commit()
    sid_row = conn.execute("SELECT last_insert_rowid() AS id").fetchone()
    poke(cfg)
    where = "" if os.environ.get("CMUX_SURFACE_ID") else "（当前不在 cmux 里，到点会在 cmux 新开工作区恢复本会话）"
    print("已安排定时唤醒 #%d：%s（%s）%s" % (sid_row["id"], fmt_time(at, now),
                                        clock(at).strftime("%Y-%m-%d %H:%M"), where))
    print("到点会把这段话发回本会话：" + args.prompt)


def cmd_schedules(args):
    conn = open_db()
    rows = conn.execute("SELECT * FROM schedules ORDER BY id DESC LIMIT 20").fetchall()
    for r in rows:
        print("#%d %s %s  %s  %s" % (r["id"], r["status"], clock(r["at"]).strftime("%m-%d %H:%M"),
                                    r["session_id"][:8], (r["note"] or r["prompt"])[:60]))


def cmd_cancel(args):
    conn = open_db()
    conn.execute("UPDATE schedules SET status='cancelled', done_at=? WHERE id=? AND status='pending'", (time.time(), args.id))
    conn.commit()
    print("已取消 #%d" % args.id)


def cmd_scan(args):
    cfg = load_config()
    conn = open_db()
    stats = scan(conn, cfg)
    board = build_board(conn, cfg)
    print("扫描 %(files)d 个文件，解析 %(parsed)d 个，读 %(bytes)d 字节，用时 %(ms)d ms" % stats)
    for g in board["groups"]:
        print("%s：%d" % (g["title"], len(g["items"])))
        if args.verbose:
            for it in g["items"][:12]:
                print("   [%s/%s] %s — %s" % (it["project"], it["toolLabel"], it["title"][:30], it["headline"][:70]))
    print("已完成/不再跟踪：%d" % len(board["hidden"]))


def cmd_digest(args):
    cfg = load_config()
    conn = open_db()
    scan(conn, cfg)
    if not args.send:
        print(json.dumps(build_digest(build_board(conn, cfg), sample=args.sample), ensure_ascii=False, indent=1))
        return
    ok, msg = send_digest(conn, cfg, sample=args.sample)
    if ok and args.sample:
        meta_set(conn, "digest_last_date", clock().strftime("%Y-%m-%d"))
        conn.commit()
    print(msg)
    sys.exit(0 if ok else 1)


def cmd_rules(args):
    if not os.path.exists(RULES_FILE):
        sys.exit("找不到 %s" % home_rel(RULES_FILE))
    block = rules_block()
    if args.action == "show":
        print(block + "\n")
    for tool, path in rule_targets():
        if not os.path.isdir(os.path.dirname(path)):
            print("➖ %s：没有 %s，没装就不加" % (tool, home_rel(os.path.dirname(path))))
        elif args.action == "show":
            print("%s %s：%s" % (tool, home_rel(path), rules_status(path, block)[1]))
        else:
            print("%s %s：%s" % (tool, home_rel(path), apply_rules(path, args.action, block)))
    if args.action == "install":
        print("新开的会话才会按规则写状态行；正在跑的会话要重开才生效。")


def cmd_doctor(args):
    items = doctor_items()
    ok = all(it["state"] == "ok" for it in items if it["level"] == "必做")
    if args.json:
        print(json.dumps({"base": BASE, "board": "http://127.0.0.1:%s" % load_config()["port"], "ok": ok, "items": items},
                         ensure_ascii=False, indent=1))
        sys.exit(0 if ok else 1)
    icons = {"ok": "✅", "fail": "❌", "off": "➖", "warn": "⚠️"}
    print("codex-inbox 体检：%s（只读，不改任何东西）" % home_rel(BASE))
    print("✅ 正常　❌ 要处理　➖ 可选，没启用　⚠️ 提示，看情况")
    for level in ("必做", "可选", "提示"):
        print("\n" + level)
        for it in items:
            if it["level"] != level:
                continue
            print("%s %s：%s" % (icons[it["state"]], it["name"], it["text"]))
            if it["note"]:
                print("   提醒：" + it["note"])
            if it["next"] and it["state"] != "ok":
                print("   下一步：" + it["next"])
    left = sum(1 for it in items if it["level"] == "必做" and it["state"] != "ok")
    print("\n" + ("必做项全部通过。看板：http://127.0.0.1:%s" % load_config()["port"] if ok else "必做项还有 %d 项没通过，按「下一步」做完再运行 %s" % (left, ledger_cmd("doctor"))))
    sys.exit(0 if ok else 1)


def cmd_feishu_setup(args):
    code, me, nxt = lark_status()
    if code != "ready":
        print("❌ " + {"missing": "没装 lark-cli（飞书命令行工具）", "unconfigured": "lark-cli 还没建飞书应用",
                       "login": "lark-cli 还没登录你的飞书账号"}.get(code, "lark-cli 出错了"))
        print("下一步：" + nxt)
        print("做完再运行 " + ledger_cmd("feishu-setup"))
        sys.exit(1)
    name = me.get("userName") or "你"
    print("✅ lark-cli 已登录：%s" % name)
    try:
        raw = read_config_file()
    except ValueError:
        sys.exit("❌ config.json 不是合法 JSON，先对照 config.example.json 修好 %s" % home_rel(CONFIG_PATH))
    cfg = dict(load_config(), feishu_open_id=me["openId"])
    same = raw.get("feishu_open_id") == me["openId"]
    if args.dry_run:
        print("（--dry-run：不写文件，也不发消息）")
        print("会%s config.json 的 feishu_open_id：%s" % ("保持" if same else "写入", me["openId"]))
        print("会用 lark-cli 以 %s 身份发一条【示意】汇总给%s：" % (cfg.get("feishu_as", "bot"), name))
        print("  %s im +messages-send --as %s --user-id %s --msg-type interactive --content <汇总卡片>"
              % (home_rel(LARK), cfg.get("feishu_as", "bot"), me["openId"]))
        return
    if same:
        print("✅ config.json 里已经是这个 open_id")
    else:
        raw["feishu_open_id"] = me["openId"]
        write_text(CONFIG_PATH, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
        print("✅ 已把 feishu_open_id 写进 %s（其他配置没动）" % home_rel(CONFIG_PATH))
    conn = open_db()
    scan(conn, cfg)
    ok, msg = send_digest(conn, cfg, sample=True)
    if not ok:
        print("❌ 测试汇总没发出去：" + msg)
        print("下一步：运行 lark-cli doctor 体检。用机器人发（feishu_as 是 bot）时，飞书应用要开「机器人」能力、"
              "有给用户发消息的权限，且你在应用的可用范围内；改完再运行 " + ledger_cmd("feishu-setup"))
        sys.exit(1)
    meta_set(conn, "digest_last_date", clock().strftime("%Y-%m-%d"))   # 和 digest --send --sample 一样：今天不再补发正式汇总
    conn.commit()
    print("✅ 已发一条【示意】汇总，去飞书看看有没有收到")
    if not same:
        print("下一步：运行 %s 重启看板服务，之后每天 %s 自动发" % (home_rel(os.path.join(BASE, "install.sh")), cfg["digest_time"]))


def cmd_cmux_setup(args):
    print("安全说明：密码模式下，这台 Mac 上知道密码的程序都能控制 cmux（看终端里的内容、往会话里打字）。"
          "密码存在 %s，只有你的账号能读；在 cmux 终端里跑的程序本来就能控制 cmux，不受影响。" % home_rel(CMUX_CONFIG))
    print("改了以后，开机自启的看板服务才能跳到 cmux 里的会话、到点唤醒会话（ledger schedule）。"
          "想改回去：在 cmux 设置 → 自动化，把「套接字控制模式」改回默认。\n")
    if not os.path.exists(CMUX):
        sys.exit("➖ 没装 cmux（找不到 %s），不用设置。用 cmux 跑 Claude Code 才需要这一步。" % CMUX)
    try:
        with open(CMUX_CONFIG, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        raw = None
    except OSError as e:
        sys.exit("❌ 读不了 %s：%s" % (home_rel(CMUX_CONFIG), e))
    data = {}
    if raw and raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
    changed = False
    if not isinstance(data, dict):
        # 带注释的 JSONC：硬改可能弄坏它，只在已经是密码模式时继续往下验证
        if cmux_mode() != "password":
            print("❌ %s 不是严格的 JSON（可能带注释），为了不弄坏它，这里不自动改。任选一种：" % home_rel(CMUX_CONFIG))
            print("  1. 打开 cmux 设置 → 自动化，把「套接字控制模式」改成「密码模式」，并设一个长密码。")
            print('  2. 编辑这个文件，在最外层的 { 后面加一行（已经有 "automation" 就改它，不要加第二个）：')
            print('     "automation": {"socketControlMode": "password", "socketPassword": "<一串长密码，可用 openssl rand -base64 32 生成>"},')
            print("     保存后运行：%s config validate && %s reload-config" % (home_rel(CMUX), home_rel(CMUX)))
            print("改完运行 %s 确认。" % ledger_cmd("doctor"))
            sys.exit(1)
        print("✅ cmux 已经是密码模式（%s 带注释，没去改它）" % home_rel(CMUX_CONFIG))
    else:
        auto = data.get("automation") or {}
        if not isinstance(auto, dict):
            sys.exit("❌ %s 里的 automation 不是对象，请手动检查" % home_rel(CMUX_CONFIG))
        has_pw = bool(auto.get("socketPassword"))
        if auto.get("socketControlMode") == "password" and has_pw:
            print("✅ cmux 已经是密码模式，不用改")
        else:
            print("会把 %s 的 automation.socketControlMode 设为 \"password\"，%s" % (
                home_rel(CMUX_CONFIG), "保留现有的 socketPassword" if has_pw else "并生成一个随机长密码写进 socketPassword（不显示）"))
            if args.dry_run:
                print("（--dry-run：没写文件，也没运行 cmux reload-config）")
                return
            auto = dict(auto, socketControlMode="password")
            if not has_pw:
                auto["socketPassword"] = secrets.token_urlsafe(32)
            data["automation"] = auto
            os.makedirs(os.path.dirname(CMUX_CONFIG), exist_ok=True)
            backup = None
            if raw is not None:
                backup = "%s.bak-%s" % (CMUX_CONFIG, time.strftime("%Y%m%d-%H%M%S"))
                shutil.copy2(CMUX_CONFIG, backup)
                os.chmod(backup, 0o600)
            fd = os.open(CMUX_CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            os.chmod(CMUX_CONFIG, 0o600)   # 里面有密码，只给自己的账号读
            r = cmux("config", "validate", "--path", CMUX_CONFIG)
            if r.returncode != 0:
                if backup:
                    os.replace(backup, CMUX_CONFIG)
                else:
                    os.remove(CMUX_CONFIG)
                sys.exit("❌ cmux 校验没通过，已恢复原文件：\n" + (r.stdout + r.stderr).strip()[-600:])
            changed = True
            print("✅ 已改成密码模式" + ("（原文件备份在 %s）" % home_rel(backup) if backup else "（新建了这个文件）"))
    if args.dry_run:
        print("（--dry-run：没运行 cmux reload-config）")
        return
    if changed:
        r = cmux("reload-config")
        if r.returncode == 0:
            print("✅ cmux reload-config 完成")
        else:
            print("⚠️ cmux reload-config 没成功（cmux 可能没开着，下次打开时会读新设置）：" + (r.stderr or r.stdout).strip()[:200])
    r = cmux("workspace", "list")
    print(("✅ cmux workspace list 通过" if r.returncode == 0 else "❌ cmux workspace list 失败：" + (r.stderr or r.stdout).strip()[:200])
          + "（在 cmux 终端里运行时这一步本来就能过，以下面看板服务的结果为准）")
    test = board_selftest(load_config())
    if test is None:
        print("➖ 看板服务没在跑（或不是这份安装），没法从服务那边实测；装好后运行 %s 看「cmux 定时唤醒」一项" % ledger_cmd("doctor"))
    elif test.get("cmux"):
        print("✅ 看板服务能控制 cmux 了")
    else:
        print("❌ 看板服务还是控制不了 cmux：确认 cmux 开着，再运行 %s" % ledger_cmd("doctor"))
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(prog="ledger", description="AI 任务台账")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="常驻服务（launchd 启动）")
    p = sub.add_parser("scan", help="扫一次并打印概要")
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("schedule", help="在 Claude 会话里安排到点唤醒本会话")
    p.add_argument("--at", required=True, help='"YYYY-MM-DD HH:MM"（按看板的时区）')
    p.add_argument("--prompt", required=True, help="到点后发回本会话的话")
    p.add_argument("--note", help="看板上显示的一句话说明")
    sub.add_parser("schedules", help="列出最近的定时唤醒")
    p = sub.add_parser("cancel", help="取消定时唤醒")
    p.add_argument("id", type=int)
    p = sub.add_parser("digest", help="预览或发送飞书汇总")
    p.add_argument("--send", action="store_true")
    p.add_argument("--sample", action="store_true", help="标为示意，并把今天记为已发")
    p = sub.add_parser("rules", help="把状态行规则加进（或移出）Codex 和 Claude Code 的全局规则文件")
    p.add_argument("action", choices=["install", "remove", "show"])
    p = sub.add_parser("doctor", help="只读体检：逐项检查安装是否完整，并给出下一步")
    p.add_argument("--json", action="store_true", help="输出 JSON，给脚本或 AI 代理读")
    p = sub.add_parser("feishu-setup", help="把 lark-cli 当前登录的人写进 config.json，并发一条测试汇总")
    p.add_argument("--dry-run", action="store_true", help="只打印会做什么，不写文件也不发消息")
    p = sub.add_parser("cmux-setup", help="把 cmux 改成密码模式，让看板服务能控制它（定时唤醒要用）")
    p.add_argument("--dry-run", action="store_true", help="只打印会改什么，不写文件也不重新加载")
    args = ap.parse_args()
    handlers = {"serve": cmd_serve, "scan": cmd_scan, "schedule": cmd_schedule, "schedules": cmd_schedules,
                "cancel": cmd_cancel, "digest": cmd_digest, "rules": cmd_rules, "doctor": cmd_doctor,
                "feishu-setup": cmd_feishu_setup, "cmux-setup": cmd_cmux_setup}
    if args.cmd not in handlers:
        ap.print_help()
        sys.exit(1)
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
