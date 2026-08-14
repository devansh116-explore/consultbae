# Use official Python runtime as base image
FROM python:3.11-slim

# Set working directory in container
WORKDIR /app

# Install system dependencies (including ffmpeg for audio processing)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy entire application
COPY . .

# Create uploads directory with proper permissions  
RUN mkdir -p uploads && chmod 755 uploads && \
    # Verify data files exist
    ls -lah data/ && \
    echo "Data files OK"

# Expose port 5000
EXPOSE 5000

# Set environment variables
ENV FLASK_APP=app/app.py
ENV FLASK_ENV=production
ENV FLASK_DEBUG=0

# Run with Gunicorn using WSGI entry point
# Note: Database initialization may take time on first boot (60+ seconds for merge pipeline)
# The wsgi.py file will initialize the database at startup
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "2", \
     "--worker-class", "sync", \
     "--timeout", "180", \
     "--graceful-timeout", "30", \
     "--keep-alive", "5", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "wsgi:app"]
