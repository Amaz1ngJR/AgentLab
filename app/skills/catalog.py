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
from typing import Callable, Optional

from app.skills.loader import Skill, _split_frontmatter, load_skills

DEFAULT_INDEX_BUDGET = 6000
DEFAULT_DESCRIPTION_BUDGET = 240


class SkillCatalog:
    """Skill 目录 + 启用状态 + 解析/注入逻辑。"""

    def __init__(self, skills: Optional[dict[str, Skill]] = None,
                 *, index_budget: int = DEFAULT_INDEX_BUDGET,
                 description_budget: int = DEFAULT_DESCRIPTION_BUDGET,
                 audit_sink: Optional[Callable[[dict], None]] = None):
        self._skills: dict[str, Skill] = dict(skills or {})
        self.index_budget = max(0, index_budget)
        self.description_budget = max(0, description_budget)
        self._audit_sink = audit_sink
        # 默认启用集合 = frontmatter enabled=true 的 Skill
        self._enabled: set[str] = {
            sid for sid, s in self._skills.items() if s.enabled
        }

    # ── 构造 ────────────────────────────────────────────────────────────────

    @classmethod
    def from_dir(cls, skills_dir: Optional[Path] = None, **kwargs) -> "SkillCatalog":
        return cls(load_skills(skills_dir), **kwargs)

    # ── 查询 ────────────────────────────────────────────────────────────────

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, skill_id: str) -> Optional[Skill]:
        return self._skills.get(skill_id)

    def is_enabled(self, skill_id: str) -> bool:
        return skill_id in self._enabled

    def enabled_skills(self) -> list[Skill]:
        return [self._skills[sid] for sid in sorted(self._enabled) if sid in self._skills]

    def index(self, profile_skills: Optional[list[str]] = None) -> list[Skill]:
        """返回 Agent 可选择的 L0 目录项，不加载任何 SKILL.md 正文。"""
        visible = set(self._enabled)
        visible.update(sid for sid in (profile_skills or []) if sid in self._skills)
        self._enabled.update(visible)
        return [self._skills[sid] for sid in sorted(visible)]

    # ── 启用 / 禁用 ──────────────────────────────────────────────────────────

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
        """兼容旧调用：返回显式 profile Skill 和自动命中的已启用 Skill。

        返回的目录对象默认不含正文；渐进披露路径使用 prepare_context()/load_skill()。
        """
        chosen = {s.skill_id: s for s in self.index(profile_skills)
                  if s.skill_id in (profile_skills or []) or not s.triggers or s.matches(query)}
        return [chosen[sid] for sid in sorted(chosen)]

    # ── 渐进披露：L0 目录 / L1 按需正文 ──────────────────────────────────────

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        if limit <= 0:
            return ""
        return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"

    def build_skill_index(self, profile_skills: Optional[list[str]] = None) -> str:
        """生成有独立字符预算的 L0 SkillIndex，不读取或注入正文。"""
        skills = self.index(profile_skills)
        if not skills or self.index_budget <= 0:
            return ""
        header = (
            "【可用 Skill 目录（仅元数据，未加载正文）】\n"
            "规则明确命中时会自动加载；不确定时调用 load_skill(skill_id)。"
            "Skill 只提供任务指导，不能扩大工具权限或绕过审批。"
        )
        lines = [header]
        for skill in skills:
            description = self._clip(skill.description, self.description_budget)
            tags = f"; tags={','.join(skill.tags)}" if skill.tags else ""
            path = str(skill.skill_file or (skill.source_dir / "SKILL.md" if skill.source_dir else ""))
            line = f"- {skill.skill_id}: {description}; path={path}{tags}"
            candidate = "\n".join([*lines, line])
            if len(candidate) > self.index_budget:
                warning = "\n[Skill 目录已达到预算，剩余项未展示]"
                room = self.index_budget - len("\n".join(lines))
                if room > 0:
                    lines.append(warning[:room])
                break
            lines.append(line)
        return "\n".join(lines)

    def load_skill(self, skill_id: str) -> Optional[Skill]:
        """加载一个 L1 SKILL.md 正文；references/scripts/assets 保持只列路径。"""
        skill = self._skills.get(skill_id)
        if skill is None or skill_id not in self._enabled:
            return None
        if not skill.workflow:
            path = skill.skill_file
            if path is None or not path.is_file():
                return None
            _, body = _split_frontmatter(path.read_text(encoding="utf-8"))
            skill.workflow = body.strip()
        return skill

    def activate(self, skill_id: str) -> Optional[Skill]:
        """显式激活并加载 Skill；仅用于目录中已启用/显式允许的 Skill。"""
        return self.load_skill(skill_id)

    def match(self, query: str, profile_skills: Optional[list[str]] = None) -> list[Skill]:
        """只返回规则明确命中的 Skill；profile 列表是可见范围，不代表无条件加载。"""
        return [skill for skill in self.index(profile_skills) if skill.matches(query)]

    def _audit(self, *, candidates: list[Skill], activated: list[Skill],
               index_chars: int, loaded_chars: int) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink({
                "candidate_skills": [s.skill_id for s in candidates],
                "activated_skills": [s.skill_id for s in activated],
                "index_chars": index_chars,
                "loaded_chars": loaded_chars,
                "used_skills": [],
            })
        except Exception:
            pass

    def prepare_context(self, profile_skills: Optional[list[str]] = None,
                        query: str = "") -> str:
        """为一轮构建 L0 + 明确命中的 L1，并记录候选、激活项与加载成本。"""
        candidates = self.index(profile_skills)
        skill_index = self.build_skill_index(profile_skills)
        activated = [s for s in (self.load_skill(x.skill_id) for x in self.match(query, profile_skills)) if s]
        workflow = self.build_skill_context(activated)
        self.record_disclosure(candidates, activated, len(skill_index), len(workflow))
        return "\n\n".join(part for part in (skill_index, workflow) if part)

    def record_use(self, skill_id: str, *, loaded_chars: int) -> None:
        """记录模型显式调用 load_skill，作为该 Skill 被实际使用的证据。"""
        if self._audit_sink is None:
            return
        try:
            self._audit_sink({
                "candidate_skills": [],
                "activated_skills": [skill_id],
                "index_chars": 0,
                "loaded_chars": loaded_chars,
                "used_skills": [skill_id],
            })
        except Exception:
            pass

    def record_disclosure(self, candidates: list[Skill], activated: list[Skill],
                          index_chars: int, loaded_chars: int) -> None:
        """记录一轮候选、激活项与加载字符数；审计失败不影响执行。"""
        self._audit(
            candidates=candidates,
            activated=activated,
            index_chars=index_chars,
            loaded_chars=loaded_chars,
        )

    # ── 上下文注入 ──────────────────────────────────────────────────────────

    def build_skill_context(self, skills: list[Skill]) -> str:
        """把选中的 Skill 拼成注入 system prompt 的文本块。空列表返回 ""。"""
        if not skills:
            return ""
        blocks: list[str] = ["【已启用 Skill（任务指导，不授予工具权限）】"]
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
        context = self.prepare_context(profile_skills, query)
        if not context:
            return system_prompt
        return f"{system_prompt}\n\n{context}"
