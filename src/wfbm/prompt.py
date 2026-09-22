"""最终 Prompt 的拼装。

一个测试组一个 Prompt。模板见 TODO.md §6。
"""

from __future__ import annotations

from pathlib import Path

from .machine import Machine
from .model import Group


def _skill_line(skill: str | None) -> str:
    if skill:
        return f"- **使用 Skill**：请用 Skill 工具调用 `{skill}`"
    return "- **使用 Skill**：（不使用任何 Skill，直接执行）"


def build_prompt(machine: Machine, group: Group, run_dir: Path) -> str:
    """为单个测试组生成最终 Prompt。run_dir 会被写成绝对路径。"""
    wf = machine.wf
    run_dir_abs = str(run_dir.resolve())
    # 用 Path 拼接再转字符串，避免 Windows 上出现 `...\runs\group-1/RESULT.md` 这种混用分隔符
    result_md_abs = str(run_dir.resolve() / "RESULT.md")
    result_json_abs = str(run_dir.resolve() / "result.json")
    frozen_until = machine.frozen_until_index(group.id)
    applicable = machine.attachments_for(group.id)
    invariants = machine.resolved_invariants()

    out: list[str] = []
    out.append(f"# 工作流变体基准测试 · 测试组 {group.index}")
    out.append("")
    out.append("你是本次基准测试的执行者。请**独立完成**下面描述的工作流：不要询问、不要臆测，")
    out.append("所有需要的信息都在本文档中。你的工作目录是：")
    out.append("")
    out.append(f"    {run_dir_abs}")
    out.append("")
    out.append("## 一、任务")
    out.append("")
    out.append(machine.session.test_prompt)
    out.append("")
    out.append("## 二、工作流")
    out.append("")
    out.append(f"名称：{wf.name}")
    if wf.description:
        out.append(f"说明：{wf.description}")
    out.append("")

    # 本组配置表 —— 仅用于留档与自检，执行以第三节为准
    out.append("## 三、本组配置（留档用，执行以第四节为准）")
    out.append("")
    out.append("| 步骤 | 本组使用的 Skill | 是否执行 |")
    out.append("|---|---|---|")
    for step in wf.steps:
        if step.index <= frozen_until:
            out.append(f"| {step.order}. {step.name} | — | 否（冻结） |")
        elif step.name in group.skills:
            out.append(f"| {step.order}. {step.name} | `{group.skills[step.name]}` | 是 |")
        else:
            skill = invariants.get(step.name, {}).get("skill")
            out.append(f"| {step.order}. {step.name} | {f'`{skill}`' if skill else '（无）'} | 是 |")
    out.append("")

    # 执行步骤
    out.append("## 四、执行步骤（严格按顺序）")
    out.append("")
    for step in wf.steps:
        if step.index <= frozen_until:
            out.append(f"### 第 {step.order} 步 · {step.name}  ⛔ 本步骤不执行")
            out.append("")
            out.append("本步骤属于**冻结前缀**，其产出已经由上一阶段生成，**不要重新执行这一步**。")
            out.append("直接采用下列数据作为本步骤（以及它之前各步骤）的产出：")
            out.append("")
            for i, att in enumerate(applicable, 1):
                prefix = f"{i}. " if len(applicable) > 1 else ""
                out.append(f"{prefix}- **复用数据来源**：`{att.path}`")
                if att.note:
                    out.append(f"  - **补充说明**：{att.note}")
            out.append("")
            continue

        out.append(f"### 第 {step.order} 步 · {step.name}")
        out.append("")
        if step.name in group.skills:
            out.append(_skill_line(group.skills[step.name]))
        else:
            out.append(_skill_line(invariants.get(step.name, {}).get("skill")))
        out.append(f"- **指令**：{step.prompt_text}")
        out.append("")

    # 产出要求
    out.append("## 五、附加要求")
    out.append("")
    out.append(f"1. 所有产物必须写入 `{run_dir_abs}` 目录内（可以在里面建子目录），不要写到该目录之外。")
    out.append(f"2. 在 `{result_md_abs}` 写一份叙述性报告，至少包含：")
    out.append("   - 每一步实际做了什么、产出了哪些文件")
    out.append("   - 遇到的困难、偏离，以及你是如何处理的")
    out.append("   - 一句话结论：这条流水线跑得怎么样")
    out.append(f"3. 在 `{result_json_abs}` 写一份结构化结果，字段固定如下：")
    out.append("")
    out.append("   ```json")
    out.append("   {")
    out.append(f'     "group_id": "{group.id}",')
    out.append('     "status": "done",')
    out.append('     "started_at": "<ISO8601>",')
    out.append('     "finished_at": "<ISO8601>",')
    out.append('     "skill_assignments": {')
    rows = _assignment_rows(machine, group)
    for i, (name, skill) in enumerate(rows):
        comma = "," if i < len(rows) - 1 else ""
        out.append(f'       "{name}": "{skill}"{comma}')
    out.append("     },")
    out.append('     "steps": [')
    out.append('       {"step": "<步骤名>", "status": "done", "outputs": ["<相对路径>"], "notes": ""}')
    out.append("     ],")
    out.append('     "artifacts": ["<相对路径>"],')
    out.append('     "summary": "<一句话结论>",')
    out.append('     "issues": "<遇到的问题，没有则留空>"')
    out.append("   }")
    out.append("   ```")
    out.append("")
    out.append("   `status` 取值：`done` | `partial` | `failed`。上面的字段名必须完全一致。")
    if applicable:
        # 冻结前缀让 subagent 去读 run_dir 之外的数据；不写清楚的话，
        # 下面第 4 条「不要读取本目录之外」会和它直接冲突，较真的执行者会拒绝读。
        out.append(
            "4. **只做上述工作。** 不要与其他测试组通信。除第四节中明确列出的"
            "「复用数据来源」（那是上游步骤的既有产出，读取它是本组的预期行为）之外，"
            "不要读取本目录之外的任何产物目录。"
        )
    else:
        out.append("4. **只做上述工作。** 不要与其他测试组通信，不要读取本目录之外的产物目录。")
    out.append("")
    return "\n".join(out)


def _assignment_rows(machine: Machine, group: Group) -> list[tuple[str, str]]:
    """result.json 里 skill_assignments 的初始骨架：只列真正会执行的步骤。"""
    frozen_until = machine.frozen_until_index(group.id)
    invariants = machine.resolved_invariants()
    rows: list[tuple[str, str]] = []
    for step in machine.wf.steps:
        if step.index <= frozen_until:
            continue
        skill = group.skills.get(step.name) or invariants.get(step.name, {}).get("skill") or ""
        rows.append((step.name, skill))
    return rows
