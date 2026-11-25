<!-- From Hallmark (MIT), commit 407fa689, changed for Nokker: self-contained fonts (rule, Sources, Loading, catalog and stamp mentions). -->
# Fonts

Load it when the page needs its type chosen.

**Needs before reading:** the page's idea and what it shows. **Gives:** a display face, a body face, an optional outlier, and the load rules.

## The rule

One display face and one body face, or one family for both; an optional outlier carries exactly one role (a wordmark, a headline figure, code). Choose the class the idea calls for, then two or three faces from it, and decide on the real headline set in each, by shape and colour on the page; a face's name, its reputation or a subject association is not a reason. Free faces only unless the user has confirmed a licence; variable fonts preferred.

## Banned as a default

Every model reaches for these first, so they read as unchosen: Inter, Roboto, Open Sans, Lato, Poppins, Source Sans, Nunito, Montserrat, Raleway, Work Sans, DM Sans, Plus Jakarta Sans, system-ui, Arial and Helvetica as a display or body face; Merriweather, Lora and Georgia as the body serif; Courier New or the system mono. A page may still use one of them when the user asks for it or the project already uses it; then say so.

**Saturated** (fine faces, but the first answer, so they want an argument in the Picks block): Playfair Display, Cormorant Garamond, DM Serif Display, Fraunces, Instrument Serif, Recoleta, Space Grotesk, Hubot Sans, Bebas Neue and Unbounded as display; Geist, Instrument Sans and Mona Sans as the body or the whole page.

## Sources

In Nokker a page is self-contained: no font is loaded from an external address (`design_check` refuses it).

- **A font file you may ship**: a free licence (SIL Open Font License or similar) checked before use, embedded with
  `@font-face` as a `data:` URL, or as a file of the working folder next to the page. The faces named below as
  Google or Fontshare are free; get the file from the user or the working folder, never from a CDN at view time.
- **A foundry face** only when the user has confirmed the licence and supplied the file.
- **Otherwise, a system stack** for the role, stated in one line (no custom face is better than a fetched one).

## Display faces

| Class | Faces (source) |
| --- | --- |
| Roman serif | EB Garamond, Cardo, Castoro, Piazzolla, Alegreya, Marcellus (Google); Gambetta, Recia (Fontshare) |
| High-contrast serif | Gloock, Libre Bodoni, Abril Fatface, Rozha One, Besley (Google); Erode, Melodrama (Fontshare) |
| Slab serif | Arvo, Hepta Slab, Domine, Alfa Slab One, Solway (Google); Bespoke Slab (Fontshare; old-style figures that ignore `lining-nums`, so figures go in the body face) |
| Grotesk sans | Rethink Sans, Reddit Sans, Albert Sans (Google); Clash Grotesk, Supreme (Fontshare) |
| Geometric sans | Lexend, League Spartan, Afacad (Google); Satoshi, Technor, Excon (Fontshare) |
| Rounded sans | Inclusive Sans, Nunito Sans at 700 only with an argument, Varela Round (Google); Chubbo, Comico (Fontshare) |
| Condensed and heavy | Teko, Tektur, Michroma, Antonio (Google); Clash Display, Panchang, Khand, Telma, Stardom, Array (Fontshare) |
| Mono as display | Space Mono, Fragment Mono, Red Hat Mono, Spline Sans Mono, Sometype Mono (Google) |
| Pixel | Press Start 2P, VT323, Pixelify Sans, Tiny5, DotGothic16, Workbench (Google) |
| Handwritten | Shantell Sans, Caveat, Gochi Hand, Reenie Beanie (Google); Sharpie (Fontshare) |

## Body faces

Sans: Hanken Grotesk, IBM Plex Sans, Figtree, Onest, Sofia Sans, Golos Text, Karla, Chivo, Albert Sans, Public Sans, Archivo, Manrope (Google); Switzer, General Sans, Satoshi (Fontshare). Serif: Newsreader, Source Serif 4, Literata, Spectral, Crimson Pro, EB Garamond, Piazzolla (Google); Sentient (Fontshare). Mono for code, figures and measurements: Geist Mono, JetBrains Mono, IBM Plex Mono, DM Mono, Fira Code, Azeret Mono, Chivo Mono, Overpass Mono (Google).

## The wordmark

The wordmark in the nav and footer may take a different face from the body, and on tone-rich pages it should: collapsing it into the body family flattens the hierarchy and the page reads as un-branded. It is the outlier's one role when the page has no other:

```css
:root { --font-body: "Switzer", ui-sans-serif, system-ui, sans-serif; --font-outlier: "Gambetta", ui-serif, serif; }
.wordmark { font-family: var(--font-outlier); font-weight: 600; letter-spacing: -0.015em; }
```

Collapse to one family only when the body voice is the brand (a letter, a manifesto, a long document); then the wordmark takes the family's display cut or its heaviest weight. A product page whose wordmark is the body face at 600 is the un-designed default.

## Loading

- Embed only the weights the page uses, one `@font-face` per weight and style, with `font-display: swap`; no `<link>` to a font host.
- `@font-face` for a self-hosted face carries `font-display: swap` and a metric-matched fallback (`size-adjust`, `ascent-override`, `descent-override`, `line-gap-override`) so the swap does not reflow the fold.
- Never synthesise bold or italic: `font-synthesis: none`, and load the italic if a head uses it.
- A variable font loads once and gives every weight; prefer it where the family ships one.
