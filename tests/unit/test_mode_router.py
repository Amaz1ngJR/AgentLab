"""Direct/Task/Loop 模式选择与 Planner 绕过测试。"""
from app.agent.mode_router import ExecutionMode, ModeRouter, SessionState


def test_simple_questions_use_direct_mode():
    assert ModeRouter.select("解释一下这个函数") is ExecutionMode.DIRECT
    assert ModeRouter.select("读取一个文件") is ExecutionMode.DIRECT


def test_explicit_multi_step_actions_use_task_mode():
    assert ModeRouter.select("修改多个文件并运行测试") is ExecutionMode.TASK
    assert ModeRouter.select("refactor and test the module") is ExecutionMode.TASK


def test_success_criteria_use_loop_mode():
    assert ModeRouter.select("实现功能，验收标准是所有测试通过") is ExecutionMode.LOOP
    assert ModeRouter.select("/loop start goal-1") is ExecutionMode.LOOP


def test_resume_only_uses_task_when_open_tasks_exist():
    assert ModeRouter.select(
        "继续上一轮未完成的任务",
        session_state=SessionState(has_open_tasks=True),
    ) is ExecutionMode.TASK
    assert ModeRouter.select(
        "继续上一轮未完成的任务",
        session_state=SessionState(has_open_tasks=False),
    ) is ExecutionMode.DIRECT


def test_disabled_orchestrator_always_uses_direct():
    assert ModeRouter.select(
        "修改多个文件并运行测试",
        session_state={"orchestrate_enabled": False},
    ) is ExecutionMode.DIRECT


def test_multiple_attachments_and_action_use_task_mode():
    assert ModeRouter.select("整理这些文件", attachments=[1, 2]) is ExecutionMode.TASK


# ── 边界情况与回归测试 ────────────────────────────────────────────────────────


def test_empty_or_none_input_defaults_to_direct():
    """空输入或 None 应回退到 Direct 模式。"""
    assert ModeRouter.select("") is ExecutionMode.DIRECT
    assert ModeRouter.select(None) is ExecutionMode.DIRECT
    assert ModeRouter.select("   ") is ExecutionMode.DIRECT


def test_single_attachment_with_question_stays_direct():
    """单张图片问答应保持 Direct，不强制进入 Task。"""
    assert ModeRouter.select("这张图是什么", attachments=["image.png"]) is ExecutionMode.DIRECT
    assert ModeRouter.select("分析这个截图", attachments=[b"data"]) is ExecutionMode.DIRECT


def test_case_insensitive_matching():
    """关键词匹配应忽略大小写。"""
    assert ModeRouter.select("修改多个文件并运行测试") is ExecutionMode.TASK
    assert ModeRouter.select("REFACTOR AND TEST") is ExecutionMode.TASK
    assert ModeRouter.select("/LOOP start") is ExecutionMode.LOOP
    assert ModeRouter.select("Success Criteria: all pass") is ExecutionMode.LOOP


def test_loop_markers_override_task_markers():
    """Loop 标记优先级高于 Task 标记。"""
    assert ModeRouter.select("修改多个文件，验收标准是测试通过") is ExecutionMode.LOOP
    assert ModeRouter.select("refactor with success criteria") is ExecutionMode.LOOP


def test_orchestrate_disabled_overrides_all_markers():
    """orchestrate_enabled=False 时，所有复杂请求都强制 Direct。"""
    state = SessionState(orchestrate_enabled=False)
    assert ModeRouter.select("修改多个文件并运行测试", session_state=state) is ExecutionMode.DIRECT
    assert ModeRouter.select("验收标准是全部通过", session_state=state) is ExecutionMode.DIRECT
    assert ModeRouter.select("/loop start", session_state=state) is ExecutionMode.DIRECT


