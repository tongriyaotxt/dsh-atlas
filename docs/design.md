# KG-Studio 设计与验证文档（由 state/graph.json 生成）

> 数据源：`state/graph.json`（`updated = 2026-11-01T18:00:10`）
> 左图 `L`（技术图谱）：T07 可微分世界模型、T10 MuJoCo Warp
> 右图 `R`（设计图）：S4 拟合世界模型、S5 在模型内 imagine 32 步
> 本文可直接复用：每一节都可独立摘出，作为设计评审、实验验收或回归门槛。

---

## 1 目标与约束

### 1.1 目标

用**学到的世界模型**替代真模拟器推演，在单卡、数小时预算内完成策略评估与训练迭代：

- 目标产物：1 个策略（在模型内完成评估/改进）
- 数据规模量级：约 12.9 万条动作轨迹（`129,514`）
- 计算预算：单卡（1×GPU），墙钟「几小时」级，而非天级

核心推断链：**要评估动作好坏 → 必须估计未来状态 s′ → 用数据拟合 world model 代替物理步进**。

### 1.2 硬约束

| 编号 | 约束 | 来源 | 对设计的影响 |
|---|---|---|---|
| C1 | 单卡、数小时预算 | L0 目标 | imagine 步数不能无界增长；模型更新频率受限 |
| C2 | 真模拟器（MuJoCo Warp）吞吐被物理步进卡住 | L10 `misc` / README L3 | 模拟器降级为**数据源**，不进入在线内环 |
| C3 | 非平滑接触不可反传 | T10.fails | 世界模型只能做**监督拟合 transitions**，不能端到端穿过模拟器求梯度 |
| C4 | 接触切换处误差大 | T07.fails | 接触密集段需要单独的探针与加权，不能只看全局平均误差 |
| C5 | 多步预测漂移 | T07.fails | 必须有 horizon 显式的误差曲线，而不是单点 one-step loss |
| C6 | 世界模型在 imagine 期间冻结 | S5.note + 边 r1「模型参数：本轮冻结」 | 内环不含梯度回传，允许把 imagine 当作纯前向 |

### 1.3 非目标

- 不追求「可微模拟器」：C3 已否证该路径在本项目中的可行性。
- 不追求长 horizon 精确推演：C5 说明漂移随 horizon 累积，短 horizon + critic/门控是设计选择，不是妥协遗漏。

---

## 2 技术选型与理由

### 2.1 选型表

| 组件 | 选型 | 角色 | 选它的理由 | 明确不做的 |
|---|---|---|---|---|
| 数据生成 | **MuJoCo Warp**（T10） | 离线数据源 | 接触动力学可信、可并行批量产出 transitions；吞吐瓶颈位于物理步进，适合批量离线跑 | 不作为在线可微环境；不把梯度穿过接触求解器 |
| 状态预测 | **可微分世界模型**（T07） | 学 `s′ = f(s, a)` | 参数空间连续、可批量前向、可多步 rollout；误差可度量、可回归 | 不假设它精确复现接触切换 |
| 训练信号 | 监督拟合 transitions | 世界模型训练目标 | 与 C3 一致：只用 (s, a, s′) 三元组，不依赖模拟器反传 | 不做 model-based 的解析梯度（除一阶近似外的路径） |
| 推理内环 | 模型内 imagine 32 步（S5） | 策略评估/改进 | 32 步覆盖必要的信用分配长度；冻结参数避免内环与外环互相污染 | 不在内环更新模型参数 |
| 更新节奏 | 每轮迭代更新一次（S4） | 外环 | 与「本轮冻结」配对：模型更新点唯一、可复现、可回滚 | 不做每 step 在线更新 |

### 2.2 为什么是这套组合（逻辑链）

```
L10 MuJoCo Warp ──feeds(transitions 不反传)──▶ T07 可微分世界模型
                                                     │ refs
                                          S4 拟合世界模型（每轮迭代一次）
                                                     │ feeds（模型参数：本轮冻结）
                                          S5 在模型内 imagine 32 步
```

1. **数据面**：T10 提供高保真 transitions，但接触非平滑 ⇒ 梯度不穿透（边 e1 的语义「transitions 不反传」）。这条边的标注是整套设计的**边界声明**，不是实现细节。
2. **模型面**：T07 用监督损失拟合 `f`，误差以 one-step MSE 度量（实测 `0.007–0.011`）。这个量级说明单步拟合可用，但**不能推出**多步可用 —— 这正是 C5。
3. **设计面**：S4 每轮只更新一次模型，S5 在本轮内冻结使用。冻结换来三件事：内环结果可复现、梯度路径单一、失败可归因（误差来自模型还是来自策略优化）。

---

## 3 每个设计节点的失效条件与探针

> 探针统一约定：固定随机种子集合 `SEEDS = {0,1,2}`；固定评测集 `EVAL_SET`（与训练集按轨迹切分，禁止帧级随机切分）；所有指标记录 `mean ± std`；阈值越界即视为该节点失效。

### 3.1 S4 · 拟合世界模型

