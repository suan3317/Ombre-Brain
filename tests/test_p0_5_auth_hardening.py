"""P0-5 回归测试：/auth/setup 加固（5-A）、改密/恢复撤销 OAuth token（5-B）、
bucket_manager.update() 并发保护（5-D）。

5-A/5-B 用真实的 web._shared 状态（不 mock _is_setup_needed），因为要测的
恰恰是并发条件下这些真实状态转换是否互斥——mock 掉就测不出 TOCTOU 有没有
真的被堵住。
"""
import asyncio
import json

import pytest

import frontmatter

import web.auth as auth_web
import web.oauth as oauth_mod
from web import _shared as shared_web


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class FakeUrl:
    scheme = "https"
    netloc = "ombre.example"


class JsonRequest:
    def __init__(self, body=None, *, headers=None, client_host="127.0.0.1", cookies=None):
        self._body = {} if body is None else body
        self.headers = headers or {"content-type": "application/json", "host": "ombre.example"}
        self.url = FakeUrl()
        self.path_params = {}
        self.query_params = {}
        self.cookies = cookies or {}
        self.client = type("Client", (), {"host": client_host})()

    async def json(self):
        # 真实请求体解析是真的 await(读 socket)；这里补一个真实的让出点，
        # 不然两个并发请求在 asyncio.gather 下根本不会交错，锁测不测都一样
        # 会通过——之前就是这么误判"过了"的，见施工记录。
        await asyncio.sleep(0)
        return self._body


def _payload(response):
    return json.loads(response.body)


def _session_cookie(response) -> str:
    """从 /auth/setup 之类响应的 Set-Cookie 头里取出 ombre_session 的值，
    喂给后续请求的 .cookies——真实走一遍 cookie 会话，不 mock _require_auth。"""
    raw = response.headers["set-cookie"]
    # "ombre_session=<token>; HttpOnly; ..." — 只要第一个分号前的 k=v。
    first_pair = raw.split(";", 1)[0]
    _, _, value = first_pair.partition("=")
    return value


@pytest.fixture
def fresh_auth_routes(monkeypatch, tmp_path):
    """真实的 _shared 鉴权状态，buckets_dir 指向隔离的 tmp_path。"""
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr(auth_web, "_bootstrap_secret", None)
    # pytest-asyncio 给每个测试用例起一个独立事件循环；_setup_lock 是模块级
    # 单例，跨测试复用会撞上"lock bound to a different event loop"——生产
    # 环境不会有这个问题(整个进程生命周期只有一个事件循环)，这里每个测试
    # 换一把新锁只是为了消除测试间的循环隔离伪影。
    monkeypatch.setattr(auth_web, "_setup_lock", asyncio.Lock())
    monkeypatch.setattr(shared_web, "config", {"buckets_dir": str(tmp_path / "buckets")})
    shared_web._sessions.clear()
    shared_web._login_failures.clear()
    shared_web._login_locked_until.clear()
    oauth_mod._oauth_clients.clear()
    oauth_mod._oauth_codes.clear()
    oauth_mod._mcp_tokens.clear()
    oauth_mod._mcp_token_resources.clear()
    oauth_mod._mcp_token_family.clear()
    oauth_mod._mcp_refresh_tokens.clear()
    oauth_mod._used_refresh_token_families.clear()
    mcp = FakeMCP()
    auth_web.register(mcp)
    return mcp.routes


# --- 5-A: /auth/setup 加固 ---------------------------------------------------


class TestConcurrentSetupOnlyOneSucceeds:
    @pytest.mark.asyncio
    async def test_two_concurrent_setups_only_one_succeeds(self, fresh_auth_routes):
        setup = fresh_auth_routes[("POST", "/auth/setup")]
        assert shared_web._is_setup_needed() is True

        results = await asyncio.gather(
            setup(JsonRequest({"password": "first-password-123"})),
            setup(JsonRequest({"password": "second-password-456"})),
        )

        statuses = sorted(r.status_code for r in results)
        # 一个 200(真的建成密码+发会话)，一个 400(Already configured)——
        # 不能出现两个都是 200(两个"都成功"的会话)。
        assert statuses == [200, 400]
        assert shared_web._is_setup_needed() is False
        # 只有一份密码哈希落盘，且能用其中一个密码登录（不是两个都半success）。
        winner_password = None
        for req_password, response in zip(
            ["first-password-123", "second-password-456"], results
        ):
            if response.status_code == 200:
                winner_password = req_password
        assert winner_password is not None
        assert shared_web._verify_any_password(winner_password) is True

    @pytest.mark.asyncio
    async def test_setup_lock_actually_serializes_concurrent_requests(
        self, fresh_auth_routes
    ):
        """直接测锁本身：持锁期间发第二个请求，必须真的等到释放才完成——
        不是通过"结果凑巧对"反推,是直接证明临界区互斥生效。current 现状是
        parse→recheck→write 中间没有别的 await,所以上面那条按最终结果断言
        的测试即使去掉锁也会通过（recheck 单独就能挡住这个具体的交错顺序）；
        这条测试专门验证锁本身在挡，不依赖那个巧合。"""
        setup = fresh_auth_routes[("POST", "/auth/setup")]
        async with auth_web._setup_lock:
            second_done = asyncio.Event()

            async def call_setup():
                await setup(JsonRequest({"password": "held-out-password-1"}))
                second_done.set()

            task = asyncio.create_task(call_setup())
            await asyncio.sleep(0.05)
            assert not second_done.is_set(), "第二个 setup 请求不应该在锁被占用时完成"

        await task
        assert second_done.is_set()


