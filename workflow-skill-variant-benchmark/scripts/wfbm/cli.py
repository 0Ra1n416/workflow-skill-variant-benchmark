"""wfbm 命令行入口。

约定：
  * stdout 只输出结果（机器可读命令输出纯 JSON）
  * stderr 输出人读信息
  * 退出码 0=成功 / 1=错误 / 2=使用方式问题
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import output as out
from .config import CONFIG_FILENAME, LoadedConfig, load_config, locate_config
from .errors import AnswerError, ConfigError, UsageError, WfbmError
from .machine import Machine
from .model import STAGE_LABELS, Session
from .render_json import done_view, error_view, page_view

PROG = "wfbm"


# ---------------------------------------------------------------------------
# 共用工具
# ---------------------------------------------------------------------------


def _dump(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _fail(exc: WfbmError, *, as_json: bool) -> int:
    if as_json:
        payload = exc.to_dict() if hasattr(exc, "to_dict") else {"kind": "error", "message": exc.message}
        _dump(payload)
    else:
        print(exc.message, file=sys.stderr)
    return exc.exit_code


def _resolve_config(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    found = locate_config(Path.cwd())
    if found is None:
        raise UsageError(
            f"当前目录下没有找到 {CONFIG_FILENAME}。请把配置文件放到工作目录，"
            f"或用 --config <路径> 指定。"
        )
    return found


def _session_dir(args) -> Path:
    if getattr(args, "session_dir", None):
        return Path(args.session_dir).expanduser().resolve()
    return out.session_dir(Path.cwd())


def _load_context(sdir: Path) -> tuple[LoadedConfig, Session, Machine]:
    session = out.load_session(sdir)
    cfg = load_config(session.config_path)
    return cfg, session, Machine(cfg, session)


def _emit_json_view(sdir: Path, machine: Machine, cfg: LoadedConfig) -> int:
    page = machine.next_page()
    if page is None:
        _dump(done_view(machine))
    else:
        _dump(page_view(page, session_phase=machine.session.phase))
    # 顺手把答案日志的游标位置刷新一遍
    out.save_session(sdir, machine.session)
    return 0


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_check(args) -> int:
    try:
        path = _resolve_config(args.config)
        cfg = load_config(path)
    except WfbmError as exc:
        return _fail(exc, as_json=args.json)

    payload = {
        "ok": True,
        "config_path": str(path.resolve()),
        "workflows": [
            {
                "name": w.name,
                "description": w.description,
                "steps": [
                    {
                        "index": s.order,
                        "name": s.name,
                        "optional_skills": list(s.optional_skills),
                    }
                    for s in w.steps
                ],
            }
            for w in cfg.workflows
        ],
        "warnings": [i.to_dict() for i in cfg.warnings],
    }

    if args.json:
        _dump(payload)
    else:
        print(f"✓ {path} 校验通过（{len(cfg.workflows)} 个 workflow）")
        for w in cfg.workflows:
            print(f"  · {w.name}（{len(w.steps)} 步）")
            for s in w.steps:
                skills = "、".join(s.optional_skills) or "（无）"
                print(f"      {s.order}. {s.name}  →  {skills}")
        for i in cfg.warnings:
            print(f"  ⚠ {i.render()}", file=sys.stderr)
    return 0


def cmd_init(args) -> int:
    cwd = Path.cwd()
    try:
        path = _resolve_config(args.config)
        cfg = load_config(path)
    except WfbmError as exc:
        return _fail(exc, as_json=args.json)

    sdir = _session_dir(args)
    existing = sdir / out.SESSION_FILENAME
    if existing.is_file() and not args.force:
        raise_msg = (
            f"已存在未完成的流程：{existing}\n"
            f"  要继续：直接运行 wfbm next\n"
            f"  要重来：wfbm init --force（会丢弃已有进度）"
        )
        if args.json:
            _dump({"kind": "error", "error": "session_exists", "message": raise_msg, "session_dir": str(sdir)})
            return 1
        print(raise_msg, file=sys.stderr)
        return 1

    session = Session(config_path=str(path.resolve()), cwd=str(cwd.resolve()))
    sdir.mkdir(parents=True, exist_ok=True)
    out.save_session(sdir, session)
    out._write_json(sdir / out.CONFIG_SNAPSHOT_FILENAME, cfg.raw)

    payload = {
        "ok": True,
        "session_dir": str(sdir),
        "config_path": str(path.resolve()),
        "phase": session.phase,
        "workflows": [{"name": w.name, "description": w.description, "steps": len(w.steps)} for w in cfg.workflows],
        "warnings": [i.to_dict() for i in cfg.warnings],
    }
    if args.json:
        _dump(payload)
    else:
        print(f"✓ 已初始化：{sdir}")
        print(f"  配置：{path}")
        print(f"  可选 workflow：{'、'.join(w.name for w in cfg.workflows)}")
        for i in cfg.warnings:
            print(f"  ⚠ {i.render()}", file=sys.stderr)
    return 0


def cmd_next(args) -> int:
    sdir = _session_dir(args)
    try:
        cfg, _session, machine = _load_context(sdir)
        return _emit_json_view(sdir, machine, cfg)
    except WfbmError as exc:
        return _fail(exc, as_json=True)


def cmd_submit(args) -> int:
    sdir = _session_dir(args)
    try:
        answers = _read_answers(args)
        cfg, session, machine = _load_context(sdir)
    except WfbmError as exc:
        return _fail(exc, as_json=True)

    page = machine.next_page()
    if page is None:
        # 流程已经结束。让 submit 在终点幂等，而不是报「未知相位」。
        _dump(done_view(machine))
        return 0

    try:
        machine.apply(answers)
    except AnswerError as exc:
        _dump(error_view(exc.to_dict(), page))
        return 1

    # 先落盘状态，再写审计日志：日志出问题不该让答案丢失。
    out.save_session(sdir, session)
    out.log_answer(sdir, page.id, answers)
    return _emit_json_view(sdir, machine, cfg)


def _read_answers(args) -> dict:
    if args.answers_file:
        if args.answers_file == "-":
            raw = _read_stdin_utf8()
        else:
            p = Path(args.answers_file).expanduser()
            if not p.is_file():
                raise UsageError(f"答案文件不存在：{p}")
            raw = p.read_text(encoding="utf-8")
    elif args.answers is not None:
        raw = args.answers
    else:
        raise UsageError("需要 --answers <JSON> 或 --answers-file <路径|->")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"答案不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}") from exc
    if not isinstance(data, dict):
        raise UsageError("答案必须是一个 JSON 对象（question_id -> 答案）")
    return data


def _read_stdin_utf8() -> str:
    """从 stdin 按 UTF-8 读取。

    不能直接 sys.stdin.read()：Windows 上被重定向时它会用区域编码（可能是 cp936）
    解码，中文会变成代理字符，后续写文件时报 UnicodeEncodeError。
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return buffer.read().decode("utf-8")
    return sys.stdin.read()


