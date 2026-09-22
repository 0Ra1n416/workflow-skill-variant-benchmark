"""最终 Prompt 拼装、finalize 与 report 的测试。"""

from __future__ import annotations

import json
from pathlib import Path

from wfbm import output as out
from wfbm.config import parse_config
from wfbm.machine import Machine
from wfbm.model import Session

RAW = [
    {
        "name": "W",
        "description": "示例工作流",
        "steps": [
            {"name": "Step 1", "description": "第一步", "optional_skills": ["a1", "a2"]},
            {"name": "Merge", "description": "合并结果", "instruction": "按时间合并", "optional_skills": ["m1", "m2"]},
            {"name": "Step 2", "description": "第二步", "optional_skills": ["b1", "b2"]},
            {"name": "Single", "description": "唯一 Skill", "optional_skills": ["only"]},
            {"name": "Tail", "description": "收尾", "optional_skills": []},
        ],
    }
]


def build(tmp_path: Path, *, freeze_until: str | None = None) -> tuple[Machine, Session, Path, object]:
    cfg = parse_config(RAW, "workflow.config.json")
    m = Machine(cfg, Session(config_path="workflow.config.json"))
    m.apply({"workflow": "W"})
    m.apply({"variables": ["Step 1", "Merge"]})
    m.apply({"skill": "b1"})
    m.apply({"action": "add"})
    m.apply({"skill": "a1"})
    m.apply({"skill": "m1"})
    m.apply({"action": "add"})
    m.apply({"skill": "a2"})
    m.apply({"skill": "m2"})
    m.apply({"action": "next"})
    m.apply({"test_prompt": "请对样本进行解析并输出报告。"})
    if freeze_until:
        m.apply({"action": "add"})
        m.apply({"type": "reusable_artifact"})
        m.apply({"groups": ["group-2"]})
        m.apply({"freeze_until": freeze_until})
        m.apply({"path": "D:/data/prefix.json"})
        m.apply({"note": "使用上次的解析结果，不要重新解析。"})
    m.apply({"action": "next"})
    m.apply({"output_root": "wfbm-runs", "confirmed": "确认并开始"})

    sdir = tmp_path / ".wfbm"
    sdir.mkdir(parents=True, exist_ok=True)
    return m, m.session, sdir, cfg


def test_prompt_contains_task_workflow_and_steps(tmp_path):
    m, session, sdir, cfg = build(tmp_path)
    out_dir, manifest = out.finalize(cfg, session, sdir, tmp_path)
    text = (out_dir / "prompts" / "group-1.md").read_text(encoding="utf-8")

    assert "# 工作流变体基准测试 · 测试组 1" in text
    assert "请对样本进行解析并输出报告。" in text
    assert "名称：W" in text
    assert "说明：示例工作流" in text
    # 变量步骤用本组的 Skill
    assert "### 第 1 步 · Step 1" in text
    assert "Skill 工具调用 `a1`" in text
    # 不变量步骤用固定 Skill
    assert "Skill 工具调用 `b1`" in text
    # 唯一选项自动选中的步骤
    assert "Skill 工具调用 `only`" in text
    # 没有 Skill 的步骤
    assert "（不使用任何 Skill，直接执行）" in text
    # instruction 覆盖 description
    assert "按时间合并" in text
    assert "合并结果" not in text
    # 产出要求里的绝对路径
    assert str((out_dir / "runs" / "group-1").resolve()) in text
    assert '"group_id": "group-1"' in text


def test_group2_prompt_reflects_its_own_skills(tmp_path):
    m, session, sdir, cfg = build(tmp_path)
    out_dir, _ = out.finalize(cfg, session, sdir, tmp_path)
    text = (out_dir / "prompts" / "group-2.md").read_text(encoding="utf-8")
    assert "Skill 工具调用 `a2`" in text
    assert "Skill 工具调用 `m2`" in text
    assert "测试组 2" in text


