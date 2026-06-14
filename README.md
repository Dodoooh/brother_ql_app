# Brother QL Printer App

[![Docker Pulls](https://img.shields.io/docker/pulls/dodoooh/brother_ql_app)](https://hub.docker.com/r/dodoooh/brother_ql_app)
[![GitHub Release](https://img.shields.io/github/v/release/dodoooh/brother_ql_app)](https://github.com/dodoooh/brother_ql_app/releases)
[![GitHub Issues](https://img.shields.io/github/issues/dodoooh/brother_ql_app)](https://github.com/dodoooh/brother_ql_app/issues)
[![Version](https://img.shields.io/badge/version-3.1.0-blue)](https://github.com/dodoooh/brother_ql_app/blob/main/changelog.md)

A modern web application to control Brother QL printers, enabling customizable text, image, and QR code printing with ease.

## 🚀 Features

- **🖋 Text Printing**: Easily print HTML-formatted text, such as `<b>Bold</b>` or `<span color="red">Red</span>`, for precise label designs.

- **🖼 Image Printing**: Upload and print images effortlessly to create visually appealing labels.

- **📱 QR Code Generation**: Create and print QR codes for URLs, contact information, or any text data.

- **🔀 Combined Layouts**: Print text and QR codes together with customizable positioning.
- **👁️ Live Preview**: See how your labels will look before printing for all label types.

- **⚙️ Custom Settings**: Fine-tune font size, label size, and text alignment to match your specific needs.

- **🔗 API Support**: Seamlessly integrate with external systems like Home Assistant ❤️ via a comprehensive, documented API.

- **🖨 Multiple Printer Support**: Control multiple printers simultaneously via the API, enabling the use of different label sizes and configurations for various tasks.

- **🔄 Printer Keep Alive**: Prevent your printer from shutting down with the keep alive feature.

- **📱 Responsive Design**: Enjoy a smooth user experience on desktop, tablet, and smartphone devices.

- **🌙 Dark Mode**: Modern interface with automatic dark mode support based on system preferences.

- **📚 Swagger Documentation**: Explore and test the API using the built-in Swagger UI documentation.

- **🔄 Error Handling**: Robust error handling with informative messages and toast notifications.

## 📸 Screenshots

### Text Printing
![Text Printing](doc/images/Screenshot_01_text_print.png)

### Image Printing
![Image Printing](doc/images/Screenshot_03_Image_print.png)


### QR Code
![QR Code](doc/images/Screenshot_04_QR_Code.png)

### Text and QR Code
![Text and QR Code](doc/images/Screenshot_05_Text_QR.png)


## 🏗️ Architecture

The application follows a modern, API-first approach with clear separation of concerns:

- **Frontend**: Responsive web interface built with HTML5, CSS3, and JavaScript with Bootstrap 5 and Bootstrap Icons
- **Backend**: Python Flask application with Connexion for OpenAPI/Swagger integration
- **API**: RESTful API with comprehensive documentation and structured error responses
- **Services**: Modular services for printer communication, settings management, QR code generation, etc.

## 🐳 Installation with Docker

### Docker Image

The application is available as a Docker image from DockerHub:

```bash
# DockerHub
docker pull dodoooh/brother_ql_app:latest  # or specific version: dodoooh/brother_ql_app:v3.0.0
```

### Using Docker Compose

Create a `docker-compose.yml` file:

```yaml
version: '3.8'

services:
  brother_ql_app:
    image: dodoooh/brother_ql_app:latest
    container_name: brother_ql_app
    ports:
      - "5000:5000"
    volumes:
      - ./data:/app/data
      - ./uploads:/app/uploads
    restart: unless-stopped
```

Start the service:

```bash
docker-compose up -d
```

### Using Docker Run

Run the application with:

```bash
docker run -d \
  -p 5000:5000 \
  --name brother_ql_app \
  -v ./data:/app/data \
  -v ./uploads:/app/uploads \
  dodoooh/brother_ql_app:latest
```

### Access the Application

Open your browser and navigate to [http://localhost:5000](http://localhost:5000)

## 🛠️ Development Setup

### Prerequisites

- Python 3.8+
- pip
- Git

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/dodoooh/brother_ql_app.git
   cd brother_ql_app
   ```

2. Run the setup script:
   ```bash
   ./run_app.sh
   ```
   
   This script will:
   - Create a virtual environment
   - Install dependencies
   - Start the application

3. Access the application at [http://localhost:5000](http://localhost:5000)

## Configuration

The application settings can be configured in the `data/settings.json` file. This file contains the default printer settings as well as the global options described below.

### Settings Fields

- `printer_uri`: The URI of the printer to use (e.g., `tcp://192.168.1.100` when over network, or `file:///dev/usb/lp0` when using USB)
- `printer_model`: The model of the printer (e.g., `QL-800`)
- `label_size`: The size of the label to print (e.g., `62`)
- `font_size`: The default font size used for text printing (e.g., `50`)
- `alignment`: The default text alignment (`left`, `center`, or `right`)
- `rotate`: The rotation applied to the rendered label in degrees (`0`, `90`, `180`, or `270`)
- `threshold`: The black/white threshold used when converting the image (e.g., `70.0`)
- `dither`: Whether to apply dithering when converting the image (`true`/`false`)
- `compress`: Whether to enable printer-side compression (`true`/`false`)
- `red`: Whether to use the red channel for two-color labels such as 62-red (`true`/`false`)
- `keep_alive_enabled`: Whether to keep the printer connection alive (only needed for network printers)
- `keep_alive_interval`: The interval for keep-alive messages (in seconds, minimum `10`)
- `ipp_port`: The IPP port used to query printer status (default `631`)

### Multiple Printers

To control more than one printer, define a `printers` array. Each entry describes a printer that can be selected via the API; the top-level fields above act as the default printer.

```json
{
    "printer_uri": "tcp://192.168.1.100",
    "printer_model": "QL-820NWB",
    "label_size": "62",
    "font_size": 50,
    "alignment": "left",
    "rotate": 0,
    "threshold": 70.0,
    "dither": false,
    "compress": false,
    "red": false,
    "keep_alive_enabled": true,
    "keep_alive_interval": 30,
    "ipp_port": 631,
    "printers": [
        {
            "id": "default",
            "name": "QL-820NWB (Office)",
            "printer_uri": "tcp://192.168.1.100",
            "printer_model": "QL-820NWB",
            "label_size": "62"
        },
        {
            "id": "label-station",
            "name": "QL-800 (Warehouse)",
            "printer_uri": "tcp://192.168.1.101",
            "printer_model": "QL-800",
            "label_size": "29"
        }
    ]
}
```

Each printer entry supports:

- `id`: A unique identifier used to reference the printer
- `name`: A human-readable label shown in the UI
- `printer_uri`: The URI of this printer (network or USB)
- `printer_model`: The model of this printer
- `label_size`: The default label size for this printer

### Environment Variables

The following environment variables can be set (e.g. in `docker-compose.yml` or via `docker run -e`):

- `API_KEY`: Opt-in API authentication. When set, every request to `/api/v1/*` must include the header `X-API-Key: <your-key>`. The bundled UI, static assets, Swagger UI and the health probes remain unauthenticated. When unset, the API is open.
- `CORS_ORIGINS`: Comma-separated list of allowed cross-origin origins (e.g. `https://home.example.com,https://hass.example.com`). When unset, only same-origin requests are allowed.
- `SECRET_KEY`: The Flask secret key. Set a stable value in production; if unset, an ephemeral random key is generated on each start.
- `ENABLE_SWAGGER_UI`: Set to `true` or `false` to force the Swagger UI on or off. When unset, the UI defaults to on unless `FLASK_ENV=production`.

## 📔 API Documentation

The API is fully documented using OpenAPI/Swagger. You can access the interactive documentation at [http://localhost:5000/api/v1/ui/](http://localhost:5000/api/v1/ui/) when the application is running.

### Available Endpoints

- **GET /api/v1/settings**: Get current settings
- **PUT /api/v1/settings**: Update settings
- **GET /api/v1/printers**: Get available printers
- **POST /api/v1/printers/status**: Check printer status
- **POST /api/v1/text/print**: Print text on a label
- **POST /api/v1/image/print**: Print image on a label
- **POST /api/v1/qrcode/print**: Print QR code on a label
- **POST /api/v1/label/text-qrcode**: Print combined text and QR code on a label
- **POST /api/v1/printers/keep-alive**: Control the printer keep alive feature
- **GET /health**: Liveness probe reporting that the web application process is up
- **GET /health/printer**: Readiness probe reporting whether the configured printer is reachable

## 📤 Example API Usage

### **Text Printing**

Use the `/api/v1/text/print` endpoint to print formatted text.

```python
import requests

url = 'http://localhost:5000/api/v1/text/print'
payload = {
    "text": {
        "content": "Hello World!\nThis is a test print.",
        "font_size": 40,
        "alignment": "center"
    },
    "settings": {
        "printer_uri": "tcp://192.168.1.100",
        "printer_model": "QL-800",
        "label_size": "62",
        "rotate": 90,
        "threshold": 70.0,
        "dither": True,
        "compress": True,
        "red": False
    }
}

response = requests.post(url, json=payload)

if response.status_code == 200:
    print("Success:", response.json())
else:
    print("Error:", response.status_code, response.json())
```

### **Image Printing**

Use the `/api/v1/image/print` endpoint to upload and print an image.

```python
import requests
import json

url = 'http://localhost:5000/api/v1/image/print'
image_path = '/path/to/image.jpg'  # Replace with the path to your image
settings = {
    "settings": {
        "printer_uri": "tcp://192.168.1.100",
        "printer_model": "QL-800",
        "label_size": "62",
        "rotate": 180,
        "threshold": 70.0,
        "dither": False,
        "compress": False,
        "red": True
    }
}

with open(image_path, 'rb') as img_file:
    files = {
        'image': img_file
    }
    data = {
        'settings': json.dumps(settings["settings"])
    }
    response = requests.post(url, files=files, data=data)

if response.status_code == 200:
    print("Success:", response.json())
else:
    print("Error:", response.status_code, response.json())
```

### **QR Code Printing**

Use the `/api/v1/qrcode/print` endpoint to generate and print a QR code.

```python
import requests

url = 'http://localhost:5000/api/v1/qrcode/print'
payload = {
    "qr": {
        "data": "https://github.com/dodoooh/brother_ql_app",
        "box_size": 10,
        "border": 4,
        "error_correction": "M",
        "version": 1,
        "size": 400
    },
    "settings": {
        "printer_uri": "tcp://192.168.1.100",
        "printer_model": "QL-800",
        "label_size": "62",
        "rotate": 0,
        "threshold": 70.0,
        "dither": False,
        "compress": False,
        "red": False
    }
}

response = requests.post(url, json=payload)

if response.status_code == 200:
    print("Success:", response.json())
else:
    print("Error:", response.status_code, response.json())
```

### **Combined Text and QR Code**

Use the `/api/v1/label/text-qrcode` endpoint to print text and QR code together.

```python
import requests

url = 'http://localhost:5000/api/v1/label/text-qrcode'
payload = {
    "text": {
        "content": "Product: Widget XYZ\nSKU: 12345\nPrice: $19.99",
        "font_size": 30,
        "alignment": "left"
    },
    "qr": {
        "data": "https://github.com/dodoooh/brother_ql_app",
        "box_size": 10,
        "border": 4,
        "error_correction": "M",
        "position": "right",
        "size": 400,
        "version": 1
    },
    "settings": {
        "printer_uri": "tcp://192.168.1.100",
        "printer_model": "QL-800",
        "label_size": "62",
        "rotate": 0,
        "threshold": 70.0,
        "dither": False,
        "compress": False,
        "red": False
    }
}

response = requests.post(url, json=payload)

if response.status_code == 200:
    print("Success:", response.json())
else:
    print("Error:", response.status_code, response.json())
```

### **Printer Keep Alive**

Use the `/api/v1/printers/keep-alive` endpoint to control the printer keep alive feature.

```python
import requests

url = 'http://localhost:5000/api/v1/printers/keep-alive'

# Enable keep alive feature
payload = {
    "settings": {
        "printer_uri": "tcp://192.168.1.100",
        "printer_model": "QL-800"
    },
    "keep_alive": {
        "enabled": True,
        "interval": 300  # Interval in seconds (5 minutes)
    }
}

response = requests.post(url, json=payload)

if response.status_code == 200:
    print("Success:", response.json())
else:
    print("Error:", response.status_code, response.json())

# Disable keep alive feature
payload = {
    "settings": {
        "printer_uri": "tcp://192.168.1.100",
        "printer_model": "QL-800"
    },
    "keep_alive": {
        "enabled": False
    }
}

response = requests.post(url, json=payload)

if response.status_code == 200:
    print("Success:", response.json())
else:
    print("Error:", response.status_code, response.json())
```

## 🔄 Keep Alive

Network Brother QL printers tend to power themselves off automatically after a period of inactivity. The keep-alive feature periodically writes to the printer's raw port (`9100`) at the configured `keep_alive_interval` to keep the connection warm and reduce the chance of an unexpected power-off.

> **Note:** Keep-alive is a best-effort mitigation. The only guaranteed way to prevent the printer from shutting down is to disable the device's own power-saving setting (set **Auto Power Off = Off** in the printer's menu).

## 📝 License

This project is licensed under the [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International Public License](LICENSE).

## 🤝 Contributing

Contributions are welcome! Please open an issue or submit a pull request.

## 👏 Acknowledgments

Special thanks to our contributors:

- **DL6ER** - For adding support for additional printer models, label types, and adding USB printer support

## 📄 Changelog

See [CHANGELOG.md](changelog.md) for more information.

---

**Enjoy using the Brother QL Printer App! If you encounter any issues or have suggestions for improvements, feel free to reach out through our [GitHub Issues](https://github.com/dodoooh/brother_ql_app/issues).**
