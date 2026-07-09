"""list_all() 活跃桶缓存回归测试（性能 P1）。

缓存必须：命中返回、写操作后失效、touch 就地更新、返回副本不污染缓存。
不能改变任何可见语义（桶数/内容/元数据都要与直读磁盘一致）。
"""
import pytest


@pytest.mark.asyncio
async def test_cache_hit_after_first_list(bucket_mgr):
    await bucket_mgr.create(content="内容一号一号一号", name="一", domain=["测试"])
    assert bucket_mgr._active_cache is None
    first = await bucket_mgr.list_all()
    assert bucket_mgr._active_cache is not None  # 建了缓存
    second = await bucket_mgr.list_all()
    assert len(first) == len(second) == 1


@pytest.mark.asyncio
async def test_write_invalidates_cache(bucket_mgr):
    await bucket_mgr.create(content="内容一号一号一号", name="一", domain=["测试"])
    await bucket_mgr.list_all()
    assert bucket_mgr._active_cache is not None
    # 新建 → 集合变了 → 缓存作废
    await bucket_mgr.create(content="内容二号二号二号", name="二", domain=["测试"])
    assert bucket_mgr._active_cache is None
    again = await bucket_mgr.list_all()
    assert len(again) == 2   # 反映了新桶


@pytest.mark.asyncio
async def test_returned_list_is_copy_not_cache(bucket_mgr):
    await bucket_mgr.create(content="内容", name="一", domain=["测试"])
    got = await bucket_mgr.list_all()
    got[0]["score"] = 123          # 调用方在返回对象上写顶层键
    got[0]["vector_match"] = True
    fresh = await bucket_mgr.list_all()   # 走缓存
    assert "score" not in fresh[0]        # 不该污染缓存
    assert "vector_match" not in fresh[0]


@pytest.mark.asyncio
async def test_touch_updates_cache_in_place(bucket_mgr):
    bid = await bucket_mgr.create(content="内容一号一号一号", name="一", domain=["测试"])
    await bucket_mgr.list_all()  # 建缓存
    before = next(b for b in bucket_mgr._active_cache if b["id"] == bid)
    before_count = float(before["metadata"].get("activation_count") or 0)
    await bucket_mgr.touch(bid)
    after = next(b for b in bucket_mgr._active_cache if b["id"] == bid)
    assert float(after["metadata"].get("activation_count") or 0) == before_count + 1
    assert after["metadata"].get("last_active")


@pytest.mark.asyncio
async def test_cache_matches_disk_after_delete(bucket_mgr):
    b1 = await bucket_mgr.create(content="留下的内容啊啊啊", name="留", domain=["测试"])
    b2 = await bucket_mgr.create(content="删掉的内容哦哦哦", name="删", domain=["测试"])
    await bucket_mgr.list_all()
    await bucket_mgr.delete(b2)   # 软删 → 失效缓存
    active = await bucket_mgr.list_all()
    ids = {b["id"] for b in active}
    assert b1 in ids and b2 not in ids
