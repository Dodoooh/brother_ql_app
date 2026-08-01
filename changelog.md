# Changelog

All notable changes to the Brother QL Printer App will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.0.0-dev] - 2026-07-30

### Breaking Changes
- **Printing is now asynchronous (print queue).** The print endpoints (`/text/print`, `/image/print`, `/qrcode/print`, `/label/text-qrcode`, `/label/text-image`, `/pdf/print`) now enqueue the job and return **immediately**. The response shape is unchanged (`{ "success": true, "job_id": "...", "message": "..." }`), but its meaning changed:
  - `success: true` now means **"queued"**, not "printed".
  - A print **failure** no longer comes back as an HTTP error on the print call — the call still returns `200`, and the failure surfaces later as the job's status (`failed`).
  - The `message` text changed (e.g. `"Print job queued"` instead of `"Text printed successfully"`).
  - *Migration:* to confirm the actual print result, poll `GET /api/v1/jobs/{job_id}` and check `status` (`done` / `failed`). Fire-and-forget clients that do not inspect the result need no change.
- **Large batches require explicit confirmation.** Any print request for **10 or more copies** must include `confirm_large_batch` (`true` for JSON endpoints, `"true"` for multipart). Without it the request is rejected with **HTTP 400** and code `CONFIRMATION_REQUIRED`.
  - *Migration:* add `confirm_large_batch: true` to requests that print 10+ copies. Requests for fewer than 10 copies are unaffected.

