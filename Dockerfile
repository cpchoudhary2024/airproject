FROM python:3.11-slim

# System libs for numpy / scipy / matplotlib (Agg backend) / reportlab
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libgomp1 \
        libglib2.0-0 \
        libfreetype6 \
        libpng16-16 \
        libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (cached layer — only re-runs when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ ./app/

# HF Spaces runs as uid 1000 — match it so file permissions are correct
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# /tmp is always world-writable so the app can create its data dirs at runtime
ENV DATA_DIR=/tmp/airproject \
    MPLCONFIGDIR=/tmp/matplotlib \
    MPLBACKEND=Agg

# HF Spaces requires exactly port 7860
EXPOSE 7860

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
