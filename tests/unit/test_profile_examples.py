"""Shipped entry points and examples must resolve without credentials or a model service."""
import re

from dotenv import dotenv_values

from app.agent.profiles import load_agent_profiles
from app.auth.cli import DEFAULT_CHATGPT_PROFILE
from app.config.loader import load_profiles
from app.config.paths import CONFIG_DIR, PROJECT_ROOT


def test_default_oauth_profiles_resolve():
    profiles = load_profiles()
    env_default = dotenv_values(PROJECT_ROOT / ".env.example")["ACTIVE_PROFILE"]
    assert profiles[env_default].provider == "openai_chatgpt"
    assert profiles[DEFAULT_CHATGPT_PROFILE].provider == "openai_chatgpt"


def test_agent_examples_resolve_to_expected_providers():
    profiles = load_profiles()
    agents = load_agent_profiles(CONFIG_DIR / "agents.example.yaml")
    actual = {key: profiles[agent.model_profile].provider for key, agent in agents.items()}
    assert actual == {"default": "anthropic", "coder": "anthropic", "local": "ollama",
                      "researcher": "anthropic", "quick": "openai_compatible"}


def test_ollama_script_defaults_agree_with_registry():
    scripts = PROJECT_ROOT / "scripts"
    verify = (scripts / "verify_local_model.sh").read_text(encoding="utf-8")
    (name,) = re.findall(r'^PROFILE="\$\{1:-([^}]+)\}"$', verify, re.M)
    profile = load_profiles()[name]
    assert profile.provider == "ollama"
    shell = (scripts / "install_local_model.sh").read_text(encoding="utf-8")
    ps1 = (scripts / "install_local_model.ps1").read_text(encoding="utf-8-sig")
    assert f'MODEL="${{1:-{profile.model}}}"' in shell
    assert f'[string]$Model = "{profile.model}"' in ps1
