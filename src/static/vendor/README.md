# Vendored assets

Everything the interface loads is served from this app. Nothing is fetched from
a CDN at runtime.

That is not a preference. This app drives a label printer on a local network,
and such a network may have no route out at all. The previous CDN links failed
*silently* when it did not: the stylesheets degraded visibly, but the Bootstrap
bundle going missing took the tab navigation, the dialogs and the dropdowns with
it, without a single console error. The interface looked fine and did nothing.

## What is here

| File | Version | Source | Licence |
|---|---|---|---|
| `bootstrap.min.css`, `bootstrap.bundle.min.js` | 5.3.0 | `cdn.jsdelivr.net/npm/bootstrap@5.3.0` | MIT (`LICENSE-bootstrap.txt`) |
| `bootstrap-icons.css`, `fonts/bootstrap-icons.woff2` | 1.11.1 | `cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1` | MIT (`LICENSE-bootstrap-icons.txt`) |
| `fonts.css`, `fonts/Inter-*.woff2` | v20 | Google Fonts | SIL OFL 1.1 (`LICENSE-inter.txt`) |
| `fonts.css`, `fonts/JetBrainsMono-*.woff2` | v24 | Google Fonts | SIL OFL 1.1 (`LICENSE-jetbrains-mono.txt`) |

The web fonts carry the `latin` and `latin-ext` subsets only. The other five
subsets Google serves (cyrillic, cyrillic-ext, greek, greek-ext, vietnamese)
would nearly triple the size for an interface that is English-only. `fonts.css`
is the Google stylesheet with the remote URLs rewritten to point here.

Only the `woff2` of Bootstrap Icons is kept. The `woff` fallback exists for
browsers predating 2016, none of which can run this interface anyway.

## Updating

Fetch the file, drop it in, bump the version in the table above. For the web
fonts, request the Google stylesheet with a current browser user agent (it
serves `woff2` only to those), keep the `latin` and `latin-ext` blocks, download
each `woff2` into `fonts/`, and rewrite the `src:` URLs to `./fonts/<name>`.

After any update, check the one thing that matters: load the interface with the
network blocked and click through every tab. No request may leave the machine,
and everything must still work.
