"""The vocabulary a queued job uses to say what it is currently doing.

Why this exists
---------------
A print job can sit in the queue for minutes before anything is sent to the
printer: relay power control switches the mains on, waits for the device to
boot, waits again for it to report itself ready, and only then hands the job
over. From the outside all of that looked identical to a job that was simply
waiting its turn -- ``status: "queued"``, nothing else -- so the queue appeared
to be doing nothing for a minute at a time.

This module names the phases so the queue can say which one it is in.

Activity is not a status
------------------------
``status`` keeps exactly the five values the API has always documented
(``queued``, ``printing``, ``done``, ``failed``, ``cancelled``). Adding a sixth,
or redefining ``queued`` to mean "queued, or possibly waiting for a printer to
boot", would break every client that already switches on it. The activity is a
*detail alongside* the status: null for a job that is genuinely just waiting its
turn, and one of the tokens below for a job something is actively happening to.

Two fields, on purpose:

``activity``
    A stable token from :data:`JOB_ACTIVITIES`, for a client that wants to
    branch -- pick an icon, decide whether to show a spinner.
``activity_message``
    A sentence for a human, which may name the concrete number of seconds
    involved and therefore is *not* stable enough to switch on. The producer
    supplies it; :data:`ACTIVITY_MESSAGES` is only the fallback for a caller
    that passes a token and nothing else.

Reading is not consuming
------------------------
The activity is plain state on the job, not an event: the UI polls, and a value
that disappeared after the first read would flicker to nothing between two
polls of a job that had not changed. It survives being read any number of times
and changes only when the job moves to another phase, which is also what lets
``GET /jobs`` and ``GET /jobs/queue`` -- two endpoints reading the same job --
agree with each other.
"""

# The relay is being told to switch the printer's mains supply on.
ACTIVITY_SWITCHING_ON = "switching_on"

# The webhook has gone out and the app is waiting for the printer to appear on
# the network. Covers both halves of that wait: the deliberate pause before the
# first probe, and the probing itself.
ACTIVITY_WAITING_FOR_PRINTER = "waiting_for_printer"

# The printer is answering, and is being given a moment to settle before a job
# is pushed at it. Reported separately from the wait because it is the phase
# that otherwise looks broken: the printer is plainly up, and the app is
# deliberately still not printing.
ACTIVITY_PRINTER_SETTLING = "printer_settling"

# A print attempt failed and the next one is pending. Only ever reported for a
# printer this app has just switched on: a job printed at a device that was
# already up either works or fails, and quietly trying again would hide a real
# fault. Separate from the settle above because "the printer refused a raster"
# and "the printer has not been asked yet" are different things to be told.
ACTIVITY_RETRYING = "retrying"

# The job is on the wire.
ACTIVITY_PRINTING = "printing"

# Every token, in the order a job passes through them. This is the set the
# OpenAPI enum declares, and the set a client may assume it will ever see.
JOB_ACTIVITIES = (
    ACTIVITY_SWITCHING_ON,
    ACTIVITY_WAITING_FOR_PRINTER,
    ACTIVITY_PRINTER_SETTLING,
    ACTIVITY_PRINTING,
    ACTIVITY_RETRYING,
)

# Fallback wording, used when a producer reports a token without a message.
# Deliberately free of numbers: anything that quotes a duration has to come from
# whoever owns that duration, or the two drift apart.
ACTIVITY_MESSAGES = {
    ACTIVITY_SWITCHING_ON: "Switching the printer on at the relay.",
    ACTIVITY_WAITING_FOR_PRINTER: "Waiting for the printer to come up.",
    ACTIVITY_PRINTER_SETTLING:
        "The printer is answering; letting it settle before printing.",
    ACTIVITY_PRINTING: "Printing.",
    ACTIVITY_RETRYING: "The print did not go through; trying again.",
}


def activity_message(activity, message=None):
    """Return the message to report for an activity.

    Args:
        activity: One of :data:`JOB_ACTIVITIES`, or None.
        message: An explicit message from the producer, which wins when given.

    Returns:
        The message, or None when there is no activity to describe.
    """
    if not activity:
        return None
    if message:
        return str(message)
    return ACTIVITY_MESSAGES.get(activity)
