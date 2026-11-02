FROM node:22-bookworm-slim AS bgutil-builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    python3 \
    make \
    g++ \
    pkg-config \
    libcairo2-dev \
    libpango1.0-dev \
    libjpeg-dev \
    libgif-dev \
    librsvg2-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/bgutil
RUN git clone --single-branch --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git . && \
    cd server && \
    npm ci && \
    npx tsc

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libopus-dev \
    libffi-dev \
    libnacl-dev \
    python3-dev \
    build-essential \
    ca-certificates \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libjpeg62-turbo \
    libgif7 \
    librsvg2-2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=bgutil-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=bgutil-builder /opt/bgutil/server /opt/bgutil/server

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    (pip install --no-cache-dir -U -r requirements.txt || \
     pip install --no-cache-dir -U "discord.py[voice]>=2.7.1" "davey>=0.1.0" "yt-dlp[default]" "bgutil-ytdlp-pot-provider" "python-dotenv>=1.0.1" "PyNaCl>=1.5.0")

COPY main.py cookies.enc ./

CMD ["python", "main.py"]
