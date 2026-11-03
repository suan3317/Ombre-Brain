"""
========================================
tools/plan/core.py — plan / letter_write / letter_read 实现
========================================

plan 桶记录我答应过、答应自己或想完成的事；letter 桶是她/他与 OB
之间的长信件。它们都是独立类型，永久保存、不衰减、不出现在普通
breath 中。

关键行为：
- plan_create：去重（同正文 + status=active 已存在 → 直接返回原 ID），
  写入 type=plan + status + weight + change_log 起点
- letter_write：原文永久保存，author 接受任意字符串署名（"ai" 或等于
  ai_name 时统一存为 ai_name 的值，其它字符串原样存为署名；"user" 为
  用户侧），写入 type=letter + author/title/letter_date 元数据
- letter_read：默认按时间倒序；带 query 时走向量近邻；支持 author /
  date_from / date_to 过滤；author 字段原样返回存储的署名，不做转换
- 时间锁（U-1 第 3 条）：letter_write 可带 lock_type/unlock_date；锁着的信
  对另一方只给"有一封锁到 X 日的信"，不进关键词/向量检索；过期的 timed 锁
  在读取时惰性归一化为 none；letter_lock_update 只改锁，只有锁主能改。
  锁的判定与解析全部在 lock.py。

不做什么（边界）：
- plan 不做向量去重，只做精确文本去重
- letter 永不合并、永不压缩、永不被衰减归档

对外暴露：plan_create / letter_write / letter_lock_update / letter_read
========================================
"""

import math
from typing import Optional

from .. import _runtime as rt
from .._common import check_content_size, check_metadata_size, check_query_size
from utils import strip_wikilinks, get_ai_name, t as _t
from .lock import (  # noqa: F401  (re-export for tests / web)
    PERMANENT_UNLOCK_DATE,
    author_side,
    letter_lock_state,
    locked_summary,
    normalize_lock_type,
    normalize_unlock_date,
    resolve_writer_name,
    safe_letter_metadata,
)


async def normalize_expired_lock(bucket: dict, state: dict) -> None:
    """timed 锁到期后把元数据收回 none，下次读就不用再算。失败不影响读取。"""
    if not state.get("expired"):
        return
    try:
        await rt.bucket_mgr.update(bucket["id"], lock_type="none", unlock_date=None)
    except Exception as e:
        rt.logger.warning(f"letter lock normalize failed: {e}")


async def plan_create(
    content: str,
    status: Optional[str] = "active",
    related_bucket: Optional[str] = "",
    weight: Optional[float] = 0.5,
    why_remembered: Optional[str] = "",
) -> str:
    if status is None:
        status = "active"
    if related_bucket is None:
        related_bucket = ""
    if weight is None:
        weight = 0.5
    if why_remembered is None:
        why_remembered = ""
    try:
        parsed_weight = float(weight)
    except (TypeError, ValueError, OverflowError):
        parsed_weight = 0.5
    if not math.isfinite(parsed_weight):
        parsed_weight = 0.5
    weight = max(0.0, min(1.0, parsed_weight))
    why_remembered = str(why_remembered).strip()[:500]
    await rt.decay_engine.ensure_started()
    if not content or not content.strip():
        return _t("内容为空，无法登记计划。", "Content is empty, nothing to register as a plan.")
    size_err = check_content_size(content)
    if size_err:
        return size_err
    metadata_err = check_metadata_size(
        status=status,
        related_bucket=related_bucket,
        why_remembered=why_remembered,
    )
    if metadata_err:
        return metadata_err
    status = status.strip().lower()
    if status not in ("active", "resolved", "abandoned"):
        status = "active"

    norm = content.strip()
    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
        for b in all_buckets:
            m = b.get("metadata", {})
            if (
                m.get("type") == "plan"
                and m.get("status", "active") == "active"
                and (b.get("content") or "").strip() == norm
            ):
                return _t(
                    f"跟原有 active plan 完全重复→{b['id']}（未重复登记）",
                    f"Exact duplicate of an existing active plan → {b['id']} (not registered again)",
                )
    except Exception as e:
        rt.logger.warning(f"plan() dedup scan failed: {e}")

    bucket_id = await rt.bucket_mgr.create(
        content=content.strip(),
        tags=["__plan__"],
        importance=7,
        domain=["plan"],
        valence=0.5,
        arousal=0.4,
        name=None,
        bucket_type="plan",
        why_remembered=why_remembered,
        weight=weight,
        source_tool="plan",
    )
    from .._common import append_plan_change_log
    initial_log = append_plan_change_log([], "created", to=status)
    update_kwargs = {"status": status, "change_log": initial_log}
    if related_bucket.strip():
        update_kwargs["related_bucket"] = related_bucket.strip()
    try:
        await rt.bucket_mgr.update(bucket_id, **update_kwargs)
    except Exception as e:
        rt.logger.warning(f"plan() failed to set status/related: {e}")
    # 注意：bucket_mgr.create() 已在 content 落盘后投递 embedding outbox
    # 向量，这里不需要也不应该重复调用 generate_and_store（见 hold/feel.py 同类注释）。
    return f"📋plan→{bucket_id} [{status}]"


