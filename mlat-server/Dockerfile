FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/wiedehopf/mlat-server.git /opt/mlat-server && \
    cd /opt/mlat-server && \
    python3 setup.py install && \
    rm -rf /opt/mlat-server/.git

EXPOSE 30105 39001

ENTRYPOINT ["mlat-server"]
CMD ["--client-listen=0.0.0.0:30105", "--results=beast,listen,39001"]
