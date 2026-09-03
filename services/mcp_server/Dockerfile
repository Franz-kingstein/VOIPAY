FROM python:3.11-slim

WORKDIR /app

# Install dependencies (requires gcc for some postgres builds if not binary)
RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY services/mcp_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/mcp_server /app/services/mcp_server
COPY shared /app/shared

WORKDIR /app/services/mcp_server

EXPOSE 8002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
