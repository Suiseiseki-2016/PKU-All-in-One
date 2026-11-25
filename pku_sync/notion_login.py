"""Optional self-hosted browser OAuth login for Notion (``pku-sync notion login``).

The open-source client works directly with a user-supplied internal integration
token in ``NOTION_TOKEN`` (in ``.env``) with zero external relay dependency.
For users who register their own public Notion OAuth integration and set both
``NOTION_OAUTH_CLIENT_ID`` and ``NOTION_OAUTH_CLIENT_SECRET`` in ``.env``,
``pku-sync notion login`` opens Notion's authorization page, catches the
single-use loopback callback on ``http://localhost:8765/callback`` or
``http://localhost:8766/callback``, exchanges the authorization code directly
with Notion, and writes ``NOTION_TOKEN`` to ``.env``.

The token exchange is deliberately NOT retried: a failed exchange consumes
the single-use authorization code, so retrying the same code can never
succeed; the user simply runs login again (a fresh authorization).
"""

from __future__ import annotations

import base64
import html
import os
import secrets as secrets_mod
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .envfile import write_env_values
from .notion import API_BASE, NotionError
from .network import dead_loopback_proxy

DEFAULT_PORT = 8765
CALLBACK_PATH = "/callback"

# The only loopback ports the OAuth callback may ever bind -- exactly the
# redirect URIs allowlisted on the relay. The listener tries the preferred
# port first and falls back to the other one; an arbitrary port is never
# selected, and a process occupying a port is never killed.
CALLBACK_PORTS = (8765, 8766)

# Pinned, actionable copy when BOTH approved callback ports are occupied.
# A named constant (not an inline literal) so the launcher and its test
# share the exact string: it names both approved ports and the recovery
# action.
CALLBACK_PORTS_BUSY_MESSAGE = "本地回调端口 8765 与 8766 都被占用：关闭占用端口的应用后重试。"

def resolve_client(settings) -> tuple[str, str]:
    """OAuth client credentials for optional self-hosted Notion OAuth."""
    client_id = (getattr(settings, "notion_oauth_client_id", "") or "").strip()
    client_secret = (getattr(settings, "notion_oauth_client_secret", "") or "").strip()
    if not client_id or not client_secret:
        raise NotionError(
            "未配置 Notion OAuth 应用凭据：开源版可直接在 .env 填写集成令牌 `NOTION_TOKEN`，"
            "或在 .env 设置自建应用的 NOTION_OAUTH_CLIENT_ID 与 NOTION_OAUTH_CLIENT_SECRET。"
        )
    return client_id, client_secret


def resolve_client_id(settings) -> str:
    client_id, _ = resolve_client(settings)
    return client_id


