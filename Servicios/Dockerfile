# Servicios/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código (respeta mayúsculas)
COPY esb_lib.py .
COPY admin ./admin
COPY auth ./auth
COPY calls ./calls
COPY messages ./messages
COPY contacts ./contacts

ENV PYTHONUNBUFFERED=1
ENV BUS_HOST=bus
ENV BUS_PORT=5000

# El comando se define en docker-compose por servicio
