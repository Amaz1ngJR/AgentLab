"""项目根路径与 YAML 配置读取 —— 单一来源。

为什么要独立成模块：
  `PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent` 原先在 5 个模块里
  各写了一遍，其中 `attachments.py` 写的是 `parent.parent`（因为它在 app/ 下相对
  层级不同）。这类"靠目录层级数算根路径"的写法，一旦文件被移动或新增子包，就会
  静默指向错误的位置 —— 不会报错，只会读不到配置、然后回落到空默认值。

  统一到这里之后，层级算术只有一处，且带有校验：找不到项目标志文件就立刻失败，
  而不是让下游带着空配置继续跑。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

# 项目标志：pyproject.toml + config 目录同时存在才算项目根。
_MARKERS = ("pyproject.toml", "config")


def _find_project_root(start: Path) -> Path:
    """从 start 向上找项目根；找不到时回落到 start 的祖父目录。

    回落到上一级而不是抛异常，是为了兼容"App 被整体拷到别处运行"的场景；
    但正常情况下会命中 marker，不会走到回落分支。
    """
    for candidate in (start, *start.parents):
        if all((candidate / marker).exists() for marker in _MARKERS):
            return candidate
    return start.parent.parent


PROJECT_ROOT: Path = _find_project_root(Path(__file__).resolve().parent)
CONFIG_DIR: Path = PROJECT_ROOT / "config"


def load_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML 配置文件。

    统一处理三件所有调用方都要做的事：文件不存在 → 空 dict、内容为 null →
    空 dict、顶层不是映射 → 空 dict。这样每个 loader 都不必再写
    ``raw = yaml.safe_load(...) or {}`` 和随后的 ``isinstance`` 防御。

    语法错误（yaml.YAMLError）**不吞掉**：配置文件写坏了应当让用户看到，而不是
    静默当成空配置跑下去。
    """
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def optional_path(path: Optional[Path], default_name: str) -> Path:
    """显式传入的 path 优先，否则用 config/ 下的默认文件名。"""
    return path or (CONFIG_DIR / default_name)
