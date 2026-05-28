"""配置加载器 —— 唯一的配置来源是 .env + config/models.yaml profile。

加载流程：
  1. 加载 .env 到进程环境变量
  2. 通过 --profile / ACTIVE_PROFILE 选择 config/models.yaml 中的 profile
  3. profile 声明 api_key_env / auth_token_env / base_url_env，从环境变量取值
  4. 推理参数从 profile.params 取，可被 LLM_TEMPERATURE 等环境变量覆盖
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

from app.config.schemas import LLMConfig, ModelProfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


def workspace_root() -> Path:
    """返回 Agent 文件工具被允许操作的根目录(绝对路径)。

    优先级:
      1. WORKSPACE_ROOT 环境变量(由 .env 注入或显式 export)
      2. 项目根目录 —— 为开发期默认行为

    使用场景:
      文件工具(read_file / write_file / list_dir)在执行前调用此函数,
      把用户传入的路径限制在返回的根目录内,越界请求会被拒绝。
    """
    raw = os.getenv("WORKSPACE_ROOT")
    if raw and raw.strip():
        return Path(raw.strip()).expanduser().resolve()
    return PROJECT_ROOT


def _env(name: str) -> Optional[str]:
    val = os.getenv(name)
    return val.strip() if val and val.strip() else None


def _env_float(name: str) -> Optional[float]:
    raw = _env(name)
    return float(raw) if raw else None


def _env_int(name: str) -> Optional[int]:
    raw = _env(name)
    return int(raw) if raw else None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    return raw.lower() in ("1", "true", "yes", "on") if raw else default


def _resolve_env_ref(value: str) -> str:
    """解析 YAML 中的 ${ENV_VAR:-default} 语法。"""
    def replace(m: re.Match) -> str:
        var, _, default = m.group(1).partition(":-")
        return os.getenv(var, default)
    return re.sub(r"\$\{([^}]+)\}", replace, value)


def load_profiles() -> dict[str, ModelProfile]:
    """加载 config/models.yaml 中所有 profile。"""
    path = CONFIG_DIR / "models.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    profiles: dict[str, ModelProfile] = {}
    for name, body in (raw.get("models") or {}).items():
        body = body or {}
        profiles[name] = ModelProfile(
            name=name,
            provider=body.get("provider", "openai_compatible"),
            model=_resolve_env_ref(body.get("model", name)),
            base_url=body.get("base_url"),
            api_key_env=body.get("api_key_env"),
            auth_token_env=body.get("auth_token_env"),
            base_url_env=body.get("base_url_env"),
            capabilities=list(body.get("capabilities") or []),
            params=dict(body.get("params") or {}),
        )
    return profiles


def load_config(profile_name: Optional[str] = None) -> LLMConfig:
    """合并 .env + profile，返回 LLMConfig。

    profile_name: 来自 --profile CLI 参数或 ACTIVE_PROFILE 环境变量。
    必须能解析出一个 profile，否则报错让用户显式选择。
    """
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    active_profile = profile_name or _env("ACTIVE_PROFILE")
    if not active_profile:
        raise RuntimeError(
            "未指定 model profile。请在 .env 设置 ACTIVE_PROFILE=xxx，"
            "或用 --profile xxx 启动。可选 profile 见 config/models.yaml。"
        )

    profiles = load_profiles()
    profile = profiles.get(active_profile)
    if profile is None:
        raise RuntimeError(
            f"未找到 profile '{active_profile}'。可选: {sorted(profiles)}"
        )

    # 凭据：profile 声明用哪个环境变量名，从进程 env 读值
    base_url = (
        os.getenv(profile.base_url_env) if profile.base_url_env else None
    ) or profile.base_url
    api_key = os.getenv(profile.api_key_env) if profile.api_key_env else None
    auth_token = os.getenv(profile.auth_token_env) if profile.auth_token_env else None

    # 推理参数：profile.params 默认值，环境变量可覆盖
    params = profile.params
    temperature = _env_float("LLM_TEMPERATURE") or params.get("temperature", 0.2)
    top_p = _env_float("LLM_TOP_P") or params.get("top_p")
    context_size = _env_int("LLM_CONTEXT_SIZE") or params.get("context_size")

    return LLMConfig(
        provider=profile.provider,
        model=profile.model,
        base_url=base_url,
        api_key=api_key,
        auth_token=auth_token,
        temperature=temperature,
        top_p=top_p,
        context_size=context_size,
        timeout_seconds=float(_env("LLM_TIMEOUT_SECONDS") or "120"),
        stream=_env_bool("LLM_STREAM", default=False),
        profile_name=active_profile,
        capabilities=list(profile.capabilities),
    )
