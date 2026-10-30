FROM python:3.10-slim

RUN apt-get update && apt-get install -y \
    poppler-utils \
    libreoffice-impress \
    libreoffice-java-common \
    fonts-dejavu \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD sh -c "gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 --timeout 120"