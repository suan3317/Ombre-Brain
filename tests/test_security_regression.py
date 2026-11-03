import io
import tarfile
import zipfile

import pytest

import web.hooks as hooks_mod
import web.oauth as oauth_mod
import web.ollama_local as ollama_mod
from web import _shared as shared_web
from web.request_limits import ManagementRequestBodyLimitMiddleware


class DummyRequest:
    def __init__(self, *, headers=None, query_params=None, cookies=None):
        self.headers = headers or {}
        self.query_params = query_params or {}
        self.cookies = cookies or {}


class JsonBodyRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


@pytest.mark.asyncio
async def test_shared_json_object_boundary_rejects_top_level_array():
    with pytest.raises(ValueError, match="JSON body must be an object"):
        await shared_web._read_json_object(JsonBodyRequest([]))

    assert await shared_web._read_json_object(JsonBodyRequest({"ok": True})) == {
        "ok": True
    }


def test_hook_requests_are_not_public_by_default(monkeypatch):
    monkeypatch.delenv("OMBRE_HOOK_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_HOOK_ALLOW_PUBLIC", raising=False)
    hooks_mod.sh.config = {"hooks": {}}

    assert hooks_mod._is_hook_request_authorized(DummyRequest()) is False


def test_hook_requests_accept_configured_token(monkeypatch):
    monkeypatch.setenv("OMBRE_HOOK_TOKEN", "secret-token")
    monkeypatch.delenv("OMBRE_HOOK_ALLOW_PUBLIC", raising=False)
    hooks_mod.sh.config = {"hooks": {}}

    assert hooks_mod._is_hook_request_authorized(
        DummyRequest(query_params={"token": "secret-token"})
    ) is True
    assert hooks_mod._is_hook_request_authorized(
        DummyRequest(headers={"x-ombre-hook-token": "secret-token"})
    ) is True
    assert hooks_mod._is_hook_request_authorized(
        DummyRequest(headers={"authorization": "Bearer secret-token"})
    ) is True
    assert hooks_mod._is_hook_request_authorized(
        DummyRequest(query_params={"token": "wrong-token"})
    ) is False


def test_hook_requests_can_be_explicitly_public(monkeypatch):
    monkeypatch.delenv("OMBRE_HOOK_TOKEN", raising=False)
    monkeypatch.setenv("OMBRE_HOOK_ALLOW_PUBLIC", "1")
    hooks_mod.sh.config = {"hooks": {}}

    assert hooks_mod._is_hook_request_authorized(DummyRequest()) is True


def test_oauth_authorize_rejects_unknown_client_redirect():
    oauth_mod._oauth_clients.clear()

    ok, error = oauth_mod._validate_authorize_redirect(
        "unknown-client",
        "https://attacker.example/callback",
    )

    assert ok is False
    assert "client_id" in error


def test_oauth_authorize_requires_exact_registered_redirect():
    oauth_mod._oauth_clients.clear()
    oauth_mod._oauth_clients["client-1"] = {
        "redirect_uris": ["https://legit.example/callback"],
        "client_name": "Legit",
    }

    ok, _ = oauth_mod._validate_authorize_redirect(
        "client-1",
        "https://legit.example/callback",
    )
    bad_ok, bad_error = oauth_mod._validate_authorize_redirect(
        "client-1",
        "https://attacker.example/callback",
    )

    assert ok is True
    assert bad_ok is False
    assert "redirect_uri" in bad_error


def test_safe_zip_extract_rejects_path_traversal(tmp_path):
    dest = tmp_path / "extract"
    dest.mkdir()
    outside = tmp_path / "escape.txt"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../escape.txt", "owned")
    buf.seek(0)

    with zipfile.ZipFile(buf) as zf:
        with pytest.raises(ValueError):
            ollama_mod._safe_extract_zip(zf, str(dest))

    assert not outside.exists()


def test_safe_tar_extract_rejects_path_traversal(tmp_path):
    dest = tmp_path / "extract"
    dest.mkdir()
    outside = tmp_path / "escape.txt"
    buf = io.BytesIO()
    payload = b"owned"
    info = tarfile.TarInfo("../escape.txt")
    info.size = len(payload)
    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.addfile(info, io.BytesIO(payload))
    buf.seek(0)

    with tarfile.open(fileobj=buf, mode="r") as tf:
        with pytest.raises(ValueError):
            ollama_mod._safe_extract_tar(tf, str(dest))

    assert not outside.exists()


