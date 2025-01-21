# Use a slim Python base image
FROM python:3.11-slim

# Cache dependencies
RUN --mount=type=cache,target=/root/.cache/pip pip install pyyaml

# Install FFmpeg
RUN apt-get update && apt-get install -y ffmpeg && apt-get clean && rm -rf /var/lib/apt/lists/*

# Set the default working directory
WORKDIR /app
