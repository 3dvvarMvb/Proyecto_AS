# 💬 Chat Interactivo con Autenticación

## Descripción

Cliente de chat interactivo que requiere autenticación para acceder al servicio de mensajería. Soporta registro de nuevos usuarios y login con credenciales existentes.

## Características

✅ **Autenticación requerida** (Login/Registro)  
✅ **Chat en tiempo real** entre usuarios  
✅ **Historial de conversaciones**  
✅ **Indicador de "escribiendo..."**  
✅ **Confirmaciones de lectura** (✓✓)  
✅ **Detección de usuarios online**  
✅ **Heartbeat automático** para mantener sesión  

## Requisitos Previos

1. **Servicios levantados** (via docker-compose):
   ```bash
   docker-compose up -d
   ```

2. **Servicios requeridos**:
   - BUS (puerto 5000)
   - MongoDB (puerto 27017)
   - Servicio de Autenticación
   - Servicio de Mensajería
   - Servicio de Administración

## Uso

### 1. Iniciar el chat

```bash
cd test
python chat_interactive.py
```

### 2. Autenticación

Al iniciar, verás el menú de autenticación:

```
🔐 AUTENTICACIÓN - Servicio de Mensajería
================================================================================

1. Iniciar sesión
2. Crear nueva cuenta
0. Salir

👉 Selecciona una opción: 
```

#### Opción 1: Iniciar sesión

Para usuarios existentes:
- Ingresa tu nombre de usuario
- Ingresa tu contraseña
- Si las credenciales son correctas, accederás al chat

#### Opción 2: Crear nueva cuenta

Para nuevos usuarios:
- Ingresa un nombre de usuario (debe ser único)
- Ingresa una contraseña
- El sistema creará la cuenta y te logeará automáticamente

### 3. Seleccionar usuario para chatear

Una vez autenticado, verás la lista de usuarios conectados:

```
👤 tu_usuario - Selecciona con quién chatear
================================================================================

📋 Usuarios conectados:

  1. maria123 (ID: 65abc...)
  2. juan456 (ID: 65def...)

  0. Actualizar lista
  Q. Salir
--------------------------------------------------------------------------------

👉 Selecciona un número: 
```

### 4. Chatear

Una vez seleccionado un usuario:
- Escribe tus mensajes y presiona Enter
- Los mensajes recibidos aparecerán automáticamente
- Verás indicador "✍️ escribiendo..." cuando el otro usuario esté escribiendo
- Los mensajes se marcan como leídos automáticamente (✓✓)

**Comandos especiales**:
- `/menu` - Volver al menú de usuarios
- `Ctrl+C` - Salir del chat

## Configuración

### Variables de entorno

```bash
export BUS_HOST=localhost  # Host del BUS
export BUS_PORT=5000       # Puerto del BUS
```

### Conectar desde otro contenedor

Si ejecutas el chat desde dentro de Docker:

```bash
export BUS_HOST=bus
export BUS_PORT=5000
python chat_interactive.py
```

## Flujo de Autenticación

```
┌─────────────────┐
│  Inicio Chat    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Autenticación  │
│  1. Login       │
│  2. Registro    │
└────────┬────────┘
         │
         ▼
    ¿Exitoso?
         │
    ┌────┴────┐
    │         │
   SÍ        NO
    │         │
    ▼         └──► Salir
┌─────────────────┐
│ Conectar al BUS │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Mensajería      │
│ - Ver usuarios  │
│ - Chatear       │
│ - Historial     │
└─────────────────┘
```

## Usuarios de Prueba

Si el servicio de autenticación tiene seed activado:

- **Usuario admin**: 
  - Username: `admin1` (configurado en docker-compose)
  - Password: `secret123` (configurado en docker-compose)

## Troubleshooting

### Error: "No se pudo conectar al servidor"
- Verifica que docker-compose esté corriendo: `docker-compose ps`
- Verifica que el BUS esté accesible: `telnet localhost 5000`

### Error: "Timeout esperando respuesta del servidor"
- Verifica que los servicios estén corriendo correctamente
- Revisa los logs: `docker-compose logs -f servicio-autenticacion`

### Error: "username ya existe"
- Intenta con otro nombre de usuario
- O usa la opción 1 (Iniciar sesión) si ya tienes cuenta

### No veo otros usuarios conectados
- Asegúrate de que otros usuarios hayan iniciado sesión
- Presiona `0` para actualizar la lista
- Espera unos segundos para que los broadcasts se propaguen

## Arquitectura

```
┌──────────────┐     ┌─────────┐     ┌──────────────────┐
│   Cliente    │────▶│   BUS   │────▶│  Autenticación   │
│   (Chat)     │◀────│ (NDJSON)│◀────│    (MongoDB)     │
└──────────────┘     └────┬────┘     └──────────────────┘
                          │
                          ▼
                     ┌──────────────────┐
                     │   Mensajería     │
                     │   (MongoDB)      │
                     └──────────────────┘
                          │
                          ▼
                     ┌──────────────────┐
                     │  Administración  │
                     │   (MongoDB)      │
                     └──────────────────┘
```

## Ejemplo de Sesión

```bash
$ python chat_interactive.py

💬 CHAT INTERACTIVO - Servicio de Mensajería
================================================================================

🔐 AUTENTICACIÓN - Servicio de Mensajería
================================================================================

1. Iniciar sesión
2. Crear nueva cuenta
0. Salir

👉 Selecciona una opción: 2

👤 Nombre de usuario: carlos
🔑 Contraseña: ********

🔄 Creando cuenta...
✅ Usuario creado exitosamente
🔄 Iniciando sesión automática...

✅ Autenticación exitosa
👤 Usuario: carlos
🎫 Session ID: a3f7h9k2l5m8...

✅ Tu User ID: 65f4a3b2c1d0e9f8a7b6c5d4
⏳ Conectando al chat y descubriendo usuarios...
🔌 Conectando al BUS en localhost:5000
✅ Registrado como carlos (65f4a3b2...)
   Client ID: chat_a7f3b2c5
✅ Conectado al servicio de mensajería

👤 carlos - Selecciona con quién chatear
================================================================================

📋 Usuarios conectados:

  1. maria (ID: 65f4a3b2c1...)

  0. Actualizar lista
  Q. Salir
--------------------------------------------------------------------------------

👉 Selecciona un número: 1

📥 Cargando historial de conversación...

💬 Chat con maria
================================================================================

📜 Últimos mensajes:
  maria: Hola! ¿Cómo estás?
  Tú: Todo bien, gracias

--------------------------------------------------------------------------------
Escribe tus mensajes y presiona Enter para enviar
Escribe '/menu' para volver al menú de usuarios
Ctrl+C para salir
--------------------------------------------------------------------------------

> Hola Maria!
✓ Enviado
✍️  maria está escribiendo...
💬 maria: Qué bueno! ¿Hablamos más tarde?
> Claro, cuando quieras
✓ Enviado
✓✓ Mensaje leído
```

## Notas

- Los mensajes se guardan en MongoDB y persisten entre sesiones
- El historial se carga automáticamente al abrir una conversación
- Los mensajes se marcan como leídos automáticamente al verlos
- El sistema mantiene la sesión activa con heartbeats cada 30 segundos
