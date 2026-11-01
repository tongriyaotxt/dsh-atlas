#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
serve.py — DSH Atlas 本地桥（v2：内嵌 TUI 终端）

两个通道：
  1) 文件通道（旧）：POST /context → .dsh/outbox/context.json
                     GET  /patch   → .dsh/inbox/patch.json
  2) 终端通道（新）：POST /tui/start  {cmd}   启动本地 TUI（默认 dsh）
                     GET  /tui/stream        SSE 推流（stdout/stderr）
                     POST /tui/input  {text} 写入 stdin
                     POST /tui/stop
                     GET  /tui/status

启动：python bridge/serve.py [port]     默认 8777
页面：http://127.0.0.1:<port>/
"""
import os, sys, json, time, queue, threading, subprocess, datetime, http.server, socketserver
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dsh_headless

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DSH = os.path.join(ROOT, ".dsh")
OUT = os.path.join(DSH, "outbox")
IN = os.path.join(DSH, "inbox")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
DEFAULT_CMD = os.environ.get("KG_TUI_CMD", "dsh dsh-tui")

# ---------------- TUI 进程管理 ----------------
class TUI:
    def __init__(self):
        self.proc = None
        self.q = queue.Queue(maxsize=4000)
        self.lock = threading.Lock()
        self.cmd = None
        self.started = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, cmd):
        with self.lock:
            if self.running():
                return False, "already running"
            try:
                env = dict(os.environ)
                env["PYTHONIOENCODING"] = "utf-8"
                env["PYTHONUNBUFFERED"] = "1"
                self.proc = subprocess.Popen(
                    cmd, shell=True, cwd=ROOT, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, bufsize=0)
            except Exception as e:
                return False, str(e)
            self.cmd = cmd
            self.started = time.time()
            threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
            self._push("\x1b[90m[bridge] 已启动: %s (pid %s)\x1b[0m\n"
                       % (cmd, self.proc.pid))
            return True, "started"

    def _reader(self, proc):
        try:
            while True:
                chunk = proc.stdout.read(1024)
                if not chunk:
                    break
                self._push(chunk.decode("utf-8", "replace"))
        except Exception as e:
            self._push("\n[bridge] read failed: %s\n" % e)
        finally:
            code = proc.poll()
            self._push("\n\x1b[90m[bridge] 进程结束 (exit %s)\x1b[0m\n"
                       % ("-" if code is None else code))

    def _push(self, text):
        try:
            self.q.put_nowait(text)
        except queue.Full:
            pass

    def write(self, text):
        if not self.running():
            return False
        try:
            self.proc.stdin.write(text.encode("utf-8"))
            self.proc.stdin.flush()
            return True
        except Exception:
            return False

    def stop(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
                return True
            return False

    def status(self):
        return {
            "running": self.running(),
            "cmd": self.cmd,
            "pid": self.proc.pid if self.proc else None,
            "uptime": round(time.time() - self.started, 1) if self.started else 0,
        }


T = TUI()

SCRIPT_DIR = os.path.join(DSH, "scripts")
TUI_LOG = os.path.join(DSH, "tui.log")
APP_STATE = os.path.join(ROOT, "state", "graph.json")
DOCS_DIR = os.path.join(ROOT, "docs")


# ---------------- 向窗口投递真实键盘输入（TUI 直连） ----------------
def _kbd_api():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    return ctypes, wintypes, user32, INPUT


def _enum_windows():
    ctypes, wintypes, user32, _ = _kbd_api()
    out = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        out.append({"hwnd": hwnd, "title": title, "pid": pid.value})
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out


def _wt_pids():
    try:
        import subprocess, json
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WindowsTerminal.exe",
                            "/FO", "CSV", "/NH"], capture_output=True, text=True)
        pids = set()
        for line in (r.stdout or "").splitlines():
            parts = [x.strip('"') for x in line.split('","')]
            if len(parts) > 1 and parts[1].isdigit():
                pids.add(int(parts[1]))
        return pids
    except Exception:
        return set()


TUI_TITLE = "DSH TUI"
TUI_WIN = {"hwnd": None, "pid": None, "at": None}


def capture_new_window(before, timeout=20):
    """启动前后差分：找出新出现的 wt 窗口，锁定它的 hwnd"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.6)
        pids = _wt_pids()
        for w in _enum_windows():
            if w["hwnd"] in before or w["pid"] not in pids:
                continue
            TUI_WIN.update(hwnd=w["hwnd"], pid=w["pid"], at=time.time())
            return w
    return None


