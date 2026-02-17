FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY schema.sql .
COPY db.py .
COPY vpn_resolver.py .
COPY geoip_helper.py .
COPY proxy.py .

EXPOSE 30005

CMD ["python", "-u", "proxy.py"]
