"""配置校验的测试。"""

from __future__ import annotations

import json

import pytest

from wfbm.config import load_config, parse_config
from wfbm.errors import ConfigError


def _wf(steps, name="W", description="d"):
    return {"name": name, "description": description, "steps": steps}


def _step(name, skills, **kw):
    return {"name": name, "description": f"{name} 的说明", "optional_skills": skills, **kw}


def test_array_form_ok():
    raw = [_wf([_step("A", ["s1", "s2"])])]
    cfg = parse_config(raw, "workflow.config.json")
    assert [w.name for w in cfg.workflows] == ["W"]
    assert cfg.workflows[0].steps[0].optional_skills == ("s1", "s2")
    assert cfg.warnings == ()


def test_object_form_ok():
    raw = {"version": 1, "workflows": [_wf([_step("A", ["s1"])])]}
    cfg = parse_config(raw, "workflow.config.json")
    assert [w.name for w in cfg.workflows] == ["W"]


def test_object_without_workflows_is_blocking():
    with pytest.raises(ConfigError) as ei:
        parse_config({"version": 1}, "workflow.config.json")
    assert "顶层既不是数组，也不包含" in ei.value.message


def test_empty_optional_skills_allowed():
    cfg = parse_config([_wf([_step("A", [])])], "workflow.config.json")
    assert cfg.workflows[0].steps[0].optional_skills == ()
    assert cfg.warnings == ()


def test_missing_skills_key_allowed():
    cfg = parse_config([_wf([{"name": "A", "description": "x"}])], "workflow.config.json")
    assert cfg.workflows[0].steps[0].optional_skills == ()


def test_duplicate_step_names_blocked():
    with pytest.raises(ConfigError) as ei:
        parse_config([_wf([_step("A", ["s1"]), _step("A", ["s2"])])], "workflow.config.json")
    assert "重名" in ei.value.message


def test_duplicate_workflow_names_blocked():
    with pytest.raises(ConfigError) as ei:
        parse_config([_wf([_step("A", ["s1"])]), _wf([_step("B", ["s2"])])], "workflow.config.json")
    assert "重名" in ei.value.message


def test_empty_steps_blocked():
    with pytest.raises(ConfigError) as ei:
        parse_config([_wf([])], "workflow.config.json")
    assert "不能为空" in ei.value.message


def test_skills_type_error_reports_json_path():
    with pytest.raises(ConfigError) as ei:
        parse_config([_wf([_step("A", ["s1"]), _step("B", "not-a-list")])], "workflow.config.json")
    assert "[0].steps[1].optional_skills" in ei.value.message
    assert "必须是字符串数组" in ei.value.message


def test_duplicate_skill_blocked():
    with pytest.raises(ConfigError) as ei:
        parse_config([_wf([_step("A", ["s1", "s1"])])], "workflow.config.json")
    assert "重复" in ei.value.message


def test_error_message_has_filename_prefix():
    with pytest.raises(ConfigError) as ei:
        parse_config([_wf([])], "some/dir/workflow.config.json")
    assert ei.value.message.startswith("workflow.config.json 格式不合法：")


def test_empty_description_and_instruction_is_warning():
    cfg = parse_config([_wf([{"name": "A", "optional_skills": []}])], "workflow.config.json")
    assert len(cfg.warnings) == 1
    assert "description 与 instruction 同时为空" in cfg.warnings[0].message
    assert cfg.workflows[0].steps[0].prompt_text == "（本步骤无指令，按步骤名理解）"


def test_instruction_overrides_description():
    cfg = parse_config([_wf([_step("A", ["s1"], instruction="做 A")])], "workflow.config.json")
    assert cfg.workflows[0].steps[0].prompt_text == "做 A"


def test_unknown_keys_are_warnings():
    raw = {"version": 1, "extra": 1, "workflows": [_wf([_step("A", ["s1"], color="red")])]}
    cfg = parse_config(raw, "workflow.config.json")
    assert len(cfg.warnings) == 2


def test_bad_json_raises(tmp_path):
    p = tmp_path / "workflow.config.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert "不是合法的 JSON" in ei.value.message


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(tmp_path / "nope.json")
    assert "文件不存在" in ei.value.message


def test_step_index_is_execution_order(tmp_path):
    raw = [_wf([_step("Step 1", ["a"]), _step("Merge", ["b"]), _step("Step 2", ["c"])])]
    p = tmp_path / "workflow.config.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    cfg = load_config(p)
    assert [s.name for s in cfg.workflows[0].steps] == ["Step 1", "Merge", "Step 2"]
    assert [s.order for s in cfg.workflows[0].steps] == [1, 2, 3]
