import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.tools.output_archive as archive
from app.tools.builtin import default_tools
from app.tools.registry import ToolExecutionError, ToolRegistry


def test_recovery_tool_registration_and_paging(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    raw = "你好\r\n" + "x" * 19000 + "\nlast\n"
    output_id = archive.archive_output(raw)
    registry = ToolRegistry()
    for tool in default_tools():
        registry.register(tool)
    tool = registry.get("read_tool_output")
    assert tool.approval_action({"output_id": output_id}) is None
    for stage in ("inspect", "analysis", "verify"):
        assert "read_tool_output" in {s["name"] for s in registry.schemas_for_stage(stage)}
    offset, column = 1, 0
    recovered = []
    for _ in range(10):
        result, error = registry.execute("read_tool_output", {
            "output_id": output_id, "offset": offset, "column_offset": column,
        })
        assert not error
        _, body = result.split("\n", 1)
        body, marker, footer = body.rpartition("\n[继续使用 read_tool_output:")
        if not marker:
            recovered.append(result.split("\n", 1)[1])
            break
        recovered.append(body)
        offset = int(re.search(r" offset=(\d+)", footer).group(1))
        match = re.search(r" column_offset=(\d+)", footer)
        column = int(match.group(1)) if match else 0
    assert "".join(recovered) == raw
    assert not (tmp_path / ".agentlab").exists()


def test_read_renews_retention_and_cleans_expired(monkeypatch):
    live_id = archive.archive_output("live")
    expired_id = archive.archive_output("expired")
    directory = archive._archive_dir()
    live = directory / (live_id[4:] + ".txt")
    expired = directory / (expired_id[4:] + ".txt")
    now = time.time()
    os.utime(live, (now - 100, now - 100))
    old = now - archive.RETENTION_SECONDS - 1
    os.utime(expired, (old, old))
    assert archive.read_output({"output_id": live_id}).endswith("live")
    assert live.stat().st_mtime >= now
    assert not expired.exists()
    with pytest.raises(ToolExecutionError, match="unavailable"):
        archive.read_output({"output_id": expired_id})


def test_capacity_evicts_oldest_and_rejects_oversized(monkeypatch):
    monkeypatch.setattr(archive, "MAX_CACHE_BYTES", 12)
    first = archive.archive_output("11111")
    first_path = archive._archive_dir() / (first[4:] + ".txt")
    os.utime(first_path, (time.time() - 10,) * 2)
    second = archive.archive_output("22222")
    third = archive.archive_output("33333")
    assert not first_path.exists()
    assert archive.read_output({"output_id": second}).endswith("22222")
    assert archive.read_output({"output_id": third}).endswith("33333")
    assert archive.archive_output("x" * 13) is None
    assert sum(p.stat().st_size for p in archive._archive_dir().glob("*.txt")) <= 12


def test_cache_rejects_paths_and_symlinks(tmp_path):
    for output_id in ("../secret", str(tmp_path), "out_" + "a" * 64 + "/../secret"):
        with pytest.raises(ToolExecutionError, match="invalid output_id"):
            archive.read_output({"output_id": output_id})
    output_id = archive.archive_output("original")
    path = archive._archive_dir() / (output_id[4:] + ".txt")
    secret = tmp_path / "secret"
    secret.write_text("private")
    path.unlink()
    path.symlink_to(secret)
    assert archive.archive_output("original") is None
    with pytest.raises(ToolExecutionError, match="unavailable"):
        archive.read_output({"output_id": output_id})
    assert secret.read_text() == "private"


def test_concurrent_archive_is_atomic_and_deduplicated():
    text = "same output\r\n" * 100
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(archive.archive_output, [text] * 12))
    assert None not in ids and len(set(ids)) == 1
    assert len(list(archive._archive_dir().glob("*.txt"))) == 1
    assert archive.read_output({"output_id": ids[0], "limit": 200}).endswith(text)


def test_cache_failure_does_not_create_fake_reference(tmp_path, monkeypatch):
    root = tmp_path / "not-a-directory"
    root.write_text("occupied")
    monkeypatch.setenv("AGENTLAB_OUTPUT_CACHE_DIR", str(root))
    assert archive.archive_output("output") is None
