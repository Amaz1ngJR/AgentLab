"""离线测试：Skill Loader + Catalog。"""
from app.skills import SkillCatalog, Skill, load_skills, parse_skill
from app.skills.loader import _split_frontmatter, _as_str_list


SKILL_MD = """---
name: code-review
description: Review changes.
allowed_tools: [read_file, list_dir]
optional_mcp_servers: [git]
triggers: [review, 审查]
enabled: true
---

# Workflow

1. Read changed files.
"""


def _write_skill(base, skill_id, content=SKILL_MD, with_ref=False):
    d = base / skill_id
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    if with_ref:
        ref = d / "references"
        ref.mkdir()
        (ref / "checklist.md").write_text("- check", encoding="utf-8")
    return d


# ── frontmatter 解析 ──────────────────────────────────────────────────────────

def test_split_frontmatter_basic():
    meta, body = _split_frontmatter(SKILL_MD)
    assert meta["name"] == "code-review"
    assert meta["enabled"] is True
    assert body.startswith("# Workflow")


def test_split_frontmatter_no_frontmatter():
    meta, body = _split_frontmatter("# Just markdown\ntext")
    assert meta == {}
    assert body == "# Just markdown\ntext"


def test_split_frontmatter_malformed_yaml():
    meta, body = _split_frontmatter("---\n: : bad\n---\nbody")
    # 解析失败时退化为 (空 meta, 全文)，不抛异常
    assert meta == {}


def test_as_str_list_variants():
    assert _as_str_list(None) == []
    assert _as_str_list("x") == ["x"]
    assert _as_str_list(["a", "b"]) == ["a", "b"]
    assert _as_str_list([1, None, 2]) == ["1", "2"]


# ── parse_skill ─────────────────────────────────────────────────────────────

def test_parse_skill_full(tmp_path):
    d = _write_skill(tmp_path, "code-review", with_ref=True)
    skill = parse_skill(d)
    assert skill is not None
    assert skill.skill_id == "code-review"
    assert skill.name == "code-review"
    assert skill.allowed_tools == ["read_file", "list_dir"]
    assert skill.optional_mcp_servers == ["git"]
    assert skill.triggers == ["review", "审查"]
    assert skill.enabled is True
    assert skill.workflow.startswith("# Workflow")
    assert len(skill.references) == 1


