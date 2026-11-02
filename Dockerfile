FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libopus-dev \
    nodejs \
    libffi-dev \
    libnacl-dev \
    python3-dev \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    (pip install --no-cache-dir -U -r requirements.txt || \
     pip install --no-cache-dir -U "discord.py[voice]>=2.7.1" "davey>=0.1.0" "yt-dlp[default]" "python-dotenv>=1.0.1" "PyNaCl>=1.5.0")

COPY main.py cookies.enc ./

CMD ["python", "main.py"]
