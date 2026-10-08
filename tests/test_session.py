"""Session renewal and error mapping, against a scripted HTTP layer."""
import contextlib
import threading

import pytest
import requests

from ds_web._session import Session
from ds_web.errors import LoginError, NetworkError, ServerError

LOGIN_PAGE = '<form><input name="usernameTextBox"/><input name="passwordTextBox"/></form>'


class Reply:
    def __init__(self, status=200, text="ok", content_type="text/html", url="https://x/browse/search.aspx", headers=None):
        self.status_code = status
        self.text = text
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.url = url


@pytest.fixture
def session(monkeypatch):
    s = Session("U", "P", timeout=5, pool_size=4)
    s.sign_ins = 0

    def fake_sign_in():
        s.sign_ins += 1

    monkeypatch.setattr(s, "_sign_in", fake_sign_in)
    return s


def script(session, monkeypatch, replies):
    """Answer requests from `replies` in order (the last one repeats)."""
    replies = list(replies)
    lock = threading.Lock()

    def request(*args, **kwargs):
        with lock:
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(session.http, "request", request)


@pytest.mark.parametrize("expired", [
    Reply(text=LOGIN_PAGE),                                   # search.aspx serves the sign-in page
    Reply(status=403),                                        # AJAX endpoints refuse
    Reply(headers={"X-Redirect-XHR-Caller": "https://x/DSLogon.aspx"}),
    Reply(url="https://x/dsws/1.0/DSLogon.aspx?x=1"),
])
def test_expiry_renews_once_and_retries(session, monkeypatch, expired):
    script(session, monkeypatch, [expired, Reply(text="fresh")])
    assert session.request("GET", "search.aspx", style="page").text == "fresh"
    assert session.sign_ins == 2  # first use, then the renewal


def test_unrenewable_session_raises_login_error(session, monkeypatch):
    script(session, monkeypatch, [Reply(text=LOGIN_PAGE)])
    with pytest.raises(LoginError):
        session.request("GET", "search.aspx", style="page")


def test_persistent_403_is_a_permission_error(session, monkeypatch):
    script(session, monkeypatch, [Reply(status=403)])
    with pytest.raises(ServerError) as info:
        session.request("POST", "usercreateddata.aspx", style="progress")
    assert info.value.status_code == 403


def test_server_error_carries_detail(session, monkeypatch):
    script(session, monkeypatch, [Reply(status=500, headers={"X-Error-Detail": "logged as 1"})])
    with pytest.raises(ServerError) as info:
        session.request("POST", "resolveLegacySelections.aspx", style="mode")
    assert info.value.status_code == 500 and info.value.detail == "logged as 1"


def test_network_failures_are_wrapped(session, monkeypatch):
    script(session, monkeypatch, [requests.ConnectionError("down")])
    with pytest.raises(NetworkError):
        session.request("GET", "search.aspx", style="page")


def test_concurrent_expiry_signs_in_once(session, monkeypatch):
    session.ensure_login()
    seen_expired = threading.Barrier(8)
    generation_at_start = session._generation

    def request(*args, **kwargs):
        if session._generation == generation_at_start:
            with contextlib.suppress(threading.BrokenBarrierError):
                seen_expired.wait(timeout=2)  # every worker sees the old session expire
            return Reply(status=403)
        return Reply(text="fresh")

    monkeypatch.setattr(session.http, "request", request)
    results = []
    threads = [threading.Thread(target=lambda: results.append(session.request("GET", "x", style="none").text)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["fresh"] * 8
    assert session.sign_ins == 2  # the initial sign-in and exactly one renewal


def test_params_styles(session):
    assert session.params("none") == {}
    assert session.params("page") == {"dsid": "U", "AppGroup": "DSAddin"}
    assert session.params("progress", host="unity") == {"dsid": "U", "AppGroup": "DSAddin", "host": "unity"}
    mode = session.params("mode", host="unity")
    assert mode["host"] == "unity" and mode["isLongname"] == "" and mode["dsid"] == "U"


def test_password_not_in_repr(session):
    assert "P" not in repr(session).replace("U", "")
