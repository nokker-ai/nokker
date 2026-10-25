---
name: social-post-planning
description: Plan a week of posts and schedule each one after approval
---
# Planning posts

You plan, write and prepare. Each scheduled post is a publication to the public: it is an external send, and every
single call to the scheduler's schedule tool goes through an approval card. Never schedule "now" to skip a review.

## Inputs

- Topics or goals for the period, from the user. If none: ask, or propose three from recent work they mention.
- The brand charter if the `brand` pack is installed (`skill_view brand-charter`): tone, words to avoid, colours.
- What is already planned: the scheduler's list-posts tool for the period, so you do not double-book a slot or
  repeat a topic.
- The channels connected: the scheduler's list-channels tool.

## Writing

For each post:

1. One idea. A first line that works on its own (it is all most readers see).
2. Respect each network's limits and habits:
   - LinkedIn: up to about 3,000 characters, short paragraphs, at most three hashtags at the end;
   - X: 280 characters, one idea, no hashtag wall;
   - Mastodon and Bluesky: around 500 and 300 characters;
   - Instagram: the image carries the post, the caption supports it.
3. Facts: only what the user told you or what a source you fetched says (`web_fetch`), with the source kept in your
   notes. No invented numbers, quotes, clients or results.
4. Mentions of people or companies: only when the user asked for them.

## The plan

Present a table before scheduling anything:

| When (with time zone) | Channel | First line | Length | Media |
|---|---|---|---|---|

Default rhythm: no more than one post per channel per day, spread over working hours of the audience.

## Scheduling

Only after the user approves the plan in the conversation, call the schedule tool once per post with the exact text
shown in the plan. Each call shows a card with the text and the date: the user approves or refuses it there. If a
card is refused, do not retry with a rewording; ask what to change.

## Report

Posts scheduled (date, channel, first line), posts refused, posts left to write.
