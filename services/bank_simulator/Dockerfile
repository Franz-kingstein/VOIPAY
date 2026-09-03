FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY services/bank_simulator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code including shared packages
COPY services/bank_simulator /app/services/bank_simulator
COPY shared /app/shared

WORKDIR /app/services/bank_simulator

EXPOSE 8003

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8003"]