def test_frozen_prefix_replaces_steps(tmp_path):
    m, session, sdir, cfg = build(tmp_path, freeze_until="Merge")
    out_dir, manifest = out.finalize(cfg, session, sdir, tmp_path)

    g1 = (out_dir / "prompts" / "group-1.md").read_text(encoding="utf-8")
    assert "⛔" not in g1
    assert "D:/data/prefix.json" not in g1

    g2 = (out_dir / "prompts" / "group-2.md").read_text(encoding="utf-8")
    assert "### 第 1 步 · Step 1  ⛔ 本步骤不执行" in g2
    assert "### 第 2 步 · Merge  ⛔ 本步骤不执行" in g2
    assert "### 第 3 步 · Step 2" in g2
    assert "D:/data/prefix.json" in g2
    assert "使用上次的解析结果，不要重新解析。" in g2
    # 被冻结的步骤不该再出现 Skill 指派
    assert "Skill 工具调用 `a2`" not in g2
    assert "Skill 工具调用 `m2`" not in g2

    assert manifest["groups"][1]["frozen_until"] == 1
    assert manifest["groups"][0]["frozen_until"] == -1


def test_finalize_creates_expected_tree(tmp_path):
    m, session, sdir, cfg = build(tmp_path)
    out_dir, manifest = out.finalize(cfg, session, sdir, tmp_path)

    assert (out_dir / "session.json").is_file()
    assert (out_dir / "config.snapshot.json").is_file()
    assert (out_dir / "manifest.json").is_file()
    assert (out_dir / "prompts" / "group-1.md").is_file()
    assert (out_dir / "prompts" / "group-2.md").is_file()
    assert (out_dir / "runs" / "group-1").is_dir()
    assert (out_dir / "runs" / "group-2").is_dir()
    assert out_dir.parent == tmp_path / "wfbm-runs"

    on_disk = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["workflow"]["name"] == "W"
    assert on_disk["variables"] == ["Step 1", "Merge"]
    assert [g["id"] for g in on_disk["groups"]] == ["group-1", "group-2"]
    assert all(g["status"] == "pending" for g in on_disk["groups"])
    assert manifest["invariants"]["Single"] == {"skill": "only", "auto": True, "reason": "single_option"}
    assert manifest["invariants"]["Tail"]["reason"] == "no_skills"


def test_timestamp_collision_gets_suffix(tmp_path):
    m, session, sdir, cfg = build(tmp_path)
    first, _ = out.finalize(cfg, session, sdir, tmp_path)
    second, _ = out.finalize(cfg, session, sdir, tmp_path)
    assert first != second
    assert second.name.endswith("-2")


def test_mark_and_report(tmp_path):
    m, session, sdir, cfg = build(tmp_path)
    out_dir, _ = out.finalize(cfg, session, sdir, tmp_path)

    out.mark_group(sdir, "group-1", "running")
    out.mark_group(sdir, "group-1", "done")

    run2 = out_dir / "runs" / "group-2"
    (run2 / "RESULT.md").write_text("# 组 2 报告\n", encoding="utf-8")
    (run2 / "result.json").write_text(
        json.dumps(
            {
                "group_id": "group-2",
                "status": "done",
                "artifacts": ["a.txt", "b.txt"],
                "summary": "整体可用",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary, manifest = out.report(sdir)

    assert (out_dir / "SUMMARY.md").is_file()
    assert "组 1" in summary and "组 2" in summary
    assert "整体可用" in summary
    assert str(run2.resolve()) in summary
    assert str((run2 / "RESULT.md").resolve()) in summary

    by_id = {g["id"]: g for g in manifest["groups"]}
    assert by_id["group-1"]["status"] == "done"
    assert by_id["group-2"]["status"] == "done"
    assert by_id["group-2"]["result_json"] == "runs/group-2/result.json"


def test_report_before_finalize_is_usage_error(tmp_path):
    m, session, sdir, cfg = build(tmp_path)
    import pytest

    from wfbm.errors import UsageError

    with pytest.raises(UsageError):
        out.report(sdir)