def get_locked_window():
    """返回锁定的窗口；若已失效返回 None"""
    import ctypes
    h = TUI_WIN.get("hwnd")
    if not h:
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.IsWindow(h):
        TUI_WIN.update(hwnd=None, pid=None, at=None)
        return None
    return h


def find_target(title_part=TUI_TITLE):
    """只认标题含 DSH TUI 的窗口。绝不按进程/序号兜底——那会把按键打进用户的会话。"""
    wins = _enum_windows()
    want = (title_part or TUI_TITLE).lower()
    hit = [w for w in wins
           if want in (w["title"] or "").lower()
           and w["pid"] in _wt_pids()]
    if hit:
        return hit[0], "title-match"
    return None, "not-found"


def _key_unicode(ch):
    ctypes, wintypes, user32, INPUT = _kbd_api()
    for flags in (0x0004, 0x0004 | 0x0002):
        inp = INPUT()
        inp.type = 1
        inp.ki.wVk = 0
        inp.ki.wScan = ord(ch)
        inp.ki.dwFlags = flags
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.003)


def _key_vk(vk):
    ctypes, wintypes, user32, INPUT = _kbd_api()
    for flags in (0, 0x0002):
        inp = INPUT()
        inp.type = 1
        inp.ki.wVk = vk
        inp.ki.wScan = 0
        inp.ki.dwFlags = flags
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.003)


def _window_ready(hwnd, timeout=45):
    """dsh-tui 接管后会把窗口标题改成有意义的内容；空标题 = 还没起来"""
    import ctypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    t0 = time.time()
    while time.time() - t0 < timeout:
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            core = "".join(ch for ch in buf.value
                           if not (0x2000 <= ord(ch) <= 0x2BFF))   # 去掉图标/盲文
            if len(core.strip()) >= 2:
                return True
        time.sleep(1)
    return False


def _activate(hwnd):
    """可靠激活：AttachThreadInput 绕过 SetForegroundWindow 的前台限制"""
    import ctypes
    ctypes_, wintypes, user32, _ = _kbd_api()
    if user32.GetForegroundWindow() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None)
    tid_t = user32.GetWindowThreadProcessId(hwnd, None)
    attached = False
    if tid_fg and tid_t and tid_fg != tid_t:
        attached = bool(user32.AttachThreadInput(tid_fg, tid_t, True))
    user32.ShowWindow(hwnd, 9)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    if attached:
        user32.AttachThreadInput(tid_fg, tid_t, False)
    time.sleep(0.45)
    return user32.GetForegroundWindow() == hwnd


def type_into(text, title_part="", enter=True):
    hwnd = get_locked_window()
    if not hwnd:
        win, how = find_target(TUI_TITLE)
        if not win:
            return False, ("还没有锁定目标窗口。先点「在 wt 中打开」——"
                           "系统会捕获新窗口并锁定；之后输入只进那个窗口。")
        hwnd = win["hwnd"]
        TUI_WIN.update(hwnd=hwnd, pid=win["pid"], at=time.time())

    # splash 画面也能接受输入；只做短检查，不做长阻塞
    _window_ready(hwnd, 6)

    if not _activate(hwnd):
        return False, ("窗口没能切到前台（hwnd=%s）。请手动点一下 wt 里那个 "
                       "DSH TUI 窗口，再发送一次。" % hwnd)

    if text:
        for ch in text:
            _key_unicode(ch)
    if enter:
        _key_vk(0x0D)

    return True, "hwnd=%s chars=%d" % (hwnd, len(text or ""))



