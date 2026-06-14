"""Gunicorn entrypoint for the Brother QL Web-App.

Gunicorn imports this module and serves the ``application`` callable, e.g.::

    gunicorn --workers 1 --threads 4 --bind 0.0.0.0:5000 wsgi:application

Connexion 2.x wraps a Flask app: ``create_app()`` returns a ``connexion.App``
whose underlying Flask/WSGI app is exposed via ``.app``. Importing this module
runs ``create_app()`` once, which performs the startup tasks (Pillow patch,
config init) and starts the keep-alive background thread via
``init_keep_alive()``.

IMPORTANT: run gunicorn with ``--workers 1`` and WITHOUT ``--preload``:
  * multiple workers would each spawn their own keep-alive thread and clobber
    the ``printer_service`` singleton state;
  * ``--preload`` would start the keep-alive thread in the master process
    before forking, so the thread would not survive the fork.
"""

import os
import sys

# Connexion resolves OpenAPI operationIds like ``api.printer_controller.xxx``
# by importing the ``api`` package, which lives under ``src/``. When started
# via ``python src/app.py`` that directory is sys.path[0] automatically, but
# gunicorn imports this module from the repo root, so we add ``src`` explicitly.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from src.app import create_app

# The WSGI callable gunicorn serves (Connexion's underlying Flask app).
application = create_app().app
