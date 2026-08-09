"""
反代下 Dashboard 登录误报回归（移植自上游 8904f47，按本仓库 dashboard.html 的
简化鉴权结构改写：无 node，用静态源码断言代替 upstream 的 node 沙箱执行）。
"""

from pathlib import Path

DASHBOARD = Path("frontend/dashboard.html")


def _html() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def test_login_does_not_hide_overlay_on_bare_resp_ok():
    html = _html()
    login_start = html.index("async function doLogin()")
    login_end = html.index("async function doLogout()", login_start)
    login_src = html[login_start:login_end]

    # 反向代理可能吞掉 Set-Cookie：resp.ok 不能直接当作"已登录"，必须回读会话。
    assert "verifyDashboardSessionEstablished" in login_src
    assert "credentials: 'same-origin'" in login_src
    assert "try {" in login_src and "catch (_)" in login_src


def test_setup_and_recover_also_verify_session_before_hiding_overlay():
    html = _html()
    for fn in ("async function doSetup()", "async function doRecover()"):
        start = html.index(fn)
        end = html.index("\n}\n", start)
        src = html[start:end]
        assert "verifyDashboardSessionEstablished" in src


def test_verify_session_established_rechecks_auth_status_before_clearing_overlay():
    html = _html()
    start = html.index("async function verifyDashboardSessionEstablished()")
    end = html.index("\n}\n", start)
    src = html[start:end]

    assert "/auth/status" in src
    assert "data.authenticated === true" in src
    assert "未建立登录会话" in src


def test_auth_error_reads_backend_error_field_not_detail():
    html = _html()
    # 历史 bug：doLogin 读 d.detail，但 /auth/login 失败时返回的是 {"error": ...}，
    # 导致真实失败原因（如限流提示）被吞掉、误报成固定的"密码错误"。
    login_start = html.index("async function doLogin()")
    login_end = html.index("async function doLogout()", login_start)
    login_src = html[login_start:login_end]
    assert "d.detail" not in login_src

    read_failure_start = html.index("async function readAuthFailure(resp, fallback)")
    read_failure_end = html.index("\n}\n", read_failure_start)
    read_failure_src = html[read_failure_start:read_failure_end]
    assert "data.error" in read_failure_src
    assert "反向代理未返回 OB 的 JSON 响应" in read_failure_src
