FROM python:3.11-slim

WORKDIR /app

COPY services/idempotency_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/idempotency_service /app/services/idempotency_service
COPY shared /app/shared

WORKDIR /app/services/idempotency_service

EXPOSE 8005

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8005"]
