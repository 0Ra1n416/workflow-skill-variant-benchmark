#!/usr/bin/env python3
"""wfbm 的入口脚本。

经 cckit 安装后这样调用（cwd 是你自己的项目目录）：

    cckit exec workflow-skill-variant-benchmark scripts/run.py <子命令> [参数...]

cckit 会用该 skill 的 venv 解释器运行本文件，并注入 CCKIT_SKILL_DIR /
CCKIT_KIT_DIR / CCKIT_ENV_DIR 等环境变量。

本文件只做一件事：把同目录下的 wfbm 包挂上 sys.path，然后交给 cli.main()。
不写解释器路径、不假设 cwd —— 所有资源路径都由包内部按 __file__ 推导。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from wfbm.cli import main  # noqa: E402  （必须在 sys.path 调整之后导入）

if __name__ == "__main__":
    sys.exit(main())
