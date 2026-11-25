---
name: design-guard
description: Check a page or a screen against the design floor
locality: local
---
# Design guard

Use it before you show a page you built (`page_create`) and before you hand back any screen: a page in the chat, a
public page, a screen for the wall. You check and fix; you do not redesign unless asked.

## Steps

1. Run `design_check` on the page file. It reads the HTML and its CSS without a browser and returns two kinds of
   findings: **floor** (fix it) and **reflex** (fix it, or keep it and say why in one line).
2. Fix every floor finding. For each reflex finding, fix it or name the reason to keep it.
3. For what the check cannot see, read the page's code for it, and for what only the render shows (overlaps, cut text, the fold at 390 and 1280 px), ask the user to look and say so; never report a render check you could not run:
   - one idea per screen; the main action visible without scrolling;
   - no invented number, quote, logo or testimonial: a fact the user did not give is absent, not made up;
   - text never covered, cut or overflowing at 390 px and at 1280 px wide;
   - light and dark themes both readable when the page offers both.
4. Run `design_check` again. Stop at one confirming run: a clean floor is not proof of quality.

## The floor in Nokker

- A page is self-contained: CSS, images and fonts inline or in `data:`; no external address (the page is served
  sandboxed, from another origin, and outside requests are blocked).
- Every colour and every font goes through a token declared at `:root` (or a theme block); none written in a rule.
- Never `transition: all`; animate `transform` and `opacity` only; every animation has a
  `prefers-reduced-motion: reduce` fallback, and the content is visible without it.
- Every interactive element has a `:focus-visible` style; every field has a label; every image has `alt`.
- Text contrast at least 4.5:1 (3:1 for large text); body text 16 px or more.
- `<html lang>` and a viewport meta; `overflow-x: clip` on `html` and `body`, never `hidden`, never `100vw`.
- No purple-to-blue gradient, no emoji used as an icon, no labels in spaced capitals, no meta strings joined by
  middle dots.

## Deeper passes

- A new page from a brief: the skill `hallmark`.
- Critique, audit or polish of an existing page: the skill `impeccable` (start with `references/audit.md`).
