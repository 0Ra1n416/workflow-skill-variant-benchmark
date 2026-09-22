"""全屏 TUI 渲染器 —— 给用户在自己终端里用的前端。

在 Claude Code 里请用 `!` 前缀运行（Agent 的工具 shell 不是 TTY，跑不了这个）。
两套前端共用同一个状态机，所以走哪条路结果完全一致。
"""

from __future__ import annotations

import sys
from typing import Callable

from .machine import Machine
from .model import Page, Question

_RULE = "─" * 68


def tui_available() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _print_page(page: Page) -> None:
    print()
    print(_RULE)
    stage = f"[{page.stage}] " if page.stage else ""
    print(f"{stage}{page.title}")
    print(_RULE)
    if page.intro:
        print(page.intro)
        print()
    if page.body:
        print(page.body)
        print()


def _ask(question: Question):
    import questionary

    if question.kind == "info":
        return None

    if question.kind == "text":
        message = question.title
        if question.help:
            message = f"{message}\n  ({question.help})"
        answer = questionary.text(message, default=question.default or "").ask()
        if answer is None:
            raise KeyboardInterrupt
        if not answer.strip() and question.default:
            return question.default
        return answer

    choices = [
        questionary.Choice(
            title=f"{o.label}" + (f"  — {o.hint}" if o.hint else ""),
            value=o.value,
            disabled=None if o.enabled else (o.hint or "不可选"),
        )
        for o in question.options
    ]

    if question.kind == "single_select":
        return questionary.select(question.title, choices=choices).ask()

    if question.kind == "multi_select":
        minimum = question.min_selected

        def _validate(selected: list[str]) -> bool | str:
            if len(selected) < minimum:
                return f"至少选择 {minimum} 项"
            return True

        return questionary.checkbox(
            f"{question.title}（空格勾选，回车确认）",
            choices=choices,
            validate=_validate,
        ).ask()

    if question.kind == "confirm":
        return questionary.select(question.title, choices=choices).ask()

    raise RuntimeError(f"未知的问题类型：{question.kind}")


def _ask_page(page: Page) -> dict:
    answers: dict = {}
    _print_page(page)
    for question in page.questions:
        answer = _ask(question)
        if answer is None and question.kind != "info":
            raise KeyboardInterrupt
        answers[question.id] = answer
    return answers


def run_tui(machine: Machine, on_answer: Callable[[Page, dict], None]) -> None:
    """跑完整条流程。on_answer 在每次答案通过校验后被调用（用于落盘）。"""
    if not tui_available():
        print(
            "wfbm: 当前 stdin/stdout 不是终端，无法启动交互界面。\n"
            "  全屏交互需要真实 TTY。注意：Claude Code 里跑不了 —— Agent 的工具 shell\n"
            "  和 `!` bash 模式都没有 TTY（均已实测）。\n"
            "  · 在 Claude Code 里：请改用 JSON 协议前端（wfbm next / wfbm submit）。\n"
            "  · 想用这个界面：另开一个真实终端窗口，cd 到项目目录后直接运行本命令。",
            file=sys.stderr,
        )
        raise SystemExit(2)

    from .errors import AnswerError

    while True:
        page = machine.next_page()
        if page is None:
            break
        while True:
            answers = _ask_page(page)
            try:
                machine.apply(answers)
            except AnswerError as exc:
                print()
                print("✗ " + exc.message)
                print("  请重新填写。")
                print()
                continue
            on_answer(page, answers)
            break

    session = machine.session
    print()
    print(_RULE)
    if session.cancelled:
        print("已取消，未生成任何测试文件。")
    else:
        print("信息收集完成。")
        print(f"  测试组：{len(session.groups)} 个")
        print(f"  输出根目录：{session.output_root}")
        print()
        print("接下来由 Claude 运行 `wfbm finalize` 生成 Prompt 并开始测试。")
    print(_RULE)
