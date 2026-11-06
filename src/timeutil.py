"""Time helpers. All internal times are timezone-aware UTC datetimes."""
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def ts(s):
    """Parse an ISO-8601 string ('...Z') to an aware datetime. Empty -> None."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(UTC)


def iso(d):
    if d is None:
        return None
    return d.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def ist_day(d):
    return d.astimezone(IST).date().isoformat()


def in_quiet_hours(d):
    """Quiet hours are 22:00-08:00 IST (messaging_policy.md rule 2)."""
    h = d.astimezone(IST).hour
    return h >= 22 or h < 8


def next_ist_time(after, hour, minute=0):
    """The next IST wall-clock time hour:minute strictly after `after`, on a later IST day."""
    local = after.astimezone(IST)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=1)
    return target.astimezone(UTC)


def true_time(e):
    """Best estimate of when an event really happened.

    received_at is set by our servers and is trustworthy. occurred_at and sent_at come from the phone clock,
    which can be off by hours, but their *difference* is still meaningful: it is how long the phone held the
    event before sending it (offline buffering). So true time = received_at - (sent_at - occurred_at).
    """
    r = ts(e['received_at'])
    s, o = ts(e.get('sent_at')), ts(e.get('occurred_at'))
    if s is None or o is None:
        return r
    held = s - o
    if held.total_seconds() < 0:  # clock went backwards between the two readings; do not trust it
        return r
    return r - held


def buffered_seconds(e):
    """How long the phone held the event before sending it (sent_at - occurred_at, same clock)."""
    s, o = ts(e.get('sent_at')), ts(e.get('occurred_at'))
    if s is None or o is None:
        return 0.0
    return max(0.0, (s - o).total_seconds())