def verify_token(settings=None) -> str:
    """Whoami with the saved token → "name @ workspace" for display.

    Constructs a fresh Settings by default: the login flow just wrote the
    token to .env, and the module-level cached instance was created before
    that.
    """
    if settings is None:
        from .config import Settings

        settings = Settings()
    from .notion import get_client

    with get_client(settings) as client:
        me = client.whoami()
    owner = (me.get("bot") or {}).get("owner") or {}
    return f"{me.get('name') or '(未命名集成)'} @ {owner.get('workspace_name') or 'workspace'}"

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """The Notion page where the user grants (or denies) access."""
    return f"{API_BASE}/oauth/authorize?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """One-shot authorization-code → access-token exchange (no retry)."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    with httpx.Client(transport=transport, timeout=30.0, trust_env=not dead_loopback_proxy()) as http:
        response = http.post(
            f"{API_BASE}/oauth/token",
            headers={"Authorization": f"Basic {basic}"},
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    if response.status_code != 200:
        try:
            body = response.json()
            detail = f"{body.get('code', '')} {body.get('message', '')}".strip()
        except ValueError:
            detail = (response.text or "")[:200]
        raise NotionError(
            f"token 交换失败（HTTP {response.status_code}）{detail}".strip(),
            status=response.status_code,
        )
    payload = response.json()
    if not payload.get("access_token"):
        raise NotionError("token 交换响应里没有 access_token")
    return payload


def env_file_path(settings) -> Path:
    """Same dual-path preference as config: ``PKU_ENV_FILE``, then CWD, then package root."""
    override = os.environ.get("PKU_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    cwd_env = Path(".env").resolve()
    pkg_env = _PACKAGE_ROOT / ".env"
    if cwd_env.exists():
        return cwd_env
    if pkg_env.exists():
        return pkg_env
    return cwd_env


def write_env_token(path: Path, token: str) -> Path:
    """Set ``NOTION_TOKEN=…`` in the .env, preserving every other line."""
    return write_env_values(path, {"NOTION_TOKEN": token})


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches Notion's redirect, hands the code to the waiting login()."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self._respond(404, "不是回调路径，忽略。")
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        states = query.get("state") or []
        if (len(states) != 1 or not states[0]
                or not secrets_mod.compare_digest(states[0].encode(), self.server.state.encode())):
            # A stale tab or a stray request: reject but keep waiting for
            # the real callback from the page this login opened.
            self._respond(400, "state 不匹配：请关闭旧授权页，用最新打开的授权页完成授权。")
            return
        # Multiple tabs can arrive concurrently at the threaded listener.
        # Only the first matching, unambiguous result may end this request.
        with self.server.result_lock:
            if self.server.result:
                status, message = 409, "本次授权回调已处理，请返回应用查看结果。"
            elif query.get("error") and query.get("code"):
                status, message = 400, "回调参数冲突，请使用最新打开的授权页。"
            elif query.get("error"):
                errors = query["error"]
                if len(errors) != 1 or not errors[0]:
                    status, message = 400, "回调错误参数无效，请使用最新打开的授权页。"
                else:
                    error = errors[0] if errors[0] in {
                        "access_denied", "user_cancelled", "user_canceled"
                    } else "provider_error"
                    self.server.result["error"] = error
                    status, message = 400, "授权被拒绝。可关闭本页面，回到 PKU All in Notion 重试。"
            elif len(query.get("code") or []) != 1 or not query["code"][0]:
                status, message = 400, "回调缺少有效的 code 参数。"
            else:
                self.server.result["code"] = query["code"][0]
                status, message = 200, "授权成功，可以关闭本页面，回到 PKU All in Notion 继续。"
        self._respond(status, message)

    def _respond(self, status: int, message: str) -> None:
        body = (
            "<html><head><meta charset='utf-8'><title>PKU All in Notion 授权</title></head>"
            f"<body style='font-family:sans-serif;padding:2em;line-height:1.6'>"
            f"<h3>{html.escape(message)}</h3></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # keep the terminal clean
        return


class _CallbackServer(ThreadingHTTPServer):
    def __init__(self, port: int, state: str):
        self.state = state
        self.result: dict = {}
        self.result_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), _CallbackHandler)

    def server_bind(self):
        # http.server defaults to SO_REUSEADDR, which on Windows silently
        # allows a second bind onto an occupied port ("port hijacking") and
        # would defeat the 8765→8766 fallback: an occupied port must raise
        # OSError. Bind exclusively on Windows so this listener is an honest
        # occupant too; other platforms keep the standard SO_REUSEADDR
        # semantics (TIME_WAIT-only reuse). SO_EXCLUSIVEADDRUSE exists only
        # on Windows sockets, so the capability check keeps this branch
        # cross-platform without naming an OS.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _wait_for_callback(server, url, *, open_browser, timeout, on_url) -> dict:
    """Serve the callback until a result arrives or the timeout elapses."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if on_url:
            on_url(url)
        if open_browser:
            webbrowser.open(url)
        deadline = time.monotonic() + timeout
        while not server.result and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return server.result


def _authorization_code(result: dict, timeout: float) -> str:
    """Map the callback wait outcome to the code, or raise the pinned state."""
    if "error" in result:
        raise NotionError(f"授权被拒绝：{result['error']}")
    if "code" not in result:
        raise NotionError(
            f"等待浏览器授权超时（>{timeout:.0f}s）。重跑一次 `pku-sync notion login` 即可。"
        )
    return result["code"]


def login(
    settings,
    *,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    timeout: float = 300.0,
    state: str | None = None,
    transport: httpx.BaseTransport | None = None,
    on_url: Callable[[str], None] | None = None,
) -> dict:
    """Legacy self-hosted REST flow (local client secret); not the CLI path.

    Kept for advanced users running their own relay-less setup.
    ``state`` and ``transport`` exist for the mock tests. ``on_url`` receives
    the authorize URL right before the wait starts (the CLI prints it so the
    flow also works when no browser can be opened, e.g. over SSH).
    """
    client_id, client_secret = resolve_client(settings)
    state = state or secrets_mod.token_hex(16)
    try:
        server = _CallbackServer(port, state)
    except OSError as exc:
        raise NotionError(
            f"本地回调端口 {port} 起不来（{exc}）。用 --port 换一个端口，"
            "并同步修改集成设置里的 redirect URI。"
        ) from exc
    redirect_uri = f"http://localhost:{server.server_address[1]}{CALLBACK_PATH}"
    url = authorize_url(client_id, redirect_uri, state)
    result = _wait_for_callback(
        server, url, open_browser=open_browser, timeout=timeout, on_url=on_url
    )
    code = _authorization_code(result, timeout)
    token_info = exchange_code(
        client_id, client_secret, code, redirect_uri, transport=transport
    )
    env_path = write_env_token(env_file_path(settings), token_info["access_token"])
    return {**token_info, "env_path": env_path, "authorize_url": url}


def login_via_relay(
    settings,
    *,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    timeout: float = 300.0,
    state: str | None = None,
    transport: httpx.BaseTransport | None = None,
    on_url: Callable[[str], None] | None = None,
) -> dict:
    """Backwards-compatible alias delegating to self-hosted Notion OAuth ``login``."""
    return login(
        settings,
        port=port,
        open_browser=open_browser,
        timeout=timeout,
        state=state,
        transport=transport,
        on_url=on_url,
    )
