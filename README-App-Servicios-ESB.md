
# Citófono — Documentación (App Android + Servicios)

> **Ámbito:** Este README resume **lo que se cambió/creó en esta sesión** y cómo se conectan la app y los servicios por el BUS.  
> **Stack:** Android (Kotlin + Jetpack Compose) · ESB TCP NDJSON · Python services · MongoDB.

---

## 0) Arquitectura rápida

```
[App Android]
    ├── EsbApi.kt  ← capa de casos de uso (Contactos, Llamadas, Auth, Admin)
    ├── EsbClient.kt ← cliente TCP del BUS (NDJSON, request/response)
    ├── SessionManager.kt ← manejo de sesión/token
    ├── AuthActivity.kt ← login
    ├── AdminActivity.kt ← consola admin (Import/Export, Registro Llamadas, Usuarios)
    ├── SearchScreen.kt / FuncionalidadesScreen.kt ← pantallas de búsqueda y menú
    └── EsbDebugActivity.kt ← util de depuración del BUS

[ESB TCP (bus:5000)]
    ├── Servicio Contactos
    ├── Servicio Autenticación / Administración
    └── Servicio RegistroLlamadas   ← **nuevo** (Python)

[MongoDB]
    ├── usuarios_db.usuarios / contactos
    ├── llamadas_db.llamadas        ← **usa RegistroLlamadas**
    └── mensajes_db.mensajes
```

**Protocolo:** mensajes **NDJSON** con `type`, `header.action`, `service`, `payload`.  
La app siempre envía **REQUEST** y recibe **DIRECT** de vuelta.

---

## 1) Archivos Kotlin (qué hace cada uno)

### 1.1. `NetConfig.kt`  *(modificado)*
- **Rol:** valores de red que usa `EsbClient`.
- **Claves típicas:** `BUS_HOST`, `BUS_PORT` (ej. `bus:5000` en docker).
- **Consejo:** mantener un solo origen para host/puerto; no hardcodear en otros archivos.

---

### 1.2. `EsbClient.kt`  *(existente, usado intensivamente)*
- **Rol:** **capa de transporte** y **cliente TCP** del BUS con NDJSON.
- **Funciones clave (usadas por EsbApi):**
  - `connectAndRegister(kind="client")`: abre socket a `BUS_HOST:BUS_PORT`, se registra y deja la conexión viva.
  - `isConnected()`
  - `request(service, action, body, timeoutMs) -> JSONObject`
    - Arma `{type:"REQUEST", service, header:{action}, payload:body}`
    - Escribe una línea JSON y **espera un `DIRECT`** con el mismo `correlationId` (si lo usa).
    - Devuelve el **payload** completo de la respuesta.
- **Errores comunes:**
  - *Timeout*: revisar `timeoutMs` y conectividad Docker/host.
  - *NDJSON mal formado*: asegurarse de no enviar saltos de línea extra.

---

### 1.3. `EsbApi.kt`  *(modificado)*
- **Rol:** **capa de casos de uso** (SDK local de la app). Encapsula llamadas al ESB y devuelve `JSONObject/JSONArray` o modelos.
- **Helpers internos:**
  - `ensureConnected()` ➜ usa `EsbClient.connectAndRegister("client")` si hace falta.
  - `unwrapPayload(resp)` ➜ extrae `{payload:{...}}` o `{data:{...}}`.
- **Módulos expuestos:**
  - **Contactos**
    - `searchContactsByDepto(depto, limit)`
    - `searchContactsFallback(term, limit)`
    - `contactosCreate(...)`, `contactosUpdate(...)`
    - `contactosImport(fileContentBase64, format)`
    - `contactosExportXlsx()`
  - **Registro de Llamadas**  *(nuevo)*
    - `recordCall(destination, status="attempted", durationSec?, callerId="android-device", depto?)`
      - Envía a `service:"RegistroLlamadas", action:"record"`.
    - `callsList(depto?, status?, dateFrom?, dateTo?, limit=100)`
      - Envía a `service:"RegistroLlamadas", action:"list"` y devuelve `JSONArray("items")`.
  - **Autenticación**
    - `authCreateUser(username, password, role="user")`
    - `authLogin(username, password)`
    - `authValidate(sessionId)`
    - `authLogout(sessionIdOrToken)`
  - **Administración**
    - `adminVerify(username)`
    - `adminGetUser(username)`
    - `adminUpdateUser(username, role?, active?, newPassword?)`
    - `adminDeleteUser(username)`
- **Notas de implementación:**
  - Todas las funciones corren en `Dispatchers.IO`.
  - Manejo de fechas flexible (helpers para timestamps).
  - Las funciones devuelven JSON plano y el **mapeo a modelos** se hace en la UI cuando corresponde.

---

### 1.4. `SessionManager.kt`  *(creado/modificado)*
- **Rol:** **persistencia y ciclo de vida de la sesión** (token / session_id).
- **Responsabilidades típicas:**
  - Guardar/leer `session_id` de `SharedPreferences`.
  - `validateSession()` ➜ llama `EsbApi.authValidate(...)` al abrir la app.
  - `logoutAndGoToLogin(activity)` ➜ hace `EsbApi.authLogout(...)`, purga credenciales y navega a `AuthActivity`.
