---
name: cover-letter-drafts
description: Cover letters from the CV and the offer, saved as drafts
---
# Cover letter drafts

A letter is a draft until the user reads it. Submitting an application (the server's submit tool) or mailing it
(`mail_send`) is an external send: one approval card per application, and only when the user asks.

## Inputs

- The offer, read in full with the get-offer tool.
- The CV, read from the project folder (`read_file`). If there are several, ask which one.
- The profile notes (languages, availability, salary floor if the offer asks for expectations).

## Writing

1. Language: the language of the offer, unless the user says otherwise.
2. Structure, 250 to 350 words:
   - why this role at this company, from something specific in the offer;
   - two or three requirements of the offer matched with a concrete fact from the CV each;
   - availability and practical points the offer asks for;
   - a short closing.
3. Truthfulness, strictly:
   - every claim about the candidate must be traceable to a line of the CV or something the user said;
   - no invented employers, titles, dates, numbers, degrees, languages or references;
   - a requirement the CV does not cover is not claimed; at most "eager to build experience in".
4. Unknown details (name of the recruiter, reference number) stay as `[to fill]`.

## Saving

- As a file in the project folder: `applications/<date>-<company>-<title>.md`, with a short header (offer link,
  date, CV used).
- If the application is by mail, also save a `mail_draft` to the address given in the offer, with the letter in
  the body and the CV mentioned as the attachment the user adds.

## Before any submission

Show the user: the offer, the letter, the CV file, the destination (form or address). Only then call the submit
tool or `mail_send`, once, with exactly what was shown. A refused card is not retried.

## Log

After each submission the user approved, append a line to `applications/log.md`: date, company, title, channel,
link, next follow-up date (default: ten working days later).
