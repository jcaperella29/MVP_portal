FROM python:3.11-slim

WORKDIR /app

# Install deps first for better layer caching
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App runtime dirs
RUN mkdir -p /app/uploads /app/instance

# Start script
COPY app/start.sh /usr/local/bin/start.sh
RUN sed -i 's/\r$//' /usr/local/bin/start.sh && chmod 0755 /usr/local/bin/start.sh

# App package
COPY app /app/app

# Entrypoint module
COPY wsgi.py /app/wsgi.py

# SQLite / Flask instance folder
COPY instance /app/instance

EXPOSE 8080

CMD ["gunicorn", "-b", "0.0.0.0:8080", "wsgi:app", "--workers", "2", "--threads", "4"]