def test_session_state_dict_compatibility():
    """应支持字典形式的 session_state（向后兼容）。"""
    # "继续" 本身不足以触发 resume，需要更明确的表述
    assert ModeRouter.select(
        "继续上一轮未完成的任务",
        session_state={"has_open_tasks": True, "orchestrate_enabled": True},
    ) is ExecutionMode.TASK
    assert ModeRouter.select(
        "继续上一轮未完成的任务",
        session_state={"open_tasks": False},  # 旧字段名，映射到 has_open_tasks
    ) is ExecutionMode.DIRECT


def test_chinese_and_english_task_markers():
    """中英文任务标记都应被识别。"""
    # _TASK_MARKERS 包含完整的关键词，必须作为子串出现
    assert ModeRouter.select("这个任务先...再做") is ExecutionMode.TASK
    assert ModeRouter.select("先…再处理") is ExecutionMode.TASK
    assert ModeRouter.select("implement and test the feature") is ExecutionMode.TASK
    assert ModeRouter.select("fix and verify the bug") is ExecutionMode.TASK
    assert ModeRouter.select("修改多个文件") is ExecutionMode.TASK
    assert ModeRouter.select("需要多个步骤完成") is ExecutionMode.TASK


def test_plain_conversation_stays_direct():
    """普通对话、解释、单步查询应保持 Direct。"""
    assert ModeRouter.select("你好") is ExecutionMode.DIRECT
    assert ModeRouter.select("请介绍下你自己") is ExecutionMode.DIRECT
    assert ModeRouter.select("这段代码是做什么的") is ExecutionMode.DIRECT
    assert ModeRouter.select("查看 README 文件") is ExecutionMode.DIRECT
    assert ModeRouter.select("搜索 main 函数") is ExecutionMode.DIRECT
    assert ModeRouter.select("解释一下这个错误") is ExecutionMode.DIRECT


def test_url_solution_request_stays_direct_and_bypasses_planner():
    assert ModeRouter.select(
        "请看https://leetcode.cn/problems/example这个题，给出c++实现和最优解"
    ) is ExecutionMode.DIRECT


def test_english_action_words_match_whole_words_only():
    """英文动作词按整词匹配，避免子串误判把普通问答推进 Task。"""
    # "already"/"latest"/"threaded" 分别含 read/test/read 子串，但都不是动作词。
    assert ModeRouter.select(
        "already at the latest version", attachments=[1, 2],
    ) is ExecutionMode.DIRECT
    # 真正的动作词仍然生效。
    assert ModeRouter.select(
        "run the tests", attachments=[1, 2],
    ) is ExecutionMode.TASK


def test_resume_intent_requires_whole_word():
    """resume 按整词匹配，presumed 之类的词不应触发任务恢复。"""
    assert ModeRouter.select(
        "the presumed cause is a typo",
        session_state=SessionState(has_open_tasks=True),
    ) is ExecutionMode.DIRECT
    assert ModeRouter.select(
        "resume the previous work",
        session_state=SessionState(has_open_tasks=True),
    ) is ExecutionMode.TASK


def test_active_goal_with_action_stays_in_loop():
    """验收进行中的目标遇到操作请求应留在 Loop，不掉回 Direct。"""
    state = SessionState(has_active_goal=True)
    assert ModeRouter.select("修复这个失败的测试", session_state=state) is ExecutionMode.LOOP
    # 纯提问不算操作，仍走 Direct。
    assert ModeRouter.select("这个目标现在什么状态", session_state=state) is ExecutionMode.DIRECT


def test_edge_case_whitespace_and_punctuation():
    """包含大量空白或标点的输入应正确处理。"""
    assert ModeRouter.select("  修改多个文件  ") is ExecutionMode.TASK
    # "修改、测试、部署" 没有明确的多步骤连接词（如"并"/"先...再"），不会被识别为 Task
    assert ModeRouter.select("修改并测试并部署") is ExecutionMode.TASK
    assert ModeRouter.select("...实现功能...") is ExecutionMode.DIRECT  # 无明确多步骤标记