### Added
- **USB / non-network printer support**: `usb://` and `file:///dev/usb/lpX` backends in addition to network printers; keep-alive is automatically disabled where it is not meaningful.
- **Printer status & clock**: IPP-based printer status and reachability reporting, plus a printer clock readout.
- **Health & deployment**: `GET /health` (liveness) and `GET /health/printer` (readiness) endpoints; the app now runs under gunicorn; optional API-key authentication via the `API_KEY` env var; configurable CORS via `CORS_ORIGINS`.
- **Copies, cut & density options**: copies (1–100), cut modes (`each`/`end`/`none`) and density/quality options (600 dpi, HQ). Copies and cut are available directly in every compose section.
- **PDF printing**: upload a PDF, select pages, fit/fill scaling, with per-page preview (`POST /pdf/print`, `POST /pdf/preview`) and a PDF compose tab.
- **Share hand-off**: `POST /api/v1/share` (+ `GET /share/{token}`) to send a PDF or image from a phone (Apple Shortcuts / Android HTTP Shortcuts) straight into the print form.
- **True-to-print live preview**: server-rendered previews for text, QR, combined and image labels (`/text/preview`, `/qrcode/preview`, `/label/preview`, `/image/preview`), backing the instant client-side preview.
- **Optional `settings` on print/preview**: any field of `settings` (or the whole object) may be omitted — the app fills it from its saved configuration, so "print on the configured printer" needs no settings. Fields present in the request still override.
- **Raw PNG previews**: send `Accept: image/png` to the preview endpoints to get the raw PNG bytes (with `X-Label-Width-Px` / `X-Label-Height-Px` headers) instead of the JSON data-URL wrapper.
- **Automatic text wrapping**: long text now wraps at word boundaries to fit the label (with hard-breaking of over-long words) instead of being truncated — in plain text, text+QR side-by-side and QR captions, in both print and preview. On by default; disable per request with `text.wrap: false` (or `settings.text_wrap: false`).
- **Lengthwise text on continuous rolls**: text labels can now be printed along the tape instead of across it. With `settings.orientation: "lengthwise"` the roll's printable width becomes the line height and the tape grows with the message, so a long text on a narrow roll (e.g. 12 mm) comes out as one continuous, readable strip instead of a narrow column in a shrunken font. The text reads bottom-to-top when the strip is held upright; combine with `rotate: 180` for the other direction. Applies to `/text/print` and `/text/preview`; die-cut labels have a fixed size in both directions and always print `across` (the default).
- **Vertical text alignment**: `settings.vertical_alignment` (`top`, `middle` or `bottom`) positions the text block across the label's height, the counterpart to `alignment`, which positions it along the width. It matters most on die-cut labels, whose height is fixed: a 24 mm round label has a lot of vertical room, and until now the text sat in the middle of it with no way to move it. On a continuous roll it moves the text across the tape width when `orientation` is `lengthwise`; with the default `across` orientation the label grows in length to fit the text exactly, so there is no spare height to move within and the setting has no effect. Applies to `/text/print` and `/text/preview`; the default `middle` reproduces the previous behaviour.
- **Print alignment calibration**: content can land off-centre on the physical label — die-cut registration tolerance, per-model raster offsets and media variation all push it around, and on a round label a design that is mathematically centred can print visibly off the circle. Until now the only remedy was swapping label identifiers. `settings.calibration` now holds a correction per label type, in millimetres (`"calibration": { "d24": { "x_mm": -0.5, "y_mm": 1.0 } }`), and Settings gained a **Print Alignment** dialog that runs the whole loop: print the target, look at how far it sits off-centre on the label, nudge the print the same way, print again. The target carries a ring (round media) or a frame (rectangular and continuous media) on the edge of the printable area, a millimetre scale on both axes and a caption naming the label and the offset it was printed with, so the error can be read off the label itself without a ruler. Calibration is applied **when printing only and never to the preview**: the preview stands for the label you designed, and calibration exists to make the paper match it — the calibration target is the one deliberate exception, since where the ink lands is its whole subject. Stored per medium rather than per printer, because the media is the dominant cause; an absent map or an absent key means no correction, so nothing changes for an installation that never calibrates. New endpoints `POST /api/v1/calibration/test-print` and `POST /api/v1/calibration/preview`; the test print also accepts `dry_run`, and an optional `sweep` (API only) that prints several numbered targets stepping around the current value, so the right one can be picked instead of worked out.
  - **The sideways offset moves the whole raster along the print head**, so no content is ever cut off — but the travel is asymmetric and bounded by how much head sits beside the loaded roll: a 24 mm round label on a QL-820NWB has roughly 37 mm of travel one way and only 3.5 mm the other. A request beyond that is clamped, and the clamp is reported rather than silently swallowed — the printed target captions itself with the offset it was really printed with, the dialog says so, and the API returns `offsets_mm`, `requested_offsets_mm`, `clamped` and `sideways_travel_mm`.
  - **Along the feed the content moves inside the label's own canvas**, because the raster starts where the feed starts and there is no equivalent lever. A large `y_mm` can therefore push content off the edge, which is logged with the amount per side; on continuous media a positive `y_mm` trims the trailing edge instead of adding a lead-in, and warns with the millimetres lost.
