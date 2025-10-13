# Servicios/auth/Autenticacion.py
import os
import socket
import json
import threading
import logging
import time
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
import bcrypt

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [AUTENTICACIÓN] - %(levelname)s - %(message)s'
)

def now_utc() -> datetime:
    return datetime.utcnow()

def to_ts(dt: datetime) -> float:
    return dt.timestamp()

def send_jsonline(sock: socket.socket, obj: dict):
    sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

class AutenticacionService:
    """
    Servicio de Autenticación (users/sessions en usuarios_db).
    Acciones:
      - authenticate_user / login
      - validate_session
      - logout
      - create_user / update_user / delete_user / get_user
      - changePassword (alias de update_user)
    """

    def __init__(
        self,
        client_id: str = "autenticacion_service",
        bus_host: str = 'bus',
        bus_port: int = 5000,
        mongo_uri: Optional[str] = None,
        mongo_db: str = "usuarios_db",
        session_ttl_minutes: int = 1440,
        run_tests: bool = False,
    ):
        self.client_id = client_id
        self.bus_host = bus_host
        self.bus_port = int(bus_port)
        self.socket: Optional[socket.socket] = None
        self.connected = False
        self.running = False

        # Mongo en el contenedor
        self.mongo_uri = mongo_uri or "mongodb://app_user:app_password_123@mongodb:27017/usuarios_db?authSource=admin"
        self.mongo_db_name = mongo_db
        self.session_ttl = timedelta(minutes=int(session_ttl_minutes))
        self.run_tests = run_tests

        self._mongo = None
        self._db = None
        self.users = None
        self.sessions = None

    # --------------- Mongo ---------------
    def _init_mongo(self):
        if self._mongo is not None:
            return
        self._mongo = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=6000)
        self._mongo.admin.command("ping")
        self._db = self._mongo[self.mongo_db_name]
        self.users = self._db["users"]
        self.sessions = self._db["sessions"]

        self.users.create_index([("username", ASCENDING)], unique=True)
        self.users.create_index([("active", ASCENDING)])
        self.sessions.create_index([("session_id", ASCENDING)], unique=True)
        self.sessions.create_index([("active", ASCENDING)])
        self.sessions.create_index([("expires_at", ASCENDING)])

    # --------------- BUS (NDJSON) ---------------
    def connect(self) -> bool:
        try:
            self._init_mongo()
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.bus_host, self.bus_port))
            logging.info(f"🔌 Conectando al BUS en {self.bus_host}:{self.bus_port}")

            register_message = {'type': 'REGISTER', 'client_id': self.client_id,
                                'kind': 'service', 'service': 'Autenticacion'}
            send_jsonline(self.socket, register_message)

            # Espera ACK (línea)
            buf = ""
            while "\n" not in buf:
                data = self.socket.recv(4096)
                if not data:
                    raise RuntimeError("BUS cerró antes del ACK")
                buf += data.decode("utf-8")
            ack = json.loads(buf.split("\n", 1)[0])

            if ack.get('type') == 'REGISTER_ACK' and ack.get('status') == 'success':
                self.connected = True
                logging.info(f"✅ Registrado exitosamente en el BUS")
                self.running = True
                threading.Thread(target=self._listen_messages, name="auth_listener", daemon=True).start()
                logging.info("🚀 Servicio de Autenticación iniciado")
                return True
            else:
                logging.error(f"❌ Error en registro: {ack}")
                return False

        except Exception as e:
            logging.error(f"❌ Error conectando al BUS: {e}")
            return False

    def _listen_messages(self):
        logging.info("👂 Iniciando escucha de mensajes del BUS...")
        buf = ""
        while self.running and self.connected:
            try:
                chunk = self.socket.recv(65536).decode('utf-8')
                if not chunk:
                    logging.warning("⚠️ Conexión cerrada por el BUS")
                    self.connected = False
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    msg = json.loads(line)
                    self._handle_message(msg)
            except json.JSONDecodeError as e:
                logging.error(f"Error decodificando mensaje: {e}")
            except Exception as e:
                if self.running:
                    logging.error(f"Error recibiendo mensaje: {e}")
                    self.connected = False
                break
        logging.info("🔇 Listener detenido")

    def _handle_message(self, message: dict):
        mtype = message.get('type')
        sender = message.get('sender', 'UNKNOWN')

        if mtype == 'REQUEST':
            payload = message.get('payload', {}) or {}
            header = message.get('header') or {}
            # inyectamos action si vino en el header
            if 'action' not in payload and header.get('action'):
                payload['action'] = header['action']
            corr = header.get('correlationId')
            self._route_request(sender, payload, corr)

        elif mtype == 'DIRECT':
            logging.info(f"📧 DIRECT de {sender}: {message.get('payload', {})}")
        elif mtype == 'BROADCAST':
            logging.info(f"📣 BROADCAST de {sender}: {message.get('payload', {})}")
        elif mtype == 'DELIVERY_ACK':
            logging.info(f"✅ Mensaje entregado a {message.get('target')}")
        elif mtype == 'ERROR':
            logging.error(f"❌ Error del BUS: {message.get('message')}")

    # --------------- Ruteo de acciones ---------------
    def _route_request(self, sender: str, payload: Dict[str, Any], corr: Optional[str] = None):
        action = (payload.get("action") or "").strip()
        try:
            if action in ("authenticate_user", "login"):
                r = self._authenticate_user(payload)
                if action == "login":
                    # contrato clásico: { token, userType, userId }
                    if r.get("status") == "authenticated":
                        res = {"token": r["session_id"],
                               "userType": r.get("user",{}).get("role","user"),
                               "userId": r.get("user",{}).get("username")}
                    else:
                        res = {"status":"denied","message":r.get("message","")}
                else:
                    res = r

            elif action == "validate_session":
                res = self._validate_session(payload)

            elif action == "logout":
                res = self._logout(payload)

            elif action in ("create_user","get_user","update_user","delete_user"):
                res = getattr(self, f"_{action}")(payload)

            elif action == "changePassword":
                res = self._update_user({"username": payload.get("userId") or payload.get("username"),
                                         "password": payload.get("newPassword")})

            else:
                res = {"status": "error", "message": f"acción desconocida: {action}"}

            self._reply(sender, {"ok": True, "response_to": payload, "data": res}, corr)
        except Exception as e:
            logging.exception("Error procesando REQUEST")
            self._reply(sender, {"ok": False, "response_to": payload, "error": str(e)}, corr)

    def _reply(self, target: str, payload: dict, corr: Optional[str] = None):
        msg = {'type': 'DIRECT', 'target': target, 'payload': payload}
        if corr:
            msg['header'] = {'correlationId': corr}
        send_jsonline(self.socket, msg)

    # --------------- Lógica de dominio ---------------
    def _hash_password(self, raw: str) -> bytes:
        salt = bcrypt.gensalt(rounds=12)
        return bcrypt.hashpw(raw.encode("utf-8"), salt)

    def _check_password(self, raw: str, hashed: bytes) -> bool:
        try:
            return bcrypt.checkpw(raw.encode("utf-8"), hashed)
        except Exception:
            return False

    def _authenticate_user(self, p: Dict[str, Any]) -> Dict[str, Any]:
        q = None
        if "username" in p:
            q = {"username": p["username"], "active": True}
        elif "user_id" in p:
            q = {"_id": p["user_id"], "active": True}
        if not q:
            return {"status": "error", "message": "falta username o user_id"}

        user = self.users.find_one(q)
        if not user:
            return {"status": "denied", "message": "usuario no encontrado o inactivo"}

        if "password" in p:
            if not user.get("password_hash"):
                return {"status": "denied", "message": "usuario sin password configurado"}
            if not self._check_password(p["password"], user["password_hash"]):
                return {"status": "denied", "message": "password incorrecto"}

        session_id = secrets.token_urlsafe(24)
        now = now_utc()
        self.sessions.insert_one({
            "session_id": session_id,
            "user_id": str(user["_id"]),
            "username": user["username"],
            "role": user.get("role", "user"),
            "created_at": now,
            "expires_at": now + self.session_ttl,
            "active": True,
        })
        return {
            "status": "authenticated",
            "session_id": session_id,
            "expires_at": to_ts(now + self.session_ttl),
            "user": {"username": user["username"], "role": user.get("role", "user")},
        }

    def _validate_session(self, p: Dict[str, Any]) -> Dict[str, Any]:
        sid = p.get("session_id")
        if not sid:
            return {"status": "error", "message": "falta session_id"}
        s = self.sessions.find_one({"session_id": sid, "active": True})
        if not s:
            return {"status": "invalid"}
        if s["expires_at"] < now_utc():
            self.sessions.update_one({"_id": s["_id"]}, {"$set": {"active": False}})
            return {"status": "expired"}
        return {"status": "valid",
                "user": {"username": s["username"], "role": s.get("role", "user")},
                "expires_at": to_ts(s["expires_at"])}

    def _logout(self, p: Dict[str, Any]) -> Dict[str, Any]:
        sid = p.get("session_id")
        if not sid:
            return {"status": "error", "message": "falta session_id"}
        upd = self.sessions.update_one({"session_id": sid, "active": True}, {"$set": {"active": False}})
        return {"status": "ok", "modified": upd.modified_count}

    def _create_user(self, p: Dict[str, Any]) -> Dict[str, Any]:
        username = p.get("username")
        password = p.get("password")
        role = p.get("role", "user")
        if not username or not password:
            return {"status": "error", "message": "falta username/password"}
        try:
            doc = {
                "username": username,
                "password_hash": self._hash_password(password),
                "role": role,
                "active": True,
                "created_at": now_utc(),
            }
            self.users.insert_one(doc)
            return {"status": "created", "username": username, "role": role}
        except DuplicateKeyError:
            return {"status": "error", "message": "username ya existe"}

    def _get_user(self, p: Dict[str, Any]) -> Dict[str, Any]:
        username = p.get("username")
        if not username:
            return {"status": "error", "message": "falta username"}
        u = self.users.find_one({"username": username})
        if not u:
            return {"status": "not_found"}
        return {"status": "ok",
                "user": {"username": u["username"], "role": u.get("role", "user"),
                         "active": u.get("active", True),
                         "created_at": to_ts(u["created_at"]) if u.get("created_at") else None}}

    def _update_user(self, p: Dict[str, Any]) -> Dict[str, Any]:
        username = p.get("username")
        if not username:
            return {"status": "error", "message": "falta username"}
        updates = {}
        if "password" in p:
            updates["password_hash"] = self._hash_password(p["password"])
        if "role" in p:
            updates["role"] = p["role"]
        if "active" in p:
            updates["active"] = bool(p["active"])
        if not updates:
            return {"status": "noop", "message": "sin cambios"}
        res = self.users.update_one({"username": username}, {"$set": updates})
        return {"status": "ok", "matched": res.matched_count, "modified": res.modified_count}

    def _delete_user(self, p: Dict[str, Any]) -> Dict[str, Any]:
        username = p.get("username")
        if not username:
            return {"status": "error", "message": "falta username"}
        res = self.users.update_one({"username": username}, {"$set": {"active": False}})
        return {"status": "ok", "matched": res.matched_count, "modified": res.modified_count}

    # --------------- Utilidades ---------------
    def test_communication(self):
        send_jsonline(self.socket, {'type': 'BROADCAST', 'payload': {'test': 'Autenticación activa'}})

    def disconnect(self):
        self.running = False
        self.connected = False
        if self.socket is not None:
            try: self.socket.close()
            except: pass

def main():
    bus_host = os.getenv("BUS_HOST", "bus")
    bus_port = int(os.getenv("BUS_PORT", "5000"))
    mongo_uri = os.getenv("MONGO_URI", "mongodb://app_user:app_password_123@mongodb:27017/usuarios_db?authSource=admin")
    mongo_db = os.getenv("MONGO_DB", "usuarios_db")
    session_ttl = int(os.getenv("SESSION_TTL_MINUTES", "1440"))
    run_tests = os.getenv("RUN_TESTS", "0") == "1"

    svc = AutenticacionService(
        client_id="autenticacion_service",
        bus_host=bus_host, bus_port=bus_port,
        mongo_uri=mongo_uri, mongo_db=mongo_db,
        session_ttl_minutes=session_ttl, run_tests=run_tests
    )
    try:
        if svc.connect():
            if run_tests: svc.test_communication()
            while svc.connected: time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        svc.disconnect()

if __name__ == "__main__":
    main()
