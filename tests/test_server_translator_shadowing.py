"""生产事故回归：server.py 里给 MCP 工具描述打 AI_NAME 身份标签的 for 循环
（`if __name__ == "__main__":` 块内，AI_NAME 设置时才跑）用 `_t` 当循环变量，
跟 `from utils import t as _t` 撞了同一个模块级名字——循环跑完后模块级 `_t`
被永久覆盖成 `mcp._tool_manager._tools.values()` 里最后一个 Tool 对象，
所有依赖 `_t(...)` 双语选择器的代码（file_save 的 _fz_save 等）全部
TypeError: 'Tool' object is not callable。

修法：双语选择器在 server.py 里改名 `_tr`（全文件替换，其他文件不动），
不再跟任何东西同名；工具打标签那个循环变量维持原名 `_t`，反正没人再
指望它是翻译器了。

这个文件锁两层：
1. 静态 AST 扫描——不管选择器以后叫什么名字，文件里任何 for 循环都不准
   把循环变量取跟选择器同名，防止未来又撞一次（比“测出错误信息”更早
   一步拦住，回到 import 时就能测出结构性隐患）。
2. 运行时——选择器（如果模块里存在同名对象）必须是 callable 且返回 str；
   外加 _fz_save 的 append 分支（原始报错的确切代码路径）端到端跑一遍。
"""
import ast
import os

import pytest

_SERVER_PY_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "server.py")


def _for_loop_targets(node) -> list[str]:
    names: list[str] = []
    if isinstance(node, ast.Name):
        names.append(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            names.extend(_for_loop_targets(elt))
    return names


def test_server_py_translator_alias_not_shadowed_by_any_for_loop():
    """核心回归：`from utils import t as ...` 的别名，不准在文件任意位置
    被某个 for 循环拿来当循环变量用——这正是原始事故的根因形状。"""
    with open(_SERVER_PY_PATH, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=_SERVER_PY_PATH)

    translator_alias = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "utils":
            for alias in node.names:
                if alias.name == "t":
                    translator_alias = alias.asname or alias.name
    assert translator_alias, "server.py 应该有 `from utils import t as ...`"
    # 事故当时撞的名字是 _t；修完之后选择器应该已经改名，不再是 _t。
    assert translator_alias != "_t", (
        "双语选择器不该再叫 _t——这个名字已经被 AI_NAME 工具打标签循环占用，"
        "改回 _t 会重新撞上 2026-09 生产事故同一个坑"
    )

    colliding_lines = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            if translator_alias in _for_loop_targets(node.target):
                colliding_lines.append(node.lineno)

    assert not colliding_lines, (
        f"server.py 里有 for 循环把循环变量取名 {translator_alias!r}，"
        f"会覆盖同名的双语选择器（行号: {colliding_lines}）"
    )


def test_server_module_translator_if_present_is_callable_and_returns_str(monkeypatch):
    """字面按工单要求：server.py 模块命名空间里，不管翻译选择器现在叫
    `_t` 还是别的名字，只要这个名字存在，就必须是 callable 且调用后
    返回 str——不能是一个被别处覆盖掉的非函数对象。"""
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    import server as srv

    checked_any = False
    for name in ("_t", "_tr"):
        if hasattr(srv, name):
            checked_any = True
            fn = getattr(srv, name)
            assert callable(fn), f"server.{name} 存在但不是 callable（疑似被同名变量覆盖）"
            result = fn("中文", "english")
            assert isinstance(result, str), f"server.{name}(...) 必须返回 str"
    assert checked_any, "server.py 里应该能找到双语选择器（_t 或改名后的 _tr）"


@pytest.mark.asyncio
async def test_fz_save_append_branch_does_not_raise_type_error(tmp_path, monkeypatch):
    """原始报错的确切代码路径：_fz_save(..., append=True) 里
    `verb = _t("追加到", "appended to")`。这里先写一次、再 append 一次，
    确认不会再抛 TypeError: 'Tool' object is not callable。"""
    monkeypatch.delenv("OMBRE_LANG", raising=False)
    import server as srv

    monkeypatch.setattr(srv, "config", {"buckets_dir": str(tmp_path)}, raising=False)

    await srv._fz_save("shadow_regression.md", "第一行", append=False)
    appended = await srv._fz_save("shadow_regression.md", "第二行", append=True)

    assert isinstance(appended, str)
    assert "追加到" in appended or "appended to" in appended