- **Size correction for printers that lay ink slightly large or small**: `scale` in the same calibration entry (0.95–1.05) multiplies the printed content about the centre of the label, and the calibration dialog adjusts it in percent — "smaller" and "larger" by 0.1 / 0.5 / 1 %, with the stored multiplier shown underneath as `x0.980`. It is a correction, not a zoom: the range is deliberately narrow, the canvas cannot grow so scaling up crops at the rim (and logs it), and like the offsets it applies to prints only and never to the preview. Scale is applied before the offsets, so correcting the size does not un-correct an alignment already measured.
- **Experimental: bleed into the label's unprintable margin (`bleed_mm`)**: every die-cut label is offered smaller than it is — `brother_ql` publishes 20 mm of a 24 mm round label as printable, so about 2 mm of paper all round is unreachable by any design, the bare ring you can measure on a finished label. `bleed_mm` hands that strip back, keyed by label type, in millimetres per side. It is **off by default**, has no UI control (set it in `settings.json` or per request via the API), and unlike `calibration` it *does* show in the preview, because it changes how large the label you are designing is. Whatever is asked for is clamped to what the medium and the print head can actually give (5 mm on `d24` yields 2.03 mm, and only 1.02 mm is available on 62 mm tape on a QL-800-class printer); values above 5 mm are rejected as a wrong unit rather than a request. It is marked experimental for reasons worth reading before switching it on:
  - **It widens the label and never lengthens it, and that cannot be fixed.** Each raster line is one step of the paper feed, so extending the raster along the feed makes the media advance further per label — 284 lines instead of 236 adds about 4 mm to every d24 page — and the cutter walks off the gap between labels until the roll loses registration. This was confirmed on paper twice, the second time on a freshly seated roll, which rules out the media seating as the cause. The only feed-related command in the stream carries the medium's own margin and is packed unsigned, so the extra steps cannot be given back. Across the tape there is no such coupling: a bled job emits exactly as many raster lines as an unbled one.
  - **A bled round label is an ellipse, not a circle** — 24 × 20 mm on `d24` — and the layout follows that ellipse rather than retreating to the circle inside it, which would have handed the bleed straight back. Because the canvas is no longer square, `rotate` 90 and 270 stop working on a bled round label, exactly as they have never worked on rectangular die cuts; such a request is now refused with an explanation naming the medium and the sizes instead of an opaque failure. `rotate` 0 and 180 are unaffected.
  - **The default stays off on purpose.** The gain is 2 mm, it trades a round printable area for an oval one, and the feed half of the same idea destroys the cut — even coverage on the smaller circle is the better default. What you opt into, on the paper: the die cut and the feed each vary by a few tenths of a millimetre, so content taken right out to the edge will show that variation from label to label, and anything overshooting the cut is printed on the liner between labels, which is visible on a black-heavy design. Bleed is for backgrounds and colour meant to run off; keep text, codes and borders inside the published area.
- **Searchable media picker**: the Label Type dropdown was 34 bare identifiers with no clue what each medium was for. It is now a picker that searches a catalogue of every supported medium — by Brother product code, identifier, size, form and description — with the results grouped into continuous rolls, die-cut labels and round labels. `DK-11218`, `dk11218`, `11218` and `DK-1218` all land on the 24 mm round label (`d24`). Both regional article numbers are indexed, since the European code doubles the leading digits and matching only one would leave half of all users finding nothing, and Brother's short-side-first sizes are matched in both orders, so `29 x 62` finds `62x29`. An identifier covering several products lists all of their codes. The plain `label_size` select remains underneath as the single source of truth, so the app still works without JavaScript.
- **Copy the product code**: a button under the media picker copies the selected medium's Brother product code to the clipboard for reordering — nobody reorders a "d24", they reorder DK-11218 — and names the code it copied, so what lands on the clipboard is never a guess. It works over plain `http` on a LAN address, where the browser clipboard API is unavailable, and says so plainly if the copy fails rather than claiming success.
- **Round media is previewed as round**: the printer receives a square raster, but the label is punched out as a circle, so the preview used to show content sitting in corners that never reach the label. The preview now draws the die cut and veils the discarded area, so ink escaping the circle fades toward the paper it will never reach. The rendered image itself is untouched — it is what goes to the printer, and the preview stays true to print.
- **Dry run**: pass `dry_run: true` to any print endpoint to validate the request end-to-end (render + printer reachability) without printing or queueing. Returns `{ ok, dry_run, printer_reachable, would_print: { label_size, copies, width_px, height_px } }` — useful for endless media and CI.
- **Print queue**: all jobs are queued and printed sequentially by a single background worker. `/jobs` endpoints to list, get, cancel, reprint, delete and download a job's file, plus a Queue panel. Reprint with the same settings or re-open a job's parameters; image/PDF job files persist for `JOB_FILE_TTL_SECONDS` (default 24 h).
- **Queue controls**: pause/resume (`POST /jobs/pause`, `/jobs/resume`), an emergency stop that cancels all waiting jobs (`POST /jobs/stop`), delete a single job (`POST /jobs/{id}/delete`), clear every job (`POST /jobs/clear-all`), and a queue status endpoint (`GET /jobs/queue`).
- **Text + Image labels**: `POST /label/text-image` endpoint and compose tab to print an uploaded image and a text block side by side.
- **Large-batch confirmation guard**: any print request for 10 or more copies must include an explicit `confirm_large_batch` flag, otherwise it is rejected with HTTP 400 and the machine-readable code `CONFIRMATION_REQUIRED`. The UI confirms before submitting such a batch.
- **Dynamic keep-alive**: keep-alive can run `forever` or in a `timed` mode that keeps the printer awake only for a configurable window after each print (`keep_alive_mode`, `keep_alive_duration_seconds`), plus an always-visible keep-alive toggle in the top bar.
- **Configurable uploads**: ephemeral, configurable upload folder (`UPLOAD_FOLDER`) with TTL cleanup of staged job/share files (`JOB_FILE_TTL_SECONDS`, `SHARE_TTL_SECONDS`).
- **Demo mode**: a bundled demo layer (`src/static/js/demo.js`) lets the static UI run on GitHub Pages with mocked API data and no backend, plus a `pages.yml` deploy workflow.
- **Testing & tooling**: unit tests (URI validation, IPP client, settings, printer status) and project tooling (Dependabot, dependency audit, lint/test configuration).

