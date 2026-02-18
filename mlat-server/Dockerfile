FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends git build-essential && \
    pip install --no-cache-dir Cython setuptools && \
    rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/wiedehopf/mlat-server.git /opt/mlat-server && \
    cd /opt/mlat-server && \
    pip install --no-cache-dir . && \
    rm -rf /opt/mlat-server/.git

EXPOSE 30105 39001

ENTRYPOINT ["python3", "-m", "mlat.server"]
CMD ["--client-listen=0.0.0.0:30105", "--results=beast,listen,39001"]
