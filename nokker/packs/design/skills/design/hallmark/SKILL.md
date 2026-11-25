---
name: hallmark
description: Design a new page that looks made, not generated
locality: local
---
# Hallmark

> **Nokker reading key.** In Nokker, these do not exist: sloplint, the scaffold and base.css, the theme catalog and the photo library (theme.mjs), capture scripts, the stamp, the build log, the render tier, the design hook, the launcher, and the files anatomy.md, imagery.md, macrostructures.md, direction.md, redesign.md, patterns.md, scroll-choreography.md and audit.native.md. design_check replaces the static tier (M); you declare every token yourself at :root. Render (R) checks are asked of the user, never claimed. A waived reflex is stated in one line in your answer, not in a stamp; no numeric self-score. Skip gate 20 and the ledger gates (8, 32). Images and fonts are only what the user supplied, inline as data:, or a system font stack: never a remote image, font or icon library (a remote asset fails the floor; it is never a WARN). A visual the user did not supply is a statement, a table or a diagram of given facts, never drawn or invented; sample data on a page is visibly marked as an example. Nothing here overrides the design-guard floor or Nokker's rules.

Adapted for Nokker from Hallmark (MIT, see the pack's notices). It makes a page a model builds look made, not
generated: a hard floor against the defaults every model falls into, one idea per screen, and the real material
shown instead of described. Use it for a NEW page or screen. For changes to an existing design, do what was asked in
that design's own system, or use `impeccable`.

What is not here: the upstream scripts (theme dealer, scaffold, sloplint, photo library, capture), themes and
macrostructure files. In their place: `design_check` for the mechanical floor; for the rest, read the page's code for it, and for what only the render shows (overlaps, cut text, the fold at 390 and 1280 px), ask the user to look and say so; never report a render check you could not run.

## The floor

Binds every output, none of it waivable:

1. Every colour and every font goes through a token declared at `:root` (or a theme block).
2. Never `transition: all`. Never animate `width`, `height`, `top` or `left`.
3. Never one italic word inside a roman heading.
4. `overflow-x: clip` on both `html` and `body`. Never `hidden`, never `100vw`.
5. Image-bearing grid tracks use `minmax(0, 1fr)`; display heads carry `overflow-wrap: break-word; min-width: 0`.
6. Every interactive element has `:focus-visible`, appearing instantly.
7. Every animation has a `prefers-reduced-motion: reduce` fallback, and content is visible with scripting off.
8. Never an invented metric, testimonial, logo wall or case-study count.
9. Never an eyebrow, and never a label in tracked capitals or small caps: labels, captions, table heads, tags and
   nav links are the body face, a step smaller, sentence or lower case, untracked.
10. Never middle-dot-joined meta strings, arrows glued to link text, or decorative status dots.
11. Never a purple-to-blue gradient, a card inside a card, an emoji as an icon, or a hand-drawn browser or OS window.
12. The headline, one sentence and the primary action are all visible at 1280 x 800 without scrolling.
13. About 80 words on any one screen; the first screen's text is the headline, one sentence and its actions.
14. The container is capped and centred: at 1920 the gutters grow, the measure and the grids do not.
15. In Nokker, the page is self-contained (no external address) and passes `design_check` with no floor finding.

Above the floor, a **Reflex** is a default a model falls into, waivable with a real reason stated in one line; a
**Finish** check is craft detail, answered, never blocking (`references/gates.md` § 1 and § 2).

## Flow

1. **Ask once**, in one message: the **material** (what the page can show: the user's photos, screenshots, real
   numbers, a quote with its author), the **audience**, the **use** (the one action the page drives) and the
   **tone** (pick an extreme: editorial, brutalist, soft, utilitarian, luxury, playful, technical, austere; "clean
   and modern" is not a tone). Skipped fields are inferred and stated in one line; facts are never inferred.
2. **Shape**: list the material (real, pictures, absent), name the job the visitor comes to do, then design the
   page: its shape, how it opens, each section's content, the one thing a visitor would remember.
3. **Load** `references/layout.md`, `references/craft.md` and `references/gates.md` § 1 to § 4. Only when needed:
   `interaction-and-states.md` for forms, `microinteractions.md` for stateful UI, `responsive.md` for an unusual
   layout, `dark-mode.md` for both modes, `data-viz.md` for charts, `fonts.md` for type choices.
4. **Say the picks** once (shape, tone, palette and type, signature, sections, material), so the user can redirect.
5. **Build**: tokens first at `:root`, then the first screen, then the sections. One idea per screen, set big;
   every control in all its states; `transform` and `opacity` only, at most one reveal.
6. **Sweep**: `design_check` until no floor finding; walk the judged gates of `gates.md` § 5; then show the page
   with `page_create` and close with the material ask: each placeholder, its size, and the one piece of material
   that would most improve the page.

A pass you did not run is never reported as passed.
