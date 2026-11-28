"""Multi-file/range reads with one JSON envelope below the runtime truncation limit."""
import json
from app.agent.context_budget import estimate_tokens
from app.tools.registry import Tool, ToolExecutionError
from app.tools.builtin.files import _read_page, _resolve_within_workspace, WorkspacePathError
from app.tools.output_budget import TOOL_OUTPUT_MAX_TOKENS, TOOL_PAGE_MAX_TOKENS, fits_output

MAX_OUTPUT = 9000

def read_files(args):
    requests = args["requests"]
    # No mixed-authority batch: external reads remain explicit read_file approvals.
    for item in requests:
        try:
            _resolve_within_workspace(item["path"])
        except (WorkspacePathError, OSError, RuntimeError) as exc:
            raise ToolExecutionError(str(exc)) from exc
    result = {"files": [], "pending_indices": list(range(len(requests)))}

    def serialize():
        return json.dumps(result, ensure_ascii=False)

    for index, item in enumerate(requests):
        if not result["pending_indices"] or result["pending_indices"][0] != index:
            continue
        entry = {"index": index, "path": item["path"]}
        result["files"].append(entry)
        result["pending_indices"].pop(0)
        # Count the serialized path and pending indices; reserve space for page metadata.
        envelope = serialize()
        remaining_chars = MAX_OUTPUT - len(envelope) - 200
        remaining_tokens = TOOL_OUTPUT_MAX_TOKENS - estimate_tokens(envelope) - 100
        if remaining_chars < 6 or remaining_tokens < 3:
            result["files"].pop()
            result["pending_indices"].insert(0, index)
            break
        left = len(requests) - index
        max_chars = min(8000, remaining_chars, max(256, remaining_chars // left))
        max_tokens = min(TOOL_PAGE_MAX_TOKENS, remaining_tokens, max(32, remaining_tokens // left))
        try:
            read_args, covered = _merge_ranges(requests, index)
            _, page = _read_page(read_args, max_chars=max_chars, max_tokens=max_tokens, json_content=True)
            if covered and (not page["truncated"] or (
                    page["next_column"] is None
                    and page["end_line"] >= read_args["offset"] + read_args["limit"] - 1)):
                entry["covered_indices"] = covered
                del result["pending_indices"][:len(covered)]
            elif covered:
                _, page = _read_page(item, max_chars=max_chars, max_tokens=max_tokens, json_content=True)
            entry.update(page)
        except (OSError, ToolExecutionError) as exc:
            entry["error"] = str(exc)[:300]
        if not fits_output(serialize(), MAX_OUTPUT):
            result["files"].pop()
            result["pending_indices"][:0] = [index, *entry.get("covered_indices", [])]
            break
    return serialize()


def _merge_ranges(requests, index):
    item = requests[index]
    start = item.get("offset", 1)
    end = start + item.get("limit", 80)
    covered = []
    if item.get("column_offset"):
        return item, covered
    for pos in range(index + 1, len(requests)):
        candidate = requests[pos]
        offset = candidate.get("offset", 1)
        candidate_end = offset + candidate.get("limit", 80)
        if (candidate["path"] != item["path"] or candidate.get("column_offset")
                or not start <= offset <= end or max(end, candidate_end) - start > 2000):
            break
        end = max(end, candidate_end)
        covered.append(pos)
    return ({**item, "offset": start, "limit": end - start} if covered else item), covered

READ_FILES = Tool(
    "read_files", "批量读取工作区内多个文件或同文件多个行段，一次调用返回带路径和行号的结果。"
    "requests 最多12项，每项 path/offset/limit（默认80行）；总输出受字符和 token 共享预算限制。"
    "相邻或重叠的连续请求可合并，covered_indices 列出同页已覆盖的其他请求。"
    "按 next_offset/next_column 续读，pending_indices 是尚未返回的请求索引。外部路径用 read_file 单独审批。",
    {"type": "object", "properties": {"requests": {"type": "array", "minItems": 1, "maxItems": 12,
        "items": {"type": "object", "properties": {
            "path": {"type": "string", "minLength": 1, "maxLength": 2048},
            "offset": {"type": "integer", "minimum": 1},
            "column_offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000}},
            "required": ["path"], "additionalProperties": False}}},
     "required": ["requests"], "additionalProperties": False},
    read_files, risk="read", target_type="filesystem", scope="workspace", parallel_safe=True,
    requires_approval=False, capabilities=frozenset({"inspect", "filesystem_read"}),
    audit_redactor=lambda args, result: (json.dumps(args, ensure_ascii=False), f"returned_chars={len(result)}"),
)

READ_GUIDANCE = """【高效读取】
先规划已知需要的文件和行段；独立读取放在同一轮工具调用中，或在提供 read_files 工具时一次提交多个路径/行段。
默认每页80行；优先 code_search 精确定位并显式填写 offset/limit。超长行按 next_column/column_offset 续读，不丢弃中间字符。
合并同一文件重叠的行段；文件未改动时复用已有结果，仅补读尚未覆盖的范围。搜索先限定目录、文件类型和少量命中。
检查已有改动时，先读 diff 统计和涉及的行段，将互不依赖的读取放进同一批。
只有下一步依赖当前结果时才逐次读取。先 code_search 定位再读相关区域，避免从头逐页扫描整个项目。
read_files 有共享输出预算，检查各项 next_offset、truncated 和 pending_indices；没有返回的内容不能假设已读。
"""
