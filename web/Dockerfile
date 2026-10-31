FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# One worker keeps APScheduler/KLV singletons; gthread lets long SSE streams (update log)
# run without blocking other requests or tripping the worker timeout.
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "-k", "gthread", "--threads", "8", "--timeout", "120", "--keep-alive", "75", "app:app"]
