# Brother QL Printer App - Release 3.0.1

Release Date: April 23, 2025

## Overview

This is a security update release that addresses several vulnerabilities in dependencies. It is recommended to update to this version as soon as possible.

## Security Updates

This release includes the following security updates:

- **Pillow**: Updated from 10.0.1 to 10.3.0+ to fix:
  - CVE-2023-50447 (CVSS 9.3 - Critical): A vulnerability in the parsing of TIFF files
  - CVE-2024-28219 (CVSS 7.3 - High): A vulnerability in the image processing functionality

- **flask-cors**: Updated from 4.0.0 to 4.0.2 to fix:
  - CVE-2024-6221 (CVSS 8.7 - High): A vulnerability in the CORS handling
  - CVE-2024-1681 (CVSS 5.3 - Medium): A vulnerability in the request processing

- **pip**: Updated to 23.3+ to fix:
  - CVE-2023-5752 (CVSS 6.8 - Medium): A vulnerability in the package installation

- **setuptools**: Updated to 70.0.0+ to fix:
  - CVE-2022-40897 (CVSS 8.7 - High): A vulnerability in the package installation
  - CVE-2024-6345 (CVSS 7.5 - High): A vulnerability in the package handling

## Known Issues

- **werkzeug**: Due to compatibility constraints with connexion 2.14.2, we cannot update werkzeug to version 3.0.6+ which would fix several vulnerabilities (CVE-2024-34069, CVE-2024-49767, CVE-2024-49766, CVE-2023-46136). This will be addressed in a future release by updating connexion.

## Installation

### Docker (Recommended)

```bash
# Pull the latest image
docker pull ghcr.io/dodoooh/brother_ql_app:3.0.1

# Run the container
docker run -d -p 5000:5000 --name brother_ql_app ghcr.io/dodoooh/brother_ql_app:3.0.1
```

### Docker Compose

```bash
# Update your docker-compose.yml to use version 3.0.1
# Then run:
docker-compose up -d
```

### Manual Installation

```bash
# Clone the repository
git clone https://github.com/dodoooh/brother_ql_app.git
cd brother_ql_app

# Checkout the 3.0.1 tag
git checkout 3.0.1

# Install dependencies
pip install -r requirements.txt

# Run the application
./run_app.sh
```

## Compatibility

This release maintains full compatibility with version 3.0.0. No API changes or breaking changes have been introduced.
