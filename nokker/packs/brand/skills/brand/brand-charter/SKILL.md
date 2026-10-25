---
name: brand-charter
description: Build the brand charter and keep it in project memory
---
# Brand charter

The charter is short, concrete and checkable. It lives in two places: a file in the project
(`brand/charter.md`) that people read, and project notes (`memory_note`) that every later session recalls.

## Interview

Ask, one block at a time, and write down the user's words:

1. **Identity**: the name as it must be written (capitals, accents), a one-sentence promise, the audience.
2. **Tone**: three adjectives, and for each one a "we say / we do not say" pair. Words and phrasings to avoid.
   Formal or informal address, per language.
3. **Colours**: the primary colour, one or two accents, neutrals, each as a hex value with its role
   (background, text, call to action). Check contrast of text on background: at least 4.5:1 for body text,
   3:1 for large text (WCAG 2.2 AA). If a pair fails, say so and propose the nearest value that passes.
4. **Type**: one family for headings, one for text (with a free fallback), sizes for title, subtitle, body.
5. **Logo**: files the user has, clear space, minimum size, backgrounds where it may not appear.
6. **Imagery**: style (photo, flat illustration, 3D), subjects to prefer and to avoid, whether people appear.

## Writing it down

1. `write_file brand/charter.md` with one section per block above, values first, explanations short.
2. One `memory_note` per durable rule, `kind: rule`, `scope: project`, `derived_from: user`, for example:
   - "Brand name is always written <as given>";
   - "Primary colour <hex> for calls to action only";
   - "Tone: direct, warm, precise; never <word to avoid>".
3. Tell the user which notes were written; they can see or forget them with the memory command of the CLI.

## Updating

When the user changes a rule, update the file and replace the matching note (forget the old one with `memory`,
write the new one). Never keep two contradictory notes.
