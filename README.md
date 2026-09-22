# workflow-skill-variant-benchmark

一个 Skill：**在同一条工作流上，用控制变量法横向对比多个 Skill 的表现。**

同一条流水线里，某个步骤该用哪个 Skill 往往没有定论。这个 skill 让你把这件事变成一个可执行的实验：

- **变量步骤** —— 每个测试组换用不同的 Skill；
- **不变量步骤** —— 整个测试固定使用同一个 Skill；
- **可复用产物** —— 可以让某个测试组直接复用指定步骤及其之前步骤的既有数据、不重新执行，
  这样下游的对比就建立在**完全相同的输入**上。

跑完之后，每个测试组得到一份独立的 Prompt 和独立的工作目录，你按组去读 `RESULT.md` 就能看出差异。

> 本工具只做控制变量式的**执行与留档**，不做自动评分与对比分析 —— 差异归因交给你自己。

---

## 要求

| 要求项 | 具体要求 |
|---|---|
| Python | **≥ 3.11**（核心功能只用标准库） |
| 额外依赖 | `questionary>=2.0`，**仅**全屏 TUI 需要；不用 TUI 则无需安装 |
| 平台 | Windows / Linux / macOS |

---

## 安装

### 方式一：作为普通 Skill（推荐）

克隆本仓库，然后把 **`workflow-skill-variant-benchmark/` 这个子目录**（不是仓库根）放进
Claude Code 的 skills 目录。

