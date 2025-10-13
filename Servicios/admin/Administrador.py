# Servicios/admin/Administrador.py
import os
import socket
import json
import threading
import logging
import time
from typing import Dict, Any, Optional
from collections import deque
from pymongo import MongoClient

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [ADMINISTRACIÓN] - %(levelname)s - %(message)s'
)

def _jsonline(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

class AdministracionService:
    """
    Servicio de Administración conectado al BUS (NDJSON).
    - Verifica admins (Mongo si está; si no, delega a Autenticación)
    - Delegaciones administrativas a Autenticación
    - Estado del sistema
    """

    def __init__(
        self,
        client_id: str = "administracion_service",
        bus_host: str = os.getenv("BUS_HOST", "bus"),
        bus_port: int = int(os.getenv("BUS_PORT", "5000")),
        mongo_uri: Optional[str] = os.getenv(
            "MONGO_URI",
            "mongodb://app_user:app_password_123@mongodb:27017/arquitectura_software?authSource=admin"
        ),
        mongo_db: str = os.getenv("MONGO_DB", "arquitectura_software"),
        run_tests: bool = False,
    ):
        self.client_id = client_id
        self.bus_host = bus_host
        self.bus_port = int(bus_port)
        self.socket: Optional[socket.socket] = None
        self.connected = False
        self.running = False

        # Mongo
        self.mongo_uri = mongo_uri
        self.mongo_db = mongo_db
        self._db = None
        self.users = None

        # Cola de respuestas desde Autenticación
        self._auth_cv = threading.Condition()
        self._auth_queue: deque[Dict[str, Any]] = deque()

        self.run_tests = run_tests

    # --------------- Mongo (opcional) ---------------
    def _init_mongo(self):
        try:
            if not self.mongo_uri or self._db is not None:
                return
            cli = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=4000)
            cli.admin.command("ping")
            self._db = cli[self.mongo_db]
            self.users = self._db["users"]
            logging.info("✅ Mongo listo en Administración")
        except Exception as e:
            logging.warning(f"⚠️ Administración sin Mongo (delegará en Autenticación): {e}")
            self._db = None
            self.users = None

    # --------------- BUS ---------------
    def connect(self) -> bool:
        try:
            self._init_mongo()

            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.bus_host, self.bus_port))
            logging.info(f"🔌 Conectando al BUS en {self.bus_host}:{self.bus_port}")

            register_message = {
                'type': 'REGISTER',
                'client_id': self.client_id,
                'kind': 'service',
                'service': 'Administracion'
            }
            # NDJSON
            self.socket.sendall(_jsonline(register_message))

            # Intento corto de esperar REGISTER_ACK (NDJSON)
            self.socket.settimeout(1.5)
            ack_ok = False
            try:
                buf = ""
                while "\n" not in buf:
                    chunk = self.socket.recv(4096).decode('utf-8')
                    if not chunk:
                        break
                    buf += chunk
                if "\n" in buf:
                    line, rest = buf.split("\n", 1)
                    if line.strip():
                        ack = json.loads(line)
                        if ack.get('type') == 'REGISTER_ACK' and ack.get('status') == 'success':
                            ack_ok = True
                            logging.info("✅ Registrado en el BUS como servicio 'Administracion'")
            except socket.timeout:
                logging.warning("⚠️ REGISTER_ACK no llegó a tiempo (continuamos de todos modos)")
            finally:
                self.socket.settimeout(None)

            self.connected = True
            self.running = True
            threading.Thread(target=self._listen_messages, name="admin_listener", daemon=True).start()

            if not ack_ok:
                logging.info("ℹ️ Esperando mensajes del BUS (el ACK puede llegar vía listener)…")
            logging.info("🚀 Servicio de Administración iniciado")
            return True

        except Exception as e:
            logging.error(f"❌ Error conectando al BUS: {e}")
            return False

    # --------------- envío / respuesta ---------------
    def _send(self, obj: dict) -> bool:
        if not self.connected or not self.socket:
            logging.error("❌ No conectado al BUS. No se puede enviar mensaje.")
            return False
        try:
            obj.setdefault("sender", self.client_id)
            self.socket.sendall(_jsonline(obj))  # NDJSON
            return True
        except Exception as e:
            logging.error(f"❌ Error enviando mensaje: {e}")
            self.connected = False
            return False

    def _reply(self, reply_to: str, payload: dict, corr: str | None = None):
        """RESPUESTA estándar para solicitudes vía BUS."""
        out = {'type': 'DIRECT', 'target': reply_to, 'payload': payload}
        if corr:
            out['header'] = {'correlationId': corr}
        self._send(out)

    # --------------- listener NDJSON ---------------
    def _listen_messages(self):
        logging.info("👂 Iniciando escucha de mensajes del BUS...")
        buf = ""
        while self.running and self.connected and self.socket:
            try:
                data = self.socket.recv(65536)
                if not data:
                    logging.warning("⚠️ Conexión cerrada por el BUS")
                    self.connected = False
                    break
                buf += data.decode('utf-8')
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        self._handle_message(msg)
                    except json.JSONDecodeError as e:
                        logging.error(f"Error decodificando JSON NDJSON: {e}")
            except Exception as e:
                if self.running:
                    logging.error(f"Error recibiendo mensaje: {e}")
                    self.connected = False
                break
        logging.info("🔇 Listener detenido")

    # --------------- Manejo de mensajes ---------------
    def _handle_message(self, message: dict):
        mtype = message.get('type')
        sender = message.get('sender', 'UNKNOWN')
        logging.info(f"📩 Mensaje recibido - Tipo: {mtype}, De: {sender}")
        logging.debug(f"Contenido: {message}")

        if mtype == 'INVOKE':
            self._handle_invoke(message)
            return

        if mtype == 'REQUEST':
            header = message.get('header') or {}
            payload = (message.get('payload') or {}).copy()
            corr = header.get('correlationId')

            # ⭐ Combinar action del header con el payload
            action = header.get('action') or payload.get('action')
            if action:
                payload['action'] = action

            try:
                res = self._execute_action(payload)
                payload_out = {"ok": True, "response_to": payload, "data": res}
            except Exception as e:
                logging.exception("Error procesando REQUEST en Administración")
                payload_out = {"ok": False, "response_to": payload, "error": str(e)}

            self._reply(sender, payload_out, corr)
            return


        if mtype == 'RESPONSE':
            payload = message.get('payload', {}) or {}
            origin = sender
            if origin == 'autenticacion_service':
                with self._auth_cv:
                    self._auth_queue.append(payload)
                    self._auth_cv.notify()
            else:
                logging.info(f"📨 RESPONSE de {origin}: {payload}")
            return

        if mtype == 'DIRECT':
            payload = message.get('payload', {}) or {}
            origin = sender
            if origin == 'autenticacion_service' and isinstance(payload, dict) and 'response_to' in payload:
                with self._auth_cv:
                    self._auth_queue.append(payload)
                    self._auth_cv.notify()
            else:
                logging.info(f"📧 DIRECT de {origin}: {payload}")
            return

        if mtype == 'BROADCAST':
            payload = message.get('payload', {}) or {}
            logging.info(f"📣 BROADCAST de {sender}: {payload}")
            return

        if mtype == 'DELIVERY_ACK':
            logging.info(f"✅ Mensaje entregado a {message.get('target')}")
            return

        if mtype == 'ERROR':
            logging.error(f"❌ Error del BUS: {message.get('message')}")
            return

        logging.warning(f"Tipo no soportado: {mtype}")

    # --------------- INVOKE ---------------
    def _handle_invoke(self, msg: dict):
        header = msg.get('header', {}) or {}
        body = msg.get('payload', {}) or msg.get('body', {}) or {}
        action = header.get('action') or body.get('action')
        reply_to = header.get('replyTo') or msg.get('sender')
        corr = header.get('correlationId')

        try:
            res = self._execute_action({"action": action, **body})
            payload = {"ok": True, "response_to": {"action": action, **body}, "data": res}
        except Exception as e:
            logging.exception("Error procesando INVOKE")
            payload = {"ok": False, "response_to": {"action": action, **body}, "error": str(e)}

        self._reply(reply_to, payload, corr)

    # --------------- Lógica de dominio ---------------
    def _execute_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        action = payload.get("action")
        if action == "verify_admin":
            return self._verify_admin(payload)
        if action == "get_user_info":
            return self._delegate_to_auth("get_user", {"username": payload.get("username")})
        if action == "admin_create_user":
            return self._delegate_to_auth("create_user", payload)
        if action == "admin_update_user":
            return self._delegate_to_auth("update_user", payload)
        if action == "admin_delete_user":
            return self._delegate_to_auth("delete_user", payload)
        if action in ("admin_change_password", "change_password"):
            return self._delegate_to_auth("changePassword", payload)
        if action == "system_status":
            return {"status": "ok", "time": time.time()}
        return {"status": "error", "message": f"acción desconocida: {action}"}


    def _verify_admin(self, p: Dict[str, Any]) -> Dict[str, Any]:
        username = p.get("username") or p.get("admin_id")
        if not username:
            return {"status": "error", "message": "falta username/admin_id"}

        # 1) Mongo directo si está disponible
        if self.users is not None:
            u = self.users.find_one({"username": username})
            if u is not None:
                return {"status": "ok", "is_admin": u.get("role") == "admin", "active": u.get("active", True)}
            logging.info("🔎 Usuario no hallado en Mongo local; probando fallback a Autenticación…")

        # 2) Fallback a Autenticación (timeout un poco mayor)
        resp = self._delegate_to_auth("get_user", {"username": username}, timeout=6.0)
        if not resp.get("ok"):
            return {"status": "error", "message": "no se pudo verificar"}
        user = resp.get("data", {}).get("user")
        if not user:
            return {"status": "not_found"}
        return {"status": "ok", "is_admin": user.get("role") == "admin", "active": user.get("active", True)}

    # --------------- Delegación a Autenticación ---------------
    def _delegate_to_auth(self, action: str, payload: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
        """
        IMPORTANTE: quitar/forzar 'action' para que NO pise la acción destino.
        Además, mantenemos matching por 'action' y (si viene) 'username'.
        """
        # Clon + sanear
        req_payload = dict(payload or {})
        req_payload.pop("action", None)           # ← evita que pise
        req_payload["action"] = action            # ← acción destino FINAL

        ok = self._send({'type': 'REQUEST', 'target': 'autenticacion_service', 'payload': req_payload})
        if not ok:
            return {"ok": False, "message": "fallo envío a autenticación"}

        deadline = time.time() + timeout
        username = req_payload.get("username")

        while time.time() < deadline:
            with self._auth_cv:
                if not self._auth_queue:
                    remaining = max(0.0, deadline - time.time())
                    self._auth_cv.wait(timeout=remaining)
                    if not self._auth_queue:
                        continue
                resp = self._auth_queue.popleft()

            rto = resp.get("response_to") or {}
            if rto.get("action") == action:
                if username is None or rto.get("username") == username:
                    return resp

            # No coincide, lo reencolamos y seguimos esperando
            with self._auth_cv:
                self._auth_queue.append(resp)
            time.sleep(0.02)

        return {"ok": False, "message": "timeout esperando autenticación"}

    # --------------- Utilidades & ciclo de vida ---------------
    def test_communication(self):
        self._send({'type': 'BROADCAST', 'payload': {
            'test': 'Administración activa', 'message': 'Supervisando', 'timestamp': time.time()
        }})

    def disconnect(self):
        logging.info("🔌 Desconectando del BUS...")
        self.running = False
        self.connected = False
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
        logging.info("👋 Desconectado del BUS")


def main():
    bus_host = os.getenv("BUS_HOST", "bus")
    bus_port = int(os.getenv("BUS_PORT", "5000"))
    mongo_uri = os.getenv("MONGO_URI", "mongodb://app_user:app_password_123@mongodb:27017/arquitectura_software?authSource=admin")
    mongo_db = os.getenv("MONGO_DB", "arquitectura_software")
    run_tests = os.getenv("RUN_TESTS", "0") == "1"

    svc = AdministracionService(
        client_id="administracion_service",
        bus_host=bus_host,
        bus_port=bus_port,
        mongo_uri=mongo_uri,
        mongo_db=mongo_db,
        run_tests=run_tests
    )

    try:
        if svc.connect():
            if run_tests:
                svc.test_communication()
            while svc.connected:
                time.sleep(1)
        else:
            logging.error("❌ No se pudo iniciar el servicio")
    except KeyboardInterrupt:
        logging.info("\n⏹️ Deteniendo servicio...")
    finally:
        svc.disconnect()


if __name__ == "__main__":
    main()
