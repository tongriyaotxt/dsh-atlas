# 给 DSH 的操作手册

你（agent）在这个项目里的唯一职责：**读上下文 → 出 patch → 写回 inbox**。
不要改图、不要替人做决定、不要给完整方案。

---

## 一、工作流（三条命令）

```bash
cd kg-studio
python tools/kgctl.py status                 # 1. 有没有新请求
python tools/kgctl.py context                # 2. 读上下文（Markdown，含硬约束）
#    … 生成 patch.json …
python tools/kgctl.py write-patch patch.json # 3. 写回 inbox
```

写完告诉人：「patch 已写入，页面点『② 拉取 patch』或开启自动轮询」。

如果 patch 含不合规 op，`write-patch` 会整体拒绝。两个选择：
- 修好再写（推荐）
- `--partial` 只写入合规的 op，其余被 DROP 并打印原因

---

## 二、上下文在哪

| 文件 | 内容 |
|---|---|
| `.dsh/outbox/context.json` | 完整图数据（L = 技术图谱，R = 设计，edges，refs） |
| `.dsh/outbox/context.md` | 同一份内容的可读摘要，**优先读这个** |
| `.dsh/request.log` | 请求时间线 |

`context.json` 的 `contextText` 字段是页面已经写好的 prompt（含规则），可直接用。

---

## 三、输出格式（严格 JSON）

```json
{
  "base": "kg-design",
  "ops": [
    {"op":"add_node","pane":"L","id":"T17","title":"...","kind":"recon",
     "note":"文献","fails":["失效1","失效2"]},

    {"op":"add_edge","pane":"L","from":"T17","to":"T11","rel":"improves","note":"为什么"},
    {"op":"add_edge","pane":"L","from":"T17","to":"T10","rel":"fails_when","note":"共享失效：..."},

    {"op":"annotate","pane":"L","target":"T11","field":"fails","value":"新的失效条件"},

    {"op":"link","design":"S6","tech":"T17"}
  ],
  "gaps": {
    "certain":   ["我确定的，附依据"],
    "uncertain": ["我不确定的，且依赖你的哪条局部信息"],
    "need_from_you": ["具体可回答的问题 1", "具体可回答的问题 2"]
  }
}
```

`op` 只有四种：`add_node` / `add_edge` / `annotate` / `link`。
`pane` 只能是 `L`（技术图谱）或 `R`（设计）。`link` 是唯一跨图操作。

---

## 四、硬规则（违反会被拒）

1. **`add_node` 必须带 `fails`（≥1 条）** — 不允许"不会坏"的技术。
2. **`kind` 必须是** stat / near / recon / prob / sup / misc / design 之一。
3. **`add_edge.rel` ∈** improves / replaces / combines / requires / fails_when / uses。
4. **`rel=replaces` 必须带 `note`**，写明"在什么条件下替代"。
5. **`link` 两端必须已存在**（如果同时 `add_node`，把它放在 `link` 之前）。
6. **`gaps.need_from_you` ≥ 2 条**。少于 2 条会警告：说明你给了"完备答案"，
   通常意味着判断被你代做了。

---

## 五、禁止事项

- ❌ 不要输出完整方案、长篇分析、方法论建议。
- ❌ 不要重复图里已有的技术（那是噪声）。要复用就提 `link`。
- ❌ 不要在没有 `probe`/代价的情况下断言"这个方案更好"——人无法裁决。
- ❌ 不要改 `.dsh/outbox/*`（那是人写的）。
- ❌ 不要越过 inbox 直接改 `index.html` 的 localStorage（做不到，也不该做）。

---

## 六、怎么出好 patch

优先级从高到低：

1. **找共享失效**：两个不同范式的方法如果有同一个死法，加一条
   `fails_when` 边，并说明"换汤不换药"的具体机制。这是最有价值的输出。
2. **补缺失的替代方案**：右图某节点的 `refs` 只有 1 个技术，而左图存在
   在约束下更合适的候选 → 提 `link` 并说明匹配哪条局部约束。
3. **指出被误判的失效条件**：右图节点写的失效条件与你了解的文献不符 →
   提 `annotate` 补充。
4. **标注前提不满足**：某技术的 `assumptions` 与右图 `note`/`fails`
   里写明的局部约束冲突 → 明确指出冲突点。

每条 patch 都要能被一句话裁决：**"这条假设成不成立？成立的话我损失多少？"**

---

## 七、参考

- `tools/kgctl.py` 的 `validate()` 是权威校验逻辑，照它写就不会被拒。
- `examples/patch_dsh.json` 是一个示例（故意含 2 个不合规 op，用来演示拦截）。
- 协议细节见 `README.md`。

---

## 八、终端模式（推荐）

页面 DSH 栏已内嵌终端，等价于在你面前开一个 shell 跑 `dsh dsh-tui`。
你（agent）看到的是同一条管道的另一端。

### 8.1 三种连接方式

| 方式 | 命令 | 用途 |
|---|---|---|
| 交互 TUI | `dsh dsh-tui` | 长会话，人在网页里直接敲 |
| 一次性任务 | `dsh headless "任务"` | 出 patch，跑完即退，输出干净 |
| 文件通道 | POST /context、GET /patch | 无终端时的备用 |

### 8.2 输出 patch 的约定（重要）

在终端里，把 patch 包在标记中间，网页会自动抽出来：

```
@@PATCH
{
  "ops": [...],
  "gaps": {"certain": [], "uncertain": [], "need_from_you": ["",""]}
}
@@END
```

页面收到后会：解析 → 填进 AI 面板 → 逐条显示「采纳 / 驳回」。
所以你不需要写文件，只要把标记打对。

### 8.3 headless 任务模板

页面「⚡ 让 DSH 出 patch」按钮实际执行的是：

```
dsh headless "读取 .dsh/outbox/context.md；按 bridge/DSH.md 的规则为右图设计节点生成 patch；写入 .dsh/inbox/patch.json；最后打印 @@PATCH <patch 的 JSON> @@END"
```

你可以照这个模板手写更细的任务。

### 8.4 注意

- 命令必须是**单行**（`shell=True` 会把换行当命令分隔符）
- 所有输出走 UTF-8；PYTHONIOENCODING=utf-8 已注入
- 进程不会自动重启；断了点「连接」重来
- 网页只保留最近 60K 字符，早期输出会被截断（避免卡）
