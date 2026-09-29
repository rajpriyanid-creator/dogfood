"""Event status derivation.

An event's status is always computed from server-side timestamps
compared against the server's own clock — never from a client-supplied
"is_open" flag or similar. This is what makes deadline enforcement real
rather than decorative.

The lifecycle is one explicit, ordered state machine:

    UPCOMING -> OPEN -> SUBMISSIONS_CLOSED -> JUDGING -> PUBLISHED

with ARCHIVED as a terminal state an organizer can set independently
(not modeled by dates - see app.py's publish/archive routes). Every
other date-derived permission (can submit, can score, can publish) is
implemented as "is the event currently in state X", so there is exactly
one place that maps timestamps to states, and every other check is a
lookup against that single result rather than its own ad hoc date
comparison.
"""
from datetime import datetime, timezone

UPCOMING = "upcoming"
OPEN = "open"
SUBMISSIONS_CLOSED = "submissions_closed"
JUDGING = "judging"
PUBLISHED = "published"
ARCHIVED = "archived"

_ORDER = [UPCOMING, OPEN, SUBMISSIONS_CLOSED, JUDGING, PUBLISHED, ARCHIVED]


def parse_iso(ts: str):
    if ts is None:
        return None
    ts = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        raise ValueError("Timestamp must include a timezone offset (e.g. Z or +00:00)")
    return dt


def event_status(event_row) -> str:
    """One state, always derived the same way:

    - ARCHIVED is an explicit, sticky publish_state value an organizer
      sets; it is not a function of dates at all.
    - PUBLISHED likewise reflects the stored publish_state once an
      organizer has actually published results, regardless of what the
      judging_close date says (publishing is an organizer action, not a
      clock event).
    - Otherwise: UPCOMING before start_at, OPEN from start_at until
      submissions_close, SUBMISSIONS_CLOSED from submissions_close until
      judging_close, and JUDGING from judging_close onward until
      published. An event with no judging_close is considered to stay
      in SUBMISSIONS_CLOSED (there's no further date to advance it) until
      an organizer publishes it.
    """
    if event_row["publish_state"] == "archived":
        return ARCHIVED
    if event_row["publish_state"] == "published":
        return PUBLISHED

    now = datetime.now(timezone.utc)
    start = parse_iso(event_row["start_at"]) if event_row["start_at"] else None
    close = parse_iso(event_row["submissions_close"])
    judging_close = parse_iso(event_row["judging_close"]) if event_row["judging_close"] else None

    if start and now < start:
        return UPCOMING
    if now < close:
        return OPEN
    if judging_close and now < judging_close:
        return SUBMISSIONS_CLOSED
    if judging_close and now >= judging_close:
        return JUDGING
    return SUBMISSIONS_CLOSED


def submissions_are_open(event_row) -> bool:
    """A draft -> submitted transition (and a draft edit) is allowed
    only while the event is OPEN."""
    return event_status(event_row) == OPEN


def scoring_is_open(event_row) -> bool:
    """A judge may create or update a score only while the event is in
    SUBMISSIONS_CLOSED or JUDGING - not before submissions close (there
    is nothing to judge yet) and not once results are PUBLISHED or the
    event is ARCHIVED (the audited P0 'judging state enforcement'
    requirement: scoring must not remain open forever)."""
    return event_status(event_row) in (SUBMISSIONS_CLOSED, JUDGING)


def can_run_normalization(event_row) -> bool:
    """Normalization is allowed only before publication, after submissions close."""
    return event_status(event_row) in (SUBMISSIONS_CLOSED, JUDGING)

def configuration_is_frozen(event_row) -> bool:
    """Tracks, prizes, judges, rubric, and assignments cannot be modified
    once judging starts."""
    return event_status(event_row) in (JUDGING, PUBLISHED, ARCHIVED)
