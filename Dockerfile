FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY signal_tracker.py .

# Volume for file-based state fallback
VOLUME ["/data"]

EXPOSE 5000

# Use gunicorn in production; single worker is fine since Flask state is in Redis/file
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--timeout", "30", "signal_tracker:app"]
