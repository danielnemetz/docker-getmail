FROM python:3.11-slim

# Install system dependencies if needed (e.g. for some pip packages or debugging)
RUN apt-get update && apt-get install -y \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install getmail6 and msmtp for SMTP delivery
RUN apt-get update && apt-get install -y \
    ca-certificates \
    msmtp \
    msmtp-mta \
    && rm -rf /var/lib/apt/lists/*
RUN pip install getmail6

# Create a non-root user 'getmail'
RUN useradd -m -d /home/getmail -s /bin/bash getmail

# Work directory
WORKDIR /app

# Copy the controller script
COPY run.py /app/run.py

# Create directories for data and set permissions
# IMPORTANT: Create /home/getmail/.getmail so the Docker Volume inherits these permissions!
RUN mkdir -p /data/getmail /data/mail /etc/msmtprc.d /home/getmail/.getmail \
    && chown -R getmail:getmail /data/getmail /data/mail /app /etc/msmtprc.d /home/getmail

# Allow getmail user to write to system-wide msmtp config or use user-local config
# Strategy: We will configure run.py to write to ~/.msmtprc which is cleaner for non-root.

# Environment variables
ENV PYTHONUNBUFFERED=1

# Switch to non-root user
USER getmail

ENTRYPOINT ["python", "/app/run.py"]
