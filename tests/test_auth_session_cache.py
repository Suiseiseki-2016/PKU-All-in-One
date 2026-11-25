"""Pure in-memory, per-account Blackboard cookie cache and auto-relogin tests."""

from __future__ import annotations

import threading

import httpx
import pytest

from pku_sync import auth


def _make_mock_client_factory(handler, *, seen_clients: list[httpx.Client] | None = None):
    def factory(*, force_direct: bool = False, identity_proxy: str | None = None) -> httpx.Client:
        client = httpx.Client(
            base_url=auth.BB_BASE,
            follow_redirects=True,
            transport=httpx.MockTransport(handler),
        )
        if seen_clients is not None:
            seen_clients.append(client)
        return client

    return factory


def _install_mock_sso(monkeypatch: pytest.MonkeyPatch, valid_tokens_by_user: dict[str, str]):
    """Wire fake IAAA -> Blackboard SSO + protected endpoint over MockTransport."""
    state = {
        "login_calls": [],
        "request_cookies": [],
        "seq": 0,
        "expired_sessions": set(),
        "force_redirect_url": None,
        "force_always_401": False,
        "closed_client_calls": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = (request.url.host or "").lower()
        path = request.url.path or ""

        if path == "/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin":
            token = request.url.params.get("token", "")
            resp = httpx.Response(200, text="ok")
            resp.headers["Set-Cookie"] = f"JSESSIONID={token}; Path=/"
            return resp

        if host == "resourcese.pku.edu.cn":
            return httpx.Response(401, text="media-expired")

        cookie = request.headers.get("cookie", "")
        state["request_cookies"].append((request.method, path, cookie))

        if state["force_always_401"]:
            return httpx.Response(401, text="always-unauthorized")

        for expired in list(state["expired_sessions"]):
            if f"JSESSIONID={expired}" in cookie:
                if state["force_redirect_url"]:
                    return httpx.Response(
                        302, headers={"Location": state["force_redirect_url"]}
                    )
                return httpx.Response(401, text="expired")

        if any(f"JSESSIONID={tok}" in cookie for tok in valid_tokens_by_user.values()):
            return httpx.Response(200, json={"ok": True, "url": url, "cookie": cookie})

        return httpx.Response(401, text="missing-cookie")

    monkeypatch.setattr(auth, "new_client", _make_mock_client_factory(handler))

    def fake_iaaa(client: httpx.Client, appid: str, redir_url: str, username: str = "", password: str = "") -> str:
        state["login_calls"].append((username, password))
        state["seq"] += 1
        token = f"{username}-s{state['seq']}"
        valid_tokens_by_user[username] = token
        return token

    monkeypatch.setattr(auth, "iaaa_authenticate", fake_iaaa)
    return state


def test_session_cache_reuses_cookies_without_sharing_client_lifecycle(monkeypatch):
    valid: dict[str, str] = {}
    state = _install_mock_sso(monkeypatch, valid)

    c1 = auth.get_session(username="230001", password="secret")
    assert len(state["login_calls"]) == 1
    assert c1.get("/learn/api/public/v1/users/me").status_code == 200
    c1.close()

    # Cache hit returns a brand-new open client with the cached cookies.
    c2 = auth.get_session(username="230001", password="secret")
    try:
        assert not c2.is_closed
        assert len(state["login_calls"]) == 1
        assert auth.cookie_header(c2) == "JSESSIONID=230001-s1"
        resp = c2.get("/learn/api/public/v1/users/me")
        assert resp.status_code == 200
        assert resp.json()["cookie"] == "JSESSIONID=230001-s1"
    finally:
        c2.close()


def test_session_cache_isolates_accounts_and_password_changes(monkeypatch):
    valid: dict[str, str] = {}
    state = _install_mock_sso(monkeypatch, valid)

    c_user1 = auth.get_session(username="user1", password="p1")
    c_user2 = auth.get_session(username="user2", password="p2")
    c_user1_new_pwd = auth.get_session(username="user1", password="p1-updated")
    try:
        assert len(state["login_calls"]) == 3
        assert auth.cookie_header(c_user1) == "JSESSIONID=user1-s1"
        assert auth.cookie_header(c_user2) == "JSESSIONID=user2-s2"
        assert auth.cookie_header(c_user1_new_pwd) == "JSESSIONID=user1-s3"

        # Verify each cached entry stays cleanly isolated on lookup.
        c_user2_again = auth.get_session(username="user2", password="p2")
        try:
            assert auth.cookie_header(c_user2_again) == "JSESSIONID=user2-s2"
            assert len(state["login_calls"]) == 3
        finally:
            c_user2_again.close()
    finally:
        c_user1.close()
        c_user2.close()
        c_user1_new_pwd.close()


def test_session_cache_ttl_expiry_and_zero_disables_cache(monkeypatch):
    valid: dict[str, str] = {}
    state = _install_mock_sso(monkeypatch, valid)

    clock = {"now": 1000.0}
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock["now"])
    monkeypatch.setenv("PKU_SESSION_CACHE_TTL_SECONDS", "60")

    auth.get_session(username="230001", password="secret").close()
    assert len(state["login_calls"]) == 1

    clock["now"] = 1059.0
    auth.get_session(username="230001", password="secret").close()
    assert len(state["login_calls"]) == 1

    clock["now"] = 1060.0
    auth.get_session(username="230001", password="secret").close()
    assert len(state["login_calls"]) == 2

    monkeypatch.setenv("PKU_SESSION_CACHE_TTL_SECONDS", "0")
    auth.get_session(username="230001", password="secret").close()
    auth.get_session(username="230001", password="secret").close()
    assert len(state["login_calls"]) == 4


