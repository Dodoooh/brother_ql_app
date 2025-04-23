"""
Default settings for the Brother QL Printer App.
These settings are used when no user-defined settings are available.
"""

DEFAULT_SETTINGS = {
    "printer_uri": "tcp://192.168.1.100",
    "printer_model": "QL-800",
    "label_size": "62",
    "font_size": 50,
    "alignment": "left",
    "rotate": 0,
    "threshold": 70.0,
    "dither": False,
    "compress": False,
    "red": False,
    "keep_alive_enabled": False,
    "keep_alive_interval": 60,  # seconds
    "printers": [
        {
            "id": "default",
            "name": "Default Printer",
            "printer_uri": "tcp://192.168.1.100",
            "printer_model": "QL-800",
            "label_size": "62"
        }
    ]
}
