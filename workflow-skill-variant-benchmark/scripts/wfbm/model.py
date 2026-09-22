"""页面、问题、选项与流程状态的数据模型。

这些是纯数据；渲染器只读，状态机只写。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

QuestionKind = Literal["single_select", "multi_select", "text", "confirm", "info"]
RenderKind = Literal["direct", "numbered", "ask_in_chat", "display"]

SESSION_VERSION = 1
DEFAULT_OUTPUT_ROOT = "wfbm-runs"
NO_SKILL_LABEL = "（不使用任何 Skill，直接执行）"
PLACEHOLDER_INSTRUCTION = "（本步骤无指令，按步骤名理解）"


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    hint: str = ""
    enabled: bool = True

    def to_dict(self) -> dict:
        return {"value": self.value, "label": self.label, "hint": self.hint, "enabled": self.enabled}


@dataclass(frozen=True)
class Question:
    id: str
    kind: QuestionKind
    title: str
    help: str = ""
    options: tuple[Option, ...] = ()
    default: str | None = None
    min_selected: int = 0
    allow_empty: bool = False
    required: bool = True

    @property
    def render(self) -> RenderKind:
        """告诉渲染器/Agent 该怎么呈现这个必问题。

        由状态机计算，不由 Agent 判断 —— 这是「确定性」的关键之一。
        """
        if self.kind == "text":
            return "ask_in_chat"
        if self.kind == "info":
            return "display"
        # AskUserQuestion 每题最多 4 个选项，超过就退化成编号列表。
        return "direct" if len(self.options) <= 4 else "numbered"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "help": self.help,
            "render": self.render,
            "options": [o.to_dict() for o in self.options],
            "default": self.default,
            "min_selected": self.min_selected,
            "allow_empty": self.allow_empty,
            "required": self.required,
        }


@dataclass(frozen=True)
class Page:
    id: str
    title: str
    stage: str = ""
    intro: str = ""
    body: str = ""
    questions: tuple[Question, ...] = ()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "stage": self.stage,
            "intro": self.intro,
            "body": self.body,
            "questions": [q.to_dict() for q in self.questions],
        }


@dataclass
class Group:
    """一个测试组：为每个变量步骤指定一个 Skill。"""

    id: str
    index: int
    skills: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "index": self.index, "skills": dict(self.skills)}


@dataclass
class Attachment:
    """一条附加信息。目前只有「可复用产物」一种类型。"""

    type: str
    groups: list[str]
    freeze_until: str
    path: str
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "groups": list(self.groups),
            "freeze_until": self.freeze_until,
            "path": self.path,
            "note": self.note,
        }


#: 状态机的相位（页面）顺序
PHASES = (
    "workflow",
    "variables",
    "invariants",
    "groups",
    "group_edit",
    "test_prompt",
    "attachments",
    "attach_type",
    "attach_groups",
    "attach_step",
    "attach_path",
    "attach_note",
    "confirm",
    "done",
)

STAGE_LABELS = {
    "workflow": "1/7 选择工作流",
    "variables": "2/7 指定变量步骤",
    "invariants": "3/7 指定不变量的 Skill",
    "groups": "4/7 制定测试组",
    "group_edit": "4/7 制定测试组",
    "test_prompt": "5/7 测试提示词",
    "attachments": "6/7 附加信息",
    "attach_type": "6/7 附加信息",
    "attach_groups": "6/7 附加信息",
    "attach_step": "6/7 附加信息",
    "attach_path": "6/7 附加信息",
    "attach_note": "6/7 附加信息",
    "confirm": "7/7 信息确认",
    "done": "完成",
}


@dataclass
class Session:
    """流程的全部状态。每次 apply 之后整体落盘，所以随时可中断续跑。"""

    version: int = SESSION_VERSION
    config_path: str = ""
    #: init 时的工作目录。output_root 等相对路径一律相对它解析 ——
    #: 否则「用户在 A 目录 init、Agent 从 B 目录 finalize」会把产物写错地方。
    cwd: str = ""
    workflow: str | None = None
    variables: list[str] = field(default_factory=list)
    invariants: dict[str, str] = field(default_factory=dict)
    groups: list[Group] = field(default_factory=list)
    #: 测试组 id 的单调计数器。撤销后重新添加不会复用旧 id，避免附加信息指错组。
    group_seq: int = 0
    test_prompt: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    output_root: str = DEFAULT_OUTPUT_ROOT
    phase: str = "workflow"
    pending: dict[str, Any] = field(default_factory=dict)
    output_dir: str | None = None
    cancelled: bool = False

    # -- 序列化 ------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "config_path": self.config_path,
            "cwd": self.cwd,
            "workflow": self.workflow,
            "variables": list(self.variables),
            "invariants": dict(self.invariants),
            "groups": [g.to_dict() for g in self.groups],
            "group_seq": self.group_seq,
            "test_prompt": self.test_prompt,
            "attachments": [a.to_dict() for a in self.attachments],
            "output_root": self.output_root,
            "phase": self.phase,
            "pending": self.pending,
            "output_dir": self.output_dir,
            "cancelled": self.cancelled,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            version=data.get("version", SESSION_VERSION),
            config_path=data.get("config_path", ""),
            cwd=data.get("cwd", ""),
            workflow=data.get("workflow"),
            variables=list(data.get("variables", [])),
            invariants=dict(data.get("invariants", {})),
            groups=[Group(**g) for g in data.get("groups", [])],
            group_seq=int(data.get("group_seq", 0)),
            test_prompt=data.get("test_prompt", ""),
            attachments=[Attachment(**a) for a in data.get("attachments", [])],
            output_root=data.get("output_root", DEFAULT_OUTPUT_ROOT),
            phase=data.get("phase", "workflow"),
            pending=dict(data.get("pending", {})),
            output_dir=data.get("output_dir"),
            cancelled=data.get("cancelled", False),
        )


__all__ = [
    "Attachment",
    "DEFAULT_OUTPUT_ROOT",
    "Group",
    "NO_SKILL_LABEL",
    "Option",
    "PHASES",
    "PLACEHOLDER_INSTRUCTION",
    "Page",
    "Question",
    "QuestionKind",
    "RenderKind",
    "SESSION_VERSION",
    "STAGE_LABELS",
    "Session",
    "asdict",
]
