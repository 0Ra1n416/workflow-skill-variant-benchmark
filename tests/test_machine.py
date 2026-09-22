"""状态机的测试。"""

from __future__ import annotations

import pytest

from wfbm.config import parse_config
from wfbm.errors import AnswerError
from wfbm.machine import Machine
from wfbm.model import Session

RAW = [
    {
        "name": "W",
        "description": "示例工作流",
        "steps": [
            {"name": "Step 1", "description": "第一步", "optional_skills": ["a1", "a2", "a3"]},
            {"name": "Merge", "description": "合并", "optional_skills": ["m1", "m2"]},
            {"name": "Step 2", "description": "第二步", "optional_skills": ["b1", "b2", "b3"]},
            {"name": "Single Simulation", "description": "只有唯一 Skill", "optional_skills": ["only"]},
            {"name": "NoSkill", "description": "没有可用 Skill", "optional_skills": []},
            {"name": "Last", "description": "最后一步", "optional_skills": ["z1", "z2"]},
        ],
    },
    {"name": "Other", "steps": [{"name": "A", "description": "x", "optional_skills": ["p", "q"]}]},
]


def make_machine() -> Machine:
    cfg = parse_config(RAW, "workflow.config.json")
    return Machine(cfg, Session(config_path="workflow.config.json"))


def to_groups(m: Machine, variables=("Step 1", "Merge"), invariants=("b1", "z1")) -> None:
    """走到 groups 页（已答完 workflow / variables / invariants）。"""
    m.apply({"workflow": "W"})
    m.apply({"variables": list(variables)})
    for skill in invariants:
        m.apply({"skill": skill})


def add_group(m: Machine, *skills: str) -> None:
    m.apply({"action": "add"})
    for skill in skills:
        m.apply({"skill": skill})


# ---------------------------------------------------------------------------


def test_first_page_is_workflow_with_options():
    m = make_machine()
    page = m.next_page()
    assert page.id == "workflow"
    assert page.stage == "1/7 选择工作流"
    assert [o.value for o in page.questions[0].options] == ["W", "Other"]


def test_render_is_direct_for_four_or_fewer_options():
    m = make_machine()
    q = m.next_page().questions[0]
    assert q.render == "direct"


def test_render_is_numbered_when_more_than_four_options():
    m = make_machine()
    m.apply({"workflow": "W"})
    q = m.next_page().questions[0]  # variables: 6 个步骤
    assert len(q.options) == 6
    assert q.render == "numbered"


def test_variables_page_lists_all_steps_and_disables_skill_less():
    m = make_machine()
    m.apply({"workflow": "W"})
    page = m.next_page()
    options = {o.value: o for o in page.questions[0].options}
    assert list(options) == ["Step 1", "Merge", "Step 2", "Single Simulation", "NoSkill", "Last"]
    assert options["NoSkill"].enabled is False
    assert "仅 1 个" in options["Single Simulation"].hint


def test_step_without_skills_cannot_be_a_variable():
    m = make_machine()
    m.apply({"workflow": "W"})
    with pytest.raises(AnswerError) as ei:
        m.apply({"variables": ["NoSkill"]})
    assert "不可选" in ei.value.message


def test_variables_need_at_least_one():
    m = make_machine()
    m.apply({"workflow": "W"})
    with pytest.raises(AnswerError) as ei:
        m.apply({"variables": []})
    assert "至少选择 1 个" in ei.value.message


def test_invariant_questions_skip_auto_and_empty_steps():
    m = make_machine()
    m.apply({"workflow": "W"})
    m.apply({"variables": ["Step 1", "Merge"]})
    # 需要问的只有 Step 2（3 个）和 Last（2 个）
    assert [s.name for s in m.invariant_steps_to_ask] == ["Step 2", "Last"]
    assert m.next_page().id == "invariant.Step 2"
    m.apply({"skill": "b1"})
    assert m.next_page().id == "invariant.Last"
    m.apply({"skill": "z1"})
    assert m.next_page().id == "groups"


def test_resolved_invariants_marks_auto_and_missing():
    m = make_machine()
    to_groups(m)
    resolved = m.resolved_invariants()
    assert resolved["Step 2"] == {"skill": "b1", "auto": False, "reason": None}
    assert resolved["Single Simulation"]["skill"] == "only"
    assert resolved["Single Simulation"]["auto"] is True
    assert resolved["NoSkill"]["skill"] is None
    assert resolved["NoSkill"]["reason"] == "no_skills"


def test_no_invariant_questions_when_all_auto():
    m = make_machine()
    m.apply({"workflow": "Other"})
    m.apply({"variables": ["A"]})
    assert m.invariant_steps_to_ask == []
    assert m.next_page().id == "groups"


