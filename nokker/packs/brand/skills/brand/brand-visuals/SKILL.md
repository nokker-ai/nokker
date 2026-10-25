---
name: brand-visuals
description: Generate on-brand visuals through an image MCP server
---
# Brand visuals

Image generation goes through the image server the user connected (role `images`). Each generation usually costs
credits: every call raises an approval card that shows the prompt. Batch your ideas and ask before generating.

## Before generating

1. Read the charter: `brand/charter.md`, or the project notes about colours, imagery and tone.
2. Agree with the user on the list: purpose, channel, size, text on the image (if any), deadline.
3. Sizes that work: 1200 x 627 (link preview, LinkedIn), 1080 x 1080 (square post), 1080 x 1350 (portrait post),
   1080 x 1920 (story), 1600 x 900 (article header).

## Prompt

Build each prompt from the charter, in this order:

- subject and composition ("a desk seen from above, laptop and notebook, empty space on the right third");
- style from the charter (photo, flat illustration…), lighting, mood;
- palette as words and hex values ("deep teal #0F5C5C, warm sand #E8D9C0 accents");
- what to avoid: logos of other brands, readable fake text, real people's faces, signs of a specific place.

Do not ask the model to draw the logo or long text: add them afterwards in an editor, where they stay exact.

## After generating

Look at every image before showing it as final. Reject and say why when you see:

- text that is misspelled or meaningless;
- extra fingers, broken hands, melted objects;
- a colour far from the charter;
- anything that looks like an existing brand, a real person or a private place.

Save kept images under `brand/visuals/<date>-<purpose>.<ext>` with a sidecar `.md` holding the prompt, the
server and model used, and the date, so a visual can be made again.

## Rules

- Never generate images of real, identifiable people without their documented consent.
- One generation per approved prompt; a refused card is not retried with a reworded prompt.
