"""Work-item lifecycle: statuses, event types, and the transition function.

Status is a projection of the event log. Every change goes through
:func:`decide`, which either returns the next status or raises. Nothing else
in the codebase is allowed to pick a status.

Two kinds of events exist:

* **Commands** (``commit``, ``start``, ``block`` …) come from a person or an
  agent. A command issued from the wrong status is an error.
* **Facts** (``github.closed``, ``github.reopened`` …) report something that
  already happened elsewhere. A fact never fails; when it does not apply to
  the current status it is recorded without a status change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Status(StrEnum):
    INBOX = "inbox"
    BACKLOG = "backlog"
    READY = "ready"
    WORKING = "working"
    REVIEW = "review"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


TERMINAL: frozenset[Status] = frozenset({Status.DONE, Status.CANCELLED})
OPEN: frozenset[Status] = frozenset(Status) - TERMINAL
#: Statuses that count against the WIP limit: things you have said you will do.
COMMITTED: frozenset[Status] = frozenset(
    {Status.READY, Status.WORKING, Status.REVIEW, Status.BLOCKED}
)


@dataclass(frozen=True, slots=True)
class Command:
    sources: frozenset[Status]
    target: Status
    help: str


COMMANDS: dict[str, Command] = {
    "triage": Command(frozenset({Status.INBOX}), Status.BACKLOG, "Seen it; not committing"),
    "commit": Command(
        frozenset({Status.INBOX, Status.BACKLOG}), Status.READY, "Commit to doing it"
    ),
    "defer": Command(
        frozenset({Status.READY, Status.WORKING, Status.REVIEW, Status.BLOCKED}),
        Status.BACKLOG,
        "Un-commit; back to the backlog",
    ),
    "start": Command(
        frozenset({Status.READY, Status.BLOCKED, Status.REVIEW}),
        Status.WORKING,
        "Start (or resume) work",
    ),
    "submit": Command(frozenset({Status.WORKING}), Status.REVIEW, "Hand off for review"),
    "block": Command(
        frozenset({Status.READY, Status.WORKING, Status.REVIEW}),
        Status.BLOCKED,
        "Waiting on someone",
    ),
    "unblock": Command(frozenset({Status.BLOCKED}), Status.READY, "No longer waiting"),
    "complete": Command(OPEN, Status.DONE, "Done"),
    "cancel": Command(OPEN, Status.CANCELLED, "Won't do"),
    "reopen": Command(TERMINAL, Status.BACKLOG, "Reopen into the backlog"),
}

#: Events that never change status.
ANNOTATIONS: frozenset[str] = frozenset({"update", "note"})
#: Facts reported by GitHub.
FACTS: frozenset[str] = frozenset({"github.closed", "github.merged", "github.reopened"})
#: Events written only when an item is created.
CREATION: frozenset[str] = frozenset({"created", "imported"})


class TransitionError(Exception):
    """A command was issued from a status that does not allow it."""

    def __init__(self, event_type: str, current: Status) -> None:
        allowed = ", ".join(sorted(COMMANDS[event_type].sources)) if event_type in COMMANDS else "-"
        super().__init__(f"cannot {event_type} from {current} (allowed from: {allowed})")
        self.event_type = event_type
        self.current = current


class UnknownEvent(Exception):
    pass


def decide(current: Status, event_type: str, payload: dict | None = None) -> Status | None:
    """Return the status after ``event_type``, or ``None`` if status is unchanged.

    Raises :class:`TransitionError` for a command issued from the wrong status
    and :class:`UnknownEvent` for an event type nobody defined.
    """
    if event_type in ANNOTATIONS:
        return None
    if event_type in COMMANDS:
        cmd = COMMANDS[event_type]
        if current not in cmd.sources:
            raise TransitionError(event_type, current)
        return cmd.target
    if event_type in FACTS:
        if event_type == "github.reopened":
            return Status.BACKLOG if current in TERMINAL else None
        if current in TERMINAL:
            return None
        if event_type == "github.merged":
            return Status.DONE
        reason = (payload or {}).get("state_reason")
        return Status.DONE if reason in (None, "COMPLETED") else Status.CANCELLED
    raise UnknownEvent(event_type)


def available_commands(current: Status) -> list[str]:
    """Commands that are valid from ``current``, in display order."""
    return [name for name, cmd in COMMANDS.items() if current in cmd.sources]