def test_ollama_download_rejects_non_http_url():
    with pytest.raises(ValueError):
        ollama_mod._validate_download_url("file:///tmp/ollama.tar.zst")


def test_ollama_download_allows_trusted_hosts(monkeypatch):
    monkeypatch.delenv("OMBRE_ALLOW_UNTRUSTED_MIRROR", raising=False)
    for url in (
        "https://ollama.com/download/OllamaSetup.exe",
        "https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst",
        "https://objects.githubusercontent.com/foo/ollama-linux-amd64.tar.zst",
    ):
        assert ollama_mod._validate_download_url(url) == url


def test_ollama_download_rejects_untrusted_host_by_default(monkeypatch):
    monkeypatch.delenv("OMBRE_ALLOW_UNTRUSTED_MIRROR", raising=False)
    with pytest.raises(ValueError):
        ollama_mod._validate_download_url("https://evil.attacker.example/OllamaSetup.exe")
    # 相似域名混淆也必须拒绝（后缀匹配不能被 github.com.evil.com 骗过）
    with pytest.raises(ValueError):
        ollama_mod._validate_download_url("https://github.com.evil.example/OllamaSetup.exe")


def test_ollama_download_untrusted_host_allowed_via_optin(monkeypatch):
    monkeypatch.setenv("OMBRE_ALLOW_UNTRUSTED_MIRROR", "1")
    url = "https://ghproxy.mycorp.internal/ollama/releases/ollama-linux-amd64.tar.zst"
    assert ollama_mod._validate_download_url(url) == url


def test_ollama_host_trust_matcher():
    assert ollama_mod._host_is_trusted("ollama.com")
    assert ollama_mod._host_is_trusted("objects.githubusercontent.com")
    assert ollama_mod._host_is_trusted("GitHub.com")
    assert not ollama_mod._host_is_trusted("github.com.evil.example")
    assert not ollama_mod._host_is_trusted("notgithub.com")
    assert not ollama_mod._host_is_trusted("")


# --- C1：下载产物完整性校验（执行/解压前挡住错误页/损坏文件）---

def test_artifact_verify_rejects_too_small(tmp_path):
    p = tmp_path / "OllamaSetup.exe"
    p.write_bytes(b"MZ" + b"\x00" * 100)  # 头对但太小
    with pytest.raises(RuntimeError):
        ollama_mod._verify_downloaded_artifact(str(p), "windows")


def test_artifact_verify_rejects_wrong_magic(tmp_path):
    p = tmp_path / "OllamaSetup.exe"
    p.write_bytes(b"<html>404 Not Found</html>" + b"\x00" * (200 * 1024))  # 够大但头不对（HTML 错误页）
    with pytest.raises(RuntimeError):
        ollama_mod._verify_downloaded_artifact(str(p), "windows")


def test_artifact_verify_accepts_valid(tmp_path):
    big = b"\x00" * (200 * 1024)
    cases = {
        "windows": b"MZ" + big,
        "linux": b"\x28\xB5\x2F\xFD" + big,
        "macos": b"PK\x03\x04" + big,
    }
    for osk, data in cases.items():
        p = tmp_path / f"art_{osk}"
        p.write_bytes(data)
        ollama_mod._verify_downloaded_artifact(str(p), osk)  # 不抛即通过