def test_group_edit_asks_each_variable_step_in_workflow_order():
    m = make_machine()
    to_groups(m)
    m.apply({"action": "add"})
    assert m.next_page().id == "group_edit.Step 1"
    m.apply({"skill": "a1"})
    assert m.next_page().id == "group_edit.Merge"
    m.apply({"skill": "m2"})
    assert m.next_page().id == "groups"
    assert len(m.session.groups) == 1
    assert m.session.groups[0].skills == {"Step 1": "a1", "Merge": "m2"}
    assert m.session.groups[0].id == "group-1"


def test_fewer_than_two_groups_is_rejected():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    assert m.next_page().id == "groups"
    with pytest.raises(AnswerError) as ei:
        m.apply({"action": "next"})
    assert "测试组数量需要大于等于 2" in ei.value.message
    assert "当前 1 组" in ei.value.message


def test_two_groups_pass():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "next"})
    assert m.next_page().id == "test_prompt"


# -- 撤销测试组 --------------------------------------------------------------


def test_undo_option_absent_while_no_groups():
    m = make_machine()
    to_groups(m)
    values = [o.value for o in m.next_page().questions[0].options]
    assert values == ["add", "next"]


def test_undo_option_appears_once_a_group_exists():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    values = [o.value for o in m.next_page().questions[0].options]
    assert values == ["add", "undo", "next"]


def test_undo_removes_the_last_group():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "undo"})
    assert [g.id for g in m.session.groups] == ["group-1"]
    assert m.session.groups[0].skills == {"Step 1": "a1", "Merge": "m1"}
    assert m.next_page().id == "groups"


def test_undo_can_be_repeated_down_to_zero():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "undo"})
    m.apply({"action": "undo"})
    assert m.session.groups == []
    assert [o.value for o in m.next_page().questions[0].options] == ["add", "next"]
    # 没有组时 undo 不再是合法选项，提交会被拒
    with pytest.raises(AnswerError):
        m.apply({"action": "undo"})


def test_undo_does_not_reuse_group_id():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "undo"})
    add_group(m, "a3", "m2")
    assert [g.id for g in m.session.groups] == ["group-1", "group-3"]
    # 显示序号按位置重排，界面上仍是「组 1 / 组 2」
    assert [g.index for g in m.session.groups] == [1, 2]


def test_undo_then_next_still_enforces_minimum():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "undo"})
    with pytest.raises(AnswerError) as ei:
        m.apply({"action": "next"})
    assert "测试组数量需要大于等于 2" in ei.value.message


def test_test_prompt_cannot_be_empty():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "next"})
    with pytest.raises(AnswerError):
        m.apply({"test_prompt": "   "})
    m.apply({"test_prompt": "请对 xxx 进行测试"})
    assert m.next_page().id == "attachments"


def test_attachments_can_be_skipped():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "next"})
    m.apply({"test_prompt": "任务"})
    m.apply({"action": "next"})
    assert m.session.attachments == []
    assert m.next_page().id == "confirm"


def test_attachment_happy_path():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "next"})
    m.apply({"test_prompt": "任务"})
    m.apply({"action": "add"})
    assert m.next_page().id == "attach_type"
    m.apply({"type": "reusable_artifact"})
    assert m.next_page().id == "attach_groups"
    m.apply({"groups": ["group-2"]})
    assert m.next_page().id == "attach_step"
    m.apply({"freeze_until": "Merge"})
    assert m.next_page().id == "attach_path"
    m.apply({"path": "D:/data/prefix.json"})
    assert m.next_page().id == "attach_note"
    m.apply({"note": "用上次结果"})
    assert m.next_page().id == "attachments"

    att = m.session.attachments[0]
    assert att.groups == ["group-2"]
    assert att.freeze_until == "Merge"
    assert att.path == "D:/data/prefix.json"
    assert m.frozen_until_index("group-2") == 1
    assert m.frozen_until_index("group-1") == -1


def test_attachment_note_may_be_empty():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "next"})
    m.apply({"test_prompt": "任务"})
    m.apply({"action": "add"})
    m.apply({"type": "reusable_artifact"})
    m.apply({"groups": ["group-1"]})
    m.apply({"freeze_until": "Step 1"})
    m.apply({"path": "p.json"})
    m.apply({"note": ""})
    assert m.session.attachments[0].note == ""


def test_attach_groups_requires_at_least_one():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "next"})
    m.apply({"test_prompt": "任务"})
    m.apply({"action": "add"})
    m.apply({"type": "reusable_artifact"})
    with pytest.raises(AnswerError) as ei:
        m.apply({"groups": []})
    assert "至少选择一个测试组" in ei.value.message


