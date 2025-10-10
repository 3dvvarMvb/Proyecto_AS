# Dockerfile para MongoDB
FROM mongo:7.0

# Variables de entorno para configuración inicial
ENV MONGO_INITDB_ROOT_USERNAME=admin
ENV MONGO_INITDB_ROOT_PASSWORD=admin123
ENV MONGO_INITDB_DATABASE=arquitectura_software

# Exponer puerto de MongoDB
EXPOSE 27017

# Copiar script de inicialización
COPY ./mongo-init.js /docker-entrypoint-initdb.d/

# Comando por defecto
CMD ["mongod"]