def _shell_exec(file, params, cwd=None):
    import ctypes
    try:
        r = ctypes.windll.shell32.ShellExecuteW(None, "open", file, params, cwd, 1)
        return r > 32, str(r)
    except Exception as e:
        return False, str(e)


def launch_wt(mode, task, cmd):
    os.makedirs(SCRIPT_DIR, exist_ok=True)
    tag = datetime.datetime.now().strftime("%H%M%S")
    sp = os.path.join(SCRIPT_DIR, "run_%s.ps1" % tag)

    if mode == "headless":
        safe = (task or "").replace("'", "''")
        body = ("$ErrorActionPreference='Continue'\n"
                "Set-Location '%s'\n"
                "$log = '%s'\n"
                "function Out-Both($l){ Write-Host $l; Add-Content -Path $log -Value $l -Encoding UTF8 }\n"
                '"=== DSH headless @ $(Get-Date -Format \'HH:mm:ss\') ===" | ForEach-Object { Out-Both $_ }\n'
                "dsh headless '%s' 2>&1 | ForEach-Object { Out-Both $_ }\n"
                '"=== done @ $(Get-Date -Format \'HH:mm:ss\') ===" | ForEach-Object { Out-Both $_ }\n'
                % (ROOT, TUI_LOG, safe))
        title = "DSH headless"
    else:
        body = ("$ErrorActionPreference='Continue'\n"
                "Set-Location '%s'\n"
                "%s\n" % (ROOT, cmd or DEFAULT_CMD))
        title = "DSH TUI"

    with open(sp, "w", encoding="utf-8-sig") as f:
        f.write(body)

    wt_params = ('-w -1 new-tab --title "%s" powershell -NoExit '
                 '-ExecutionPolicy Bypass -File "%s"' % (title, sp))
    before = {w["hwnd"] for w in _enum_windows()}
    ok, info = _shell_exec("wt.exe", wt_params, ROOT)
    if ok:
        threading.Thread(target=capture_new_window, args=(before,), daemon=True).start()
        return True, sp + " (wt, ret=%s)" % info

    ps_params = '-NoExit -ExecutionPolicy Bypass -File "%s"' % sp
    ok2, info2 = _shell_exec("powershell.exe", ps_params, ROOT)
    if ok2:
        return True, sp + " (powershell fallback, wt ret=%s)" % info

    return False, "wt ret=%s / powershell ret=%s" % (info, info2)


def state_path(name):
    """每个应用一个状态文件：state/<name>.json"""
    safe = "".join(c for c in (name or "graph") if c.isalnum() or c in "-_")
    os.makedirs(os.path.join(ROOT, "state"), exist_ok=True)
    return os.path.join(ROOT, "state", (safe or "graph") + ".json")


