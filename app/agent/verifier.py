"""Verifier —— Loop Engineering 的验证器。

Verifier 是 Loop 模式的核心。没有 Verifier，Agent 只能"声称完成"；有 Verifier，
Agent 才能用证据判断是否达到目标。

当前实现：
  - command: 运行命令（单元测试、lint、typecheck、构建），检查 exit code
  - file_assertion: 检查文件存在、内容包含/不包含

待实现（后续补充）：
  - browser: 打开页面、DOM snapshot、截图、点击后状态
  - api: 调本地或远程 HTTP API 验证行为
  - database_readonly: 只读查询验证迁移或数据状态
  - remote: 在已配置远程 host/workspace 上验证
  - human: 需要用户主观判断或外部系统权限
  - llm_judge: 评估文本质量、总结质量等软指标

PRD §7.6.5
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.agent.goals import VerificationCheck


@dataclass
class CheckResult:
    """单个检查项的结果。"""
    name: str
    status: Literal["pass", "fail", "blocked", "uncertain"]
    evidence_ref: str | None = None  # 工具执行 id 或文件路径
    summary: str = ""
    error: str | None = None


@dataclass
class VerificationResult:
    """验证结果的统一输出。"""
    status: Literal["pass", "fail", "blocked", "uncertain"]
    checks: list[CheckResult] = field(default_factory=list)
    failure_category: str | None = None  # "test_failed" / "env_failed" / "permission_denied" 等
    confidence: float = 1.0  # 0.0-1.0，flaky 检查降低置信度
    next_hint: str | None = None  # 给 Replanner 的提示
    created_at: str | None = None

    def is_success(self) -> bool:
        """所有检查都通过才算成功。"""
        return self.status == "pass" and all(c.status == "pass" for c in self.checks)


class Verifier:
    """验证器：用证据判断目标是否达成。"""

    def __init__(self, workspace_root: str | None = None):
        self.workspace_root = Path(workspace_root) if workspace_root else Path.cwd()

    def verify(self, checks: list[VerificationCheck]) -> VerificationResult:
        """执行验证计划，返回统一结果。

        规则（PRD §7.6.5）：
          - 所有检查项都通过才算 pass
          - 任一 blocked 则整体 blocked
          - 任一 fail 则整体 fail（除非被 uncertain 覆盖）
          - flaky 检查重试后仍失败 → uncertain 或 fail
        """
        if not checks:
            return VerificationResult(
                status="blocked",
                failure_category="no_verifier",
                next_hint="验证计划为空，无法判定目标是否达成",
            )

        results: list[CheckResult] = []
        has_blocked = False
        has_fail = False
        has_uncertain = False

        for check in checks:
            result = self._run_check(check)
            results.append(result)

            if result.status == "blocked":
                has_blocked = True
            elif result.status == "fail":
                has_fail = True
            elif result.status == "uncertain":
                has_uncertain = True

        # 计算整体状态
        if has_blocked:
            overall = "blocked"
            category = "permission_denied"
        elif has_fail:
            overall = "fail"
            category = "test_failed"
        elif has_uncertain:
            overall = "uncertain"
            category = "flaky_check"
        else:
            overall = "pass"
            category = None

        # 失败提示
        hint = None
        if overall != "pass":
            failed = [r for r in results if r.status in ("fail", "blocked", "uncertain")]
            if failed:
                hint = f"检查失败: {', '.join(r.name for r in failed)}"

        return VerificationResult(
            status=overall,
            checks=results,
            failure_category=category,
            confidence=1.0 if not has_uncertain else 0.7,
            next_hint=hint,
        )

    def _run_check(self, check: VerificationCheck) -> CheckResult:
        """执行单个检查项。"""
        if check.type == "command":
            return self._check_command(check)
        elif check.type == "file_assertion":
            return self._check_file_assertion(check)
        elif check.type == "human":
            return CheckResult(
                name=check.description or "人工确认",
                status="blocked",
                summary="需要用户手动确认",
            )
        else:
            # 未实现的类型
            return CheckResult(
                name=check.description or check.type,
                status="blocked",
                summary=f"验证器类型 '{check.type}' 尚未实现",
            )

    def _check_command(self, check: VerificationCheck) -> CheckResult:
        """command 验证器：运行命令，检查 exit code。

        用途：单元测试、lint、typecheck、构建命令
        通过标准：exit code 匹配 expected_exit_code，默认 0
        """
        if not check.command:
            return CheckResult(
                name="command",
                status="blocked",
                summary="command 参数为空",
            )

        name = check.description or f"command: {check.command[:50]}"
        timeout = check.timeout or 30
        expected = check.expected_exit_code

        # 执行命令（简化版，后续可集成到 shell 工具）
        try:
            result = subprocess.run(
                check.command,
                shell=True,
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(
                name=name,
                status="fail",
                summary=f"命令超时（{timeout}s）",
                error="timeout",
            )
        except Exception as exc:
            return CheckResult(
                name=name,
                status="blocked",
                summary=f"执行失败: {exc}",
                error=str(exc),
            )

        # 判断结果
        if result.returncode == expected:
            stdout_preview = result.stdout.strip()[:200] if result.stdout else ""
            return CheckResult(
                name=name,
                status="pass",
                summary=f"exit code {result.returncode}" + (f": {stdout_preview}" if stdout_preview else ""),
                evidence_ref=f"command:{check.command}",
            )
        else:
            stderr_preview = result.stderr.strip()[:200] if result.stderr else ""
            return CheckResult(
                name=name,
                status="fail",
                summary=f"exit code {result.returncode} (expected {expected})",
                error=stderr_preview or result.stdout.strip()[:200],
            )

    def _check_file_assertion(self, check: VerificationCheck) -> CheckResult:
        """file_assertion 验证器：检查文件存在、内容包含/不包含。

        用途：验证文件是否创建、配置是否正确、diff 范围
        """
        if not check.path:
            return CheckResult(
                name="file_assertion",
                status="blocked",
                summary="path 参数为空",
            )

        name = check.description or f"file: {check.path}"
        path = self.workspace_root / check.path

        # 检查存在性
        if check.exists is not None:
            if check.exists and not path.exists():
                return CheckResult(
                    name=name,
                    status="fail",
                    summary=f"文件不存在: {check.path}",
                )
            elif not check.exists and path.exists():
                return CheckResult(
                    name=name,
                    status="fail",
                    summary=f"文件不应存在但存在: {check.path}",
                )

        # 检查内容
        if check.contains or check.not_contains:
            if not path.exists():
                return CheckResult(
                    name=name,
                    status="fail",
                    summary=f"无法检查内容，文件不存在: {check.path}",
                )
            try:
                content = path.read_text(errors="replace")
            except Exception as exc:
                return CheckResult(
                    name=name,
                    status="blocked",
                    summary=f"无法读取文件: {exc}",
                    error=str(exc),
                )

            if check.contains and check.contains not in content:
                return CheckResult(
                    name=name,
                    status="fail",
                    summary=f"文件不包含期望内容: '{check.contains[:50]}'",
                )
            if check.not_contains and check.not_contains in content:
                return CheckResult(
                    name=name,
                    status="fail",
                    summary=f"文件包含不应存在的内容: '{check.not_contains[:50]}'",
                )

        # 所有断言通过
        return CheckResult(
            name=name,
            status="pass",
            summary=f"文件断言通过: {check.path}",
            evidence_ref=str(path),
        )
