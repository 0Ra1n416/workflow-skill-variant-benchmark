"""流程状态机。

**这是整个 skill 的唯一逻辑源。** 页面顺序、选项枚举、分页、校验、汇总全部在这里决定；
两个渲染器和 Agent 都只是搬运工，没有任何决策权。

接口只有两个：
    next_page() -> Page | None      # None 表示流程结束
    apply(answers) -> None          # 校验并写入答案，不合法则抛 AnswerError
"""

from __future__ import annotations

import re
from typing import Any

from .config import LoadedConfig, Step, Workflow
from .errors import AnswerError, Issue
from .model import (
    DEFAULT_OUTPUT_ROOT,
    NO_SKILL_LABEL,
    STAGE_LABELS,
    Attachment,
    Group,
    Option,
    Page,
    Question,
    Session,
)

_SPLIT_RE = re.compile(r"[,，、;；]+")
_CONFIRM_YES = "确认并开始"
_CONFIRM_NO = "取消"


def _coerce_values(raw: Any, options: tuple[Option, ...], *, multi: bool) -> list[str]:
    """把渲染器/Agent 传回来的答案归一化成 option.value 列表。

    接受三种形式（这样 Agent 就能把用户输入原样透传，无需自己理解）：
      * 值列表：            ["Step 1", "Merge"]
      * 编号串：            "1,3" / "1 3" / "all"
      * 单个值或单个编号：  "Step 1" / "2"

    注意：**先做整串精确匹配**，再考虑切分 —— 否则 "Step 1" 这种带空格的
    选项名会被切坏。
    """
    all_by_value = {o.value: o for o in options}
    # 编号与「展示顺序」一致：不可选项也占一个编号，只是选了会被拒。
    numeric: dict[int, str] = {i + 1: o.value for i, o in enumerate(options)}

    def _match_one(tok: str) -> Option | None:
        if tok in all_by_value:
            return all_by_value[tok]
        if tok.isdigit() and int(tok) in numeric:
            return all_by_value[numeric[int(tok)]]
        loose = [o for o in options if o.value.strip().casefold() == tok.casefold()]
        return loose[0] if len(loose) == 1 else None

    if raw is None:
        tokens: list[str] = []
    elif isinstance(raw, list):
        tokens = [str(x).strip() for x in raw if str(x).strip()]
    elif isinstance(raw, bool):
        raise AnswerError([Issue("", f"无法识别的选项：{raw!r}")])
    elif isinstance(raw, (int, float)):
        tokens = [str(int(raw))]
    else:
        text = str(raw).strip()
        if text.lower() in {"all", "全部", "全选", "*"}:
            tokens = [o.value for o in options if o.enabled]
        elif text in all_by_value:
            # 整串就是一个选项值（含空格也照样命中）
            tokens = [text]
        elif _SPLIT_RE.search(text):
            tokens = [t.strip() for t in _SPLIT_RE.split(text) if t.strip()]
        elif text.replace(" ", "").isdigit():
            # "1 3" 这种用空格分隔的编号串
            tokens = [t for t in text.split() if t]
        else:
            tokens = [text]

    resolved: list[str] = []
    for tok in tokens:
        option = _match_one(tok)
        if option is None:
            choices = "、".join(o.value for o in options if o.enabled) or "无"
            raise AnswerError([Issue("", f"无法识别的选项：{tok!r}（可选值：{choices}）")])
        if not option.enabled:
            raise AnswerError([Issue("", f"「{option.value}」不可选：{option.hint or '该项不可选'}")])
        resolved.append(option.value)

    if not multi and len(resolved) > 1:
        raise AnswerError([Issue("", f"这里只能选一个，收到 {len(resolved)} 个")])
    return resolved


