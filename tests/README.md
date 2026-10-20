# 测试分层

本仓库的默认测试是**离线、确定性测试**。使用 FakeRouter 或 mock 模型不代表一定是单元测试：如果实际连接了多个运行时组件、SQLite 或 Git，它仍然可以验证真实集成边界。

## 目录

```text
tests/
├── conftest.py                 # 按目录自动添加 unit / integration marker
├── support/                    # 共享数据、模型替身和构造器；不放测试用例
│   ├── context.py
│   ├── delegation.py
│   └── runtime.py
├── unit/                       # 单模块规则、组件契约、受控替身下的行为回归
└── integration/                # 多组件工作流及真实本地依赖
    ├── conftest.py             # 临时 Git 仓库 fixture
    ├── test_agent_manager.py   # 委派、并发、预算与结果交付
    ├── test_agent_delivery.py  # 真实 worktree、验证、审批与补丁集成
    ├── test_multiagent_handoff.py
    ├── test_subagents.py
    ├── test_subagent_runtime.py
    ├── test_loop_integration.py
    ├── test_loop_evidence.py
    ├── test_session_router.py  # SQLite 重启、检查点事务与回执
    ├── test_session_attachments.py
    ├── test_cli_session_enhancements.py
    ├── test_runtime_service_storage.py
    ├── test_protocol_storage.py
    ├── test_context_storage.py
    ├── test_memory_storage.py
    ├── test_storage.py
    └── test_worktree.py
```

## 如何归类

- **unit**：重点验证某个组件的输入输出、状态变化或协议契约；例如计划解析、权限判断、参数校验、事件序列化、provider 响应解析。允许用临时文件或 fake 验证局部行为。
- **integration**：重点验证组件之间是否正确连接，例如父子 Agent 消费结果、Loop 调用编排器、Service → SessionRouter → SQLite 持久化，以及真实 Git worktree 写入交付。
- **混合文件**：优先拆开纯逻辑与集成场景。本次已拆开 execution_plan、protocol、memory、context 的存储场景及 runtime_service 的持久化场景。以工作流为主的集成文件允许保留少量紧密相关的边界用例，不为每条断言再建目录。
- **support**：共享替身放这里，不新增 `from tests.unit.test_x import ...` 或跨测试层导入。作用域相关的 pytest fixture 放相应层的 `conftest.py`。

本轮是现有用例的分层整理，不宣称 `unit/` 已经全部变成纯函数测试：例如 shell/PTY 工具回归仍可能启动本地进程，部分 runtime/context 文件仍保留组件协作测试。后续新增跨层工作流应放入 integration，而不是继续扩大 unit 中的混合文件。

## 运行

在仓库根目录执行（Python 3.11+，安装项目依赖和 pytest）：

```bash
python -m pip install -e .
python -m pip install pytest

# 默认运行两层，不隐式跳过集成测试
python -m pytest -q

# 分层运行
python -m pytest tests/unit -q
python -m pytest tests/integration -q

# 等价的 marker 选择
python -m pytest -m unit -q
python -m pytest -m integration -q

# 查看收集结果，或排查耗时
python -m pytest --collect-only -q
python -m pytest --durations=15 -q
```

集成测试需要本机可用的 Git；数据和仓库应位于 pytest 临时目录，不需要真实模型凭据、线上数据库或浏览器登录态。缺少 Git 属于环境未就绪，不应把相关测试静默算作通过。现有 MCP 三平台 CI 专项路径不变；完整回归应运行 `python -m pytest tests -q`，不能只跑 unit。

## 覆盖边界

现有集成测试覆盖本地组件交接、持久化、失败/取消、并发及 Git 写入交付，但**不等于线上端到端验证**。真实 provider 流式网络、真实 MCP server / 浏览器交互、原生终端键盘操作和平台差异仍需要专门的 smoke/E2E 验证；不要仅创建空目录或替换全部组件后就称其为 E2E。
