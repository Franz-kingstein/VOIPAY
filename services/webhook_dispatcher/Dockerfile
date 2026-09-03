FROM python:3.11-slim

WORKDIR /app

COPY services/webhook_dispatcher/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/webhook_dispatcher /app/services/webhook_dispatcher
COPY shared /app/shared

WORKDIR /app/services/webhook_dispatcher

EXPOSE 8006

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8006"]