class Machine:
    def __init__(self, config: LoadedConfig, session: Session):
        self.config = config
        self.session = session

    # ------------------------------------------------------------------
    # 派生量
    # ------------------------------------------------------------------

    @property
    def wf(self) -> Workflow:
        if not self.session.workflow:
            raise AnswerError([Issue("", "还没有选定工作流")])
        return self.config.workflow(self.session.workflow)

    @property
    def variable_steps(self) -> list[Step]:
        """变量步骤，按工作流的执行顺序排列。"""
        names = set(self.session.variables)
        return [s for s in self.wf.steps if s.name in names]

    @property
    def invariant_steps(self) -> list[Step]:
        """不变量步骤，按执行顺序排列。"""
        names = set(self.session.variables)
        return [s for s in self.wf.steps if s.name not in names]

    @property
    def invariant_steps_to_ask(self) -> list[Step]:
        """需要用户选的（可选 Skill 数 >= 2）；0 个和 1 个的都自动定。"""
        return [s for s in self.invariant_steps if len(s.optional_skills) >= 2]

    def resolved_invariants(self) -> dict[str, dict[str, Any]]:
        """不变量步骤 -> 最终使用的 Skill（含自动选定的说明）。"""
        out: dict[str, dict[str, Any]] = {}
        for step in self.invariant_steps:
            chosen = self.session.invariants.get(step.name)
            if chosen:
                out[step.name] = {"skill": chosen, "auto": False, "reason": None}
            elif len(step.optional_skills) == 1:
                out[step.name] = {"skill": step.optional_skills[0], "auto": True, "reason": "single_option"}
            else:
                out[step.name] = {"skill": None, "auto": False, "reason": "no_skills"}
        return out

    def attachments_for(self, group_id: str) -> list[Attachment]:
        return [a for a in self.session.attachments if group_id in a.groups]

    def frozen_until_index(self, group_id: str) -> int:
        """该测试组被冻结到第几步（含）。-1 表示不冻结。"""
        idx = -1
        for att in self.attachments_for(group_id):
            try:
                idx = max(idx, self.wf.step(att.freeze_until).index)
            except KeyError:
                continue
        return idx

    # ------------------------------------------------------------------
    # 页面构造
    # ------------------------------------------------------------------

    def next_page(self) -> Page | None:
        phase = self.session.phase
        if phase == "done":
            return None
        builder = getattr(self, f"_page_{phase}", None)
        if builder is None:
            raise AnswerError([Issue("", f"内部错误：未知的流程相位 {phase!r}")])
        return builder()

    def _page_workflow(self) -> Page:
        options = tuple(
            Option(value=w.name, label=w.name, hint=w.description or f"{len(w.steps)} 个步骤")
            for w in self.config.workflows
        )
        return Page(
            id="workflow",
            title="选择工作流",
            stage=STAGE_LABELS["workflow"],
            intro="从配置文件中选出本次要测试的工作流。",
            questions=(
                Question(
                    id="workflow",
                    kind="single_select",
                    title="要测试哪个工作流？",
                    options=options,
                ),
            ),
        )

    def _page_variables(self) -> Page:
        options = []
        for step in self.wf.steps:
            if step.optional_skills:
                hint = f"{len(step.optional_skills)} 个可选 Skill"
                if len(step.optional_skills) == 1:
                    hint += "（仅 1 个，各组取值将相同）"
                options.append(Option(value=step.name, label=step.name, hint=hint))
            else:
                options.append(
                    Option(value=step.name, label=step.name, hint="无可用 Skill，不能作为变量", enabled=False)
                )
        return Page(
            id="variables",
            title="指定变量步骤",
            stage=STAGE_LABELS["variables"],
            intro=(
                "选择哪些步骤作为本次测试的**变量**。这些步骤会在每个测试组里换用不同的 Skill。\n"
                "未选中的步骤是不变量，在整个测试中固定使用同一个 Skill。"
            ),
            body="\n".join(
                f"  {s.order}. {s.name}" + ("" if s.optional_skills else "   ← 无可用 Skill")
                for s in self.wf.steps
            ),
            questions=(
                Question(
                    id="variables",
                    kind="multi_select",
                    title="哪些步骤作为变量？（可多选，至少 1 个）",
                    options=tuple(options),
                    min_selected=1,
                ),
            ),
        )

    def _page_invariants(self) -> Page:
        steps = self.invariant_steps_to_ask
        idx = int(self.session.pending.get("inv_idx", 0))
        idx = max(0, min(idx, len(steps) - 1))
        step = steps[idx]
        options = tuple(Option(value=s, label=s) for s in step.optional_skills)
        return Page(
            id=f"invariant.{step.name}",
            title=f"为不变量步骤「{step.name}」指定 Skill",
            stage=STAGE_LABELS["invariants"],
            intro=f"该步骤在本次测试的所有组里都固定使用同一个 Skill。（第 {idx + 1}/{len(steps)} 个待定步骤）",
            body=f"步骤 {step.order} · {step.name}\n  {step.prompt_text}",
            questions=(
                Question(
                    id="skill",
                    kind="single_select",
                    title=f"「{step.name}」使用哪个 Skill？",
                    options=options,
                ),
            ),
        )

    def _page_groups(self) -> Page:
        actions = [Option(value="add", label="添加测试组")]
        if self.session.groups:
            actions.append(
                Option(
                    value="undo",
                    label="撤销上一个测试组",
                    hint=f"移除组 {len(self.session.groups)}",
                )
            )
        actions.append(Option(value="next", label="下一步", hint="至少要有 2 个测试组"))
        return Page(
            id="groups",
            title="制定测试组",
            stage=STAGE_LABELS["groups"],
            intro=(
                "每个测试组为所有变量步骤各指定一个 Skill。\n"
                "测试时，同一组内的变量步骤会**同时**取用该组指定的 Skill。"
            ),
            body=self._render_groups_body(),
            questions=(
                Question(
                    id="action",
                    kind="single_select",
                    title="接下来做什么？",
                    options=tuple(actions),
                ),
            ),
        )

    def _page_group_edit(self) -> Page:
        var_steps = self.variable_steps
        idx = int(self.session.pending.get("ge_idx", 0))
        idx = max(0, min(idx, len(var_steps) - 1))
        step = var_steps[idx]
        draft: dict[str, str] = dict(self.session.pending.get("draft", {}))
        options = tuple(Option(value=s, label=s) for s in step.optional_skills)
        return Page(
            id=f"group_edit.{step.name}",
            title=f"新测试组 · 为「{step.name}」选 Skill",
            stage=STAGE_LABELS["group_edit"],
            intro=f"正在添加第 {len(self.session.groups) + 1} 个测试组。（第 {idx + 1}/{len(var_steps)} 个变量步骤）",
            body="\n".join(f"  {k} = {v}" for k, v in draft.items()) or "  （本组尚未选择）",
            questions=(
                Question(
                    id="skill",
                    kind="single_select",
                    title=f"「{step.name}」在本组使用哪个 Skill？",
                    options=options,
                ),
            ),
        )

    def _page_test_prompt(self) -> Page:
        return Page(
            id="test_prompt",
            title="测试提示词",
            stage=STAGE_LABELS["test_prompt"],
            intro=(
                "这是**所有测试组共用**的提示词，会被原样拼进每组最终的 Prompt 里。\n"
                "它相当于这条工作流要完成的任务本身。"
            ),
            body=(
                "请注意：\n"
                "  · 不要写「第几步用哪个 Skill」这类内容 —— 那部分由本工具根据测试组自动拼接。\n"
                "  · 只描述任务目标与要求即可。\n"
                "  · 换行可以正常输入。"
            ),
            questions=(
                Question(
                    id="test_prompt",
                    kind="text",
                    title="请输入本次测试的提示词：",
                    required=True,
                    allow_empty=False,
                ),
            ),
        )

    def _page_attachments(self) -> Page:
        return Page(
            id="attachments",
            title="附加信息",
            stage=STAGE_LABELS["attachments"],
            intro="附加信息会在拼接 Prompt 时按测试组插入。可以不加，直接下一步。",
            body=self._render_attachments_body(),
            questions=(
                Question(
                    id="action",
                    kind="single_select",
                    title="接下来做什么？",
                    options=(
                        Option(value="add", label="添加附加信息"),
                        Option(value="next", label="下一步"),
                    ),
                ),
            ),
        )

    def _page_attach_type(self) -> Page:
        return Page(
            id="attach_type",
            title="附加信息 · 选择类型",
            stage=STAGE_LABELS["attach_type"],
            intro="目前只支持一种附加信息类型。",
            questions=(
                Question(
                    id="type",
                    kind="single_select",
                    title="要添加哪种附加信息？",
                    options=(
                        Option(
                            value="reusable_artifact",
                            label="可复用产物",
                            hint="指定测试组在某个步骤之前的数据直接复用既有文件，不重新执行",
                        ),
                    ),
                ),
            ),
        )

    def _page_attach_groups(self) -> Page:
        return Page(
            id="attach_groups",
            title="附加信息 · 适用于哪些测试组",
            stage=STAGE_LABELS["attach_groups"],
            intro="选中的测试组会应用这条附加信息。",
            questions=(
                Question(
                    id="groups",
                    kind="multi_select",
                    title="这条附加信息适用于哪些测试组？（可多选）",
                    options=tuple(Option(value=g.id, label=f"组 {g.index}") for g in self.session.groups),
                    min_selected=1,
                ),
            ),
        )

    def _page_attach_step(self) -> Page:
        options = tuple(
            Option(
                value=s.name,
                label=f"冻结到「{s.name}」为止",
                hint="该步骤及其之前全部不执行，从下一步开始",
            )
            for s in self.wf.steps
        )
        return Page(
            id="attach_step",
            title="附加信息 · 冻结到哪一步",
            stage=STAGE_LABELS["attach_step"],
            intro="被冻结的步骤不会执行，直接采用你提供的数据文件作为它们的产出。",
            questions=(
                Question(
                    id="freeze_until",
                    kind="single_select",
                    title="冻结到哪一步为止？",
                    options=options,
                ),
            ),
        )

    def _page_attach_path(self) -> Page:
        return Page(
            id="attach_path",
            title="附加信息 · 数据文件路径",
            stage=STAGE_LABELS["attach_path"],
            intro="被冻结的步骤将直接采用这个文件的内容作为产出。",
            questions=(
                Question(
                    id="path",
                    kind="text",
                    title="数据文件路径：",
                    help="可以是绝对路径，也可以是相对于工作目录的路径。这里不做存在性检查。",
                    required=True,
                    allow_empty=False,
                ),
            ),
        )

    def _page_attach_note(self) -> Page:
        return Page(
            id="attach_note",
            title="附加信息 · 补充说明",
            stage=STAGE_LABELS["attach_note"],
            intro="这段文字会被原样插入最终 Prompt 中该测试组的对应位置。可以留空。",
            questions=(
                Question(
                    id="note",
                    kind="text",
                    title="补充说明（可留空）：",
                    required=False,
                    allow_empty=True,
                ),
            ),
        )

    def _page_confirm(self) -> Page:
        return Page(
            id="confirm",
            title="信息确认",
            stage=STAGE_LABELS["confirm"],
            intro="确认无误后开始生成测试文件。",
            body=self.render_summary(),
            questions=(
                Question(
                    id="output_root",
                    kind="text",
                    title="输出根目录：",
                    help="本次测试的所有产物会放在该目录下的一个时间戳子目录里。",
                    default=self.session.output_root or DEFAULT_OUTPUT_ROOT,
                    required=True,
                    allow_empty=False,
                ),
                Question(
                    id="confirmed",
                    kind="confirm",
                    title="确认并开始？",
                    options=(Option(value=_CONFIRM_YES, label=_CONFIRM_YES), Option(value=_CONFIRM_NO, label=_CONFIRM_NO)),
                ),
            ),
        )

    # ------------------------------------------------------------------
    # 只读正文渲染
    # ------------------------------------------------------------------

    def _render_groups_body(self) -> str:
        if not self.session.groups:
            return "（尚未添加测试组）"
        lines = []
        for g in self.session.groups:
            pairs = "   ".join(f"{k}={v}" for k, v in g.skills.items())
            lines.append(f"  组 {g.index}： {pairs}")
        return "\n".join(lines)

    def _render_attachments_body(self) -> str:
        if not self.session.attachments:
            return "（尚未添加附加信息）"
        lines = []
        for i, att in enumerate(self.session.attachments, 1):
            groups = "、".join(f"组 {g.index}" for g in self.session.groups if g.id in att.groups)
            lines.append(f"  {i}. 可复用产物 → 适用于 [{groups}]")
            lines.append(f"     冻结到「{att.freeze_until}」为止；数据来源：{att.path}")
            if att.note:
                lines.append(f"     补充说明：{att.note}")
        return "\n".join(lines)

    def render_summary(self, *, truncate_prompt: int = 200) -> str:
        """第 7 步的汇总正文。不依赖任何渲染器。"""
        wf = self.wf
        out: list[str] = []
        out.append(f"工作流：{wf.name}")
        if wf.description:
            out.append(f"说明：{wf.description}")
        out.append("")

        out.append(f"变量步骤（{len(self.variable_steps)} 个）：")
        for s in self.variable_steps:
            out.append(f"  · {s.name}")
        out.append("")

        invariants = self.resolved_invariants()
        out.append(f"不变量步骤（{len(invariants)} 个）：")
        for name, info in invariants.items():
            if info["skill"]:
                suffix = "（唯一选项，自动选中）" if info["auto"] else ""
                out.append(f"  · {name}   →   {info['skill']}{suffix}")
            else:
                out.append(f"  · {name}   →   （无可用 Skill）")
        out.append("")

        out.append(f"测试组（{len(self.session.groups)} 组）：")
        for g in self.session.groups:
            pairs = "   ".join(f"{k}={v}" for k, v in g.skills.items())
            out.append(f"  组 {g.index}： {pairs}")
        out.append("")

        prompt = self.session.test_prompt
        if len(prompt) > truncate_prompt:
            prompt = prompt[:truncate_prompt] + f"…（完整内容见 session.json，共 {len(self.session.test_prompt)} 字）"
        out.append("测试提示词：")
        out.append("  " + prompt.replace("\n", "\n  "))
        out.append("")

        if self.session.attachments:
            out.append(f"附加信息（{len(self.session.attachments)} 条）：")
            out.append(self._render_attachments_body())
        else:
            out.append("附加信息：（无）")
        out.append("")

        warnings = self._structural_warnings()
        if warnings:
            for w in warnings:
                out.append(f"⚠ {w}")
            out.append("")

        out.append(f"输出根目录：{self.session.output_root or DEFAULT_OUTPUT_ROOT}")
        return "\n".join(out)

    def _structural_warnings(self) -> list[str]:
        """结构性问题提示。

        注意：这里**只报会让本次测试变得没有意义的结构性错误**（比如所有步骤都被冻结，
        跑起来什么都不会发生）。按用户决定，不做「多变量同时变动、差异无法归因」这类
        方法论层面的提示 —— 那是使用者的判断，工具不越俎代庖。
        """
        out: list[str] = []
        for g in self.session.groups:
            if self.frozen_until_index(g.id) >= len(self.wf.steps) - 1:
                out.append(f"组 {g.index} 的所有步骤都被冻结，该组将没有实际执行内容")
        return out

    # ------------------------------------------------------------------
    # 答案写入
    # ------------------------------------------------------------------

    def apply(self, answers: dict[str, Any]) -> None:
        if not isinstance(answers, dict):
            raise AnswerError([Issue("", "answers 必须是一个对象（question_id -> 答案）")])
        phase = self.session.phase
        if phase == "done":
            raise AnswerError([Issue("", "流程已经结束，没有待回答的问题")])
        handler = getattr(self, f"_apply_{phase}", None)
        if handler is None:
            raise AnswerError([Issue("", f"内部错误：未知的流程相位 {phase!r}")])
        handler(answers)

    # -- 各相位的写入 ---------------------------------------------------

    def _apply_workflow(self, answers: dict[str, Any]) -> None:
        page = self._page_workflow()
        q = page.questions[0]
        values = _coerce_values(answers.get(q.id), q.options, multi=False)
        if not values:
            raise AnswerError([Issue(q.id, "必须选一个工作流")])
        self.session.workflow = values[0]

        # 换工作流时，后面所有依赖它的选择全部作废
        self.session.variables = []
        self.session.invariants = {}
        self.session.groups = []
        self.session.group_seq = 0
        self.session.attachments = []
        self.session.pending = {}
        self.session.phase = "variables"

    def _apply_variables(self, answers: dict[str, Any]) -> None:
        page = self._page_variables()
        q = page.questions[0]
        values = _coerce_values(answers.get(q.id), q.options, multi=True)
        if len(values) < q.min_selected:
            raise AnswerError([Issue(q.id, f"至少选择 {q.min_selected} 个步骤作为变量")])

        by_name = {s.name: s for s in self.wf.steps}
        for name in values:
            step = by_name.get(name)
            if step is None:
                raise AnswerError([Issue(q.id, f"工作流里没有名为 {name!r} 的步骤")])
            if not step.optional_skills:
                raise AnswerError([Issue(q.id, f"步骤「{name}」没有任何可选 Skill，不能作为变量")])

        # 按工作流执行顺序存储，保证展示与产物稳定
        self.session.variables = [s.name for s in self.wf.steps if s.name in set(values)]
        self.session.invariants = {}
        self.session.groups = []
        self.session.group_seq = 0
        self.session.attachments = []
        self.session.pending = {}

        self.session.phase = "invariants" if self.invariant_steps_to_ask else "groups"
        self.session.pending["inv_idx"] = 0

    def _apply_invariants(self, answers: dict[str, Any]) -> None:
        page = self._page_invariants()
        q = page.questions[0]
        values = _coerce_values(answers.get(q.id), q.options, multi=False)
        if not values:
            raise AnswerError([Issue(q.id, "必须选一个 Skill")])
        idx = int(self.session.pending.get("inv_idx", 0))
        step = self.invariant_steps_to_ask[max(0, min(idx, len(self.invariant_steps_to_ask) - 1))]
        self.session.invariants[step.name] = values[0]

        idx += 1
        if idx >= len(self.invariant_steps_to_ask):
            self.session.phase = "groups"
            self.session.pending.pop("inv_idx", None)
        else:
            self.session.pending["inv_idx"] = idx

    def _apply_groups(self, answers: dict[str, Any]) -> None:
        page = self._page_groups()
        q = page.questions[0]
        values = _coerce_values(answers.get(q.id), q.options, multi=False)
        if not values:
            raise AnswerError([Issue(q.id, "必须选一项")])

        if values[0] == "add":
            self.session.pending["draft"] = {}
            self.session.pending["ge_idx"] = 0
            self.session.phase = "group_edit"
            return

        if values[0] == "undo":
            if not self.session.groups:
                raise AnswerError([Issue(q.id, "还没有测试组可以撤销")])
            removed = self.session.groups.pop()
            # 防御性清理：正常流程下此刻不可能有附加信息（附加信息在测试组之后才收集），
            # 但万一状态被手工改过，不能留下指向已删除组的悬空引用。
            self.session.attachments = [a for a in self.session.attachments if removed.id not in a.groups]
            # 显示序号按当前位置重排，保证界面上的「组 N」始终连续
            for i, g in enumerate(self.session.groups, 1):
                g.index = i
            return

        if len(self.session.groups) < 2:
            raise AnswerError(
                [Issue("groups", f"测试组数量需要大于等于 2（当前 {len(self.session.groups)} 组）")]
            )
        self.session.phase = "test_prompt"

    def _apply_group_edit(self, answers: dict[str, Any]) -> None:
        page = self._page_group_edit()
        q = page.questions[0]
        values = _coerce_values(answers.get(q.id), q.options, multi=False)
        if not values:
            raise AnswerError([Issue(q.id, "必须选一个 Skill")])

        step = self.variable_steps[max(0, min(int(self.session.pending.get("ge_idx", 0)), len(self.variable_steps) - 1))]
        step_name = step.name
        draft = dict(self.session.pending.get("draft", {}))
        draft[step_name] = values[0]
        idx = int(self.session.pending.get("ge_idx", 0)) + 1
        var_steps = self.variable_steps

        if idx >= len(var_steps):
            missing = [s.name for s in var_steps if s.name not in draft]
            if missing:
                raise AnswerError([Issue("groups", f"以下变量步骤还没有选 Skill：{'、'.join(missing)}")])
            self.session.group_seq += 1
            self.session.groups.append(
                Group(
                    id=f"group-{self.session.group_seq}",
                    index=len(self.session.groups) + 1,
                    skills=draft,
                )
            )
            self.session.pending.pop("draft", None)
            self.session.pending.pop("ge_idx", None)
            self.session.phase = "groups"
        else:
            self.session.pending["draft"] = draft
            self.session.pending["ge_idx"] = idx

    def _apply_test_prompt(self, answers: dict[str, Any]) -> None:
        page = self._page_test_prompt()
        q = page.questions[0]
        raw = answers.get(q.id)
        text = "" if raw is None else str(raw)
        if not text.strip():
            raise AnswerError([Issue(q.id, "测试提示词不能为空")])
        self.session.test_prompt = text
        self.session.phase = "attachments"

    def _apply_attachments(self, answers: dict[str, Any]) -> None:
        page = self._page_attachments()
        q = page.questions[0]
        values = _coerce_values(answers.get(q.id), q.options, multi=False)
        if not values:
            raise AnswerError([Issue(q.id, "必须选一项")])
        if values[0] == "add":
            self.session.pending["draft"] = {}
            self.session.phase = "attach_type"
        else:
            self.session.phase = "confirm"

    def _apply_attach_type(self, answers: dict[str, Any]) -> None:
        page = self._page_attach_type()
        q = page.questions[0]
        values = _coerce_values(answers.get(q.id), q.options, multi=False)
        if not values:
            raise AnswerError([Issue(q.id, "必须选一个类型")])
        draft = dict(self.session.pending.get("draft", {}))
        draft["type"] = values[0]
        self.session.pending["draft"] = draft
        self.session.phase = "attach_groups"

    def _apply_attach_groups(self, answers: dict[str, Any]) -> None:
        page = self._page_attach_groups()
        q = page.questions[0]
        values = _coerce_values(answers.get(q.id), q.options, multi=True)
        if len(values) < 1:
            raise AnswerError([Issue(q.id, "至少选择一个测试组")])
        draft = dict(self.session.pending.get("draft", {}))
        draft["groups"] = values
        self.session.pending["draft"] = draft
        self.session.phase = "attach_step"

    def _apply_attach_step(self, answers: dict[str, Any]) -> None:
        page = self._page_attach_step()
        q = page.questions[0]
        values = _coerce_values(answers.get(q.id), q.options, multi=False)
        if not values:
            raise AnswerError([Issue(q.id, "必须选一个步骤")])
        draft = dict(self.session.pending.get("draft", {}))
        draft["freeze_until"] = values[0]
        self.session.pending["draft"] = draft
        self.session.phase = "attach_path"

    def _apply_attach_path(self, answers: dict[str, Any]) -> None:
        page = self._page_attach_path()
        q = page.questions[0]
        raw = answers.get(q.id)
        text = "" if raw is None else str(raw)
        if not text.strip():
            raise AnswerError([Issue(q.id, "数据文件路径不能为空")])
        draft = dict(self.session.pending.get("draft", {}))
        draft["path"] = text.strip()
        self.session.pending["draft"] = draft
        self.session.phase = "attach_note"

    def _apply_attach_note(self, answers: dict[str, Any]) -> None:
        page = self._page_attach_note()
        q = page.questions[0]
        raw = answers.get(q.id)
        note = "" if raw is None else str(raw).strip()

        draft = dict(self.session.pending.get("draft", {}))
        draft["note"] = note
        known = {g.id for g in self.session.groups}
        missing = [g for g in draft.get("groups", []) if g not in known]
        if missing:
            raise AnswerError([Issue("groups", f"引用了不存在的测试组：{'、'.join(missing)}")])

        self.session.attachments.append(
            Attachment(
                type=draft["type"],
                groups=list(draft["groups"]),
                freeze_until=draft["freeze_until"],
                path=draft["path"],
                note=note,
            )
        )
        self.session.pending.pop("draft", None)
        self.session.phase = "attachments"

    def _apply_confirm(self, answers: dict[str, Any]) -> None:
        page = self._page_confirm()
        root_q, confirm_q = page.questions

        raw_root = answers.get(root_q.id)
        root = "" if raw_root is None else str(raw_root).strip()
        if not root:
            root = root_q.default or DEFAULT_OUTPUT_ROOT
        self.session.output_root = root

        values = _coerce_values(answers.get(confirm_q.id), confirm_q.options, multi=False)
        if not values:
            raise AnswerError([Issue(confirm_q.id, f"必须选择「{_CONFIRM_YES}」或「{_CONFIRM_NO}」")])
        self.session.cancelled = values[0] == _CONFIRM_NO
        self.session.phase = "done"


__all__ = ["Machine", "NO_SKILL_LABEL"]
