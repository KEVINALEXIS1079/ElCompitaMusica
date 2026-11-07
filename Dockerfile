FROM brainicism/bgutil-ytdlp-pot-provider:latest AS bgutil

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libopus-dev \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libjpeg62-turbo \
    libgif7 \
    librsvg2-2 \
    libffi-dev \
    libnacl-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=bgutil /usr/local/bin/node /usr/local/bin/node
COPY --from=bgutil /app /opt/bgutil/server

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    (pip install --no-cache-dir -U -r requirements.txt || \
     pip install --no-cache-dir -U "discord.py[voice]>=2.7.1" "davey>=0.1.7,<0.2.0" "yt-dlp[default]" "python-dotenv>=1.0.1" "PyNaCl>=1.5.0") && \
    (pip install --no-cache-dir -U bgutil-ytdlp-pot-provider || true)

COPY main.py cookies.enc ./

CMD ["python", "main.py"]
