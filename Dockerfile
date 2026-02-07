FROM python:3.12-alpine

LABEL maintainer="Parallax Intelligence Partnership, LLC"
LABEL description="OpenScanHub - Universal network scanner bridge"

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY openscan/ openscan/

# Config and output volumes
VOLUME ["/config", "/scans"]

ENV OPENSCAN_DOCKER=1
ENV OPENSCAN_CONFIG_DIR=/config

EXPOSE 8020

CMD ["python", "-m", "openscan", "--no-browser"]
