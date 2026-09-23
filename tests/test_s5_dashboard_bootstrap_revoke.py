"""
S-5：Dashboard 两个缺口的前端回归（沿用本仓库前端测试惯例：无 node，
对 frontend/dashboard.html 做静态源码断言，验证"能发出正确请求"的代码路径）。

1. 首次 setup 表单带 bootstrap_secret 输入框，非回环访问时显示，随 password 一起
   POST /auth/setup（后端 src/web/auth.py 已支持，读 body["bootstrap_secret"]）。
2. MCP 鉴权块里的"断开全部连接器"按钮 → POST /oauth/revoke-all，成功后显示撤销数量
   并提示 claude.ai 侧连接器需重新授权。
"""

from pathlib import Path

DASHBOARD = Path("frontend/dashboard.html")


def _html() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _fn_src(html: str, header: str) -> str:
    start = html.index(header)
    end = html.index("\n}\n", start)
    return html[start:end]


def test_setup_form_posts_bootstrap_secret_with_password_for_non_loopback():
    html = _html()

    # 输入框存在，默认隐藏，放在 setup 表单里
    form_start = html.index('id="auth-setup-form"')
    form_end = html.index('id="auth-login-form"', form_start)
    form = html[form_start:form_end]
    assert 'id="auth-setup-bootstrap-wrap"' in form
    assert 'id="auth-setup-bootstrap"' in form
    wrap_tag = form[form.index('id="auth-setup-bootstrap-wrap"'):]
    wrap_tag = wrap_tag[: wrap_tag.index(">")]
    assert "display:none" in wrap_tag

    # checkAuth：setup_needed 时按 host 是否回环决定显示
    check_src = _fn_src(html, "async function checkAuth()")
    assert "auth-setup-bootstrap-wrap" in check_src
    assert "isLoopbackHost()" in check_src

    loopback_src = _fn_src(html, "function isLoopbackHost()")
    for host in ("'localhost'", "'127.0.0.1'", "'::1'"):
        assert host in loopback_src

    # doSetup：secret 随 password 同一请求体 POST /auth/setup
    setup_src = _fn_src(html, "async function doSetup()")
    assert "getElementById('auth-setup-bootstrap')" in setup_src
    assert "const setupBody = { password: p1 }" in setup_src
    assert "setupBody.bootstrap_secret = bootstrap" in setup_src
    assert (
        "fetch('/auth/setup', { method: 'POST', headers: {'Content-Type':'application/json'}, "
        "body: JSON.stringify(setupBody) })" in setup_src
    )
    # 历史写法（只发 password）不能残留
    assert "JSON.stringify({password: p1})" not in setup_src
    # 后端 403（非回环且 secret 缺失/错误）时强制亮出输入框，不让用户卡死
    assert "resp.status === 403" in setup_src


def test_revoke_all_connectors_button_posts_to_oauth_revoke_all():
    html = _html()

    # 按钮放在 MCP 鉴权块内（MCP 鉴权标题之后、服务端口块之前）
    auth_block_start = html.index(
        '<div style="font-weight:600;font-size:13px;margin-bottom:8px;">MCP 鉴权</div>'
    )
    auth_block_end = html.index("<!-- 服务端口 -->", auth_block_start)
    block = html[auth_block_start:auth_block_end]
    assert 'id="mcp-revoke-all-btn"' in block
    assert 'onclick="revokeAllConnectors()"' in block
    assert 'id="mcp-revoke-all-msg"' in block
    assert "断开全部连接器" in block

    # 请求：走带 401 处理的 authFetch，POST 现有端点 /oauth/revoke-all
    src = _fn_src(html, "async function revokeAllConnectors()")
    assert "authFetch('/oauth/revoke-all', { method: 'POST' })" in src
    # 二次确认，防误触
    assert "confirm(" in src
    # 成功：显示后端返回的撤销数量 + claude.ai 侧需重新授权提示
    assert "result.revoked" in src
    assert "claude.ai 侧连接器需重新授权" in src
    # 非 OAuth 模式后端返回 404，要有人话提示而不是解析失败
    assert "res.status === 404" in src
