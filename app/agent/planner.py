"""Planner —— 把用户目标拆成带依赖的初始任务清单(TaskPlan)。

职责(technical_architecture.md §7.1):
  判断任务复杂度,复杂任务拆成可执行子任务,声明依赖,写入 TaskStore。

实现策略:
  让模型只输出一段 JSON(不调用工具),描述子任务及其依赖;解析成 Task 列表。
  解析失败或模型判定为简单任务时,退化为"单任务计划"(content = 原始 goal),
  保证编排路径永远有至少一个任务可跑,绝不因为模型 JSON 不规范而卡死。

只依赖 llm.create_message(...).text,因此离线测试用 FakeRouter 返回 JSON 即可。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.agent.tasks import PENDING, Task

PLANNER_SYSTEM = """你是任务规划器。把用户目标拆成可执行的子任务清单。

关键要求:
1. 只输出一个 JSON 对象,不要有任何额外文字、解释、分析或 markdown 代码块。
2. 不要在 JSON 外添加任何前言、后语、思考过程。
3. 每个子���务的 content 必须是明确的、可执行的动作，而不是"确认"、"检查"、"分析"等模糊指令。

输出格式:
{"tasks": [
  {"id": "t1", "content": "用 read_file 读取 xxx.py 文件内容", "dependencies": []},
  {"id": "t2", "content": "用 edit_file 修改 xxx.py 第 N 行，将 A 改为 B", "dependencies": ["t1"]}
]}

规则:
- id 用 t1/t2/t3... 顺序编号,稳定唯一。
- content 是一句话祈使句,描述这一步要做什么。必须包含具体的工具调用（如 read_file、write_file、edit_file、shell 等）。
- dependencies 列出必须先完成的子任务 id;没有依赖就写 []。
- 简单目标(一步能完成)就只给一个任务。
- 不要拆得过细,通常 2-5 个任务即可。
- 避免"确认"、"检查上下文"、"分析"等空泛任务，直接写具体操作。
"""


@dataclass
class TaskPlan:
    """Planner 产出的初始计划。tasks 顺序即建议执行顺序(实际按依赖 claim)。"""
    goal: str
    tasks: list[Task] = field(default_factory=list)


def _extract_json(text: str) -> dict | None:
    """从模型输出里抠出第一个 JSON 对象。

    容忍三种常见污染:① markdown ```json 围栏;② JSON 前后有解释文字;
    ③ 纯 JSON。抠不出合法对象时返回 None。
    """
    if not text:
        return None
    # 去掉 ```json ... ``` 围栏
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        # 退而求其次:抓第一个 { 到最后一个 } 的跨度
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
    if candidate is None:
        return None
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


def _parse_tasks(obj: dict) -> list[Task]:
    """把 {"tasks": [...]} 解析成 Task 列表。非法项跳过;全非法则返回空。"""
    raw = obj.get("tasks")
    if not isinstance(raw, list):
        return []
    tasks: list[Task] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        tid = str(item.get("id") or f"t{i + 1}").strip() or f"t{i + 1}"
        if tid in seen:  # id 撞车就重新编号,保证唯一
            tid = f"t{i + 1}"
        seen.add(tid)
        deps_raw = item.get("dependencies") or []
        deps = [str(d) for d in deps_raw] if isinstance(deps_raw, list) else []
        tasks.append(Task(id=tid, content=content, status=PENDING, dependencies=deps))
    return tasks


class Planner:
    """用模型把目标拆成 TaskPlan;失败时退化为单任务。"""

    def __init__(self, llm, system: str = PLANNER_SYSTEM):
        self._llm = llm
        self._system = system
        # 最近一次规划的 token 用量,供 Orchestrator 汇总进 run 统计
        self.last_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self.last_actual_model: str | None = None

    def create_plan(self, goal: str, context: str = "", *, on_progress=None) -> TaskPlan:
        """生成初始计划。context 可放 workspace、已知约束等补充信息。

        on_progress 是可选的 token 进度回调(签名同 create_message 的 on_progress),
        让 CLI spinner 在规划阶段也能实时刷新计数。
        """
        self.last_usage = {"input_tokens": 0, "output_tokens": 0}
        prompt = goal if not context else f"{context}\n\n目标:{goal}"
        try:
            resp = self._llm.create_message(
                messages=[{"role": "user", "content": prompt}],
                tools=None,
                system=self._system,
                on_progress=on_progress,
            )
            for k in ("input_tokens", "output_tokens"):
                self.last_usage[k] = resp.usage.get(k, 0) if getattr(resp, "usage", None) else 0
            if getattr(resp, "actual_model", None):
                self.last_actual_model = resp.actual_model
            obj = _extract_json(getattr(resp, "text", "") or "")
            tasks = _parse_tasks(obj) if obj else []
        except Exception:
            tasks = []
        if not tasks:
            # 兜底:单任务计划。用简短标题而非整段 goal(避免面板被长问题刷屏)
            summary = goal.strip()[:50].rstrip() + ("…" if len(goal.strip()) > 50 else "")
            tasks = [Task(id="t1", content=summary or "完成用户请求", status=PENDING)]
        return TaskPlan(goal=goal, tasks=tasks)
