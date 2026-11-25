# Design guard-rails

Two parts, for the pages and screens Nokker builds for you.

## `design_check`

A read-only tool on Lokkal (flag `local.design_check`, on by default). It checks an HTML page of the working folder
against a design floor without a browser, a model or the network, and changes nothing. Findings come in two levels:

- **floor**: fix it;
- **reflex**: a default models fall into; fix it, or keep it and say why in one line.

What it checks:

- colours and fonts go through tokens declared at `:root`, never written in a rule;
- no `transition: all`, only `transform` and `opacity` animate, and every animation has a
  `prefers-reduced-motion` fallback;
- `:focus-visible` on interactive elements, a label on every field, `alt` on every image;
- text contrast of 4.5:1 (3:1 for large text), with tokens resolved;
- `lang` and a viewport meta, `overflow-x: clip` without `100vw`;
- no external resource: a Nokker page is self-contained;
- no purple-to-blue gradient, no emoji used as an icon, no labels in spaced capitals;
- two or three font families at most, and body text of 16 px or more.

A clean result is a floor, not proof of quality. What only the render shows (overlaps, cut text, what fits at 390
and 1280 px) is not checked: the agent reads the page's code, asks you to look, and never reports a render check it
could not run. Output is capped at 40 findings, and a page may weigh 2 MB at most.

## The `design` pack

Machine only, like `mcp-builder`; it does not install in the web app. See [packs.md](packs.md).

| Skill | Use |
|---|---|
| `design-guard` | the short procedure: check, fix, look, check once more |
| `hallmark` | build a NEW page that looks made rather than generated |
| `impeccable` | audit, polish or harden an existing page or screen |

Agent template: `page-design-review`, run on request and read only. It lists the fixes for one page.

`hallmark` and `impeccable` are adapted from two open-source projects, Hallmark (MIT) and Impeccable (Apache-2.0),
pinned to a commit. Their reference files are kept as published, and each file changed for Nokker says so on its first
line. Their scripts, binaries, live mode and hooks are not included. In their place, the skills use `design_check` and
a reading key that maps their tooling to Nokker. Licences are in `LICENSES/` and the notices in `NOTICE`.
