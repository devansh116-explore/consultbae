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

# Initialize database by running merge pipeline
RUN python3 db/merge_pipeline.py

# Create uploads directory with proper permissions
RUN mkdir -p uploads && chmod 755 uploads

# Expose port 5000
EXPOSE 5000

# Set environment variables
ENV FLASK_APP=app/app.py
ENV FLASK_ENV=production
ENV FLASK_DEBUG=0

# Run with Gunicorn (production WSGI server)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--timeout", "120", "app.app:app"]
