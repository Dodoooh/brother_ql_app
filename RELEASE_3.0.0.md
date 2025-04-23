# Brother QL Printer App v3.0.0

## Overview

Version 3.0.0 represents a major update to the Brother QL Printer App with a complete rebuild using an API-first approach. This release introduces significant improvements to the architecture, user interface, and functionality.

## New Features

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
- Improved printer keep alive feature
- API endpoints for controlling the keep alive feature
- UI controls for the keep alive feature
- Docker and Docker Compose support
- Development setup documentation
- Automated release creation with changelog generation

## Improvements

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

## Bug Fixes

- Error handling for printer connection issues
- Image rotation and processing
- Settings validation
- Font handling for text printing
- API response consistency
- File upload handling
- Settings controller bug with request body handling
- Dependency conflicts between Flask and Connexion

## Removed

- Multi-language support in favor of a simplified English-only interface
- Legacy file structure
- Outdated configuration files

## Docker Images

```
docker pull ghcr.io/dodoooh/brother_ql_app:v3.0.0
docker pull dodoooh/brother_ql_app:v3.0.0
```

## Upgrading from v2.x

This is a major release with breaking changes. Users upgrading from version 2.x should be aware of the following:

1. API endpoints have been restructured for better organization and consistency
2. Settings format has been updated
3. Multi-language support has been removed

For more details, see the [changelog](changelog.md).