**结论式描述**：每轮迭代更新一次，用 `EVAL_SET` 上的 one-step MSE 与 rollout 漂移曲线判断该节点是否仍有效。

**已知失效条件**

| ID | 失效条件 | 现象 |
|---|---|---|
| F4-1 | 一步误差超出 `0.007–0.011` | 拟合质量回退 |
| F4-2 | 多步漂移 | one-step 正常但 rollout 误差随 k 快速发散 |

**探针**

| 探针 | 度量 | 判据（阈值可调，量级已锚定） | 采集方式 |
|---|---|---|---|
| P4-1 一步误差 | `MSE(f(s,a), s′)` | 3 seed 均值落在 `[0.007, 0.011]`；单 seed 上界 `0.013` | `EVAL_SET` 全量前向 |
| P4-2 漂移曲线 | `e(k) = MSE(f^k(s,·), s_k)`，`k ∈ {1,2,4,8,16,32}` | 要求 `e(k)` 的斜率显著优于恒等基线（persistence）：`e_persist(k)/e_model(k) ≥ 1.3` 在 `k=32` 处成立 | 用真轨迹做 teacher forcing → 再切自由 rollout 两段分别报告 |
| P4-3 接触段误差 | 接触窗口（接触标志位触发帧 `±2`）内误差 / 稳态误差 | 比值 `≤ 2.0`；超过则判定 F4-2 的主因是接触切换 | 依赖模拟器导出的接触标志位 |
| P4-4 更新频率审计 | 单个迭代轮内模型参数版本号变化次数 | 必须 `== 1`（不满足即违反 S4.note，属于设计漂移） | 参数 hash 前后对比 |

**最小可跑探针（伪代码，可直接实现）**

```python
def probe_s4(model, eval_batches, seeds=(0, 1, 2), ks=(1, 2, 4, 8, 16, 32)):
    out = {"one_step": [], "drift": {}, "contact_ratio": []}
    for seed in seeds:
        for s, a, s_next, contact in eval_batches:
            out["one_step"].append(((model(s, a) - s_next) ** 2).mean().item())
            for k in ks:                       # 自由 rollout，不回灌真值
                cur, ref = s, s
                for _ in range(k):
                    cur = model(cur, a)
                    ref = s_next               # persistence 基线（占位：按真实长期轨迹替换）
                out["drift"].setdefault(k, []).append(((cur - ref) ** 2).mean().item())
            if contact.any():
                out["contact_ratio"].append(contact_err(model, s, a, s_next, contact) /
                                            steady_err(model, s, a, s_next))
    return summarize(out)   # mean/std，逐项与 F4-1/F4-2/P4-2/P4-3 判据比对
```

### 3.2 S5 · 在模型内 imagine 32 步

**结论式描述**：内环纯前向、模型冻结、horizon 固定为 32；判断节点是否有效看「回报估计是否有偏」。

**已知失效条件**

| ID | 失效条件 | 现象 |
|---|---|---|
| F5-1 | horizon 太短 → 回报有偏 | 估计回报系统性低于/高于真模拟器回报，偏差随 horizon 单调收窄 |

**探针**

| 探针 | 度量 | 判据 | 采集方式 |
|---|---|---|---|
| P5-1 冻结校验 | imagine 前后模型参数 hash | 必须完全相同；任一参数张量变化即失效 | 每次 imagine 调用前后 `hash_params()` |
| P5-2 horizon 偏置 | `bias(h) = E[R̂_h − R_true]`，`h ∈ {8,16,32}` | `h=32` 处 `|bias| ≤ 0.05·std(R_true)`，且 `bias(h)` 随 h 增大而收窄（单调性检查） | 同一批状态分别用模型与真模拟器 rollout |
| P5-3 方差/置信 | bootstrap 置信区间宽度 | `h=32` 的 CI 宽度不超过 `R_true` 标准差的 `0.5×` | 对 `SEEDS` 重采样 |
| P5-4 截断一致性 | 终止条件触发时的截断点 | imagine 不得越过终止帧（门控语义）；越界即失效 | 记录每条 imagine 的终止索引 |

**最小可跑探针（伪代码）**

```python
def probe_s5(model, policy, init_states, truth_returns, hs=(8, 16, 32)):
    h0 = hash_params(model)
    rep = {}
    for h in hs:
        est = [imagine_return(model, policy, s, horizon=h) for s in init_states]  # 纯前向
        rep[h] = bias_ci(est, truth_returns)      # (bias, ci_width)
    assert hash_params(model) == h0, "F5-1 违反：imagine 期间模型被改动"
    return rep    # 比对 |bias(32)| <= 0.05*std(R_true) 且 bias 随 h 收窄
```

### 3.3 上游技术节点的失效条件（作为设计前提，需持续监控）

| 节点 | 失效条件 | 监控探针 | 触发后的动作 |
|---|---|---|---|
| T10 MuJoCo Warp | 非平滑接触不可反传（C3） | 尝试对 `f` 求 `d s′/d a` 时梯度为 `None`/零 → 确认边界仍成立 | 保持「只当数据源」；禁止把模拟器接回内环 |
| T07 可微分世界模型 | 多步预测漂移、接触切换误差大 | 直接复用 P4-2 / P4-3 | 命中则缩短 horizon 或对接触段加权，回到 S5 重新标定 |

