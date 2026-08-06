"""浮现记忆排序回归测试（任务书阶段1）。

F 窗口实测：权重 2.42、3.30 的条目排在权重 10.16 之前，低权重先吃掉 token
预算，结尾高权重条目被截断。根因：surface_default 选出候选集后做了
"top1 + 2~20 随机洗牌"（保留随机性/自发感的设计），但没有在渲染/分配
token_budget 前重新按权重降序排列，导致渲染顺序不等于权重顺序。

本测试用一个按 bucket 元数据里预置的固定分值打分的假 decay engine，
反复多个随机种子调用 surface_default，断言输出里的桶顺序始终严格按
权重降序——即使选取阶段的随机洗牌把顺序打乱过。
"""
import random
import re
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath.surface import surface_default


class ScoreByMetaDecay:
    """按 bucket meta 里手工塞的 _test_score 字段返回固定分数，可控制排序。"""

    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return float(meta.get("_test_score", 0.0))


def install_runtime(bucket_mgr):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = ScoreByMetaDecay()
    rt.dehydrator = None
    rt.embedding_engine = None
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


async def _make_bucket(bucket_mgr, score, importance=5):
    bid = await bucket_mgr.create(content=f"score={score} 测试记忆正文", importance=importance)
    fpath = bucket_mgr._find_bucket_file(bid)
    import frontmatter as fm
    post = fm.load(fpath)
    post["_test_score"] = score
    # 避免冷启动插队逻辑（importance>=8 且 activation_count==0）干扰本测试的排序断言
    post["activation_count"] = 1
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))
    return bid


@pytest.mark.asyncio
async def test_dynamic_results_strictly_descending_by_weight(bucket_mgr):
    install_runtime(bucket_mgr)

    # 复现 F 窗口实测的量级：2.42、3.30 混在 10.16 这种高权重条目旁边
    scores = [2.42, 3.30, 10.16, 5.0, 7.7, 1.1, 9.9]
    for s in scores:
        await _make_bucket(bucket_mgr, s)

    for seed in range(20):
        random.seed(seed)
        out = await surface_default(max_results=20, max_tokens=100000, tag_filter=[])
        found = [float(m) for m in re.findall(r"\[权重:([\d.]+)\]", out)]
        assert found, f"seed={seed} 没有解析到任何 [权重:x.xx] 条目"
        assert found == sorted(found, reverse=True), (
            f"seed={seed} 浮现记忆未按权重降序输出: {found}"
        )
