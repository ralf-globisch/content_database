FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        mediainfo \
        curl \
        unzip \
    && curl -fsSL https://github.com/duckdb/duckdb/releases/latest/download/duckdb_cli-linux-amd64.zip -o /tmp/duckdb.zip \
    && unzip /tmp/duckdb.zip -d /usr/local/bin \
    && rm /tmp/duckdb.zip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY catalogue.py app.py queries.yaml ./

ENTRYPOINT ["python", "catalogue.py"]
