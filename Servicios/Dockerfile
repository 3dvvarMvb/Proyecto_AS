# Dockerfile para los Servicios

FROM python:3.11-slim

# Establecer directorio de trabajo
WORKDIR /app

# Copiar requirements
COPY requirements.txt .

# Instalar dependencias
RUN pip install --no-cache-dir -r requirements.txt

# Copiar todos los servicios
COPY Llamadas.py .
COPY Autenticacion.py .
COPY Mensajeria.py .
COPY Administrador.py .
COPY Registro_llamadas.py .

# Variables de entorno
ENV PYTHONUNBUFFERED=1
ENV BUS_HOST=bus
ENV BUS_PORT=5000

# El comando se especificará en docker-compose para cada servicio