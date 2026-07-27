"""测试 /model 命令的功能"""
import os
from unittest.mock import Mock, patch

import pytest

from app.cli import _handle_model_command
from app.config.loader import load_profiles


class TestModelCommand:
    """测试 /model 命令处理"""

    def test_load_profiles_returns_dict(self):
        """测试加载配置返回字典"""
        profiles = load_profiles()
        assert isinstance(profiles, dict)
        # 至少应该有一些配置
        assert len(profiles) > 0

    def test_model_list_command(self):
        """测试 /model list 命令"""
        mock_router = Mock()
        mock_session = Mock()
        mock_llm = Mock()
        mock_llm.profile_name = "test_profile"
        mock_session.llm = mock_llm
        mock_router.current = mock_session

        result = _handle_model_command(mock_router, "/model list")

        # 应该包含配置列表
        assert "可用模型配置" in result
        # 应该显示当前使用的模型
        assert "当前使用" in result or "当前未激活" in result

    def test_model_current_command_with_session(self):
        """测试 /model current 命令（有活跃 session）"""
        mock_router = Mock()
        mock_session = Mock()
        mock_llm = Mock()
        mock_llm.profile_name = "test_profile"
        mock_llm.provider = "ollama"
        mock_llm.model = "test-model"
        mock_llm.base_url = "http://localhost:11434"
        mock_llm.temperature = 0.2
        mock_llm.context_size = 8192

        mock_session.llm = mock_llm
        mock_session.cumulative_usage = {
            "input_tokens": 100,
            "output_tokens": 50
        }
        mock_session.cumulative_seconds = 10.5

        mock_router.current = mock_session

        result = _handle_model_command(mock_router, "/model current")

        # 应该包含配置信息
        assert "当前模型配置" in result
        assert "test_profile" in result
        assert "ollama" in result
        assert "test-model" in result

    def test_model_current_command_without_session(self):
        """测试 /model current 命令（无活跃 session）"""
        mock_router = Mock()
        mock_router.current = None

        result = _handle_model_command(mock_router, "/model current")

        # 应该提示没有活跃 session
        assert "当前无活跃" in result or "无活跃 session" in result

    def test_model_switch_command_valid_profile(self):
        """测试 /model switch 命令（有效的 profile）"""
        mock_router = Mock()

        # 获取一个实际存在的 profile
        profiles = load_profiles()
        valid_profile = list(profiles.keys())[0]

        result = _handle_model_command(mock_router, f"/model switch {valid_profile}")

        # 应该提示设置成功
        assert "已设置 ACTIVE_PROFILE" in result
        assert valid_profile in result
        assert "新建 session" in result

    def test_model_switch_command_invalid_profile(self):
        """测试 /model switch 命令（无效的 profile）"""
        mock_router = Mock()

        result = _handle_model_command(mock_router, "/model switch nonexistent_profile")

        # 应该提示找不到 profile
        assert "未找到 profile" in result
        assert "nonexistent_profile" in result

    def test_model_switch_command_missing_argument(self):
        """测试 /model switch 命令（缺少参数）"""
        mock_router = Mock()

        result = _handle_model_command(mock_router, "/model switch")

        # 应该提示用法
        assert "用法" in result
        assert "/model switch" in result

    def test_model_unknown_subcommand(self):
        """测试未知的子命令"""
        mock_router = Mock()

        result = _handle_model_command(mock_router, "/model unknown")

        # 应该提示未知子命令
        assert "未知子命令" in result
        assert "unknown" in result

    def test_model_list_with_current_profile_marker(self):
        """测试 /model list 显示当前激活的 profile 标记"""
        mock_router = Mock()
        mock_session = Mock()
        mock_llm = Mock()

        # 使用实际存在的 profile
        profiles = load_profiles()
        if profiles:
            first_profile = list(profiles.keys())[0]
            mock_llm.profile_name = first_profile
            mock_session.llm = mock_llm
            mock_router.current = mock_session

            result = _handle_model_command(mock_router, "/model list")

            # 应该有 → 标记
            assert "→" in result
            # 当前 profile 应该被标记
            assert first_profile in result

    def test_model_list_without_profile_name_attribute(self):
        """测试 /model list 当 llm 对象没有 profile_name 属性时的回退机制"""
        mock_router = Mock()
        mock_session = Mock()
        mock_llm = Mock()

        # 模拟 llm 对象没有 profile_name 属性
        # 在这种情况下，应该从配置文件读取
        if hasattr(mock_llm, 'profile_name'):
            delattr(mock_llm, 'profile_name')

        mock_session.llm = mock_llm
        mock_router.current = mock_session

        result = _handle_model_command(mock_router, "/model list")

        # 应该能正常显示列表，并可能从配置文件获取当前 profile
        assert "可用模型配置" in result
        # 应该有当前使用的提示（可能来自配置文件）
        assert "当前使用" in result or "当前未激活" in result

    @patch('app.config.loader.load_profiles')
    def test_model_list_handles_empty_profiles(self, mock_load):
        """测试处理空配置的情况"""
        mock_load.return_value = {}
        mock_router = Mock()

        result = _handle_model_command(mock_router, "/model list")

        # 应该提示未找到配置
        assert "未找到任何模型配置" in result

    @patch('app.config.loader.load_profiles')
    def test_model_list_handles_exception(self, mock_load):
        """测试处理加载异常"""
        mock_load.side_effect = Exception("配置文件错误")
        mock_router = Mock()

        result = _handle_model_command(mock_router, "/model list")

        # 应该提示加载失败
        assert "加载模型配置失败" in result

    def test_model_command_without_subcommand_defaults_to_list(self):
        """测试不带子命令时默认为 list"""
        mock_router = Mock()
        mock_session = Mock()
        mock_llm = Mock()
        mock_llm.profile_name = "test"
        mock_session.llm = mock_llm
        mock_router.current = mock_session

        result = _handle_model_command(mock_router, "/model")

        # 应该显示列表
        assert "可用模型配置" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
