"""Skill Catalog —— 管理 Skill 的启用状态、按任务匹配、生成上下文注入。

职责（PRD §8.3）：
  1. 持有 load_skills() 扫描出的全部 Skill。
  2. 跟踪"哪些 Skill 当前启用"：默认取 frontmatter 的 enabled，
     可被 AgentProfile.skills 显式追加，也可运行时 enable/disable。
  3. resolve()：给定 AgentProfile.skills + 本轮 query，挑出要注入上下文的 Skill。
  4. build_skill_context()：把选中的 Skill 拼成一段注入 system prompt 的文本，
     并明确声明"列出的工具是需求，不代表授权"。

安全约束：本模块只负责*上下文*。任何"Skill 想用某工具"都不在这里授权——
工具可用性由 AgentProfile.tools + ToolRegistry + 审批策略独立决定。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.skills.loader import Skill, load_skills


class SkillCatalog:
    """Skill 目录 + 启用状态 + 解析/注入逻辑��"""

    def __init__(self, skills: Optional[dict[str, Skill]] = None):
        self._skills: dict[str, Skill] = dict(skills or {})
        # 默认启用集合 = frontmatter enabled=true 的 Skill
        self._enabled: set[str] = {
            sid for sid, s in self._skills.items() if s.enabled
        }

    # ── 构造 ────────────────────────────────────────────────────────────────

    @classmethod
    def from_dir(cls, skills_dir: Optional[Path] = None) -> "SkillCatalog":
        return cls(load_skills(skills_dir))

    # ── 查询 ──────────────────────────���─────────────────────────────────────

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, skill_id: str) -> Optional[Skill]:
        return self._skills.get(skill_id)

    def is_enabled(self, skill_id: str) -> bool:
        return skill_id in self._enabled

    def enabled_skills(self) -> list[Skill]:
        return [self._skills[sid] for sid in self._enabled if sid in self._skills]

    # ── 启用 / 禁用 ──────���───────────────────────────────────────────────────

    def enable(self, skill_id: str) -> bool:
        """启用一个 Skill，返回是否成功（未知 skill_id 返回 False）。"""
        if skill_id not in self._skills:
            return False
        self._enabled.add(skill_id)
        return True

    def disable(self, skill_id: str) -> bool:
        self._enabled.discard(skill_id)
        return skill_id in self._skills

    # ── 解析：选出本轮要注入的 Skill ───────────────────────────────────────────

    def resolve(self, profile_skills: Optional[list[str]] = None,
                query: str = "") -> list[Skill]:
        """挑出要注入上下文的 Skill，去重后按 skill_id 稳定排序。

        来源有三：
          1. AgentProfile.skills 显式引用的（视为对该 Agent 显式启用，
             即便它默认 disabled——这就是 §8.3.5 "启用前用户显式声明"）。
          2. 全局已启用（frontmatter enabled=true 或 enable() 过）的。
          3. 已启用 Skill 中触发关键词命中 query 的（自动推荐）。

        说明：profile 引用的 Skill 无条件加入；全局启用的 Skill 若有 triggers
        则需命中 query 才加入（避免无关 Skill 占用上下文），无 triggers 的
        全局启用 Skill 始终加入（用户已显式表态要用它）。
        """
        chosen: dict[str, Skill] = {}

        for sid in (profile_skills or []):
            skill = self._skills.get(sid)
            if skill is not None:
                chosen[sid] = skill

        for sid in self._enabled:
            skill = self._skills.get(sid)
            if skill is None or sid in chosen:
                continue
            if skill.triggers:
                if skill.matches(query):
                    chosen[sid] = skill
            else:
                chosen[sid] = skill

        return [chosen[sid] for sid in sorted(chosen)]

    # ── 上下文注入 ──────────────────────────────────────────────────────────

    def build_skill_context(self, skills: list[Skill]) -> str:
        """把选中的 Skill 拼成注入 system prompt 的文本块。空列表返回 ""。"""
        if not skills:
            return ""
        blocks: list[str] = ["【已启用 Skill（任务指导，不授予���具权限）】"]
        for s in skills:
            blocks.append(f"\n## Skill: {s.name}")
            if s.description:
                blocks.append(s.description)
            if s.allowed_tools:
                blocks.append(
                    f"建议工具: {', '.join(s.allowed_tools)}"
                    f"（需求声明，实际可用工具仍以本次会话授权为准）"
                )
            if s.optional_mcp_servers:
                blocks.append(f"可选 MCP: {', '.join(s.optional_mcp_servers)}")
            if s.workflow:
                blocks.append(s.workflow)
        return "\n".join(blocks)

    def inject(self, system_prompt: str,
               profile_skills: Optional[list[str]] = None,
               query: str = "") -> str:
        """便捷方法：resolve + build_skill_context + 追加到 system prompt 末尾。"""
        context = self.build_skill_context(self.resolve(profile_skills, query))
        if not context:
            return system_prompt
        return f"{system_prompt}\n\n{context}"