def test_cached_client_auto_relogins_on_401_and_refreshes_cache_and_cookies(monkeypatch):
    valid: dict[str, str] = {}
    state = _install_mock_sso(monkeypatch, valid)

    c1 = auth.get_session(username="230001", password="secret")
    c1.close()

    # Next call gets cached s1 cookie, then server revokes s1 before request executes.
    c2 = auth.get_session(username="230001", password="secret")
    try:
        assert len(state["login_calls"]) == 1
        state["expired_sessions"].add("230001-s1")

        resp = c2.get("/learn/api/public/v1/courses")
        assert resp.status_code == 200
        assert len(state["login_calls"]) == 2
        assert auth.cookie_header(c2) == "JSESSIONID=230001-s2"
        assert resp.json()["cookie"] == "JSESSIONID=230001-s2"

        # Subsequent get_session uses the refreshed cached s2 without logging in again.
        c3 = auth.get_session(username="230001", password="secret")
        try:
            assert len(state["login_calls"]) == 2
            assert auth.cookie_header(c3) == "JSESSIONID=230001-s2"
        finally:
            c3.close()
    finally:
        c2.close()


@pytest.mark.parametrize(
    "redirect_target",
    [
        "https://iaaa.pku.edu.cn/iaaa/oauth.jsp?appID=blackboard",
        "/webapps/login/?action=relogin",
    ],
)
def test_cached_client_auto_relogins_on_login_redirects(monkeypatch, redirect_target):
    valid: dict[str, str] = {}
    state = _install_mock_sso(monkeypatch, valid)
    state["force_redirect_url"] = redirect_target

    auth.get_session(username="230001", password="secret").close()
    c2 = auth.get_session(username="230001", password="secret")
    try:
        state["expired_sessions"].add("230001-s1")
        resp = c2.get("/webapps/portal/execute/defaultTab")
        assert resp.status_code == 200
        assert len(state["login_calls"]) == 2
        assert auth.cookie_header(c2) == "JSESSIONID=230001-s2"
    finally:
        c2.close()


def test_auto_relogin_ignores_non_bb_hosts_and_never_loops(monkeypatch):
    valid: dict[str, str] = {}
    state = _install_mock_sso(monkeypatch, valid)

    client = auth.get_session(username="230001", password="secret")
    try:
        # 401 on resourcese.pku.edu.cn must pass straight through without IAAA login.
        media_resp = client.get("https://resourcese.pku.edu.cn/file.pdf")
        assert media_resp.status_code == 401
        assert len(state["login_calls"]) == 1

        # Persistent 401 on Blackboard triggers at most one relogin per request.
        state["force_always_401"] = True
        bb_resp = client.get("/learn/api/public/v1/users/me")
        assert bb_resp.status_code == 401
        assert len(state["login_calls"]) == 2
    finally:
        client.close()


