# DSH Atlas · 科研知识图谱工作台

> 左边技术推导树，右边设计方案，DSH 在中间帮你把图写成能进评审的文档。
> Atlas = 图谱集：解剖图谱、脑图谱、天体图集，都是这个用法。

> DSH 通过 `dsh headless --json` 接入：任务走 stdin，事件流实时回，会话自动续接。
> 应用把状态写成文件，DSH 直接读写 —— **同一套 bridge 可以驱动任何 app**。

截图：`shot_dsh_new.png`（DSH 面板）· `shot_demo.png`（通用性 demo）· `shot_tree.png`（推导树）

---

## 一、三个端点就是全部

| 端点 | 作用 |
|---|---|
| `POST /dsh/ask` | `{task, session_id}` → **SSE 事件流**（session/text/status/final/error） |
| `GET|POST /state?name=<app>` | 应用状态读写，`state/<app>.json`，**不同 app 天然隔离** |
| `GET /docs` · `GET /docs/<name>` | 文档列表 / 内容 |

启动：

```bash
python bridge/serve.py            # 127.0.0.1:8777
# http://127.0.0.1:8777/         DSH Atlas
# http://127.0.0.1:8777/demo     通用性 demo
```

---

## 二、DSH 怎么接进来

后端 `bridge/dsh_headless.py`（零依赖，可直接抄到别的项目）：

```python
from dsh_headless import stream_task, ask

for ev in stream_task("解释这个仓库", cwd="/proj"):
    if ev["type"] == "text":
        print(ev["text"], end="")

r = ask("写个 README", cwd="/proj")
print(r["text"], r["session_id"], r["usage"])
```

前端：`fetch("/dsh/ask")` + `ReadableStream` 逐块解析 `data: {...}\n\n`。

**会话续接**：把 `session` 事件里的 `sessionId` 存下来，下次请求带上即可（实测第二轮能记住上一轮设定的代号）。

---

## 三、基础操作能力：状态文件就是 API

DSH 不需要专门的工具接口 —— 它用自带的 fs/bash 工具直接改文件。

```
app 状态  →  state/<app>.json   ← DSH 读、改
产出文档  →  docs/*.md          ← DSH 写，界面列出来
```

前端每 2 秒检查状态文件的 `updated` 字段，变了就重载画布。这样：

- **人**在界面上改 → 写回文件 → DSH 下次读得到
- **DSH** 改文件 → 界面自动刷新 → 人立刻看见

不需要 WebSocket 双向协议，不需要自定义命令集。

---

## 四、实测结果

| 验证项 | 结果 |
|---|---|
| `POST /dsh/ask` SSE 流 | ✅ 事件序列 `session → status → thinking → text → status → final` 全到 |
| 会话续接 | ✅ 第一轮记 "ORION-7"，第二轮答出 `ORION-7`，`same sid: True` |
| token 统计 | ✅ `status.step_end.usage` 透传到前端 |
| **DSH Atlas 写文档** | ✅ 读 `state/graph.json` → 生成 `docs/design.md`，**13.3 KB / 151 行**，五节结构齐全 |
| **通用性：第二个 app** | ✅ `demo-app` 用同一 bridge、不同状态文件，DSH 读 `state/notes.json` → 生成 `docs/demo-notes.md` |

两个 app 完全不同的状态结构（一个是图，一个是列表），走同一套端点，DSH 都能读懂并产出文档。

---

## 五、为什么这样是"通用"的

三个约束一满足，任何 app 都能接：

1. **状态可序列化** —— 有 `toJSON()` 就能存 `state/<app>.json`
2. **有一张文档出口** —— `docs/` 目录
3. **有一个任务输入框** —— 一个 textarea + 一次 fetch

不需要为每个 app 写专用的工具协议。DSH 的 fs/bash 就是通用接口。

---

## 六、踩过的坑（已修）

| 坑 | 表现 | 修法 |
|---|---|---|
| Windows 上 `dsh` 是 `.cmd` | `Popen` 报 `FileNotFoundError` | `shutil.which()` 解析 + `cmd /c` 包装 |
| HTTP/1.1 下 SSE 卡住 | 客户端一直等，收不到数据 | 发 `Transfer-Encoding: chunked` 并手写 chunk 帧 |
| 任务拼命令行 | 中文/引号/反斜杠被 shell 吃掉 | 任务走 **stdin**，参数只留 `-` |
| `--patch` 位置 | `dsh headless --patch` 报 unknown option | 必须在 profile 名之前：`dsh --profile headless --patch x.yml --json -` |
| headless 没有 MCP | 实测 `NO_MCP` | 用 `--patch` 叠加 MCP 配置（见 `../dsh-bridge/mcp.patch.yml`） |

---

## 七、文件

```
dsh-atlas/
├── index.html              双画布 + DSH 面板（headless 流式）
├── bridge/
│   ├── serve.py            静态页 + /dsh/ask + /state + /docs
│   ├── dsh_headless.py     通用接入层（零依赖，可抄走）
│   └── DSH.md              给 agent 的规则手册
├── demo-app/index.html     通用性验证：另一个 app 用同一 bridge
├── state/                  graph.json（KG）/ notes.json（demo）
├── docs/                   design.md / demo-notes.md（DSH 生成）
└── tools/kgctl.py          文件通道 CLI（备用）
```

---

## 八、一句话

> **app 把状态写成文件，DSH 用 fs 工具改文件，界面轮询文件变化。**
> 这就是全部协议 —— 不需要 SDK，不需要双向 RPC，不需要为每个 app 定制工具。
