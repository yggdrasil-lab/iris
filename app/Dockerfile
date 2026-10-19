# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Prevent Python from writing pyc files to disc and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies & create unprivileged user
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -m -s /bin/bash appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the current directory contents into the container at /app/app
# Context is ./app, so we copy content to a new 'app' folder to preserve package structure
COPY --chown=appuser:appuser . app/

# Expose port 8000 (HTTP) and 2525 (SMTP)
EXPOSE 8000
EXPOSE 2525

USER appuser

# Run the unified application
CMD ["python", "-m", "app.run"]