async def letter_write(
    author: str,
    content: str,
    user_name: Optional[str] = "",
    title: Optional[str] = "",
    date: Optional[str] = "",
    ai_name: Optional[str] = "",
    lock_type: Optional[str] = "none",
    unlock_date: Optional[str] = "",
) -> str:
    if user_name is None:
        user_name = ""
    if title is None:
        title = ""
    if date is None:
        date = ""
    try:
        normalized_lock = normalize_lock_type(lock_type)
        normalized_unlock = normalize_unlock_date(normalized_lock, unlock_date)
    except ValueError as exc:
        return _t(f"无法写带锁的信：{exc}", f"Can't write a locked letter: {exc}")
    # ai_name：显式传入优先，否则取环境变量 AI_NAME（回退 "AI"）。
    ai = (ai_name or "").strip() or get_ai_name()
    if not author or not author.strip():
        return _t("author 不能为空。", "author can't be empty.")
    if not content or not content.strip():
        return _t("信件内容不能为空。", "Letter content can't be empty.")
    size_err = check_content_size(content)
    if size_err:
        return size_err
    metadata_err = check_metadata_size(
        author=author,
        user_name=user_name,
        title=title,
        date=date,
        ai_name=ai_name,
    )
    if metadata_err:
        return metadata_err

    # 署名归一化：
    #   - "user" → 用户侧，存 "user"（用户名另存 user_name，逻辑不变）
    #   - "ai" / 等于 ai_name / 旧值 "claude"（历史兼容）→ 统一存 ai_name 的值
    #   - 其它任意字符串 → 原样作为署名
    raw = author.strip()
    low = raw.lower()
    if low == "user":
        a = "user"
    elif low in ("ai", "claude") or raw == ai:
        a = ai
    else:
        a = raw

    # 锁属于写信的一方。MCP 入口的调用者是 AI，所以锁主固定是 "ai"；
    # 署名成 user 的代存信不能上锁——那把锁会变成 AI 替她锁她自己的信。
    if normalized_lock != "none" and author_side(raw, ai_name=ai) == "human":
        return _t(
            "无法写带锁的信：MCP 入口不能替对方写锁信（无锁代存照常可用）。",
            "Can't write a locked letter: the MCP entry can't lock a letter on the other side's behalf (unlocked proxy letters still work).",
        )
    writer_name = resolve_writer_name("ai", author=raw, user_name=user_name, ai_name=ai_name) or a

    extra_meta = {"author": a}
    if user_name.strip():
        extra_meta["user_name"] = user_name.strip()
    if title.strip():
        extra_meta["title"] = title.strip()[:120]
    if date.strip():
        extra_meta["letter_date"] = date.strip()

    # 锁元数据随 create 原子落盘（bucket_manager 只对 letter 类型接受这组参数），
    # 不走 create→update 的窗口：并发读或向量检索可能在窗口里看到裸正文。
    bucket_id = await rt.bucket_mgr.create(
        content=content.strip(),
        tags=["__letter__"],
        importance=10,
        domain=["letter"],
        valence=0.5,
        arousal=0.3,
        name=(title.strip()[:60] or f"{a}_{date.strip() or 'letter'}"),
        bucket_type="letter",
        source_tool="letter",
        lock_type=normalized_lock,
        unlock_date=normalized_unlock,
        locked_by="ai",
        writer_name=writer_name,
    )
    try:
        await rt.bucket_mgr.update(bucket_id, **extra_meta)
    except Exception as e:
        rt.logger.warning(f"letter_write update meta failed: {e}")
    # 注意：bucket_mgr.create() 已在 content 落盘后投递 embedding outbox
    # 向量，这里不需要也不应该重复调用 generate_and_store。
    if normalized_lock != "none":
        lock_suffix = f" 🔒{normalized_lock}"
        if normalized_lock == "timed":
            lock_suffix += _t(f" 解锁:{normalized_unlock}", f" unlocks:{normalized_unlock}")
        return f"💌letter→{bucket_id} [{a}]{lock_suffix}"
    return f"💌letter→{bucket_id} [{a}]"


