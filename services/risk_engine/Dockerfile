FROM python:3.11-slim

WORKDIR /app

# Install build dependencies if needed, then python libraries
COPY services/risk_engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service source code
COPY services/risk_engine /app/services/risk_engine
COPY shared /app/shared

WORKDIR /app/services/risk_engine

# Pre-train the model during Docker image build so it's ready on startup
RUN python train_synthetic.py

EXPOSE 8004

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8004"]
