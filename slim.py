#!/usr/bin/env python3
"""
slim.py：给 Claude Code 的会话做一次 slim（第三版）。

从一份旧会话文件（~/.claude/projects/<项目目录>/<会话id>.jsonl）里，
挑一个时间点，把那之前的对话整段拿掉，之后的保留下来，
写成一份新的会话文件，然后用 `claude --resume <新id>` 接着聊。

两种保留方式（--history）：
  dialogue（默认）：只带对话。人说的话、AI 说的话、语音工具（成对）、--keep-tool 指定的工具（成对）。
                    不做摘要，不改写任何一句话；只加三处标记：压缩摘要前的说明、图片占位、发送时间。
  full：            从切点那一轮起，主线上的行原样带过去（工具调用和结果、图片、附件都在），
                    只改四处：首行 parentUuid 置空并按顺序重新串链、换新 sessionId、删 message.usage、
                    剥掉 thinking。活干到一半、还要用刚才的工具结果时用它。只能配 --from。

旧文件只读，不改不删。新文件排他创建，已存在就报错，绝不覆盖。只用 Python 3.9+ 标准库。

安全提醒：不要让 Claude 用 cat / Read 直接读会话 jsonl。里面逐条存着 thinking 字段，
把它读进上下文可能触发平台拦截（[reasoning_extraction]），整轮被拒。要看有哪些轮次，就用 --list-turns。
这个脚本只在内存里处理 thinking，永远不打印 thinking 的内容。

用法：
  python3 slim.py 旧会话.jsonl --list-turns --tz Asia/Shanghai     # 先看每一轮的时间和开头
  python3 slim.py 旧会话.jsonl --from 2026-09-30T15:40:00+08:00 --dry-run
  python3 slim.py 旧会话.jsonl --from 2026-09-30T15:40:00+08:00 --tz Asia/Shanghai
  python3 slim.py 旧会话.jsonl --from 2026-09-30T15:40:00+08:00 --history full
  python3 slim.py 旧会话.jsonl --all                               # 不切，全部轮次只做瘦身
"""

import argparse
import copy
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

CHAIN_TYPES = {"user", "assistant", "attachment", "system"}
THINKING_TYPES = {"thinking", "redacted_thinking"}
# Messages API 对 tool_use id 的约束
TOOL_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
# 语音类工具：电话里她说的话只存在于 listen 的返回里，念出口的话只存在于 speak 的输入里，
# 剥掉就两边都没了。所以默认成对保留，--no-voice 关掉。
VOICE_TOOLS = ("mcp__tts__speak", "mcp__voice__speak", "mcp__voice__listen",
               "mcp__voice__converse", "mcp__voice__hangup")


class SlimError(Exception):
    """能预见的失败：给人看的中文说明，不写文件。"""


# ---------- 读 ----------

