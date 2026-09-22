# workflow-skill-variant-benchmark 开发文档

> 本文件是实现前的完整设计稿。所有设计决策均已与用户确认（见 §1）。
> 实现过程中如需偏离本文档，先回到这里更新，再写代码。

---

## 1. 已确认的设计决策

| 编号 | 议题 | 结论 |
|---|---|---|
| D1 | 1–7 步的交互前端 | **状态机 + 双前端**。Python 状态机是唯一逻辑源；`render_tui.py`（questionary 全屏 TUI，用户在自己终端跑）与 `render_json.py`（JSON 协议，Agent 用 `AskUserQuestion` 转发）是两个可替换的渲染器 |
| D2 | 每组测试的执行者 | **每组一个 subagent**。主 Agent 用 Agent 工具为每个测试组起独立 subagent，独立 context + 独立工作目录 |
| D3 | 步骤 6「可复用产物」语义 | **跳过前缀（冻结）**。指定的步骤 S 及其之前的所有步骤不执行，文件内容直接作为这些步骤的产出注入，执行从 S+1 开始 |
| D4 | 增强项 | **全部不做**（不实现每组重复 N 次、不实现 baseline 对照组、不实现多变量混杂警告、不实现自动对比分析）。但**跑完必须汇报各测试组的结果文件夹位置** |
| D5 | 步骤「做什么」的来源 | `description` **即**给 Agent 的步骤指令，直接拼进 Prompt；同时支持可选字段 `instruction`，存在时优先使用 |
| D6 | 配置文件 schema | 顶层**兼容**数组 与 `{"version":1,"workflows":[...]}` 两种写法；`steps[].optional_skills` **允许空数组**；其余字段不变 |
| D7 | subagent 的产出约定 | 每个 subagent 必须写 **`RESULT.md`（叙述）+ `result.json`（固定字段）** |
| D8 | 输出根目录 | **在确认页由用户填写**，默认值 `wfbm-runs` |

### 勘察结论（影响架构的硬约束）

- **Claude Code 的 Bash/PowerShell 工具中 `stdin/stdout` 都不是 TTY**，且 stdin 立即读到 EOF。
  → Agent 无法直接启动任何全屏交互程序。这是 D1 选"双前端"的根本原因。
- 用户终端可能可以用 `!` 前缀跑 TUI，但**尚未实测**。因此 JSON 协议前端是必需的兜底，不是可选装饰。
- 工具链：uv 0.12.6 / Python 3.13.12 / Windows 11。目标是纯 CLI（含 Linux 服务器终端），不使用任何 Web 技术。

---

## 2. 目录结构

```text
workflow-skill-variant-benchmark/
├── SKILL.md                    # Agent 入口（§9 定义 Agent 的职责边界）
├── TODO.md                     # 本文件
├── pyproject.toml              # uv 项目定义
├── .gitignore
├── template/
│   └── workflow.example.config.json
├── src/wfbm/
│   ├── __init__.py
│   ├── __main__.py             # python -m wfbm
│   ├── cli.py                  # 命令行入口与子命令分发
│   ├── errors.py               # 异常类型 + 错误信息格式化
│   ├── config.py               # 载入 + 校验 workflow 配置
│   ├── model.py                # Page / Question / Option / Session 数据类
│   ├── machine.py              # 状态机：next_page() / apply()
│   ├── prompt.py               # 最终 Prompt 拼装
│   ├── output.py               # session/manifest/SUMMARY 落盘
│   ├── render_json.py          # JSON 协议渲染器（Agent 驱动）
│   └── render_tui.py           # questionary TUI 渲染器（用户驱动）
└── tests/
    ├── test_config.py
    ├── test_machine.py
    └── test_prompt.py
```

`pyproject.toml` 需要补：

```toml
[project]
name = "workflow-skill-variant-benchmark"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["questionary>=2.0"]

[project.scripts]
wfbm = "wfbm.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/wfbm"]
```

**调用方式**（实现时二选一，实测后定稿）：`uv run --directory <skill_dir> wfbm …` 或
`uv run --project <skill_dir> wfbm …`。关键要求是：**进程 cwd 必须保持为用户的项目目录**，
因为 `workflow.config.json`、用户填的输出根目录、数据文件路径都是相对于用户 cwd 解析的。

---

