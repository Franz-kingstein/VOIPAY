FROM python:3.11-slim

WORKDIR /app

COPY services/merchant_app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/merchant_app /app/services/merchant_app
COPY shared /app/shared

WORKDIR /app/services/merchant_app

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