def test_confirm_cancel_marks_cancelled():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "next"})
    m.apply({"test_prompt": "任务"})
    m.apply({"action": "next"})
    m.apply({"output_root": "out", "confirmed": "取消"})
    assert m.session.cancelled is True
    assert m.next_page() is None


def test_confirm_defaults_output_root():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "next"})
    m.apply({"test_prompt": "任务"})
    m.apply({"action": "next"})
    page = m.next_page()
    assert page.questions[0].default == "wfbm-runs"
    m.apply({"output_root": "", "confirmed": "确认并开始"})
    assert m.session.output_root == "wfbm-runs"
    assert m.session.cancelled is False


def test_confirm_body_contains_key_facts():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "next"})
    m.apply({"test_prompt": "请对 xxx 进行测试"})
    m.apply({"action": "next"})
    body = m.next_page().body
    assert "工作流：W" in body
    assert "Step 1" in body and "Merge" in body
    assert "自动选中" in body
    assert "无可用 Skill" in body
    assert "请对 xxx 进行测试" in body
    assert "输出根目录：wfbm-runs" in body


def test_summary_warns_when_everything_is_frozen():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    m.apply({"action": "next"})
    m.apply({"test_prompt": "任务"})
    m.apply({"action": "add"})
    m.apply({"type": "reusable_artifact"})
    m.apply({"groups": ["group-1"]})
    m.apply({"freeze_until": "Last"})
    m.apply({"path": "p.json"})
    m.apply({"note": ""})
    m.apply({"action": "next"})
    body = m.next_page().body
    assert "所有步骤都被冻结" in body


def test_out_of_phase_answer_is_rejected():
    """状态机只接受当前页面的答案，Agent 无法「跳步」。"""
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    # 当前在 groups 页，却想直接回答 workflow
    with pytest.raises(AnswerError) as ei:
        m.apply({"workflow": "Other"})
    assert "必须选一项" in ei.value.message
    assert m.session.workflow == "W"


# -- 答案归一化 --------------------------------------------------------------


def test_value_with_space_is_not_split():
    m = make_machine()
    m.apply({"workflow": "W"})
    m.apply({"variables": ["Step 1"]})
    assert m.session.variables == ["Step 1"]


def test_numeric_indices_are_accepted():
    m = make_machine()
    m.apply({"workflow": "2"})
    assert m.session.workflow == "Other"


def test_comma_separated_string_is_accepted():
    m = make_machine()
    m.apply({"workflow": "W"})
    m.apply({"variables": "Step 1,Merge"})
    assert m.session.variables == ["Step 1", "Merge"]


def test_all_keyword_selects_everything_enabled():
    m = make_machine()
    m.apply({"workflow": "W"})
    m.apply({"variables": "all"})
    assert m.session.variables == ["Step 1", "Merge", "Step 2", "Single Simulation", "Last"]


def test_unknown_option_reports_error():
    m = make_machine()
    with pytest.raises(AnswerError) as ei:
        m.apply({"workflow": "不存在的工作流"})
    assert "无法识别的选项" in ei.value.message


def test_single_select_rejects_multiple():
    m = make_machine()
    with pytest.raises(AnswerError) as ei:
        m.apply({"workflow": "W,Other"})
    assert "只能选一个" in ei.value.message


def test_answers_must_be_a_dict():
    m = make_machine()
    with pytest.raises(AnswerError):
        m.apply(["W"])  # type: ignore[arg-type]


def test_missing_answer_is_rejected():
    m = make_machine()
    with pytest.raises(AnswerError):
        m.apply({})


# -- 序列化 ------------------------------------------------------------------


def test_session_roundtrip():
    m = make_machine()
    to_groups(m)
    add_group(m, "a1", "m1")
    add_group(m, "a2", "m2")
    data = m.session.to_dict()
    restored = Session.from_dict(data)
    assert restored.to_dict() == data


def test_numbered_render_for_many_groups():
    m = make_machine()
    to_groups(m)
    for _ in range(5):
        add_group(m, "a1", "m1")
    m.apply({"action": "next"})
    m.apply({"test_prompt": "任务"})
    m.apply({"action": "add"})
    m.apply({"type": "reusable_artifact"})
    page = m.next_page()
    assert page.id == "attach_groups"
    assert len(page.questions[0].options) == 5
    assert page.questions[0].render == "numbered"


def test_groups_page_action_is_direct():
    m = make_machine()
    to_groups(m)
    page = m.next_page()
    assert page.id == "groups"
    assert page.questions[0].render == "direct"