## 3. 配置文件 schema 与校验

### 3.1 接受的格式

顶层两种写法等价：

```jsonc
// 写法 A：数组
[ { "name": "...", "steps": [...] } ]

// 写法 B：带元数据的对象
{ "version": 1, "workflows": [ { "name": "...", "steps": [...] } ] }
```

自动识别：顶层是 list → 写法 A；顶层是 dict 且含 `workflows` → 写法 B；否则报错。

### 3.2 字段

```text
Workflow
  name         : str, 必填, 非空, 同一文件内唯一
  description  : str, 可选（缺省 ""）
  steps        : list[Step], 必填, 非空

Step
  name             : str, 必填, 非空, 同一 workflow 内唯一
  description      : str, 可选（缺省 ""）
  instruction      : str, 可选（缺省 null）
  optional_skills  : list[str], 可选（缺省 []）, 元素非空且互不重复
```

**数组顺序即执行顺序**（例如样例中 `"Merge"` 位于索引 1，它就是第 2 步；`name` 只是标签）。

### 3.3 校验规则

| 规则 | 级别 | 触发条件 |
|---|---|---|
| V1 | 阻断 | 顶层既不是 list 也不是含 `workflows` 的 dict |
| V2 | 阻断 | `name` 缺失 / 非字符串 / 空串 / 同一文件内重名 |
| V3 | 阻断 | `steps` 缺失 / 非 list / 空 |
| V4 | 阻断 | `steps[i].name` 缺失 / 空串 / 同一 workflow 内重名 |
| V5 | 阻断 | `steps[i].optional_skills` 非 list / 含非字符串 / 含空串 / 含重复项 |
| V6 | **警告** | `steps[i].description` 与 `instruction` 同时缺失或同时为空。允许继续，Prompt 中该步骤的指令写为 `（本步骤无指令，按步骤名理解）`。**warning 必须回显给用户**，不能静默吞掉 |
| V7 | 警告 | 未知的顶层键 / 未知的 step 键（向前兼容） |

阻断级错误立即终止；警告级问题收集后随校验结果一并输出，用户可选择继续。

### 3.4 错误信息格式

所有校验失败统一格式，**用户可读、带 JSON 路径**：

```text
workflow.config.json 格式不合法：workflows[0].steps[2].optional_skills 必须是字符串数组（当前为 str）
workflow.config.json 格式不合法：workflows[0].steps 不能为空
workflow.config.json 格式不合法：顶层既不是数组，也不包含 "workflows" 字段
```

多个错误时全部列出，每条一行。SKILL.md 要求 Agent **原样转达**，不得改写、不得自行修复、不得猜测用户意图后继续。

---

## 4. 状态机设计

### 4.1 核心接口

```python
# machine.py
def next_page(sess: Session) -> Page | None      # None 表示流程结束
def apply(sess: Session, answers: dict) -> None  # 校验并写入答案；不合法则抛 ValidationError
```

- 状态机**无副作用**，只操作 `Session`（纯数据）。
- `Session` 在每次 `apply` 之后整体序列化到 `session.json`，因此流程天然可中断续跑。
- 页面顺序、选项枚举、分页、校验、汇总**全部**由状态机决定。渲染器与 Agent 都没有决策权。

### 4.2 数据模型

```python
@dataclass
class Option:
    value: str          # 机器用
    label: str          # 人读
    hint: str = ""      # 灰字补充说明（如"仅 1 个可选 Skill"）

@dataclass
class Question:
    id: str
    kind: Literal["single_select", "multi_select", "text", "confirm", "info"]
    title: str
    help: str = ""
    options: list[Option] = ()
    default: str | None = None
    min_selected: int = 0
    allow_empty: bool = True      # text 用
    required: bool = True

@dataclass
class Page:
    id: str
    title: str
    intro: str = ""        # 顶部固定说明
    body: str = ""         # 只读展示内容（当前列表 / 汇总）
    questions: list[Question] = ()
    stage: str = ""        # 进度标识，如 "3/7"
```

### 4.3 页面清单与转移

