---
name: workflow-skill-variant-benchmark
description: 读取 workflow.config.json，用控制变量法在同一条工作流上对比多个 Skill 的表现。把部分步骤设为变量（每个测试组换用不同 Skill）、其余设为不变量（固定 Skill），为每个测试组生成一份独立 Prompt 并派发 subagent 执行，最后汇报各组结果文件夹。
when_to_use: 用户想做 skill 横向评测、A/B 对比、消融实验、控制变量测试；提到 workflow.config.json；或想比较几个 skill 在同一条流水线里跑出来的效果差异。
---

# workflow-skill-variant-benchmark

在同一条工作流上，用**控制变量法**比较不同 Skill 的表现。

- **变量步骤**：每个测试组换用不同的 Skill。
- **不变量步骤**：整个测试固定使用同一个 Skill。
- **可复用产物**：可以让某个测试组直接复用指定步骤及其之前步骤的既有数据，不重新执行，
  这样下游的对比就建立在完全相同的输入上。

## 命令怎么写

**先确认这个 skill 是怎么装的**，两种调用方式选其一：

**A. 作为普通 skill 手动放置**（在 `~/.claude/skills/` 或 `<项目>/.claude/skills/` 下）：

```text
python "<本 SKILL.md 所在目录>/scripts/run.py" <子命令> [参数...]
```

只用标准库，任何 Python ≥3.11 即可（`python` / `python3` 按平台选）。
只有 `tui` 子命令需要额外装 `questionary`。

**B. 通过 cckit 安装**（能跑 `cckit list` 并看到本 skill）：

```text
cckit exec workflow-skill-variant-benchmark scripts/run.py <子命令> [参数...]
```

判断方法：跑一下 `cckit list`，里面有 `workflow-skill-variant-benchmark` 就用 B，否则用 A。
两种方式**行为完全一致** —— 同一份代码、同一个状态机，只是谁来提供解释器不同。

下文提到 `wfbm <子命令>` 时，指的就是上面选定的那种方式的完整命令。

> ⚠️ **给用户的指令里必须把完整命令原样写出来**，不许留任何 `<>` 占位符 ——
> 用户会照抄，看到占位符只会得到一条报错。

## 核心约束：你（Agent）只是搬运工

**1–7 步的全部判断都在 Python 状态机里**——页面顺序、选项枚举、分页、校验、汇总、
Prompt 拼装。你不知道下一步该问什么，是状态机告诉你的；你也不知道答案合不合法，
校验在 `submit` 里做。

因此：

- ✅ 把返回的 `page.intro` / `page.body` **逐字**转达给用户。
- ✅ 严格按每个问题的 `render` 字段决定怎么提问。
- ✅ 把用户输入**原样**回填给 `submit`（解析由 wfbm 负责）。
- ❌ 不要自己推断、总结、改写或跳过任何一页。
- ❌ 不要替用户编造答案；校验失败就按错误信息重新提问。
- ❌ 不要自行修改 `workflow.config.json`、`.wfbm/` 或产出区里的任何文件。

## 子命令速查

| 子命令 | 用途 |
|---|---|
| `check --json` | 校验配置并列出可选 workflow |
| `init --json` | 初始化暂存区 `.wfbm/` |
| `next` | 取当前页面（JSON） |
| `submit --answers-file <路径>` | 回填答案，返回下一页或 `done` |
| `status --json` | 查看进度 |
| `tui` | 全屏交互界面。**CC 里跑不了**（`!` 也没有 TTY，已实测）；只在用户另开真实终端时可用。见下 |
| `finalize` | 生成产出区、manifest 与各组 Prompt |
| `mark --group <id> --status <s>` | 更新某个测试组的进度标识 |
| `report` | 汇总各组结果，生成 `SUMMARY.md` |

---

## 步骤 0 · 准备

1. 在工作目录下查找 `workflow.config.json`。
2. 校验配置：

   ```text
   wfbm check --json
   ```

   - **失败**：把 `message` 字段**原样**转达给用户（形如
     `workflow.config.json 格式不合法：<JSON 路径> <说明>`），然后问用户想怎么处理。
     **绝对不要自己动手改配置文件。**
   - 文件不存在：问用户是否有该文件的路径。
     - 用户取消 → 结束，什么都不做。
     - 用户给了路径 → 加上 `--config "<路径>"` 重跑，同上处理。
   - 有 `warnings`：一并转达给用户（例如某步骤的 description 与 instruction 同时为空）。
3. 校验通过后初始化：

   ```text
   wfbm init --json
   ```

   - 若返回 `error: session_exists`，问用户是**继续上次的进度**（直接跳到步骤 1 的循环）
     还是**重来**（加 `--force`）。

### 交互方式：两条路，二选一

**前提（已实测）：Claude Code 里没有 TTY。** 你的工具 shell 和用户的 `!` bash 模式都没有
—— `tui` 在 CC 内必然报 `当前 stdin/stdout 不是终端` 后退出。所以全屏 TUI 只能在 CC 之外跑。

#### 路线 A：新开一个终端（有箭头键 / 空格多选的界面）

只在用户主动想要图形化界面时才提。

> ⚠️ **发出去之前必须把 `wfbm` 展开成完整命令**（A 或 B 里那条），用户不该看到
> `wfbm`、`$SKILL` 或任何 `<>` 占位符。下面引文里的 `wfbm` 只是给你看的占位。

这样对他说：

> 想要箭头键的完整界面的话，请**新开一个终端窗口**（Windows Terminal / PowerShell / SSH 都行），
> 依次执行：
>
> ```text
> cd "<放着 workflow.config.json 的那个项目目录>"
> <把 wfbm init 展开成完整命令>
> <把 wfbm tui 展开成完整命令>
> ```
>
> 两点注意：
> - `cd` 到的是**你自己的项目目录**（有 `workflow.config.json` 的那个）。cwd 很关键，
>   `.wfbm/` 就建在那儿。
> - `init` 只有**第一次**需要跑；之后直接从 `tui` 开始，进度会续上。
>
> 答完回到这边跟我说一声就行。

