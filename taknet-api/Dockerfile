FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5001
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5001", "--timeout", "30", "--access-logfile", "-", "api:app"]
