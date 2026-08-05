"""Server entrypoint for the Brother QL Web-App.

Gunicorn imports this module and serves the ``application`` callable::

    gunicorn --workers 1 -k uvicorn.workers.UvicornWorker \
             --bind 0.0.0.0:5000 asgi:application

This used to be ``wsgi.py`` and used to hand out ``create_app().app``, the Flask
application underneath Connexion. That stopped being the whole app in Connexion
3: routing, request validation and security moved into ASGI middleware that sits
*above* Flask, and the Flask layer no longer carries the API routes at all.
Serving it alone starts cleanly, answers ``/health`` and the static files, and
returns 404 for every one of the 35 API operations -- a failure that looks like
a success. So what is exported here is the Connexion application itself.

IMPORTANT: run with ``--workers 1`` and WITHOUT ``--preload``:
  * multiple workers would each spawn their own keep-alive thread, print-queue
    worker and relay scheduler, and clobber the ``printer_service`` singleton;
  * ``--preload`` would start those threads in the master process before
    forking, so they would not survive the fork.

Concurrency comes from the worker's own thread pool rather than gunicorn's
``--threads``: the operation handlers are ordinary synchronous functions, and
Connexion runs them off the event loop so a slow render does not block the
server.
"""

import os
import sys

# Connexion resolves OpenAPI operationIds like ``api.printer_controller.xxx``
# by importing the ``api`` package, which lives under ``src/``. When started
# via ``python src/app.py`` that directory is sys.path[0] automatically, but the
# server imports this module from the repo root, so we add ``src`` explicitly.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from src.app import create_app  # noqa: E402 - sys.path must be set up first

# The ASGI callable the server serves: the Connexion app, middleware included.
application = create_app()