### Changed
- Reworked the web UI into a "Console" layout: sidebar navigation, light/dark themes that follow the system preference, a fully responsive and iOS-friendly experience, and Settings as its own dedicated view.
- Invalid input now returns HTTP 400 (instead of 500), and image uploads are hardened.
- Expanded documentation for settings, environment variables and API endpoints.
- **Python 3.11 is now the minimum** (was 3.9, which reached end of life in October 2025). The Docker image moves to `python:3.11-slim`. Running from source on 3.9 or 3.10 is no longer supported. This also unblocks current Pillow, urllib3 and requests releases.
- Dependencies: Pillow 10.4.0 → 12.3.0, flask-cors 4.0.0 → 6.0.5, gunicorn 21.2.0 → 23.0.0. `marshmallow`, `pydantic` and `python-dotenv` were unused and have been dropped; `pytest`/`pytest-cov` moved to `requirements-dev.txt` so test tooling stays out of the runtime image.

### Removed
- **The P-touch tape sizes (`pt12`, `pt18`, `pt24`, `pt36`) are no longer offered.** They are TZe cassettes for Brother's P-touch machines, not DK rolls: no QL printer can take one mechanically, and this app has only ever offered QL models to print with. Selecting one could never produce a label, so the four entries have been dropped from the label list and from the API's `label_size` enum. Nothing else changes — the printing library still knows them, and no supported medium is affected.

### Security
- Optional API-key authentication; printer-URI scheme allowlist with an SSRF guard; path-traversal protection on served job/share files; image decompression-bomb limit.

