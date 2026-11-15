"""ChatGPT public-client OAuth. Never import or mutate Codex credentials."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from filelock import FileLock

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PERMISSION = "chatgpt.tokens.use.direct"
TERMINAL_REFRESH = {"invalid_grant", "invalid_refresh_token", "token_expired",
                    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"}


class AuthError(RuntimeError):
    retryable = False
    user_actionable = True  # Messages are sanitized; CLI can show the repair action directly.


def _atomic_json(path: Path, data: dict):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class TokenManager:
    def __init__(self, account="default", root=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account):
            raise AuthError("账号标签只能包含字母、数字、下划线和连字符（1–64 字符）")
        self.account = account
        self.root = Path(root or os.environ.get("CHATGPT_AUTH_DIR") or
                         Path.home() / ".agentlab/auth/chatgpt").expanduser()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.root.chmod(0o700)
        self.path = self.root / (account + ".json")
        self.lock = FileLock(str(self.path) + ".lock", timeout=30)

    def read(self):
        if not self.path.exists():
            return {}
        if os.name != "nt":
            self.path.chmod(0o600)
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _host(self):
        path = self.root / "host.json"
        with FileLock(str(path) + ".lock", timeout=30):
            if not path.exists():
                _atomic_json(path, {"id": "urn:uuid:" + str(uuid.uuid4())})
            return json.loads(path.read_text())["id"]

    @staticmethod
    def _exchange(data):
        try:
            response = httpx.post(TOKEN_URL, data=data, timeout=30, follow_redirects=False)
        except httpx.TransportError:
            raise AuthError("ChatGPT 授权服务暂时无法连接；凭据已保留，请稍后重试") from None
        if response.is_error:
            try:
                code = response.json().get("error", "unknown")
                if isinstance(code, dict):
                    code = code.get("code", "unknown")
            except ValueError:
                code = "unknown"
            # Only known codes are displayed; never echo token endpoint bodies.
            exc = AuthError("ChatGPT 授权失败；请检查连接或重新登录")
            exc.terminal = code in TERMINAL_REFRESH
            if exc.terminal:
                exc.args = ("ChatGPT 续期凭据已失效，请重新登录",)
            elif code == "invalid_client":
                exc.args = ("ChatGPT client_id 无效，请检查账号注册配置",)
            raise exc
        return response.json()

    @staticmethod
    def _tokens(previous, tokens):
        if not tokens.get("access_token") or str(tokens.get("token_type", "")).lower() != "bearer":
            raise AuthError("授权服务没有返回有效的 Bearer token")
        result = {**previous, **{k: tokens[k] for k in
                  ("access_token", "refresh_token", "id_token") if tokens.get(k)}}
        result["scopes"] = tokens["scope"].split() if "scope" in tokens else previous.get("scopes", [])
        result["expires_at"] = time.time() + float(tokens["expires_in"])
        return result

    def access_token(self):
        with self.lock:
            record = self.read()
            if not record.get("access_token"):
                raise AuthError(f"请先运行 agentlab auth login chatgpt --account {self.account}")
            if PERMISSION not in record.get("scopes", []):
                raise AuthError("该账号尚未授权 AgentLab 使用 ChatGPT 额度，请重新登录授权")
            if record["expires_at"] <= time.time() + 60:
                if not record.get("refresh_token"):
                    raise AuthError("缺少续期凭据，请重新登录 ChatGPT")
                try:
                    tokens = self._exchange({"grant_type": "refresh_token",
                        "client_id": record["client_id"], "refresh_token": record["refresh_token"],
                        "resource": RESOURCE})
                except AuthError as exc:
                    if getattr(exc, "terminal", False):
                        self._clear(record)
                    raise
                record = self._tokens(record, tokens)
                _atomic_json(self.path, record)
                if PERMISSION not in record["scopes"]:
                    raise AuthError("续期后的授权不允许使用 ChatGPT 额度，请重新登录")
            return record["access_token"]

    def _clear(self, record):
        _atomic_json(self.path, {k: v for k, v in record.items()
                     if k not in {"access_token", "refresh_token", "id_token", "expires_at", "scopes"}})

    def login(self, timeout=180):
        # Serialize login against refresh/logout, including across AgentLab processes.
        with self.lock:
            previous = self.read()
            host = self._host()
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            callback = {}

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass  # HTTP request targets contain authorization codes.

                def do_GET(self):
                    parsed = urlparse(self.path)
                    query = parse_qs(parsed.query)
                    valid = (parsed.path == "/auth/callback" and
                             len(query.get("state", [])) == 1 and
                             secrets.compare_digest(query["state"][0], state))
                    self.send_response(200 if valid else 400)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"Return to AgentLab." if valid else b"Invalid callback.")
                    if valid:
                        callback.update(query)

            with HTTPServer(("127.0.0.1", 0), Handler) as server:
                server.timeout = 0.5
                redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
                params = dict(client_id=previous.get("client_id", "dynamic_agent_client"),
                    ext_agent_host_id=host, response_type="code", redirect_uri=redirect,
                    scope=SCOPES, resource=RESOURCE, state=state, nonce=nonce,
                    code_challenge_method="S256", code_challenge=challenge)
                if not previous.get("client_id"):
                    params["agent_name_hint"] = "AgentLab"
                if previous.get("id_token"):
                    params["id_token_hint"] = previous["id_token"]
                print("Continue with ChatGPT：请在浏览器中完成 AgentLab 授权。")
                if not webbrowser.open(ISSUER + "/api/accounts/authorize?" + urlencode(params)):
                    raise AuthError("无法打开浏览器，请检查默认浏览器后重试")
                deadline = time.monotonic() + timeout
                while not callback and time.monotonic() < deadline:
                    server.handle_request()
            if not callback:
                raise AuthError("登录等待超时，原有凭据未更改")
            if "error" in callback:
                raise AuthError("登录授权被拒绝，原有凭据未更改")
            client_id = callback.get("client_id", [previous.get("client_id")])[0]
            if not client_id or client_id == "dynamic_agent_client":
                raise AuthError("注册未返回正式 client_id")
            if previous.get("client_id") and previous["client_id"] != client_id:
                raise AuthError("回调 client_id 与所选账号不匹配")
            if len(callback.get("code", [])) != 1:
                raise AuthError("回调缺少唯一授权码")
            tokens = self._exchange(dict(grant_type="authorization_code", client_id=client_id,
                code=callback["code"][0], code_verifier=verifier, redirect_uri=redirect, resource=RESOURCE))
            claims = validate_identity(tokens.get("id_token", ""), client_id, nonce)
            if previous.get("subject") and previous["subject"] != claims["sub"]:
                raise AuthError("登录身份与所选账号不匹配，请使用另一个 --account 标签")
            record = self._tokens(dict(client_id=client_id, ext_agent_host_id=host,
                subject=claims["sub"], issuer=ISSUER, email=claims.get("email", "")), tokens)
            _atomic_json(self.path, record)
            if PERMISSION not in record["scopes"]:
                raise AuthError("已登录，但未授权使用 ChatGPT 额度；请重新登录并允许额度使用")

    def logout(self):
        with self.lock:
            record = self.read()
            confirmed = not bool(record.get("refresh_token"))
            if not confirmed:
                for attempt in range(2):
                    try:
                        discovery_response = httpx.get(ISSUER + "/.well-known/openid-configuration", timeout=10)
                        discovery_response.raise_for_status()
                        endpoint = discovery_response.json()["revocation_endpoint"]
                        if not endpoint.startswith(ISSUER + "/"):
                            raise AuthError("无效的撤销端点")
                        response = httpx.post(endpoint, data={"token": record["refresh_token"],
                            "token_type_hint": "refresh_token", "client_id": record["client_id"]}, timeout=10)
                        confirmed = response.status_code == 200
                        if confirmed or response.status_code < 500:
                            break
                    except (httpx.HTTPError, ValueError, KeyError, AuthError):
                        pass
                    if attempt == 0:
                        time.sleep(.25)
            self._clear(record)
            return confirmed


def validate_identity(token, client_id, nonce):
    import jwt
    try:
        response = httpx.get(ISSUER + "/.well-known/openid-configuration", timeout=15)
        response.raise_for_status()
        jwks_uri = response.json()["jwks_uri"]
        if not jwks_uri.startswith(ISSUER + "/"):
            raise AuthError("无效的 JWKS 端点")
        key = jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=client_id,
            issuer=ISSUER, options={"require": ["exp", "iss", "aud", "sub", "nonce"]})
        if not secrets.compare_digest(str(claims["nonce"]), nonce):
            raise AuthError("ID token nonce 不匹配")
        return claims
    except (jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError):
        raise AuthError("ChatGPT 身份验证失败，原有凭据未更改") from None
