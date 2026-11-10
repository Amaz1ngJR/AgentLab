"""Explicit user decisions routed through the existing approval broker/menu."""
import json
from app.tools.registry import Tool

CONFIRMATION_GUIDANCE = """【交互确认】
需要用户批准具体方案或决定下一步时，必须调用 request_confirmation，不能仅输出“是否同意”后结束。
调用前说明实际方案、影响和可选替代方式；用户同意后再执行真实操作。普通工具已有审批且用户已授权时不重复确认。
关闭/重启用户正在使用的浏览器等中断性操作，必须明确说明影响后用该工具确认。
自动执行不等于用户已回答新的确认问题；拒绝后调整方案，选择修改方案后等待用户补充。"""

REQUEST_CONFIRMATION = Tool(
    "request_confirmation",
    "需要用户对具体方案作决定时调用，显示同意、拒绝、修改方案、完全托管自动执行四个选项。"
    "不要只在普通回复里询问同意。此工具仅记录决定，不执行方案；同意后再调用实际工具。",
    {"type": "object", "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 500},
        "details": {"type": "string", "minLength": 1, "maxLength": 4000,
                    "description": "将执行的具体操作、影响和可选替代方案"},
    }, "required": ["summary", "details"], "additionalProperties": False},
    lambda args: json.dumps({"decision": "approved", "summary": args["summary"],
                            "message": "用户已同意该方案；请执行实际操作，尚未执行任何方案步骤。"}, ensure_ascii=False),
    risk="observe", requires_approval=True,
)
