# 3.11 and not 3.12, though the reason has changed and the old note was wrong
# twice over: it blamed pysnmp's use of asyncore (it is `imp`, in
# pysnmp/smi/builder.py) and packbits' distutils sdist (packbits installs on
# 3.12 today). pysnmp itself is gone now, and every remaining requirement
# installs and imports on 3.12.
#
# What held this at 3.11 was connexion 2.14.2, which pinned Werkzeug to 2.2.3
# and Flask to 2.2.5, neither of them tested against 3.12. That pin is gone with
# the move to connexion 3, so 3.12 is now a question of running the suite
# against it rather than of a dependency forbidding it. Still a change of its
# own, not a side effect of this one.
#
# 3.9 is EOL and locks the image out of the current Pillow/urllib3.
FROM python:3.11-slim

WORKDIR /app

# UPLOAD_FOLDER is set explicitly so the app never falls back to its
# code-relative default (/app/src/uploads). The code tree belongs to root and is
# not writable by the runtime user (see the ownership block below), so a
# fallback there would fail at startup rather than at the first upload.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UPLOAD_FOLDER=/app/uploads

# Runtime system dependencies only. build-essential, libffi-dev and libssl-dev
# are deliberately absent: every requirement resolves to a wheel on amd64 and
# arm64, and the one sdist (packbits) is pure Python. Leaving a compiler and
# apt in a running container is handing post-exploitation tooling to whoever
# gets in -- and it is 437 MB of it, measured.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

RUN mkdir -p /app/uploads /app/data

# Copy application code. Only the files the container actually runs -- tests,
# docs, screenshots and CI config have no business in the published image.
COPY src/ ./src/
COPY asgi.py ./
# CC BY-NC-SA 4.0 requires the licence notice to travel with the distribution,
# and a published image counts as one.
COPY LICENSE ./

COPY docker-entrypoint.sh /app/

# The application code belongs to root and is not writable by the user the app
# runs as. Only the two data directories are.
#
# It used to be `chown -R appuser /app`, which meant the process could rewrite
# its own source: a pentest against this image overwrote app.py and
# docker-entrypoint.sh as the runtime user, turning any file-write bug in the
# upload or share handling into code that survives a restart. There is no
# reason for a running app to be able to edit itself.
RUN chmod +x /app/docker-entrypoint.sh \
 && chown -R root:root /app \
 && chmod -R go-w /app \
 && chown appuser:appgroup /app/data /app/uploads \
 && chmod 750 /app/data /app/uploads

USER appuser

# Expose port
EXPOSE 5000

# Liveness check against the lightweight /health endpoint (no printer access).
# python -c avoids needing curl/wget in the slim image.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/health',timeout=2).status==200 else 1)"

# Set the entrypoint (will run as appuser)
ENTRYPOINT ["/app/docker-entrypoint.sh"]
