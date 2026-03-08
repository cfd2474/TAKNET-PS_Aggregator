FROM python:3.11-slim

WORKDIR /app
COPY merge.py .
ENV PYTHONUNBUFFERED=1
EXPOSE 8090
CMD ["python3", "merge.py"]
