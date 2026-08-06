"""任务书阶段6：Ren 工程桶候选清单脚本测试。只读，不依赖真实生产数据——
构造合成桶文件验证关键词匹配、家庭/情感排除、以及脚本本身不写文件。"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from list_ren_engineering_buckets import scan  # noqa: E402


def _write_bucket(root, subdir, filename, frontmatter_lines, body):
    d = os.path.join(root, subdir)
    os.makedirs(d, exist_ok=True)
    text = "---\n" + "\n".join(frontmatter_lines) + "\n---\n" + body
    with open(os.path.join(d, filename), "w", encoding="utf-8") as f:
        f.write(text)


def _seed_vault(root):
    _write_bucket(
        root, "dynamic", "eng1.md",
        ["id: eng1abc123", "name: renshuo部署记录", "domain:", "- 编程", "- AI",
         "importance: 6", "type: dynamic", "resolved: false"],
        "今天在 renshuo.zeabur.app 上做了一次金丝雀发布，先切 10% 流量观察日志。",
    )
    _write_bucket(
        root, "dynamic", "eng2.md",
        ["id: eng2def456", "name: Ren部署复盘", "domain:", "- 编程",
         "importance: 4", "type: dynamic", "resolved: true", "digested: false"],
        "Ren 部署工程细节复盘：回滚了一次，金丝雀策略需要调整超时阈值。",
    )
    _write_bucket(
        root, "dynamic", "family1.md",
        ["id: fam1xyz789", "name: 和Ren的一次对话", "domain:", "- 家庭", "- 回忆",
         "importance: 7", "type: dynamic", "resolved: false"],
        "和Ren聊起小时候的事，聊到金丝雀这个词是因为他养过一只，不是部署的意思。",
    )
    _write_bucket(
        root, "dynamic", "feel1.md",
        ["id: feel1aaa", "name: Ren相关的感受", "domain:", "- 情绪",
         "importance: 5", "type: feel"],
        "想起renshuo那段日子还是有点感触。",
    )
    _write_bucket(
        root, "dynamic", "unrelated.md",
        ["id: unrel001", "name: 无关的一条记忆", "domain:", "- 工作",
         "importance: 5", "type: dynamic"],
        "今天开会讨论了下季度计划，跟这个人没关系。",
    )


def test_scan_finds_only_keyword_matching_buckets(tmp_path):
    _seed_vault(str(tmp_path))
    result = scan(str(tmp_path))
    all_ids = {r["id"] for r in result["engineering_candidates"]} | {
        r["id"] for r in result["needs_human_review_family_emotional"]
    }
    assert "unrel001" not in all_ids
    assert len(all_ids) == 4


def test_scan_separates_engineering_from_family_emotional(tmp_path):
    _seed_vault(str(tmp_path))
    result = scan(str(tmp_path))
    eng_ids = {r["id"] for r in result["engineering_candidates"]}
    review_ids = {r["id"] for r in result["needs_human_review_family_emotional"]}

    assert eng_ids == {"eng1abc123", "eng2def456"}
    assert review_ids == {"fam1xyz789", "feel1aaa"}


def test_scan_preserves_resolved_field_for_diagnostic(tmp_path):
    _seed_vault(str(tmp_path))
    result = scan(str(tmp_path))
    by_id = {r["id"]: r for r in result["engineering_candidates"]}
    assert by_id["eng1abc123"]["resolved"] is False
    assert by_id["eng2def456"]["resolved"] is True


def test_scan_is_strictly_read_only(tmp_path):
    _seed_vault(str(tmp_path))

    def _hash_tree():
        digest = {}
        for dirpath, _dirs, files in os.walk(str(tmp_path)):
            for fn in files:
                p = os.path.join(dirpath, fn)
                with open(p, "rb") as f:
                    digest[p] = hashlib.sha256(f.read()).hexdigest()
        return digest

    before = _hash_tree()
    scan(str(tmp_path))
    after = _hash_tree()
    assert before == after


def test_scan_handles_empty_vault(tmp_path):
    result = scan(str(tmp_path))
    assert result["engineering_candidates"] == []
    assert result["needs_human_review_family_emotional"] == []
    assert result["parse_errors"] == []