@pytest.mark.asyncio
async def test_management_body_limit_rejects_public_auth_oversize():
    calls = []
    sent = []

    async def app(_scope, _receive, send):
        calls.append("called")
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = ManagementRequestBodyLimitMiddleware(app, max_bytes=10)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "headers": [(b"content-length", b"11")],
        },
        receive,
        send,
    )

    assert calls == []
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_chunked_limit_cannot_be_swallowed_by_route_json_handler():
    calls = []
    sent = []

    async def app(_scope, receive, send):
        calls.append("called")
        try:
            while (await receive()).get("more_body", False):
                pass
        except Exception:
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"invalid JSON"})

    messages = iter([
        {"type": "http.request", "body": b"123456", "more_body": True},
        {"type": "http.request", "body": b"789012", "more_body": False},
    ])

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    middleware = ManagementRequestBodyLimitMiddleware(app, max_bytes=10)
    await middleware(
        {"type": "http", "method": "POST", "path": "/auth/login", "headers": []},
        receive,
        send,
    )

    assert calls == []
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_bounded_body_replay_delegates_to_real_disconnect_after_body():
    observed = []
    messages = iter([
        {"type": "http.request", "body": b"{}", "more_body": False},
        {"type": "http.disconnect"},
    ])

    async def receive():
        return next(messages)

    async def send(_message):
        return None

    async def app(_scope, receive, _send):
        observed.append(await receive())
        observed.append(await receive())

    middleware = ManagementRequestBodyLimitMiddleware(app, max_bytes=10)
    await middleware(
        {"type": "http", "method": "POST", "path": "/auth/login", "headers": []},
        receive,
        send,
    )

    assert observed[0]["body"] == b"{}"
    assert observed[1]["type"] == "http.disconnect"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/api/import/preflight", "/api/import/upload", "/api/migrate/upload"],
)
async def test_management_body_limit_preserves_large_upload_routes(path):
    calls = []
    sent = []

    async def app(scope, _receive, send):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = ManagementRequestBodyLimitMiddleware(app, max_bytes=10)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"content-length", b"1000")],
        },
        receive,
        send,
    )

    assert calls == [path]
    assert sent[0]["status"] == 204


# ============================================================ A-1 追加 3:相册上传 12MB

async def _run_management_limit(path, *, body_len, max_bytes, chunk=256 * 1024, **kw):
    """用真实分块 body(不靠 Content-Length)走一遍 ManagementRequestBodyLimitMiddleware。"""
    calls = []
    sent = []

    async def app(scope, receive, send):
        got = 0
        while True:
            m = await receive()
            got += len(m.get("body", b""))
            if not m.get("more_body", False):
                break
        calls.append((scope["path"], got))
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = ManagementRequestBodyLimitMiddleware(app, max_bytes=max_bytes, **kw)
    remaining = body_len

    async def receive():
        nonlocal remaining
        n = min(chunk, remaining)
        remaining -= n
        return {"type": "http.request", "body": b"x" * n, "more_body": remaining > 0}

    async def send(message):
        sent.append(message)

    await middleware({"type": "http", "method": "POST", "path": path, "headers": []}, receive, send)
    return calls, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/album", "/api/album/ingest"])
async def test_album_upload_paths_accept_4_5mb_under_4mb_management_limit(path):
    """Rio 11-03 的 413:4.5MB 图走 ingest 要能过,普通管理上限 4MB 不动。"""
    four_mb = 4 * 1024 * 1024
    body = int(4.5 * 1024 * 1024)
    calls, sent = await _run_management_limit(path, body_len=body, max_bytes=four_mb)
    assert calls == [(path, body)]
    assert sent[0]["status"] == 201


@pytest.mark.asyncio
async def test_album_upload_paths_still_reject_above_12mb():
    four_mb = 4 * 1024 * 1024
    calls, sent = await _run_management_limit("/api/album/ingest", body_len=13 * 1024 * 1024, max_bytes=four_mb)
    assert calls == []
    assert sent[0]["status"] == 413
    assert b"12582912" in sent[1]["body"]


@pytest.mark.asyncio
async def test_other_management_routes_keep_4mb_limit():
    """放宽只对相册两条路径;PATCH /api/album/{id}、/auth/login 等仍是 4MB。"""
    four_mb = 4 * 1024 * 1024
    for path in ("/auth/login", "/api/album/abc123", "/api/letters"):
        calls, sent = await _run_management_limit(path, body_len=int(4.5 * 1024 * 1024), max_bytes=four_mb)
        assert calls == [], path
        assert sent[0]["status"] == 413, path
        assert b"4194304" in sent[1]["body"], path


@pytest.mark.asyncio
async def test_album_limit_never_tighter_than_management_limit():
    """配置把相册值调得比管理上限还小,只回落到管理上限,不会把相册卡得更紧。"""
    calls, sent = await _run_management_limit(
        "/api/album", body_len=3000, max_bytes=4096, max_album_upload_bytes=1024
    )
    assert calls == [("/api/album", 3000)]
    assert sent[0]["status"] == 201
    # 管理上限 0 = 不限,相册路径也跟着不限
    calls, sent = await _run_management_limit(
        "/api/album/ingest", body_len=13 * 1024 * 1024, max_bytes=0
    )
    assert calls == [("/api/album/ingest", 13 * 1024 * 1024)]
    assert sent[0]["status"] == 201

