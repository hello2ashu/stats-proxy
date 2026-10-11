# Stage 1: Build Nushell
FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        pkg-config \
        libssl-dev \
        build-essential \
        rustc \
        cargo \
        ca-certificates \
    && cargo install nu --locked

# Stage 2: Production Image
FROM python:3.13-slim
 
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Los_Angeles
 
# Core utilities (Removed build-essential, rustc, cargo, pkg-config, and libssl-dev)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata \
        curl \
        ca-certificates \
        wget \
        openssh-client \
        iputils-ping \
        dnsutils \
        traceroute \
        iproute2 \
        netcat-openbsd \
        vim \
    && rm -rf /var/lib/apt/lists/*
 
# Copy the compiled Nushell binary into a standard system path
COPY --from=builder /root/.cargo/bin/nu /usr/local/bin/nu

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .
 
USER nobody
EXPOSE 4321
 
# /health never touches upstream apps, so a slow Paperless/BookOrbit can't mark the proxy unhealthy
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4321/health', timeout=3)"
 
CMD ["python", "server.py"]