async def letter_lock_update(
    letter_id: str,
    lock_type: str,
    unlock_date: Optional[str] = "",
    *,
    caller_side: str = "ai",
) -> str:
    """只改锁元数据；caller_side 由可信入口给（MCP=ai，Dashboard=human）。"""
    if not letter_id or not letter_id.strip():
        return _t("letter_id 不能为空", "letter_id is required")
    try:
        normalized_lock = normalize_lock_type(lock_type)
        normalized_unlock = normalize_unlock_date(normalized_lock, unlock_date)
    except ValueError as exc:
        return _t(f"无法修改信的锁：{exc}", f"Can't change the letter lock: {exc}")
    bucket = await rt.bucket_mgr.get(letter_id.strip())
    if not bucket or (bucket.get("metadata") or {}).get("type") != "letter":
        return _t("未找到这封信", "Letter not found")
    state = letter_lock_state(bucket, caller_side)
    if not state["locked_by"]:
        return _t(
            "这是一封没有锁主的老信（锁功能之前写的），不能事后补锁；要锁请新写一封。",
            "This letter predates locks and has no lock owner; write a new locked letter instead.",
        )
    if not state["owner"]:
        return _t("只有写这封信的一方能改它的锁。", "Only the side that wrote this letter can change its lock.")
    ok = await rt.bucket_mgr.update(
        bucket["id"], lock_type=normalized_lock, unlock_date=normalized_unlock,
    )
    if not ok:
        return _t("信的锁状态修改失败", "Failed to update the letter lock")
    if normalized_lock == "none":
        return _t(f"🔓 已解锁 {bucket['id']}，恢复双方可读。", f"🔓 Unlocked {bucket['id']}; readable by both sides again.")
    if normalized_lock == "permanent":
        return _t(f"🔒 已将 {bucket['id']} 设为永久封存。", f"🔒 {bucket['id']} is now permanently sealed.")
    return _t(
        f"🔒 已将 {bucket['id']} 设为定时锁，解锁时刻：{normalized_unlock}。",
        f"🔒 {bucket['id']} is now time-locked until {normalized_unlock}.",
    )


