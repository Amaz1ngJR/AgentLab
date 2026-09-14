"""按需加载 Skill 正文的模型工具。"""
from __future__ import annotations

import json
from typing import Optional

from app.skills.catalog import SkillCatalog
from app.tools.registry import Tool


def make_load_skill_tool(
    catalog: SkillCatalog,
    profile_skills: Optional[list[str]] = None,
) -> Tool:
    """创建会话级 load_skill；只加载当前 Agent 可见的 Skill。"""
    allowed = {skill.skill_id for skill in catalog.index(profile_skills)}

    def _load(args: dict) -> str:
        skill_id = str(args.get("skill_id", "")).strip()
        if not skill_id or skill_id not in allowed:
            return json.dumps({"error": "unknown or disabled skill", "skill_id": skill_id}, ensure_ascii=False)
        skill = catalog.load_skill(skill_id)
        if skill is None:
            return json.dumps({"error": "skill could not be loaded", "skill_id": skill_id}, ensure_ascii=False)
        resources = {
            "references": [str(path) for path in skill.references],
            "scripts": _resource_paths(skill, "scripts"),
            "assets": _resource_paths(skill, "assets"),
        }
        catalog.record_use(skill.skill_id, loaded_chars=len(skill.workflow))
        return json.dumps({
            "skill_id": skill.skill_id,
            "name": skill.name,
            "description": skill.description,
            "workflow": skill.workflow,
            "resources": resources,
            "note": "资源仅列出路径，按 Skill 步骤需要时再用现有工具读取；本工具不授予额外权限。",
        }, ensure_ascii=False)

    return Tool(
        name="load_skill",
        description=(
            "按需加载一个已启用 Skill 的完整 SKILL.md 工作流。"
            "仅在 Skill 目录无法确定做法或需要完整步骤时调用；references/scripts/assets 不会自动读取。"
            "Skill 不能扩大工具权限或绕过审批。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "skill_id": {"type": "string", "description": "Skill 目录中的稳定标识"},
            },
            "required": ["skill_id"],
        },
        executor=_load,
        risk="read",
        target_type="skill",
        scope="session",
        origin="builtin",
        requires_approval=False,
    )


def _resource_paths(skill, directory: str) -> list[str]:
    if skill.source_dir is None:
        return []
    root = skill.source_dir / directory
    if not root.is_dir():
        return []
    return [str(path) for path in sorted(root.rglob("*")) if path.is_file()]
