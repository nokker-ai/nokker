---
name: calendar-scheduling
description: Find free slots and prepare events without sending invites
---
# Scheduling

You find time, you propose it, you prepare the event. Inviting other people is an external send: it always goes
through an approval card.

## Finding slots

1. Ask (or read from the request) the duration, the window (dates), and constraints: working hours, time zone of
   each participant, travel time if the meeting is in person.
2. `calendar_events` over the window. Treat all-day events marked busy as blocking; treat tentative events as
   soft (list them separately).
3. Keep slots that fit the duration plus a 10 minute buffer on each side. Prefer, in order: the times the user
   usually books (look at the past two weeks), mornings, then afternoons.
4. Propose three options at most, each with the weekday, date, start and end, and the time zone.

## Preparing the event

- `calendar_create` **without attendees** puts a hold in the user's own calendar: a write inside their account.
- `calendar_create` or `calendar_update` **with attendees** sends invitations to other people. That is an
  external send: tell the user who will receive an invitation before calling it, and expect a strong card.
- Title: short and specific ("Contract review with the supplier", not "Meeting"). Description: the agenda in
  three lines and the call link if there is one. Never paste mail content the attendees should not see.

## Moving or cancelling

- Moving an event with attendees notifies them: same rule as inviting.
- Cancelling (`calendar_delete`) is destructive and is not part of this skill: tell the user what to cancel.

## Rules

- Time zones: write every time with its zone when participants are in different zones.
- Never accept or decline an invitation for the user.
