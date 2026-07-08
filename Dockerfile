FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV RAG_API_HOST=0.0.0.0
ENV RAG_API_PORT=18200
ENV RAG_MODE=ingest
ENV RAG_LOCAL_STORAGE_DIR=/data/storage

RUN mkdir -p /data/storage

EXPOSE 18200

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18200"]