async def letter_read(
    query: Optional[str] = "",
    limit: Optional[int] = 10,
    author: Optional[str] = "",
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    if query is None:
        query = ""
    if limit is None:
        limit = 10
    if author is None:
        author = ""
    if date_from is None:
        date_from = ""
    if date_to is None:
        date_to = ""
    query_err = check_query_size(query)
    if query_err:
        return query_err
    metadata_err = check_metadata_size(
        author=author,
        date_from=date_from,
        date_to=date_to,
    )
    if metadata_err:
        return metadata_err
    try:
        limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError, OverflowError):
        limit = 10
    try:
        all_b = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        return _t(f"读取信件失败: {e}", f"Failed to read letters: {e}")
    letters = [b for b in all_b if b["metadata"].get("type") == "letter"]
    # 锁：MCP 入口的读者是 AI。锁着的（另一方写的、未到期的）信只留一行存在
    # 感，不进关键词/向量检索；到期的 timed 锁顺手收回 none。
    states = {b["id"]: letter_lock_state(b, "ai") for b in letters}
    for bucket in letters:
        await normalize_expired_lock(bucket, states[bucket["id"]])
    visible_ids = {b["id"] for b in letters if not states[b["id"]]["locked"]}
    af = author.strip()
    if af:
        ai = get_ai_name()
        af_low = af.lower()
        if af_low == "user":
            letters = [b for b in letters if b["metadata"].get("author") == "user"]
        elif af_low in ("ai", "claude") or af == ai:
            # AI 侧：匹配新署名 ai_name + 历史遗留的 "claude"
            ai_aliases = {ai, "claude"}
            letters = [b for b in letters if b["metadata"].get("author") in ai_aliases]
        else:
            # 任意自定义署名：精确匹配存储值
            letters = [b for b in letters if b["metadata"].get("author") == af]

    def _within(b):
        d = b["metadata"].get("letter_date") or b["metadata"].get("created", "")
        if date_from and d and d < date_from:
            return False
        if date_to and d and d > date_to:
            return False
        return True

    letters = [b for b in letters if _within(b)]

    query_text = query.strip()
    if query_text:
        # 锁着的信连"被搜到"都不行：标题/正文都不该泄露它和 query 有关。
        letters = [b for b in letters if b["id"] in visible_ids]

    def _matches_query(b):
        if not query_text:
            return True
        meta = b.get("metadata", {})
        parts = [
            b.get("content", ""),
            str(meta.get("name") or ""),
            str(meta.get("title") or ""),
            str(meta.get("author") or ""),
        ]
        parts.extend(str(t) for t in (meta.get("tags") or []))
        return query_text.lower() in "\n".join(parts).lower()

    if query_text and rt.embedding_engine and getattr(rt.embedding_engine, "enabled", False):
        try:
            # 锁着的信在向量排序之前就排除，不是排完再过滤。
            sims = await rt.embedding_engine.search_similar(
                query_text, top_k=limit * 3, allowed_bucket_ids=set(visible_ids),
            )
            id_score = {bid: sc for bid, sc in sims}
            vector_matches = [b for b in letters if b["id"] in id_score]
            if vector_matches:
                letters = vector_matches
                letters.sort(key=lambda b: id_score.get(b["id"], 0.0), reverse=True)
            else:
                letters = [b for b in letters if _matches_query(b)]
                letters.sort(key=lambda b: b["metadata"].get("letter_date") or b["metadata"].get("created", ""), reverse=True)
        except Exception as e:
            rt.logger.warning(f"letter_read vector search failed: {e}")
            letters = [b for b in letters if _matches_query(b)]
            letters.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
    else:
        if query_text:
            letters = [b for b in letters if _matches_query(b)]
        letters.sort(key=lambda b: b["metadata"].get("letter_date") or b["metadata"].get("created", ""), reverse=True)

    letters = letters[:limit]
    if not letters:
        return _t("没有找到匹配的信件。", "No matching letters found.")
    parts = []
    for b in letters:
        m = b["metadata"]
        state = states[b["id"]]
        if state["locked"]:
            parts.append(locked_summary(b, state))
            continue
        a = m.get("author", "?")
        d = (m.get("letter_date") or m.get("created", ""))[:10]
        title = m.get("title") or m.get("name", "")
        parts.append(
            f"[{b['id']}] {a} · {d}{(' · ' + title) if title else ''}\n"
            + strip_wikilinks(b["content"])
        )
    return _t("=== 信件 ===\n", "=== Letters ===\n") + "\n\n---\n\n".join(parts)