### Fixed
- **Labels are rendered at the loaded roll's real printable width** instead of a hardcoded 696 px (62 mm). Narrower media was previously drawn too wide and rescaled on the way to the printer, so the requested font size never matched what came out — on 50 mm tape everything printed about 20% small and soft. Affects text, images, PDF pages and the combined text+QR and text+image layouts (reported and diagnosed by [MSanteler](https://github.com/MSanteler) in [#18](https://github.com/dodoooh/brother_ql_app/pull/18)).
- **Text on die-cut labels works at all.** The canvas is now pinned to the label's fixed physical height, which `brother_ql` requires; before, any other height was rejected outright.
- **Die-cut labels work for every kind of content, not just plain text.** Images, PDF pages, QR codes and both combined layouts (text+QR, text+image) were rejected outright on die-cut media, because `brother_ql` requires a die-cut image to be exactly the label's own size and only the plain-text path produced one. A QR code was always sent at its own configured size and came back as "Bad image dimensions". Every content type is now centre-fitted onto the label's exact canvas. Affects round and rectangular die-cut media alike.
- **Images on round labels are no longer a third smaller than they could be.** The fit measured the image's rectangle, and a square only fits inside a circle at about 71% of its width, so every picture was shrunk by that much whatever it contained — a design drawn to the edge of a 24 mm round label printed at 13 mm, ringed by bare paper. Round media now measures the ink instead, so artwork that is itself round, or simply has empty corners, fills the label: the same design now prints at 19 mm. A photograph with ink in its corners is unchanged, because there the rectangle really is the content — as it is for QR codes, whose quiet zone is part of the symbol and is deliberately left alone.
- **Round labels (`d12`, `d24`, `d58`) are laid out to the circle instead of the square around it.** The printable area of a round label is a circle, but the raster is its bounding square, so anything near the corners used to be printed onto the backing paper and lost. Text is now measured against the width actually available at each line's own height, which keeps a single centred line close to the full diameter — fitting everything into the largest square inside the circle would have cost about a third of the font size for nothing. Images and QR codes are fitted so they sit inside the circle, and a small rim margin absorbs the tolerance of the die cut.
- **Text on die-cut labels is centred vertically** instead of starting near the top edge, which on a round label is the narrowest part of the medium.
- **Words are no longer broken in the middle on die-cut labels.** `auto_fit` only tested whether the text block fit the label, and chopping a word in half satisfies that just as well as shrinking the font does: "Kalibriert 2026" on a 24 mm round label came out as "Kalibrier" / "t 2026" at the default font size, although it fits on one unbroken line two steps down. Die-cut media now reduces the font until whole words fit, the same rule continuous tape already followed. Rectangular die-cut labels such as `62x29` had the same fault.
- **Combined text+image labels no longer fail on narrow media.** The layout reserved a fixed padding that exceeded the printable width below about 120 px, leaving no room for the image column and raising an error — this broke `d12` and 12 mm continuous tape alike. The padding now scales with the label; nothing changes at 24 mm and above.
- **Rotation now has an effect.** The image was rotated once by the app and a second time by `brother_ql`, which returned it to its original orientation — the log said "Rotation applied" and the label came out unrotated. Image and PDF prints also rotate before being fitted to the label, instead of after, which used to leave them narrower than the tape and scaled back up.
- Descenders on the last line are no longer clipped: line height is measured from the font's ascent+descent rather than the ink bounding box, which only covers the glyphs actually present.
- Text on narrow continuous rolls no longer degrades into a column of single letters. With the true width in play, a word can be wider than a 12 mm or P-touch label; the new `auto_fit` setting (on by default) shrinks the font until every word fits a line instead of hard-breaking it. On die-cut labels it shrinks to the fixed height instead. Disable per request with `settings.auto_fit: false`.
- **`QL-1100NWB` was never a real model.** It was offered in the printer dropdown and accepted by the API, but `brother_ql` has no such model, so choosing it failed at print time. The actual device is the **QL-1110NWB**, which was missing; it and the **QL-600** are now offered instead. The printer list in the UI and in the API specification is once again exactly the set the printing library supports.
- Keep-alive now writes to the printer's raw port (`9100`) instead of only reading status, so it can actually prevent the auto power-off on network printers.
- Corrected the port example in `docker-compose.yml` (5000).

## [3.1.0] - 2025-08-18

### Added
- Support for additional printer models: QL-1100, QL-1100NWB, QL-1115NWB (thanks to DL6ER)
- Support for more label types and sizes including 12+17, 18, 62red, 103, 104, 54x29, 60x86, 103x164, pt12, pt18, pt24, pt36 (thanks to DL6ER)
- Improved UI with dropdown selection for printer models and label types (thanks to DL6ER)
- Updated to brother-ql-inventree 1.3 library for enhanced printer support (thanks to DL6ER)
- Added USB printer support with automatic backend detection (thanks to DL6ER)
- Added documentation for USB printer configuration (thanks to DL6ER)

### Changed
- Fixed port mapping in docker-compose.yml from 5055:5000 to 5000:5000 (thanks to DL6ER)

## [3.0.0] - 2025-04-23

### Breaking Changes
- Complete rebuild of the application with API-first approach
- Restructured project layout for better maintainability
- Simplified to English-only interface

