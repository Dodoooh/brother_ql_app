# Brother QL Printer App v4.0.0

## Breaking Changes
- Restructured and cleaned up API for better organization and consistency
- API consumers may need to update their integration to accommodate these changes

## New Features
- QR code generation and printing functionality
- Combined text+QR code label layouts with customizable positioning
- Docker image deployment to DockerHub in addition to GitHub Container Registry
- GitHub workflow for automated Docker image building and publishing
- Printer keep alive feature to prevent printer from shutting down
- Dark mode support with automatic system preference detection
- Live preview for all label types (text, image, QR code, and combined layouts)

## Improvements
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

## Bug Fixes
- UI rendering issues on different screen sizes
- Edge case in printer connection handling
- Error recovery for network connectivity issues
- Image rotation and processing
- Form validation to prevent invalid submissions

## Docker Images
```
docker pull ghcr.io/dodoooh/brother_ql_app:v4.0.0
docker pull dodoooh/brother_ql_app:v4.0.0
```

For more details, see the [changelog](changelog.md).
