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
    "copies": 1,
    "cut_mode": "each",  # each | end | none
    "dpi_600": False,
    "hq": True,
    "keep_alive_enabled": False,
    "keep_alive_interval": 60,  # seconds
    "keep_alive_mode": "forever",  # forever | timed
    "keep_alive_duration_seconds": 7200,  # used when mode == "timed" (default 2h)
    "ipp_port": 631,  # IPP port for network status/keep-alive (IANA standard)
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
