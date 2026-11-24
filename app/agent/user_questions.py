"""Structured questions and answers shared by CLI and protocol frontends."""
from __future__ import annotations

import sys

from app.agent.approval import ApprovalResult

QUESTION_ACTION = "request_user_input"


def validate_questions(questions: list[dict]) -> None:
    ids = {question["id"] for question in questions}
    if len(ids) != len(questions):
        raise ValueError("question IDs must be unique")
    for question in questions:
        options = question.get("options", [])
        if len({option["id"] for option in options}) != len(options):
            raise ValueError("option IDs must be unique within each question")
        if not options and not question.get("allow_free_text", True):
            raise ValueError("each question must allow a choice or free text")


def normalize_answers(questions: list[dict], answers: dict) -> dict:
    if not isinstance(answers, dict) or answers.keys() != {q["id"] for q in questions}:
        raise ValueError("provide exactly one answer for each question ID")
    normalized = {}
    for question in questions:
        key = question["id"]
        answer = answers[key]
        if isinstance(answer, dict) and answer.keys() == {"option_id"} and any(
            answer["option_id"] == option["id"] for option in question.get("options", [])
        ):
            normalized[key] = {"option_id": answer["option_id"]}
            continue
        if isinstance(answer, dict) and answer.keys() == {"text"} and question.get(
            "allow_free_text", not question.get("options")
        ):
            value = answer["text"]
            if isinstance(value, str) and 0 < len(value.strip()) <= 2000:
                normalized[key] = {"text": value.strip()}
                continue
        raise ValueError(f"invalid answer for question {key}")
    return normalized


def prompt_questions(questions: list[dict]) -> ApprovalResult:
    if not sys.stdin.isatty():
        return ApprovalResult(False, feedback="no interactive input frontend", cancelled=True)
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application.current import create_app_session, get_app_session
    from prompt_toolkit.key_binding import KeyBindings
    from app.util.input_arbiter import cancel_background_run
    from app.util.menu import select_menu

    bindings = KeyBindings()

    @bindings.add("escape")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    def safe(text):
        return "".join(ch for ch in text if ch.isprintable())

    answers = {}
    for question in questions:
        options = question.get("options", [])
        title = safe(question["question"])
        if options:
            choices = [(safe(option["label"]) + (
                " — " + safe(option["description"]) if option.get("description") else ""
            ), ("option_id", option["id"])) for option in options]
            if question.get("allow_free_text", False):
                choices.append(("输入其他答案", ("text", "")))
            selected = select_menu(choices=choices, title=title, header_lines=[])
            if selected is None:
                return ApprovalResult(False, cancelled=True)
            if selected[0] == "option_id":
                answers[question["id"]] = {"option_id": selected[1]}
                continue
        caller = get_app_session()
        try:
            with create_app_session(input=caller.input, output=caller.output):
                value = PromptSession(key_bindings=bindings).prompt(title + " ").strip()
        except (KeyboardInterrupt, EOFError):
            cancel_background_run()
            return ApprovalResult(False, cancelled=True)
        answers[question["id"]] = {"text": value}
    return ApprovalResult(True, answers=normalize_answers(questions, answers))