```text
[0] workflow          单选一个 workflow
        │
        ▼
[1] variables         多选：哪些步骤作为变量
        │              （选项 = 全部步骤；optional_skills 为空的步骤不可选）
        ▼
[2] invariant.<step>  对每个"不变量步骤"各一页：单选一个 Skill
        │              跳过条件见 §4.4-①
        ▼
[3] groups            列表页，body 显示已添加的测试组
        │              动作：single_select
        │                ["添加测试组", "撤销上一个测试组"（有组时才出现）, "下一步"]
        ├─ 添加 → [3a] group_edit.<step>  对每个变量步骤各一页：单选 Skill
        │            （收集完回到 [3]，追加一个测试组）
        ├─ 撤销 → 移除最后一个测试组，回到 [3]
        └─ 下一步 → 校验 len(groups) >= 2，否则报错"测试组数量需要大于等于 2"
        ▼
[4] test_prompt       text：本次测试的统一提示词
        │
        ▼
[5] attachments       列表页，body 显示已添加的附加信息
        │              动作：single_select ["添加附加信息", "下一步"]（下一步无校验）
        ├─ 添加 → [5a] attach_type   单选类型（目前仅"可复用产物"）
        │         → [5b] attach_groups  多选：适用于哪些测试组
        │         → [5c] attach_step    单选：冻结到哪一步为止
        │         → [5d] attach_path    text：数据文件路径
        │         → [5e] attach_note    text：补充说明（可空）
        │         → 回到 [5]
        └─ 下一步 → [6]
        ▼
[6] confirm           info（完整汇总 body）+ text（输出根目录，默认 wfbm-runs）+ confirm
        │
        ▼
      DONE  → 交由 finalize 生成产物
```

### 4.4 边界规则（必须实现）

① **不变量步骤的 Skill 选择页跳过条件**
   - `optional_skills` 为空 → 该步骤不使用任何 Skill，直接跳过提问，汇总里标"（无可用 Skill）"。
   - 恰好 1 个 → 自动选中，跳过提问，汇总里标"（唯一选项，自动选中）"。
   - ≥2 个 → 生成一页单选。

② **变量步骤的候选范围**
   - `variables` 页列出**全部步骤**，但 `optional_skills` 为空的步骤标记为不可选（`hint="无可用 Skill，不能作为变量"`）。
   - 只有 1 个可选 Skill 的步骤仍可作为变量，但在选项上标注 `hint="仅 1 个可选 Skill，各组取值将相同"`。

③ **测试组校验**
   - 每个测试组必须为**每一个**变量步骤各选中一个 Skill（无缺项、无多余项）。
   - 组数 ≥ 2，否则 `apply` 抛错，错误信息 `测试组数量需要大于等于 2（当前 N 组）`。
   - **撤销**（`action=undo`）：只在已存在测试组时作为选项出现；移除最后一个测试组。
     可以连按直到清空。组 id 用单调计数器 `session.group_seq` 生成，撤销后重新添加
     **不会复用旧 id**（否则附加信息会指错组）；界面上的「组 N」按当前位置重排，保持连续。
     撤销时会顺带清掉引用了被删组的附加信息 —— 正常流程下此刻不可能有附加信息
     （附加信息在第 6 步才收集，而第 4 步之后无法回退），这纯属防御。

④ **附加信息校验**
   - `attach_step` 的选项 = 该 workflow 的全部步骤，label 为
     `冻结到「<step.name>」为止（该步骤及其之前全部不执行，从下一步开始）`。
   - 选中最后一个步骤 → **不阻断**，但在 `confirm` 页显著警告：
     `⚠ 测试组 g 的所有步骤都被冻结，该组将没有实际执行内容`。
   - `attach_groups` 至少选 1 个测试组。
   - `attach_path` 非空。**不做存在性校验**（路径可能由先跑的组产出，此刻尚不存在）。

⑤ **`confirm` 页**
   - `body` 是完整汇总（§4.5）。
   - 校验：输出根目录非空。若该目录下已存在同名时间戳目录 → 自动加后缀，不报错。
   - `confirm` 问题的选项固定为 `["确认并开始", "取消"]`；取消则整个流程退出且不落盘。

### 4.5 汇总页内容（`confirm` 页的 body）

