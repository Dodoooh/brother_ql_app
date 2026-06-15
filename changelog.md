# Changelog

All notable changes to the Brother QL Printer App will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Security
- Optional API-key authentication; printer-URI scheme allowlist with an SSRF guard; path-traversal protection on served job/share files; image decompression-bomb limit.

### Fixed
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
