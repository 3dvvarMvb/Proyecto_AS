# Dockerfile para el Bus de Mensajes

FROM python:3.11-slim

# Establecer directorio de trabajo
WORKDIR /app

# Copiar requirements
COPY requirements.txt .

# Instalar dependencias
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código del BUS
COPY BUS.py .

# Exponer puerto del BUS
EXPOSE 5000

# Variables de entorno
ENV BUS_HOST=0.0.0.0
ENV BUS_PORT=5000
ENV PYTHONUNBUFFERED=1

# Healthcheck para verificar que el BUS está activo
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import socket; s=socket.socket(); s.connect(('localhost', 5000)); s.close()" || exit 1

# Comando para ejecutar el BUS
CMD ["python", "-u", "BUS.py"]