```text
工作流：Example Workflow
说明：An example workflow for demonstration purposes.

变量步骤（3 个）：
  • Step 1
  • Merge
  • Step 2

不变量步骤（3 个）：
  • Single Simulation   →  example-skill-8
  • Decompile Audit     →  example-skill-10（唯一选项，自动选中）
  • System Simulation   →  （无可用 Skill）

测试组（2 组）：
  组 1： Step 1=example-skill-1   Merge=example-skill-4   Step 2=example-skill-6
  组 2： Step 1=example-skill-2   Merge=example-skill-5   Step 2=example-skill-7

测试提示词：
  <原文，过长则截断显示并注明"（完整内容见 session.json）">

附加信息（1 条）：
  1. 可复用产物 → 适用于 [组 2]
     冻结到「Step 1」为止；数据来源：D:\data\prefix.json
     补充说明：使用上一次的解析结果，不要重新解析。

输出根目录：wfbm-runs
```

---

## 5. 双前端

### 5.1 渲染器接口

```python
class Renderer(Protocol):
    def render(self, page: Page) -> dict     # 返回 {question_id: answer}
    def notice(self, text: str) -> None      # 展示只读信息
    def finish(self, summary: str) -> None
```

### 5.2 `render_tui.py`（用户在自己终端跑）

- 基于 `questionary`：`select` / `checkbox` / `text` / `confirm`。
- `page.body` 用 `print` 原样输出，再进入问题。
- 多选用 `checkbox`，支持空格勾选、回车确认。
- 启动时检测 `sys.stdin.isatty()`；不是 TTY → 打印明确指引后退出码 2：
  ```
  wfbm: 当前 stdin 不是终端，无法启动交互界面。
  请在真实终端中运行，或在 Claude Code 里用 `!` 前缀运行：
      ! uv run --directory <skill_dir> wfbm tui
  ```

### 5.3 `render_json.py`（Agent 驱动）

一条命令一次交互，无状态驻留：

```
wfbm next   --session-dir DIR              → 打印一个 JSON "view"
wfbm submit --session-dir DIR --answers J  → 应用答案，返回下一个 view 或 done
```

`view` 的结构：

```jsonc
{
  "kind": "page",
  "page": {
    "id": "variables", "stage": "2/7", "title": "选择变量步骤",
    "intro": "...", "body": "...",
    "questions": [
      {
        "id": "variables",
        "kind": "multi_select",
        "title": "哪些步骤作为本次测试的变量？",
        "help": "...",
        "render": "direct",          // direct | numbered | ask_in_chat
        "options": [ {"value":"Step 1","label":"Step 1","hint":""} ],
        "min_selected": 1
      }
    ]
  },
  "agent_instructions": [
    "逐字转达 intro/body，不要改写、不要总结。",
    "kind=multi_select 且 render=direct → 用 AskUserQuestion，multiSelect=true。",
    "render=numbered → 在正文里打印编号列表，用 AskUserQuestion 让用户填编号。",
    "kind=text 且 render=ask_in_chat → 直接在对话里向用户索取文本，不要用 AskUserQuestion。",
    "收到答案后调用 wfbm submit 回填。校验失败时按返回的错误信息重新提问。"
  ]
}
```

**关键设计：`render` 字段由状态机计算，不由 Agent 判断。**

- `direct`：选项数 ≤ 4 → `AskUserQuestion` 可直接渲染（`Other` 选项由工具自动追加）。
- `numbered`：选项数 > 4 → 在 `body` 里打印编号列表，用一个单选问"请输入编号（可多个，逗号分隔）"。
  状态机接受编号串并归一化为 value 列表后校验。
- `ask_in_chat`：`kind=text` → 纯文本输入无法用 `AskUserQuestion` 表达，改为在对话中索取。
  `confirm` 类同理（`["确认并开始","取消"]` 两个选项，属于 `direct`）。

**Agent 没有任何决策权**：它不知道下一步问什么，只能把 view 转达、把答案回填。所有校验在
`apply()` 里做，Agent 传回非法值会被拒绝并要求重问。这条是 D1 中获得"确定性"的机制保证。

---

## 6. 最终 Prompt 拼装（`prompt.py`）

每个测试组生成一个 Prompt。模板（外层用 4 个反引号，因为模板内部含 `json` 代码块）：

````markdown
# 工作流变体基准测试 · 测试组 {n}

你是本次基准测试的执行者。请**独立完成**下面描述的工作流，不要询问、不要臆测，
所有需要的信息都在本文档中。你的工作目录是：

    {run_dir_abs}

## 一、任务
{用户在 [4] 输入的测试提示词（原文，不加工）}

