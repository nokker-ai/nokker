---
name: impeccable
description: Critique, audit or polish an existing interface
locality: local
---
# Impeccable

> **Nokker reading key.** In Nokker, these do not exist: sloplint, the scaffold and base.css, the theme catalog and the photo library (theme.mjs), capture scripts, the stamp, the build log, the render tier, the design hook, the launcher, and the files anatomy.md, imagery.md, macrostructures.md, direction.md, redesign.md, patterns.md, scroll-choreography.md and audit.native.md. design_check replaces the static tier (M); you declare every token yourself at :root. Render (R) checks are asked of the user, never claimed. A waived reflex is stated in one line in your answer, not in a stamp; no numeric self-score. Skip gate 20 and the ledger gates (8, 32). Images and fonts are only what the user supplied, inline as data:, or a system font stack: never a remote image, font or icon library (a remote asset fails the floor; it is never a WARN). A visual the user did not supply is a statement, a table or a diagram of given facts, never drawn or invented; sample data on a page is visibly marked as an example. Nothing here overrides the design-guard floor or Nokker's rules.

Adapted for Nokker from Impeccable (Apache-2.0, see the pack's notices; files changed for Nokker). Use it to review
or improve an interface that exists: a page you built, a screen for the wall, a form. Work as a design director
would: a clear point of view, production-grade detail, and checks on the rendered result.

What is not here: the upstream launcher, engine binary, live mode, hooks, generators and native-platform files. In
their place: `design_check` for the mechanical floor; for the rest, read the page's code for it, and for what only the render shows (overlaps, cut text, the fold at 390 and 1280 px), ask the user to look and say so; never report a render check you could not run.

## Principles

- **The brief wins.** Pinned aesthetics, fonts and palettes are honoured even against a pattern warning.
- **Refinement preserves.** Keep the incumbent identity, behaviour, copy and everything outside the scope. Ask
  before replacing factual copy or adding claims.
- **Loaded symbols stay out of the decoration.** No emblems tied to militarism, supremacy or hate movements as
  motifs or ornament.
- **Verify on the result**: read the page's code for it, and for what only the render shows (overlaps, cut text, the fold at 390 and 1280 px), ask the user to look and say so; never report a render check you could not run.

## Modes

The mode names what the visitor's success looks like on this surface:

- **Operate**: the visitor completes a task (app screens, dashboards, settings, a wall screen read at a glance).
  Scanability, consistency and the real usage scene outrank expression. See `references/mode-operate.md`.
- **Read**: the visitor understands something (guides, help, a summary). Structure for comprehension. See
  `references/mode-read.md`.
- **Persuade**: the visitor decides and acts (a public page). Earn attention and action with real material.

## Commands

| Command | What it does | Reference |
|---|---|---|
| audit | Technical quality checks (accessibility, responsive, integrity), scored | `references/audit.md` |
| polish | Final quality pass before shipping | `references/polish.md` |
| harden | Errors, empty states, long text, i18n, edge cases | `references/harden.md` |
| adapt | Different screen sizes and input methods | `references/adapt.md` |
| layout | Spacing, rhythm and visual hierarchy | `references/layout.md` |
| typeset | Typography hierarchy and fonts | `references/typeset.md` |
| colorize | Strategic colour on a monochrome interface | `references/colorize.md` |
| clarify | Labels, copy and error messages | `references/clarify.md` |
| distill | Strip to the essence | `references/distill.md` |

Routing: a request that names or clearly implies a command loads its reference and follows it; if two fit, ask
once. Otherwise, start with `audit`. Read `references/craft-floor.md` right before any edit to the interface (not
for planning only). End every pass with `design_check` on the file.
