"""Live display metadata for the public model list; model IDs stay stable."""
from datetime import datetime
import logging

from admin.model_busy import LABELS, normalize_response

_logger = logging.getLogger(__name__)


def enrich_entries(session, entries, on_error=None):
    """Read official capacity on every listing, using the catalog's identity.

    Only request models already filtered by the caller's permissions. Capacity
    failures must not hide the catalog or turn stale observations into live data.
    """
    ids = sorted({entry["id"] for entry in entries})
    levels = {model: "unknown" for model in ids}
    observed_at = None
    stale = False
    if ids:
        try:
            fresh = {}
            for offset in range(0, len(ids), 50):
                batch = ids[offset:offset + 50]
                fresh.update(normalize_response(batch, session.fetch_model_busy(batch)))
            levels = fresh
            observed_at = datetime.utcnow().isoformat() + "Z"
        except Exception as error:
            stale = True
            _logger.warning("模型列表繁忙状态获取失败，返回未知状态")
            if on_error is not None:
                on_error(error)
    result = []
    for entry in entries:
        level = levels[entry["id"]]
        multiplier = entry.get("credit_multiplier")
        credit_label = f"×{multiplier:g}" if multiplier is not None else "×未知"
        result.append({**entry,
                       "name": f'{entry["id"]} · {credit_label} · {LABELS[level]}',
                       "busy_level": level, "busy_label": LABELS[level],
                       "busy_observed_at": observed_at, "busy_stale": stale})
    return result