## 二、工作流
名称：{workflow.name}
说明：{workflow.description}

## 三、执行步骤（严格按顺序）

### 第 1 步 · {step.name}
- **使用 Skill**：`{skill_name}`
- **指令**：{step.instruction or step.description or "（本步骤无指令，按步骤名理解）"}

### 第 2 步 · {step.name}
- **使用 Skill**：（不使用任何 Skill，直接执行）
- **指令**：...

{若该组命中"可复用产物"，被冻结的步骤改为如下块，且标注"不执行"}

### 第 2 步 · {step.name}  ⛔ 本步骤不执行
本步骤属于**冻结前缀**，已被上一阶段产出的数据替代。**不要执行这一步**，
直接采用下列数据作为本步骤（及其之前各步骤）的产出：

- **复用数据来源**：`{path}`
- **补充说明**：{note}

## 四、附加要求
1. 所有产物必须写入 `{run_dir_abs}` 目录内（可建子目录），不要写到目录之外。
2. 在 `{run_dir_abs}/RESULT.md` 写一份叙述性报告，至少包含：
   - 每一步实际做了什么、产出了什么文件
   - 遇到的困难、偏离、以及你是如何处理的
   - 一句话结论：这条流水线跑得怎么样
3. 在 `{run_dir_abs}/result.json` 写一份结构化结果，字段固定为：
   ```json
   {
     "group_id": "group-1",
     "status": "done",                  // done | partial | failed
     "started_at": "ISO8601",
     "finished_at": "ISO8601",
     "skill_assignments": {"Step 1": "example-skill-1"},
     "steps": [{"step":"Step 1","status":"done","outputs":["相对路径"],"notes":""}],
     "artifacts": ["相对路径"],
     "summary": "一句话结论",
     "issues": "遇到的问题，没有则留空"
   }
   ```
4. **只做上述工作**。不要与其他测试组通信，不要读取 `{run_dir_abs}` 之外的产物目录。
````

要点：
- 每步的 Skill 用**反引号 + 明确措辞**（"使用 Skill 工具调用 `xxx`"）表述，统一格式。
- `run_dir_abs` 写**绝对路径**，避免 subagent cwd 不确定导致产物乱飞。
- 冻结前缀的步骤**仍然列出**（保持步骤编号对齐），但明确标 ⛔ 不执行。

---

## 7. 输出产物与落盘

### 7.1 两阶段目录

**暂存区**（流程 1–7 步期间，内部用，固定路径以便续跑、Agent 无需记忆时间戳）：

```
<cwd>/.wfbm/
├── session.json          # 状态机全量状态（每次 submit 后覆写）
└── answers.jsonl         # 追加式审计日志：每次问答一行
```

**最终产出区**（`finalize` 时在用户填的输出根目录下创建）：

```
<output_root>/<YYYYMMDD-HHMMSS>/
├── session.json              # 暂存区 session.json 的副本（可完整复现）
├── config.snapshot.json      # 当时的 workflow.config.json 快照
├── manifest.json             # 运行信息 + 进度标识
├── SUMMARY.md                # report 阶段生成
├── prompts/
│   ├── group-1.md
│   └── group-2.md
└── runs/
    ├── group-1/              # subagent 的工作目录 + 产物 + RESULT.md + result.json
    └── group-2/
```

### 7.2 `manifest.json`

```jsonc
{
  "version": 1,
  "created_at": "ISO8601",
  "skill": "workflow-skill-variant-benchmark",
  "workflow": { "name": "Example Workflow", "description": "..." },
  "config_path": "C:\\...\\workflow.config.json",
  "test_prompt": "...",
  "variables": ["Step 1", "Merge", "Step 2"],
  "invariants": {
    "Single Simulation": { "skill": "example-skill-8", "auto": false },
    "Decompile Audit":   { "skill": "example-skill-10", "auto": true },
    "System Simulation": { "skill": null, "auto": false, "reason": "no_skills" }
  },
  "reuse_rules": [
    { "groups": ["group-2"], "freeze_until": "Step 1",
      "path": "D:\\data\\prefix.json", "note": "..." }
  ],
  "output_root": "wfbm-runs",
  "groups": [
    {
      "id": "group-1", "index": 1,
      "skills": { "Step 1": "example-skill-1", "Merge": "example-skill-4", "Step 2": "example-skill-6" },
      "run_dir": "runs/group-1",
      "prompt": "prompts/group-1.md",
      "status": "pending",          // pending | running | done | failed | skipped
      "started_at": null, "finished_at": null,
      "result_json": null,          // 回填为 runs/group-1/result.json
      "notes": ""
    }
  ]
}
```

