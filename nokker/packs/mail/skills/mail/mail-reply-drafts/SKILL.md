---
name: mail-reply-drafts
description: Write replies as drafts the user reviews and sends
---
# Reply drafts

You write replies; the user sends them. A reply is always saved with `mail_draft`. Sending (`mail_send`) is an
external send: it is never part of this skill, and if the user asks you to send, the approval card is the place
where they confirm.

## Before writing

1. Read the whole thread with `mail_read`, not only the last message.
2. Find what is asked. If there are several questions, list them; the reply answers each, in the same order.
3. Look up facts you need instead of inventing them:
   - dates and availability: `calendar_events` for the days in question;
   - earlier exchanges with the same person: `mail_search` on their address.
4. If a fact is missing (a price, a decision the user has not taken), do not make it up. Put a visible placeholder
   such as `[confirm the date]` and say so in your summary.

## Writing

- Language: the language of the thread. Formality: mirror the sender (first name if they used it).
- Length: as short as the question allows. One idea per paragraph. No filler openings.
- Commitments: never promise a date, a price, a meeting or a deliverable the user did not state. Offer options
  ("Tuesday or Thursday afternoon") taken from the free slots you found.
- Attachments: mention what should be attached; you cannot attach files the user did not give you.
- Signature: the user's usual signature if the thread shows it; otherwise none.

## Saving

Call `mail_draft` with the thread or message id so the draft is threaded, the recipients of the original
(reply, not reply-all, unless the user asked), and the subject prefixed with `Re:` if it is not already.

## Report

For each draft: recipient, subject, one line on what it says, and every placeholder left to fill. End with:
"Drafts saved, nothing sent."
