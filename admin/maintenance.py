"""Pace background upstream calls and reserve idle account identities."""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
import threading
import time

from admin.config import settings
from admin.pool import POOL

_batch_lock = threading.Lock()
_state_lock = threading.Lock()
_last_finished = None
_skips = Counter()


def _reason(account):
    if POOL.waf.active():
        return "waf"
    if account.status != "active":
        return "inactive"
    now = datetime.utcnow()
    if any(getattr(account, field, None) and getattr(account, field) > now
           for field in ("cool_until", "breaker_until", "degrade_until")):
        return "account_cooling"
    from admin.routers.proxy import _looks_login_dead
    if _looks_login_dead(account, now):
        return "login_required"
    return None


#: 「登录态失效」的统一人话文案。跳过原因与任务失败原因都引用这一份，
#: 同一个结论在哪个页面都写成同一句话 —— 用户据此决定要不要重登。
LOGIN_DEAD_TEXT = "登录态已失效，需在客户端重新登录后导入"

#: 刷新/鉴权返回里代表「服务端会话已被删除」的标记 —— OAuth 标准语义，
#: 属**终态**：命中即无重试价值。与 `_classify_error()` 的文案判定、
#: 保活探测的判据同源，改口径时只改这一处。
_AUTH_DEAD_MARKERS = (
    "invalid_grant",
    "offline user session not found",
    "refresh token failed",
    "session not found",
)


#: _reason() / account_slot() 的原因码 → 界面文案。各调用方（后台批量刷新、
#: 权益领取）共用一份，避免同一个原因在不同页面被描述成不同的话。
#: 不翻译的话，多种原因会被压成一句泛泛的描述，其中最需要用户行动的
#: 「登录态失效」往往不在那句话里。
_SKIP_REASON_TEXT = {
    "waf": "出口 IP 正被上游 WAF 拦截，暂时保护中",
    "inactive": "账号已停用",
    "account_cooling": "账号处于冷却 / 熔断 / 降权期",
    "login_required": LOGIN_DEAD_TEXT,
    "background_busy": "后台批量任务正在运行",
    "inflight": "账号在途占用已满",
}


def skip_reason_text(reason, action: str = "已跳过") -> str:
    """把跳过原因码翻成给人看的文案；未知码原样带出，不吞掉信息。"""
    if not reason:
        return f"账号本轮不可用，{action}"
    text = _SKIP_REASON_TEXT.get(reason)
    if text is None:
        return f"账号不可用（{reason}），{action}"
    return f"{text}，{action}"


@contextmanager
def account_slot(account, db=None):
    """Skip busy/restricted accounts; release reservations even on failure.

    The lock and lease are process local, matching the account pool. No waits
    are imposed on conversations. Background work skips a contested identity.
    """
    global _last_finished
    reason = _reason(account)
    locked = False
    reserved = False
    uid = account.uid or ""
    try:
        if reason is None:
            locked = _batch_lock.acquire(blocking=False)
            if not locked:
                reason = "background_busy"
        if reason is None:
            with _state_lock:
                last = _last_finished
            gap = min(60.0, max(0.0, settings.KEEPALIVE_ACCOUNT_GAP))
            if last is not None:
                remaining = gap - (time.monotonic() - last)
                if remaining > 0:
                    time.sleep(remaining)
            reason = _reason(account)
        if reason is None:
            # Pace before reserving, so an idle gap does not take capacity away
            # from conversations. Foreground arrivals win while we are waiting.
            reserved = POOL.inflight.acquire_maintenance(uid)
            if not reserved:
                reason = "inflight"
        if reason is None:
            if db is not None:
                # Reload after reserving: credentials or account state may have
                # changed while the batch waited or processed another identity.
                db.refresh(account)
            reason = _reason(account)
        if reason:
            with _state_lock:
                _skips[reason] += 1
        yield reason
    finally:
        if reserved:
            with _state_lock:
                _last_finished = time.monotonic()
            POOL.inflight.release_maintenance(uid)
        if locked:
            _batch_lock.release()


def note_failure(db, account, error):
    """后台任务失败的统一入口：分类落库，并返回 `_classify_error` 的分类。

    除了 WAF / 限流策略，**「登录态失效」也必须在这里落一笔结论**（见
    `note_dead_session()`）。少了这一笔，坏号在号池和选号眼里仍是健康号。
    """
    from admin.routers.proxy import _apply_account_policy, _classify_error, _extract_http_status
    message = str(error)
    status = _extract_http_status(message)
    kind = _classify_error(status, message)
    if kind in ("waf", "soft_rate", "account_fault"):
        from admin.model_limits import reset_from_body
        _apply_account_policy(db, account, kind, status, message,
                              reset_at=reset_from_body(message))
    elif kind == "session_dead":
        note_dead_session(db, account, error)
    return kind


def note_dead_session(db, account, error) -> bool:
    """把「上游拒绝登录态」落到账号行；确属失效时返回 True。

    为什么后台任务也要写这一笔：`last_err_msg` 里的结论是**自锁门控** ——
    `_looks_login_dead()` 只认它，选号与后台批量都先按它排除账号。
    签到 / 余额刷新 / 积分快照 / 成长任务原来只把上游原文写进「运行结果」，
    账号行一个字都没动，于是坏号在界面上依旧是健康号：每天被重新选中、
    重新失败一次（实测 3 个号的签到连着两天报的都是同一段 HTML 401，
    而号池里看不出任何异常）。

    判定口径与网关请求路径（`_apply_account_policy`）保持一致：
    HTTP 401/403 属**权威**结论 —— token 被吊销/被换掉就是彻底不可用了，
    重试多少次都一样 → 一次即禁用；12153 一类文案式抖动按阈值累计，
    一次就禁用等于误杀健康号。
    """
    from admin.routers.proxy import _classify_error, _extract_http_status
    message = str(error)
    status = _extract_http_status(message)
    if _classify_error(status, message) != "session_dead":
        return False
    account.session_dead_fails = (account.session_dead_fails or 0) + 1
    account.cool_kind = "session_dead"
    account.last_err_at = datetime.utcnow()
    authoritative = status in (401, 403) or any(
        m in message.lower() for m in _AUTH_DEAD_MARKERS)
    if authoritative:
        account.last_err_msg = (
            f"登录态被上游拒绝（HTTP {status}），需重新登录：{message[:180]}"
        )[:255]
    else:
        account.last_err_msg = (message or "登录态失效")[:255]
    threshold = 1 if authoritative else max(1, settings.SESSION_DEAD_THRESHOLD)
    if account.session_dead_fails >= threshold:
        account.status = "disabled"
        account.cool_until = None
        account.breaker_until = None
        account.degrade_until = None
    db.commit()
    return True


def snapshot():
    with _state_lock:
        skipped = dict(_skips)
    return {"scope": "process_local", "skipped_since_start": skipped,
            "reserved_accounts": POOL.inflight.maintenance_count(),
            "account_gap_seconds": min(60.0, max(0.0, settings.KEEPALIVE_ACCOUNT_GAP))}
