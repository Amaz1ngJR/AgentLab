from pathlib import Path
import pytest
from app.config.loader import use_workspace_root
from app.tools.builtin.patch import apply_patch, preview_patch, APPLY_PATCH
from app.tools.registry import ToolRegistry, ToolExecutionError


def patch(body):
    return {"patch": "*** Begin Patch\n" + body + "\n*** End Patch"}


def test_batch_add_update_multiple_hunks_move_and_delete(tmp_path):
    (tmp_path / "a.py").write_bytes(b"first\r\nkeep\r\nlast\r\n")
    (tmp_path / "remove.txt").write_text("gone")
    args = patch("*** Update File: a.py\n*** Move to: moved.py\n@@\n-first\n+FIRST\n@@\n-last\n+LAST\n*** Add File: new.py\n+hello\n*** Delete File: remove.txt")
    with use_workspace_root(tmp_path):
        assert "FIRST" in preview_patch(args)
        result = apply_patch(args)
    assert "preflight" in result and "write" in result
    assert (tmp_path / "moved.py").read_bytes() == b"FIRST\r\nkeep\r\nLAST\r\n"
    assert not (tmp_path / "a.py").exists() and not (tmp_path / "remove.txt").exists()
    assert (tmp_path / "new.py").read_text() == "hello\n"


def test_late_invalid_hunk_prevents_all_writes(tmp_path):
    (tmp_path / "a").write_text("old\n")
    with use_workspace_root(tmp_path), pytest.raises(ToolExecutionError, match="context not found"):
        apply_patch(patch("*** Add File: new\n+new\n*** Update File: a\n@@\n-missing\n+new"))
    assert not (tmp_path / "new").exists()
    assert (tmp_path / "a").read_text() == "old\n"


def test_context_tolerance_preserves_untouched_indentation(tmp_path):
    (tmp_path / "a").write_text("    anchor  \n    old  \n")
    with use_workspace_root(tmp_path):
        apply_patch(patch("*** Update File: a\n@@\n anchor\n-old\n+    new"))
    assert (tmp_path / "a").read_text() == "    anchor  \n    new\n"


def test_ambiguous_context_requires_anchor(tmp_path):
    (tmp_path / "a").write_text("one\nold\ntwo\nold\n")
    with use_workspace_root(tmp_path):
        with pytest.raises(ToolExecutionError, match="ambiguous"):
            apply_patch(patch("*** Update File: a\n@@\n-old\n+new"))
        apply_patch(patch("*** Update File: a\n@@ two\n-old\n+new"))
    assert (tmp_path / "a").read_text() == "one\nold\ntwo\nnew\n"


@pytest.mark.parametrize("target", ["../outside", ".git/config"])
def test_forbidden_paths_preflight_entire_batch(tmp_path, target):
    with use_workspace_root(tmp_path), pytest.raises(ToolExecutionError):
        apply_patch(patch(f"*** Add File: good\n+ok\n*** Add File: {target}\n+bad"))
    assert not (tmp_path / "good").exists()


def test_write_failure_rolls_back_completed_files(tmp_path, monkeypatch):
    from app.tools.builtin import patch as module
    original = module.atomic_write_text
    def write(path, value, **kwargs):
        if path.name == "b":
            raise OSError("disk failure")
        original(path, value, **kwargs)
    monkeypatch.setattr(module, "atomic_write_text", write)
    (tmp_path / "a").write_text("old\n")
    with use_workspace_root(tmp_path), pytest.raises(ToolExecutionError, match="rolled back"):
        apply_patch(patch("*** Update File: a\n@@\n-old\n+new\n*** Add File: b\n+hello"))
    assert (tmp_path / "a").read_text() == "old\n" and not (tmp_path / "b").exists()


def test_child_patch_checks_move_destination_before_writing(tmp_path):
    from app.agent.subagent_runtime import RestrictedToolRegistry
    tools = ToolRegistry()
    tools.register(APPLY_PATCH)
    child = RestrictedToolRegistry(tools, {"apply_patch"}, workspace=tmp_path, file_scope=("allowed",))
    (tmp_path / "allowed").write_text("old\n")
    with use_workspace_root(tmp_path):
        result, error = child.execute("apply_patch", patch("*** Update File: allowed\n*** Move to: forbidden\n@@\n-old\n+new"), approved_action="apply_patch")
    assert error and "scope" in result
    assert (tmp_path / "allowed").read_text() == "old\n" and not (tmp_path / "forbidden").exists()


def test_one_approval_previews_all_files(tmp_path, monkeypatch):
    from app.agent.approval import InteractivePolicy
    seen = []
    monkeypatch.setattr("app.util.menu.select_menu", lambda **kwargs: seen.append(kwargs) or "yes")
    args = patch("*** Add File: a\n+one\n*** Add File: b\n+two")
    with use_workspace_root(tmp_path):
        assert InteractivePolicy().request_tool(APPLY_PATCH, "apply_patch", args).approved
    assert len(seen) == 1
    text = "\n".join(seen[0]["header_lines"])
    assert "+one" in text and "+two" in text



def test_mixed_line_endings_and_move_permissions_are_preserved(tmp_path):
    (tmp_path / "a").write_bytes(b"old\r\nkeep\nlast\r\n")
    (tmp_path / "a").chmod(0o600)
    with use_workspace_root(tmp_path):
        apply_patch(patch("*** Update File: a\n*** Move to: b\n@@\n-old\n+new"))
    assert (tmp_path / "b").read_bytes() == b"new\r\nkeep\nlast\r\n"
    assert (tmp_path / "b").stat().st_mode & 0o777 == 0o600


def test_session_edits_multiple_files_with_one_tool_call_and_one_approval(tmp_path, monkeypatch):
    from app.agent.runtime import AgentSession
    from app.agent.approval import InteractivePolicy
    from app.models.protocol import ModelResponse, ToolCall
    from tests.support.delegation import FakeRouter, response
    menus = []
    monkeypatch.setattr("app.util.menu.select_menu", lambda **kwargs: menus.append(kwargs) or "yes")
    args = patch("*** Add File: a\n+one\n*** Add File: b\n+two")
    model = FakeRouter([ModelResponse("", [ToolCall("batch", "apply_patch", args)], {}, []), response("done")])
    tools = ToolRegistry()
    tools.register(APPLY_PATCH)
    session = AgentSession(model, tools, approval=InteractivePolicy(), system_prompt="自定义角色", max_steps=3)
    with use_workspace_root(tmp_path):
        assert session.chat("继续") == "done"
    assert len(menus) == 1 and len(model.calls) == 2
    assert "批量文件编辑" in model.calls[0]["system"]
    assert (tmp_path / "a").read_text() == "one\n" and (tmp_path / "b").read_text() == "two\n"


def test_invalid_patch_preview_does_not_crash_approval(monkeypatch, tmp_path):
    from app.agent.approval import InteractivePolicy
    menus = []
    monkeypatch.setattr("app.util.menu.select_menu", lambda **kwargs: menus.append(kwargs) or "no")
    with use_workspace_root(tmp_path):
        result = InteractivePolicy().request_tool(APPLY_PATCH, "apply_patch", patch("*** Delete File: missing"))
    assert not result.approved
    assert "预检失败" in "\n".join(menus[0]["header_lines"])
