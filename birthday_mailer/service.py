"""Delivery orchestration with explicit handling of ambiguous SMTP outcomes."""

from __future__ import annotations

from datetime import date
from email.message import EmailMessage
from typing import Callable, Iterable

from .contacts import Member
from .ledger import Ledger
from .mail import SMTPConfig, build_message


def due_members(members: Iterable[Member], today: date, feb29: str = "feb28", lunar_leap: str = "regular") -> list[tuple[Member, str]]:
    due = []
    for member in members:
        occurrence = member.occurrence_on(today, feb29, lunar_leap)
        if occurrence:
            due.append((member, occurrence))
    return due


def deliver(
    due: Iterable[tuple[Member, str]], ledger: Ledger, config: SMTPConfig,
    organization: str, send: Callable[[EmailMessage], None],
) -> int:
    count = 0
    for member, occurrence in due:
        if ledger.contains(member.email, occurrence):
            continue
        message_id = f"<birthday-{ledger.token(member.email, occurrence)}@{config.sender.rsplit('@', 1)[1]}>"
        message = build_message(member, config, organization, message_id)
        if not ledger.reserve(member.email, occurrence):
            continue
        try:
            send(message)
        except Exception:
            ledger.finish(member.email, occurrence, "uncertain")
            raise
        ledger.finish(member.email, occurrence, "sent")
        count += 1
    return count
