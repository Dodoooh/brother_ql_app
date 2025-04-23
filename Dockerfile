FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libssl-dev \
    fonts-dejavu \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Create necessary directories
RUN mkdir -p /app/uploads /app/src/config

# Create a non-root user and group
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

# Create necessary directories (including /app/data for volume mount point)
RUN mkdir -p /app/uploads /app/data /app/src/config

# Copy application code
COPY . .

# Set permissions and ownership
# Give execute permissions to entrypoint, ensure appuser owns necessary dirs
COPY docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh && \
    chown -R appuser:appgroup /app && \
    # Ensure the volume mount points are owned by appuser
    chown appuser:appgroup /app/data && \
    chown appuser:appgroup /app/uploads

# Switch to the non-root user
USER appuser

# Expose port
EXPOSE 5000

# Set the entrypoint (will run as appuser)
ENTRYPOINT ["/app/docker-entrypoint.sh"]
