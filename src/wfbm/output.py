"""落盘：暂存区、最终产出区、manifest、SUMMARY。"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import LoadedConfig
from .errors import UsageError
from .machine import Machine
from .model import DEFAULT_OUTPUT_ROOT, Group, Session
from .prompt import build_prompt

SESSION_DIRNAME = ".wfbm"
SESSION_FILENAME = "session.json"
ANSWERS_LOG_FILENAME = "answers.jsonl"
CONFIG_SNAPSHOT_FILENAME = "config.snapshot.json"
MANIFEST_FILENAME = "manifest.json"
SUMMARY_FILENAME = "SUMMARY.md"

GROUP_STATUSES = ("pending", "running", "done", "failed", "skipped")


# ---------------------------------------------------------------------------
# 暂存区
# ---------------------------------------------------------------------------


def session_dir(cwd: Path) -> Path:
    return cwd / SESSION_DIRNAME


def save_session(sdir: Path, session: Session) -> None:
    sdir.mkdir(parents=True, exist_ok=True)
    _write_json(sdir / SESSION_FILENAME, session.to_dict())


def load_session(sdir: Path) -> Session:
    path = sdir / SESSION_FILENAME
    if not path.is_file():
        raise UsageError(f"找不到流程状态文件：{path}（请先运行 wfbm init）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"流程状态文件损坏：{path}（{exc}）") from exc
    return Session.from_dict(data)


def log_answer(sdir: Path, page_id: str, answers: dict[str, Any]) -> None:
    sdir.mkdir(parents=True, exist_ok=True)
    record = {"at": _now_iso(), "page": page_id, "answers": answers}
    with (sdir / ANSWERS_LOG_FILENAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 最终产出区
# ---------------------------------------------------------------------------


def finalize(config: LoadedConfig, session: Session, sdir: Path, cwd: Path) -> tuple[Path, dict]:
    """在用户指定的输出根目录下建好时间戳产出区，返回 (产出区路径, manifest)。"""
    if session.cancelled:
        raise UsageError("流程已被取消，没有可生成的内容")
    if session.phase != "done":
        raise UsageError(f"流程还没有走完（当前：{session.phase}），请先完成第 7 步的确认")
    if not session.groups:
        raise UsageError("还没有任何测试组")
    if not session.test_prompt.strip():
        raise UsageError("测试提示词为空，无法生成 Prompt")

    machine = Machine(config, session)
    # 相对路径一律相对「用户 init 时的工作目录」解析，而不是 finalize 时的 cwd。
    base_cwd = Path(session.cwd) if session.cwd else cwd
    root_raw = session.output_root or DEFAULT_OUTPUT_ROOT
    root = Path(root_raw).expanduser()
    if not root.is_absolute():
        root = base_cwd / root

    out_dir = _unique_dir(root, datetime.now().strftime("%Y%m%d-%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=False)
    (out_dir / "prompts").mkdir()
    (out_dir / "runs").mkdir()

    # 快照：可完整复现
    _write_json(out_dir / SESSION_FILENAME, session.to_dict())
    _write_json(out_dir / CONFIG_SNAPSHOT_FILENAME, config.raw)

    manifest = _build_manifest(config, session, machine, base_cwd, out_dir)

    for group in session.groups:
        run_dir = out_dir / "runs" / group.id
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = out_dir / "prompts" / f"{group.id}.md"
        prompt_path.write_text(build_prompt(machine, group, run_dir), encoding="utf-8")

    _write_json(out_dir / MANIFEST_FILENAME, manifest)

    session.output_dir = str(out_dir.resolve())
    save_session(sdir, session)
    return out_dir, manifest


def _build_manifest(
    config: LoadedConfig,
    session: Session,
    machine: Machine,
    cwd: Path,
    out_dir: Path,
) -> dict:
    wf = machine.wf
    invariants = machine.resolved_invariants()
    return {
        "version": 1,
        "created_at": _now_iso(),
        "skill": "workflow-skill-variant-benchmark",
        "workflow": {"name": wf.name, "description": wf.description},
        "config_path": session.config_path,
        "test_prompt": session.test_prompt,
        "variables": [s.name for s in machine.variable_steps],
        "invariants": invariants,
        "steps": [{"index": s.order, "name": s.name} for s in wf.steps],
        "reuse_rules": [
            {
                "type": a.type,
                "groups": list(a.groups),
                "freeze_until": a.freeze_until,
                "path": a.path,
                "note": a.note,
            }
            for a in session.attachments
        ],
        "output_root": session.output_root,
        "output_dir": str(out_dir.resolve()),
        "cwd": str(cwd.resolve()),
        "groups": [
            {
                "id": g.id,
                "index": g.index,
                "skills": dict(g.skills),
                "frozen_until": machine.frozen_until_index(g.id),
                "run_dir": f"runs/{g.id}",
                "prompt": f"prompts/{g.id}.md",
                "status": "pending",
                "started_at": None,
                "finished_at": None,
                "result_json": None,
                "notes": "",
            }
            for g in session.groups
        ],
    }


# ---------------------------------------------------------------------------
# manifest 读取 / 进度标识
# ---------------------------------------------------------------------------


def manifest_path(sdir: Path) -> Path:
    session = load_session(sdir)
    if not session.output_dir:
        raise UsageError("流程还没有 finalize，找不到产出区（请先运行 wfbm finalize）")
    return Path(session.output_dir) / MANIFEST_FILENAME


def load_manifest(sdir: Path) -> dict:
    path = manifest_path(sdir)
    if not path.is_file():
        raise UsageError(f"找不到 manifest：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"manifest 损坏：{path}（{exc}）") from exc


def save_manifest(sdir: Path, manifest: dict) -> None:
    _write_json(manifest_path(sdir), manifest)


def mark_group(sdir: Path, group_id: str, status: str, notes: str = "") -> dict:
    if status not in GROUP_STATUSES:
        raise UsageError(f"未知的状态 {status!r}（可选：{'、'.join(GROUP_STATUSES)}）")
    manifest = load_manifest(sdir)
    target = _find_group(manifest, group_id)
    target["status"] = status
    if status == "running" and not target.get("started_at"):
        target["started_at"] = _now_iso()
    if status in ("done", "failed", "skipped"):
        target["finished_at"] = _now_iso()
    if notes:
        target["notes"] = notes
    save_manifest(sdir, manifest)
    return target


def _find_group(manifest: dict, group_id: str) -> dict:
    for g in manifest.get("groups", []):
        if g["id"] == group_id:
            return g
    raise UsageError(f"manifest 里没有测试组 {group_id!r}")


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def report(sdir: Path) -> tuple[str, dict]:
    """回填 result.json 的结果，生成 SUMMARY.md，返回 (摘要正文, manifest)。"""
    manifest = load_manifest(sdir)
    out_dir = Path(manifest["output_dir"])

    rows = []
    for g in manifest.get("groups", []):
        run_dir = out_dir / "runs" / g["id"]
        result = _read_result(run_dir)
        if result is not None:
            status = str(result.get("status") or g.get("status") or "done")
            g["status"] = status if status in GROUP_STATUSES else "done"
            g["result_json"] = f"runs/{g['id']}/result.json"
            if not g.get("finished_at"):
                g["finished_at"] = result.get("finished_at") or _now_iso()
        rows.append({"group": g, "run_dir": run_dir, "result": result})

    save_manifest(sdir, manifest)

    lines = _render_summary_lines(manifest, rows, out_dir)
    summary = "\n".join(lines) + "\n"
    (out_dir / SUMMARY_FILENAME).write_text(summary, encoding="utf-8")
    return summary, manifest


def _render_summary_lines(manifest: dict, rows: list[dict], out_dir: Path) -> list[str]:
    variables: list[str] = manifest.get("variables", [])
    lines: list[str] = []
    lines.append("# 基准测试结果汇总")
    lines.append("")
    lines.append(f"工作流：{manifest['workflow']['name']}")
    lines.append(f"运行时间：{manifest.get('created_at', '')}")
    lines.append(f"变量步骤：{', '.join(variables) or '（无）'}")
    lines.append("")
    lines.append("| 组 | " + " | ".join(variables) + " | 状态 | 产物数 | 一句话结论 |")
    lines.append("|" + "---|" * (len(variables) + 4))
    for row in rows:
        g = row["group"]
        result = row["result"] or {}
        cells = [f"组 {g['index']}"]
        for v in variables:
            cells.append(g.get("skills", {}).get(v, ""))
        cells.append(g.get("status", "pending"))
        cells.append(str(len(result.get("artifacts", []))))
        cells.append((result.get("summary") or "").replace("|", "\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## 结果文件夹")
    lines.append("")
    for row in rows:
        g = row["group"]
        run_dir = row["run_dir"]
        mark = "" if run_dir.is_dir() else "  ← 目录不存在"
        lines.append(f"- 组 {g['index']}：`{run_dir}`{mark}")
        result_md = run_dir / "RESULT.md"
        if result_md.is_file():
            lines.append(f"  - 报告：`{result_md}`")
        result_json = run_dir / "result.json"
        if result_json.is_file():
            lines.append(f"  - 结构化结果：`{result_json}`")
    lines.append("")
    lines.append(f"产出区根目录：`{out_dir}`")
    lines.append("")
    lines.append("> 本工具只做控制变量式的执行与留档，不做自动评分与对比分析。差异归因请阅读各组的 RESULT.md。")
    lines.append("")
    return lines


def _read_result(run_dir: Path) -> dict | None:
    path = run_dir / "result.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def group_run_dirs(manifest: dict) -> list[tuple[Group, Path]]:
    out_dir = Path(manifest["output_dir"])
    out: list[tuple[Group, Path]] = []
    for g in manifest.get("groups", []):
        out.append((Group(id=g["id"], index=g["index"], skills=dict(g.get("skills", {}))), out_dir / "runs" / g["id"]))
    return out


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _unique_dir(root: Path, name: str) -> Path:
    candidate = root / name
    n = 2
    while candidate.exists():
        candidate = root / f"{name}-{n}"
        n += 1
    return candidate


def _now_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