---

## 4 跨图映射表

### 4.1 节点映射（L → R）

| L 节点 | R 节点 | 关系 | 说明 | 证据（refs / 边） |
|---|---|---|---|---|
| T07 可微分世界模型 | S4 拟合世界模型 | 技术 → 设计（实例化） | S4 是 T07 的具体落地：每轮迭代拟合一次 | `S4.refs = ["T07"]` |
| T07 可微分世界模型 | S5 imagine 32 步 | 技术 → 使用方 | S5 消费 T07 的前向能力，且此处参数冻结 | `S5.refs = ["T07"]`；边 r1 note「模型参数：本轮冻结」 |
| T10 MuJoCo Warp | S4 拟合世界模型 | 间接（数据供给） | 经 T07 提供 transitions，不直接进 R 图 | 边 e1：`T10 → T07`，`rel = feeds` |
| T10 MuJoCo Warp | S5 imagine 32 步 | 无直接映射 | 内环不接触模拟器（C2/C6 决定） | 无边 —— 缺失即设计意图，不是漏画 |

### 4.2 边映射（L ↔ R）

| L 边 | R 边 | 语义对应 |
|---|---|---|
| `e1: T10 → T07`，`feeds`，note「transitions 不反传」 | `r1: S4 → S5`，`feeds`，note「模型参数：本轮冻结」 | 两条 `feeds` 边都携带**边界声明**：一条声明梯度不穿过模拟器，一条声明梯度不穿过本轮模型。设计上的对称性来自同一个约束源（C3 + C1） |

### 4.3 覆盖度与缺口

| 检查项 | 结果 |
|---|---|
| 每个 R 节点都有 L 依据（refs 非空且存在） | ✅ S4→T07、S5→T07 均成立 |
| 每个 L 节点都被至少一个 R 节点引用 | ✅ T07 被 S4/S5 引用；T10 经 e1 间接进入 |
| 边类型在工具词表内 | ⚠️ `feeds` **不在** `tools/kgctl.py` 的 `VALID_REL = {improves, replaces, combines, requires, fails_when, uses}` 中；直接写回 patch 会被校验拒绝。要么把 `feeds` 加入词表，要么映射为 `requires`（T07 requires T10 的数据） |
| 失效条件覆盖率 | 4/4 节点均写了 `fails`（T07、T10、S4、S5） |
| 悬空引用 | 无 |

---

## 5 未解决风险

| ID | 风险 | 级别 | 触发信号 | 缓解/下一步 |
|---|---|---|---|---|
| R1 | **多步漂移不可消除**：one-step `0.007–0.011` 无法保证 `k=32` 的回报可用 | 高 | P4-2 中 `e(32)` 与 persistence 基线持平或更差 | 缩短 horizon + critic 补偿；接触段样本加权；把漂移曲线作为发布门禁 |
| R2 | **接触切换误差**是漂移主因，且无法通过反传修正（C3） | 高 | P4-3 比值 `> 2.0` | 单独建模接触模式/事件预测；对接触窗口做重采样；接受该段误差并在文档标注 |
| R3 | **回报偏置**被 horizon 掩盖：horizon 短则偏，长则漂移 | 高 | P5-2 单调性不成立，或 `h=32` 的 bias 超阈 | 扫 `h`，取 bias 与方差同时可接受的区间；必要时改为 λ-return / 截断 bootstrap |
| R4 | 图数据不完整：L 图仅 2 个节点（缺 L0–L6 推导链），L2 分叉 / L4 手段 / L6 残余未入图 | 中 | README 的推导树与 `graph.json` 不一致 | 从 README 第三节回填节点并标注 `kind`，否则跨图映射只能覆盖世界模型这一支 |
| R5 | 边词表不一致（`feeds` 非法） | 中 | `kgctl.py validate` 报错 | 统一 `VALID_REL` 与 `index.html` 的连线简写词表 |
| R6 | 冻结语义仅写在边的 note 里，不是强约束 | 中 | 参数 hash 在 imagine 期间变化 | 把 P5-1 作为运行时断言，而不只是文档约定 |
| R7 | 阈值为本文设定的工程默认值，非实测标定 | 中 | 探针首次运行即大面积超阈/全通过 | 用一次 baseline 运行重新标定 `1.3×`、`2.0×`、`0.05·std` 三个常数，并把标定值写回本节 |

---

### 附：文件与命令索引

```bash
python bridge/serve.py                     # 起本地工作台（默认 8777）
python tools/kgctl.py status               # 看是否有待裁决 patch
python tools/kgctl.py context              # 拉设计上下文
python tools/kgctl.py validate p.json      # 只校验不写入（会检查 VALID_REL / refs 悬空）
```

相关文件：`state/graph.json`（本图）· `tools/kgctl.py`（校验器，`VALID_REL`/`KINDS` 定义）· `README.md`（完整推导树与自动检查规则）· `bridge/DSH.md`（patch 生成规则）