进度存在 `.wfbm/session.json`，两个终端共享，所以用户在 TUI 里答完，你这边
`status --json` 就能接着往下走（若已是 `done`，直接跳到步骤 8）。

> TUI 已在真实终端验证可用（2026-09-22，用户实跑 25 页全流程通过）。

#### 路线 B（CC 内，默认）：JSON 协议前端

不切窗口，由你用 `next` / `submit` + `AskUserQuestion` 一问一答地驱动。
没有箭头键，选项超过 4 个时退化成编号列表（见下文 `render=numbered`）。
**用户没有特别要求时，一律走这条。**

---

## 步骤 1–7 · 驱动状态机

循环执行：

```text
wfbm next
  ↓  按 page.agent_instructions 和每个问题的 render 字段向用户提问
answers = 用户的选择（写成 JSON 文件）
  ↓
wfbm submit --answers-file <路径>
  ↓  返回下一页，或 {"kind":"done"}，或 {"kind":"error"}
```

**`render` 字段的含义（必须照做）：**

| render | 怎么做 |
|---|---|
| `direct` | 用 `AskUserQuestion` 提问；`kind=multi_select` 时设 `multiSelect=true` |
| `numbered` | 选项超过 4 个，`AskUserQuestion` 装不下。先在对话里按 `1. label（hint）` 打印编号列表，再让用户填编号。用户答 `1,3` 或 `all` 都行，**原样**提交 |
| `ask_in_chat` | 自由文本（测试提示词、文件路径等）。**直接在对话里向用户索取**，不要用 `AskUserQuestion` |
| `display` | 只展示，不需要输入 |

**提交答案**：把答案写成 JSON 文件再用 `--answers-file`，比 `--answers` 免去引号转义之苦。
键就是问题的 `id`：

```json
{ "workflow": "Example Workflow" }
{ "variables": ["Step 1", "Merge"] }
{ "action": "add" }
{ "action": "undo" }
{ "action": "next" }
{ "skill": "example-skill-1" }
{ "test_prompt": "请对目标样本完成整条流水线的处理……" }
```

测试组列表页的 `action` 有三个可能值：`add`（添加）/ `undo`（撤销最后一个测试组）/
`next`（下一步）。**以返回的 options 为准**——`undo` 只在已有测试组时才出现。
用户说"删掉最后一组""加错了"时就提交 `undo`。

**出错时**：`kind=error` 会带 `message` 和 `issues`，以及**同一个 page**。
把 `message` 原样转达用户，然后按那一页重新提问。不要自己猜一个合法值填进去。

几个必须原样转达的提示：

- 测试组不足 2 个时点下一步 → `测试组数量需要大于等于 2（当前 N 组）`
- 第 5 步要提醒用户：**不要**把「第几步用哪个 Skill」写进测试提示词，那部分由工具自动拼接。

---

## 步骤 8 · 生成测试文件

```text
wfbm finalize
```

返回产出区路径、每组 Prompt 的绝对路径、每组的运行目录。

---

## 步骤 9 · 派发 subagent

**为每一个测试组起一个独立 subagent**（在一条消息里并发起，互不污染）：

- `prompt` = 该组 `prompts/<group-id>.md` 的**全文**，逐字传入，不要摘要、不要改写。
- 起之前：

  ```text
  wfbm mark --group <id> --status running
  ```

- 返回后把 `--status` 改成 `done`（失败则 `failed --notes "<原因>"`）。

不要在你自己（主 Agent）的上下文里执行 Prompt 的内容——那会让后跑的组看到前面组的结果。

---

## 步骤 Last · 汇报

```text
wfbm report
```

把返回的 `summary`（即 `SUMMARY.md` 的内容）**原样**转达给用户，尤其是
「结果文件夹」一节里的**绝对路径**——用户要靠它去翻各组的 `RESULT.md` 和产物。

本工具只做控制变量式的执行与留档，**不做自动评分与对比分析**。不要自行给各组打分或
下"哪个 Skill 更好"的结论；差异归因交给用户读各组的 `RESULT.md`。

---

## 故障处理

| 现象 | 处理 |
|---|---|
| `wfbm: 当前 stdin/stdout 不是终端` | 预期之中——TUI 需要真实 TTY，CC 的 `!` bash 模式和你的工具 shell 都没有。**改用 JSON 协议（`next`/`submit`）**，这是 CC 里的唯一路径。绝不要重试 TUI |
| `找不到流程状态文件` | 还没 `init`，或 `--session-dir` 指错了 |
| `流程还没有 finalize` | `report`/`mark` 之前必须先 `finalize` |
| `流程已经结束，没有待回答的问题` | 1–7 步已走完。直接进入步骤 8 |
| `已存在未完成的流程` | 问用户是继续还是加 `--force` 重来 |
| `cckit: command not found` | 用户还没装 cckit。见 CCKitKit 仓库的安装说明 |

## 目录约定

| 路径 | 内容 |
|---|---|
| `<cwd>/.wfbm/` | 暂存区：`session.json`（状态机全量状态）、`answers.jsonl`（问答审计日志） |
| `<output_root>/<时间戳>/` | 最终产出区：`manifest.json`（进度标识的唯一真相）、`prompts/`、`runs/<group-id>/`、`SUMMARY.md` |

`output_root` 由用户在第 7 步填写，默认 `wfbm-runs`，相对**用户 init 时的 cwd** 解析
（不是 finalize 时的 cwd）。两个目录都已加进 `.gitignore`。
