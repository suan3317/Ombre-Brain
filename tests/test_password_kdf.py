"""密码 / 安全问题答案哈希：PBKDF2 + 旧格式兼容 + 登录静默升级。

对应安全加固 #5：历史用单轮 salt:sha256hex，auth 文件泄露即离线爆破。
改用 PBKDF2-HMAC-SHA256；旧格式仍能校验，并在校验成功时升级到新格式。
"""
import hashlib

import pytest

import web._shared as sh


@pytest.fixture
def auth_dir(tmp_path, monkeypatch):
    monkeypatch.setitem(sh.config, "buckets_dir", str(tmp_path))
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    return tmp_path


def _legacy_hash(secret: str) -> str:
    salt = "deadbeefdeadbeefdeadbeefdeadbeef"
    h = hashlib.sha256(f"{salt}:{secret}".encode()).hexdigest()
    return f"{salt}:{h}"


def test_new_hash_is_pbkdf2_format():
    stored = sh._hash_secret("hunter2")
    assert stored.startswith("pbkdf2_sha256$")
    parts = stored.split("$")
    assert len(parts) == 4 and int(parts[1]) >= 200_000


def test_pbkdf2_roundtrip():
    stored = sh._hash_secret("correct horse")
    assert sh._verify_secret("correct horse", stored)
    assert not sh._verify_secret("wrong horse", stored)


def test_legacy_hash_still_verifies():
    stored = _legacy_hash("oldpass")
    assert sh._verify_secret("oldpass", stored)
    assert not sh._verify_secret("nope", stored)


def test_needs_rehash_detects_legacy_and_weak():
    assert sh._needs_rehash(_legacy_hash("x")) is True
    assert sh._needs_rehash("") is True
    assert sh._needs_rehash(f"pbkdf2_sha256$1000$aa$bb") is True  # 迭代数过低
    assert sh._needs_rehash(sh._hash_secret("x")) is False


def test_password_save_and_verify_uses_pbkdf2(auth_dir):
    sh._save_password_hash("s3cret!")
    stored = sh._load_password_hash()
    assert stored.startswith("pbkdf2_sha256$")
    assert sh._verify_any_password("s3cret!")
    assert not sh._verify_any_password("bad")


def test_login_upgrades_legacy_hash(auth_dir):
    # 手写一个旧格式 auth 文件
    import json
    legacy = _legacy_hash("legacypw")
    (auth_dir / ".dashboard_auth.json").write_text(
        json.dumps({"password_hash": legacy}), encoding="utf-8"
    )
    assert sh._load_password_hash() == legacy
    # 用旧密码登录成功
    assert sh._verify_any_password("legacypw")
    # 成功后应已静默升级为 PBKDF2
    upgraded = sh._load_password_hash()
    assert upgraded.startswith("pbkdf2_sha256$")
    assert sh._verify_any_password("legacypw")


def test_security_answer_pbkdf2_and_legacy(auth_dir):
    sh._save_security_qa("你的城市？", "  ShangHai  ")
    # 答案归一化（strip+lower）后校验
    assert sh._verify_security_answer("shanghai")
    assert not sh._verify_security_answer("beijing")
    stored = sh._load_auth_data().get("security_answer_hash", "")
    assert stored.startswith("pbkdf2_sha256$")
