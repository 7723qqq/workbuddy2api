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
    """Let background WAF/rate observations protect the conversation pool too."""
    from admin.routers.proxy import _apply_account_policy, _classify_error, _extract_http_status
    message = str(error)
    status = _extract_http_status(message)
    kind = _classify_error(status, message)
    if kind in ("waf", "soft_rate", "account_fault"):
        from admin.model_limits import reset_from_body
        _apply_account_policy(db, account, kind, status, message,
                              reset_at=reset_from_body(message))
    return kind


def snapshot():
    with _state_lock:
        skipped = dict(_skips)
    return {"scope": "process_local", "skipped_since_start": skipped,
            "reserved_accounts": POOL.inflight.maintenance_count(),
            "account_gap_seconds": min(60.0, max(0.0, settings.KEEPALIVE_ACCOUNT_GAP))}
