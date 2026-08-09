"""
========================================
tools/_wake_seed.py — wake 继承区选取（纯函数，v3 Commit A）
========================================

seed:true 的桶显式进入 wake 的"继承区"段，不受 importance 阈值限制
（seed:true + importance:4 也必须可见，见设计定稿推论四 + 验收 4b）。
继承区跟核心记忆段（select_core_memory_buckets 的 importance>=8 筛选）是
两套完全独立的选取逻辑——种子的价值不在"重要"，在"给下一个空白实例的
入场券"，混进同一套筛选条件会把两个概念绑死。

不做什么（边界）：
- 不判定要不要在别处出现（核心记忆段/最近连续性段的去重由调用方拿本函数
  返回的 id 集合去过滤，见 server.py _wake_impl）
- 不做任何 I/O、不依赖 bucket_mgr/rt——脱离 server.py 独立单测

对外暴露：select_seed_buckets(all_buckets) -> list[dict]
========================================
"""

from utils import parse_bool


def select_seed_buckets(all_buckets: list) -> list:
    """筛出 seed:true 的桶，按 importance 降序、created 升序排列。

    排序理由：继承区是给下一个实例看"这个人已经内化了什么"的清单，越重要
    的越靠前；同重要度按立种子的时间先后排（先圈定的种子先列），纯粹为了
    稳定顺序，不承载任何衰减/年龄语义。
    """
    seeds = [
        b for b in all_buckets
        if parse_bool((b.get("metadata") or {}).get("seed"), default=False)
    ]

    def _key(bucket: dict):
        meta = bucket.get("metadata") or {}
        try:
            importance = int(meta.get("importance") or 0)
        except (TypeError, ValueError):
            importance = 0
        return (-importance, str(meta.get("created") or ""))

    return sorted(seeds, key=_key)
