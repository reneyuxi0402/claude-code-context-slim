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


if __name__ == "__main__":
    unittest.main()