def cmd_status(args) -> int:
    sdir = _session_dir(args)
    try:
        cfg, session, machine = _load_context(sdir)
    except WfbmError as exc:
        return _fail(exc, as_json=args.json)

    payload = {
        "session_dir": str(sdir),
        "config_path": session.config_path,
        "phase": session.phase,
        "stage": STAGE_LABELS.get(session.phase, session.phase),
        "workflow": session.workflow,
        "variables": session.variables,
        "invariants": machine.resolved_invariants() if session.workflow else {},
        "groups": [g.to_dict() for g in session.groups],
        "attachments": [a.to_dict() for a in session.attachments],
        "output_root": session.output_root,
        "output_dir": session.output_dir,
        "cancelled": session.cancelled,
    }
    if args.json:
        _dump(payload)
    else:
        print(f"暂存区：{sdir}")
        print(f"配置：{session.config_path}")
        print(f"进度：{payload['stage']}")
        print(f"工作流：{session.workflow or '（未选）'}")
        print(f"变量步骤：{'、'.join(session.variables) or '（未选）'}")
        print(f"测试组：{len(session.groups)} 个")
        print(f"附加信息：{len(session.attachments)} 条")
        if session.output_dir:
            print(f"产出区：{session.output_dir}")
    return 0


def cmd_tui(args) -> int:
    from .render_tui import run_tui

    sdir = _session_dir(args)
    try:
        cfg, session, machine = _load_context(sdir)
    except WfbmError as exc:
        return _fail(exc, as_json=False)

    def _on_answer(page, answers):
        out.log_answer(sdir, page.id, answers)
        out.save_session(sdir, machine.session)

    try:
        run_tui(machine, _on_answer)
    except KeyboardInterrupt:
        out.save_session(sdir, machine.session)
        print("\n已中断。进度已保存，可以重新运行 wfbm tui 继续。", file=sys.stderr)
        return 130
    return 0


def cmd_finalize(args) -> int:
    sdir = _session_dir(args)
    try:
        cfg, session, machine = _load_context(sdir)
        out_dir, manifest = out.finalize(cfg, session, sdir, Path.cwd())
    except WfbmError as exc:
        return _fail(exc, as_json=True)

    payload = {
        "ok": True,
        "output_dir": str(out_dir),
        "manifest": str(out_dir / out.MANIFEST_FILENAME),
        "groups": [
            {
                "id": g["id"],
                "index": g["index"],
                "skills": g["skills"],
                "prompt": str(out_dir / g["prompt"]),
                "run_dir": str(out_dir / g["run_dir"]),
            }
            for g in manifest["groups"]
        ],
        "next": "为每个测试组起一个独立 subagent，prompt 用上面的 prompt 文件全文；"
        "起之前 wfbm mark --group <id> --status running，完成后 --status done|failed",
    }
    _dump(payload)
    return 0


