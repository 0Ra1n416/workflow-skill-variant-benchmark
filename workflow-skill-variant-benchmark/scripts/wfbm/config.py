"""workflow.config.json 的定位、载入与校验。

校验规则见 TODO.md §3.3。阻断级问题抛 ConfigError；警告级问题随结果带回，
由调用方回显给用户（不静默吞掉）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError, Issue

CONFIG_FILENAME = "workflow.config.json"

_KNOWN_TOP_KEYS = {"version", "workflows"}
_KNOWN_STEP_KEYS = {"name", "description", "instruction", "optional_skills"}


@dataclass(frozen=True)
class Step:
    """工作流中的一个步骤。index 为 0 基下标，即执行顺序。"""

    index: int
    name: str
    description: str
    instruction: str | None
    optional_skills: tuple[str, ...]

    @property
    def prompt_text(self) -> str:
        """拼进最终 Prompt 的「指令」文本。"""
        return self.instruction or self.description or "（本步骤无指令，按步骤名理解）"

    @property
    def order(self) -> int:
        """给人看的 1 基序号。"""
        return self.index + 1


@dataclass(frozen=True)
class Workflow:
    name: str
    description: str
    steps: tuple[Step, ...]

    def step(self, name: str) -> Step:
        for s in self.steps:
            if s.name == name:
                return s
        raise KeyError(name)


@dataclass(frozen=True)
class LoadedConfig:
    path: Path
    raw: Any
    workflows: tuple[Workflow, ...]
    warnings: tuple[Issue, ...]

    def workflow(self, name: str) -> Workflow:
        for w in self.workflows:
            if w.name == name:
                return w
        raise KeyError(name)


def locate_config(cwd: Path) -> Path | None:
    """在 cwd 里找 workflow.config.json。"""
    candidate = cwd / CONFIG_FILENAME
    return candidate if candidate.is_file() else None


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def _type_name(value: Any) -> str:
    return {
        str: "str",
        int: "int",
        float: "float",
        bool: "bool",
        list: "list",
        dict: "dict",
        type(None): "null",
    }.get(type(value), type(value).__name__)


def _require_str(value: Any, path: str, blocking: list[Issue]) -> str | None:
    if value is None:
        blocking.append(Issue(path, "字段缺失"))
        return None
    if not isinstance(value, str):
        blocking.append(Issue(path, f"必须是字符串（当前为 {_type_name(value)}）"))
        return None
    if not value.strip():
        blocking.append(Issue(path, "不能为空字符串"))
        return None
    return value


def _optional_str(value: Any, path: str, blocking: list[Issue]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        blocking.append(Issue(path, f"必须是字符串（当前为 {_type_name(value)}）"))
        return None
    return value or None


def _parse_skills(value: Any, path: str, blocking: list[Issue]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        blocking.append(Issue(path, f"必须是字符串数组（当前为 {_type_name(value)}）"))
        return ()
    seen: list[str] = []
    for i, item in enumerate(value):
        item_path = f"{path}[{i}]"
        if not isinstance(item, str):
            blocking.append(Issue(item_path, f"必须是字符串（当前为 {_type_name(item)}）"))
            continue
        if not item.strip():
            blocking.append(Issue(item_path, "不能为空字符串"))
            continue
        if item in seen:
            blocking.append(Issue(item_path, f"与前面的条目重复（{item!r}）"))
            continue
        seen.append(item)
    return tuple(seen)


def _parse_steps(raw: Any, wf_path: str, blocking: list[Issue], warnings: list[Issue]) -> tuple[Step, ...]:
    if raw is None:
        blocking.append(Issue(f"{wf_path}.steps", "字段缺失"))
        return ()
    if not isinstance(raw, list):
        blocking.append(Issue(f"{wf_path}.steps", f"必须是数组（当前为 {_type_name(raw)}）"))
        return ()
    if not raw:
        blocking.append(Issue(f"{wf_path}.steps", "不能为空"))
        return ()

    steps: list[Step] = []
    seen_names: set[str] = set()
    for i, item in enumerate(raw):
        sp = f"{wf_path}.steps[{i}]"
        if not isinstance(item, dict):
            blocking.append(Issue(sp, f"必须是对象（当前为 {_type_name(item)}）"))
            continue

        for key in item:
            if key not in _KNOWN_STEP_KEYS:
                warnings.append(Issue(f"{sp}.{key}", "未知字段，已忽略"))

        name = _require_str(item.get("name"), f"{sp}.name", blocking)
        if name is not None:
            if name in seen_names:
                blocking.append(Issue(f"{sp}.name", f"与同一 workflow 内的其他步骤重名（{name!r}）"))
                name = None
            else:
                seen_names.add(name)

        description = _optional_str(item.get("description"), f"{sp}.description", blocking)
        instruction = _optional_str(item.get("instruction"), f"{sp}.instruction", blocking)
        skills = _parse_skills(item.get("optional_skills"), f"{sp}.optional_skills", blocking)

        if name is None:
            continue

        if not description and not instruction:
            warnings.append(
                Issue(
                    sp,
                    "description 与 instruction 同时为空，Prompt 中该步骤将写为"
                    "「（本步骤无指令，按步骤名理解）」",
                )
            )

        steps.append(
            Step(
                index=len(steps),
                name=name,
                description=description or "",
                instruction=instruction,
                optional_skills=skills,
            )
        )
    return tuple(steps)


def _parse_workflows(raw_list: Any, prefix: str, blocking: list[Issue], warnings: list[Issue]) -> tuple[Workflow, ...]:
    if not isinstance(raw_list, list):
        blocking.append(Issue(prefix or "workflows", f"必须是数组（当前为 {_type_name(raw_list)}）"))
        return ()
    if not raw_list:
        blocking.append(Issue(prefix or "workflows", "至少要包含一个 workflow"))
        return ()

    workflows: list[Workflow] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_list):
        wp = f"{prefix}[{i}]" if prefix else f"[{i}]"
        if not isinstance(item, dict):
            blocking.append(Issue(wp, f"必须是对象（当前为 {_type_name(item)}）"))
            continue

        name = _require_str(item.get("name"), f"{wp}.name", blocking)
        if name is not None:
            if name in seen:
                blocking.append(Issue(f"{wp}.name", f"与同一文件内的其他 workflow 重名（{name!r}）"))
                name = None
            else:
                seen.add(name)

        description = _optional_str(item.get("description"), f"{wp}.description", blocking)

        steps = _parse_steps(item.get("steps"), wp, blocking, warnings)

        if name is None or not steps:
            continue
        workflows.append(Workflow(name=name, description=description or "", steps=steps))
    return tuple(workflows)


def parse_config(raw: Any, path: str | Path, *, strict: bool = True) -> LoadedConfig:
    """校验已经解析好的 JSON 数据。strict=False 时不抛 ConfigError，仅返回 warnings。"""
    blocking: list[Issue] = []
    warnings: list[Issue] = []

    if isinstance(raw, list):
        workflows = _parse_workflows(raw, "", blocking, warnings)
    elif isinstance(raw, dict):
        for key in raw:
            if key not in _KNOWN_TOP_KEYS:
                warnings.append(Issue(key, "未知的顶层字段，已忽略"))
        if "workflows" not in raw:
            blocking.append(Issue("", '顶层既不是数组，也不包含 "workflows" 字段'))
            workflows = ()
        else:
            workflows = _parse_workflows(raw["workflows"], "workflows", blocking, warnings)
    else:
        blocking.append(Issue("", f"顶层必须是数组或对象（当前为 {_type_name(raw)}）"))
        workflows = ()

    if blocking and strict:
        raise ConfigError(path, blocking, warnings)

    return LoadedConfig(path=Path(path), raw=raw, workflows=workflows, warnings=tuple(warnings))


def load_config(path: str | Path) -> LoadedConfig:
    """读取并校验一个配置文件。"""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(p, [Issue("", "文件不存在")])

    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(p, [Issue("", f"无法读取：{exc}")]) from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(p, [Issue("", f"不是合法的 UTF-8 文本：{exc}")]) from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(p, [Issue("", f"不是合法的 JSON：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}")]) from exc

    return parse_config(raw, p)
