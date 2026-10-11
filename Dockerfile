# Stage 1: Fetch Nushell Binary
FROM python:3.13-slim AS nushell-fetcher
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates tar && rm -rf /var/lib/apt/lists/*

# Fix: Corrected the full GitHub release URL path and variable interpolation ($NU_ARCH)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then NU_ARCH="x86_64-unknown-linux-musl"; \
    elif [ "$ARCH" = "aarch64" ]; then NU_ARCH="aarch64-unknown-linux-musl"; \
    else echo "Unsupported architecture: $ARCH" && exit 1; fi && \
    curl -L -o nushell.tar.gz "https://github.com{NU_ARCH}.tar.gz" && \
    tar -xzf nushell.tar.gz --strip-components=1

# Stage 2: Production Image
FROM python:3.13-slim
 
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Los_Angeles
 
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
 
# Copy the verified pre-compiled Nushell binary from Stage 1
COPY --from=nushell-fetcher /nu /usr/local/bin/nu

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .
 
USER nobody
EXPOSE 4321
 
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0', timeout=3)"
 
CMD ["python", "server.py"]
