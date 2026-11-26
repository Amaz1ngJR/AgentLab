"""Authentication commands run before AgentSession construction."""
import argparse
import json
import os
import time

import httpx

from app.auth.chatgpt import AuthError, PERMISSION, RESOURCE, TokenManager

DEFAULT_CHATGPT_PROFILE = "codex"


def main(argv):
    from dotenv import load_dotenv
    from app.config.loader import PROJECT_ROOT
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(prog="agentlab auth")
    parser.add_argument("action", choices=["login", "status", "logout", "models", "test"])
    parser.add_argument("provider", choices=["chatgpt"])
    parser.add_argument("--account", default=os.getenv("CHATGPT_ACCOUNT", "default"))
    parser.add_argument("--profile", default=DEFAULT_CHATGPT_PROFILE, help="test 使用的 ChatGPT OAuth profile")
    args = parser.parse_args(argv)
    try:
        manager = TokenManager(args.account)
        if args.action == "login":
            manager.login()
            print(f"ChatGPT 授权成功。可使用 agentlab --profile {DEFAULT_CHATGPT_PROFILE}。")
        elif args.action == "status":
            with manager.lock:
                record = manager.read()
            print(json.dumps({"account": args.account, "email": record.get("email"),
                "signed_in": bool(record.get("access_token")),
                "plan_authorized": PERMISSION in record.get("scopes", []),
                "access_expired": record.get("expires_at", 0) <= time.time()}, ensure_ascii=False))
        elif args.action == "logout":
            confirmed = manager.logout()
            print("已退出 ChatGPT。" if confirmed else
                  "本地已退出；远程撤销未确认，请在 ChatGPT 设置中断开 AgentLab。")
        elif args.action == "models":
            response = httpx.get(RESOURCE + "/models",
                headers={"Authorization": "Bearer " + manager.access_token()}, timeout=30)
            if response.is_error:
                raise AuthError(f"模型列表请求失败，HTTP {response.status_code}")
            for model in response.json().get("models", []):
                if model.get("visibility") == "list":
                    window = model.get("context_window", "unknown")
                    print(f"{model['slug']}\t{model.get('display_name', '')}\tcontext={window}")
        elif args.action == "test":
            from app.config.loader import load_config
            from app.models.router import build_model_router
            from app.models.protocol import ToolResult
            cfg = load_config(args.profile)
            if cfg.provider != "openai_chatgpt":
                raise AuthError("auth test 仅接受 openai_chatgpt profile，不会切换到 API Key 计费")
            cfg.chatgpt_account = args.account
            router = build_model_router(cfg)
            print("文本测试：", router.chat([{"role": "user", "content": "Reply exactly: OK"}]))
            messages = [{"role": "user", "content": "Call oauth_probe with value hello, then report its returned value."}]
            tools = [{"name": "oauth_probe", "description": "A harmless local echo probe.",
                "input_schema": {"type": "object", "properties": {"value": {"type": "string"}},
                                 "required": ["value"], "additionalProperties": False}}]
            result = router.create_message(messages, tools=tools)
            if not result.tool_calls:
                raise AuthError("模型未调用测试工具，工具闭环验证未通过")
            messages.extend(result.provider_payload)
            messages.extend(router.format_tool_results([
                ToolResult(tool_call_id=call.id, output=json.dumps(call.arguments)) for call in result.tool_calls]))
            result = router.create_message(messages, tools=tools)
            if result.tool_calls or not result.text:
                raise AuthError("工具回放后未收到最终文本，工具闭环验证未通过")
            print("工具闭环测试：", result.text)
        return 0
    except KeyboardInterrupt:
        print("授权操作已取消。")
        return 130
    except Exception as exc:
        # Network/SDK exception representations can contain sensitive request data.
        from app.models.openai_adapter import ProviderStreamError
        print(str(exc) if isinstance(exc, (AuthError, ProviderStreamError)) else
              "ChatGPT 操作失败，请检查网络、依赖和账号配置。")
        return 1
