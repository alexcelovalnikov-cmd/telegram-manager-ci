"""Personal V18: the lightning is a forecast, never the financial ledger.

Legacy confirmed review metadata is bounded by its recorded review date, not
renewed at every sync. No ISO year is inferred from an imported MM.DD suffix.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BUSINESS_TIMEZONE = 'Asia/Yekaterinburg'
PROTOCOL = 'v18_payment_window'


def _day(value: object) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('full_iso_date_required')
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError('full_iso_date_required')
    return result


def _recorded_day(value: object, tz: str) -> date:
    if not isinstance(value, str):
        raise ValueError('review_timestamp_required')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timezone_required')
    return stamp.astimezone(ZoneInfo(tz)).date()


def forecast(status: str, data: dict, today: date | None = None) -> dict:
    """Return a display-only decision; does not set, clear or infer debt/payment."""
    result = {'suffix': '', 'reason': 'no_confirmed_window', 'needs_review': False}
    if status == 'paid':
        return dict(result, reason='paid')
    if status not in ('awaiting', 'partial'):
        return result
    tz = data.get('business_timezone') or BUSINESS_TIMEZONE
    try:
        now = today or datetime.now(ZoneInfo(tz)).date()
        window = data.get('payment_window')
        if isinstance(window, dict) and window.get('confirmed') is True:
            if window.get('kind') == 'relative':
                start = _recorded_day(window.get('confirmed_at'), tz)
                days = window.get('within_days')
                if type(days) is not int or not 0 <= days <= 14:
                    raise ValueError('invalid_horizon')
                end = start + timedelta(days=days)
            else:
                start, end = _day(window.get('start_date')), _day(window.get('end_date'))
            exact = window.get('exact_date')
        elif 'payment_window' in data:
            # Explicitly cleared or unconfirmed window never revives an older marker.
            return result
        else:
            legacy = data.get('payment_window_rule') or {}
            if not isinstance(legacy, dict) or legacy.get('lightning') is not True or legacy.get('rule_key') != 'payment_lightning_two_week_rule':
                return result
            start = _recorded_day(legacy.get('reviewed_at'), tz)
            end, exact = start + timedelta(days=14), None
        if start > end:
            raise ValueError('reversed_window')
        if end < now:
            return dict(result, reason='expired_window', needs_review=True)
        if exact:
            target = _day(exact)
            if not start <= target <= end:
                raise ValueError('exact_outside_window')
            if target < now:
                return dict(result, reason='expired_window', needs_review=True)
            if (target-now).days <= 14:
                return dict(result, suffix=' ⚡️ '+target.strftime('%m.%d'), reason='exact_payment_date')
            return dict(result, reason='outside_horizon')
        if (end-now).days <= 14:
            return dict(result, suffix=' ⚡️', reason='confirmed_payment_window')
        return dict(result, reason='outside_horizon')
    except (ValueError, TypeError, KeyError, OverflowError):
        return dict(result, reason='invalid_window', needs_review=True)


def project_display(task: dict, title: str, today: date | None = None) -> str:
    from tm_projects import strip_payment_suffix, display_title
    data = task.get('project_data') or {}
    if data.get('display_protocol') != PROTOCOL:
        return display_title(title, task.get('payment_status'), data.get('expected_payment_mmdd'))
    return strip_payment_suffix(title) + forecast(task.get('payment_status'), data, today)['suffix']


def personal_task(task: dict) -> dict:
    """Virtual read projection: no DB mutation until guarded native acknowledgement."""
    data = task.get('project_data') or {}
    return dict(task, _v18_metadata_pending=data.get('display_protocol') != PROTOCOL,
                project_data=dict(data, display_protocol=PROTOCOL, business_timezone=BUSINESS_TIMEZONE))
