"""Recover tool evidence without granting general external-file access."""
from app.tools.output_archive import read_output
from app.tools.output_budget import OUTPUT_TOKEN_LIMIT_SCHEMA
from app.tools.registry import Tool

READ_TOOL_OUTPUT = Tool(
    name="read_tool_output",
    description=("按输出 ID 读取工具留存的完整输出，无需重新执行命令。"
                 "offset 从第1行开始，limit 默认80行；超长行用 column_offset 续读。"
                 "只读取当前工作区缓存，过期或容量淘汰后不可用。"),
    input_schema={
        "type": "object",
        "properties": {
            "output_id": {"type": "string", "pattern": "^out_[0-9a-f]{64}$"},
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            "column_offset": {"type": "integer", "minimum": 0},
            "max_output_tokens": OUTPUT_TOKEN_LIMIT_SCHEMA,
        },
        "required": ["output_id"],
        "additionalProperties": False,
    },
    executor=read_output,
    requires_approval=False,
    scope="workspace",
    parallel_safe=True,
)