def load_rows(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:  # 只读
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # 写了一半的行，跳过
            if isinstance(row, dict):
                rows.append(row)
    return rows


def active_chain(rows):
    """CLI 恢复时会走的那条主线：从文件里最后写入的一行，沿 parentUuid 往回走。

    rewind、重试留下的废弃分支还在文件里，但最后写入的那一行一定在主线上。
    """
    by_uuid, leaf = {}, None
    for row in rows:
        u = row.get("uuid")
        if not u or row.get("isSidechain") is True or row.get("type") not in CHAIN_TYPES:
            continue
        by_uuid.setdefault(u, row)
        leaf = row
    chain, seen, row = [], set(), leaf
    while row is not None and row["uuid"] not in seen:
        seen.add(row["uuid"])
        chain.append(row)
        parent = row.get("parentUuid")
        row = by_uuid.get(parent) if parent else None
    chain.reverse()
    return chain


def blocks(row):
    content = (row.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def use_ids(row):
    if row.get("type") != "assistant":
        return []
    return [b.get("id") for b in blocks(row) if b.get("type") == "tool_use"]


def result_ids(row):
    if row.get("type") != "user":
        return []
    return [b.get("tool_use_id") for b in blocks(row) if b.get("type") == "tool_result"]


def sibling_results(rows, chain):
    """并行工具调用的结果，有一部分不在主线上。

    Claude Code 记并行调用时，每个结果行挂在自己那条调用行下面，
    从最后一行往回走只会经过最后一个结果，前面几个结果在旁支上（CLI 恢复时自己会捞回来）。
    这里同样捞回来：tool_use_id → 那条结果行（不在主线、不是子 agent）。
    """
    on_chain = {r.get("uuid") for r in chain}
    found = {}
    for row in rows:
        if row.get("isSidechain") is True or row.get("uuid") in on_chain:
            continue
        for rid in result_ids(row):
            if rid:
                found.setdefault(rid, row)
    return found


def is_prompt(row):
    """一轮的开头：真正由人输入的那条消息（不是工具结果、系统提醒、压缩摘要）。"""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isCompactSummary"):
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    types = [b.get("type") for b in blocks(row)]
    return "tool_result" not in types and any(t in ("text", "image") for t in types)


def parse_time(value):
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None  # 没有时区的时间戳不可信，当作缺失


def split_turns(chain):
    starts = [i for i, row in enumerate(chain) if is_prompt(row)]
    turns = []
    for n, start in enumerate(starts):
        begin = 0 if n == 0 else start  # 第一轮之前的东西（比如压缩摘要、上次 slim 的头）算进第一轮
        end = starts[n + 1] if n + 1 < len(starts) else len(chain)
        times = [t for t in (parse_time(r.get("timestamp")) for r in chain[begin:end]) if t]
        turns.append({"begin": begin, "start": start, "end": end,
                      "started": parse_time(chain[start].get("timestamp")),
                      "ended": max(times) if times else None})
    return turns


def prompt_text(row):
    """人说的话的文字。只取 text，图片换占位；永远不碰 thinking。"""
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    parts = []
    for b in blocks(row):
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "image":
            parts.append("[图片，slim 时省略]")
    return "\n".join(p for p in parts if p)


# ---------- 时区 ----------

def local_zone_name():
    """本机时区的名字，尽量拿到 Asia/Shanghai 这种形式。"""
    tz_env = os.environ.get("TZ", "").lstrip(":")
    if tz_env and "/" in tz_env:
        return tz_env
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        with open("/etc/timezone", encoding="utf-8") as f:  # 部分 Linux
            name = f.read().strip()
            if name:
                return name
    except OSError:
        pass
    return None


def resolve_tz(name):
    """返回 (tzinfo 或 None, 给人看的说明)。None 表示用本机时区（astimezone()）。"""
    if name:
        if ZoneInfo is None:
            raise SlimError("这个 Python 没有 zoneinfo（需要 3.9+），不能用 --tz。")
        try:
            return ZoneInfo(name), None
        except Exception:
            raise SlimError(f"认不出时区 {name}。写成 Asia/Shanghai 这种形式；"
                            "Windows 上可能要先 pip install tzdata。")
    zone = local_zone_name()
    offset = datetime.now().astimezone().strftime("%z")
    offset = offset[:3] + ":" + offset[3:] if offset else "?"
    if zone and ZoneInfo is not None:
        try:
            return ZoneInfo(zone), f"使用本机时区 {zone}（现在是 UTC{offset}）；要换就加 --tz"
        except Exception:
            pass
    abbr = time.tzname[0] if time.tzname else "?"
    return None, f"使用本机时区 {abbr}（现在是 UTC{offset}）；要换就加 --tz"


def to_local(ts, tz):
    t = parse_time(ts)
    if not t:
        return None
    return t.astimezone(tz) if tz else t.astimezone()


def short_label(ts, tz):
    t = to_local(ts, tz)
    return t.strftime("%Y-%m-%d %H:%M") if t else None


def iso_label(ts, tz):
    t = to_local(ts, tz)
    return t.isoformat(timespec="seconds") if t else None


# ---------- dialogue：投影成对话 ----------

def project_dialogue(kept, keep_tools, extra_results, add_time, tz, stats):
    """把保留段投影成对话事件：人说的话、AI 说的话、要保留的工具（调用和结果一起）。

    工具以「一对」为单位出事件：遇到 tool_use 时把它的结果一起带上，
    结果行后面再遇到就跳过。并行调用因此也是 调用1→结果1→调用2→结果2。
    """
    results = {}  # tool_use_id → (结果块, toolUseResult, 时间戳)
    for row in kept:
        for b in blocks(row):
            if row.get("type") == "user" and b.get("type") == "tool_result":
                results.setdefault(b.get("tool_use_id"),
                                   (b, row.get("toolUseResult"), row.get("timestamp")))
    for rid, row in extra_results.items():
        for b in blocks(row):
            if b.get("type") == "tool_result" and b.get("tool_use_id") == rid:
                results.setdefault(rid, (b, row.get("toolUseResult"), row.get("timestamp")))

    events = []  # (kind, payload, timestamp)
    for row in kept:
        ts = row.get("timestamp")
        kind = row.get("type")
        if kind == "user":
            if row.get("isCompactSummary"):
                content = (row.get("message") or {}).get("content")
                text = content if isinstance(content, str) else prompt_text(row)
                if text.strip():
                    note = "[这之前的对话被 Claude Code 自动压缩过，下面是压缩摘要]\n"
                    events.append(("user", note + text, ts))
            elif is_prompt(row):
                text = prompt_text(row)
                if text.startswith(("<command-", "<local-command-")):
                    continue  # 斜杠命令和它的输出，不是对话
                if add_time:
                    label = iso_label(ts, tz)
                    if label:
                        text = f"[消息发送时间：{label}]\n{text}"
                    else:
                        stats["no_time_prompts"] += 1
                events.append(("user", text, ts))
            # 工具结果行：结果已经跟着它的调用出过了，这里不再输出
        elif kind == "assistant":
            for b in blocks(row):
                t = b.get("type")
                if t == "text" and b.get("text", "").strip():
                    events.append(("text", b["text"], ts))
                elif t == "tool_use":
                    hit = results.get(b.get("id"))
                    if b.get("name") in keep_tools and hit is not None:
                        events.append(("tool_pair", {"use": b, "result": hit}, ts))
                    # 其他工具、没有结果的调用：丢掉
                # thinking、图片之外的其他块：丢掉
    return events


def build_dialogue_rows(events, session_id, cwd, model, stats):
    rows, parent, pending = [], None, []
    used_ids = set()
    known = [ts for _, _, ts in events if parse_time(ts)]
    fallback = known[0] if known else datetime.now(timezone.utc).isoformat()

    def fix_ts(ts):
        nonlocal fallback
        if parse_time(ts):
            fallback = ts
            return ts
        stats["no_time_rows"] += 1  # 缺时间戳：借用前一行的时间，并计数警告
        return fallback

    def push(kind, message, ts, extra=None):
        nonlocal parent
        u = str(uuid.uuid4())
        row = {"parentUuid": parent, "isSidechain": False, "type": kind, "uuid": u,
               "timestamp": fix_ts(ts), "sessionId": session_id, "cwd": cwd, "message": message}
        if extra:
            row.update(extra)
        rows.append(row)
        parent = u

    def flush(ts):
        nonlocal pending
        if pending:
            message = {"id": "msg_" + uuid.uuid4().hex, "type": "message",
                       "role": "assistant", "model": model, "content": pending}
            push("assistant", message, ts)
            pending = []

    def tool_id(raw):
        tid = str(raw or "")
        if not TOOL_ID_RE.match(tid) or tid in used_ids:
            tid = "toolu_vslim_" + uuid.uuid4().hex
            stats["renamed_tool_ids"] += 1
        used_ids.add(tid)
        return tid

    last_ts = None
    for kind, payload, ts in events:
        last_ts = ts
        if kind == "user":
            flush(ts)
            push("user", {"role": "user", "content": payload}, ts)
        elif kind == "text":
            pending.append({"type": "text", "text": payload})
        elif kind == "tool_pair":
            use = payload["use"]
            block, tool_use_result, result_ts = payload["result"]
            tid = tool_id(use.get("id"))
            pending.append({"type": "tool_use", "id": tid, "name": use.get("name"),
                            "input": use.get("input") or {}})
            flush(ts)  # 调用结束这条 assistant 消息，下一行紧跟它的结果
            result = {"type": "tool_result", "tool_use_id": tid,
                      "content": block.get("content", "")}
            if block.get("is_error"):
                result["is_error"] = True
            extra = {"toolUseResult": tool_use_result} if tool_use_result is not None else None
            push("user", {"role": "user", "content": [result]}, result_ts or ts, extra)
    flush(last_ts)
    return rows


# ---------- full：原行带过去 ----------

def build_full_rows(kept, extra_results, session_id, cwd_override, stats):
    """保留段原行深拷贝。只改：剥 thinking、首行 parentUuid=null 并重新串链、
    换 sessionId、删 message.usage（以及给了 --cwd 时的 cwd）。"""
    rows = [copy.deepcopy(r) for r in kept]
    on_chain = {r.get("uuid") for r in rows}
    kept_uses = {i for r in rows for i in use_ids(r)}
    kept_answered = {i for r in rows for i in result_ids(r)}
    extras = []
    for rid, row in extra_results.items():
        # 只捞保留段里有调用、却还没有结果的那几条
        if rid not in kept_uses or rid in kept_answered:
            continue
        if row.get("uuid") not in on_chain:
            on_chain.add(row.get("uuid"))
            extras.append(copy.deepcopy(row))
    stats["recovered_results"] = len(extras)
    rows.extend(extras)

    # 1. 剥 thinking；剥完空了的 assistant 行整行删掉
    cleaned = []
    for row in rows:
        content = (row.get("message") or {}).get("content")
        if row.get("type") == "assistant" and isinstance(content, list):
            keep = [b for b in content if not (isinstance(b, dict) and b.get("type") in THINKING_TYPES)]
            stripped = len(content) - len(keep)
            if stripped:
                stats["thinking"] += stripped
                row["message"]["content"] = keep
                if not keep:
                    stats["dropped_rows"] += 1
                    continue
        cleaned.append(row)
    rows = cleaned

    # 2. 配对检查。最后一轮里还没返回的调用（比如正在跑 slim 的这一次）去掉，其余不成对就拒绝
    uses = {i for r in rows for i in use_ids(r)}
    answered = {i for r in rows for i in result_ids(r)}
    orphans = [i for i in answered if i not in uses]
    unanswered = [i for i in uses if i not in answered]
    if unanswered:
        last_prompt = max((n for n, r in enumerate(rows) if is_prompt(r)), default=-1)
        final_ids = set()
        last_mid = None
        for n in range(len(rows) - 1, -1, -1):  # 最后一条 assistant 消息（同一个 message.id）
            if rows[n].get("type") == "assistant":
                last_mid = (rows[n].get("message") or {}).get("id")
                break
        for n, r in enumerate(rows):
            if n > last_prompt and r.get("type") == "assistant" and \
                    (r.get("message") or {}).get("id") == last_mid:
                final_ids.update(use_ids(r))
        in_flight = [i for i in unanswered if i in final_ids]
        unanswered = [i for i in unanswered if i not in final_ids]
        if in_flight and not unanswered and not orphans:
            stats["dropped_tools"] += len(in_flight)
            drop = set(in_flight)
            kept_rows = []
            for r in rows:
                if r.get("type") == "assistant" and drop & set(use_ids(r)):
                    r["message"]["content"] = [b for b in blocks(r) if not (
                        b.get("type") == "tool_use" and b.get("id") in drop)]
                    if not r["message"]["content"]:
                        stats["dropped_rows"] += 1
                        continue
                kept_rows.append(r)
            rows = kept_rows
    if unanswered or orphans:
        raise SlimError(f"保留段里的工具调用不成对：{len(unanswered)} 个调用没有结果，"
                        f"{len(orphans)} 个结果找不到调用。full 模式不能只带一半（API 会拒绝）。"
                        "把切点往前或往后挪一轮，或者改用 --history dialogue。")

    # 3. 每个调用行后面紧跟它的结果行（并行调用：调用1→结果1→调用2→结果2，CLI 自己也这样记）
    by_result = {}
    for r in rows:
        for rid in result_ids(r):
            by_result.setdefault(rid, r)
    result_rows = {id(r) for r in by_result.values()}
    ordered, placed = [], set()
    for r in rows:
        if id(r) in result_rows:
            continue
        ordered.append(r)
        for uid in use_ids(r):
            rr = by_result.get(uid)
            if rr is not None and id(rr) not in placed:
                placed.add(id(rr))
                ordered.append(rr)
    rows = ordered

    # 4. 重新串链、换 sessionId、删 usage
    parent = None
    for r in rows:
        r["parentUuid"] = parent
        r["sessionId"] = session_id
        if cwd_override:
            r["cwd"] = cwd_override
        if isinstance(r.get("message"), dict):
            r["message"].pop("usage", None)
        if r.get("type") in ("user", "assistant") and not parse_time(r.get("timestamp")):
            stats["no_time_rows"] += 1
        parent = r.get("uuid")
    return rows


# ---------- 自检 ----------

def validate(rows, session_id):
    """写文件之前的自检。返回问题列表（只含行号和 id，不含任何内容）；空列表才写。"""
    problems = []
    if not rows:
        return ["新会话一行都没有"]
    seen = set()
    for n, r in enumerate(rows):
        u = r.get("uuid")
        if not u:
            problems.append(f"第 {n + 1} 行没有 uuid")
        elif u in seen:
            problems.append(f"第 {n + 1} 行 uuid 重复：{u}")
        seen.add(u)
        want = None if n == 0 else rows[n - 1].get("uuid")
        if r.get("parentUuid") != want:
            problems.append(f"第 {n + 1} 行 parentUuid 没有指向上一行" if n else
                            "第 1 行 parentUuid 不是 null")
        if r.get("sessionId") != session_id:
            problems.append(f"第 {n + 1} 行 sessionId 不是新 id")
        message = r.get("message")
        if isinstance(message, dict) and "usage" in message:
            problems.append(f"第 {n + 1} 行还带着 message.usage")
        for b in blocks(r):
            if b.get("type") in THINKING_TYPES:
                problems.append(f"第 {n + 1} 行还有 {b.get('type')} 块")

    tool_seen = set()
    for n, r in enumerate(rows):
        ids = use_ids(r)
        for i in ids:
            if not isinstance(i, str) or not TOOL_ID_RE.match(i):
                problems.append(f"第 {n + 1} 行 tool_use.id 不合法：{i!r}")
            elif i in tool_seen:
                problems.append(f"第 {n + 1} 行 tool_use.id 重复：{i}")
            tool_seen.add(i)
        if ids:
            # 下一行（连续几行结果行）必须含它的 tool_result
            got, m = set(), n + 1
            while m < len(rows) and result_ids(rows[m]):
                got.update(result_ids(rows[m]))
                m += 1
            for i in ids:
                if i not in got:
                    problems.append(f"第 {n + 1} 行的 tool_use {i} 下一行不是它的 tool_result")
        rids = result_ids(r)
        if rids:
            # 往前跨过连续的结果行，第一条非结果行必须是含对应 tool_use 的那行
            m = n - 1
            while m >= 0 and result_ids(rows[m]):
                m -= 1
            before = set(use_ids(rows[m])) if m >= 0 else set()
            for i in rids:
                if i not in before:
                    problems.append(f"第 {n + 1} 行的 tool_result {i} 前一行没有对应的 tool_use")
    return problems


# ---------- 写 ----------

def project_dir_for(cwd):
    slug = "".join(c if (c.isalnum() and c.isascii()) or c in ".-" else "-" for c in cwd)
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", slug)


def serialize(rows):
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")


def write_session(path, data):
    """排他创建：已存在就抛 FileExistsError，绝不覆盖。写完 fsync 文件和目录。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass  # 有的文件系统不支持对目录 fsync，文件本身已经落盘


# ---------- 主流程 ----------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="给 Claude Code 会话做一次 slim：切掉某个时间点之前的轮次，保留之后的原话")
    ap.add_argument("source", help="旧会话文件 .jsonl（只读）")
    ap.add_argument("--from", dest="retain_from",
                    help="保留这个时间点之后结束的完整轮次，必须带时区，如 2026-09-30T15:40:00+08:00")
    ap.add_argument("--all", action="store_true",
                    help="不切，全部轮次都保留，只做 dialogue 瘦身（与 --from 二选一）")
    ap.add_argument("--history", choices=("dialogue", "full"), default="dialogue",
                    help="dialogue（默认）只带对话；full 把保留段原行带过去（只能配 --from）")
    ap.add_argument("--keep-tool", action="append", default=[],
                    help="dialogue 模式下额外成对保留的工具名，可以写多次")
    ap.add_argument("--no-voice", action="store_true",
                    help="不默认保留语音工具（" + "、".join(VOICE_TOOLS) + "）")
    ap.add_argument("--no-time", action="store_true", help="不在人说的话前面加发送时间")
    ap.add_argument("--tz", help="显示和发送时间用的时区，例如 Asia/Shanghai；默认用本机时区")
    ap.add_argument("--cwd", help="新会话所属的项目目录；默认和旧会话相同")
    ap.add_argument("--list-turns", action="store_true",
                    help="列出每一轮的时间和人说的话的开头，不写文件")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    return ap.parse_args(argv)


def run(args):
    tz, tz_note = resolve_tz(args.tz)
    if tz_note:
        print(tz_note)

    rows = load_rows(args.source)
    chain = active_chain(rows)
    turns = split_turns(chain)
    if not turns:
        raise SlimError("这份文件里找不到对话轮次。")

    if args.list_turns:
        for n, t in enumerate(turns):
            head = prompt_text(chain[t["start"]]).replace("\n", " ")[:50]
            label = short_label(chain[t["start"]].get("timestamp"), tz) or "?"
            print(f"{n:4d}  {label:16s}  {head}")
        return 0

    if args.all and args.retain_from:
        raise SlimError("--all 和 --from 只能二选一：--all 是全部保留，--from 是从某个时间点切。")
    if args.all and args.history == "full":
        raise SlimError("--all 不能配 --history full：那等于把要离开的整个 context 原样搬过去，"
                        "什么都没省。全部保留就用 --all（dialogue）；要带工具结果就用 --from 切掉前面一段。")
    if not args.all and not args.retain_from:
        raise SlimError("需要 --from（带时区的时间点）或者 --all。先用 --list-turns 看看每一轮的时间。")

    if args.all:
        first = 0
    else:
        cut_at = parse_time(args.retain_from)
        if not cut_at:
            raise SlimError("--from 必须是带时区的时间，例如 2026-09-30T15:40:00+08:00")
        if cut_at > datetime.now(timezone.utc):
            raise SlimError("--from 在未来。")
        first = next((i for i, t in enumerate(turns) if t["ended"] and t["ended"] >= cut_at), None)
        if first is None:
            raise SlimError("这个时间点之后没有完整的轮次。")
    kept = chain[turns[first]["begin"]:]
    extra = sibling_results(rows, chain)

    models = [r["message"].get("model") for r in chain
              if r.get("type") == "assistant" and isinstance(r.get("message"), dict)]
    models = [m for m in models if m and m != "<synthetic>"]
    model = models[-1] if models else "claude"
    cwd = args.cwd or next((r.get("cwd") for r in reversed(chain) if r.get("cwd")), None)
    if not cwd:
        raise SlimError("旧会话里没有 cwd，请用 --cwd 指定项目目录。")
    if args.cwd and not os.path.isabs(args.cwd):
        raise SlimError("--cwd 必须是绝对路径。")

    stats = {"no_time_prompts": 0, "no_time_rows": 0, "renamed_tool_ids": 0, "thinking": 0,
             "dropped_rows": 0, "dropped_tools": 0, "recovered_results": 0}
    kept_uses = sum(1 for r in kept for b in blocks(r) if b.get("type") == "tool_use")
    kept_thinking = sum(1 for r in kept for b in blocks(r) if b.get("type") in THINKING_TYPES)
    kept_images = sum(1 for r in kept for b in blocks(r) if b.get("type") == "image")

    new_id = str(uuid.uuid4())
    if args.history == "full":
        if args.keep_tool or args.no_voice:
            print("提示：full 模式保留全部工具，--keep-tool / --no-voice 不起作用。")
        out_rows = build_full_rows(kept, extra, new_id, args.cwd, stats)
    else:
        keep_tools = set(args.keep_tool)
        if not args.no_voice:
            keep_tools.update(VOICE_TOOLS)
        events = project_dialogue(kept, keep_tools, extra, not args.no_time, tz, stats)
        out_rows = build_dialogue_rows(events, new_id, cwd, model, stats)
        stats["dropped_tools"] = kept_uses - sum(
            1 for r in out_rows for b in blocks(r) if b.get("type") == "tool_use")
        stats["thinking"] = kept_thinking

    problems = validate(out_rows, new_id)
    data = serialize(out_rows)

    # ---- 统计 ----
    first_turn, last_turn = turns[first], turns[-1]
    mode = "全部保留（--all）" if args.all else f"从 --from {args.retain_from} 切"
    print(f"模式：{args.history}，{mode}")
    print(f"总轮次 {len(turns)}，切掉 {first} 轮，保留第 {first} 轮起的 {len(turns) - first} 轮")
    print(f"保留段：第一轮开始于 {short_label(chain[first_turn['start']].get('timestamp'), tz) or '?'}，"
          f"最后一轮结束于 {short_label(last_turn['ended'].isoformat() if last_turn['ended'] else None, tz) or '?'}")
    if args.history == "full":
        print(f"保留段里：工具调用 {kept_uses - stats['dropped_tools']} 次带过去"
              f"（丢掉还没返回的 {stats['dropped_tools']} 次，捞回并行调用的旁支结果 "
              f"{stats['recovered_results']} 条），剥掉 thinking {stats['thinking']} 块"
              f"（因此整行删掉 {stats['dropped_rows']} 行），图片 {kept_images} 张原样带过去")
    else:
        print(f"保留段里：丢掉工具调用 {stats['dropped_tools']} 次，thinking {stats['thinking']} 块，"
              f"图片 {kept_images} 张（只留占位）")
    print(f"新会话 {len(out_rows)} 行，产物 {len(data)} 字节，"
          f"粗估约 {len(data) // 3} token（字节÷3 的粗估，只看量级）")
    if stats["renamed_tool_ids"]:
        print(f"注意：{stats['renamed_tool_ids']} 个 tool_use id 不合法或重复，已换成新 id")
    missing = stats["no_time_prompts"] + stats["no_time_rows"]
    if missing:
        print(f"警告：保留的行里有 {missing} 处缺时间戳"
              f"（{stats['no_time_prompts']} 条人说的话没加上发送时间，"
              f"{stats['no_time_rows']} 行借用了前一行的时间或保持原样）")

    if problems:
        print("自检没通过，不写文件：", file=sys.stderr)
        for p in problems[:20]:
            print("  - " + p, file=sys.stderr)
        if len(problems) > 20:
            print(f"  ……还有 {len(problems) - 20} 条", file=sys.stderr)
        return 2
    print("自检通过：工具调用都紧跟结果、uuid 唯一、单链、无 usage、无 thinking")

    if args.dry_run:
        print("dry-run：没有写文件。")
        return 0

    # 默认写在旧文件旁边：同一个项目目录，claude --resume 才找得到
    out_dir = project_dir_for(cwd) if args.cwd else os.path.dirname(os.path.abspath(args.source))
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    out_path = os.path.join(out_dir, new_id + ".jsonl")
    if os.path.abspath(out_path) == os.path.abspath(args.source):
        raise SlimError("新文件路径和旧文件相同，停下。")
    try:
        write_session(out_path, data)
    except FileExistsError:
        raise SlimError(f"{out_path} 已经存在，不覆盖。再跑一次会换一个新 id。")
    print(f"已写入 {out_path}")
    print(f"新 session id：{new_id}")
    print(f"接着聊：cd {cwd} && claude --resume {new_id}")
    print("有自己的 bridge 管 session_id 的，把记着的 id 换成这个即可")
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except SlimError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
