"""JSON 协议渲染器 —— 给 Agent 用的前端。

设计要点：Agent 是**纯搬运工**。它不知道下一步问什么（页面是状态机给的），
也不知道答案合不合法（校验在 apply() 里做）。每个问题都带一个由状态机算出的
`render` 字段，明确告诉 Agent 该怎么呈现，Agent 不得自行判断。

对应用户约定的「确定性」要求：1–7 步的判断全部在 Python 里，Agent 只做转达。
"""

from __future__ import annotations

from typing import Any

from .machine import Machine
from .model import Page, Question

BASE_INSTRUCTIONS = [
    "把 page.intro 和 page.body 逐字转达给用户，不要改写、不要总结、不要省略。",
    "然后按每个问题的 render 字段取值分别处理（见下），不要自行改变提问顺序或选项。",
    "收集到答案后，用 `wfbm submit` 回填。若返回 kind=error，按 issues 里的说明重新向用户提问。",
    "不要替用户编造答案，不要跳过任何一页，不要自行修改配置或状态文件。",
]

RENDER_INSTRUCTIONS = {
    "direct": "render=direct：用 AskUserQuestion 提问；multi_select 用 multiSelect=true。",
    "numbered": (
        "render=numbered：选项超过 4 个，AskUserQuestion 装不下。"
        "请先在对话里按 `1. label（hint）` 打印编号列表，再用 AskUserQuestion 让用户填编号"
        "（把用户输入原样放进答案里即可，如 \"1,3\" 或 \"all\"，解析由 wfbm 负责）。"
    ),
    "ask_in_chat": "render=ask_in_chat：这是自由文本。直接在对话里向用户索取，不要用 AskUserQuestion。",
    "display": "render=display：只展示，不需要用户输入。",
}

KIND_HINTS = {
    "confirm": '这是确认项，答案必须是选项之一（如 "确认并开始" / "取消"）。',
    "text": "文本题：把用户的原话原样提交（不要加引号、不要改写）。",
}


def _question_instructions(questions: tuple[Question, ...]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for q in questions:
        if q.render not in seen:
            seen.add(q.render)
            out.append(RENDER_INSTRUCTIONS[q.render])
        if q.kind in KIND_HINTS:
            out.append(f"[{q.id}] {KIND_HINTS[q.kind]}")
        if q.kind == "multi_select" and q.min_selected:
            out.append(f"[{q.id}] 至少选择 {q.min_selected} 项。")
    return out


def page_view(page: Page, *, session_phase: str) -> dict:
    return {
        "kind": "page",
        "phase": session_phase,
        "page": page.to_dict(),
        "agent_instructions": BASE_INSTRUCTIONS + _question_instructions(page.questions),
        "submit_with": "wfbm submit --answers-file <path>",
    }


def done_view(machine: Machine) -> dict:
    session = machine.session
    if session.cancelled:
        return {
            "kind": "done",
            "cancelled": True,
            "message": "用户选择了取消，流程已终止。没有生成任何测试文件。",
            "agent_instructions": ["告知用户流程已取消，不要继续后续步骤。"],
        }

    reuse = [
        {
            "groups": a.groups,
            "freeze_until": a.freeze_until,
            "path": a.path,
            "note": a.note,
        }
        for a in session.attachments
    ]
    return {
        "kind": "done",
        "cancelled": False,
        "message": "1–7 步的信息收集已完成，可以 finalize 并开始测试。",
        "workflow": session.workflow,
        "variables": [s.name for s in machine.variable_steps],
        "groups": [g.to_dict() for g in session.groups],
        "reuse_rules": reuse,
        "output_root": session.output_root,
        "next_command": "wfbm finalize",
        "agent_instructions": [
            "运行 `wfbm finalize` 生成产出区、manifest 与各组 Prompt。",
            "然后为每一个测试组起一个独立 subagent：prompt 就是该组 prompts/<group-id>.md 的全文。",
            "起之前先 `wfbm mark --group <id> --status running`，回来后 `--status done` 或 `failed`。",
            "最后运行 `wfbm report`，把 SUMMARY.md 的内容（尤其是各组结果文件夹路径）转达给用户。",
        ],
    }


def error_view(payload: dict[str, Any], page: Page | None) -> dict:
    view: dict[str, Any] = dict(payload)
    if page is not None:
        view["page"] = page.to_dict()
    view["agent_instructions"] = [
        "把 message 原样转达给用户，不要自己解释或修复。",
        "如果带了 page，按该页的 render 说明重新向用户提问。",
    ]
    return view