- **Uso en la app:**
  - `AuthActivity` lo usa para **guardar** sesión tras login.
  - `AdminActivity` (TopBar “Salir”) lo usa para **cerrar sesión**.

---

### 1.5. `AuthActivity.kt`  *(modificado)*
- **Rol:** pantalla de **login**.
- **Flujo:**
  1. Usuario ingresa `username/password`.
  2. Llama `EsbApi.authLogin(...)`.
  3. Si **ok**, guarda token con `SessionManager` y redirige a `MainActivity` o directo a `AdminActivity` si tiene rol admin (opcional).
  4. Si **error**, muestra `Toast`.
- **Errores comunes:** credenciales inválidas, BUS caído (mostrar mensaje y permitir reintento).

---

### 1.6. `AdminActivity.kt`  *(creado/modificado — núcleo de esta sesión)*
- **Rol:** **Consola administrativa** con 4 secciones (BottomNavigation):
  1. **Import/Export** de Contactos  
     - Importa **XLSX/CSV** (lectura como Base64, envía a `Contactos.import`).  
     - Exporta **XLSX** (`Contactos.export`) y guarda en **Descargas**.
  2. **Reg. Llamadas**  
     - Filtros rápidos (**HOY / SEMANA / TODO**) y búsqueda por texto.  
     - Lista (`LazyColumn`) con columnas: *Fecha, Hora, Caller, Depto, Dur., Estado*.  
     - Mapea `status` a **color** (verde: recibida, rojo: rechazada, ámbar: sin respuesta).
     - Obtiene datos de `EsbApi.callsList(...)` y los renderiza (modelo `CallLog`).
  3. **Reg. Mensajería** *(placeholder)*.
  4. **Usuarios**  
     - Crear usuario (username, password, rol).
     - Cargar/actualizar (rol, activo, nuevo password).  
     - Desactivar usuario.  
     - Opción **“Ver JSON”** para depurar respuestas crudas.
- **Detalles técnicos relevantes:**
  - Usa `remember`, `LaunchedEffect` para refrescar al cambiar filtros.
  - Guardas archivos con **MediaStore** en Android 10+ y con `Environment.DIRECTORY_DOWNLOADS` en versiones anteriores.
  - **Imports necesarios** en Compose para evitar errores:
    ```kotlin
    import androidx.compose.foundation.background
    import androidx.compose.foundation.lazy.LazyColumn
    import androidx.compose.foundation.lazy.itemsIndexed
    import androidx.compose.foundation.layout.size
    import androidx.compose.foundation.layout.weight
    import androidx.compose.foundation.shape.CircleShape
    ```

---

### 1.7. `MainActivity.kt`  *(sin cambios funcionales grandes)*
- **Rol:** punto de entrada de la app.
- **Patrón habitual:**
  - Al iniciar, consulta a `SessionManager` si hay sesión válida.  
  - Si no hay, navega a `AuthActivity`; si hay, navega a `FuncionalidadesScreen` o `AdminActivity` según el rol.

---

### 1.8. `FuncionalidadesScreen.kt`  *(existente)*
- **Rol:** menú/landing con accesos a funciones comunes (búsqueda, llamadas, etc.).
- **Relación con el ESB:** cuando se selecciona una funcionalidad que golpea servidores, utilizará métodos en `EsbApi`.
- **Nota:** No se modificó durante esta sesión (salvo wiring/navegación si lo hiciste).

---

### 1.9. `SearchScreen.kt`  *(existente)*
- **Rol:** buscador de contactos (por *depto* y *nombre*).
- **Llamadas:** usa `EsbApi.searchContactsByDepto(...)` y `searchContactsFallback(...)` para autocompletar y listas.
- **Resultado:** muestra contactos y permite iniciar interacción (llamar/enviar).

---

### 1.10. `EsbDebugActivity.kt`  *(existente)*
- **Rol:** utilidad para **probar** el BUS sin tocar otras pantallas.
- **Uso típico:**
  - Enviar un `REQUEST` arbitrario (servicio/acción/payload) y ver el `DIRECT` recibido.
  - Útil para depurar cuando un servicio está caído o responde con error.

---

### 1.11. `MyDeviceAdminReceiver.kt`  *(existente)*
- **Rol:** receptor para APIs de **Device Admin** (si la app las usa: bloqueo, políticas, etc.).
- **En esta sesión:** no se cambió; no interviene en el flujo de ESB.

---

## 2) Flujo de conexión con servicios (ESB)

### 2.1. Mensaje de ejemplo (grabar llamada)
**App ➜ ESB**
```json
{ "type": "REQUEST", "service": "RegistroLlamadas",
  "header": { "action": "record" },
  "payload": { "destination":"803A","status":"attempted","duration":5,"callerId":"8b4240bc240635f2ba84d20f","depto":"803A" }
}
```
**Servicio ➜ App (DIRECT)**
```json
{ "ok": true, "id": "66fd..." }
```

### 2.2. Diagrama de secuencia (texto)

