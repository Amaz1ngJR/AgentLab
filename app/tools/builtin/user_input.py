"""Ask the user a question without granting permission to execute an action."""
import json

from app.agent.user_questions import QUESTION_ACTION, validate_questions
from app.tools.registry import Tool, ToolExecutionError, current_cancel_token


def make_request_user_input_tool(ask) -> Tool:
    identifier = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,40}$"}
    label = {"type": "string", "minLength": 1, "maxLength": 120}
    option = {
        "type": "object", "required": ["id", "label"], "additionalProperties": False,
        "properties": {
            "id": identifier, "label": label,
            "description": {"type": "string", "maxLength": 250},
        },
    }
    question = {
        "type": "object", "required": ["id", "question"], "additionalProperties": False,
        "properties": {
            "id": identifier,
            "question": {"type": "string", "minLength": 1, "maxLength": 500},
            "options": {"type": "array", "maxItems": 6, "items": option},
            "allow_free_text": {"type": "boolean"},
        },
    }

    def request(args):
        questions = args["questions"]
        try:
            validate_questions(questions)
        except ValueError as exc:
            raise ToolExecutionError(str(exc)) from exc
        result = ask(questions, current_cancel_token())
        if result.answers is None:
            return json.dumps({"status": "unanswered"}, ensure_ascii=False)
        return json.dumps({"status": "answered", "answers": result.answers}, ensure_ascii=False)

    return Tool(
        name=QUESTION_ACTION,
        description="询问最多三个明确问题，支持单选与自由输入。需要用户补充信息时使用；回答不授予工具执行权限。",
        input_schema={
            "type": "object", "required": ["questions"], "additionalProperties": False,
            "properties": {"questions": {"type": "array", "minItems": 1, "maxItems": 3, "items": question}},
        },
        executor=request, risk="observe", target_type="user", origin="builtin",
        requires_approval=False,
    )
