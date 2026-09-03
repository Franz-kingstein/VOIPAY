FROM python:3.11-slim

WORKDIR /app

# Install dependencies (requires gcc and pg drivers)
RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY services/ledger_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/ledger_service /app/services/ledger_service
COPY shared /app/shared

WORKDIR /app/services/ledger_service

EXPOSE 8089

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8089"]