`manifest.json` 是**进度标识的唯一真相**。配套命令：

- `wfbm mark --session-dir DIR --group group-1 --status running|done|failed [--notes ...]`
- `wfbm report --session-dir DIR` → 扫描每个 `result.json`，回填 manifest，生成 `SUMMARY.md`，并把
  **各组结果文件夹的绝对路径**打印到 stdout（D4 的硬性要求）。

### 7.3 `SUMMARY.md`

```markdown
# 基准测试结果汇总

工作流：Example Workflow
运行时间：2026-09-22 22:30:00
变量步骤：Step 1, Merge, Step 2

| 组 | Step 1 | Merge | Step 2 | 状态 | 产物数 | 一句话结论 |
|----|--------|-------|--------|------|--------|-----------|
| 1  | example-skill-1 | example-skill-4 | example-skill-6 | done | 7 | ... |
| 2  | example-skill-2 | example-skill-5 | example-skill-7 | done | 5 | ... |

## 结果文件夹

- 组 1：`<abs>/runs/group-1/`  （报告：`<abs>/runs/group-1/RESULT.md`）
- 组 2：`<abs>/runs/group-2/`  （报告：`<abs>/runs/group-2/RESULT.md`）

> 本工具只做控制变量式的**执行与留档**，不做自动评分与对比分析。
> 差异归因请阅读各组的 RESULT.md。
```

---

## 8. CLI 命令清单

| 命令 | 用途 |
|---|---|
| `wfbm check [--config PATH]` | 步骤 0：定位并校验配置，打印 workflow 名称清单。失败时按 §3.4 输出错误并退出码 1 |
| `wfbm init --config PATH [--cwd DIR]` | 初始化暂存区，写入 config 快照与初始 session |
| `wfbm next --session-dir DIR` | 输出下一个 view（JSON） |
| `wfbm submit --session-dir DIR --answers JSON` | 回填答案，返回下一个 view 或 `{"kind":"done"}` |
| `wfbm status --session-dir DIR` | 打印当前进度摘要（人读文本） |
| `wfbm tui --session-dir DIR` | 全屏 TUI（用户终端） |
| `wfbm finalize --session-dir DIR` | 生成最终产出区：manifest、prompts、session 副本 |
| `wfbm mark --session-dir DIR --group ID --status S [--notes N]` | 更新进度标识 |
| `wfbm report --session-dir DIR` | 回填 result.json、生成 SUMMARY.md、打印各组结果目录绝对路径 |

统一约定：所有命令 stdout 只输出结果（JSON 命令输出纯 JSON），stderr 输出人读信息，退出码 0/1/2 分别表示成功/错误/使用者操作问题。

---

## 9. SKILL.md 与 Agent 的职责边界

**Agent 只做 6 件事，且每一步都不得自由发挥：**

1. **步骤 0**：在 cwd 查找 `workflow.config.json`。
   - 存在但不合法 → 原样转达 `wfbm check` 的错误输出，询问用户如何处理，**不自行修复**。
   - 不存在 → 询问用户是否有该文件的路径；用户取消则结束；用户给了路径 → `wfbm check --config <path>`。
2. **驱动流程**：循环 `wfbm next` → 按 `agent_instructions` 转达 → `wfbm submit`。
   - `render=direct` 用 `AskUserQuestion`；`render=numbered` 打印编号列表 + 单选；`kind=text` 直接在对话里索取。
   - 校验失败 → 按错误信息重新提问，**不得**替用户编造答案。
3. **`wfbm finalize`**：生成 manifest、各组 Prompt。
4. **起 subagent**：为每个测试组起一个独立 subagent，prompt = `prompts/group-N.md` 的**全文**。
   起之前 `wfbm mark --status running`，回来后 `wfbm mark --status done|failed`。
5. **`wfbm report`**：生成 SUMMARY.md。
6. **转达**：把 SUMMARY.md 的内容（尤其是**各组结果文件夹绝对路径**）原样呈现给用户。

