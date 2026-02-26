FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gzip \
    && rm -rf /var/lib/apt/lists/*

# Download free GeoIP database (db-ip.com, mmdb format, no registration)
RUN MONTH=$(date -u +%Y-%m) && \
    curl -sSL "https://download.db-ip.com/free/dbip-city-lite-${MONTH}.mmdb.gz" -o /tmp/geoip.mmdb.gz && \
    gunzip /tmp/geoip.mmdb.gz && \
    mv /tmp/geoip.mmdb /app/GeoLite2-City.mmdb && \
    echo "GeoIP database downloaded: dbip-city-lite-${MONTH}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY schema.sql .
COPY db.py .
COPY vpn_resolver.py .
COPY geoip_helper.py .
COPY proxy.py .

EXPOSE 30004

CMD ["python", "-u", "proxy.py"]
