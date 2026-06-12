"""Skill Loader —— 扫描 skills/ 目录，解析 SKILL.md，校验 metadata。

Skill 是本地可配置的"任务指导包"（PRD §8）：告诉 Agent 某类任务该遵循的步骤、
允许使用的工具、参考材料和输出约束。

关键安全约束（PRD §8.3.4 / §8.3.5）：
  - Skill 声明的 allowed_tools 是"需求或上限"，*不代表自动授权*。
    Skill 只影响上下文（注入工作流说明），工具能否被调用仍由 AgentProfile.tools
    和审批策略决定。
  - 来自未知来源的 Skill 默认禁用：metadata 里 enabled 缺省为 False，
    必须显式 `enabled: true` 或被 AgentProfile.skills 显式引用才会激活。

目录格式（PRD §8.2）::

    skills/
      code-review/
        SKILL.md            # 元数据 frontmatter + 工作流正文
        references/         # 可选；按需注入的参考资料
        scripts/            # 可选；只能经被批准的执行工具调用

SKILL.md 形如::

    ---
    name: code-review
    description: Review source changes for correctness and test gaps.
    allowed_tools: [read_file, list_dir]
    optional_mcp_servers: [git]
    triggers: [review, 代码审查]
    enabled: true
    ---

    # Workflow

    1. Read changed files and related tests.
    2. Report correctness and security issues before summaries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = PROJECT_ROOT / "skills"


@dataclass
class Skill:
    """一个 Skill 包的解析结果。

    skill_id              - 稳定标识，等于目录名（不取 frontmatter，避免漂移）
    name                  - 展示名称（frontmatter name，缺省用 skill_id）
    description           - 一句话说明，用于展示和（未来）自动推荐
    workflow              - SKILL.md 正文（frontmatter 之后的 markdown），注入上下文
    allowed_tools         - 声明所需工具，是"上限/需求"而非授权（见模块 docstring）
    optional_mcp_servers  - 声明可选 MCP server 需求
    triggers              - 触发关键词，用于 query 匹配做自动推荐
    enabled               - 是否默认启用；未知来源 Skill 缺省 False
    references            - references/ 下附带的参考资料文件路径
    source_dir            - Skill 目录绝对路径
    """
    skill_id: str
    name: str
    description: str = ""
    workflow: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    optional_mcp_servers: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    enabled: bool = False
    references: list[Path] = field(default_factory=list)
    source_dir: Optional[Path] = None

    def matches(self, query: str) -> bool:
        """query 是否命中该 Skill 的触发关键词（大小写不敏感）。"""
        if not query or not self.triggers:
            return False
        q = query.lower()
        return any(t.lower() in q for t in self.triggers if t)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """把 SKILL.md 拆成 (frontmatter dict, 正文)。

    没有合法 frontmatter 时返回 ({}, 全文)，让上层按"缺 metadata"处理。
    """
    if not text.startswith("---"):
        return {}, text
    # 形如：---\n<yaml>\n---\n<body>
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    body = parts[2].lstrip("\n")
    return meta, body


def _as_str_list(value) -> list[str]:
    """把 frontmatter 里可能是 None / 字符串 / 列表的字段统一成字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None]
    return [str(value)]


def parse_skill(skill_dir: Path) -> Optional[Skill]:
    """解析单个 Skill 目录，返回 Skill；目录非法或缺 metadata 时返回 None。

    校验规则：
      - 必须有 SKILL.md
      - frontmatter 必须能解析出 name 或 description 之一（否则视为无效 Skill）
    """
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        return None
    meta, body = _split_frontmatter(md.read_text(encoding="utf-8"))
    name = (meta.get("name") or "").strip()
    description = (meta.get("description") or "").strip()
    if not name and not description:
        # 缺少最基本的 metadata,视为无效,不进 catalog
        return None

    refs: list[Path] = []
    ref_dir = skill_dir / "references"
    if ref_dir.is_dir():
        refs = sorted(p for p in ref_dir.iterdir() if p.is_file())

    return Skill(
        skill_id=skill_dir.name,
        name=name or skill_dir.name,
        description=description,
        workflow=body.strip(),
        allowed_tools=_as_str_list(meta.get("allowed_tools")),
        optional_mcp_servers=_as_str_list(meta.get("optional_mcp_servers")),
        triggers=_as_str_list(meta.get("triggers")),
        enabled=bool(meta.get("enabled", False)),
        references=refs,
        source_dir=skill_dir,
    )


def load_skills(skills_dir: Optional[Path] = None) -> dict[str, Skill]:
    """扫描 skills_dir 下每个子目录的 SKILL.md，返回 skill_id -> Skill。

    目录不存在时返回空字典（向后兼容，不强制要求该目录）。
    无效 / 缺 metadata 的子目录被静默跳过。
    """
    base = skills_dir or SKILLS_DIR
    if not base.is_dir():
        return {}
    skills: dict[str, Skill] = {}
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        skill = parse_skill(child)
        if skill is not None:
            skills[skill.skill_id] = skill
    return skills
