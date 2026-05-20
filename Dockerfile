# Use an official lightweight Python image
FROM python:3.10-slim

# Install system dependencies required for OpenClaw and networking
# We do NOT skip apt-get here because Render's build environment has a stable, fast connection.
RUN apt-get update && apt-get install -y 
    curl 
    netcat-openbsd 
    gnupg 
    && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - 
    && apt-get install -y nodejs 
    && rm -rf /var/lib/apt/lists/*

# Install OpenClaw globally
RUN npm install -g openclaw

# Set working directory
WORKDIR /app

# Install Python dependencies
RUN pip install --no-cache-dir python-telegram-bot httpx

# Copy project files to container
COPY bot.py .
COPY start.sh .

# Make start script executable
RUN chmod +x start.sh

# Set default environment variables
ENV BALE_TOKEN=""
ENV PYTHONUNBUFFERED=1

# Expose the gateway port
EXPOSE 18789

# Entry point
ENTRYPOINT ["./start.sh"]
