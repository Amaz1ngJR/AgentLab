"""离线测试：敏感凭据脱敏。"""
from app.util.redact import format_exception, format_traceback, redact


def test_redact_anthropic_api_key():
    assert "sk-ant-***" in redact("sk-ant-abcDEF123456789xyz")


def test_redact_openai_api_key():
    raw = "OPENAI_API_KEY=sk-abc123def456ghi789jkl000"
    out = redact(raw)
    # 原始 token 必须被遮蔽
    assert "sk-abc123def456ghi789jkl000" not in out
    # 字段名保留以便排查
    assert "OPENAI_API_KEY" in out


def test_redact_proxy_token():
    out = redact("token: cr_d7d6b7cb521b703c8acda91cccab504b2304510")
    assert "cr_***" in out
    # 字段名保留
    assert "token:" in out


def test_redact_bearer_header():
    out = redact("Authorization: Bearer cr_xxxxxxxxxxxxxxxxxxxx")
    assert "Bearer ***" in out
    assert "cr_xxxxxxxxxxxxxxxxxxxx" not in out


def test_redact_x_api_key_header():
    out = redact("x-api-key: sk-ant-abcdef1234")
    # 名字保留,值替换
    assert "x-api-key:" in out
    assert "sk-ant-abcdef1234" not in out


def test_redact_no_op_on_clean_text():
    assert redact("hello world") == "hello world"
    assert redact("") == ""


def test_redact_value_preserves_nested_structure():
    from app.util.redact import redact_value

    value = {
        "output": 'api_key="secret-value" and "quoted"',
        "items": ["auth_token=secret-value", 1, None],
    }
    cleaned = redact_value(value)
    assert cleaned["output"] == 'api_key="***" and "quoted"'
    assert cleaned["items"] == ["auth_token=***", 1, None]
    assert value["output"] != cleaned["output"]


def test_redact_multiple_in_one_string():
    out = redact("key1=sk-ant-aaaaaaaa key2=cr_bbbbbbbbbbbbbbbbbbbb")
    assert "sk-ant-***" in out
    assert "cr_***" in out


def test_format_exception_redacts():
    exc = RuntimeError("connect failed: Authorization: Bearer cr_secret_token_value_xxxx")
    out = format_exception(exc)
    assert "RuntimeError:" in out
    assert "Bearer ***" in out
    assert "cr_secret_token_value_xxxx" not in out


def test_format_traceback_redacts():
    try:
        raise RuntimeError("auth_token=cr_xxxxxxxxxxxxxxxxxxxx failed")
    except RuntimeError as exc:
        out = format_traceback(exc)
    assert "Traceback" in out
    assert "cr_xxxxxxxxxxxxxxxxxxxx" not in out
