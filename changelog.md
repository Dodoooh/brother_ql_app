# Changelog

All notable changes to the Brother QL Printer App will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.0.0] - 2025-04-23

### Breaking Changes
- Restructured and cleaned up API for better organization and consistency
- API consumers may need to update their integration to accommodate these changes

### Added
- QR code generation and printing functionality
- Combined text+QR code label layouts with customizable positioning
- Docker image deployment to DockerHub in addition to GitHub Container Registry
- GitHub workflow for automated Docker image building and publishing
- Printer keep alive feature to prevent printer from shutting down
- Dark mode support with automatic system preference detection
- Live preview for all label types (text, image, QR code, and combined layouts)
- New API endpoints for QR code printing (/api/v1/qrcode/print)
- New API endpoints for combined text+QR code labels (/api/v1/label/text-qrcode)
- Printer keep alive API endpoints (/api/v1/printers/keep-alive)
- Toast notifications for success and error messages
- Printer status indicator in the navigation bar
- Enhanced documentation for Docker deployment options

### Changed
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
- UI rendering issues on different screen sizes
- Edge case in printer connection handling
- Error recovery for network connectivity issues
- Image rotation and processing
- Form validation to prevent invalid submissions

## [3.0.1] - 2025-04-23

### Added
- Complete rebuild of the application with API-first approach
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

### Changed
- Restructured project layout for better maintainability
- Improved settings management
- Enhanced image processing
- Updated frontend with modern design using Bootstrap 5 and Bootstrap Icons
- Refactored API endpoints for consistency
- Improved error messages and handling
- Updated documentation with comprehensive examples
- Simplified to English-only interface
- Enhanced notification system with toast messages
- Improved form validation and user feedback
- More robust run scripts with better error handling

### Fixed
- Error handling for printer connection issues
- Image rotation and processing
- Settings validation
- Font handling for text printing
- API response consistency
- File upload handling
- Settings controller bug with request body handling
- Dependency conflicts between Flask and Connexion

### Removed
- Multi-language support in favor of a simplified English-only interface
- Legacy file structure
- Outdated configuration files

## [2.0.0] - 2024-01-15

### Added
- Intermediate release with partial improvements
- Enhanced web interface
- Improved API functionality
- Better error handling
- Additional printer support

## [1.0.0] - 2023-01-15

### Added
- Initial release
- Basic web interface for printing text and images
- API for text and image printing
- Settings management
- Multi-language support
- Docker support
