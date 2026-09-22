"""异常类型与错误条目。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Issue:
    """一条校验问题。

    path 是 JSON 路径（如 ``workflows[0].steps[2].optional_skills``），
    message 是给人读的说明。
    """

    path: str
    message: str

    def render(self) -> str:
        return f"{self.path} {self.message}" if self.path else self.message

    def to_dict(self) -> dict:
        return {"path": self.path, "message": self.message}


class WfbmError(Exception):
    """所有 wfbm 错误的基类。"""

    exit_code = 1

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UsageError(WfbmError):
    """调用方式不对（缺参数、目录不存在等）。"""

    exit_code = 2


class ConfigError(WfbmError):
    """workflow.config.json 不合法。"""

    def __init__(self, config_path: str | Path, blocking: list[Issue], warnings: list[Issue] | None = None):
        self.config_path = str(config_path)
        self.blocking = list(blocking)
        self.warnings = list(warnings or [])
        super().__init__(self.render())

    def render(self) -> str:
        name = Path(self.config_path).name or self.config_path
        lines = [f"{name} 格式不合法：{issue.render()}" for issue in self.blocking]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "kind": "error",
            "error": "invalid_config",
            "config_path": self.config_path,
            "message": self.render(),
            "issues": [i.to_dict() for i in self.blocking],
            "warnings": [i.to_dict() for i in self.warnings],
        }


class AnswerError(WfbmError):
    """用户/Agent 提交的答案没通过状态机校验。"""

    def __init__(self, issues: list[Issue]):
        self.issues = list(issues)
        super().__init__("\n".join(i.render() for i in self.issues))

    def to_dict(self) -> dict:
        return {
            "kind": "error",
            "error": "invalid_answer",
            "message": self.message,
            "issues": [i.to_dict() for i in self.issues],
        }
