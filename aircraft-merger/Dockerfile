FROM python:3.11-slim

WORKDIR /app
COPY merge.py aircraft_db.py ./

# Same Mictronics DB tar1090/readsb use (enables dbFlags on ADSBHub aircraft).
ARG AIRCRAFT_DB_URL=https://raw.githubusercontent.com/wiedehopf/tar1090-db/csv/aircraft.csv.gz
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends curl ca-certificates \
    && curl -fsSL "${AIRCRAFT_DB_URL}" -o /app/aircraft.csv.gz \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
ENV AIRCRAFT_DB_PATH=/app/aircraft.csv.gz
EXPOSE 8090
CMD ["python3", "merge.py"]