def ensure():
    for d in (DSH, OUT, IN):
        os.makedirs(d, exist_ok=True)


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_md(payload):
    L = payload.get("L", {}).get("nodes", [])
    R = payload.get("R", {}).get("nodes", [])
    Le = payload.get("L", {}).get("edges", [])
    Re = payload.get("R", {}).get("edges", [])
    lines = ["# 设计请求 · %s" % payload.get("ts", ""), "",
             "- 左图技术节点：%d　右图设计节点：%d" % (len(L), len(R)),
             "- 边：%d + %d" % (len(Le), len(Re)), "",
             "- 请求类型：%s" % payload.get("request", ""), "",
             "## 设计节点（局部约束）", ""]
    for n in R:
        lines.append("- **%s · %s**　关联技术：%s" % (
            n["id"], n["title"], ", ".join(n.get("refs") or []) or "-"))
        if n.get("note"):
            lines.append("  - 说明：%s" % n["note"])
        for f in (n.get("fails") or []):
            lines.append("  - 失效：%s" % f)
    lines += ["", "## 技术节点（先验）", ""]
    for n in L:
        lines.append("- `%s` %s · %s · 失效：%s" % (
            n["id"], n["title"], n.get("kind", ""),
            "；".join(n.get("fails") or []) or "-"))
    lines += ["", "## 关系边", ""]
    for e in Le:
        lines.append("- %s -%s-> %s%s" % (e["from"], e["rel"], e["to"],
                                          ("：" + e["note"]) if e.get("note") else ""))
    lines.append("")
    for e in Re:
        lines.append("- %s -%s-> %s%s" % (e["from"], e["rel"], e["to"],
                                          ("：" + e["note"]) if e.get("note") else ""))
    with open(os.path.join(OUT, "context.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


class H(http.server.BaseHTTPRequestHandler):
    server_version = "DshAtlasBridge/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (now(), fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_OPTIONS(self):
        self._send(204)

    # ---------------- GET ----------------
    def do_GET(self):
        ensure()
        p = self.path.split("?")[0]

        if p.startswith("/demo"):
            fp = os.path.join(ROOT, "demo-app", "index.html")
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            return self._send(404, "demo not found", "text/plain; charset=utf-8")

        if p in ("/", "/index.html"):
            fp = os.path.join(ROOT, "index.html")
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            return self._send(404, "index.html not found", "text/plain; charset=utf-8")

        if p == "/health":
            return self._json({"ok": True, "at": now(), "tui": T.status()})

        if p == "/tui/status":
            return self._json(T.status())

        if p == "/state":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            fp = state_path((q.get("name") or ["graph"])[0])
            if not os.path.exists(fp):
                return self._json({"ok": False, "msg": "no state"}, 404)
            with open(fp, "rb") as f:
                return self._send(200, f.read())

        if p == "/docs":
            items = []
            if os.path.isdir(DOCS_DIR):
                for fn in sorted(os.listdir(DOCS_DIR)):
                    if fn.endswith((".md", ".txt")):
                        fp = os.path.join(DOCS_DIR, fn)
                        items.append({"name": fn,
                                      "bytes": os.path.getsize(fp),
                                      "mtime": datetime.datetime.fromtimestamp(
                                          os.path.getmtime(fp)).strftime("%m-%d %H:%M")})
            return self._json({"ok": True, "docs": items})

        if p.startswith("/docs/"):
            name = os.path.basename(p[len("/docs/"):])
            fp = os.path.join(DOCS_DIR, name)
            if not os.path.exists(fp):
                return self._json({"ok": False, "msg": "not found"}, 404)
            with open(fp, "rb") as f:
                return self._send(200, f.read(), "text/plain; charset=utf-8")

        if p == "/tui/log":
            if not os.path.exists(TUI_LOG):
                return self._json({"ok": False, "text": ""}, 404)
            with open(TUI_LOG, "rb") as f:
                raw = f.read()
            if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                txt = raw.decode("utf-16", "replace")
            else:
                txt = raw.decode("utf-8", "replace")
                if "\x00" in txt[:300]:
                    txt = raw.decode("utf-16-le", "replace")
            return self._json({"ok": True, "bytes": len(raw), "text": txt[-60000:]})

        if p == "/tui/stream":
            return self._sse()

        if p == "/patch":
            fp = os.path.join(IN, "patch.json")
            if not os.path.exists(fp):
                return self._json({"ok": False, "msg": "no patch yet"}, 404)
            with open(fp, "rb") as f:
                return self._send(200, f.read())

        if p == "/context":
            fp = os.path.join(OUT, "context.json")
            if not os.path.exists(fp):
                return self._json({"ok": False, "msg": "no context yet"}, 404)
            with open(fp, "rb") as f:
                return self._send(200, f.read())

        if p == "/status":
            def st(d, name):
                fp = os.path.join(d, name)
                if not os.path.exists(fp):
                    return {"exists": False}
                return {"exists": True,
                        "mtime": datetime.datetime.fromtimestamp(
                            os.path.getmtime(fp)).strftime("%H:%M:%S"),
                        "bytes": os.path.getsize(fp)}
            return self._json({"ok": True, "root": ROOT, "port": PORT,
                               "context": st(OUT, "context.json"),
                               "patch": st(IN, "patch.json"),
                               "tui": T.status()})

        self._json({"ok": False, "msg": "unknown route"}, 404)

    def _sse(self):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 1500\n\n")
            self.wfile.flush()
            while True:
                try:
                    chunk = T.q.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps({"t": chunk}, ensure_ascii=False)
                self.wfile.write(("data: %s\n\n" % payload).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        except Exception:
            return

    # ---------------- POST ----------------
    def do_POST(self):
        ensure()
        p = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""

        def body():
            try:
                return json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                return {}

        if p == "/tui/start":
            b = body()
            cmd = (b.get("cmd") or DEFAULT_CMD).strip() or DEFAULT_CMD
            ok, msg = T.start(cmd)
            return self._json({"ok": ok, "msg": msg, "tui": T.status()})

        if p == "/dsh/ask":
            b = body()
            task = (b.get("task") or "").strip()
            if not task:
                return self._json({"ok": False, "msg": "empty task"}, 400)
            sid = b.get("session_id") or None
            extra = b.get("extra_args") or None
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def put(data):
                if not data:
                    return
                b = data.encode("utf-8") if isinstance(data, str) else data
                self.wfile.write(b"%x\r\n" % len(b))
                self.wfile.write(b)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            try:
                for chunk in dsh_headless.sse_lines(task, cwd=ROOT, session_id=sid,
                                                   extra_args=extra):
                    put(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            except Exception as ex:
                try:
                    put("data: %s\n\n" % json.dumps(
                        {"type": "error", "message": str(ex)}, ensure_ascii=False))
                except Exception:
                    pass
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass
            return

        if p == "/state":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            fp = state_path((q.get("name") or ["graph"])[0])
            b = body()
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(b, f, ensure_ascii=False, indent=2)
            return self._json({"ok": True, "file": fp,
                               "bytes": os.path.getsize(fp)})

        if p == "/tui/type":
            b = body()
            ok, info = type_into(b.get("text", ""), b.get("title", "dsh"),
                                 b.get("enter", True))
            return self._json({"ok": ok, "info": info})

        if p == "/tui/launch":
            b = body()
            ok, info = launch_wt(b.get("mode", "tui"), b.get("task", ""),
                                 b.get("cmd") or DEFAULT_CMD)
            return self._json({"ok": ok, "info": info, "log": TUI_LOG})

        if p == "/tui/input":
            b = body()
            text = b.get("text", "")
            if b.get("enter", True):
                text += "\n"
            ok = T.write(text)
            return self._json({"ok": ok})

        if p == "/tui/stop":
            return self._json({"ok": T.stop()})

        if p == "/context":
            payload = body()
            if not payload:
                return self._json({"ok": False, "msg": "bad json"}, 400)
            payload.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
            with open(os.path.join(OUT, "context.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            try:
                write_md(payload)
            except Exception as e:
                sys.stderr.write("write_md failed: %s\n" % e)
            old = os.path.join(IN, "patch.json")
            if os.path.exists(old):
                os.replace(old, os.path.join(IN, "patch.prev.json"))
            with open(os.path.join(DSH, "request.log"), "a", encoding="utf-8") as f:
                f.write("%s  context received  L=%d R=%d\n" % (
                    now(), len(payload.get("L", {}).get("nodes", [])),
                    len(payload.get("R", {}).get("nodes", []))))
            return self._json({"ok": True, "bytes": len(raw)})

        if p == "/applied":
            with open(os.path.join(IN, "applied.log"), "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (now(), raw.decode("utf-8", "replace")[:2000]))
            return self._json({"ok": True})

        self._json({"ok": False, "msg": "unknown route"}, 404)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    ensure()
    print("DSH Atlas bridge on http://127.0.0.1:%d" % PORT)
    print("  page  : http://127.0.0.1:%d/" % PORT)
    print("  tui   : POST /tui/start {cmd}   (default: %s)" % DEFAULT_CMD)
    print("  outbox: %s" % OUT)
    print("  inbox : %s" % IN)
    print("  Ctrl+C to stop")
    with Server(("127.0.0.1", PORT), H) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            T.stop()
            print("\nbye")