**Agent 明确禁止：**
- 自行推断或跳过任何页面；自行改写 Prompt 或错误信息；自行修复配置文件；
- 把 1–7 步的判断"简化"成一句自然语言总结；
- 修改 `<run_dir>` 之外的任何文件。

---

## 10. 实施计划

> 状态：**A–E 全部完成**（E3 未做，见下）。测试 **63 项**全绿，CLI 端到端演练跑通。

### 阶段 A：骨架与配置层 — 完成
- [x] A1 补全 `pyproject.toml`（hatchling + `questionary` + console script `wfbm`）
- [x] A2 实测调用方式 → 用 **`uv run --project "$SKILL" wfbm <cmd>`**：已实测可从任意 cwd 调用，
      且**进程 cwd 保持为用户目录**（`--directory` 会切 cwd，不能用）。`src/` 布局 + console script。
- [x] A3 `errors.py` + `config.py`：两种顶层写法兼容、V1–V7 校验、§3.4 错误格式
- [x] A4 `tests/test_config.py`（17 项）
- [x] A5 样例配置**保持原样**（用户决定）。我一度在 `Merge` 上加了 `instruction` 示例，
      但用户随后把它去掉了，并明确表示样例"无所谓，无需在意"。因此
      `template/workflow.example.config.json` 目前**不演示 `instruction`** ——
      该字段仍然完全支持，只是样例里没有。空 `optional_skills` 同理未加样例。

### 阶段 B：状态机 — 完成
- [x] B1 `model.py`：`Option` / `Question` / `Page` / `Group` / `Attachment` / `Session`
- [x] B2 `machine.py`：`next_page` / `apply`，§4.3 全部页面 + §4.4 全部边界规则
- [x] B3 `tests/test_machine.py`（39 项，含测试组撤销的 6 项）
- [x] B4 `render` 计算（`direct` / `numbered` / `ask_in_chat` / `display`）在 `Question.render` 属性里

### 阶段 C：Prompt 与落盘 — 完成
- [x] C1 `prompt.py`：§6 模板，含冻结前缀块与 `result.json` 骨架
- [x] C2 `tests/test_prompt.py`（7 项，含冻结前缀与 finalize/report 集成）- [x] C3 `output.py`：暂存区、`session.json`、`answers.jsonl`、`finalize`、`manifest.json`、`mark`、`report`

### 阶段 D：双前端 — 完成
- [x] D1 `render_json.py` + `cli.py` 的 `next` / `submit` / `status`
- [x] D2 `render_tui.py` + `cli.py` 的 `tui`（含非 TTY 检测与指引）
- [ ] D3 TUI 手工跑通 —— **未验证**。`questionary` 的代码路径没有被真实终端跑过，
      只做了静态检查。JSON 协议前端已端到端跑通。**首次使用 TUI 时请留意**。

### 阶段 E：SKILL.md 与端到端 — 基本完成
- [x] E1 `SKILL.md`（触发条件、职责边界、命令速查、render 对照表、故障处理）
- [x] E2 端到端演练：用样例配置跑了完整的 1–7 步 → finalize → 模拟 subagent 产物 → mark → report，
      产出结构、冻结前缀 Prompt、SUMMARY 表格与路径均正确。演练中发现并修掉 3 个真 bug（见 §11）。
- [ ] E3 用 `kit-builder` 校验 skill 规范性 —— **未做**。跑它意味着本仓库要按 CCKitKit 的
      `cckit.yaml` 规范改造（隔壁 `FuCai_Lib/CCKitKit` 就是那套）。这是个方向性决定，等你拍板。

---

## 11. 未决问题、实现记录与未来扩展

### 11.1 曾拿不准、现已拍板的两处

1. **冻结边界（§4.4-④）**：确认「冻结**到 X 为止**」——步骤 X **及其之前**全部不执行，从 X+1 开始。
2. **空指令步骤（§3.3-V6）**：确认**降为警告**，允许继续；Prompt 中该步骤的指令写为
   `（本步骤无指令，按步骤名理解）`。

