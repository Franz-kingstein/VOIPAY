FROM python:3.11-slim

WORKDIR /app

COPY services/voice_gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/voice_gateway /app/services/voice_gateway
COPY shared /app/shared

WORKDIR /app/services/voice_gateway

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
