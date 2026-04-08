# FamDoc: Telegram bot + Mini App (FastAPI/uvicorn) in one process.
FROM python:3.12-slim-bookworm

WORKDIR /app

# PyMuPDF wheels are manylinux; no extra libs required for most PDF ops.
RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY webapp ./webapp
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    WEBAPP_HOST=0.0.0.0 \
    WEBAPP_PORT=8080 \
    FAMDOC_DATA_DIR=/data

RUN mkdir -p /data/files

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