def test_post_401_invalidates_cache_without_replaying_non_idempotent_request(monkeypatch):
    valid: dict[str, str] = {}
    state = _install_mock_sso(monkeypatch, valid)

    client = auth.get_session(username="230001", password="secret")
    try:
        state["expired_sessions"].add("230001-s1")
        post_resp = client.post("/webapps/assignment/uploadAssignment", data={"title": "hw1"})
        assert post_resp.status_code == 401
        assert len(state["login_calls"]) == 1

        # Cache entry was invalidated, so the next get_session performs a fresh login.
        c2 = auth.get_session(username="230001", password="secret")
        try:
            assert len(state["login_calls"]) == 2
            assert auth.cookie_header(c2) == "JSESSIONID=230001-s2"
        finally:
            c2.close()
    finally:
        client.close()


def test_clear_session_cache_scopes_to_user_or_clears_all(monkeypatch):
    valid: dict[str, str] = {}
    state = _install_mock_sso(monkeypatch, valid)

    auth.get_session(username="u1", password="p1").close()
    auth.get_session(username="u2", password="p2").close()
    assert len(state["login_calls"]) == 2

    auth.clear_session_cache(" u1 ")
    auth.get_session(username="u2", password="p2").close()
    assert len(state["login_calls"]) == 2

    auth.get_session(username="u1", password="p1").close()
    assert len(state["login_calls"]) == 3

    auth.clear_session_cache()
    auth.get_session(username="u1", password="p1").close()
    auth.get_session(username="u2", password="p2").close()
    assert len(state["login_calls"]) == 5


def test_concurrent_expiry_deduplicates_relogin_for_same_account(monkeypatch):
    seq = {"val": 0}
    login_calls: list[str] = []
    barrier = threading.Barrier(2, timeout=5)
    expired: set[str] = set()
    active: set[str] = set()
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin":
            token = request.url.params.get("token", "")
            resp = httpx.Response(200, text="ok")
            resp.headers["Set-Cookie"] = f"JSESSIONID={token}; Path=/"
            return resp
        cookie = request.headers.get("cookie", "")
        if "JSESSIONID=u1-s1" in cookie and "u1-s1" in expired:
            barrier.wait()
            return httpx.Response(401, text="expired")
        if any(f"JSESSIONID={tok}" in cookie for tok in active):
            return httpx.Response(200, json={"cookie": cookie})
        return httpx.Response(401, text="unauthorized")

    monkeypatch.setattr(auth, "new_client", _make_mock_client_factory(handler))

    def fake_iaaa(client: httpx.Client, appid: str, redir_url: str, username: str = "", password: str = "") -> str:
        with lock:
            seq["val"] += 1
            token = f"{username}-s{seq['val']}"
            login_calls.append(token)
            active.add(token)
            return token

    monkeypatch.setattr(auth, "iaaa_authenticate", fake_iaaa)

    c1 = auth.get_session(username="u1", password="p1")
    c2 = auth.get_session(username="u1", password="p1")
    expired.add("u1-s1")

    results: list[tuple[int, str]] = []
    errors: list[BaseException] = []

    def worker(client: httpx.Client) -> None:
        try:
            r = client.get("/learn/api/public/v1/courses")
            results.append((r.status_code, auth.cookie_header(client)))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=(c1,))
    t2 = threading.Thread(target=worker, args=(c2,))
    t1.start()
    t2.start()
    t1.join(5)
    t2.join(5)
    c1.close()
    c2.close()

    assert not errors
    assert results == [
        (200, "JSESSIONID=u1-s2"),
        (200, "JSESSIONID=u1-s2"),
    ]
    assert login_calls == ["u1-s1", "u1-s2"]


def test_session_cache_evicts_oldest_entry_above_max_entries(monkeypatch):
    valid: dict[str, str] = {}
    state = _install_mock_sso(monkeypatch, valid)
    monkeypatch.setattr(auth, "_MAX_SESSION_CACHE_ENTRIES", 2)

    clock = {"now": 100.0}
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock["now"])

    auth.get_session(username="u1", password="p1").close()
    clock["now"] = 101.0
    auth.get_session(username="u2", password="p2").close()
    clock["now"] = 102.0
    auth.get_session(username="u3", password="p3").close()
    assert len(state["login_calls"]) == 3

    # u2 and u3 remain cached; u1 was evicted and triggers a fresh login.
    auth.get_session(username="u2", password="p2").close()
    auth.get_session(username="u3", password="p3").close()
    assert len(state["login_calls"]) == 3

    auth.get_session(username="u1", password="p1").close()
    assert len(state["login_calls"]) == 4