### Added
- OpenAPI/Swagger specification for all API endpoints
- Comprehensive API documentation with Swagger UI
- Modular architecture with clear separation of concerns
- Improved error handling with structured error responses
- Detailed logging with structured logs
- New frontend with responsive design
- Dark mode support with automatic system preference detection
- Image preview functionality
- Printer status checking endpoint
- Multiple printer support with configuration
- Improved printer keep alive feature to prevent printer from shutting down without printing blank labels
- API endpoints for controlling the keep alive feature
- UI controls for the keep alive feature
- Docker and Docker Compose support
- Development setup documentation
- Automated release creation with changelog generation
- QR code generation and printing functionality
- Combined text+QR code label layouts with customizable positioning
- Docker image deployment to DockerHub in addition to GitHub Container Registry
- GitHub workflow for automated Docker image building and publishing
- Live preview for all label types (text, image, QR code, and combined layouts)
- New API endpoints for QR code printing (/api/v1/qrcode/print)
- New API endpoints for combined text+QR code labels (/api/v1/label/text-qrcode)
- Printer keep alive API endpoints (/api/v1/printers/keep-alive)
- Toast notifications for success and error messages
- Printer status indicator in the navigation bar
- Enhanced documentation for Docker deployment options

### Changed
- Improved settings management
- Enhanced image processing
- Updated frontend with modern design using Bootstrap 5 and Bootstrap Icons
- Refactored API endpoints for consistency
- Improved error messages and handling
- Updated documentation with comprehensive examples
- Enhanced notification system with toast messages
- Improved form validation and user feedback
- More robust run scripts with better error handling
- Completely redesigned web interface with modern Bootstrap 5 and Bootstrap Icons
- Responsive design for mobile, tablet, and desktop devices
- Tabbed interface for different label types (text, image, QR code, text+QR)
- Collapsible settings panel for better space utilization
- Enhanced form controls with intuitive icons and better organization
- Improved API documentation with comprehensive examples
- Enhanced error responses with detailed information
- Updated dependencies to latest versions
- Improved Docker build process for smaller image size
- Optimized container startup time
- Improved image processing for better label quality
- Optimized JavaScript code for better performance
- Added structured CSS with CSS variables for easier theming

### Fixed
- Error handling for printer connection issues
- Image rotation and processing
- Settings validation
- Font handling for text printing
- API response consistency
- File upload handling
- Settings controller bug with request body handling
- Dependency conflicts between Flask and Connexion
- UI rendering issues on different screen sizes
- Edge case in printer connection handling
- Error recovery for network connectivity issues
- Form validation to prevent invalid submissions

### Removed
- Multi-language support in favor of a simplified English-only interface
- Legacy file structure
- Outdated configuration files

## [2.1.1] - 2024-03-15

### Fixed
- Small bugfixes

## [2.1.0] - 2024-02-20

### Added
- Multilanguage Support: Added support for multiple languages
- Enhanced Web UI: Updated the web user interface for a more attractive design
- Made UI fully responsive for mobile devices

### Changed
- Improved user interface design
- Enhanced mobile responsiveness

## [2.0.1] - 2024-01-30

### Added
- Enhanced API functionality allowing dynamic settings like printer_uri, dither, and more to be passed via POST requests for both text and image printing
- Implemented image support for both the API and Web-GUI, enabling users to upload, process, and print images directly

## [2.0.0] - 2024-01-15

### Added
- Intermediate release with partial improvements
- Enhanced web interface
- Improved API functionality
- Better error handling
- Additional printer support

## [1.2.1] - 2023-06-20

### Added
- Modern, responsive UI with collapsible settings for better usability
- Dynamic result section with a copy-to-clipboard button for JSON output
- Automatic <br> conversion for line breaks in text input

### Changed
- Improved error handling and streamlined functionality

## [1.2.0] - 2023-05-15

### Added
- Modern, responsive UI with collapsible settings for better usability
- Dynamic result section with a copy-to-clipboard button for JSON output
- Automatic <br> conversion for line breaks in text input

### Changed
- Improved error handling and streamlined functionality

## [1.0.1] - 2023-02-10

### Changed
- Automated release with minor improvements and bug fixes

## [1.0.0] - 2023-01-15

### Added
- Initial release
- Basic web interface for printing text and images
- API for text and image printing
- Settings management
- Multi-language support
- Docker support
