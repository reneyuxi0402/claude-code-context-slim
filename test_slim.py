"""slim.py 的合成数据测试（标准库 unittest）。

python3 test_slim.py -v
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid as uuidlib
from unittest import mock

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "slim.py")
sys.dont_write_bytecode = True  # 不在教程目录里留 __pycache__
spec = importlib.util.spec_from_file_location("slim", SCRIPT)
slim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slim)

SID = "11111111-1111-1111-1111-111111111111"
CWD = os.path.join(tempfile.gettempdir(), "slim_test", "project")
os.makedirs(os.path.dirname(CWD), exist_ok=True)


class Builder:
    """按顺序造会话行；默认每行挂在上一行下面，parent= 可以手动指定（造旁支）。"""

    def __init__(self):
        self.rows = []
        self.minute = 0

    def _ts(self):
        self.minute += 1
        return f"2026-09-30T07:{self.minute:02d}:00.000Z"

    def add(self, kind, content=None, parent="__last__", **extra):
        u = str(uuidlib.uuid4())
        if parent == "__last__":
            parent = self.rows[-1]["uuid"] if self.rows else None
        row = {"parentUuid": parent, "isSidechain": False, "type": kind, "uuid": u,
               "timestamp": self._ts(), "sessionId": SID, "cwd": CWD}
        if content is not None:
            role = "assistant" if kind == "assistant" else "user"
            row["message"] = {"role": role, "content": content}
        row.update(extra)
        self.rows.append(row)
        return row

    def prompt(self, text):
        return self.add("user", text)

    def assistant(self, blocks, mid, usage=True):
        row = self.add("assistant", blocks)
        row["message"].update({"id": mid, "type": "message", "model": "claude-test"})
        if usage:
            row["message"]["usage"] = {"input_tokens": 300000, "output_tokens": 10}
        return row

    def result(self, tid, text="ok", parent="__last__"):
        return self.add("user", [{"type": "tool_result", "tool_use_id": tid, "content": text}],
                        parent=parent, toolUseResult=text)

    def write(self, path):
        with open(path, "w", encoding="utf-8") as f:
            for r in self.rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def thinking(text="内部推理"):
    return {"type": "thinking", "thinking": text, "signature": "sig"}


def use(tid, name):
    return {"type": "tool_use", "id": tid, "name": name, "input": {"text": "hi"}}


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class SlimTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="case_", dir=os.path.dirname(CWD))
        self.src = os.path.join(self.dir, SID + ".jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_slim(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = slim.main([self.src, *args])
        return code, out.getvalue(), err.getvalue()

    def outputs(self):
        return [os.path.join(self.dir, n) for n in os.listdir(self.dir)
                if n.endswith(".jsonl") and n != SID + ".jsonl"]

    def sha(self):
        with open(self.src, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    # ---- 会话样本 ----

    def parallel_voice_session(self, results_on_chain=False):
        """第 0 轮闲聊；第 1 轮里并行调用两个语音工具 + 一个 Bash。

        真实的 Claude Code 文件里，并行调用的结果各自挂在自己的调用行下，
        主线只经过最后一个结果（results_on_chain=False 模拟这种情况）。
        """
        b = Builder()
        b.prompt("早上好")
        b.assistant([thinking()], "msg_a")
        b.assistant([{"type": "text", "text": "早"}], "msg_a")
        b.prompt("打个电话吧")
        b.assistant([thinking()], "msg_b")
        u1 = b.assistant([use("toolu_speak1", "mcp__tts__speak")], "msg_b")
        u2 = b.assistant([use("toolu_listen2", "mcp__voice__listen")], "msg_b")
        if results_on_chain:
            b.result("toolu_speak1", "spoken")
            b.result("toolu_listen2", "她说：喂")
        else:
            b.result("toolu_speak1", "spoken", parent=u1["uuid"])   # 旁支
            b.result("toolu_listen2", "她说：喂", parent=u2["uuid"])  # 主线
        b.assistant([use("toolu_bash3", "Bash")], "msg_c")
        b.result("toolu_bash3", "ls output")
        b.assistant([{"type": "text", "text": "听到了"}], "msg_d")
        b.write(self.src)
        return b

    # ---- 测试 ----

    def test_parallel_voice_pairs_adjacent(self):
        for on_chain in (False, True):
            with self.subTest(results_on_chain=on_chain):
                for p in self.outputs():
                    os.remove(p)
                self.parallel_voice_session(results_on_chain=on_chain)
                before = self.sha()
                code, out, err = self.run_slim("--all", "--tz", "Asia/Shanghai")
                self.assertEqual(code, 0, err)
                self.assertEqual(before, self.sha(), "旧文件被改了")
                (path,) = self.outputs()
                rows = read_jsonl(path)
                self.assertEqual(slim.validate(rows, rows[0]["sessionId"]), [])
                seq = []
                for r in rows:
                    for blk in slim.blocks(r):
                        if blk["type"] == "tool_use":
                            seq.append(("use", blk["id"]))
                        elif blk["type"] == "tool_result":
                            seq.append(("result", blk["tool_use_id"]))
                self.assertEqual(seq, [("use", "toolu_speak1"), ("result", "toolu_speak1"),
                                       ("use", "toolu_listen2"), ("result", "toolu_listen2")])
                self.assertNotIn("Bash", json.dumps(rows))
                self.assertNotIn("内部推理", json.dumps(rows, ensure_ascii=False))
                first_user = rows[0]["message"]["content"]
                self.assertTrue(first_user.startswith("[消息发送时间：2026-09-30T15:01:00+08:00]\n"))
                self.assertIn("claude --resume", out)
                self.assertIn("bridge", out)

    def test_no_voice_drops_voice_tools(self):
        self.parallel_voice_session()
        code, out, err = self.run_slim("--all", "--no-voice", "--tz", "Asia/Shanghai")
        self.assertEqual(code, 0, err)
        (path,) = self.outputs()
        self.assertNotIn("tool_use", read_text(path))

    def test_full_strips_thinking_chain_intact(self):
        self.parallel_voice_session()
        code, out, err = self.run_slim("--from", "2026-09-30T15:04:00+08:00",
                                       "--history", "full", "--tz", "Asia/Shanghai")
        self.assertEqual(code, 0, err)
        (path,) = self.outputs()
        rows = read_jsonl(path)
        new_id = os.path.basename(path)[:-6]
        self.assertEqual(slim.validate(rows, new_id), [])
        self.assertIsNone(rows[0]["parentUuid"])
        for prev, cur in zip(rows, rows[1:]):
            self.assertEqual(cur["parentUuid"], prev["uuid"])
        text = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("thinking", text)
        self.assertNotIn("usage", text)
        self.assertTrue(all(r["sessionId"] == new_id for r in rows))
        # 第 0 轮被切掉；第 1 轮原样：prompt + 三个调用各带结果 + 最后一句
        self.assertEqual(rows[0]["message"]["content"], "打个电话吧")
        kinds = [(r["type"], tuple(b["type"] for b in slim.blocks(r))) for r in rows]
        self.assertEqual(kinds, [("user", ()),
                                 ("assistant", ("tool_use",)), ("user", ("tool_result",)),
                                 ("assistant", ("tool_use",)), ("user", ("tool_result",)),
                                 ("assistant", ("tool_use",)), ("user", ("tool_result",)),
                                 ("assistant", ("text",))])
        self.assertIn("Bash", text)  # full 带全部工具
        self.assertIn("剥掉 thinking 1 块", out)

    def test_full_drops_in_flight_call_at_tail(self):
        b = Builder()
        b.prompt("帮我 slim 一下")
        b.assistant([{"type": "text", "text": "好"}], "msg_a")
        b.assistant([use("toolu_running", "Bash")], "msg_a")  # 正在跑的这一次，还没结果
        b.write(self.src)
        code, out, err = self.run_slim("--from", "2026-09-30T15:00:00+08:00", "--history", "full")
        self.assertEqual(code, 0, err)
        (path,) = self.outputs()
        self.assertNotIn("toolu_running", read_text(path))

    def test_full_unpaired_refused(self):
        # 结果在切点之后、调用在切点之前
        b = Builder()
        b.prompt("第一轮")
        b.assistant([use("toolu_x", "Bash")], "msg_a")
        b.prompt("第二轮")
        b.result("toolu_x")
        b.assistant([{"type": "text", "text": "嗯"}], "msg_b")
        b.write(self.src)
        code, out, err = self.run_slim("--from", "2026-09-30T15:03:00+08:00", "--history", "full")
        self.assertEqual(code, 1)
        self.assertIn("不成对", err)
        self.assertEqual(self.outputs(), [])

        # 中间一个没有结果的调用（不是正在跑的最后一个）
        b = Builder()
        b.prompt("第一轮")
        b.assistant([use("toolu_lost", "Bash")], "msg_a")
        b.prompt("第二轮")
        b.assistant([{"type": "text", "text": "嗯"}], "msg_b")
        b.write(self.src)
        code, out, err = self.run_slim("--from", "2026-09-30T15:00:00+08:00", "--history", "full")
        self.assertEqual(code, 1)
        self.assertIn("不成对", err)
        self.assertEqual(self.outputs(), [])

    def test_all_and_full_exclusive(self):
        self.parallel_voice_session()
        code, out, err = self.run_slim("--all", "--history", "full")
        self.assertEqual(code, 1)
        self.assertIn("--all 不能配 --history full", err)
        code, out, err = self.run_slim("--all", "--from", "2026-09-30T15:00:00+08:00")
        self.assertEqual(code, 1)
        self.assertIn("二选一", err)
        code, out, err = self.run_slim()
        self.assertEqual(code, 1)
        self.assertIn("--from", err)
        self.assertEqual(self.outputs(), [])

    def test_dry_run_writes_nothing(self):
        self.parallel_voice_session()
        before = sorted(os.listdir(self.dir))
        for extra in ([], ["--history", "full"]):
            code, out, err = self.run_slim("--from", "2026-09-30T15:00:00+08:00", "--dry-run", *extra)
            self.assertEqual(code, 0, err)
            self.assertIn("dry-run：没有写文件", out)
            self.assertIn("粗估", out)
            self.assertIn("字节", out)
        self.assertEqual(sorted(os.listdir(self.dir)), before)

    def test_list_turns_never_prints_thinking(self):
        self.parallel_voice_session()
        code, out, err = self.run_slim("--list-turns", "--tz", "Asia/Shanghai")
        self.assertEqual(code, 0, err)
        self.assertIn("早上好", out)
        self.assertNotIn("内部推理", out + err)
        code, out, err = self.run_slim("--from", "2026-09-30T15:00:00+08:00", "--history", "full", "--dry-run")
        self.assertNotIn("内部推理", out + err)

    def test_no_tz_prints_local_zone(self):
        self.parallel_voice_session()
        code, out, err = self.run_slim("--all", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("使用本机时区", out)

    def test_existing_output_not_overwritten(self):
        self.parallel_voice_session()
        fixed = uuidlib.UUID("22222222-2222-2222-2222-222222222222")
        target = os.path.join(self.dir, str(fixed) + ".jsonl")
        with open(target, "w", encoding="utf-8") as f:
            f.write("原来的内容\n")
        real = uuidlib.uuid4
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            return fixed if calls["n"] == 1 else real()  # 第一次是新 session id

        with mock.patch.object(slim.uuid, "uuid4", side_effect=fake):
            code, out, err = self.run_slim("--all")
        self.assertEqual(code, 1)
        self.assertIn("不覆盖", err)
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "原来的内容\n")
        with self.assertRaises(FileExistsError):
            slim.write_session(target, b"x")

    def test_new_file_mode_0600(self):
        self.parallel_voice_session()
        code, out, err = self.run_slim("--all")
        self.assertEqual(code, 0, err)
        (path,) = self.outputs()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_validate_catches_bad_sequences(self):
        sid = "33333333-3333-3333-3333-333333333333"

        def row(kind, content, parent, u=None, **extra):
            r = {"parentUuid": parent, "type": kind, "uuid": u or str(uuidlib.uuid4()),
                 "sessionId": sid, "message": {"role": kind, "content": content}}
            r.update(extra)
            return r

        def chain(*specs):
            out, parent = [], None
            for kind, content in specs:
                r = row(kind, content, parent)
                out.append(r)
                parent = r["uuid"]
            return out

        a1 = ("assistant", [use("t1", "x")])
        a2 = ("assistant", [use("t2", "x")])
        r1 = ("user", [{"type": "tool_result", "tool_use_id": "t1", "content": ""}])
        r2 = ("user", [{"type": "tool_result", "tool_use_id": "t2", "content": ""}])

        self.assertEqual(slim.validate(chain(a1, r1, a2, r2), sid), [])
        # 老版 slim.py 的并行 bug 形状
        bad = slim.validate(chain(a1, a2, r1, r2), sid)
        self.assertTrue(any("下一行不是它的 tool_result" in p for p in bad))
        self.assertTrue(any("前一行没有对应的 tool_use" in p for p in bad))
        # 孤儿结果
        self.assertTrue(slim.validate(chain(r1), sid))
        # 没有结果的调用
        self.assertTrue(slim.validate(chain(a1), sid))
        # 非法 id
        self.assertTrue(any("不合法" in p for p in slim.validate(
            chain(("assistant", [use("bad id!", "x")]),
                  ("user", [{"type": "tool_result", "tool_use_id": "bad id!", "content": ""}])), sid)))
        # thinking、usage
        rows = chain(("assistant", [thinking()]))
        self.assertTrue(any("thinking" in p for p in slim.validate(rows, sid)))
        rows = chain(("assistant", [{"type": "text", "text": "x"}]))
        rows[0]["message"]["usage"] = {"input_tokens": 1}
        self.assertTrue(any("usage" in p for p in slim.validate(rows, sid)))
        # 首行 parentUuid 不是 null、断链、uuid 重复、sessionId 不对
        rows = chain(("user", "a"), ("user", "b"))
        rows[0]["parentUuid"] = "x"
        self.assertTrue(any("第 1 行 parentUuid" in p for p in slim.validate(rows, sid)))
        rows = chain(("user", "a"), ("user", "b"), ("user", "c"))
        rows[2]["parentUuid"] = rows[0]["uuid"]
        self.assertTrue(any("没有指向上一行" in p for p in slim.validate(rows, sid)))
        rows = chain(("user", "a"), ("user", "b"))
        rows[1]["uuid"] = rows[0]["uuid"]
        self.assertTrue(any("重复" in p for p in slim.validate(rows, sid)))
        self.assertTrue(any("sessionId" in p for p in slim.validate(chain(("user", "a")), "other")))

    def test_validate_failure_blocks_write(self):
        self.parallel_voice_session()
        with mock.patch.object(slim, "validate", return_value=["人为构造的问题"]):
            code, out, err = self.run_slim("--all")
        self.assertEqual(code, 2)
        self.assertIn("自检没通过", err)
        self.assertEqual(self.outputs(), [])

    def test_missing_timestamp_warned(self):
        b = Builder()
        b.prompt("有时间")
        b.assistant([{"type": "text", "text": "嗯"}], "msg_a")
        p = b.prompt("没时间")
        del p["timestamp"]
        b.assistant([{"type": "text", "text": "嗯嗯"}], "msg_b")
        b.write(self.src)
        code, out, err = self.run_slim("--all", "--dry-run", "--tz", "Asia/Shanghai")
        self.assertEqual(code, 0, err)
        self.assertIn("缺时间戳", out)

    # ---- 第四版补的边界 ----

    def unordered_session(self):
        """一条回复里并行调用 A、B，完成顺序 B→A，后续对话接在 A 的结果下面：B 的调用行和结果都在旁支上。"""
        b = Builder()
        b.prompt("两个一起来")
        u1 = b.assistant([use("toolu_slow", "mcp__voice__listen")], "msg_par")
        u2 = b.assistant([use("toolu_fast", "mcp__tts__speak")], "msg_par")
        b.result("toolu_fast", "fast-result", parent=u2["uuid"])
        b.result("toolu_slow", "slow-result", parent=u1["uuid"])
        b.assistant([{"type": "text", "text": "都好了"}], "msg_end")
        b.write(self.src)
        return b

    @staticmethod
    def tool_seq(rows):
        seq = []
        for r in rows:
            for blk in slim.blocks(r):
                if blk["type"] == "tool_use":
                    seq.append(("use", blk["id"]))
                elif blk["type"] == "tool_result":
                    seq.append(("result", blk["tool_use_id"]))
        return seq

    def test_unordered_parallel_dialogue_keeps_both(self):
        self.unordered_session()
        # 修之前：主线上只有 slow 那一支，fast 的调用行和结果一起丢
        self.assertNotIn("toolu_fast", json.dumps(slim.active_chain(slim.load_rows(self.src))))
        code, out, err = self.run_slim("--all", "--no-time")
        self.assertEqual(code, 0, err)
        (path,) = self.outputs()
        rows = read_jsonl(path)
        self.assertEqual(slim.validate(rows, rows[0]["sessionId"]), [])
        self.assertEqual(self.tool_seq(rows), [("use", "toolu_slow"), ("result", "toolu_slow"),
                                               ("use", "toolu_fast"), ("result", "toolu_fast")])
        self.assertIn("fast-result", read_text(path))
        self.assertIn("源保留段工具调用 2 / 产物 2", out)

    def test_unordered_parallel_full_keeps_both(self):
        self.unordered_session()
        code, out, err = self.run_slim("--from", "2026-09-30T07:00:00Z", "--history", "full")
        self.assertEqual(code, 0, err)
        (path,) = self.outputs()
        rows = read_jsonl(path)
        self.assertEqual(slim.validate(rows, os.path.basename(path)[:-6]), [])
        self.assertEqual(self.tool_seq(rows), [("use", "toolu_slow"), ("result", "toolu_slow"),
                                               ("use", "toolu_fast"), ("result", "toolu_fast")])
        self.assertEqual(rows[-1]["message"]["content"][0]["text"], "都好了")
        self.assertIn("源保留段工具调用 2 / 产物 2", out)
        self.assertIn("捞回的并行旁支 2 行", out)

    def compact_preserved_session(self, shape):
        b = Builder()
        if shape == "reviewer":
            # 保留段在主线上、边界和摘要不在（新对话接在保留段尾巴下面）
            p = b.add("user", "压缩前保留的问", parent="physically-pruned-parent")
            a = b.assistant([{"type": "text", "text": "压缩前保留的答"}], "msg_kept")
            boundary = b.add("system", parent=None, subtype="compact_boundary", content="compacted",
                             compactMetadata={"trigger": "auto"})
            summary = b.add("user", "必须带上的压缩摘要", isCompactSummary=True)
            b.add("user", "压缩后的问", parent=a["uuid"])
        else:
            # 真实会话里见到的形状：边界、摘要在主线上，保留段在边界之前的旁支上
            b.prompt("更早的问")
            p = b.prompt("压缩前保留的问")
            a = b.assistant([{"type": "text", "text": "压缩前保留的答"}], "msg_kept")
            boundary = b.add("system", parent=None, subtype="compact_boundary", content="compacted",
                             compactMetadata={"trigger": "auto"}, logicalParentUuid=a["uuid"])
            summary = b.add("user", "必须带上的压缩摘要", isCompactSummary=True)
            b.prompt("压缩后的问")
        boundary["compactMetadata"]["preservedMessages"] = {"anchorUuid": summary["uuid"],
                                                            "uuids": [p["uuid"], a["uuid"]]}
        boundary["compactMetadata"]["preservedSegment"] = {"anchorUuid": summary["uuid"],
                                                           "headUuid": p["uuid"], "tailUuid": a["uuid"]}
        b.assistant([{"type": "text", "text": "压缩后的答"}], "msg_post")
        b.prompt("再后来的问")
        b.assistant([{"type": "text", "text": "再后来的答"}], "msg_later")
        b.write(self.src)
        return b

    def test_compact_preserved_tail_refused(self):
        for shape in ("reviewer", "native"):
            for args in (["--all"], ["--from", "2026-09-30T07:00:00Z", "--history", "full"],
                         ["--from", "2026-09-30T07:00:00Z", "--dry-run"]):
                with self.subTest(shape=shape, args=args):
                    for p in self.outputs():
                        os.remove(p)
                    self.compact_preserved_session(shape)
                    code, out, err = self.run_slim(*args, "--tz", "UTC")
                    self.assertEqual(code, 1, out + err)
                    self.assertIn("preservedSegment", err)
                    self.assertIn("不写文件", err)
                    self.assertEqual(self.outputs(), [])
            # 切点放在这次压缩之后的轮次：保留段整个落在切掉的部分，可以照常 slim
            with self.subTest(shape=shape, after_compaction=True):
                self.compact_preserved_session(shape)
                code, out, err = self.run_slim("--from", "2026-09-30T07:08:00Z", "--tz", "UTC")
                self.assertEqual(code, 0, err)
                (path,) = self.outputs()
                text = read_text(path)
                self.assertIn("再后来的问", text)
                self.assertNotIn("压缩前保留的", text)
                os.remove(path)

    def test_compact_preserved_already_linked_ok(self):
        # 保留段已经实实在在接在摘要后面（主线上一样不少）：不拒绝
        b = Builder()
        boundary = b.add("system", parent=None, subtype="compact_boundary", content="compacted",
                         compactMetadata={"trigger": "auto"})
        summary = b.add("user", "压缩摘要", isCompactSummary=True)
        p = b.prompt("保留的问")
        a = b.assistant([{"type": "text", "text": "保留的答"}], "msg_kept")
        boundary["compactMetadata"]["preservedMessages"] = {"anchorUuid": summary["uuid"],
                                                            "uuids": [p["uuid"], a["uuid"]]}
        b.prompt("之后的问")
        b.assistant([{"type": "text", "text": "之后的答"}], "msg_post")
        b.write(self.src)
        code, out, err = self.run_slim("--all", "--no-time")
        self.assertEqual(code, 0, err)
        (path,) = self.outputs()
        text = read_text(path)
        for want in ("压缩摘要", "保留的问", "保留的答", "之后的答"):
            self.assertIn(want, text)

    def test_repeat_slim_times_and_cut(self):
        b = Builder()
        b.prompt("P1")                                          # 07:01
        b.assistant([{"type": "text", "text": "A1"}], "msg_a")  # 07:02
        b.prompt("P2")                                          # 07:03
        b.assistant([{"type": "text", "text": "A2"}], "msg_b")  # 07:04
        b.write(self.src)
        code, out, err = self.run_slim("--all", "--tz", "UTC")
        self.assertEqual(code, 0, err)
        (first,) = self.outputs()
        rows = read_jsonl(first)
        times = [(r["type"], r["timestamp"][11:16]) for r in rows]
        self.assertEqual(times, [("user", "07:01"), ("assistant", "07:02"),
                                 ("user", "07:03"), ("assistant", "07:04")])

        out_io, err_io = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_io), contextlib.redirect_stderr(err_io):
            code = slim.main([first, "--from", "2026-09-30T07:03:00Z", "--tz", "UTC"])
        self.assertEqual(code, 0, err_io.getvalue())
        self.assertIn("切掉 1 轮", out_io.getvalue())
        (second,) = [p for p in self.outputs() if p != first]
        rows2 = read_jsonl(second)
        self.assertEqual([r["type"] for r in rows2], ["user", "assistant"])
        # 发送时间前缀不重复叠加
        self.assertEqual(rows2[0]["message"]["content"], "[消息发送时间：2026-09-30T07:03:00+00:00]\nP2")
        self.assertNotIn("A1", read_text(second))

    def test_time_prefix_not_stacked_on_all(self):
        b = Builder()
        b.prompt("[消息发送时间：2026-09-30T15:01:00+08:00]\n已经带过时间的话")
        b.assistant([{"type": "text", "text": "嗯"}], "msg_a")
        b.write(self.src)
        code, out, err = self.run_slim("--all", "--tz", "Asia/Shanghai")
        self.assertEqual(code, 0, err)
        (path,) = self.outputs()
        content = read_jsonl(path)[0]["message"]["content"]
        self.assertEqual(content.count("[消息发送时间："), 1)

    def test_source_preservation_blocks_dropped_call(self):
        # full：构建过程人为丢一对调用（产物本身仍然自洽，validate 拦不住）
        self.unordered_session()
        real_full = slim.build_full_rows

        def lossy_full(*a, **k):
            rows = real_full(*a, **k)
            out = [r for r in rows if "toolu_fast" not in json.dumps(r)]
            parent = None
            for r in out:
                r["parentUuid"] = parent
                parent = r["uuid"]
            return out

        with mock.patch.object(slim, "build_full_rows", side_effect=lossy_full):
            code, out, err = self.run_slim("--from", "2026-09-30T07:00:00Z", "--history", "full")
        self.assertEqual(code, 1, out + err)
        self.assertIn("源侧保全检查没通过", err)
        self.assertIn("源保留段工具调用 2 / 产物 1", out)
        self.assertEqual(self.outputs(), [])

        # dialogue：投影时人为丢一对
        real_project = slim.project_dialogue

        def lossy_project(*a, **k):
            return [e for e in real_project(*a, **k)
                    if not (e[0] == "tool_pair" and e[1]["use"].get("id") == "toolu_fast")]

        with mock.patch.object(slim, "project_dialogue", side_effect=lossy_project):
            code, out, err = self.run_slim("--all", "--dry-run")
        self.assertEqual(code, 1, out + err)
        self.assertIn("源侧保全检查没通过", err)
        self.assertEqual(self.outputs(), [])

    def interjection_session(self, tool="mcp__voice__listen"):
        b = Builder()
        b.prompt("开始")                                                  # 07:01
        b.assistant([use("toolu_a", tool)], "msg_a")                      # 07:02
        r = b.result("toolu_a", "工具结果")                                # 07:03
        r["message"]["content"].append({"type": "text", "text": "<system-reminder>系统注入</system-reminder>"})
        r["message"]["content"].append({"type": "text", "text": "她插的一句话"})
        b.assistant([{"type": "text", "text": "收到插话"}], "msg_b")       # 07:04
        b.write(self.src)
        return b

    def test_interjection_in_tool_result_kept(self):
        # 工具对被保留：插话紧跟那一对；系统注入不带
        self.interjection_session()
        code, out, err = self.run_slim("--all", "--tz", "UTC")
        self.assertEqual(code, 0, err)
        self.assertIn("工具结果行里夹带的人类文字 1 条", out)
        (path,) = self.outputs()
        rows = read_jsonl(path)
        self.assertEqual(slim.validate(rows, rows[0]["sessionId"]), [])
        shape = [(r["type"], tuple(b["type"] for b in slim.blocks(r))) for r in rows]
        self.assertEqual(shape, [("user", ()), ("assistant", ("tool_use",)), ("user", ("tool_result",)),
                                 ("user", ()), ("assistant", ("text",))])
        self.assertEqual(rows[3]["message"]["content"],
                         "[消息发送时间：2026-09-30T07:03:00+00:00]\n她插的一句话")
        self.assertNotIn("系统注入", read_text(path))
        # 二次 slim：插话已经是普通的话，前缀不叠加
        out_io, err_io = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_io), contextlib.redirect_stderr(err_io):
            self.assertEqual(slim.main([path, "--all", "--tz", "UTC"]), 0, err_io.getvalue())
        (second,) = [p for p in self.outputs() if p != path]
        self.assertEqual(read_text(second).count("[消息发送时间："), 2)

    def test_interjection_when_tool_dropped(self):
        # 工具不保留（Bash）：调用和结果丢掉，插话仍然在原位置带过去
        self.interjection_session(tool="Bash")
        code, out, err = self.run_slim("--all", "--no-time")
        self.assertEqual(code, 0, err)
        (path,) = self.outputs()
        rows = read_jsonl(path)
        self.assertEqual([(r["type"], r["message"]["content"] if r["type"] == "user" else None)
                          for r in rows],
                         [("user", "开始"), ("user", "她插的一句话"), ("assistant", None)])
        self.assertNotIn("toolu_a", read_text(path))
        self.assertIn("工具结果行里夹带的人类文字 1 条", out)
        # full 原行照搬：文字块和结果同一行
        code, out, err = self.run_slim("--from", "2026-09-30T07:00:00Z", "--history", "full")
        self.assertEqual(code, 0, err)
        full = [p for p in self.outputs() if p != path][0]
        self.assertIn("她插的一句话", read_text(full))

    def broken_chain_session(self):
        b = Builder()
        b.prompt("断点之前的问")                                           # 07:01
        b.assistant([{"type": "text", "text": "断点之前的答"}], "msg_a")    # 07:02
        b.add("user", "断点之后的问", parent="missing-parent-uuid")          # 07:03
        b.assistant([{"type": "text", "text": "断点之后的答"}], "msg_b")    # 07:04
        b.prompt("再后来的问")                                             # 07:05
        b.assistant([{"type": "text", "text": "再后来的答"}], "msg_c")      # 07:06
        b.write(self.src)

    def test_broken_chain_refused(self):
        self.broken_chain_session()
        for args in (["--all"], ["--from", "2026-09-30T07:00:00Z", "--history", "full"],
                     ["--from", "2026-09-30T07:03:00Z", "--dry-run"]):
            with self.subTest(args=args):
                code, out, err = self.run_slim(*args, "--tz", "UTC")
                self.assertEqual(code, 1, out + err)
                self.assertIn("会话链在 2026-09-30T07:03:00+00:00 附近断开", err)
                self.assertIn("按时间戳", err)
                self.assertEqual(self.outputs(), [])
        code, out, err = self.run_slim("--list-turns", "--tz", "UTC")
        self.assertEqual(code, 0, err)
        self.assertIn("警告：会话链在 2026-09-30T07:03:00+00:00 附近断开", out)

    def test_broken_chain_cut_after_break_ok(self):
        self.broken_chain_session()
        for args in (["--from", "2026-09-30T07:05:00Z"],
                     ["--from", "2026-09-30T07:05:00Z", "--history", "full"]):
            with self.subTest(args=args):
                for p in self.outputs():
                    os.remove(p)
                code, out, err = self.run_slim(*args, "--tz", "UTC")
                self.assertEqual(code, 0, err)
                self.assertIn("警告：会话链在 2026-09-30T07:03:00+00:00 附近断开", out)
                (path,) = self.outputs()
                text = read_text(path)
                self.assertIn("再后来的问", text)
                self.assertNotIn("断点之前", text)

    def test_broken_chain_mid_turn_refused(self):
        # 断点在一轮中间：--from 晚于断点也不能放行，否则只留下半截回答
        b = Builder()
        b.prompt("必须保留的问")                                               # 07:01
        b.assistant([{"type": "text", "text": "回答前半段"}], "msg_a")          # 07:02
        b.add("assistant", [{"type": "text", "text": "断点后的回答"}], parent="missing-parent")  # 07:03
        b.assistant([{"type": "text", "text": "回答结尾"}], "msg_c")            # 07:04
        b.prompt("下一轮的问")                                                 # 07:05
        b.assistant([{"type": "text", "text": "下一轮的答"}], "msg_d")          # 07:06
        b.prompt("第三轮的问")                                                 # 07:07
        b.assistant([{"type": "text", "text": "第三轮的答"}], "msg_e")          # 07:08
        b.write(self.src)
        for history in ("dialogue", "full"):
            with self.subTest(history=history):
                code, out, err = self.run_slim("--from", "2026-09-30T07:03:30Z", "--history", history,
                                               "--no-time", "--tz", "UTC")
                self.assertEqual(code, 1, out + err)
                self.assertIn("断点落在一轮的中间", err)
                self.assertIn("2026-09-30T07:06:00+00:00", err)
                self.assertEqual(self.outputs(), [])
        # 挪到混着残片的那一轮之后：只留第三轮，照常写，带断链警告
        code, out, err = self.run_slim("--from", "2026-09-30T07:06:30Z", "--tz", "UTC")
        self.assertEqual(code, 0, err)
        self.assertIn("警告：会话链", out)
        (path,) = self.outputs()
        text = read_text(path)
        self.assertIn("第三轮的问", text)
        self.assertNotIn("断点后的回答", text)


if __name__ == "__main__":
    unittest.main()
