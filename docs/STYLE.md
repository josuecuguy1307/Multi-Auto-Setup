# Style

One look for every surface of Multi Auto Setup. The source of truth is [`src/envrec_style/tokens.json`](../src/envrec_style/tokens.json); every renderer reads it, and `tests/test_style.py` fails if a surface drifts from it.

## Logo

"MAS" in pixel art on a 5-row grid. Letters are 5 columns wide with one empty column between them.

```
M: X...X  XX.XX  X.X.X  X...X  X...X
A: .XXX.  X...X  XXXXX  X...X  X...X
S: .XXXX  X....  .XXX.  ....X  XXXX.
```

| Part | Dark surface | Light surface (default A) |
|---|---|---|
| Letters | `#3B82F6` | `#2563EB` |
| Shadow: the same shape half a cell down and right, drawn behind | `#86198F` | `#86198F` |
| Tagline `MULTI AUTO SETUP`, wide letter-spacing, monospace | `#E879F9` | `#A21CAF` |
| Image background (image files only; a terminal keeps the user's own) | `#0A0614` | transparent |

Motto: "Your computers, working as a team."

Files are in [`assets/brand/`](../assets/brand/): `logo-dark.svg` (with background), `logo-transparent.svg`, `logo-light.svg` (default, for white pages), `logo-light-alt.svg` (alternative B: the exact dark-mode blue with a lighter shadow), and PNGs at 1x (1000 px wide) and 2x for the first three. `gallery.html` shows every logo variant and every symbol on dark and light backgrounds. Regenerate with `python scripts/make_brand.py`; `--check` reports drift.

Contrast on white (checked by a test): light A letters 5.2:1, tagline and shadow above 4.5:1. Light B keeps the brand blue (3.7:1, enough for large graphics) and is shown for comparison.

## Product colors are not brand colors

| Color | Meaning | On light | On dark |
|---|---|---|---|
| teal | the data stays on this machine | `#0d7a6b` | `#2dd4b0` |
| amber | the data goes to the cloud | `#9a4509` | `#f5a15a` |
| a lock | confidential | ink | ink |

They never change with the brand. **Meaning is never carried by color alone**: every symbol also has a shape and a word, so it survives `NO_COLOR`, a 16-color terminal, and a chat that shows our text as plain markdown.

## Symbols

27 pixel-art symbols on an 8x8 grid, in the tokens file: meaning (`local`, `cloud`, `confidential`), access (`chip` local model, `key` API key, `terminal` subscription CLI), machines (`coordinator`, `node`, `laptop`, `desktop`, `server`, `oldpc`), states (`searching`, `waiting`, `paired`, `approved`, `denied`, `running`, `done`, `failed`, `undone`, `warning`), actors (`owner`, `model`, `guard`, `script`) and `network`.

Each has a pixel SVG (`envrec_style.svg.symbol_svg`), a text form with its word (`envrec_style.tokens.symbol_text`: `☁ cloud`, or `[CLOUD] cloud` where the character cannot be trusted) and a one-sentence meaning. To add one: add an entry to `symbols` (id, group, word, a unique character, role `neutral`, meaning, 8 rows of 8 `X`/`.`), run `python scripts/make_brand.py`, look at the gallery, commit the tokens and the regenerated assets. The tests require a distinct shape, word and character.

## Terminal banner

`envrec_style.term.render` draws the same glyph. A pixel is two characters wide and one row tall; the shadow is one character right and half a row down, made with half blocks.

* Wide (38 columns or more): logo, spaced tagline, motto.
* Narrow (18 to 37): a pixel is one character wide, no motto.
* Compact (under 18): `MAS · Multi Auto Setup`.
* Color: truecolor when `COLORTERM` says so, else 256 colors, else 16, by nearest match. `NO_COLOR` set and not empty, `TERM=dumb`: the same shape in shades (full block letters, light-shade shadow).
* Not a terminal, or one that cannot show UTF-8: plain ASCII, no escape codes at all.

The terminal's own background is never painted.

## Where the style applies

Terminal banner and menu; the text of MCP tools (symbol text forms only, no ANSI); Open WebUI cards and the panel (CSS variables from `envrec_style.css`, teal and amber from the tokens); README images; the deck and the video (the SVG/PNG files and the symbols).

## Do and don't

* Do read colors from the tokens; do pair every color with a shape and a word; sentence case; flat rectangles; the half-cell shadow.
* Don't invent a second teal or amber, use gradients or blur, tint the logo to fit a page (use the light variant), or paint a background in a terminal.

## Owner-facing text (one formatter)

Every intent that reaches a chat, a terminal or the panel passes through `envrec_harness/formatter.py`; the log keeps the intent's own text. In Claude Desktop the person sees only the text of a tool result and the host model rewrites it, so the text stands alone: a title line with a symbol, short sections, numbered options without ids (a number answers), a `Next:` line only when the intent itself leaves something to do, then the rules for relaying an answer. Plain markdown, never ANSI.

Three levels, the owner's setting (`mas config verbosity brief|normal|detailed`, the profile field `verbosity` in a chat question, the panel): `brief` keeps the facts in few lines, `normal` is the default, `detailed` is the original text with ids and hashes. Outside `detailed` a hash is its first eight characters and a line of raw JSON is left out; the commands of a plan or an approval are never touched, checksums included. Views that are data are cut at a line limit and say how many lines they cut; a plan, an approval and a question are never cut. Presentation only: the level never changes what the model may do or what is asked.

The language of the model's own sentences follows the person (skill `mas-voice`); fixed strings are English and are not translated.
