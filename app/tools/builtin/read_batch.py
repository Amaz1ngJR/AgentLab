"""Multi-file/range reads with one JSON envelope below the runtime truncation limit."""
import json
from app.tools.registry import Tool, ToolExecutionError
from app.tools.builtin.files import _read_page, _resolve_within_workspace, WorkspacePathError

MAX_OUTPUT = 9000

def read_files(args):
    requests = args["requests"]
    # No mixed-authority batch: external reads remain explicit read_file approvals.
    for item in requests:
        try:
            _resolve_within_workspace(item["path"])
        except (WorkspacePathError, OSError, RuntimeError) as exc:
            raise ToolExecutionError(str(exc)) from exc
    result = {"files": [], "pending_indices": []}
    def size():
        return len(json.dumps(result, ensure_ascii=False))
    for index, item in enumerate(requests):
        remaining = MAX_OUTPUT - size()
        if remaining < 700 + len(json.dumps(item["path"], ensure_ascii=False)):
            result["pending_indices"].extend(range(index, len(requests)))
            break
        allowance = max(256, min(8000, (remaining - 500) // max(1, len(requests) - index)))
        entry = {"index": index, "path": item["path"]}
        try:
            _, page = _read_page(item, max_chars=allowance)
            entry.update(page)
            if not page["content"] and page["truncated"]:
                entry["note"] = "本项分配预算不足以容纳下一行，请单独使用 read_file 续读。"
        except (OSError, ToolExecutionError) as exc:
            entry["error"] = str(exc)[:300]
        result["files"].append(entry)
        # JSON escaping/path labels also count toward the shared output budget.
        while size() > MAX_OUTPUT - 100 and entry.get("content"):
            lines = entry["content"].splitlines(keepends=True)
            entry["content"] = "".join(lines[:-1])
            entry["end_line"] = entry["start_line"] + len(lines) - 2
            entry["next_offset"] = entry["end_line"] + 1
            entry["truncated"] = True
        if size() > MAX_OUTPUT - 100:
            result["files"].pop()
            result["pending_indices"].extend(range(index, len(requests)))
            break
    return json.dumps(result, ensure_ascii=False)

READ_FILES = Tool(
    "read_files", "批量读取工作区内多个文件或同文件多个行段，一次调用返回带路径和行号的结果。"
    "requests 最多12项，每项 path/offset/limit（默认200行）；总输出最多9000字符，共享预算。"
    "每项 next_offset 为续读起始行，pending_indices 是尚未返回的请求索引。外部路径用 read_file 单独审批。",
    {"type": "object", "properties": {"requests": {"type": "array", "minItems": 1, "maxItems": 12,
        "items": {"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 2048},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000}},
            "required": ["path"], "additionalProperties": False}}},
     "required": ["requests"], "additionalProperties": False},
    read_files, risk="read", target_type="filesystem", scope="workspace", parallel_safe=True,
    requires_approval=False, capabilities=frozenset({"inspect", "filesystem_read"}),
    audit_redactor=lambda args, result: (json.dumps(args, ensure_ascii=False), f"returned_chars={len(result)}"),
)

READ_GUIDANCE = """【高效读取】
先规划已知需要的文件和行段；独立读取放在同一轮工具调用中，或在提供 read_files 工具时一次提交多个路径/行段。
只有下一步依赖当前结果时才逐次读取。先 code_search 定位再读相关区域，避免从头逐页扫描整个项目。
read_files 有共享输出预算，检查各项 next_offset、truncated 和 pending_indices；没有返回的内容不能假设已读。
"""
