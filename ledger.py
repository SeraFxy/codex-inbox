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
import getpass
import glob
import hashlib
import json
import mmap
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
from urllib.parse import parse_qs, quote as quote_url, unquote as unquote_url, urlparse
from urllib.request import Request, urlopen

HOME = os.path.expanduser("~")
BASE = os.path.dirname(os.path.realpath(__file__))
DATA_DIR = os.path.join(BASE, "data")
LOG_DIR = os.path.join(BASE, "logs")
DB_PATH = os.path.join(DATA_DIR, "ledger.db")
TOKEN_PATH = os.path.join(DATA_DIR, "token")
CONFIG_PATH = os.path.join(BASE, "config.json")
BOARD_HTML = os.path.join(BASE, "board.html")


def board_build():
    """看板页面的版本：deploy.sh 换了 board.html，开着的标签页对不上就自己刷新，不会一直用旧页面"""
    try:
        st = os.stat(BOARD_HTML)
        return "%d-%d" % (st.st_mtime_ns // 1000000, st.st_size)
    except OSError:
        return ""
IMG_DIR = os.path.join(DATA_DIR, "images")
CMUX_STATE = os.path.join(HOME, "Library", "Application Support", "cmux", "session-com.cmuxterm.app.json")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CLAUDE_LIVE = os.path.join(HOME, ".claude", "sessions")
CODEX_HOME = os.path.join(HOME, ".codex")
CODEX_WORKTREES = os.path.join(CODEX_HOME, "worktrees")
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
GLAB = shutil.which("glab", path=SUB_ENV["PATH"]) or "glab"            # 查 MR 合并了没有，没装就不查

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
    "feishu_doc_titles": True, # 产出物里的飞书文档用 lark-cli 以你的身份查真实标题；false 就只用 AI 写的说明
    "gitlab_mr_state": True,   # AI 给的 GitLab MR 用 glab 以你的身份查标题和合并了没有；false 就不查
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
CREATE TABLE IF NOT EXISTS cmux_open (session_key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS tags (project TEXT PRIMARY KEY, label TEXT, color TEXT);
CREATE TABLE IF NOT EXISTS goals (session_key TEXT PRIMARY KEY, goal TEXT, criteria TEXT, status TEXT, at REAL);
CREATE TABLE IF NOT EXISTS links (session_key TEXT, url TEXT, label TEXT, ts REAL, PRIMARY KEY (session_key, url));
CREATE TABLE IF NOT EXISTS doc_titles (token TEXT PRIMARY KEY, title TEXT, at REAL);
CREATE TABLE IF NOT EXISTS mr_state (ref TEXT PRIMARY KEY, state TEXT, title TEXT, at REAL);
CREATE TABLE IF NOT EXISTS images (session_key TEXT, sha TEXT, ts REAL, file TEXT, bytes INTEGER, PRIMARY KEY (session_key, sha));
CREATE INDEX IF NOT EXISTS images_session ON images(session_key, ts);
CREATE TABLE IF NOT EXISTS topics (id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, name TEXT, aliases TEXT DEFAULT '[]',
  created REAL, deleted INTEGER DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS topics_name ON topics(project, name);
CREATE TABLE IF NOT EXISTS session_topics (session_key TEXT PRIMARY KEY, topic_id INTEGER, at REAL);
CREATE TABLE IF NOT EXISTS handoffs (session_key TEXT PRIMARY KEY, ts REAL, text TEXT);
CREATE TABLE IF NOT EXISTS ask_dismiss (session_key TEXT, k TEXT, at REAL, PRIMARY KEY (session_key, k));
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
    migrate_links(conn)
    load_user_dirs(conn)
    return conn


def migrate_links(conn):
    """产出物表后加的列：ctx = AI 给出它的那句话，cat = 飞书 / 代码 / 文件 / 网页 / MR，doc = 飞书文档的「类型:token」，用来取真实标题，
    mr = GitLab MR 的「主机/项目!编号」，用来查合并了没有。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(links)")}
    for col in ("ctx", "cat", "doc", "mr", "alt"):
        if col not in cols:
            try:
                conn.execute("ALTER TABLE links ADD COLUMN %s TEXT DEFAULT ''" % col)
            except sqlite3.OperationalError:
                pass   # 命令行和服务同时打开，另一边已经加上了
    conn.execute("CREATE INDEX IF NOT EXISTS links_ts ON links(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS links_url ON links(url)")
    conn.execute("CREATE INDEX IF NOT EXISTS links_doc ON links(doc)")
    rows = conn.execute("SELECT DISTINCT url FROM links WHERE cat IS NULL OR cat = ''").fetchall()
    if meta_get(conn, "links_mr") != "1":   # MR、流水线、提交从「代码」里分出来，单算一类
        rows += conn.execute("SELECT DISTINCT url FROM links WHERE cat = 'code'").fetchall()
        meta_set(conn, "links_mr", "1")
    if meta_get(conn, "links_alt") != "2":  # 已记下的本机文件：在额外 worktree 里的，补记主工作区里的同一路径
        rows += conn.execute("SELECT DISTINCT url FROM links WHERE cat = 'file'").fetchall()
        meta_set(conn, "links_alt", "2")
    roots = known_roots(conn) if rows else []
    for (url,) in rows:
        conn.execute("UPDATE links SET cat=?, doc=?, mr=?, alt=? WHERE url=?", (out_cat(url), feishu_doc(url), mr_ref(url), worktree_twin(url, roots), url))
    conn.commit()


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


def run(args, timeout=15, env=None):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env or SUB_ENV)
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


def fmt_hm(ts, now=None):
    """今天的只写钟点（「14:30」），其余同 fmt_time。"""
    t = fmt_time(ts, now)
    return t[3:] if t.startswith("今天 ") else t


def fmt_ago(sec):
    sec = max(0, sec)
    if sec < 3600:
        return "%d 分钟" % max(1, int(sec // 60))
    if sec < 86400:
        h, m = int(sec // 3600), int(sec % 3600 // 60)
        return "%d 小时 %d 分" % (h, m) if m else "%d 小时" % h
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


# ❓ 的编号写在事情末尾的括号里，如（Q-031）；也认开头的【Q-031】。「BUG-0016（…）」这种正文里的编号不算
ASK_ID_TAIL_RE = re.compile(r"[（(\[【]\s*([A-Za-z][A-Za-z0-9]*-\d+)\s*[）)\]】]\s*[？?。.]?\s*$")
ASK_ID_HEAD_RE = re.compile(r"^\s*[\[【]\s*([A-Za-z][A-Za-z0-9]*-\d+)\s*[\]】]")
BLOCK_RE = re.compile(r"^(?:卡住?|阻塞|挡着)\s*[：:]\s*(.+)")
REC_RE = re.compile(r"^(?:荐|推荐)\s*[：:]\s*(.+)")
REC_SEP_RE = re.compile(r"^[\s，,：:；;。.、—–\-（(]+|[）)]+$")
Q_TAIL_RE = re.compile(r"[？?：:。.\s]+$")


def ask_id(q):
    m = ASK_ID_TAIL_RE.search(q) or ASK_ID_HEAD_RE.match(q)
    return m.group(1).upper() if m else ""


def ask_key(d):
    """同一件事认编号；没编号的按问题原文（去掉空白和末尾标点）认。"""
    return d.get("id") or "q:" + re.sub(r"\s+", "", Q_TAIL_RE.sub("", d["q"]))


def split_rec(text, opts):
    """「荐：先别，现在发会打断评测」→（推荐的选项，理由）。开头对不上哪个选项的，选项留空，整段当理由。"""
    hit = max((o for o in opts if text.startswith(o)), key=len, default="")
    return hit, REC_SEP_RE.sub("", text[len(hit):]).strip()[:120]


def parse_decisions(text):
    """「❓ 事情（编号）｜选项｜选项｜卡：任务｜荐：选项，理由」：每行一件等你拍板的事；代码块里的是举例，不算。"""
    items, fence = [], False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        m = None if fence else ASK_RE.match(line)
        parts = split_parts(m.group(1)) if m else None
        if parts:
            q, opts, block, rec = parts[0][:200], [], [], ""
            for p in parts[1:]:
                b, r = BLOCK_RE.match(p), REC_RE.match(p)
                if b:
                    block.append(b.group(1))
                elif r:
                    rec = r.group(1).strip()
                elif len(opts) < 6:
                    opts.append(p[:40])
            d = {"q": q, "opts": opts, "id": ask_id(q), "block": "；".join(block)[:80]}
            d["rec"], d["why"] = split_rec(rec, opts) if rec else ("", "")
            d["k"] = ask_key(d)
            items.append(d)
    return items[:12]


# 🧭 它没问你、自己定了的事：「🧭 定了什么｜为什么」，每轮最多记 5 件，会话详情里列出来，你不同意就去会话里说
CALL_RE = re.compile(r"^\s*(?:[-*]\s+)?🧭\s*(.+)")
CALL_HEAD_RE = re.compile(r"^(?:我(?:替你)?定了|替你定了|定了|我决定)\s*[：:]\s*")
CALL_WHY_RE = re.compile(r"^(?:因为|为什么|理由|原因)\s*[：:]\s*")
CALLS_MAX = 40


def parse_calls(text):
    items, fence = [], False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        m = None if fence else CALL_RE.match(line)
        parts = split_parts(m.group(1)) if m else None
        if parts:
            t = CALL_HEAD_RE.sub("", parts[0])[:160]
            why = CALL_WHY_RE.sub("", "；".join(parts[1:]))[:160]
            if t:
                items.append({"t": t, "why": why})
    return items[:5]


def note_calls(st, text, ts):
    """这一轮的 🧭 记进 st["calls"]。按原文认同一件（重读对话记录时不重复记），留最近 CALLS_MAX 件。"""
    items = parse_calls(text)
    if not items:
        return
    calls = st.setdefault("calls", [])
    seen = {c["k"] for c in calls}
    for d in items:
        k = re.sub(r"\s+", "", d["t"])
        if k not in seen:
            seen.add(k)
            calls.append({"k": k, "t": d["t"], "why": d["why"], "ts": ts})
    if len(calls) > CALLS_MAX:
        st["calls"] = calls[-CALLS_MAX:]


# ---------------------------------------------------------------- 没答的 ❓：跨轮记账，按固定格式判断答没答（不调模型）

ASKS_MAX = 60
NUM_HEAD_RE = re.compile(r"^\s*(?:\d+\s*[.、．)）]|❓️?|[-*•])\s*")
PENDING_RE = re.compile(r"[：:｜|]\s*待定\s*[。.]?\s*$")


def note_asks(st, text, ts):
    """这一轮回复里的 ❓ 记进 st["asks"]。同一件再问：用新说法，保留第一次问的时间；之前算答了的重新打开（又问了说明没定下来）。"""
    items = parse_decisions(text)
    if not items:
        return
    asks = st.setdefault("asks", [])
    idx = {a["k"]: a for a in asks}
    for d in items:
        a = idx.get(d["k"])
        if a is None:
            a = idx[d["k"]] = {"k": d["k"], "first": ts}
            asks.append(a)
        a.update(q=d["q"], opts=d["opts"], id=d["id"], block=d["block"], rec=d["rec"], why=d["why"], last=ts, done=0, ans="")
    if len(asks) > ASKS_MAX:
        # 先丢最早答掉的，再丢最早问的
        drop = sorted(asks, key=lambda a: (not a.get("done"), a.get("last") or 0))[:len(asks) - ASKS_MAX]
        st["asks"] = [a for a in asks if a not in drop]


HEAD_SEP_RE = re.compile(r"\s*(?:——|—|--|：|:)\s*")
QUOTE_RE = re.compile(r"[\s“”\"'「」『』《》`]+")


def short_head(body, q):
    """「Top20 名单——ok的」「样卡写法：认可」：用问题开头几个字（至少 4 个）指代这件，后面接答复。返回答复，不是就返回空。"""
    m = HEAD_SEP_RE.search(body)
    if not m:
        return ""
    head = QUOTE_RE.sub("", body[:m.start()])
    return body[m.end():].strip() if len(head) >= 4 and QUOTE_RE.sub("", q).startswith(head) else ""


def answer_of(a, lines):
    """你这条消息里有没有答这件。认四种：「N. 事情：选项」（看板拼的格式）、把 ❓ 行抄回来改过选项、提到编号、
    用问题开头几个字指代（「Top20 名单——ok的」）。写「待定」的不算；原样抄回来、选项一个没删的也不算。"""
    q = Q_TAIL_RE.sub("", a.get("q") or "")
    pid = re.compile(r"(?<![A-Za-z0-9-])%s(?!\d)" % re.escape(a["id"]), re.I) if a.get("id") else None
    echo = "｜".join(a.get("opts") or [])
    for line in lines:
        body = NUM_HEAD_RE.sub("", line, count=1)
        hit_q = bool(q) and body.startswith(q)
        if not hit_q and not (pid and pid.search(line)):
            short = short_head(body, q)
            if short and not PENDING_RE.search(line) and short != "待定":
                return short[:80]
            continue
        if PENDING_RE.search(line):
            continue
        rest = body[len(q):] if hit_q else body
        rest = re.sub(r"^[？?：:。.｜|\s]+", "", rest).strip()
        # 原样抄回来的（「卡：」「荐：」那几段不算选项）不算答了
        if hit_q and (not rest or "｜".join(p for p in split_parts(rest) if not BLOCK_RE.match(p) and not REC_RE.match(p)) == echo):
            continue
        return (rest or line)[:80]
    return ""


def note_answers(st, text, ts):
    """你发的消息：把答了的 ❓ 标成已答。"""
    asks = [a for a in st.get("asks") or [] if not a.get("done")]
    if not asks or not (text or "").strip():
        return
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for a in asks:
        ans = answer_of(a, lines)
        if ans:
            a["done"], a["ans"] = ts or time.time(), ans
    # 只回了一个选项（「两条一起开」）：哪件有这个选项就是答哪件；几件都有，只认最近一轮问的，那一轮里还不止一件就不猜
    for line in lines:
        t = Q_TAIL_RE.sub("", NUM_HEAD_RE.sub("", line, count=1)).strip()
        hit = [a for a in asks if not a.get("done") and t and t != "待定" and t in (a.get("opts") or [])]
        if hit:
            newest = max(a.get("last") or 0 for a in hit)
            hit = [a for a in hit if (a.get("last") or 0) == newest]
            if len(hit) == 1:
                hit[0]["done"], hit[0]["ans"] = ts or time.time(), t


PASTE_TAG_RE = re.compile(r'</?pasted_content(?:\s+id="[^"]*")?\s*>')


def unwrap_paste(text):
    """Claude Code 把整段粘贴的文字包在 <pasted_content id="…"> 里；去掉标签，不然整条消息会被当成系统注入的跳过。"""
    return PASTE_TAG_RE.sub("", text) if "pasted_content" in (text or "") else text


# ---------------------------------------------------------------- 产出链接与图片

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"'`，。；、）」』|]+")
MD_LINK_RE = re.compile(r"\[([^\]\n]{1,80})\]\((https?://[^\s)]+)\)")
SKIP_LINK_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|0\.0\.0\.0)([:/]|$)")
IMG_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


CODEISH_RE = re.compile(r"""^\s*([$!>#]|curl\b|wget\b|open\b|"[\w.-]+"\s*:|[\w.-]+=)""")
LINK_KINDS = {"docx": "飞书文档", "docs": "飞书文档", "doc": "飞书文档", "wiki": "飞书知识库", "base": "飞书多维表格",
              "sheets": "飞书表格", "minutes": "飞书妙记", "drive": "飞书云盘", "mindnotes": "飞书思维笔记", "slides": "飞书幻灯片"}
# AI 提到的本机文件也算产出物。光写路径的只认这些后缀；写成 [名字](路径) 链接的再加 json 和文件夹
FILE_KINDS = {"md": "Markdown", "markdown": "Markdown", "html": "网页文件", "htm": "网页文件", "pdf": "PDF", "docx": "Word", "doc": "Word",
              "xlsx": "Excel", "xls": "Excel", "csv": "表格", "tsv": "表格", "pptx": "PPT", "ppt": "PPT", "key": "Keynote",
              "png": "图片", "jpg": "图片", "jpeg": "图片", "gif": "图片", "svg": "图片", "webp": "图片",
              "mp4": "视频", "mov": "视频", "mp3": "音频", "wav": "音频", "txt": "文本", "zip": "压缩包", "json": "JSON"}
BARE_EXTS = sorted((set(FILE_KINDS) - {"json"}), key=len, reverse=True)
FILE_TAIL_RE = re.compile(r"(.+\.(?:%s))(?![A-Za-z0-9])" % "|".join(BARE_EXTS), re.I)
MD_FILE_RE = re.compile(r"\[([^\]\n]{1,80})\]\(((?:file://)?(?:~|/)[^)\s]+)\)")
PATH_RE = re.compile(r"(?<![\w.:/~@-])((?:~|/Users|/private/tmp|/tmp|/Volumes)/[^\s`'\"<>()\[\]{}，。；、）」』|*]+"
                     r"|[\w.-]+(?:/[^\s`'\"<>()\[\]{}，。；、）」』|*:]+)+)")
SKIP_FILE_RE = re.compile(r"/(\.claude|\.agents|\.git|node_modules|\.codex/(skills|sessions|memories|rules))(/|$)|^/var/folders/|\.app(/|$)")
SKIP_FILE_NAMES = {"SKILL.md", "AGENTS.md", "CLAUDE.md", "MEMORY.md"}
FEISHU_DOC_RE = re.compile(r"^/(docx|docs|doc|wiki|base|sheets|mindnotes|slides|file|drive/folder)/([A-Za-z0-9]{16,})")
FEISHU_TYPES = {"docx": "docx", "docs": "doc", "doc": "doc", "wiki": "wiki", "base": "bitable", "sheets": "sheet",
                "mindnotes": "mindnote", "slides": "slides", "file": "file", "drive/folder": "folder"}


def file_kind(path):
    base = os.path.basename(path.rstrip("/"))
    ext = base.rsplit(".", 1)[1].lower() if "." in base.strip(".") else ""
    return FILE_KINDS.get(ext) or ("文件" if ext else "文件夹")


def link_name(url):
    if url.startswith("/"):
        return file_kind(url)
    u = urlparse(url)
    host, path = u.netloc.lower(), u.path
    m = re.search(r"/([^/]+)/-/merge_requests/(\d+)", path)
    if m:
        return "MR !%s · %s" % (m.group(2), m.group(1))
    m = re.search(r"^/[^/]+/([^/]+)/pull/(\d+)", path) if host == "github.com" else None
    if m:
        return "PR #%s · %s" % (m.group(2), m.group(1))
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


MR_PATH_RE = re.compile(r"/-/(merge_requests|pipelines|jobs|commit|compare)(/|$)|^/[^/]+/[^/]+/(pull|commit|compare|actions/runs)(/|$)")
GITLAB_MR_RE = re.compile(r"^/(.+?)/-/merge_requests/(\d+)(/|$)")


def out_cat(url):
    """AI 给的东西分两种：产物（飞书文档表格、代码仓库和文件、本机文件、网页），一直要留着；
    MR 和流水线（MR / PR、流水线、提交）是临时的，合并完就不用再看，单算一类 mr，不算产物。"""
    if url.startswith("/"):
        return "file"
    u = urlparse(url)
    host, path = u.netloc.lower(), u.path
    if (host.endswith(".feishu.cn") or host.endswith(".larksuite.com")) and host.split(".")[0] not in ("open", "www"):
        return "feishu"
    if "/-/" in path or host.startswith(("git.", "gitlab.")) or host == "github.com":
        return "mr" if MR_PATH_RE.search(path) else "code"
    return "web"


def mr_ref(url):
    """GitLab MR 链接 -> 「主机/项目路径!编号」，按它用 glab 查标题和合并了没有；别的链接返回空。"""
    if url.startswith("/"):
        return ""
    u = urlparse(url)
    m = GITLAB_MR_RE.match(u.path)
    return "%s/%s!%s" % (u.netloc.lower(), m.group(1), m.group(2)) if m and u.netloc else ""


_ROOTS = {"at": 0, "roots": []}
_REMOTES = {"at": 0, "map": {}}
_BRANCH = {}


def known_roots(conn):
    """会话待过的、AI 给过文件的仓库的主工作区目录（只用 stat 判断，十分钟重算一次）。看板服务读不了「桌面」里的文件内容，
    靠它们认出 worktree 是哪个主仓库的。"""
    if time.time() - _ROOTS["at"] < 600:
        return _ROOTS["roots"]
    roots = set()
    dirs = [r[0] for r in conn.execute("SELECT DISTINCT cwd FROM prompts WHERE cwd != ''")]
    dirs += [os.path.dirname(r[0]) for r in conn.execute("SELECT DISTINCT url FROM links WHERE cat = 'file'")]
    for cwd in dirs:
        d = cwd
        while d and d not in ("/", HOME):
            if os.path.isdir(os.path.join(d, ".git")):
                roots.add(d)
                break
            d = os.path.dirname(d)
    _ROOTS.update(at=time.time(), roots=sorted(roots))
    return _ROOTS["roots"]


def _main_of(wt, roots):
    """额外 worktree 的根目录 -> 主工作区目录。先读 wt/.git（写着 gitdir: <主仓库>/.git/worktrees/<名字>）；
    在「桌面」等后台服务读不了的地方，就看哪个已知主仓库里有 .git/worktrees/<wt 的文件夹名>（只用 stat）。"""
    try:
        with open(os.path.join(wt, ".git"), encoding="utf-8") as f:
            m = re.match(r"gitdir:\s*(.+)", f.read(1000).strip())
        gd = os.path.normpath(os.path.join(wt, m.group(1).strip())) if m else ""
        # 子模块的 gitdir 是 .git/modules/…，不算
        if not gd or os.path.basename(os.path.dirname(gd)) != "worktrees" or os.path.basename(os.path.dirname(os.path.dirname(gd))) != ".git":
            return ""
        return os.path.dirname(os.path.dirname(os.path.dirname(gd)))
    except (OSError, ValueError):
        name = os.path.basename(wt)
        cands = [r for r in roots or [] if r != wt and os.path.isdir(os.path.join(r, ".git", "worktrees", name))]
        cands.sort(key=lambda r: os.path.dirname(r) != os.path.dirname(wt))   # 和 worktree 在同一个文件夹里的优先
        return cands[0] if cands else ""


def worktree_twin(path, roots=None):
    """本机文件在 git 的额外 worktree 里时 -> 主工作区里同一相对路径的位置，worktree 被清理后从那里找；不在 worktree 里返回空。
    只记一个路径，不复制文件；不跑 git。"""
    if not path.startswith("/"):
        return ""
    d = os.path.dirname(path)
    while d not in ("/", "", HOME):
        g = os.path.join(d, ".git")
        if os.path.isdir(g):
            return ""   # 主工作区
        if os.path.isfile(g):
            main = _main_of(d, roots)
            return os.path.join(main, os.path.relpath(path, d)) if main and main != d else ""
        d = os.path.dirname(d)
    return ""


def repo_root(path):
    d = os.path.dirname(path)
    while d not in ("/", "", HOME):
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        d = os.path.dirname(d)
    return ""


def _web_base(raw):
    """git 远端地址 -> (主机, 项目路径)；git@host:a/b.git、https://host/a/b.git、ssh://git@host:22/a/b 都认。"""
    m = re.match(r"^(?:https?://(?:[^@/]+@)?|ssh://(?:[^@/]+@)?|[^@/]+@)([^/:]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", raw or "")
    return (m.group(1).lower(), m.group(2)) if m else None


def codex_remotes(conn, roots):
    """{主工作区目录: 远端地址}，从 Codex 会话记录开头的 git 信息里取（后台服务读不了「桌面」里的 .git/config）。一小时重算一次。"""
    if time.time() - _REMOTES["at"] < 3600:
        return _REMOTES["map"]
    out = {}
    for (data,) in conn.execute("SELECT data FROM sessions WHERE key LIKE 'codex:%'"):
        try:
            st = json.loads(data)
            with open(st.get("path") or "", encoding="utf-8") as f:
                p = (json.loads(f.readline()) or {}).get("payload") or {}
        except (OSError, ValueError, TypeError):
            continue
        url, cwd = ((p.get("git") or {}).get("repository_url") or ""), p.get("cwd") or ""
        if not url or not cwd.startswith("/"):
            continue
        twin = worktree_twin(os.path.join(cwd, "_"), roots)
        root = repo_root(twin) if twin else repo_root(os.path.join(cwd, "_"))
        if root:
            out.setdefault(root, url)
    _REMOTES.update(at=time.time(), map=out)
    return out


def default_branch(host, proj):
    """GitLab 项目的默认分支，用 glab 查一次记住；GitHub 和查不到的用 HEAD（网页上会跳到默认分支）。"""
    if host == "github.com":
        return "HEAD"
    k = host + "/" + proj
    if k not in _BRANCH and os.path.isabs(GLAB) and os.access(GLAB, os.X_OK):
        r = run([GLAB, "api", "--hostname", host, "projects/" + quote_url(proj, safe="")], timeout=15)
        try:
            _BRANCH[k] = (json.loads(r.stdout or "") or {}).get("default_branch") or "" if r.returncode == 0 else ""
        except (ValueError, AttributeError):
            _BRANCH[k] = ""
    return _BRANCH.get(k) or "HEAD"


def repo_web_url(root, rel, remotes):
    """主仓库在 GitLab / GitHub 上时 -> 默认分支上这个文件的网页地址。远端先读 .git/config，读不了用 Codex 记下的（remotes）。"""
    raw = ""
    try:
        with open(os.path.join(root, ".git", "config"), encoding="utf-8") as f:
            m = re.search(r'\[remote "origin"\][^\[]*?\burl\s*=\s*(\S+)', f.read(20000))
            raw = m.group(1) if m else ""
    except (OSError, ValueError):
        pass
    raw = raw or (remotes or {}).get(root, "")
    hp = _web_base(raw)
    if not hp:
        return ""
    host, proj = hp
    sep = "/blob/" if host == "github.com" else "/-/blob/"
    return "https://%s/%s%s%s/%s" % (host, proj, sep, quote_url(default_branch(host, proj)), quote_url(rel))


def file_fallback(path, alt, remotes=None, resolve=True):
    """AI 给的本机文件不在了（所在的 worktree 被清理）时去哪找：主工作区里有同名文件 -> ("file", 路径)；
    没有、但主仓库在 GitLab / GitHub 上 -> ("web", 默认分支上的网页地址；resolve=False 时只说有，不查分支)。都没有返回 None。"""
    if not alt:
        return None
    if os.path.exists(alt):
        return "file", alt
    root = repo_root(alt)
    if not root:
        return None
    if not resolve:
        return ("web", "") if os.path.isfile(os.path.join(root, ".git", "config")) else None
    web = repo_web_url(root, os.path.relpath(alt, root), remotes)
    return ("web", web) if web else None


def feishu_doc(url):
    """飞书文档链接 -> 「类型:token」，按它去飞书查真实标题；别的链接返回空。"""
    if out_cat(url) != "feishu":
        return ""
    m = FEISHU_DOC_RE.match(urlparse(url).path)
    return "%s:%s" % (FEISHU_TYPES[m.group(1)], m.group(2)) if m else ""


def out_text(url, label, title):
    """产出物显示的名字：飞书文档用真实标题，其次是 AI 写的说明，文件用文件名。"""
    if title:
        return title
    name = link_name(url)
    core = re.sub(r"^(MR|PR|CI|流水线|任务)\s*", "", label or "").strip()
    if label and not (core and core in name):   # 说明只是「!1496」「CI 91196」这种编号，用「MR !1496 · chatdam」更好认
        return label
    return os.path.basename(url.rstrip("/")) if url.startswith("/") else name


def link_label(before):
    """链接前面那句话里最后一小句，去掉「请到」「见」这类引导词，当作链接说明。"""
    t = clean_line(re.split(r"[，。；;！？!?]|[,.](?=\s)", before)[-1])
    if re.search(r"[{}\"=$]|\B--\w", t):
        return ""
    t = re.sub(r"[\s:：（(【\[<|-]+$", "", t)
    t = re.sub(r"(请现在|请|现在|可以)?(到|在|见|打开|点开|访问|看|参考|地址|链接|网址|位置|入口)$", "", t)
    t = re.sub(r"[\s:：（(【\[<|-]+$", "", t).strip()
    return t[-30:] if len(t) >= 2 else ""


def link_ctx(lines, i, at):
    """AI 给出这个产出物的那句话：Markdown 链接只留名字，网址换成「（链接）」，家目录写成 ~；只剩链接时带上前一行，太长截命中附近。"""
    def tidy(t):
        return URL_RE.sub("（链接）", re.sub(r"\[([^\]\n]*)\]\([^)\s]*\)", r"\1", t)).replace(HOME + "/", "~/")
    before, after = tidy(lines[i][:at]), tidy(lines[i][at:])
    t = clean_line(("…" + before[-60:] if len(before) > 60 else before) + (after[:120] + "…" if len(after) > 120 else after))
    if len(re.sub(r"[（链接）\s:：-]", "", t)) < 4:
        prev = next((clean_line(tidy(x)) for x in reversed(lines[:i]) if x.strip()), "")
        if prev:
            t = (prev[-100:] + " " + t).strip()
    return t[:200]


def local_file(raw, cwd, linked=False):
    """AI 回复里的一个路径 -> 本机存在的绝对路径；不像产出物（规则文件、依赖、缓存）或已经不在的返回空。"""
    p = raw[7:] if raw.startswith("file://") else raw
    if "%" in p:
        p = unquote_url(p)
    p = re.sub(r"(#L\d+(-L?\d+)?|:\d+(:\d+)?(-\d+)?)$", "", p.rstrip(".,;:!?)"))
    if not linked:
        m = FILE_TAIL_RE.match(p)
        if not m:
            return ""
        p = m.group(1)
    if p.startswith("~/"):
        p = HOME + p[1:]
    elif not p.startswith("/"):
        if not cwd or "/" not in p:
            return ""
        p = os.path.join(cwd, p)
    p = os.path.normpath(p)
    if os.path.basename(p) in SKIP_FILE_NAMES or SKIP_FILE_RE.search(p):
        return ""
    try:
        is_dir = os.path.isdir(p)
        if not (is_dir or os.path.isfile(p)):
            return ""
    except (OSError, ValueError):
        return ""
    if not is_dir and file_kind(p) in ("文件", "文件夹"):
        return ""
    return p


def extract_links(text, cwd=""):
    """AI 回复里给你的产出物：链接（飞书文档、MR、产物地址等）和本机文件，带一小段说明和原话；代码块和命令行里的不算。"""
    text = re.sub(r"```.*?(```|$)", "", text or "", flags=re.S)
    lines = text.splitlines()
    out, seen = [], set()

    def add(url, label, i, at):
        if url and url not in seen and not SKIP_LINK_RE.match(url):
            seen.add(url)
            out.append((url, label, link_ctx(lines, i, at)))

    for i, ln in enumerate(lines):
        if CODEISH_RE.match(ln):
            continue
        for m in MD_LINK_RE.finditer(ln):
            add(m.group(2).rstrip(".,;:!?"), clean_line(m.group(1))[:60], i, m.start())
        if "/" in ln:
            for m in MD_FILE_RE.finditer(ln):
                p = local_file(m.group(2), cwd, linked=True)
                name = clean_line(m.group(1))[:60]
                add(p, "" if not p or name in (os.path.basename(p), m.group(2)) else name, i, m.start())
        for m in URL_RE.finditer(ln):
            url = m.group(0).rstrip(".,;:!?*_")
            if len(url) >= 12 and url not in seen:
                add(url, link_label(ln[:m.start()]), i, m.start())
        if "/" in ln:
            bare = MD_FILE_RE.sub(lambda m: " " * len(m.group(0)), URL_RE.sub(lambda m: " " * len(m.group(0)), ln))
            for m in PATH_RE.finditer(bare):
                add(local_file(m.group(1), cwd), "", i, m.start())
    return out


def note_links(st, ts, text):
    if text and ("http" in text or "/" in text):
        found = extract_links(text, st.get("cwd") or "")
        if found:
            st.setdefault("new_links", []).extend([ts, u, l, c] for u, l, c in found)


# 你在消息里写「#主题」，会话就归到这个主题。# 前面紧跟字母数字或网址符号的不算（C#、网址锚点），纯数字（[Image #9]）和色值也不算
HASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_/&#.:?=@-])#([^\s#]{1,24})(#?)")
TAG_STOP_RE = re.compile(r"[，。、,.;；:：!！?？()（）\[\]【】{}<>《》\"'“”‘’「」『』|｜/\\~～`*]")
HEX_RE = re.compile(r"[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8}")
CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def is_color(name):
    # #fff、#CCFB5B 是色值；#add、#cafe 这种全是字母的英文词不算
    return bool(HEX_RE.fullmatch(name)) and (any(c.isdigit() for c in name) or len(set(name.lower())) == 1)


def note_hashtag(st, ts, text):
    """记下消息里的 #主题（最近 5 个），看板上从最近写的往前找第一个认得出的，见 topic_by_tag。kind：
    new = 认不出时可以新建主题：#评测#，或在消息开头、结尾，前后空格隔开的 #评测；
    name = 夹在消息中间、前后空格隔开的，只认同名的已有主题，贴进来的代码、聊天记录里的 #xxx 不会建出一堆主题；
    prefix = 中文里连着写的（看下#评测的进度），只认名字是它开头的已有主题。代码块里的不算。"""
    text = CODE_RE.sub(" ", text or "")
    tags = st.get("hashtags") or []
    for m in HASHTAG_RE.finditer(text):
        raw = m.group(1)
        stop = TAG_STOP_RE.search(raw)
        name = raw[:stop.start()] if stop else raw
        if not name or name.isdigit() or is_color(name):
            continue
        a, b = m.start(), m.end(1)
        spaced = (a == 0 or text[a - 1].isspace()) and not stop and (b == len(text) or text[b].isspace())
        nxt = text[b:].lstrip(" \t")[:1]
        if m.group(2) and not stop:
            kind = "new"
        elif spaced and len(name) <= 12:
            edge = not text[:a].strip() or not text[b:].strip()
            kind = "new" if edge and not (nxt and nxt in "{(<=;:") else "name"   # #include <x>、#main { 是代码
        else:
            kind = "prefix"
        tags = [t for t in tags if t["name"] != name] + [{"name": name, "kind": kind, "ts": ts}]
    if tags:
        st["hashtags"] = tags[-5:]


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
    if st.get("_dry"):
        return   # 重读整段对话（看全部对话）时不再存图，图在扫描时已经存过
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
    for row in st.pop("new_links", []):
        ts, url, label = row[:3]
        ctx = row[3] if len(row) > 3 else ""
        url = url[:1000]
        # 同一个会话里多次给出同一个链接：时间取最近一次，说明取最近一次非空的，原话取最近一次的
        conn.execute("INSERT INTO links (session_key, url, label, ts, ctx, cat, doc, mr, alt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT(session_key, url) DO UPDATE SET ts=MAX(links.ts, excluded.ts), "
                     "label=CASE WHEN excluded.label != '' THEN excluded.label ELSE links.label END, "
                     "ctx=CASE WHEN excluded.ctx != '' AND (links.ctx IS NULL OR links.ctx = '' OR excluded.ts >= links.ts) "
                     "THEN excluded.ctx ELSE links.ctx END",
                     (key, url, label, ts or 0, ctx, out_cat(url), feishu_doc(url), mr_ref(url), worktree_twin(url, known_roots(conn)) if url.startswith("/") else ""))
    for ts, sha, name, size in st.pop("new_images", []):
        conn.execute("INSERT OR IGNORE INTO images (session_key, sha, ts, file, bytes) VALUES (?, ?, ?, ?, ?)",
                     (key, sha, ts or 0, name, size))


# Claude 压缩上下文时写的交接：前面一段「This session is being continued…Summary:」、后面让它接着干活的说明都是固定套话
HANDOFF_TAIL = ("\nIf you need specific details from before compaction", "\nContinue the conversation from where it left off",
                "\nPlease continue the conversation from where we left", "\nThe messages after this summary are")


def clean_handoff(text):
    t = re.sub(r"<system-reminder>.*?</system-reminder>\s*", "", text or "", flags=re.S)
    m = re.search(r"^Summary:[ \t]*\n", t[:4000], re.M)
    if m:
        t = t[m.end():]
    elif t.startswith("This session is being continued"):
        t = t.split("\n\n", 1)[-1]
    # 套话可能叠了两段（各带一句「If you need specific details…」），在末尾 2000 字里取最早出现的那处
    cut = [i for i in (t.find(x, max(0, len(t) - 2000)) for x in HANDOFF_TAIL) if i >= 0]
    if cut:
        t = t[:min(cut)]
    t = re.sub(r"<analysis>.*?</analysis>", "", t, flags=re.S)
    return re.sub(r"</?summary>", "", t).strip()[:40000]


def last_handoff(path):
    """整个文件里最后一段压缩交接，返回 (时间, 正文)。大文件第一次读、或一次长了 2MB 以上时只读头尾，中间的交接靠这里找回来。"""
    mark = b'"isCompactSummary":true'
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            i = m.rfind(mark)
            while i >= 0:
                a = m.rfind(b"\n", 0, i) + 1
                b = m.find(b"\n", i)
                try:
                    rec = json.loads(m[a:b if b >= 0 else len(m)].decode("utf-8", "replace"))
                except ValueError:
                    rec = None
                if isinstance(rec, dict) and rec.get("type") == "user" and rec.get("isCompactSummary") and not rec.get("isSidechain"):
                    text = clean_handoff(take_text((rec.get("message") or {}).get("content")))
                    if text:
                        return parse_iso(rec.get("timestamp")) or 0, text
                i = m.rfind(mark, 0, a)
    except (OSError, ValueError):
        return None
    return None


def flush_handoff(conn, key, st):
    got = st.pop("new_handoff", None)
    if got:
        conn.execute("INSERT INTO handoffs (session_key, ts, text) VALUES (?, ?, ?) ON CONFLICT(session_key) DO UPDATE SET "
                     "ts=excluded.ts, text=excluded.text WHERE excluded.ts >= handoffs.ts", (key, got[0] or 0, got[1]))


def handoff_goal(text):
    """压缩时的交接开头的「### 目标」那段 -> (目标, 完成标准)；没写这段返回 None"""
    m = re.search(r"^#{1,4}\s*目标\s*\n(.*?)(?=^#{1,4}\s|\Z)", text or "", re.M | re.S)
    if not m:
        return None
    body = " ".join(re.sub(r"^\s*[-*]\s*", "", l).strip() for l in m.group(1).splitlines() if l.strip())
    goal, _, crit = body.partition("完成标准")
    goal, crit = goal.strip().rstrip("，,；;（(").strip(), re.sub(r"^[：:]\s*", "", crit).strip().rstrip("）)")
    return (goal[:300], crit[:300]) if goal else None


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
        "thread_source": "", "source": "", "ai_title": "", "custom_title": "", "first_prompt": "", "last_prompt": "",
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


AMBIENT_RE = re.compile(r'(?:\s*<([\w-]+) source="ambient-ui-state">.*?</\1>)+\s*(?:## My request:\s*)?', re.S)
FILES_RE = re.compile(r'\s*# Files mentioned by the user:\n.*?\n## My request(?: for Codex)?:[ \t]*\n?', re.S)


def strip_ambient(text):
    """Codex 会在你的话前面套几段说明：发了图或文件时的「# Files mentioned by the user:」清单，开着内置浏览器时的
    <in-app-browser-context>（两段可能都有）。「## My request:」（旧版是「## My request for Codex:」）后面才是你说的。"""
    m = FILES_RE.match(text) or AMBIENT_RE.match(text)
    return text[m.end():] if m else text


def is_injected(text):
    t = (text or "").lstrip()
    return (not t) or t.startswith(("<", "[Request interrupted", "Caveat:", "# AGENTS.md",
                                    "This session is being continued", "【定时唤醒"))


def on_final(st, text, ts):
    if "_collect" in st and text.strip():
        st["_collect"].append({"kind": "it", "ts": ts or 0, "text": reply_text(text)})
    st["turn"] = "ended"
    st["last_kind"] = "final"
    st["final_ts"] = ts or st["final_ts"]
    if text.strip():
        st["last_text"] = text[-6000:]
    note_asks(st, text, ts or time.time())
    note_calls(st, text, ts or time.time())
    s = parse_status(text, ts or time.time())
    st["final_has_status"] = bool(s)
    if s:
        st["status"] = s
    g = parse_goal(text)
    if g:
        st["goal"] = g
        st["goal_ts"] = ts or time.time()


CLAUDE_KEYS = ('"type":"assistant"', '"type":"user"', '"type":"system"', '"type":"ai-title"',
               '"type":"custom-title"', '"type":"last-prompt"', '"queued_command"')


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
    if typ == "attachment":
        # 它干活时你插进来的话：不算新的一轮，只看里面有没有答 ❓
        a = rec.get("attachment") or {}
        if a.get("type") == "queued_command" and (a.get("origin") or {}).get("kind") == "human":
            text = unwrap_paste(a.get("prompt") if isinstance(a.get("prompt"), str) else take_text(a.get("prompt")))
            if not is_injected(text):
                note_answers(st, text, ts)
        return
    msg = rec.get("message") or {}
    content = msg.get("content")
    if typ == "user":
        if rec.get("isCompactSummary"):
            text = clean_handoff(take_text(content))
            if text:
                st["new_handoff"] = [ts, text]
            return
        if rec.get("isMeta"):
            return
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            st["turn"] = "running"
            st["last_kind"] = "tool"
            return
        text = unwrap_paste(take_text(content))
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
        note_hashtag(st, ts, text)
        note_answers(st, text, ts)
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
        st["source"] = codex_source(p)
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
        text = strip_ambient(take_text(p.get("content")))
        if p.get("role") == "user" and isinstance(p.get("content"), list):
            note_images(st, ts, data_url_pairs(p["content"]))
        if p.get("role") == "user" and not is_injected(text):
            st["n_prompts"] += 1
            if not st["first_prompt"]:
                st["first_prompt"] = text[:500]
            st["last_prompt"] = text[:500]
            note_hashtag(st, ts, text)
            note_answers(st, text, ts)
        elif p.get("role") == "assistant" and text.strip():
            st["progress_text"] = text[-1500:]
            if st.get("thread_source") != "subagent":   # 子代理的回复是给主会话的，不是给你的（Claude 的 isSidechain 同理）
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
    migrate_codex_prompts(conn)
    migrate_codex_files_preamble(conn)
    migrate_codex_source(conn)
    migrate_project_parents(conn)
    migrate_handoffs(conn)
    migrate_asks(conn, cfg)
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
        skipped = stt.st_size - offset > (TAIL_BYTES if offset else HEAD_BYTES + TAIL_BYTES)
        try:
            n, offset = parse_file(tool, path, st, offset, stt.st_size)
        except OSError as e:
            log("读取失败 %s: %s" % (path, e))
            continue
        nbytes += n
        parsed += 1
        key = "%s:%s" % (tool, st["id"])
        flush_media(conn, key, st)
        if tool == "claude":
            if skipped:
                got = last_handoff(path)
                if got:
                    st["new_handoff"] = list(got)
            flush_handoff(conn, key, st)
        if tool == "claude" and st.get("goal"):
            save_goal(conn, key, st["goal"].get("goal"), st["goal"].get("criteria"))
        conn.execute("INSERT OR REPLACE INTO sessions (key, last_activity, data) VALUES (?, ?, ?)",
                     (key, st["last_ts"] or stt.st_mtime, json.dumps(st, ensure_ascii=False)))
        conn.execute("INSERT OR REPLACE INTO files (path, session_key, size, mtime, offset) VALUES (?, ?, ?, ?, ?)",
                     (path, key, stt.st_size, stt.st_mtime, offset))
    for step in (sync_prompts, sync_codex_goals, sync_cmux):
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


# ---------------------------------------------------------------- Codex 定时任务：哪些会话是定时任务在跑，看板上标「定时」

_autos_cache = {"at": 0, "map": {}}


def rrule_text(rule):
    """Codex 定时任务的 rrule 写成（全称, 左栏的短写）：「每天 10:00」「每周一 09:00」「每 5 分钟」，一天几次的短写成「每天 4 次」。
    看不懂的返回空。DTSTART 是 UTC 的，钟点换成看板的时区。"""
    lines = (rule or "").split("\n")
    body = next((x for x in lines if "FREQ=" in x), "")
    kv = dict(x.split("=", 1) for x in body.replace("RRULE:", "").split(";") if "=" in x)
    utc = any(x.startswith("DTSTART") and x.endswith("Z") for x in lines)
    off = int(dt.datetime.now().astimezone(WORK_TZ).utcoffset().total_seconds() // 60) if utc else 0
    freq, n = kv.get("FREQ"), kv.get("INTERVAL", "1")
    if freq == "MINUTELY":
        return ("每 %s 分钟" % n,) * 2
    if freq == "HOURLY":
        return ("每小时" if n == "1" else "每 %s 小时" % n,) * 2
    try:
        mins = sorted(((int(h) * 60 + int(kv.get("BYMINUTE", "0").split(",")[0]) + off) % 1440) for h in kv.get("BYHOUR", "").split(","))
    except ValueError:
        return "", ""
    at = "、".join("%02d:%02d" % divmod(m, 60) for m in mins)
    if kv.get("COUNT") == "1":
        return "%s 跑一次" % at, "只跑一次"
    if freq == "DAILY":
        return "每天 " + at, "每天 " + at if len(mins) == 1 else "每天 %d 次" % len(mins)
    if freq == "WEEKLY":
        days = [WEEK["MO TU WE TH FR SA SU".split().index(d)] for d in kv.get("BYDAY", "").split(",") if d in "MO TU WE TH FR SA SU".split()]
        if days:
            return "每周" + "、".join(days) + " " + at, ("周%s %s" % (days[0], at)) if len(days) == 1 and len(mins) == 1 else "每周%s" % "、".join(days)
    return "", ""


def codex_automations():
    """Codex 定时任务（~/.codex/automations/*/automation.toml）：{线程 id: {name, when, paused}}，1 分钟缓存。
    同一个会话挂了好几个定时任务的，名字和时间连起来写，有一个在跑就不算暂停。"""
    if time.time() - _autos_cache["at"] > 60:
        found = {}
        for path in glob.glob(os.path.join(CODEX_HOME, "automations", "*", "automation.toml")):
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            got = {}
            for m in re.finditer(r'^(name|status|rrule|target_thread_id)\s*=\s*(".*")\s*$', text, re.M):
                try:
                    got[m.group(1)] = json.loads(m.group(2))
                except ValueError:
                    pass
            tid = got.get("target_thread_id")
            if not tid:
                continue
            paused = got.get("status", "ACTIVE") != "ACTIVE"
            found.setdefault(tid, []).append((paused, got.get("name") or "") + rrule_text(got.get("rrule")))
        out = {}
        for tid, xs in found.items():
            xs.sort()
            on = [x for x in xs if not x[0] and x[2]]
            out[tid] = {"name": "、".join(x[1] for x in xs if x[1]), "when": "；".join(x[2] for x in on),
                        "short": (on[0][3] + (" 等" if len(on) > 1 else "")) if on else "", "paused": all(x[0] for x in xs)}
        _autos_cache.update(at=time.time(), map=out)
    return _autos_cache["map"]


def auto_of(key):
    tool, sid = key.split(":", 1)
    return codex_automations().get(sid) if tool == "codex" else None


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
    threads = codex_threads()
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
        if source not in ("vscode", "cli"):
            continue
        try:
            item = json.loads(item_json)
        except ValueError:
            continue
        text = strip_ambient(take_text(item.get("content")))
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
    threads = codex_threads()
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
        if threads.get(tid, ("",))[0] not in ("vscode", "cli"):
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


_cmux_state = {"mtime": None}


def sync_cmux(conn):
    """读 cmux 记下的工作区：侧边栏里你手动改的名字（只认单个 AI 会话的工作区，分组标题不算），
    和哪些会话的标签页关掉了。"""
    try:
        mt = os.stat(CMUX_STATE).st_mtime
    except OSError:
        return
    if mt == _cmux_state["mtime"]:
        return
    with open(CMUX_STATE, encoding="utf-8") as f:
        data = json.load(f)
    _cmux_state["mtime"] = mt
    now = time.time()
    open_keys = set()
    for win in data.get("windows") or []:
        tm = win.get("tabManager") or {}
        anchors = {g.get("anchorWorkspaceId") for g in tm.get("workspaceGroups") or []}
        for w in tm.get("workspaces") or []:
            agents = [((p.get("terminal") or {}).get("agent") or {}) for p in w.get("panels") or []]
            agents = [a for a in agents if a.get("sessionId") and a.get("kind") in ("claude", "codex")]
            open_keys.update("%s:%s" % (a["kind"], a["sessionId"]) for a in agents)
            if w.get("workspaceId") in anchors or len(agents) != 1:
                continue
            key = "%s:%s" % (agents[0]["kind"], agents[0]["sessionId"])
            title = (w.get("customTitle") or "").strip()
            if w.get("customTitleSource") == "user" and title:
                conn.execute("INSERT INTO names (session_key, title, at) VALUES (?, ?, ?) ON CONFLICT(session_key) "
                             "DO UPDATE SET title=excluded.title, at=excluded.at WHERE names.title != excluded.title",
                             (key, title, now))
            else:
                conn.execute("DELETE FROM names WHERE session_key=?", (key,))
    sync_closed_tabs(conn, open_keys, now)


def sync_closed_tabs(conn, open_keys, now):
    """在 cmux 里关掉标签页的会话，自动标为完成；之后又在 cmux 里打开（比如看板上点「打开会话」），自动回到看板。
    还挂着的事不动：已定时、等同事、等时间点还没到的，多半是先关掉省内存，到点还要提醒你。"""
    if not open_keys:
        return   # 一个 AI 会话都没有，更像 cmux 重置了状态文件，不当成你全关了
    seen = {r["session_key"] for r in conn.execute("SELECT session_key FROM cmux_open")}
    for key in seen - open_keys:
        ov = conn.execute("SELECT action, at FROM overrides WHERE key=?", (key,)).fetchone()
        row = conn.execute("SELECT last_activity, data FROM sessions WHERE key=?", (key,)).fetchone()
        last = row["last_activity"] if row else 0
        if ov and (ov["action"] == "untrack" or ov["action"] in ("done", "closed") and last <= (ov["at"] or 0) + 120):
            continue
        status = (json.loads(row["data"]).get("status") or {}) if row else {}
        if (status.get("kind") == "peer" or status.get("kind") in ("time", "sched", "me") and (status.get("at") or 0) > now
                or conn.execute("SELECT 1 FROM schedules WHERE session_key=? AND status='pending'", (key,)).fetchone()):
            continue
        conn.execute("INSERT OR REPLACE INTO overrides (key, action, until_ts, at) VALUES (?, 'closed', NULL, ?)", (key, now))
        log("cmux 标签页关了，自动标为完成：%s" % key)
    conn.executemany("DELETE FROM overrides WHERE key=? AND action='closed'", [(k,) for k in open_keys])
    conn.execute("DELETE FROM cmux_open")
    conn.executemany("INSERT INTO cmux_open (session_key) VALUES (?)", [(k,) for k in open_keys])


def name_map(conn):
    return {r["session_key"]: r["title"] for r in conn.execute("SELECT session_key, title FROM names")}


def tag_map(conn):
    return {r["project"]: (r["label"], r["color"]) for r in conn.execute("SELECT * FROM tags")}


def tag_of(tags, project):
    label, color = tags.get(project, (None, None))
    return label or project, color or color_of(project)


# ---------------------------------------------------------------- 主题：项目下面按事情分组，你手动归的永远优先，其次看消息里的 #主题

def topic_name(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lstrip("#").strip()[:24]


def load_topics(conn):
    rows = []
    for r in conn.execute("SELECT * FROM topics ORDER BY id"):
        r = dict(r)
        try:
            r["aliases"] = json.loads(r["aliases"] or "[]")
        except ValueError:
            r["aliases"] = []
        rows.append(r)
    return rows


def topic_names(r):
    return [n.lower() for n in [r["name"]] + r["aliases"]]


def find_topic(rows, project, name):
    name = name.lower()
    return next((r for r in rows if r["project"] == project and name in topic_names(r)), None)


def insert_topic(conn, project, name, now):
    """新建主题。看板服务和命令行（ledger digest 等）可能同时建同名的，所以不报错，用已有的那条。"""
    conn.execute("INSERT OR IGNORE INTO topics (project, name, aliases, created, deleted) VALUES (?, ?, '[]', ?, 0)", (project, name, now))
    r = dict(conn.execute("SELECT * FROM topics WHERE project=? AND name=?", (project, name)).fetchone())
    r["aliases"] = json.loads(r["aliases"] or "[]")
    return r


def ensure_topic(conn, project, name, now):
    """按名字找这个项目的主题（含改名前的旧名），删掉的恢复，没有就新建。返回 id。"""
    hit = find_topic(load_topics(conn), project, name)
    if hit:
        if hit["deleted"]:
            conn.execute("UPDATE topics SET deleted=0 WHERE id=?", (hit["id"],))
        return hit["id"]
    return insert_topic(conn, project, name, now)["id"]


GENERIC_PROJECTS = ("桌面", "主目录", "其他")   # 杂项目录：在这里开的会话常常是别的项目的事


def topic_by_tag(conn, rows, project, tags, now):
    """消息里的 #标签 对到主题：从最近写的往前找，用第一个认得出的（各种 kind 见 note_hashtag），认不出的随口一提不会把会话挤出原来的主题。
    先认本项目里同名的（含改名前的旧名）；中文连着写的再认名字是它开头的；会话在桌面这类杂项目录时，再认别的项目里唯一同名的。
    都认不出、标签又写得完整（kind=new）时，在本项目下新建一个。认出来的是删掉的主题就算未归类，标签不会把它重新建出来。"""
    for tag in reversed(tags):
        name = tag["name"].lower()
        hit = find_topic(rows, project, name)
        if hit is None and tag["kind"] == "prefix":
            pre = [(len(n), r) for r in rows if r["project"] == project and not r["deleted"]
                   for n in topic_names(r) if len(n) >= 2 and name.startswith(n)]
            hit = max(pre, key=lambda x: x[0])[1] if pre else None
        if hit is None and project in GENERIC_PROJECTS:
            other = [r for r in rows if not r["deleted"] and name in topic_names(r)]
            hit = other[0] if len(other) == 1 else None
        if hit is not None:
            return None if hit["deleted"] else hit
        if tag["kind"] == "new":
            hit = insert_topic(conn, project, tag["name"], now)
            rows.append(hit)
            return hit
    return None


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


def session_extras(conn, key):
    g = conn.execute("SELECT goal, criteria, status FROM goals WHERE session_key=?", (key,)).fetchone()
    n = conn.execute("SELECT COUNT(DISTINCT " + OUT_KEY.replace("l.", "") + ") FROM links WHERE session_key=? AND cat != 'mr'", (key,)).fetchone()[0]
    return {
        "goal": g["goal"] if g else "", "criteria": g["criteria"] if g else "",
        "goalStatus": GOAL_STATUS.get(g["status"], g["status"]) if g and g["status"] else "",
        "linksTotal": n,
    }


GOAL_STATUS = {"active": "进行中", "paused": "已暂停", "blocked": "卡住了", "usage_limited": "额度用完暂停",
               "budget_limited": "预算用完暂停", "complete": "已完成"}


def prompt_rows_to_items(conn, rows, now):
    titles, _ = codex_extras()
    threads = codex_threads()
    names, tags, moved = name_map(conn), tag_map(conn), load_moved(conn)
    out = []
    for r in rows:
        tool, sid = r["session_key"].split(":", 1)
        title = names.get(r["session_key"]) or r["title"]
        if names.get(r["session_key"]):
            pass
        elif tool == "codex":
            title = readable(titles.get(sid)) or readable(threads.get(sid, ("", "", ""))[2]) or title
        elif not title:
            got = conn.execute("SELECT data FROM sessions WHERE key=?", (r["session_key"],)).fetchone()
            if got:
                st = json.loads(got["data"])
                title = st.get("custom_title") or readable(st.get("ai_title"))
        if not title:
            fp = conn.execute("SELECT text FROM prompts WHERE session_key=? ORDER BY ts LIMIT 1", (r["session_key"],)).fetchone()
            if fp:
                title = prompt_title(fp["text"], 30)
        project = moved.get(r["session_key"]) or r["project"]
        label, color = tag_of(tags, project)
        out.append({
            "key": r["session_key"], "tool": tool, "toolLabel": "Claude" if tool == "claude" else "Codex",
            "ts": r["ts"], "timeText": fmt_time(r["ts"], now), "text": r["text"], "project": label,
            "projectKey": project, "color": color, "title": title or "（未命名会话）", "images": [], "auto": auto_of(r["session_key"]),
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


def handoff_snippet(text, q):
    """交接里第一个命中词前后各取一段，压成一行，给搜索结果当预览。"""
    low, at = text.lower(), -1
    for w in [w for w in (q or "").split() if w]:
        i = low.find(w.lower())
        if i >= 0 and (at < 0 or i < at):
            at = i
    a = max(0, at - 80)
    out = re.sub(r"\s+", " ", re.sub(r"[#*`]+", "", text[a:max(at, 0) + 320])).strip()
    return ("…" if a else "") + out + ("…" if max(at, 0) + 320 < len(text) else "")


def search_sessions(conn, q, limit=50, offset=0, tool="", cross=True, handoff=False):
    """按会话汇总：有命中的会话按最近一次命中排序，每个会话带最多 3 条命中预览。tool = claude / codex 时只看这个工具的。
    cross：搜第一页时顺带搜产出物，带回总数和前 3 个，页面上两边一起列（不用先想清楚是你说的还是 AI 给的）。
    handoff：也搜 Claude 压缩时的交接（「全部」标签用）。只在交接里对上的会话，命中数记 0，排序按交接写的时间。"""
    now = time.time()
    conds, args = _like_where(q)
    by_tool = (" WHERE tool=?", [tool]) if tool in ("claude", "codex") else ("", [])
    if by_tool[1]:
        conds.append("tool=?")
        args.append(tool)
    sql_where = (" WHERE " + " AND ".join(conds)) if conds else ""
    prompt_total = conn.execute("SELECT COUNT(DISTINCT session_key) FROM prompts" + sql_where, args).fetchone()[0]
    grouped, gargs = "SELECT session_key, COUNT(*) AS hits, MAX(ts) AS last_ts FROM prompts" + sql_where + " GROUP BY session_key", list(args)
    words = [w for w in (q or "").split() if w][:6] if handoff and tool != "codex" else []
    hwhere = " AND ".join(["text LIKE ? ESCAPE '\\'"] * len(words))
    hargs = ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in words]
    if words:
        grouped = ("SELECT session_key, SUM(hits) AS hits, MAX(last_ts) AS last_ts FROM (" + grouped +
                   " UNION ALL SELECT session_key, 0, ts FROM handoffs WHERE " + hwhere + ") GROUP BY session_key")
        gargs += hargs
    total = conn.execute("SELECT COUNT(*) FROM (" + grouped + ")", gargs).fetchone()[0] if words else prompt_total
    rows = conn.execute(grouped + " ORDER BY last_ts DESC LIMIT ? OFFSET ?", gargs + [limit, offset]).fetchall()
    out = []
    for r in rows:
        key = r["session_key"]
        n = conn.execute("SELECT COUNT(*) FROM prompts WHERE session_key=?", (key,)).fetchone()[0]
        extra = (" AND " + " AND ".join(conds)) if conds else ""
        prev = conn.execute("SELECT * FROM prompts WHERE session_key=?" + extra + " ORDER BY ts DESC LIMIT ?",
                            [key] + args + [8 if conds else 4]).fetchall() if r["hits"] or not conds else []
        items = prompt_rows_to_items(conn, prev, now)[:3 if conds else 1]
        if items:
            head = items[0]
        else:   # 只在交接里对上：标题、项目按这个会话最近一句取
            last = prompt_rows_to_items(conn, conn.execute("SELECT * FROM prompts WHERE session_key=? ORDER BY ts DESC LIMIT 1", (key,)).fetchall(), now)
            head = last[0] if last else dict(session_briefs(conn, [key], now)[key], resume="")
        ho = None
        if words:
            h = conn.execute("SELECT ts, text FROM handoffs WHERE session_key=? AND " + hwhere, [key] + hargs).fetchone()
            if h:
                ho = {"ts": h["ts"], "text": handoff_snippet(h["text"], q)}
        ex = session_extras(conn, key)
        out.append({"key": key, "title": head["title"], "project": head["project"], "projectKey": head["projectKey"],
                    "color": head["color"], "tool": head["tool"], "toolLabel": head["toolLabel"], "auto": head["auto"], "count": n,
                    "hits": r["hits"], "lastTs": r["last_ts"], "lastText": fmt_time(r["last_ts"], now), "resume": head["resume"],
                    "goal": ex["goal"], "linksTotal": ex["linksTotal"], "handoff": ho,
                    "preview": [{"ts": it["ts"], "timeText": it["timeText"], "text": it["text"], "images": it["images"]}
                                for it in items]})
    first = conn.execute("SELECT MIN(ts), COUNT(*), COUNT(DISTINCT session_key) FROM prompts" + by_tool[0], by_tool[1]).fetchone()
    res = {"q": q, "total": total, "promptTotal": prompt_total, "sessions": out, "all": first[1], "allSessions": first[2],
           "since": fmt_time(first[0], now) if first[0] else "", "outputHits": 0, "outputs": []}
    if cross and q.strip() and not offset:
        got = search_outputs(conn, q, tool=tool, limit=3)
        res["outputHits"], res["outputs"] = got["total"], got["items"]
    return res


_TRANSCRIPTS = {}   # 会话的对话记录文件 -> (文件大小和修改时间, 它每一轮的回复)


def transcript_replies(tool, paths):
    """重读对话记录，取它每一轮最后的回复和压缩时的交接（和扫描用同一套解析，不调 AI）。文件没变就用上次读的"""
    sig = tuple((p, st.st_size, st.st_mtime) for p in paths for st in [safe_stat(p)] if st)
    got = _TRANSCRIPTS.get(tuple(paths))
    if got and got[0] == sig:
        return got[1]
    out = []
    for p, _, _ in sorted(sig, key=lambda x: x[2]):
        st = new_state(tool, "", p)
        st["_collect"], st["_dry"], seen = out, True, None
        parse = claude_line if tool == "claude" else codex_line
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    parse(st, line)
                    ho = st.get("new_handoff")
                    if ho and ho is not seen:
                        seen = ho
                        out.append({"kind": "handoff", "ts": ho[0], "text": ho[1]})
        except OSError:
            continue
    if len(_TRANSCRIPTS) > 16:
        _TRANSCRIPTS.pop(next(iter(_TRANSCRIPTS)))
    _TRANSCRIPTS[tuple(paths)] = (sig, out)
    return out


def safe_stat(path):
    try:
        return os.stat(path)
    except OSError:
        return None


def session_prompts(conn, key):
    rows = conn.execute("SELECT * FROM prompts WHERE session_key=? ORDER BY ts", (key,)).fetchall()
    res = {"key": key, "items": prompt_rows_to_items(conn, rows, time.time())}
    res.update(session_extras(conn, key))
    return res


def _out_where(q, cat="", key="", tool=""):
    """产出物的搜索条件：每个词都要在链接 / 文件路径、真实标题、AI 写的说明和原话、会话名或项目里出现。
    cat 空着 = 只看产物（不含 MR 和流水线），any = 全都要，mr_open = 还开着的 MR（没查过的也算），其余 = 只看这一类。"""
    conds, args = [], []
    for w in [w for w in (q or "").split() if w][:6]:
        like = "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds.append("(l.url LIKE ? ESCAPE '\\' OR l.label LIKE ? ESCAPE '\\' OR l.ctx LIKE ? ESCAPE '\\' OR d.title LIKE ? ESCAPE '\\' OR m.title LIKE ? ESCAPE '\\' "
                     "OR l.session_key IN (SELECT session_key FROM names WHERE title LIKE ? ESCAPE '\\') "
                     "OR l.session_key IN (SELECT session_key FROM prompts WHERE title LIKE ? ESCAPE '\\' OR project LIKE ? ESCAPE '\\' "
                     "OR project IN (SELECT project FROM tags WHERE label LIKE ? ESCAPE '\\')))")
        args += [like] * 9
    if key:
        conds.append("l.session_key=?")
        args.append(key)
    if tool in ("claude", "codex"):
        conds.append("l.session_key LIKE ?")
        args.append(tool + ":%")
    if cat in OUT_CATS:
        conds.append("l.cat=?")
        args.append(cat)
    elif cat == "mr_open":
        conds.append("l.mr != '' AND (m.state IS NULL OR m.state IN ('opened', 'locked'))")
    elif cat != "any":
        conds.append("l.cat != 'mr'")
    return (" WHERE " + " AND ".join(conds)) if conds else "", args


OUT_FROM = (" FROM links l LEFT JOIN doc_titles d ON l.doc != '' AND d.token = l.doc"
            " LEFT JOIN mr_state m ON l.mr != '' AND m.ref = l.mr")
# 同一篇飞书文档算一个产出物：带 #块锚点、?from= 的各种写法都归到「类型:token」；别的按链接本身
OUT_KEY = "(CASE WHEN l.doc != '' THEN l.doc ELSE l.url END)"
OUT_CATS = ("feishu", "code", "file", "web", "mr")


def count_outputs(conn, q, tool=""):
    where, args = _out_where(q, tool=tool)
    return conn.execute("SELECT COUNT(DISTINCT " + OUT_KEY + ")" + OUT_FROM + where, args).fetchone()[0]


def session_briefs(conn, keys, now):
    """会话 key -> 标题、项目、工具，给产出物标出处用。标题和「我说过的话」同一套取法。"""
    rows = []
    for k in keys:
        r = conn.execute("SELECT * FROM prompts WHERE session_key=? ORDER BY ts DESC LIMIT 1", (k,)).fetchone()
        if r:
            rows.append(r)
    out = {}
    for x in prompt_rows_to_items(conn, rows, now) if rows else []:
        out[x["key"]] = {k: x[k] for k in ("key", "title", "project", "projectKey", "color", "tool", "toolLabel", "auto")}
    for k in keys:
        if k not in out:
            tool = k.split(":", 1)[0]
            got = conn.execute("SELECT data FROM sessions WHERE key=?", (k,)).fetchone()
            st = json.loads(got["data"]) if got else {}
            project = load_moved(conn).get(k) or project_of(st.get("cwd") or "")
            label, color = tag_of(tag_map(conn), project)
            out[k] = {"key": k, "title": name_map(conn).get(k) or st.get("custom_title") or readable(st.get("ai_title")) or "（未命名会话）",
                      "project": label, "projectKey": project, "color": color, "tool": tool,
                      "toolLabel": "Claude" if tool == "claude" else "Codex", "auto": auto_of(k)}
    return out


def search_outputs(conn, q, cat="", key="", tool="", limit=100, offset=0):
    """AI 给过你的产出物（链接和本机文件），同一个链接只列一次，带上给过它的会话（最近的在前）和 AI 给出它时的原话。"""
    now = time.time()
    where, args = _out_where(q, cat, key, tool)
    total = conn.execute("SELECT COUNT(DISTINCT " + OUT_KEY + ")" + OUT_FROM + where, args).fetchone()[0]
    rows = conn.execute("SELECT " + OUT_KEY + " AS g, MAX(l.ts) AS ts" + OUT_FROM + where +
                        " GROUP BY g ORDER BY ts DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
    cw, cargs = _out_where(q, "any", key, tool)
    counts = {r[0] or "web": r[1] for r in conn.execute("SELECT l.cat, COUNT(DISTINCT " + OUT_KEY + ")" + OUT_FROM + cw + " GROUP BY l.cat", cargs)}
    ow, oargs = _out_where(q, "mr_open", key, tool)
    counts["mr_open"] = conn.execute("SELECT COUNT(DISTINCT " + OUT_KEY + ")" + OUT_FROM + ow, oargs).fetchone()[0]
    by_tool = ([key], " AND l.session_key=?") if key else (([tool + ":%"], " AND l.session_key LIKE ?") if tool in ("claude", "codex") else ([], ""))
    items, keys = [], []
    for r in rows:
        by_doc = "://" not in r["g"] and not r["g"].startswith("/")   # 「类型:token」按 doc 列查，别的按链接查，都走索引
        occ = conn.execute("SELECT l.session_key, l.url, l.label, l.ctx, l.ts, l.alt, COALESCE(d.title, m.title, '') AS title, m.state" + OUT_FROM + (" WHERE l.doc=?" if by_doc else " WHERE l.url=?") + by_tool[1] +
                           " ORDER BY l.ts DESC", [r["g"]] + by_tool[0]).fetchall()
        if not occ:
            continue
        sess = []
        for o in occ:
            if all(x["key"] != o["session_key"] for x in sess):
                sess.append({"key": o["session_key"], "ts": o["ts"]})
        head = occ[0]
        label = next((o["label"] for o in occ if o["label"]), "")
        url = next((o["url"] for o in occ if "#" not in o["url"]), head["url"])   # 有不带锚点的就用它，打开是文档开头
        it = {"url": url, "cat": out_cat(url), "name": link_name(url), "title": head["title"] or "", "label": label,
              "text": out_text(url, label, head["title"]), "ctx": head["ctx"] or "", "ts": r["ts"], "timeText": fmt_time(r["ts"], now),
              "nSessions": len(sess), "sessions": sess[:3], "state": head["state"] or ""}
        if it["cat"] == "file":
            it["path"] = home_rel(url)
            it["gone"] = not os.path.exists(url)
            fb = file_fallback(url, head["alt"], resolve=False) if it["gone"] else None
            if fb:   # 所在的 worktree 被清理了：点开的是主工作区里的同名文件，或 GitLab 上默认分支的版本
                it["gone"], it["twin"] = False, fb[0]
                if fb[0] == "file":
                    it["path"] = home_rel(fb[1])
        items.append(it)
        keys += [x["key"] for x in sess[:3]]
    briefs = session_briefs(conn, list(dict.fromkeys(keys)), now)
    for it in items:
        for x in it["sessions"]:
            x.update(briefs.get(x["key"], {}))
    allc = conn.execute("SELECT COUNT(DISTINCT " + OUT_KEY + "), COUNT(DISTINCT l.session_key) FROM links l WHERE l.cat != 'mr'").fetchone()
    res = {"q": q, "cat": cat, "key": key, "total": total, "counts": counts, "items": items,
           "all": allc[0], "allSessions": allc[1], "wider": 0, "promptHits": 0}
    if q.strip():
        # 只看某个会话、某一类时没搜到：告诉你放开后有几个；你自己发过这个词的会话数，给「我说过的话」的入口
        if (key or cat) and not total:
            res["wider"] = count_outputs(conn, q, tool)
        pw, pargs = _like_where(q)
        if tool in ("claude", "codex"):
            pw, pargs = pw + ["tool=?"], pargs + [tool]
        res["promptHits"] = conn.execute("SELECT COUNT(DISTINCT session_key) FROM prompts WHERE " + " AND ".join(pw), pargs).fetchone()[0]
    return res


def out_brief(url, label, title, ts, now, state=""):
    """详情里一行产出物要的字段。state = MR 的状态：opened / merged / closed，没查过是空的。"""
    it = {"url": url, "cat": out_cat(url), "name": link_name(url), "text": out_text(url, label, title),
          "timeText": fmt_time(ts, now) if ts else "", "state": state or ""}
    if it["cat"] == "file":
        it["path"] = home_rel(url)
    return it


def pending_docs(conn, now, limit=200):
    """还没取过真实标题的飞书文档（新给的排前面）；上次没取到的（没权限、删了）一周后再试。"""
    return [r[0] for r in conn.execute(
        "SELECT l.doc FROM links l LEFT JOIN doc_titles d ON d.token = l.doc WHERE l.doc != '' "
        "AND (d.token IS NULL OR (d.title = '' AND d.at < ?)) GROUP BY l.doc ORDER BY MAX(l.ts) DESC LIMIT ?",
        (now - 7 * 86400, limit))]


def pending_mrs(conn, now, limit=300):
    """要查合并状态的 MR（新给的排前面）：没查过的；开着的、最近 30 天给过的，15 分钟查一次；上次没查到的 6 小时后再试。
    合并了、关了的不再查。"""
    return [r[0] for r in conn.execute(
        "SELECT l.mr FROM links l LEFT JOIN mr_state m ON m.ref = l.mr WHERE l.mr != '' "
        "AND (m.ref IS NULL OR (m.state IN ('opened', 'locked') AND m.at < ?) OR (m.state = '' AND m.at < ?)) "
        "GROUP BY l.mr HAVING m.ref IS NULL OR m.state = '' OR MAX(l.ts) >= ? ORDER BY MAX(l.ts) DESC LIMIT ?",
        (now - 900, now - 6 * 3600, now - 30 * 86400, limit))]


def fetch_mr_states(refs):
    """用 glab 以你的身份按项目批量查 MR（一次 40 个，多了 GitLab 会回 502；不调 AI）。返回 {ref: (状态, 标题)}，查不到的是 ("", "")；
    glab 没装或一个都没查成返回 None。"""
    if not (os.path.isabs(GLAB) and os.access(GLAB, os.X_OK)):
        return None
    groups = {}
    for ref in refs:
        where, iid = ref.rsplit("!", 1)
        groups.setdefault(tuple(where.split("/", 1)), []).append(iid)
    got, ok = {}, False
    for (host, proj), iids in groups.items():
        for i in range(0, len(iids), 40):
            part = iids[i:i + 40]
            r = run([GLAB, "api", "--hostname", host, "projects/%s/merge_requests?%s&per_page=100"
                     % (quote_url(proj, safe=""), "&".join("iids[]=" + x for x in part))], timeout=30)
            try:
                res = json.loads(r.stdout or "") if r.returncode == 0 else None
            except ValueError:
                res = None
            ok = ok or isinstance(res, list)
            seen = {str(m.get("iid")): m for m in res if isinstance(m, dict)} if isinstance(res, list) else {}
            for x in part:
                m = seen.get(x) or {}
                got["%s/%s!%s" % (host, proj, x)] = (str(m.get("state") or "")[:12], str(m.get("title") or "").strip()[:200])
    return got if ok else None


def fetch_doc_titles(docs):
    """用 lark-cli 以你的身份批量查飞书文档的标题（一次最多 200 篇，不调 AI）。返回 {「类型:token」: 标题}，查不了返回 None。"""
    if not (os.path.isabs(LARK) and os.access(LARK, os.X_OK)):
        return None
    req = [{"doc_token": d.split(":", 1)[1], "doc_type": d.split(":", 1)[0]} for d in docs]
    r = run([LARK, "api", "POST", "/open-apis/drive/v1/metas/batch_query", "--as", "user",
             "--data", json.dumps({"request_docs": req})], timeout=40)
    try:
        res = json.loads(r.stdout or "")
    except ValueError:
        return None
    if not isinstance(res, dict) or not res.get("ok"):
        return None
    got = {}
    for m in (res.get("data") or {}).get("metas") or []:
        info = m.get("request_doc_info") or {}
        tok, typ = info.get("doc_token") or m.get("doc_token"), info.get("doc_type") or m.get("doc_type")
        if tok and typ:
            got["%s:%s" % (typ, tok)] = (m.get("title") or "").strip()[:200]
    return got


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


# 收纳文件夹：本身不算项目，里面的每个子文件夹各算一个。和 config 里的 project_parents 一起按「最具体的那层」认。
BUILTIN_PARENTS = [os.path.join(HOME, x) for x in (
    "Desktop", "Documents", "Documents/ChatGPT", "Documents/Codex", "Nutstore Files", "Nutstore Files/Nutstore", "Dropbox")]
ALL_PARENTS = sorted(set(PROJECT_PARENTS + BUILTIN_PARENTS), key=len, reverse=True)
CODEX_WORKTREES = os.path.join(CODEX_HOME, "worktrees")
_wt_main = {}   # Codex 工作树目录 → 主仓库目录


def codex_worktree_main(wt, repo):
    """Codex 开的工作树（~/.codex/worktrees/<名字>/<仓库>）对应的主仓库目录：读工作树的 .git 文件，里面写着主仓库在哪。
    工作树已经删了的，借同名仓库别的工作树查；都查不到返回空。只读 ~/.codex 下的文件，不碰桌面。"""
    if wt not in _wt_main:
        for g in [os.path.join(wt, ".git")] + sorted(glob.glob(os.path.join(CODEX_WORKTREES, "*", glob.escape(repo), ".git"))):
            try:
                with open(g, encoding="utf-8") as f:
                    m = re.match(r"gitdir:\s*(/.+?)/\.git/worktrees/", f.read(4096))
            except (OSError, ValueError):
                continue
            if m:
                _wt_main[wt] = m.group(1)
                break
    return _wt_main.get(wt, "")


def project_name(name):
    """文件夹名 → 项目名：去掉工作树后缀；隐藏目录、只是个日期的（Codex 自动建的临时目录）算「其他」。"""
    name = re.sub(r"(-wt-.*|-worktrees)$", "", name)
    return "其他" if not name or name.startswith(".") or re.fullmatch(r"\d{4}-\d{2}-\d{2}", name) else name


USER_DIRS = []   # 你在看板「位置」那一行点过的文件夹：每个就是一个项目，里面开的会话都归它，比上面两种都优先。存在 meta 的 project_dirs


def set_user_dirs(dirs):
    global USER_DIRS
    USER_DIRS = sorted({d for d in dirs if isinstance(d, str) and d.startswith("/")}, key=len, reverse=True)
    return USER_DIRS


def load_user_dirs(conn):
    try:
        got = json.loads(meta_get(conn, "project_dirs", "[]"))
    except ValueError:
        got = []
    return set_user_dirs(got if isinstance(got, list) else [])


def save_user_dirs(conn, dirs):
    meta_set(conn, "project_dirs", json.dumps(set_user_dirs(dirs), ensure_ascii=False))


def project_info(cwd):
    """会话目录 → (项目名, 当项目的那层文件夹, 是不是你点出来的)。"""
    p = (cwd or "").rstrip("/")
    for _ in range(2):   # 先按原目录认你点过的；Codex 工作树再按主仓库认一遍
        mine = next((d for d in USER_DIRS if p == d or p.startswith(d + "/")), None)
        if mine:
            return re.sub(r"(-wt-.*|-worktrees)$", "", os.path.basename(mine)) or os.path.basename(mine), mine, True
        if not p.startswith(CODEX_WORKTREES + "/"):
            break
        parts = p[len(CODEX_WORKTREES) + 1:].split("/")
        if len(parts) < 2:
            return "其他", p, False
        wt = os.path.join(CODEX_WORKTREES, parts[0], parts[1])
        main = codex_worktree_main(wt, parts[1])
        if not main:
            return project_name(parts[1]), wt, False
        p = main + p[len(wt):]
    for parent in ALL_PARENTS:
        if p.startswith(parent + "/"):
            child = p[len(parent) + 1:].split("/")[0]
            return project_name(child), parent + "/" + child, False
    if p == os.path.join(HOME, "Desktop"):
        return "桌面", p, False
    if not p.startswith(HOME):
        return os.path.basename(p) or "其他", p, False
    parts = [x for x in p[len(HOME):].split("/") if x]
    return (project_name(parts[0]), os.path.join(HOME, parts[0]), False) if parts else ("主目录", HOME, False)


def project_of(cwd):
    return project_info(cwd)[0]


def color_of(name):
    fixed = {"桌面": "#8f959e", "主目录": "#8f959e"}   # 其余按名字取色，可在看板上点标签改
    if name in fixed:
        return fixed[name]
    return PALETTE[sum(ord(c) for c in name) % len(PALETTE)]


def readable(t):
    """工具或 AI 自动起的名字：去掉链接；去完只剩一两个字（比如「这…」）的不算，换下一个来源。"""
    t = re.sub(r"\s+", " ", re.sub(r"https?://\S+", "", t or "")).strip()
    return t if len(re.sub(r"\W", "", t)) >= 2 else ""


def prompt_title(text, n=40):
    """没起名字的会话，拿第一句话当标题：跳过链接和图片占位，取第一行像句话的。"""
    for line in (text or "").splitlines():
        t = re.sub(r"https?://\S+", "", clean_line(line))
        t = re.sub(r"\s+", " ", t).strip(" \t[]()<>【】（）|-—:：,，.。")
        if len(re.sub(r"\W", "", t)) >= 2:
            return t[:n]
    return clean_line((text or "").strip().splitlines()[0] if (text or "").strip() else "")[:n]


def clean_line(s):
    s = re.sub(r"\[Image #\d+\]", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^\s*(#+|[-*>]|\d+[.、)])\s*", "", s)
    s = CLEAN_RE.sub("", s).strip()
    return s


def unquote(s):
    """整句本身就包在「」或引号里时去掉外层，免得再套一层「」变成「「…」」。"""
    s = s.strip()
    if len(s) > 2 and (s[0], s[-1]) in (("「", "」"), ("“", "”"), ('"', '"')) and not any(c in s[1:-1] for c in "「」“”"):
        return s[1:-1].strip()
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


def reply_text(text):
    """它最后说的：去掉 📌、🎯 两行（详情顶上已经有了），其余原样，Markdown 交给看板渲染。"""
    return "\n".join(l for l in (text or "").splitlines() if not l.strip().startswith(("📌", "🎯"))).strip()


def head_text(text, limit=800):
    """看板状态里只带开头一段（结论多在开头）；详情打开时再按 /api/reply 取全文，免得每分钟拉的状态太大。"""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def claude_live():
    """开着的 Claude 会话自己报的状态（~/.claude/sessions/<pid>.json）：busy / idle / shell / waiting。
    弹出批准框时是 waiting，waitingFor 写着在等什么。对话记录里不记批准框，只看它要等 10 分钟才猜得到。"""
    found = {}
    for path in glob.glob(os.path.join(CLAUDE_LIVE, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            os.kill(int(d["pid"]), 0)   # 进程已经退出的残留文件不算
        except (OSError, ValueError, KeyError, TypeError):
            continue
        sid = d.get("sessionId")
        if sid and (d.get("updatedAt") or 0) >= (found.get(sid, {}).get("updatedAt") or 0):
            found[sid] = d
    return found


WAITING_FOR = {
    # Claude Code 把 input needed 归为提问：AskUserQuestion 弹出的选项、MCP 要你填的表单都是它。
    # 提问那一条 tool_use 要等你回答后才写进对话记录，pending_tool 这时还是上一个工具，只能靠它认
    "input needed": "它问了你一个问题，在等你回答",
    "goal proposal": "它提了一个目标，在等你确认",
    "sandbox request": "在等你批准一个沙箱外的访问",
    "worker request": "后台任务在等你批准一个操作",
    "dialog open": "开着一个对话框，在等你处理",
}


def waiting_headline(what, tool):
    if what == "permission prompt" or not what:
        if tool == "AskUserQuestion":
            return "它问了你一个问题，在等你回答"
        if tool == "ExitPlanMode":
            return "计划写好了，在等你确认"
        return ("在等你批准一个操作（%s）" % tool) if tool else "在等你批准一个操作"
    return WAITING_FOR.get(what) or "在等你处理：「%s」" % what


# 停在哪种要你点头的地方。问题、计划、要批准的操作都只显示在会话里，看板据此写「打开会话去回答 / 去批准 / 去确认」
WAIT_KIND = {"input needed": "question", "goal proposal": "goal", "sandbox request": "permission", "worker request": "permission"}


def wait_kind(what, tool):
    if what == "permission prompt" or not what:
        return {"AskUserQuestion": "question", "ExitPlanMode": "plan"}.get(tool, "permission")
    return WAIT_KIND.get(what, "other")


# AI 自己调起的 Codex：codex exec（命令行跑一次）、mcp（别的 AI 把 Codex 当工具用）。thread_source 照样写 user，要看 source 认
CODEX_AGENT_SOURCES = ("exec", "mcp")


def codex_source(meta):
    """session_meta 里的 source：桌面版是 vscode，终端是 cli；子代理和审查代理是一个对象，这里记成空，它们靠 thread_source 排除。"""
    src = meta.get("source")
    return src if isinstance(src, str) else ""


def include_session(st):
    # 只收你自己开的会话，AI 自己调起的子任务不收：
    # Claude 的子代理记录在 subagents/ 子目录，不扫描；claude -p 和 SDK 的 entrypoint 是 sdk-*。
    # Codex 的子代理、审查代理 thread_source 不是 user；codex exec 和 MCP 调起的看 source。
    # Codex 的自动任务（心跳）挂在你自己开的线程上，那条线程照样是你的任务；心跳发的 <heartbeat> 消息不算你说的话
    if st["tool"] == "claude":
        if (st.get("entrypoint") or "").startswith("sdk"):
            return False
    elif st.get("thread_source") != "user" or st.get("source") in CODEX_AGENT_SOURCES:
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
    title = (ctx["names"].get(key) or st.get("custom_title") or readable(titles.get(st["id"])) or readable(st.get("ai_title")) or goal_obj.get("goal")
             or prompt_title(st.get("first_prompt"))
             or "（无标题）")
    home, home_dir, home_mine = project_info(st.get("cwd"))
    project = ctx["moved"].get(key) or home
    project_label, project_color = tag_of(ctx["tags"], project)
    saved = ctx["goals"].get(key) or {}
    # 目标取最新的一份：压缩时的交接比 🎯 那行新，就用交接里「### 目标」写的（🎯 多半是压缩前某个小任务的）
    goal_text = goal_obj.get("goal") or codex_goal.get("objective") or saved.get("goal") or ""
    goal_crit = goal_obj.get("criteria") or saved.get("criteria") or ""
    goal_ts, goal_from = st.get("goal_ts") or saved.get("at") or 0, ""
    ho_ts, ho_goal = ctx["handoffs"].get(key) or 0, ctx["ho_goals"].get(key)
    if ho_goal and (not goal_text or ho_ts > goal_ts + 60):
        goal_text, goal_crit, goal_ts, goal_from = ho_goal[0], ho_goal[1], ho_ts, "handoff"
    reply_hint = excerpt(st.get("last_text")) or excerpt(st.get("progress_text"))
    badges = []
    group, kind, headline, sort, wait = "need", "me", "", 0, ""
    stale_sec = cfg["stale_hours"] * 3600
    live = ctx["live"].get(st["id"]) if st["tool"] == "claude" else None
    busy = bool(live) and live.get("status") == "busy"   # 命令跑得久，不是卡住了
    waiting = bool(live) and live.get("status") == "waiting" and (
        live.get("waitingFor") != "dialog open" or turn == "running")   # 你自己在空闲时打开的对话框不算

    if ov and ov["action"] == "untrack":
        group, headline = "hidden", "不再跟踪"
    elif ov and ov["action"] in ("done", "closed") and last <= (ov["at"] or 0) + 120:
        group, headline = "hidden", "已完成" if ov["action"] == "done" else "关了 cmux 标签页，自动标为完成"
    elif waiting:
        group, kind, sort = "need", "approval", 0
        tool = st.get("pending_tool") if turn == "running" else ""
        headline, wait = waiting_headline(live.get("waitingFor"), tool), wait_kind(live.get("waitingFor"), tool)
    elif turn == "running" and st.get("last_kind") == "tool_use" and not busy and (
            st.get("pending_tool") in ("AskUserQuestion", "ExitPlanMode") or idle > 600):
        group, kind, sort = "need", "approval", 0
        tool = st.get("pending_tool") or "操作"
        if tool == "AskUserQuestion":
            headline, wait = "它问了你一个问题，在等你回答", "question"
        elif tool == "ExitPlanMode":
            headline, wait = "计划写好了，在等你确认", "plan"
        else:
            headline, wait = "可能在等你批准一个操作（%s），也可能命令还在跑（已 %s）" % (tool, fmt_ago(idle)), "maybe"
    elif turn == "running":
        if idle > cfg["interrupted_min"] * 60 and not busy:
            group, kind, sort = "need", "interrupted", 0
            if st.get("last_kind") == "user":
                ask = unquote(clean_line((st.get("last_prompt") or "").splitlines()[0] if st.get("last_prompt") else ""))[:40].rstrip(" ：:，,")
                headline = "你 %s 发的消息，它一直没回：「%s」" % (fmt_hm(last, now), ask)
            else:
                headline = "%s前停在半路，可能已中断，点「打开会话」接着做" % fmt_ago(idle)
        else:
            group, kind = "ai", "ai"
            ask = unquote(clean_line((st.get("last_prompt") or "").splitlines()[0] if st.get("last_prompt") else ""))[:40].rstrip(" ：:，,")
            prog = excerpt(st.get("progress_text"), 60)
            headline = ("在做「%s」" % ask) if ask else ("在做：" + prog if prog else "AI 在做")
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
        if k == "me":
            # 「等你」就是现在轮到你，后面的时间不管：AI 常把它估的「现在几点」填进去，估晚了会被当成将来、压在「在等」里。要你到点再做的，它该写「等时间点」
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
                headline = "%s 提醒：%s" % (fmt_hm(at, now), desc)
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

    cwd = st.get("cwd") or ""
    if st["tool"] == "codex" and cwd.startswith(CODEX_WORKTREES + os.sep) and not os.path.isdir(cwd):
        # Codex 过一阵会删掉不活跃线程的 worktree，对话记录里不留痕迹；接着做要先在 Codex 里点「Restore worktree」
        badges.append("worktree 已清理")
    if group in ("unknown", "stale"):
        badges.insert(0, "状态不明" if group == "unknown" else "%s没动静" % fmt_ago(idle))
        group = "backlog"
    snoozed = (ov and ov["action"] == "snooze" and (ov["until_ts"] or 0) > now and last <= (ov["at"] or 0) + 120)
    if snoozed and group in ("need", "peer", "confirm", "backlog"):
        group, sort = "time", ov["until_ts"]
        headline = "推迟到 %s · %s" % (fmt_time(ov["until_ts"], now), headline)
    done_ask = False
    if decisions and group != "hidden":
        badges.insert(0, "%d 件待你决定" % len(decisions))
        if group == "confirm":
            # 说做完了，又问了你要拍板的事：球在你这边，按「等你」排（红色），不按「待你确认完成」
            group, kind, sort, done_ask = "need", "me", 2, True
    else:
        decisions = []
    # 之前问过、你还没答的 ❓：只加徽标，不挪组（最新 📌 说了算）。点了「不用答了」、会话点过完成的收起来；它再问就再列出来
    old_asks = []
    if group != "hidden":
        cur = {d["k"] for d in decisions}
        gone = ctx["ask_dismiss"].get(key, {})
        closed = (ov["at"] or 0) if ov and ov["action"] in ("done", "closed") else 0
        old_asks = sorted(({"k": a["k"], "q": a["q"], "opts": a.get("opts") or [], "id": a.get("id") or "", "block": a.get("block") or "",
                            "rec": a.get("rec") or "", "why": a.get("why") or "", "first": a.get("first") or 0}
                           for a in st.get("asks") or []
                           if not a.get("done") and a["k"] not in cur and max(gone.get(a["k"], 0), closed) < (a.get("last") or 0)),
                          key=lambda a: a["first"])[:20]
        if old_asks:
            badges.append("%d 件没答" % len(old_asks))
    # 🧭 它没问你、自己定了的事：详情里列最近的；最后一轮回复里有的，行上提一句
    calls = st.get("calls") or []
    final_ts = st.get("final_ts") or 0
    fresh_calls = sum(1 for c in calls if (c.get("ts") or 0) >= final_ts) if final_ts and final_ts >= (st.get("last_user_ts") or 0) else 0
    if fresh_calls and group != "hidden":
        badges.append("替你定了 %d 件" % fresh_calls)

    if st["tool"] == "claude":
        resume = "cd %s && claude --resume %s" % (shell_quote(st.get("cwd") or HOME), st["id"])
    else:
        resume = "codex resume %s" % st["id"]
    return {
        "key": key, "tool": st["tool"], "toolLabel": "Claude" if st["tool"] == "claude" else "Codex",
        "title": title, "project": project_label, "projectKey": project, "color": project_color, "group": group, "kind": kind,
        "moved": {"from": home, "label": tag_of(ctx["tags"], home)[0]} if project != home else None, "auto": auto_of(key),
        "headline": headline, "badges": badges, "sort": sort, "idle": idle, "last": last, "wait": wait,
        "meta": "最后动静 %s（%s前）" % (fmt_time(last, now), fmt_ago(idle)) if last else "",
        "scheduleId": sched["id"] if sched and sched["status"] == "pending" else None,
        "since": (st.get("last_user_ts") or 0) if group == "ai" else 0, "snoozed": bool(snoozed and group == "time"),
        "statusKey": "%s|%s" % ((status or {}).get("ts"), (status or {}).get("at")),
        "details": {
            "goal": goal_text, "criteria": goal_crit, "goalTs": goal_ts, "goalFrom": goal_from,
            "goalStatus": GOAL_STATUS.get(codex_goal.get("status") or saved.get("status") or "", ""),
            "originalTitle": (st.get("custom_title") or titles.get(st["id"]) or st.get("ai_title") or "")
                             if ctx["names"].get(key) else "",
            # 产物先列 8 个，展开再按会话取全部；MR 和流水线是临时的，单独列在下面
            "links": [l for l in ctx["links"].get(key, []) if l["cat"] != "mr"][:8],
            "linksTotal": sum(1 for l in ctx["links"].get(key, []) if l["cat"] != "mr"),
            "mrs": [l for l in ctx["links"].get(key, []) if l["cat"] == "mr"][:50],
            "mrsTotal": sum(1 for l in ctx["links"].get(key, []) if l["cat"] == "mr"),
            "firstPrompt": (st.get("first_prompt") or "")[:300],
            "lastPrompt": (st.get("last_prompt") or "")[:300] if st.get("last_prompt") != st.get("first_prompt") else "",
            "promptTs": st.get("last_user_ts") or 0, "nPrompts": st.get("n_prompts") or 0,
            "lastReply": head_text(reply_text(st.get("last_text") or st.get("progress_text"))),
            "replyLen": len(reply_text(st.get("last_text") or st.get("progress_text"))),
            "replyTs": st.get("final_ts") or last,
            "handoffTs": ctx.get("handoffs", {}).get(key) or 0,   # 压缩时的交接：正文较长，详情里再按 /api/handoff 取
            "status": ("📌 " + status["label"] + ("｜" + status["desc"] if status.get("desc") else "")) if status else "",
            "decisions": decisions, "doneAsk": done_ask, "oldAsks": old_asks,
            "calls": [{"t": c["t"], "why": c.get("why") or "", "ts": c.get("ts") or 0}   # 新的在前，同一轮的按原文顺序
                      for c in sorted(calls[-20:], key=lambda c: -(c.get("ts") or 0))],
            "callsFresh": fresh_calls,
            "cwd": st.get("cwd") or "", "branch": st.get("branch") or "", "id": st["id"], "resume": resume,
            "projectDir": home_dir, "projectDirMine": home_mine,   # 按哪层文件夹分的项目；Mine = 你在「位置」那一行点的
        },
    }


def shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def load_ask_dismiss(conn):
    out = {}
    for r in conn.execute("SELECT session_key, k, at FROM ask_dismiss"):
        out.setdefault(r["session_key"], {})[r["k"]] = r["at"]
    return out


ORDER_VIEWS = ("rail",)   # 拖过的顺序：rail 是左栏项目的顺序（存项目 key）。队列不再拖着排，改成置顶，见 load_pins


def load_order(conn):
    try:
        order = json.loads(meta_get(conn, "order", "{}"))
    except ValueError:
        return {}
    return {k: v for k, v in order.items() if k in ORDER_VIEWS} if isinstance(order, dict) else {}


def load_archived(conn):
    """归档的项目 -> 归档时间。归档的项目不出现在「全部」里，在左栏最下面的「归档」里看。"""
    try:
        got = json.loads(meta_get(conn, "archived", "{}"))
    except ValueError:
        return {}
    return {k: v for k, v in got.items() if isinstance(v, (int, float))} if isinstance(got, dict) else {}


def load_archived_topics(conn):
    """归档的主题 id（字符串）-> 归档时间。主题归档后不在项目下列出，它的会话也不进「全部」和项目，在「归档」里看。"""
    try:
        got = json.loads(meta_get(conn, "archived_topics", "{}"))
    except ValueError:
        return {}
    return {k: v for k, v in got.items() if isinstance(v, (int, float))} if isinstance(got, dict) else {}


def load_moved(conn):
    """移过项目的会话 key -> 项目 key。不再按文件夹算它在哪个项目，比如放错文件夹的通用定时任务，移到你自己建的「定时任务」里。"""
    try:
        got = json.loads(meta_get(conn, "moved", "{}"))
    except ValueError:
        return {}
    return {k: v for k, v in got.items() if isinstance(v, str) and v} if isinstance(got, dict) else {}


def meta_keys(conn, k):
    try:
        keys = json.loads(meta_get(conn, k, "[]"))
    except ValueError:
        return []
    return [x for x in keys if isinstance(x, str)] if isinstance(keys, list) else []


def load_pins(conn):
    """置顶的会话 key，新钉的在前。看板上置顶的排在每个标签页最上面，其余按更新时间。"""
    return meta_keys(conn, "pins")


def board_favs(conn, items, now):
    """收藏的会话，新收藏的在前。完成了、过了 30 天不在看板上的也留着。"""
    return board_refs(conn, meta_keys(conn, "favs"), items, now)


def board_autos(conn, items, now):
    """左栏「定时任务」：Codex 定时任务在跑的会话，在跑的在前、新动静的在前。会话不在看板上、也没记过你说的话的，用定时任务的名字。"""
    autos = codex_automations()
    keys = ["codex:" + tid for tid in autos]
    got = {x["key"]: x for x in board_refs(conn, keys, items, now)}
    out = [got.get(k) or {"key": k, "title": autos[k[6:]]["name"] or "（未命名定时任务）", "tool": "codex", "project": "",
                          "last": 0, "onBoard": False, "auto": autos[k[6:]]} for k in keys]
    out.sort(key=lambda x: (x["auto"]["paused"], -(x["last"] or 0)))
    return out


def board_refs(conn, keys, items, now):
    """左栏列的会话（收藏、定时任务）：在看板上的直接取；完成了、过了 30 天不在看板上的，标题和项目从「我说过的话」里取。"""
    by_key, out = {it["key"]: it for it in items}, []
    for k in keys:
        it = by_key.get(k)
        if it:
            out.append({"key": k, "title": it["title"], "tool": it["tool"], "project": it["project"], "last": it["last"], "onBoard": True, "auto": it["auto"],
                        "topic": it.get("topic")})
            continue
        row = conn.execute("SELECT * FROM prompts WHERE session_key=? ORDER BY ts DESC LIMIT 1", (k,)).fetchone()
        if row:
            x = prompt_rows_to_items(conn, [row], now)[0]
            # 不在看板上的：只认你手动放进去的主题（#主题 要读对话记录才知道）
            t = conn.execute("SELECT t.id, t.name, t.project FROM session_topics s JOIN topics t ON t.id = s.topic_id "
                             "WHERE s.session_key=? AND NOT t.deleted", (k,)).fetchone()
            out.append({"key": k, "title": x["title"], "tool": x["tool"], "project": x["project"], "last": x["ts"], "onBoard": False, "auto": x["auto"],
                        "topic": dict(t) if t else None})
    return out


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
    items, hidden, hashtags = [], [], {}
    ctx = {"names": name_map(conn), "tags": tag_map(conn), "moved": load_moved(conn), "links": {}, "live": claude_live(),
           "goals": {r["session_key"]: dict(r) for r in conn.execute("SELECT * FROM goals")},
           "handoffs": {r["session_key"]: r["ts"] for r in conn.execute("SELECT session_key, ts FROM handoffs")},
           "ho_goals": {r["session_key"]: handoff_goal(r["head"]) for r in conn.execute("SELECT session_key, substr(text, 1, 3000) AS head FROM handoffs")},
           "ask_dismiss": load_ask_dismiss(conn)}
    seen_out = set()
    for r in conn.execute("SELECT l.session_key, l.url, l.label, l.ts, l.doc, COALESCE(d.title, m.title, '') AS title, m.state" + OUT_FROM +
                          " WHERE l.session_key IN (SELECT key FROM sessions WHERE last_activity >= ?) ORDER BY l.ts DESC", (now - 30 * 86400,)):
        if (r["session_key"], r["doc"] or r["url"]) in seen_out:
            continue   # 同一篇飞书文档的别的写法（带锚点等），只留最近一次
        seen_out.add((r["session_key"], r["doc"] or r["url"]))
        ctx["links"].setdefault(r["session_key"], []).append(out_brief(r["url"], r["label"], r["title"], r["ts"], now, r["state"]))
    for r in conn.execute("SELECT data FROM sessions WHERE last_activity >= ?", (now - 30 * 86400,)):
        st = json.loads(r["data"])
        if not include_session(st):
            continue
        key = "%s:%s" % (st["tool"], st["id"])
        it = make_item(st, now, cfg, overrides.get(key), scheds.get(key), titles, goals, ctx)
        if it is None:
            continue
        hashtags[key] = st.get("hashtags")
        if it["group"] == "hidden":
            if now - it["last"] < cfg["window_days"] * 86400:
                hidden.append(it)
        else:
            items.append(it)
    topics = load_topics(conn)
    known = len(topics)
    by_id = {t["id"]: t for t in topics}
    pinned = {r["session_key"]: r["topic_id"] for r in conn.execute("SELECT session_key, topic_id FROM session_topics")}
    for it in items + hidden:
        tags = hashtags.get(it["key"])
        if it["key"] in pinned:
            t = by_id.get(pinned[it["key"]])
        else:
            t = topic_by_tag(conn, topics, it["projectKey"], tags, now) if tags else None
        it["topic"] = {"id": t["id"], "name": t["name"], "project": t["project"]} if t and not t["deleted"] else None
        it["topicPinned"] = it["key"] in pinned
    if len(topics) > known:
        conn.commit()
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
    # 归档的项目里再有会话动起来（比归档晚），项目自动回来
    archived = load_archived(conn)
    back = {p for p in archived for it in items
            if (it["topic"]["project"] if it["topic"] else it["projectKey"]) == p and it["last"] > archived[p]}
    if back:
        archived = {p: t for p, t in archived.items() if p not in back}
        meta_set(conn, "archived", json.dumps(archived, ensure_ascii=False))
        conn.commit()
    archived_topics = load_archived_topics(conn)
    back = {t for t in archived_topics for it in items if it["topic"] and str(it["topic"]["id"]) == t and it["last"] > archived_topics[t]}
    if back:
        archived_topics = {t: at for t, at in archived_topics.items() if t not in back}
        meta_set(conn, "archived_topics", json.dumps(archived_topics, ensure_ascii=False))
        conn.commit()
    # 置顶只给还在看板上的会话：完成了、不再跟踪了，就不再钉着
    pins = load_pins(conn)
    active = {it["key"] for it in items}
    if any(k not in active for k in pins):
        pins = [k for k in pins if k in active]
        meta_set(conn, "pins", json.dumps(pins, ensure_ascii=False))
        conn.commit()
    last_scan = float(meta_get(conn, "last_scan", 0) or 0)
    return {
        "build": board_build(), "now": now, "nowText": "%d月%d日 周%s %s" % (clock(now).month, clock(now).day, WEEK[clock(now).weekday()], clock(now).strftime("%H:%M")),
        "lastScan": last_scan, "lastScanText": fmt_time(last_scan, now) if last_scan else "从未",
        "tzOffsetMin": int(clock(now).astimezone(WORK_TZ).utcoffset().total_seconds() // 60),
        "staleHours": cfg["stale_hours"], "interruptedMin": cfg["interrupted_min"], "windowDays": cfg["window_days"],
        "scanMs": int(meta_get(conn, "last_scan_ms", 0) or 0), "scanIntervalMin": cfg["scan_interval_min"],
        "groups": groups, "hidden": hidden, "order": load_order(conn), "pins": pins, "archived": archived, "archivedTopics": archived_topics, "favs": board_favs(conn, items + hidden, now), "autos": board_autos(conn, items + hidden, now), "digestTime": cfg["digest_time"], "tzText": tz_text(cfg), "home": HOME, "projectDirs": USER_DIRS,
        "topics": [{"id": t["id"], "name": t["name"], "project": t["project"], "projectLabel": tag_of(ctx["tags"], t["project"])[0]}
                   for t in topics if not t["deleted"]],
    }


# ---------------------------------------------------------------- cmux 会话定位、打开、唤醒

# 看板自己存的 cmux 套接字密码（cmux-setup 写，只有你的账号能读）。新版 cmux 会把密码从 cmux.json 挪进它自己的设置，
# 开机自启的服务调命令行时读不到，所以看板另存一份，每次经 CMUX_SOCKET_PASSWORD 带上
CMUX_PW_FILE = os.path.join(DATA_DIR, "cmux-password")


def cmux_password():
    try:
        with open(CMUX_PW_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def save_cmux_password(pw):
    os.makedirs(DATA_DIR, exist_ok=True)
    fd = os.open(CMUX_PW_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(pw + "\n")
    os.chmod(CMUX_PW_FILE, 0o600)


def cmux(*args, timeout=10, password=None):
    pw = cmux_password() if password is None else password
    return run([CMUX] + list(args), timeout=timeout, env=dict(SUB_ENV, CMUX_SOCKET_PASSWORD=pw) if pw else None)


_surface_cache = {"at": 0, "map": {}}


def cmux_denied():
    return "cmux 不让看板控制它，没法替你跳转：在终端里运行 %s，按提示设置一次" % ledger_cmd("cmux-setup")


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


def cmux_title_match(titles, taken):
    """cmux 没记下会话 ID 的标签页（cmux 装好钩子之前就开着的会话，恢复记录是空的）：按标签页标题认。
    Claude 会把终端标题设成会话标题；只认唯一一个、且没被别的会话认走的标签页，两个同名就不猜。"""
    titles = {t.strip() for t in titles if t and t.strip()}
    if not titles:
        return None
    try:
        tree = json.loads(cmux("tree", "--all", "--json").stdout)
    except ValueError:
        return None
    hits = []
    for win in tree.get("windows") or []:
        for w in win.get("workspaces") or []:
            for pane in w.get("panes") or []:
                for sf in pane.get("surfaces") or []:
                    if sf.get("type") == "terminal" and (sf.get("title") or "").strip() in titles and sf.get("ref") not in taken:
                        hits.append((w.get("ref"), sf.get("ref")))
    return hits[0] if len(hits) == 1 and hits[0][0] else None


def surface_checkpoint(ws, sf):
    r = cmux("surface", "resume", "show", "--json", "--workspace", ws, "--surface", sf)
    try:
        rec = (json.loads(r.stdout) or {}).get("restore_record") or {}
        return rec.get("checkpoint_id")
    except ValueError:
        return None


def close_cmux_tab(conn, key):
    """看板上点了「完成」：把这个会话在 cmux 里的标签页也关掉，开着的标签页就都是还要跟的。
    AI 还没停下来（在跑、在等你批准）的不关；工作区里还有别的标签页（比如浏览器、另一个会话）就只关它自己；分组的标题工作区不动。
    关之前要 cmux 自己的两份记录对得上：标签页的恢复记录，和状态文件里这个工作区跑的 AI 会话。对不上就不关（终端号会被复用，不拿它认）。
    返回给提示条的半句话，没在 cmux 里开着的返回空。"""
    tool, _, sid = key.partition(":")
    if not os.path.exists(CMUX):
        return ""
    live = claude_live().get(sid) if tool == "claude" else None
    miss = "没找到它在 cmux 里的标签页，没关" if live else ""
    try:
        with open(CMUX_STATE, encoding="utf-8") as f:
            wins = json.load(f).get("windows") or []
        tms = [win.get("tabManager") or {} for win in wins]
        anchors = {g.get("anchorWorkspaceId") for tm in tms for g in tm.get("workspaceGroups") or []}
        homes = {w.get("workspaceId") for tm in tms for w in tm.get("workspaces") or [] for p in w.get("panels") or []
                 if ((p.get("terminal") or {}).get("agent") or {}).get("sessionId") == sid}
    except (OSError, ValueError, AttributeError):
        return miss
    if not homes:
        return miss
    row = conn.execute("SELECT data FROM sessions WHERE key=?", (key,)).fetchone()
    st = json.loads(row["data"]) if row else {}
    if (live.get("status") not in ("idle", None)) if live else (st.get("turn") == "running" and time.time() - (st.get("last_ts") or 0) < 600):
        return "它还没停下来，终端标签页没关"
    surfaces = cmux_surfaces()
    if surfaces is None:
        return "cmux 不让看板控制它，终端标签页没关"
    loc = surfaces.get(sid)
    if not loc or surface_checkpoint(*loc) != sid:
        return miss
    try:
        tree = json.loads(cmux("tree", "--all", "--json").stdout or "")
        w = next(w for win in tree.get("windows") or [] for w in win.get("workspaces") or [] if w.get("ref") == loc[0])
    except (ValueError, AttributeError, StopIteration):
        return miss
    if w.get("id") not in homes:
        return "cmux 里的记录对不上，终端标签页没关"
    if w.get("id") in anchors:
        return "它是 cmux 分组的标题标签，没替你关"
    if sum(len(p.get("surfaces") or []) for p in w.get("panes") or []) > 1:
        r = cmux("close-surface", "--surface", loc[1], "--workspace", loc[0], "--force")
    else:
        r = cmux("workspace", "close", "--workspace", loc[0], "--force")
    _surface_cache["at"] = 0
    if r.returncode != 0:
        log("关 cmux 标签页失败：%s %s" % (key, (r.stderr or r.stdout).strip()[:200]))
        return "终端标签页没关成"
    # 不等 cmux 的状态文件：先从「开着的」里拿掉，免得下次核对时当成你在 cmux 里关的，撤销完成后又被标回去
    conn.execute("DELETE FROM cmux_open WHERE session_key=?", (key,))
    conn.commit()
    return "终端标签页也关了"


def resume_in_new_workspace(st, title):
    sid, cwd = st["id"], st.get("cwd") or HOME
    if not UUID_RE.match(sid):
        return None
    name = re.sub(r"[\r\n]+", " ", title)[:40]
    r = cmux("new-workspace", "--name", name, "--cwd", cwd, "--command", "claude --resume " + sid, "--focus", "true")
    _surface_cache["at"] = 0
    m = re.search(r"workspace:\d+", r.stdout or "")
    return m.group(0) if m else ("" if r.returncode == 0 else None)


def rename_session(conn, key, title):
    """看板上给会话改名：看板、Claude / Codex 自己、cmux 标签页一起改。不调 AI，只追加或改一条记录，
    和 Claude 的 /rename、Codex 的重命名写的一样；哪处没改成就跳过，结果里说一声。"""
    tool, _, sid = key.partition(":")
    if tool not in ("claude", "codex") or not UUID_RE.match(sid):
        return False, "会话 ID 不对"
    now = time.time()
    # 看板的标题从 Claude / Codex 自己的名字读，以后在它们里面改名看板也跟着变；
    # 只有 cmux 标签页上的名字排在它们前面，那份也改掉（下次核对 cmux 时会按标签页新名字再同步）
    conn.execute("UPDATE names SET title=?, at=? WHERE session_key=?", (title, now, key))
    done, miss = [], []

    def append_line(path, rec):
        with open(path, "ab+") as f:
            f.seek(0, os.SEEK_END)
            if f.tell():
                f.seek(-1, os.SEEK_END)
                lead = b"" if f.read(1) == b"\n" else b"\n"
            else:
                lead = b""
            f.write(lead + (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))

    if tool == "claude":
        paths = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", sid + ".jsonl"))
        try:
            if not paths:
                raise OSError("找不到对话记录")
            append_line(paths[0], {"type": "custom-title", "customTitle": title, "sessionId": sid})
            done.append("Claude")
            row = conn.execute("SELECT data FROM sessions WHERE key=?", (key,)).fetchone()
            if row:   # 看板马上显示新名字，不用等下次扫描读到这一行
                st = json.loads(row["data"])
                st["custom_title"] = title
                conn.execute("UPDATE sessions SET data=? WHERE key=?", (json.dumps(st, ensure_ascii=False), key))
        except OSError as e:
            log("改 Claude 会话名失败：%s" % e)
            miss.append("Claude")
    else:
        ok = 0
        try:   # Codex 界面显示的名字
            c = sqlite3.connect(os.path.join(CODEX_HOME, "state_5.sqlite"), timeout=3)
            ok += c.execute("UPDATE threads SET name=? WHERE id=?", (title, sid)).rowcount
            c.commit()
            c.close()
        except sqlite3.Error as e:
            log("改 Codex 线程名失败：%s" % e)
        try:   # Codex 的会话索引，看板也从这里读
            append_line(os.path.join(CODEX_HOME, "session_index.jsonl"), {"id": sid, "thread_name": title,
                        "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")})
            ok += 1
        except OSError as e:
            log("写 Codex 会话索引失败：%s" % e)
        (done if ok else miss).append("Codex")
    surfaces = cmux_surfaces()
    loc = surfaces.get(sid) if surfaces else None
    if loc:
        r = cmux("workspace-action", "--action", "rename", "--workspace", loc[0], "--title", title)
        (done if r.returncode == 0 else miss).append("cmux 标签页")
    if tool.capitalize() in miss:
        # 工具里没改成：看板自己记一份，至少看板上是新名字
        conn.execute("INSERT INTO names (session_key, title, at) VALUES (?, ?, ?) ON CONFLICT(session_key) "
                     "DO UPDATE SET title=excluded.title, at=excluded.at", (key, title, now))
    conn.commit()
    if not done:
        return True, "只改了看板上的名字，" + "、".join(miss) + " 里没改成"
    return True, "已改名，" + "、".join(done) + " 里也改了" + ("；" + "、".join(miss) + " 没改成" if miss else "")


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
        return False, cmux_denied()
    loc = surfaces.get(st["id"])
    if not loc and st["id"] in claude_live():
        # 还开着，但 cmux 没记它的会话 ID：按标签页标题找
        loc = cmux_title_match([name_map(conn).get(key), st.get("custom_title"), st.get("ai_title")], {v[1] for v in surfaces.values()})
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
            return False, cmux_denied()
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
            try:
                sync_cmux(self.conn)   # 关掉的 cmux 标签页不等下次扫描：读的是 cmux 自己的文件，没变就只 stat 一下
                self.conn.commit()
            except Exception:
                log("sync_cmux 出错：%s" % traceback.format_exc())
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
                # 完成、不再跟踪都把 cmux 里的标签页顺手关掉（关不了的会说为什么）
                tabs = [close_cmux_tab(c, k) for k in keys]
                if len(keys) == 1:
                    return True, ("完成了，已移出看板" if action == "done" else "已不再跟踪") + ("，" + tabs[0] if tabs[0] else "")
                shut = sum(t == "终端标签页也关了" for t in tabs)
                left = sum(bool(t) and t != "终端标签页也关了" for t in tabs)
                return True, "已不再跟踪 %d 个" % len(keys) + ("，关了 %d 个终端标签页" % shut if shut else "") + ("，%d 个没关" % left if left else "")
            if action == "snooze":
                until = snooze_until(arg, now)
                c.execute("INSERT OR REPLACE INTO overrides (key, action, until_ts, at) VALUES (?, 'snooze', ?, ?)", (key, until, now))
                c.commit()
                return True, "已推迟到 " + fmt_time(until, now)
            if action == "archive" and isinstance(arg, dict) and (arg.get("project") or arg.get("topic") is not None):
                # {project} 归档项目，{topic: id} 归档主题；on=false 取消
                mk, k = ("archived", str(arg["project"])) if arg.get("project") else ("archived_topics", str(arg["topic"]))
                archived = load_archived(c) if mk == "archived" else load_archived_topics(c)
                if arg.get("on"):
                    archived[k] = now
                else:
                    archived.pop(k, None)
                meta_set(c, mk, json.dumps(archived, ensure_ascii=False))
                c.commit()
                return True, "已归档" if arg.get("on") else "已取消归档"
            if action == "fav" and key and isinstance(arg, dict):
                favs = [k for k in meta_keys(c, "favs") if k != key]
                if arg.get("on"):
                    favs.insert(0, key)
                meta_set(c, "favs", json.dumps(favs, ensure_ascii=False))
                c.commit()
                return True, "已收藏，在左栏「收藏」里" if arg.get("on") else "已取消收藏"
            if action == "pin" and key and isinstance(arg, dict):
                pins = [k for k in load_pins(c) if k != key]
                if arg.get("on"):
                    pins.insert(0, key)
                meta_set(c, "pins", json.dumps(pins, ensure_ascii=False))
                c.commit()
                return True, "已置顶" if arg.get("on") else "已取消置顶"
            if action == "topic" and isinstance(arg, dict):
                # 归到主题：{id} 归进去（id 为空 = 移出主题），{name, project} 先找或新建再归，{auto} 删掉手动的、改回按 #主题 自动归
                if arg.get("auto"):
                    c.execute("DELETE FROM session_topics WHERE session_key=?", (key,))
                    c.commit()
                    return True, "已改回按 #主题 自动归"
                tid, name = arg.get("id"), topic_name(arg.get("name"))
                if name:
                    if not arg.get("project"):
                        return False, "没说是哪个项目的主题"
                    tid = ensure_topic(c, str(arg["project"]), name, now)
                row = c.execute("SELECT name FROM topics WHERE id=? AND deleted=0", (tid,)).fetchone() if tid is not None else None
                if tid is not None and not row:
                    return False, "这个主题已经删掉了"
                if key:
                    c.execute("INSERT OR REPLACE INTO session_topics (session_key, topic_id, at) VALUES (?, ?, ?)", (key, tid, now))
                c.commit()
                return True, ("已归到「%s」" % row["name"] if key else "已新建主题「%s」" % row["name"]) if row else "已移出主题"
            if action == "project_dir" and key and isinstance(arg, dict):
                # 「位置」那一行点了一段：{path} 这层文件夹算一个项目，以后在它里面开的会话都归它；{path, remove: true} 去掉这条，回到默认的取法
                got = c.execute("SELECT data FROM sessions WHERE key=?", (key,)).fetchone()
                if not got:
                    return False, "找不到这个会话"
                cwd = (json.loads(got["data"]).get("cwd") or "").rstrip("/")
                path = os.path.normpath(str(arg.get("path") or "/")).rstrip("/")
                dirs = list(USER_DIRS)
                if arg.get("remove"):
                    save_user_dirs(c, [d for d in dirs if d != path])
                else:
                    if not (path.startswith(HOME + "/") and (cwd == path or cwd.startswith(path + "/"))):
                        return False, "只能点这个会话所在路径里、主目录下面的文件夹"
                    # 这个会话路径上更深的、你以前点过的会挡住这一层，去掉
                    save_user_dirs(c, [d for d in dirs if not (d.startswith(path + "/") and (cwd == d or cwd.startswith(d + "/")))] + [path])
                    moved = load_moved(c)
                    if moved.pop(key, None) is not None:   # 手动移过项目的，改回按文件夹算
                        meta_set(c, "moved", json.dumps(moved, ensure_ascii=False))
                name = project_of(cwd)
                row = c.execute("SELECT t.project FROM session_topics s JOIN topics t ON t.id = s.topic_id"
                                " WHERE s.session_key=? AND t.deleted=0", (key,)).fetchone()
                if row and row["project"] != name:   # 原来归的主题不在新项目里，改回按 #主题 自动归
                    c.execute("DELETE FROM session_topics WHERE session_key=?", (key,))
                refresh_prompt_projects(c)
                c.commit()
                label = tag_of(tag_map(c), name)[0]
                if arg.get("remove"):
                    return True, "已恢复默认，这个会话归到「%s」" % label
                return True, "以后「%s」里的会话都归到项目「%s」" % (os.path.basename(path), label)
            if action == "project_dirs" and isinstance(arg, dict) and isinstance(arg.get("dirs"), list):
                # 撤销用：把你点过的文件夹整个换回原来那份
                save_user_dirs(c, [os.path.normpath(d).rstrip("/") for d in arg["dirs"] if isinstance(d, str) and d.startswith(HOME + "/")])
                refresh_prompt_projects(c)
                c.commit()
                return True, "已撤销"
            if action == "move" and key and isinstance(arg, dict):
                # 移到项目：{project} 移过去，可以写个新名字（左栏就多一个项目）；空着或写回会话所在文件夹的项目 = 移回去
                got = c.execute("SELECT data FROM sessions WHERE key=?", (key,)).fetchone()
                if not got:
                    return False, "找不到这个会话"
                home = project_of(json.loads(got["data"]).get("cwd"))
                tags = tag_map(c)
                target = re.sub(r"\s+", " ", str(arg.get("project") or "")).strip()[:24]
                if target and target not in tags:   # 写的是左栏显示的名字（改过的短码），就用那个项目
                    target = next((p for p, (label, _) in tags.items() if label and label.lower() == target.lower()), target)
                target = target or home
                moved = load_moved(c)
                if target == home:
                    moved.pop(key, None)
                else:
                    moved[key] = target
                meta_set(c, "moved", json.dumps(moved, ensure_ascii=False))
                # 原来归的主题在新项目里就留着（拖到「未归类」上的除外）；不在的移出来，成了新项目里的「未归类」。移回去的改回按 #主题 自动归
                row = c.execute("SELECT t.project FROM session_topics s JOIN topics t ON t.id = s.topic_id"
                                " WHERE s.session_key=? AND t.deleted=0", (key,)).fetchone()
                if arg.get("untopic") or not (row and row["project"] == target):
                    if target == home:
                        c.execute("DELETE FROM session_topics WHERE session_key=?", (key,))
                    else:
                        c.execute("INSERT OR REPLACE INTO session_topics (session_key, topic_id, at) VALUES (?, NULL, ?)", (key, now))
                c.commit()
                return True, ("已移回「%s」" if target == home else "已移到「%s」") % tag_of(tags, target)[0]
            if action == "topic_edit" and isinstance(arg, dict):
                row = c.execute("SELECT * FROM topics WHERE id=? AND deleted=0", (arg.get("id"),)).fetchone()
                if not row:
                    return False, "这个主题已经删掉了"
                if arg.get("delete"):
                    # 删掉的主题留着名字，消息里再写这个 #主题 也不会被重新建出来
                    c.execute("UPDATE topics SET deleted=1 WHERE id=?", (row["id"],))
                    c.execute("DELETE FROM session_topics WHERE topic_id=?", (row["id"],))
                    c.commit()
                    return True, "已删除「%s」，里面的会话回到未归类" % row["name"]
                name = topic_name(arg.get("name"))
                if not name or name == row["name"]:
                    return False, "名字没变"
                same = find_topic(load_topics(c), row["project"], name)
                if same and same["id"] != row["id"]:
                    if not same["deleted"]:
                        return False, "这个项目里已经有「%s」了" % same["name"]
                    c.execute("DELETE FROM topics WHERE id=?", (same["id"],))
                # 旧名字记下来，以前消息里写的 #旧名 还认得
                aliases = [a for a in json.loads(row["aliases"] or "[]") + [row["name"]] if a.lower() != name.lower()]
                c.execute("UPDATE topics SET name=?, aliases=? WHERE id=?", (name, json.dumps(aliases, ensure_ascii=False), row["id"]))
                c.commit()
                return True, "已改名为「%s」" % name
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
            if action == "order" and isinstance(arg, dict) and arg.get("view") in ORDER_VIEWS:
                # 左栏拖项目排序：存一串项目 key，空列表 = 恢复默认排序
                keys = [k for k in (arg.get("keys") or []) if isinstance(k, str)][:500]
                order = load_order(c)
                if keys:
                    order[arg["view"]] = keys
                else:
                    order.pop(arg["view"], None)
                meta_set(c, "order", json.dumps(order, ensure_ascii=False))
                c.commit()
                return True, "顺序已保存" if keys else "已恢复默认排序"
            if action == "ask" and key and isinstance(arg, dict) and arg.get("k"):
                # 详情里「不用答了」：这几件不再列；它以后再问会重新列出来。undo = 撤销
                ks = [str(x) for x in (arg["k"] if isinstance(arg["k"], list) else [arg["k"]])]
                for k in ks:
                    if arg.get("undo"):
                        c.execute("DELETE FROM ask_dismiss WHERE session_key=? AND k=?", (key, k))
                    else:
                        c.execute("INSERT OR REPLACE INTO ask_dismiss (session_key, k, at) VALUES (?, ?, ?)", (key, k, now))
                c.commit()
                return True, "又列出来了" if arg.get("undo") else ("好，这件不再列出" if len(ks) == 1 else "好，这 %d 件不再列出" % len(ks))
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
        if action == "openfile" and isinstance(arg, dict):
            return open_output_file(self.conn, self.lock, str(arg.get("path") or ""), bool(arg.get("reveal")))
        if action == "rename" and isinstance(arg, dict):
            title = re.sub(r"\s+", " ", str(arg.get("title") or "")).strip()[:80]
            if not title:
                return False, "名字不能空着"
            with self.lock:
                return rename_session(self.conn, key, title)
        return False, "未知操作"

    def tick(self):
        now = time.time()
        self.maybe_scan(self.cfg["scan_interval_min"] * 60)
        self.run_schedules(now)
        self.notify_due(now)
        self.maybe_digest(now)
        self.sync_doc_titles(now)
        self.sync_mr_states(now)

    def sync_mr_states(self, now):
        """MR 合并了没有：每分钟最多查一批（300 个），glab 没装、没登录就隔 6 小时再试。"""
        if not self.cfg.get("gitlab_mr_state", True):
            return
        with self.lock:
            if now < float(meta_get(self.conn, "mr_state_retry", 0) or 0):
                return
            refs = pending_mrs(self.conn, now)
        if not refs:
            return
        got = fetch_mr_states(refs)   # 网络请求不占锁
        with self.lock:
            if got is None:
                meta_set(self.conn, "mr_state_retry", now + 6 * 3600)
                log("查 MR 状态失败（glab 没装或没登录），6 小时后再试")
            else:
                for ref in refs:
                    state, title = got.get(ref, ("", ""))
                    self.conn.execute("INSERT OR REPLACE INTO mr_state (ref, state, title, at) VALUES (?, ?, ?, ?)", (ref, state, title, now))
            self.conn.commit()

    def sync_doc_titles(self, now):
        """飞书文档换成真实标题：每分钟最多查一批（200 篇），lark-cli 没装、没登录就隔 6 小时再试。"""
        if not self.cfg.get("feishu_doc_titles", True):
            return
        with self.lock:
            if now < float(meta_get(self.conn, "doc_titles_retry", 0) or 0):
                return
            docs = pending_docs(self.conn, now)
        if not docs:
            return
        got = fetch_doc_titles(docs)   # 网络请求不占锁
        with self.lock:
            if got is None:
                meta_set(self.conn, "doc_titles_retry", now + 6 * 3600)
                log("取飞书文档标题失败（lark-cli 没装或没以用户身份登录），6 小时后再试")
            else:
                for d in docs:
                    self.conn.execute("INSERT OR REPLACE INTO doc_titles (token, title, at) VALUES (?, ?, ?)", (d, got.get(d, ""), now))
            self.conn.commit()

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
            mac_notify("到点了 · " + fresh[0]["title"], fresh[0]["headline"])
        elif fresh:
            mac_notify("%d 件事到点了" % len(fresh), "；".join(it["title"] for it in fresh))

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
    if opt == "3d":
        return (d.replace(hour=9, minute=0, second=0, microsecond=0) + dt.timedelta(days=3)).timestamp()
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
                html = f.read().replace("__LEDGER_TOKEN__", APP.token).replace("__LEDGER_BUILD__", board_build())
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
                res = search_sessions(APP.conn, (q.get("q") or [""])[0], 50, int((q.get("offset") or ["0"])[0]), (q.get("tool") or [""])[0],
                                      handoff=(q.get("handoff") or [""])[0] == "1")
            return self._send(200, json.dumps(res, ensure_ascii=False))
        if u.path == "/api/outputs":
            q = parse_qs(u.query)
            arg = lambda k, d="": (q.get(k) or [d])[0]
            with APP.lock:
                res = search_outputs(APP.conn, arg("q"), arg("cat"), arg("key"), arg("tool"),
                                     min(int(arg("limit", "100")), 500), int(arg("offset", "0")))
            return self._send(200, json.dumps(res, ensure_ascii=False))
        if u.path == "/api/reply":
            q = parse_qs(u.query)
            with APP.lock:
                r = APP.conn.execute("SELECT data FROM sessions WHERE key=?", ((q.get("key") or [""])[0],)).fetchone()
            st = json.loads(r["data"]) if r else {}
            return self._send(200, json.dumps({"text": reply_text(st.get("last_text") or st.get("progress_text"))}, ensure_ascii=False))
        if u.path == "/api/handoff":
            q = parse_qs(u.query)
            with APP.lock:
                r = APP.conn.execute("SELECT ts, text FROM handoffs WHERE session_key=?", ((q.get("key") or [""])[0],)).fetchone()
            return self._send(200, json.dumps({"ts": r["ts"], "text": r["text"]} if r else {}, ensure_ascii=False))
        if u.path == "/api/transcript":
            key = (parse_qs(u.query).get("key") or [""])[0]
            with APP.lock:
                rows = APP.conn.execute("SELECT * FROM prompts WHERE session_key=? ORDER BY ts", (key,)).fetchall()
                you = [{"kind": "you", "ts": x["ts"], "text": x["text"], "images": x.get("images") or []}
                       for x in prompt_rows_to_items(APP.conn, rows, time.time())]
                paths = [r["path"] for r in APP.conn.execute("SELECT path FROM files WHERE session_key=?", (key,))]
            its = transcript_replies(key.split(":", 1)[0], paths) if ":" in key else []
            items = sorted(you + its, key=lambda x: (x["ts"] or 0, x["kind"] != "you"))
            return self._send(200, json.dumps({"items": items, "you": len(you), "it": sum(1 for x in its if x["kind"] == "it")}, ensure_ascii=False))
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


def open_output_file(conn, lock, path, reveal):
    """打开 AI 给过的本机文件（或在访达里显示）。只开台账记过的；不认识的类型只在访达里显示，不直接运行。"""
    with lock:
        known = path.startswith("/") and conn.execute("SELECT alt FROM links WHERE url=? ORDER BY alt DESC LIMIT 1", (path,)).fetchone()
        remotes = codex_remotes(conn, known_roots(conn)) if known and known["alt"] and not os.path.exists(path) else {}
    if not known:
        return False, "看板没记过这个文件"
    note = ""
    if not os.path.exists(path):
        fb = file_fallback(path, known["alt"], remotes)   # 可能要用 glab 查默认分支，不占锁
        if not fb:
            return False, "文件已经不在了：" + home_rel(path)
        if fb[0] == "web":
            r = run(["open", fb[1]], timeout=15)
            return (r.returncode == 0,
                    "原来的 worktree 已清理，已在浏览器打开仓库默认分支上的这个文件" if r.returncode == 0 else "没打开：" + (r.stderr or "").strip()[-200:])
        path, note = fb[1], "原来的 worktree 已清理，打开的是主工作区里的同名文件："
    reveal = reveal or (not os.path.isdir(path) and file_kind(path) in ("文件", "文件夹"))
    r = run(["open", "-R", path] if reveal else ["open", path], timeout=15)
    if r.returncode != 0:
        return False, "没打开：" + (r.stderr or r.stdout or "").strip()[-200:]
    return True, note + ("已在访达里找到 " if reveal else "已打开 ") + os.path.basename(path.rstrip("/"))


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


def migrate_codex_prompts(conn):
    """早期版本把开着 Codex 内置浏览器时你发的话、和挂了心跳的线程里你发的话都丢了，有的线程因此进不了看板：重收一遍，补回提问数和最近一句。"""
    if meta_get(conn, "codex_prompts_v2"):
        return
    meta_set(conn, "hist_codex_ms", 0)
    sync_prompts(conn)
    n = 0
    for r in conn.execute("SELECT key, data FROM sessions WHERE key LIKE 'codex:%'").fetchall():
        st = json.loads(r["data"])
        got = conn.execute("SELECT text FROM prompts WHERE session_key=? ORDER BY ts", (r["key"],)).fetchall()
        if len(got) <= st.get("n_prompts", 0):
            continue
        st["n_prompts"] = len(got)
        st["first_prompt"] = got[0]["text"][:500]
        st["last_prompt"] = got[-1]["text"][:500]
        conn.execute("UPDATE sessions SET data=? WHERE key=?", (json.dumps(st, ensure_ascii=False), r["key"]))
        n += 1
    meta_set(conn, "codex_prompts_v2", time.time())
    conn.commit()
    log("已补回 %d 个 Codex 会话里你说过的话" % n)


def migrate_handoffs(conn):
    """压缩交接是后加的：已经读过的 Claude 对话记录，把每个会话最后一段交接补进来。只跑一次。"""
    if meta_get(conn, "handoffs_v1"):
        return
    n = 0
    for r in conn.execute("SELECT path, session_key FROM files WHERE session_key LIKE 'claude:%'").fetchall():
        got = last_handoff(r["path"])
        if got:
            flush_handoff(conn, r["session_key"], {"new_handoff": list(got)})
            n += 1
    meta_set(conn, "handoffs_v1", time.time())
    conn.commit()
    log("已补回 %d 个 Claude 会话压缩时的交接" % n)


def migrate_asks(conn, cfg):
    """没答的 ❓ 是后加的：最近 window_days 天、问过 ❓ 的会话重读一遍，把问过没答的补进来。只跑一次。"""
    if meta_get(conn, "asks_v2"):   # v2：认法加了「问题开头几个字——答复」和「只回一个选项」，重读一遍
        return
    t0, n = time.time(), 0
    horizon = t0 - cfg["window_days"] * 86400
    mark, cap = "❓".encode(), 16 * 1024 * 1024   # 平时增量读不漏；补的时候大文件只读最后 16MB（问答多在后面）
    for r in conn.execute("SELECT f.path, f.session_key, s.data FROM files f JOIN sessions s ON s.key = f.session_key "
                          "WHERE f.mtime >= ?", (horizon,)).fetchall():
        tool = r["session_key"].split(":", 1)[0]
        try:
            size = os.path.getsize(r["path"])
            with open(r["path"], "rb") as f:
                f.seek(max(0, size - cap))
                data = f.read()
        except OSError:
            continue
        if mark not in data:
            continue
        if size > cap:
            data = data[data.find(b"\n") + 1:]
        fresh = new_state(tool, r["session_key"].split(":", 1)[1], r["path"])
        handler = claude_line if tool == "claude" else codex_line
        for raw in data.split(b"\n"):
            if raw:
                handler(fresh, raw.decode("utf-8", "replace"))
        if not fresh.get("asks"):
            continue
        st = json.loads(r["data"])
        st["asks"] = fresh["asks"]
        conn.execute("UPDATE sessions SET data=? WHERE key=?", (json.dumps(st, ensure_ascii=False), r["session_key"]))
        n += 1
    meta_set(conn, "asks_v2", time.time())
    conn.commit()
    log("已补回 %d 个会话问过的 ❓，用时 %d ms" % (n, int((time.time() - t0) * 1000)))


def migrate_project_parents(conn):
    """改了 project_parents 或推项目名的规则：「我说过的话」里存的项目按新规则重算一遍（看板每次现算，不用管）。
    改了 project_of 的规则就把 rules 加一。"""
    cur = json.dumps({"rules": 2, "parents": sorted(PROJECT_PARENTS)}, ensure_ascii=False)
    if meta_get(conn, "project_parents") == cur:
        return
    refresh_prompt_projects(conn)
    meta_set(conn, "project_parents", cur)
    conn.commit()


def refresh_prompt_projects(conn):
    """「我说过的话」里存的项目按现在的规则重算（改了 project_parents，或在「位置」那一行点了一层之后）。不提交。"""
    for r in conn.execute("SELECT DISTINCT cwd, project FROM prompts").fetchall():
        p = project_of(r["cwd"])
        if r["cwd"] and p != r["project"]:
            conn.execute("UPDATE prompts SET project=? WHERE cwd=?", (p, r["cwd"]))


def migrate_codex_source(conn):
    """早期版本没记 Codex 会话的启动方式，codex exec 起的会话混进了看板：已存的会话从记录第一行补上。"""
    if meta_get(conn, "codex_source_v1"):
        return
    n = 0
    for r in conn.execute("SELECT key, data FROM sessions WHERE key LIKE 'codex:%'").fetchall():
        st = json.loads(r["data"])
        if "source" in st:
            continue
        try:
            with open(st.get("path") or "", "rb") as f:
                rec = json.loads(f.readline().decode("utf-8", "replace"))
        except (OSError, ValueError):
            rec = {}
        st["source"] = codex_source(rec.get("payload") or {}) if rec.get("type") == "session_meta" else ""
        conn.execute("UPDATE sessions SET data=? WHERE key=?", (json.dumps(st, ensure_ascii=False), r["key"]))
        n += st["source"] in CODEX_AGENT_SOURCES
    meta_set(conn, "codex_source_v1", time.time())
    conn.commit()
    if n:
        log("%d 个 codex exec 起的会话不再进看板" % n)


def migrate_codex_files_preamble(conn):
    """早期版本没去掉 Codex 套在你的话前面的附件清单，「最初的要求」显示成了「# Files mentioned by the user:」：已存的去一遍。"""
    if meta_get(conn, "codex_files_preamble_v1"):
        return
    for r in conn.execute("SELECT id, text FROM prompts WHERE tool='codex' AND text LIKE '%# Files mentioned by the user:%'").fetchall():
        t = strip_ambient(r["text"])
        if t != r["text"]:
            conn.execute("UPDATE prompts SET text=? WHERE id=?", (t, r["id"]))
    n = 0
    for r in conn.execute("SELECT key, data FROM sessions WHERE key LIKE 'codex:%'").fetchall():
        st = json.loads(r["data"])
        old = (st.get("first_prompt"), st.get("last_prompt"))
        got = None
        for k in ("first_prompt", "last_prompt"):
            t = strip_ambient(st.get(k) or "")
            if "# Files mentioned by the user:" in t[:40]:
                # 清单太长，存的前 500 字里没到「## My request:」：改用 prompts 表里完整的那句
                got = got or conn.execute("SELECT text FROM prompts WHERE session_key=? ORDER BY ts", (r["key"],)).fetchall()
                if got:
                    t = got[0 if k == "first_prompt" else -1]["text"]
            if t[:500] != (st.get(k) or ""):
                st[k] = t[:500]
        if (st.get("first_prompt"), st.get("last_prompt")) != old:
            conn.execute("UPDATE sessions SET data=? WHERE key=?", (json.dumps(st, ensure_ascii=False), r["key"]))
            n += 1
    meta_set(conn, "codex_files_preamble_v1", time.time())
    conn.commit()
    log("已去掉 %d 个 Codex 会话里套在你的话前面的附件清单" % n)


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
    threads = codex_threads()
    r = run(["grep", "-l", "-r", "--include=*.jsonl", '"input_image"', os.path.join(CODEX_HOME, "sessions")], timeout=900)
    for path in (r.stdout or "").splitlines():
        tid = session_id_from_path("codex", path)
        if threads.get(tid, ("",))[0] not in ("vscode", "cli"):
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


def backfill_outputs():
    """产出物加了「原话」和本机文件后补一遍：重读还在的 Claude 对话记录和 Codex 的全部回复，只跑一次。"""
    conn = open_db()
    if meta_get(conn, "outputs_backfill_v1"):
        return
    threads = codex_threads()
    subs = ["codex:" + tid for tid, th in threads.items() if th[0].startswith('{"subagent"')]
    for i in range(0, len(subs), 500):   # 以前从 Codex 子代理记下的，子代理的回复不是给你的
        conn.execute("DELETE FROM links WHERE session_key IN (%s)" % ",".join("?" * len(subs[i:i + 500])), subs[i:i + 500])
    conn.commit()
    t0, before = time.time(), conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    for path in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
        key = "claude:" + session_id_from_path("claude", path)
        st = {}
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"type":"assistant"' not in line or '"type":"text"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("isSidechain") or rec.get("type") != "assistant":
                        continue
                    st["cwd"] = rec.get("cwd") or st.get("cwd") or ""
                    note_links(st, parse_iso(rec.get("timestamp")) or 0, take_text((rec.get("message") or {}).get("content")))
        except OSError:
            continue
        flush_media(conn, key, st)
        conn.commit()
    try:
        c = sqlite3.connect("file:%s?mode=ro" % os.path.join(CODEX_HOME, "thread_history_1.sqlite"), uri=True, timeout=5)
        cur = c.execute("SELECT thread_id, created_at_ms, item_json FROM thread_items WHERE item_type='agentMessage' ORDER BY rowid")
        while True:
            rows = cur.fetchmany(500)
            if not rows:
                break
            for tid, cms, item_json in rows:
                th = threads.get(tid, ("", "", ""))
                if th[0] not in ("vscode", "cli"):
                    continue
                try:
                    text = json.loads(item_json).get("text") or ""
                except ValueError:
                    continue
                st = {"cwd": th[1]}
                note_links(st, (cms or 0) / 1000.0, text)
                flush_media(conn, "codex:" + tid, st)
            conn.commit()
        c.close()
    except sqlite3.Error as e:
        log("补产出物时读 Codex 回复失败：%s" % e)
        return
    meta_set(conn, "outputs_backfill_v1", time.time())
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    log("产出物回填完成：新增 %d 条（链接和文件），用时 %d 秒" % (after - before, time.time() - t0))


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
    threading.Thread(target=lambda: (_guard(backfill_media), _guard(backfill_outputs)), daemon=True).start()
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
                "确认 cmux 开着，在终端里运行 %s，按提示把 cmux 设置里的套接字密码交给看板" % ledger_cmd("cmux-setup"))
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
        add("提示", "ok", "时区", "看板按你设置的 %s 算时间，和本机一致" % utc_text(h))
    else:
        # 是用户自己在 config.json 里设的，不算问题，只说清楚按哪个时区
        add("提示", "ok", "时区", "看板按你设置的 %s 算时间（本机是 %s）" % (utc_text(h), utc_text(local)))

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
          "密码存在 cmux 设置里，看板另存一份在 %s，只有你的账号能读。" % home_rel(CMUX_PW_FILE))
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
        if cmux_mode() == "password":
            # 已是密码模式就不动文件：新版 cmux 把密码挪进了自己的设置，这里再写一个会和设置里的对不上
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
    # 看板另存一份密码，每次调 cmux 时带上：新版 cmux 会把密码从 cmux.json 挪进自己的设置，服务就读不到了
    file_pw = ((data.get("automation") or {}).get("socketPassword") or "") if isinstance(data, dict) else ""
    if file_pw and file_pw != cmux_password() and cmux("workspace", "list", password=file_pw).returncode == 0:
        save_cmux_password(file_pw)
        print("✅ 已把 %s 里的密码另存一份给看板（%s）" % (home_rel(CMUX_CONFIG), home_rel(CMUX_PW_FILE)))
    r = cmux("workspace", "list")   # 和看板服务一样，不带 cmux 终端的环境变量
    if r.returncode != 0 and cmux_mode() == "password" and re.search(r"auth|password", r.stderr + r.stdout, re.I):
        print("看板还没有 cmux 认的套接字密码。到 cmux 设置 → 自动化，看「套接字密码」：有就复制；"
              "空着就设一个长密码（可用 openssl rand -base64 32 生成），保存后复制。")
        if not sys.stdin.isatty():
            sys.exit("❌ 要在终端里运行这条命令，才能粘贴密码：%s" % ledger_cmd("cmux-setup"))
        pw = getpass.getpass("粘贴到这里（不显示），回车：").strip()
        if not pw:
            sys.exit("➖ 没输入，什么也没改")
        r = cmux("workspace", "list", password=pw)
        if r.returncode != 0:
            sys.exit("❌ cmux 不认这个密码：%s。确认在 cmux 设置里保存了，再运行一次" % (r.stderr or r.stdout).strip()[:200])
        save_cmux_password(pw)
        print("✅ 密码对了，已另存一份给看板（%s）" % home_rel(CMUX_PW_FILE))
    print("✅ cmux workspace list 通过" if r.returncode == 0 else "❌ cmux workspace list 失败：" + (r.stderr or r.stdout).strip()[:200])
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
