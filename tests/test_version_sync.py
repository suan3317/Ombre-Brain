"""返修单一号 1b：Dashboard 版本号显示源修复。

根因：get_version()（utils.py）故意 src/VERSION 优先于根目录 VERSION 读取
——这是为热更新设计的，有意为之（热更新只覆盖 src/，很多用户根目录
VERSION 是装机时的旧值，改成根目录优先会导致版本号在热更新后倒退，
2.3.10 真出过这个回归）。do-update（web/meta.py）在解压后会显式把 zip
里的根 VERSION 强制写到 <root>/VERSION 与 <root>/src/VERSION 两处，
所以线上热更新链路本身没问题。

真正的 bug 是本地开发流程的疏漏：2.6.18-2.6.24 这几次 stage 提交只 bump
了根目录 VERSION，忘了同步 src/VERSION，导致 src/VERSION 停在 2.6.17、
Dashboard（经 get_version() 读 src/VERSION 优先）显示的版本号跟实际运行
的代码对不上。修法不是把 get_version() 改成根目录优先（那会重新引入
2.3.10 那个回归），而是把两个 VERSION 文件的值锁死一致——这条测试就是
那把锁：任何一次只 bump 一个 VERSION 文件而漏了另一个，CI 立刻炸。
"""
import os

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read_version(rel_path: str) -> str:
    with open(os.path.join(_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read().strip()


def test_root_and_src_version_files_stay_in_sync():
    root_version = _read_version("VERSION")
    src_version = _read_version("src/VERSION")
    assert root_version == src_version, (
        f"根目录 VERSION({root_version}) 与 src/VERSION({src_version}) 不一致——"
        "发版/每次 bump 必须两处一起改，否则 Dashboard 显示的版本号会跟实际代码脱节。"
    )


def test_get_version_reflects_root_version_when_synced(monkeypatch):
    import sys
    sys.path.insert(0, os.path.join(_ROOT, "src"))
    import importlib
    import utils as utils_mod
    importlib.reload(utils_mod)

    root_version = _read_version("VERSION")
    assert utils_mod.get_version() == root_version