def test_parse_skill_missing_skill_md(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert parse_skill(d) is None


def test_parse_skill_no_metadata(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "SKILL.md").write_text("# no frontmatter", encoding="utf-8")
    # 缺 name 和 description → 无效 Skill
    assert parse_skill(d) is None


def test_parse_skill_id_from_dirname_not_frontmatter(tmp_path):
    # skill_id 取目录名，不取 frontmatter name（避免漂移）
    d = _write_skill(tmp_path, "my-dir")
    skill = parse_skill(d)
    assert skill.skill_id == "my-dir"
    assert skill.name == "code-review"


def test_parse_skill_enabled_defaults_false(tmp_path):
    content = "---\nname: x\ndescription: d\n---\nbody"
    d = _write_skill(tmp_path, "x", content=content)
    skill = parse_skill(d)
    # 未知来源 Skill 默认禁用
    assert skill.enabled is False


# ── load_skills ─────────────────────────────────────────────────────────────

def test_load_skills_scans_dirs(tmp_path):
    _write_skill(tmp_path, "code-review")
    _write_skill(tmp_path, "other", content="---\nname: other\ndescription: d\n---\nx")
    skills = load_skills(tmp_path)
    assert set(skills) == {"code-review", "other"}


def test_load_skills_missing_dir_returns_empty(tmp_path):
    assert load_skills(tmp_path / "nope") == {}


def test_load_skills_skips_invalid(tmp_path):
    _write_skill(tmp_path, "good")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("no meta", encoding="utf-8")
    skills = load_skills(tmp_path)
    assert "good" in skills
    assert "bad" not in skills


# ── Skill.matches ───────────────────────────────────────────────────────────

def test_skill_matches_trigger():
    s = Skill(skill_id="x", name="x", triggers=["review", "审查"])
    assert s.matches("帮我做 code review")
    assert s.matches("代码审查一下")
    assert not s.matches("写个函数")


def test_skill_matches_no_triggers_is_false():
    s = Skill(skill_id="x", name="x", triggers=[])
    assert not s.matches("review")


# ── SkillCatalog ────────────────────────────────────────────────────────────

def _catalog(tmp_path):
    _write_skill(tmp_path, "code-review")  # enabled: true, triggers
    _write_skill(tmp_path, "disabled",
                 content="---\nname: disabled\ndescription: d\n---\nbody")  # 默认禁用
    return SkillCatalog.from_dir(tmp_path)


def test_catalog_default_enabled_from_frontmatter(tmp_path):
    cat = _catalog(tmp_path)
    assert cat.is_enabled("code-review")
    assert not cat.is_enabled("disabled")
    assert [s.skill_id for s in cat.enabled_skills()] == ["code-review"]


def test_catalog_enable_disable(tmp_path):
    cat = _catalog(tmp_path)
    assert cat.enable("disabled")
    assert cat.is_enabled("disabled")
    assert cat.disable("disabled")
    assert not cat.is_enabled("disabled")


def test_catalog_enable_unknown_returns_false(tmp_path):
    cat = _catalog(tmp_path)
    assert not cat.enable("nope")


def test_catalog_resolve_profile_skills_always_included(tmp_path):
    cat = _catalog(tmp_path)
    # disabled 默认不启用，但被 profile 显式引用 → 注入
    resolved = cat.resolve(profile_skills=["disabled"], query="")
    assert [s.skill_id for s in resolved] == ["disabled"]


def test_catalog_resolve_enabled_with_trigger_needs_match(tmp_path):
    cat = _catalog(tmp_path)
    # code-review 全局启用但有 triggers → 需命中 query
    assert cat.resolve(query="写个函数") == []
    matched = cat.resolve(query="帮我 review 代码")
    assert [s.skill_id for s in matched] == ["code-review"]


def test_catalog_resolve_enabled_no_trigger_always_included(tmp_path):
    # 全局启用且无 triggers → 始终注入
    _write_skill(tmp_path, "always",
                 content="---\nname: always\ndescription: d\nenabled: true\n---\nbody")
    cat = SkillCatalog.from_dir(tmp_path)
    cat.enable("always")
    resolved = cat.resolve(query="任意")
    assert "always" in [s.skill_id for s in resolved]


def test_catalog_resolve_dedupes(tmp_path):
    cat = _catalog(tmp_path)
    # code-review 既被 profile 引用、又全局启用 → 只出现一次
    resolved = cat.resolve(profile_skills=["code-review"], query="review")
    assert [s.skill_id for s in resolved].count("code-review") == 1


def test_catalog_build_context_includes_workflow_and_guard(tmp_path):
    cat = _catalog(tmp_path)
    skills = cat.resolve(profile_skills=["code-review"], query="")
    ctx = cat.build_skill_context(skills)
    assert "不授予" in ctx                  # 安全声明
    assert "code-review" in ctx
    assert "建议工具" in ctx                 # allowed_tools 标注为需求
    assert "实际可用工具仍以本次会话授权为准" in ctx


def test_catalog_build_context_empty():
    cat = SkillCatalog({})
    assert cat.build_skill_context([]) == ""


def test_catalog_inject_appends(tmp_path):
    cat = _catalog(tmp_path)
    out = cat.inject("你是助手", profile_skills=["code-review"], query="")
    assert out.startswith("你是助手")
    assert "Skill: code-review" in out


def test_catalog_inject_no_skills_unchanged(tmp_path):
    cat = _catalog(tmp_path)
    # 没有 profile skills，且 query 不命中任何 trigger → 原样返回
    assert cat.inject("你是助手", profile_skills=[], query="无关") == "你是助手"