class TestSetupLoopbackAndBootstrapSecret:
    @pytest.mark.asyncio
    async def test_loopback_setup_succeeds_without_secret(self, fresh_auth_routes):
        response = await fresh_auth_routes[("POST", "/auth/setup")](
            JsonRequest({"password": "loopback-password-123"}, client_host="127.0.0.1")
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_non_loopback_setup_rejected_without_secret(self, fresh_auth_routes):
        response = await fresh_auth_routes[("POST", "/auth/setup")](
            JsonRequest({"password": "remote-password-123"}, client_host="203.0.113.5")
        )
        assert response.status_code == 403
        assert shared_web._is_setup_needed() is True

    @pytest.mark.asyncio
    async def test_non_loopback_setup_succeeds_with_correct_secret(self, fresh_auth_routes):
        secret = auth_web._bootstrap_secret
        assert secret, "register() 应该已经生成过一次性 bootstrap secret"

        response = await fresh_auth_routes[("POST", "/auth/setup")](
            JsonRequest(
                {"password": "remote-password-123", "bootstrap_secret": secret},
                client_host="203.0.113.5",
            )
        )
        assert response.status_code == 200
        assert shared_web._is_setup_needed() is False

    @pytest.mark.asyncio
    async def test_bootstrap_secret_is_single_use(self, fresh_auth_routes):
        secret = auth_web._bootstrap_secret
        ok = await fresh_auth_routes[("POST", "/auth/setup")](
            JsonRequest(
                {"password": "remote-password-123", "bootstrap_secret": secret},
                client_host="203.0.113.5",
            )
        )
        assert ok.status_code == 200
        assert auth_web._bootstrap_secret is None

    @pytest.mark.asyncio
    async def test_non_loopback_setup_rejected_with_wrong_secret(self, fresh_auth_routes):
        response = await fresh_auth_routes[("POST", "/auth/setup")](
            JsonRequest(
                {"password": "remote-password-123", "bootstrap_secret": "totally-wrong"},
                client_host="203.0.113.5",
            )
        )
        assert response.status_code == 403
        assert shared_web._is_setup_needed() is True


# --- 5-B: 改密/恢复撤销全部 OAuth token --------------------------------------


class TestPasswordChangeRevokesTokens:
    @pytest.mark.asyncio
    async def test_change_password_revokes_existing_mcp_tokens(self, fresh_auth_routes):
        setup_resp = await fresh_auth_routes[("POST", "/auth/setup")](
            JsonRequest({"password": "original-password-1"})
        )
        assert setup_resp.status_code == 200

        access_token = oauth_mod._issue_mcp_access_token("https://ombre.example/mcp")
        assert oauth_mod._is_valid_mcp_token(access_token) is True

        session_token = _session_cookie(setup_resp)
        change_resp = await fresh_auth_routes[("POST", "/auth/change-password")](
            JsonRequest(
                {"current": "original-password-1", "new": "brand-new-password-2"},
                cookies={"ombre_session": session_token},
            )
        )
        assert change_resp.status_code == 200
        body = _payload(change_resp)
        assert body["revoked_mcp_tokens"] == 1
        assert body["mcp_revocation_persisted"] is True
        assert oauth_mod._is_valid_mcp_token(access_token) is False

    @pytest.mark.asyncio
    async def test_recover_revokes_existing_mcp_tokens(self, fresh_auth_routes, monkeypatch):
        setup_resp = await fresh_auth_routes[("POST", "/auth/setup")](
            JsonRequest({"password": "original-password-1"})
        )
        assert setup_resp.status_code == 200
        shared_web._save_security_qa("pet name?", "fluffy")

        access_token = oauth_mod._issue_mcp_access_token("https://ombre.example/mcp")
        assert oauth_mod._is_valid_mcp_token(access_token) is True

        recover_resp = await fresh_auth_routes[("POST", "/auth/recover")](
            JsonRequest({"answer": "fluffy", "new_password": "rescued-password-3"})
        )
        assert recover_resp.status_code == 200
        body = _payload(recover_resp)
        assert body["revoked_mcp_tokens"] == 1
        assert oauth_mod._is_valid_mcp_token(access_token) is False


class TestManualRevokeAllEndpoint:
    @pytest.mark.asyncio
    async def test_revoke_all_endpoint_requires_auth(self, monkeypatch, tmp_path):
        oauth_mod._oauth_clients.clear()
        oauth_mod._mcp_tokens.clear()
        monkeypatch.setattr(oauth_mod.sh, "config", {
            "buckets_dir": str(tmp_path / "buckets"),
            "mcp_require_auth": True,
        })
        mcp = FakeMCP()
        oauth_mod.register(mcp)

        response = await mcp.routes[("POST", "/oauth/revoke-all")](JsonRequest())
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_revoke_all_endpoint_clears_tokens_when_authenticated(
        self, monkeypatch, tmp_path
    ):
        oauth_mod._oauth_clients.clear()
        oauth_mod._mcp_tokens.clear()
        oauth_mod._mcp_refresh_tokens.clear()
        monkeypatch.setattr(oauth_mod.sh, "config", {
            "buckets_dir": str(tmp_path / "buckets"),
            "mcp_require_auth": True,
        })
        monkeypatch.setattr(oauth_mod.sh, "_require_auth", lambda _request: None)
        mcp = FakeMCP()
        oauth_mod.register(mcp)

        token = oauth_mod._issue_mcp_access_token("https://ombre.example/mcp")
        response = await mcp.routes[("POST", "/oauth/revoke-all")](JsonRequest())
        assert response.status_code == 200
        assert _payload(response)["revoked"] == 1
        assert oauth_mod._is_valid_mcp_token(token) is False


# --- 5-D: bucket_manager.update() 并发保护 -----------------------------------


class TestBucketUpdateConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_updates_on_different_fields_both_preserved(
        self, bucket_mgr, monkeypatch
    ):
        bucket_id = await bucket_mgr.create(content="hello world")

        real_count_anchors = bucket_mgr.count_anchors

        async def yielding_count_anchors():
            # 强制在 read 和 write 之间真的让出一次事件循环，稳定复现"两个
            # 并发 update() 交错、后写的覆盖先写的"这个竞态窗口——不依赖
            # list_all() 内部有没有恰好命中真正挂起的 I/O。
            await asyncio.sleep(0)
            return await real_count_anchors()

        monkeypatch.setattr(bucket_mgr, "count_anchors", yielding_count_anchors)

        results = await asyncio.gather(
            bucket_mgr.update(bucket_id, anchor=True),
            bucket_mgr.update(bucket_id, tags=["tag-a"]),
        )
        assert all(results), f"both updates should succeed: {results}"

        file_path = bucket_mgr._find_bucket_file(bucket_id)
        post = frontmatter.load(file_path)
        assert post.get("anchor") is True, "第一个 update 的字段被第二个覆盖丢失了"
        assert post.get("tags") == ["tag-a"], "第二个 update 的字段被第一个覆盖丢失了"

    @pytest.mark.asyncio
    async def test_same_bucket_updates_are_serialized_not_just_correct_by_luck(
        self, bucket_mgr
    ):
        """直接断言锁生效：同一个 bucket_id 两次 update() 期间，锁对象是同一个
        且第二次调用真的等到第一次释放才能拿到——不是通过最终文件状态反推。"""
        bucket_id = await bucket_mgr.create(content="hello world")
        lock = bucket_mgr._update_locks.setdefault(bucket_id, asyncio.Lock())
        assert not lock.locked()

        async with lock:
            assert lock.locked()
            second_acquired = asyncio.Event()

            async def try_update():
                await bucket_mgr.update(bucket_id, tags=["x"])
                second_acquired.set()

            task = asyncio.create_task(try_update())
            await asyncio.sleep(0.05)
            assert not second_acquired.is_set(), "第二次 update 不应该在第一次持锁期间完成"

        await task
        assert second_acquired.is_set()
