"""PKU IAAA single sign-on for course.pku.edu.cn (Blackboard Learn).

Flow, reverse-engineered from OAuthLogin.js on iaaa.pku.edu.cn:
  1. GET /iaaa/getPublicKey.do            -> RSA public key (PEM)
  2. RSA/PKCS1v15-encrypt the password with that key (matches JSEncrypt)
  3. POST /iaaa/oauthlogin.do             -> {"success": true, "token": "..."}
  4. GET campusLogin?token=...            -> Blackboard session cookies

PKU's CA is absent from the default macOS/Windows trust stores, so TLS
verification is disabled for these hosts.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import logging
import math
import os
import threading
import time
import weakref

import httpx
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from .config import settings
from . import network
from .network import campus_identity_proxy, campus_identity_fallback_proxy

IAAA_BASE = "https://iaaa.pku.edu.cn"
BB_BASE = "https://course.pku.edu.cn"
_BB_HOST = "course.pku.edu.cn"
RESOURCE_BASE = "https://resourcese.pku.edu.cn"

# Pure in-memory, per-account Blackboard cookie cache (never persisted to disk).
_DEFAULT_SESSION_CACHE_TTL = 720.0  # 12 minutes
_MAX_SESSION_CACHE_ENTRIES = 512

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CachedSession:
    cached_at: float
    username: str
    cookies: tuple[tuple[str, str, str, str], ...]
    generation: int = 0


_SESSION_CACHE_LOCK = threading.RLock()
_SESSION_COOKIE_CACHE: dict[str, _CachedSession] = {}
_RELOGIN_LOCKS: dict[str, threading.Lock] = {}
_CACHE_GENERATION = 0

# IAAA's appid allowlist is registered against the HTTP callback, but the HTTP
# gateway intermittently 502s, so the token is exchanged over HTTPS instead.
_CAMPUS_LOGIN_REGISTERED = (
    "http://course.pku.edu.cn/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
)
_CAMPUS_LOGIN_HTTPS = (
    "https://course.pku.edu.cn/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0"
)


class CampusLoginRejected(RuntimeError):
    """IAAA refused the supplied credentials; never include its private response."""


def _encrypt_password(password: str, public_key_pem: str) -> str:
    key = load_pem_public_key(public_key_pem.encode())
    assert isinstance(key, RSAPublicKey)
    return base64.b64encode(key.encrypt(password.encode("utf-8"), padding.PKCS1v15())).decode()


def new_client(*, force_direct: bool = False, identity_proxy: str | None = None) -> httpx.Client:
    """An httpx client tuned for the campus network.

    course.pku.edu.cn aborts a TLS handshake every so often, roughly once in
    several attempts and more often from off-campus hosts. Retrying inside the
    transport means any request in a long sync recovers, not just the login.
    """
    proxy = None if force_direct else (identity_proxy or campus_identity_proxy())
    mounts = ({IAAA_BASE: httpx.HTTPTransport(proxy=proxy, retries=1, verify=False)}
              if proxy else {})
    media_proxy = None if force_direct else network.campus_media_proxy()
    if media_proxy:
        for host in (RESOURCE_BASE, "https://yjapise.pku.edu.cn", "https://onlineroomse.pku.edu.cn"):
            mounts[host] = httpx.HTTPTransport(proxy=media_proxy, retries=1, verify=False)
    return httpx.Client(
        base_url=BB_BASE,
        follow_redirects=True,
        timeout=httpx.Timeout(20.0, connect=6.0),
        transport=httpx.HTTPTransport(retries=1, verify=False),
        mounts=mounts or None,
        headers={"User-Agent": USER_AGENT},
    )


def _session_cache_ttl() -> float:
    raw = os.environ.get("PKU_SESSION_CACHE_TTL_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_SESSION_CACHE_TTL
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_SESSION_CACHE_TTL
    if not math.isfinite(value):
        return _DEFAULT_SESSION_CACHE_TTL
    return max(0.0, value)


def _cache_key(username: str, password: str) -> str:
    return hashlib.sha256(f"{username}\0{password}".encode("utf-8")).hexdigest()


def _extract_cookies(client: object) -> tuple[tuple[str, str, str, str], ...]:
    cookies = getattr(client, "cookies", None)
    jar = getattr(cookies, "jar", None)
    if jar is None:
        return ()
    return tuple(
        (cookie.name, cookie.value or "", cookie.domain or "", cookie.path or "/")
        for cookie in list(jar)
    )


def _apply_cookies(
    client: httpx.Client, cookies: tuple[tuple[str, str, str, str], ...]
) -> None:
    for name, value, domain, path in cookies:
        client.cookies.set(name, value, domain=domain, path=path)


def _replace_bb_cookies(
    client: httpx.Client, cookies: tuple[tuple[str, str, str, str], ...]
) -> None:
    jar = client.cookies.jar
    doomed = [
        (cookie.domain, cookie.path, cookie.name)
        for cookie in list(jar)
        if (cookie.domain or "").lstrip(".").lower() in ("", _BB_HOST, "pku.edu.cn")
    ]
    for domain, path, name in doomed:
        try:
            jar.clear(domain, path, name)
        except KeyError:
            pass
    _apply_cookies(client, cookies)


def _prune_cache_locked(now: float, ttl: float) -> None:
    expired = [
        key
        for key, entry in _SESSION_COOKIE_CACHE.items()
        if (now - entry.cached_at) >= ttl
    ]
    for key in expired:
        _SESSION_COOKIE_CACHE.pop(key, None)
        lock = _RELOGIN_LOCKS.get(key)
        if lock is not None and not lock.locked():
            _RELOGIN_LOCKS.pop(key, None)
    while len(_SESSION_COOKIE_CACHE) > _MAX_SESSION_CACHE_ENTRIES:
        oldest_key = min(
            _SESSION_COOKIE_CACHE,
            key=lambda key: (
                _SESSION_COOKIE_CACHE[key].cached_at,
                _SESSION_COOKIE_CACHE[key].generation,
            ),
        )
        _SESSION_COOKIE_CACHE.pop(oldest_key, None)
        lock = _RELOGIN_LOCKS.get(oldest_key)
        if lock is not None and not lock.locked():
            _RELOGIN_LOCKS.pop(oldest_key, None)


def _get_relogin_lock(cache_key: str) -> threading.Lock:
    with _SESSION_CACHE_LOCK:
        return _RELOGIN_LOCKS.setdefault(cache_key, threading.Lock())


def _store_cached_session(
    cache_key: str,
    username: str,
    cookies: tuple[tuple[str, str, str, str], ...],
    ttl: float,
) -> tuple[float, int]:
    global _CACHE_GENERATION
    with _SESSION_CACHE_LOCK:
        now = time.monotonic()
        _CACHE_GENERATION += 1
        generation = _CACHE_GENERATION
        _SESSION_COOKIE_CACHE[cache_key] = _CachedSession(
            cached_at=now,
            username=username,
            cookies=cookies,
            generation=generation,
        )
        _prune_cache_locked(now, ttl)
        return now, generation


def _invalidate_cache_entry(
    cache_key: str,
    *,
    expected_cached_at: float | None = None,
    expected_generation: int | None = None,
) -> None:
    with _SESSION_CACHE_LOCK:
        entry = _SESSION_COOKIE_CACHE.get(cache_key)
        if entry is None:
            return
        if expected_cached_at is None:
            _SESSION_COOKIE_CACHE.pop(cache_key, None)
            return
        if expected_generation is not None:
            if entry.generation <= expected_generation and entry.cached_at <= expected_cached_at:
                _SESSION_COOKIE_CACHE.pop(cache_key, None)
        elif entry.cached_at <= expected_cached_at:
            _SESSION_COOKIE_CACHE.pop(cache_key, None)


def clear_session_cache(username: str | None = None) -> None:
    """Clear pure in-memory cached Blackboard sessions (all users or one user)."""
    with _SESSION_CACHE_LOCK:
        if username is None:
            _SESSION_COOKIE_CACHE.clear()
            for key in [k for k, lock in _RELOGIN_LOCKS.items() if not lock.locked()]:
                _RELOGIN_LOCKS.pop(key, None)
            return
        target = username.strip()
        if not target:
            return
        doomed = [
            key for key, entry in _SESSION_COOKIE_CACHE.items() if entry.username == target
        ]
        for key in doomed:
            _SESSION_COOKIE_CACHE.pop(key, None)
            lock = _RELOGIN_LOCKS.get(key)
            if lock is not None and not lock.locked():
                _RELOGIN_LOCKS.pop(key, None)


def _is_bb_host(url: httpx.URL) -> bool:
    return (url.host or "").lower() == _BB_HOST


def _is_login_callback(url: httpx.URL) -> bool:
    path = (url.path or "").lower()
    return "/webapps/bb-sso-bblearn/" in path or "/webapps/login" in path


def _is_expired_auth_response(request: httpx.Request, response: httpx.Response) -> bool:
    if not _is_bb_host(request.url) or _is_login_callback(request.url):
        return False
    if response.status_code == 401:
        return True
    if response.status_code in (301, 302, 303, 307, 308):
        location = (response.headers.get("location") or "").strip().lower()
        return bool(location) and (
            "iaaa.pku.edu.cn" in location
            or "/webapps/login" in location
            or "bb-sso-bblearn" in location
        )
    return False


class _AutoReloginTransport(httpx.BaseTransport):
    """Wrap Blackboard transport to transparently refresh expired cookies once."""

    def __init__(
        self,
        inner: httpx.BaseTransport,
        client: httpx.Client,
        *,
        cache_key: str,
        username: str,
        password: str,
        cached_at: float,
        generation: int,
        attempts: int,
    ) -> None:
        self._inner = inner
        self._client_ref = weakref.ref(client)
        self._cache_key = cache_key
        self._username = username
        self._password = password
        self._cached_at = cached_at
        self._generation = generation
        self._attempts = attempts

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        if not _is_expired_auth_response(request, response):
            return response
        if request.method.upper() not in ("GET", "HEAD"):
            _invalidate_cache_entry(
                self._cache_key,
                expected_cached_at=self._cached_at,
                expected_generation=self._generation,
            )
            return response

        response.close()
        _logger.info("bb_session_cache=relogin reason=status_%s", response.status_code)
        fresh_cookies, new_cached_at, new_generation = self._refresh_cookies()
        self._cached_at = new_cached_at
        self._generation = new_generation

        client = self._client_ref()
        if client is not None:
            _replace_bb_cookies(client, fresh_cookies)
            request.headers.pop("cookie", None)
            client.cookies.set_cookie_header(request)
        else:
            temp_cookies = httpx.Cookies()
            for name, value, domain, path in fresh_cookies:
                temp_cookies.set(name, value, domain=domain, path=path)
            request.headers.pop("cookie", None)
            temp_cookies.set_cookie_header(request)

        return self._inner.handle_request(request)

    def _refresh_cookies(self) -> tuple[tuple[tuple[str, str, str, str], ...], float, int]:
        relogin_lock = _get_relogin_lock(self._cache_key)
        with relogin_lock:
            ttl = _session_cache_ttl()
            now = time.monotonic()
            with _SESSION_CACHE_LOCK:
                current = _SESSION_COOKIE_CACHE.get(self._cache_key)
                if (
                    ttl > 0
                    and current is not None
                    and (current.generation > self._generation or current.cached_at > self._cached_at)
                    and (now - current.cached_at) < ttl
                ):
                    return current.cookies, current.cached_at, current.generation

            _invalidate_cache_entry(
                self._cache_key,
                expected_cached_at=self._cached_at,
                expected_generation=self._generation,
            )

            fresh_client = _login_with_retries(
                attempts=self._attempts,
                username=self._username,
                password=self._password,
            )
            try:
                fresh_cookies = _extract_cookies(fresh_client)
            finally:
                close_fn = getattr(fresh_client, "close", None)
                if callable(close_fn):
                    close_fn()

            if ttl > 0 and fresh_cookies:
                new_cached_at, new_generation = _store_cached_session(
                    self._cache_key,
                    self._username,
                    fresh_cookies,
                    ttl,
                )
            else:
                new_cached_at = time.monotonic()
                new_generation = self._generation + 1
            return fresh_cookies, new_cached_at, new_generation

    def close(self) -> None:
        self._inner.close()


def _attach_auto_relogin(
    client: object,
    *,
    cache_key: str,
    username: str,
    password: str,
    cached_at: float,
    generation: int,
    attempts: int,
) -> None:
    if not isinstance(client, httpx.Client):
        return
    transport = getattr(client, "_transport", None)
    if not isinstance(transport, httpx.BaseTransport) or isinstance(transport, _AutoReloginTransport):
        return
    client._transport = _AutoReloginTransport(
        transport,
        client,
        cache_key=cache_key,
        username=username,
        password=password,
        cached_at=cached_at,
        generation=generation,
        attempts=attempts,
    )


def _login_with_retries(
    attempts: int = 2, username: str = "", password: str = ""
) -> httpx.Client:
    """Authenticate with IAAA, retrying transient network errors around login."""
    last_error: httpx.TransportError | None = None
    fallback = campus_identity_fallback_proxy() if campus_identity_proxy() is None else None
    for attempt in range(attempts):
        try:
            return _login(username, password)
        except httpx.TransportError as exc:
            last_error = exc
            if fallback:
                logging.getLogger(__name__).warning(
                    "campus_identity_route=windows-local-proxy host=iaaa.pku.edu.cn reason=%s",
                    type(exc).__name__)
                try:
                    return _login(username, password, identity_proxy=fallback)
                except httpx.TransportError as fallback_error:
                    last_error = fallback_error
                fallback = None
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
    if campus_identity_proxy() is not None:
        try:
            return _login(username, password, force_direct=True)
        except httpx.TransportError as exc:
            last_error = exc
    raise RuntimeError(
        f"北大统一身份认证连接失败，请检查校园网或代理设置后重试（IAAA login failed after {attempts} attempts: {last_error}）"
    ) from last_error


def get_session(
    attempts: int = 2,
    username: str = "",
    password: str = "",
    *,
    use_cache: bool = True,
) -> httpx.Client:
    """Authenticate with IAAA and return a client holding a Blackboard session.

    Credentials default to the configured pair in ``.env``; explicit values
    override per call, so a service layer can inject one user's credentials
    without touching global config.
    Transport errors that survive the client's own retries are retried once more
    around the whole login; a rejected credential is raised immediately, since
    retrying cannot help.

    When caching is enabled (default TTL 12 minutes, configurable via
    ``PKU_SESSION_CACHE_TTL_SECONDS``; 0 disables), session cookies are kept
    purely in memory per credential pair and automatically refreshed via IAAA
    if Blackboard rejects them early (HTTP 401 or redirect back to login).
    """
    user = (username or getattr(settings, "pku_username", "") or "").strip()
    pwd = password or getattr(settings, "pku_password", "") or ""
    ttl = _session_cache_ttl()
    cache_key = _cache_key(user, pwd) if (user and pwd) else ""

    if use_cache and ttl > 0 and cache_key:
        entry: _CachedSession | None = None
        now = time.monotonic()
        with _SESSION_CACHE_LOCK:
            candidate = _SESSION_COOKIE_CACHE.get(cache_key)
            if candidate is not None:
                if (now - candidate.cached_at) < ttl:
                    entry = candidate
                else:
                    _SESSION_COOKIE_CACHE.pop(cache_key, None)
        if entry is not None:
            client = new_client()
            if isinstance(client, httpx.Client):
                _apply_cookies(client, entry.cookies)
                _attach_auto_relogin(
                    client,
                    cache_key=cache_key,
                    username=user,
                    password=pwd,
                    cached_at=entry.cached_at,
                    generation=entry.generation,
                    attempts=attempts,
                )
            return client

    client = _login_with_retries(attempts=attempts, username=username, password=password)
    if ttl > 0 and cache_key and isinstance(client, httpx.Client):
        cookies = _extract_cookies(client)
        if cookies:
            cached_at, generation = _store_cached_session(cache_key, user, cookies, ttl)
            _attach_auto_relogin(
                client,
                cache_key=cache_key,
                username=user,
                password=pwd,
                cached_at=cached_at,
                generation=generation,
                attempts=attempts,
            )
    return client


def iaaa_authenticate(
    client: httpx.Client,
    appid: str,
    redir_url: str,
    username: str = "",
    password: str = "",
) -> str:
    """Redeem the given (or configured) credentials into an IAAA OAuth token.

    ``appid`` selects the target system (blackboard, portalPublicQuery, …);
    ``redir_url`` is the callback IAAA has registered for that appid. The
    token is what the caller exchanges for a session at the target's SSO
    endpoint. Explicit ``username``/``password`` override the configured
    pair (service-layer injection); empty means
    "use .env".
    """
    user = username or settings.pku_username
    pwd = password or settings.pku_password
    key_resp = client.get(f"{IAAA_BASE}/iaaa/getPublicKey.do")
    key_resp.raise_for_status()
    key_data = key_resp.json()
    if not key_data.get("success"):
        raise RuntimeError(f"无法获取北大统一身份认证公钥，请稍后重试（Failed to fetch IAAA public key: {key_data}）")

    encrypted_pwd = _encrypt_password(pwd, key_data["key"])

    login_resp = client.post(
        f"{IAAA_BASE}/iaaa/oauthlogin.do",
        data={
            "appid": appid,
            "userName": user,
            "password": encrypted_pwd,
            "randCode": "",
            "smsCode": "",
            "otpCode": "",
            "redirUrl": redir_url,
        },
    )
    login_resp.raise_for_status()
    payload = login_resp.json()
    if not payload.get("success"):
        raise CampusLoginRejected(
            "教学网登录被拒绝：请确认账号填写的是纯学号（不含 @stu.pku.edu.cn 邮箱后缀），且密码为北大统一身份认证（IAAA 门户）密码而非本站登录密码；如需额外验证，请先到统一身份认证页面完成验证后再试。"
        )
    return payload["token"]


def _login(
    username: str = "", password: str = "", *, force_direct: bool = False,
    identity_proxy: str | None = None
) -> httpx.Client:
    user = username or settings.pku_username
    pwd = password or settings.pku_password
    if not user or not pwd:
        raise RuntimeError("未配置北大教学网账号密码，请在工作台「连接设置」中保存账号（PKU_USERNAME and PKU_PASSWORD must be set in .env）")

    client = (new_client(force_direct=True) if force_direct else
              new_client(identity_proxy=identity_proxy) if identity_proxy else new_client())
    try:
        token = iaaa_authenticate(client, "blackboard", _CAMPUS_LOGIN_REGISTERED, user, pwd)
        bb_resp = client.get(_CAMPUS_LOGIN_HTTPS, params={"token": token})
        bb_resp.raise_for_status()
        return client
    except Exception:
        client.close()
        raise


def cookie_header(client: httpx.Client, domain_suffix: str = "pku.edu.cn") -> str:
    """Render the session cookies as a single `Cookie:` header value.

    ffmpeg cannot share httpx's cookie jar, so HLS downloads receive the
    session this way.
    """
    seen: dict[str, str] = {}
    for cookie in client.cookies.jar:
        if cookie.domain and cookie.domain.lstrip(".").endswith(domain_suffix):
            seen[cookie.name] = cookie.value or ""
    return "; ".join(f"{name}={value}" for name, value in seen.items())