> ⚠️ 注意目录层级：仓库根是 [cckit](https://github.com/0Ra1n416/CCKitKit) 的 **kit 根**，真正的 skill 在它下面同名的子目录里。
> 要让 Claude Code 看到的是 `.../skills/workflow-skill-variant-benchmark/SKILL.md`。

**macOS / Linux**

```bash
git clone https://github.com/0Ra1n416/workflow-skill-variant-benchmark.git ~/wfbm-kit
ln -s ~/wfbm-kit/workflow-skill-variant-benchmark ~/.claude/skills/workflow-skill-variant-benchmark
```

**Windows（PowerShell）**

```powershell
git clone https://github.com/0Ra1n416/workflow-skill-variant-benchmark.git "$HOME\wfbm-kit"
New-Item -ItemType Junction `
  -Path   "$HOME\.claude\skills\workflow-skill-variant-benchmark" `
  -Target "$HOME\wfbm-kit\workflow-skill-variant-benchmark"
```

软链的好处是 `git pull` 之后立刻生效。不想用软链，直接复制那个子目录也可以，只是以后更新要重新复制。

装到 `~/.claude/skills/` 是**个人级**（所有项目可用）；放进某个项目的 `.claude/skills/` 则是**项目级**。

装好后重启 Claude Code，直接说「用 workflow-skill-variant-benchmark 跑一次对比」之类即可，
Claude 会自己接上。

### 方式二：用 cckit 安装

本仓库符合 [CCKit](https://github.com/0Ra1n416/CCKitKit) 规范（根目录有 `cckit.yaml`），
因此也可以交给 cckit 管理 —— 它会自动建好 Python 环境、按需装依赖，并支持 skill 粒度的
开关、干净卸载和 Web 界面。

```bash
uv tool install cckit
cckit add https://github.com/0Ra1n416/workflow-skill-variant-benchmark
cckit list                      # 查看状态
cckit disable workflow-skill-variant-benchmark   # 不卸载，只是关掉
```

安装、开关、卸载、CLI 与 Web 界面的完整用法见 CCKitKit 仓库的说明。

---

## 使用

### 1. 写一份 `workflow.config.json`

放在你的项目目录下（就是运行时的当前工作目录）。最小示例：

```json
[
  {
    "name": "固件分析流程",
    "description": "解析 → 合并 → 反编译审计",
    "steps": [
      {
        "name": "解析",
        "description": "解析目标固件，输出中间表示",
        "optional_skills": ["parser-a", "parser-b", "parser-c"]
      },
      {
        "name": "反编译审计",
        "description": "对中间表示做反编译并审计风险点",
        "optional_skills": ["audit-x", "audit-y"]
      }
    ]
  }
]
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✓ | workflow 名，同一文件内唯一 |
| `description` | | 说明 |
| `steps[].name` | ✓ | 步骤名，同一 workflow 内唯一 |
| `steps[].description` | | **会作为「给 Agent 的步骤指令」拼进最终 Prompt** |
| `steps[].instruction` | | 可选。写了就优先于 `description` |
| `steps[].optional_skills` | | 该步骤可选的 Skill 名列表；可以为空 |

- **数组顺序就是执行顺序**，步骤名只是标签。
- 顶层也可以写成 `{"version": 1, "workflows": [...]}`，两种等价。
- `optional_skills` 里的名字必须是**你机器上真实存在的 skill 名**，否则派出去的 subagent 会找不到。

### 2. 让 Claude 跑起来

在放有 `workflow.config.json` 的项目目录里，对 Claude Code 说：

> 用 workflow-skill-variant-benchmark 跑一次对比

接下来 Claude 会带你走完 7 步信息收集（选工作流 → 选变量步骤 → 给不变量选 Skill →
制定测试组 → 填测试提示词 → 加附加信息 → 确认），然后自动生成 Prompt、为每组派发一个
独立 subagent 执行，最后把各组结果文件夹的路径报给你。

**测试组至少要 2 个** —— 只有一个就没得比。

### 3. 交互方式：默认是 TUI

信息收集（7 步）**默认由你在新开的终端里用 TUI 完成** —— 箭头键选择、空格多选，一屏看完全部选项。
Claude 会先帮你校验并初始化，然后把命令给你：

```bash
cd "<你的项目目录>"                  # 放着 workflow.config.json 的那个
cckit exec workflow-skill-variant-benchmark scripts/run.py init   # 只有第一次需要
cckit exec workflow-skill-variant-benchmark scripts/run.py tui
```

> 装的是「普通 Skill」而不是 cckit 的话，第二三段换成
> `python "<skill 目录>/scripts/run.py" init` / `... tui`。Claude 会给你写好的完整命令。

**为什么必须另开终端**：Claude Code 的 shell（包括 `!` 前缀的 bash 模式）**没有 TTY**，
全屏界面画不出来。这不是本工具的限制，是 CC 的。

进度存在 `.wfbm/session.json`，**两个终端共享** —— 你在 TUI 里答到一半按 `Ctrl+C` 退出，
回到 Claude Code 这边它能接着走；反过来也一样。答完跟 Claude 说一声，它继续后面的步骤。

**回退**：如果你开不了新终端、或 `questionary` 装不上，直接跟 Claude 说
「就在对话里问」，它会改用 `AskUserQuestion` 一页页带你走。代价是没有箭头键，
选项超过 4 个时会退化成「打印编号 → 你回 `1,3`」，而且要点击十几次。

---

## 产出

所有结果落在 `<输出根目录>/<时间戳>/`（默认 `wfbm-runs/`，第 7 步可以改）：

```text
wfbm-runs/20260923-101530/
├── manifest.json          # 运行信息 + 进度标识（唯一真相）
├── session.json           # 你的全部选择，可完整复现
├── config.snapshot.json   # 当时的配置快照
├── SUMMARY.md             # 各组结果汇总表 + 结果文件夹路径
├── prompts/
│   ├── group-1.md         # 每个测试组的最终 Prompt（就是发给 subagent 的全文）
│   └── group-2.md
└── runs/
    ├── group-1/           # 该组 subagent 的工作目录：产物 + RESULT.md + result.json
    └── group-2/
```

中间状态存在项目目录的 `.wfbm/` 下（`session.json` + `answers.jsonl` 问答审计日志）。
两个目录都建议加进 `.gitignore`。

---

## 平台与已知限制

- **Windows / Linux / macOS 均可**，无系统级二进制依赖。
- **TUI 不能在 Claude Code 内启动**（没有 TTY），只能用 JSON 协议或另开终端。见上。
- **没有跨页「上一步」回退**。测试组列表页可以撤销最后一个测试组，但已经走过的步骤不能倒回去改。
- **不做自动评分**。跑完只给你各组的结果和 Prompt，孰优孰劣由你判断。

---

## License

<!-- 发布前请补上，cckit 会把这一项展示在安装计划里 -->
MIT
