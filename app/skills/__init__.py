"""Skill 运行时支持：扫描 skills/、解析 SKILL.md、按 AgentProfile 注入工作流上下文。

公开接口：
  Skill / load_skills        - 数据模型与目录扫描（loader）
  SkillCatalog               - 启用状态、按任务解析、上下文注入（catalog）
"""
from app.skills.catalog import SkillCatalog
from app.skills.loader import Skill, load_skills, parse_skill

__all__ = ["Skill", "load_skills", "parse_skill", "SkillCatalog"]