### 11.2 端到端演练中发现并修掉的 3 个真 bug

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | 从 stdin 读中文答案时崩溃：`UnicodeEncodeError: 'utf-8' codec can't encode character '\udcaf'` | Windows 上重定向的 stdin 默认按区域编码（cp936）解码，中文变成代理字符，随后写 `answers.jsonl` 时炸 | `_read_stdin_utf8()` 走 `sys.stdin.buffer` 显式按 UTF-8 解码；同时在 `main()` 里把三个标准流统统 reconfigure 成 UTF-8 |
| 2 | 流程走完后再次 `submit` 返回「内部错误：未知的流程相位 'done'」 | `Machine.apply` 没有对 `done` 的兜底 | `apply` 给出明确错误；`cmd_submit` 在终点**幂等**（直接返回 done view，退出码 0） |
| 3 | 第 7 步确认还没提交，`finalize` 照样生成产出区 | `finalize` 只检查了「有测试组」 | 加守卫：必须 `phase == "done"`、未取消、`test_prompt` 非空 |

另外调整了一处顺序：`cmd_submit` 现在**先落盘状态、再写审计日志** —— 日志出问题不该让答案丢失。

### 11.3 本轮实现中按你决定回退/未做的

- **多变量混杂警告已按你的决定移除**。`machine._structural_warnings()` 里现在只保留
  「某组所有步骤都被冻结，跑起来什么都不会发生」这一条**结构性**提示（那不是方法论建议，
  而是防呆）。此前它还会输出「多个变量同时在变，差异无法单独归因」——**那一条已删掉**。
  想恢复的话是一行代码。
- 每组重复 N 次、baseline 对照组、自动对比分析：均未实现。

### 11.4 需要你拍板的新问题（实现过程中冒出来的）—— 已全部有结论

1. **样例配置里步骤顺序与描述自相矛盾** → 你的答复：**无所谓，无需在意**。保持原样，未改。
2. **测试组没有撤销** → 你的答复：**可以**。**已实现**（见 §4.4-③），
   `[3] groups` 页在有测试组时会多出一个「撤销上一个测试组」选项。8 项新测试覆盖。
3. **TUI 前端没有在真实终端里跑过** → 你的答复：**稍后自行验证**。仍未验证。
   验证方法：在 **Claude Code 的对话框**里发这一行（开头的 `!` 是 CC 的 bash 模式，
   会把整行当命令在你自己的终端执行；你的终端有 TTY，我的工具 shell 没有）：

   ```bash
   ! uv run --project "C:/1111school/FuCai_Lib/workflow-skill-variant-benchmark" wfbm tui
   ```

   需要先在放有 `workflow.config.json` 的项目目录里跑过 `wfbm init`。

   **`!` bash 模式的行为（已从本地 changelog `~/.claude/cache/changelog.md` 核实）：**

   | 事项 | 结论 | 来源 |
   |---|---|---|
   | 怎么进入 | 在**空**输入框里以 `!` 开头（粘贴 `!command` 到空输入框同样会进入） | changelog 4045 / 4178 |
   | 命令跑完 | 自动回到普通对话，无需按键 | — |
   | 空输入框下退出 bash 模式 | `Esc` / `Backspace` / `Ctrl+U` 三者等价 | changelog 4612 |
   | 平台是否鼓励这么用 | 是。有一条 "Improved `!` bash mode discoverability — Claude now suggests it when you need to run an interactive command" | changelog 4258 |

   **未能核实**：跑 `!` 命令期间按 `Ctrl+C` 是只杀子进程还是波及 CC 本身
   （官方文档这次取不到，WebFetch 被环境屏蔽）。对实际使用无影响：`wfbm tui` 跑完自己会退，
   中途中断由 `cmd_tui` 里的 `except KeyboardInterrupt` 兜底并保存进度。

   `SKILL.md` 里给用户的措辞已按此写明（用户反馈：他此前不知道输入框能直接跑命令）。
4. **要不要跑 `kit-builder`** → 你的答复：**稍后进行，先不做**。未做。

### 11.5 未来可扩展

- 从 `session.json` 一键重跑（`wfbm replay`）
- 附加信息支持更多类型（模型上已留 `Attachment.type` 字段，目前枚举只有 `reusable_artifact`）
- 多 workflow 混合测试
- 状态机回退（「上一步」）。注意：这跟「撤销测试组」不是一回事 —— 撤销是在同一个页面上
  改变集合，回退是跨页面倒着走。要做回退需要给每个相位实现逆操作。
