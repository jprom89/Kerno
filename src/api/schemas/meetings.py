"""Pydantic shapes for the monthly meeting-pack endpoint.

What:  JSON contract for GET /api/v1/meetings/pack — agenda rows plus
       copy-paste markdown.
Why:   the dashboard Meeting page and any paste-into-Docs workflow share
       one response so the chair never rebuilds the pack by hand.
How:   pytest tests/unit/api/test_meetings.py -v
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MeetingControlOut(BaseModel):
    """One NIS2 control on the agenda, in the language the chair will read."""

    model_config = ConfigDict(from_attributes=True)

    control_id: str
    control_ref: str
    title: str
    category: str
    what_this_means: str
    status: str
    evidence_count: int
    evidence_titles: list[str]
    open_recommendation_rationale: str | None
    ask_in_the_meeting: str
    skip_unless_asked: bool


class MeetingPackResponse(BaseModel):
    """The exception-review pack: counts, decisions, skip list, and notes."""

    model_config = ConfigDict(from_attributes=True)

    generated_at: datetime
    met: int
    partial: int
    gap: int
    total_controls: int
    decisions_needed: list[MeetingControlOut]
    skip_unless_asked: list[MeetingControlOut]
    notes_markdown: str
    preamble: str
    review_minutes: int
