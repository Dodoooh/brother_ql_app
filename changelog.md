# Changelog

All notable changes to the Brother QL Printer App will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
