#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
dsh_headless.py — 通用 DSH 接入层（零依赖，任何 Python 应用可直接用）

只做一件事：把 `dsh headless --json` 的 JSONL 事件流包装成 Python 生成器 / SSE。

    from dsh_headless import stream_task, ask

    for ev in stream_task("解释这个仓库", cwd="/path"):
        print(ev["type"], ev.get("text", ""))

    r = ask("写个 README", cwd="/path")
    print(r["text"], r["session_id"], r["usage"])

设计约束（踩过的坑）：
  1. 任务走 stdin，不拼命令行（中文/引号/反斜杠会被 shell 吃掉）
  2. Windows 上 dsh 是 .cmd，必须 cmd /c 包装
  3. stderr 单独收，否则污染 JSONL
  4. 超时自己算，超时 kill
"""
import json, os, shutil, subprocess, sys, threading, time
from typing import Iterator, Optional

__all__ = ["stream_task", "ask", "DshHeadless", "DshError"]


class DshError(RuntimeError):
    pass


def _resolve(dsh: str) -> list:
    """返回可直接交给 Popen 的前缀命令"""
    exe = shutil.which(dsh) or dsh
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe]
    return [exe]


def stream_task(task: str,
                cwd: Optional[str] = None,
                session_id: Optional[str] = None,
                dsh: str = "dsh",
                profile: str = "headless",
                extra_args: Optional[list] = None,
                env: Optional[dict] = None,
                timeout: float = 900.0) -> Iterator[dict]:
    """逐条产出 dsh 事件；结束（final / 进程退出）即停止。"""
    prefix = _resolve(dsh)
    args = prefix + ["--profile", profile] + list(extra_args or []) + ["--json"]
    if session_id:
        args += ["--session-id", session_id]
    args.append("-")

    e = {**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})}
    proc = subprocess.Popen(args, cwd=cwd or os.getcwd(), env=e,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)
    try:
        proc.stdin.write(task.encode("utf-8"))
        proc.stdin.close()
    except Exception as ex:
        proc.kill()
        raise DshError("写入任务失败: %s" % ex)

    err = []
    th = threading.Thread(target=lambda: err.append(
        proc.stderr.read().decode("utf-8", "replace")), daemon=True)
    th.start()

    deadline = time.time() + timeout
    try:
        while True:
            if time.time() > deadline:
                proc.kill()
                yield {"type": "error", "message": "timeout %.0fs" % timeout}
                return
            raw = proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield {"type": "_raw", "text": line}
    finally:
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        th.join(timeout=2)

    code = proc.returncode
    if code not in (0, None):
        tail = "".join(err).strip()[-800:]
        yield {"type": "error", "message": "exit %s: %s" % (code, tail)}


def ask(task: str, **kw) -> dict:
    """跑完一轮，返回 {text, session_id, usage, events}"""
    text, sid, usage, events = "", None, None, []
    for ev in stream_task(task, **kw):
        events.append(ev)
        t = ev.get("type")
        if t == "session":
            sid = ev.get("sessionId")
        elif t == "text":
            text += ev.get("text", "")
        elif t == "final":
            text = ev.get("text", text)
        elif t == "status" and ev.get("usage"):
            usage = ev["usage"]
    return {"text": text, "session_id": sid, "usage": usage, "events": events}


class DshHeadless:
    """带会话记忆的轻封装"""

    def __init__(self, cwd: Optional[str] = None, **kw):
        self.cwd = cwd or os.getcwd()
        self.kw = kw
        self.session_id = None

    def stream(self, task: str, **over) -> Iterator[dict]:
        kw = {**self.kw, **over, "cwd": self.cwd}
        for ev in stream_task(task, session_id=self.session_id, **kw):
            if ev.get("type") == "session" and ev.get("sessionId"):
                self.session_id = ev["sessionId"]
            yield ev

    def ask(self, task: str, **over) -> dict:
        kw = {**self.kw, **over, "cwd": self.cwd}
        r = ask(task, session_id=self.session_id, **kw)
        if r["session_id"]:
            self.session_id = r["session_id"]
        return r

    def reset(self):
        self.session_id = None


# ---------------- 应用侧：把事件流转成 SSE ----------------
def sse_lines(task: str, **kw) -> Iterator[str]:
    """给 HTTP 端点用：每行 'data: {json}\\n\\n'"""
    for ev in stream_task(task, **kw):
        yield "data: %s\n\n" % json.dumps(ev, ensure_ascii=False)


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) or "reply with OK"
    for ev in stream_task(task, cwd=os.getcwd()):
        t = ev.get("type")
        if t == "text":
            sys.stdout.write(ev.get("text", ""))
            sys.stdout.flush()
        elif t == "final":
            print()
        elif t == "error":
            print("\n[error]", ev.get("message"), file=sys.stderr)
