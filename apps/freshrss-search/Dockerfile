FROM python:3.10-slim

WORKDIR /app/code

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "sync_daemon.py"]