```
AuthActivity       EsbApi           EsbClient          ESB            Servicio Auth
    | login()        |                 |                 |                  |
    |--------------->| ensureConnected |                 |                  |
    |                |---------------> | REGISTER        |                  |
    |                |<--------------- | ACK             |                  |
    |                | request(login)  |---------------->| REQUEST(login)   |
    |                |                 |                 |--> valida ------>|
    |                |                 |                 |<-- DIRECT -------|
    |<---------------|<----------------| DIRECT          |                  |
    | guarda token   |                 |                 |                  |
```

---

## 3) Servicio **RegistroLlamadas** (resumen para app)

- **Nombre en ESB:** `RegistroLlamadas`
- **Acciones:**
  - `record`/`save`: inserta documento en `llamadas_db.llamadas`.
  - `list`: lista últimas llamadas (`limit` 1..200). Devuelve `items[]` con `_id ➜ id (string)` y `fecha` en ISO.
  - `history`: filtro por `userId` (coincide con `caller` o `callerId`) y/o `depto`.
- **Normalización de estado**: duración > 0 ⇒ `recibida`; `rejected/busy/...` ⇒ `rechazada`; `attempted/no_answer/...` ⇒ `sin_respuesta`.
- **Errores ya resueltos:**
  - `ObjectId is not JSON serializable` ⇒ conversión a `string` antes de responder.
  - `_handle` no encontrado ⇒ método agregado y llamado desde `_listen()`.

---

## 4) Modelos y utilidades de UI

### 4.1. `CallLog` (UI)
```kotlin
data class CallLog(
  val id: String,
  val tsMillis: Long,
  val caller: String?,
  val depto: String?,
  val durationSec: Int,
  val status: String,
  val destination: String?
)
```
> Se construye desde cada `JSONObject` del `JSONArray("items")` que entrega `EsbApi.callsList(...)`.  
> La fecha/hora (`tsMillis`) se calcula con helpers de parseo/normalización.

### 4.2. Formateos en `AdminActivity`
- `formatDate(ms)`, `formatTime(ms)`, `formatDuration(sec)`.
- `colorForStatus(status)` (verde/rojo/ámbar/gris).

---

## 5) Configuración y despliegue

- **MongoDB** inicializado con `mongo-init.js` (DBs: `usuarios_db`, `llamadas_db`, `mensajes_db`).  
  Usuario de app: `app_user / app_password_123` con `authSource=admin`.
- **Servicio registro**: levantado con `MONGO_URI` y `BUS_HOST=bus`, `BUS_PORT=5000`.
- **Android**: en `AndroidManifest.xml` **remover** `package="..."` (Gradle usa `namespace` del módulo).

---

## 6) Pruebas rápidas (E2E)

1. **Login** en `AuthActivity` ➜ verifica `validate_session`.
2. **Admin → Reg. Llamadas** ➜ debe listar documentos (tabs HOY/SEMANA/TODO).
3. **Admin → Import/Export** ➜ importar XLSX/CSV y exportar XLSX (archivo en Descargas).
4. **Admin → Usuarios** ➜ crear/cargar/actualizar/desactivar y revisar “Ver JSON”.
5. **Registro**: enviar `record` (desde app o EsbDebugActivity) y ver la fila nueva.

---

## 7) Troubleshooting

- **No conecta al BUS:** revisar `BUS_HOST/PORT` en `NetConfig.kt` y logs de `EsbClient`/docker.
- **Lista vacía:** confirmar que el servicio `RegistroLlamadas` imprime `📩 Mensaje recibido` y que `list` responde `ok:true`.
- **Crash por Compose:** verificar imports indicados (LazyColumn, weight, background, CircleShape…).
- **Export falla:** en Android < Q se usa `Environment.DIRECTORY_DOWNLOADS` con WRITE permissions si aplica.

---

## 8) Qué se creó/cambió en esta sesión

- **Creado/ajustado**: `SessionManager.kt`, `AuthActivity.kt` (flujo de sesión completo).
- **Modificado**: `EsbApi.kt` (Contactos, Llamadas, Auth, Admin), `AdminActivity.kt` (4 secciones + UI tabla llamadas).
- **Configuración**: `NetConfig.kt` para BUS.  
- **Servicio nuevo**: `RegistroLlamadas` (Python) con acciones `record/list/history` y normalización de estados.

---

## 9) Referencias rápidas de uso (snippets)

```kotlin
// Login
val auth = EsbApi.authLogin(user, pass) // -> { token | session_id | user }
SessionManager.saveSession(context, auth.optString("session_id"))

// Validar al abrir app
val ok = EsbApi.authValidate(SessionManager.sessionId(context)).optBoolean("ok")

// Listar llamadas
val arr = EsbApi.callsList(limit = 100) // JSONArray "items"

// Grabar llamada (ejemplo)
EsbApi.recordCall(destination="803A", status="attempted", durationSec=5, callerId="8b4240...")

// Importar contactos
EsbApi.contactosImport(fileAsBase64, "xlsx")

// Exportar contactos
val x = EsbApi.contactosExportXlsx() // -> { fileContent(base64), count }
```

---

**Fin.**
