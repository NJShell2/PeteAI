"""Small shared helpers for the agent and the browser agent.

Lives in its own module so both can import it: ``agent`` and ``browser_agent``
already pull each other in indirectly through ``browser_session``, and putting
this in either one would create an import cycle.
"""
from datetime import datetime


def current_date_note() -> str:
    """A sentence telling the model what today actually is.

    The models served here are locally-hosted checkpoints whose training data
    stops well before the present, so asked "what's today's date" they answer
    with whatever date they last saw -- confidently, and wrong. Suppressing that
    matters most for anything date-dependent: weather, moon phases, schedules,
    deadlines, "tomorrow". It also stops the worse failure mode where the model
    silently reasons over a stale calendar and never mentions a date at all.

    Deliberately not part of ``settings.system_prompt``: that field is
    user-editable and persisted, so a date written there would freeze at the
    moment it was saved. This is rebuilt on every request instead.
    """
    now = datetime.now().astimezone()
    zone = now.strftime("%Z") or "local time"
    return (
        f"Today's date is {now.strftime('%A, %B %d, %Y')} and the current time is "
        f"{now.strftime('%I:%M %p')} {zone}. Treat this as the present whenever a "
        "question depends on the current date or time. Do not rely on your training "
        "data for dates, and never state a current date that differs from this one.\n"
    )