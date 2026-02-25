FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system deps (ping AND tzdata for timezone support)
RUN apt-get update && apt-get install -y --no-install-recommends \
    iputils-ping \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Set the container-wide timezone
ENV TZ=America/New_York

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .env secrets.env ./

RUN mkdir -p /app/config
ENV CONFIG_DIR=/app/config

CMD ["python", "bot.py"]