def cmd_mark(args) -> int:
    sdir = _session_dir(args)
    try:
        group = out.mark_group(sdir, args.group, args.status, args.notes or "")
    except WfbmError as exc:
        return _fail(exc, as_json=True)
    _dump({"ok": True, "group": group})
    return 0


def cmd_report(args) -> int:
    sdir = _session_dir(args)
    try:
        summary, manifest = out.report(sdir)
    except WfbmError as exc:
        return _fail(exc, as_json=True)

    out_dir = Path(manifest["output_dir"])
    payload = {
        "ok": True,
        "output_dir": str(out_dir),
        "summary_path": str(out_dir / out.SUMMARY_FILENAME),
        "groups": [
            {
                "id": g["id"],
                "index": g["index"],
                "status": g.get("status", "pending"),
                "run_dir": str(out_dir / g["run_dir"]),
                "result_json": g.get("result_json"),
            }
            for g in manifest["groups"]
        ],
        "summary": summary,
        "agent_instructions": [
            "把 summary 原文转达给用户，尤其是「结果文件夹」一节里的绝对路径。",
            "不要自行改写、评分或做对比分析 —— 本工具不做这件事。",
        ],
    }
    _dump(payload)
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="工作流变体基准测试工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="校验 workflow.config.json")
    p_check.add_argument("--config", help=f"配置文件路径（默认 ./{CONFIG_FILENAME}）")
    p_check.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_check.set_defaults(func=cmd_check)

    p_init = sub.add_parser("init", help="初始化流程暂存区")
    p_init.add_argument("--config", help=f"配置文件路径（默认 ./{CONFIG_FILENAME}）")
    p_init.add_argument("--force", action="store_true", help="丢弃已有进度重新开始")
    p_init.add_argument("--session-dir", help="暂存区路径（默认 ./.wfbm）")
    p_init.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_init.set_defaults(func=cmd_init)

    p_next = sub.add_parser("next", help="取下一个页面（JSON）")
    p_next.add_argument("--session-dir", help="暂存区路径（默认 ./.wfbm）")
    p_next.set_defaults(func=cmd_next)

    p_submit = sub.add_parser("submit", help="回填答案并取下一页（JSON）")
    p_submit.add_argument("--answers", help="答案 JSON 字符串")
    p_submit.add_argument("--answers-file", help="答案 JSON 文件路径，或 - 表示 stdin")
    p_submit.add_argument("--session-dir", help="暂存区路径（默认 ./.wfbm）")
    p_submit.set_defaults(func=cmd_submit)

    p_status = sub.add_parser("status", help="查看当前进度")
    p_status.add_argument("--session-dir", help="暂存区路径（默认 ./.wfbm）")
    p_status.add_argument("--json", action="store_true", help="以 JSON 输出")
    p_status.set_defaults(func=cmd_status)

    p_tui = sub.add_parser("tui", help="在真实终端里跑全屏交互界面")
    p_tui.add_argument("--session-dir", help="暂存区路径（默认 ./.wfbm）")
    p_tui.set_defaults(func=cmd_tui)

    p_fin = sub.add_parser("finalize", help="生成产出区、manifest 与各组 Prompt")
    p_fin.add_argument("--session-dir", help="暂存区路径（默认 ./.wfbm）")
    p_fin.set_defaults(func=cmd_finalize)

    p_mark = sub.add_parser("mark", help="更新某个测试组的进度标识")
    p_mark.add_argument("--group", required=True, help="测试组 id，如 group-1")
    p_mark.add_argument("--status", required=True, choices=out.GROUP_STATUSES)
    p_mark.add_argument("--notes", help="补充说明")
    p_mark.add_argument("--session-dir", help="暂存区路径（默认 ./.wfbm）")
    p_mark.set_defaults(func=cmd_mark)

    p_rep = sub.add_parser("report", help="汇总各组结果，生成 SUMMARY.md")
    p_rep.add_argument("--session-dir", help="暂存区路径（默认 ./.wfbm）")
    p_rep.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 上标准流被重定向时默认走区域编码（可能是 cp936），
    # 中文和 emoji 会乱码甚至抛 UnicodeEncodeError。统一强制 UTF-8。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except WfbmError as exc:
        return _fail(exc, as_json=getattr(args, "json", False))
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
