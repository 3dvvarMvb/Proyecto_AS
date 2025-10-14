# calls/Registro_llamadas.py
import socket, json, threading, logging, time, os, hashlib, re
from datetime import datetime
from typing import Any, Dict, Optional
from pymongo import MongoClient, ASCENDING
from bson.objectid import ObjectId

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [REGISTRO] - %(levelname)s - %(message)s")

def _jsonline(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

# ---------- helpers ----------
def to_object_id_any(v: Any) -> Optional[ObjectId]:
    """Devuelve ObjectId desde:
    - ObjectId directo
    - string con 24 hex -> ObjectId(...)
    - cualquier otro valor -> ObjectId(sha1(...)[0:24])  (determinístico)
    """
    if v is None:
        return None
    if isinstance(v, ObjectId):
        return v
    try:
        return ObjectId(v)
    except Exception:
        h = hashlib.sha1(str(v).encode("utf-8")).hexdigest()[:24]
        return ObjectId(h)

def map_status(v: str, duration: Any = None) -> str:
    """Normaliza status al enum {recibida, rechazada, sin_respuesta}."""
    s = (v or "").strip().lower()
    if s in ("recibida", "rechazada", "sin_respuesta"):
        return s
    # Heurística:
    # - attempted / no_answer -> sin_respuesta
    # - rejected / cancel -> rechazada
    # - si duration > 0 -> recibida
    try:
        d = int(duration) if duration is not None else 0
    except Exception:
        d = 0
    if d > 0:
        return "recibida"
    if s in ("attempted", "no_answer", "missed", "timeout"):
        return "sin_respuesta"
    if s in ("rejected", "cancelled", "canceled", "busy"):
        return "rechazada"
    return "sin_respuesta"

class RegistroLlamadasService:
    def __init__(
        self,
        client_id: str = os.getenv("CLIENT_ID", "registro_llamadas_service"),
        bus_host: str = os.getenv("BUS_HOST", "bus"),
        bus_port: int = int(os.getenv("BUS_PORT", "5000")),
        mongo_uri: str = os.getenv("MONGO_URI", "mongodb://app_user:app_password_123@mongodb:27017/?authSource=admin"),
        mongo_db: str = os.getenv("MONGO_DB", "llamadas_db"),
        mongo_coll: str = os.getenv("MONGO_COLL", "llamadas"),
    ):
        self.client_id = client_id
        self.bus_host = bus_host
        self.bus_port = bus_port
        self.socket: Optional[socket.socket] = None
        self.connected = False
        self.running = False

        self.mongo_uri = mongo_uri
        self.mongo_db = mongo_db
        self.mongo_coll = mongo_coll
        self.db = None
        self.col = None

    # ---------- Mongo ----------
    def init_db(self) -> bool:
        try:
            cli = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=6000)
            cli.admin.command("ping")
            self.db = cli[self.mongo_db]
            self.col = self.db[self.mongo_coll]
            self.col.create_index([("fecha", ASCENDING), ("hora", ASCENDING)])
            logging.info("✅ Mongo listo (Registro de llamadas)")
            return True
        except Exception as e:
            logging.error(f"❌ Error Mongo: {e}")
            return False

    # ---------- BUS ----------
    def connect_bus(self) -> bool:
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            logging.info(f"🔌 Conectando al BUS en {self.bus_host}:{self.bus_port}")
            self.socket.connect((self.bus_host, self.bus_port))
            reg = {"type": "REGISTER", "client_id": self.client_id, "kind": "service", "service": "RegistroLlamadas"}
            self.socket.sendall(_jsonline(reg))

            self.socket.settimeout(2.0)
            buf = ""
            try:
                while "\n" not in buf:
                    chunk = self.socket.recv(4096).decode("utf-8")
                    if not chunk: break
                    buf += chunk
            finally:
                self.socket.settimeout(None)

            if "\n" in buf:
                line, _ = buf.split("\n", 1)
                line = line.strip()
                if line:
                    ack = json.loads(line)
                    if ack.get("type") == "REGISTER_ACK" and ack.get("status") == "success":
                        self.connected = True
                        self.running = True
                        threading.Thread(target=self._listen, daemon=True).start()
                        logging.info("✅ Registrado exitosamente en el BUS")
                        logging.info("🚀 Servicio de Registro de Llamadas")
                        return True
            logging.error(f"❌ Error registro BUS: {buf!r}")
            return False
        except Exception as e:
            logging.error(f"❌ Error conectando BUS: {e}")
            return False

    def _listen(self):
        logging.info("👂 Iniciando escucha de mensajes del BUS...")
        buf = ""
        while self.running and self.connected:
            try:
                data = self.socket.recv(65536)
                if not data:
                    logging.warning("⚠️ BUS cerró conexión")
                    self.connected = False
                    break
                buf += data.decode("utf-8")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        m = json.loads(line)
                        self._handle(m)
                    except json.JSONDecodeError as e:
                        logging.error(f"JSON NDJSON error: {e}")
            except Exception as e:
                if self.running:
                    logging.error(f"Error recibiendo: {e}")
                    self.connected = False
                break
        logging.info("🔇 Listener detenido")

    def _send(self, d: Dict[str, Any]) -> bool:
        if not self.connected:
            logging.error("❌ No conectado al BUS")
            return False
        try:
            d.setdefault("sender", self.client_id)
            self.socket.sendall(_jsonline(d))
            logging.info(f"📤 Mensaje enviado - Tipo: {d.get('type')}")
            return True
        except Exception as e:
            logging.error(f"❌ Error enviando: {e}")
            self.connected = False
            return False

    def _respond_direct(self, target: str, payload: Dict[str, Any], corr: Optional[str]):
        out = {"type": "DIRECT", "target": target, "payload": payload, "service": "RegistroLlamadas"}
        if corr:
            out["header"] = {"correlationId": corr}
        self._send(out)

    # ---------- Handler ----------
    def _handle(self, m: Dict[str, Any]):
        mtype = m.get("type")
        sender = m.get("sender", "UNKNOWN")
        logging.info(f"📩 Mensaje recibido - Tipo: {mtype}, De: {sender}")

        if mtype == "REQUEST":
            payload = m.get("payload", {}) or {}
            header = m.get("header") or {}
            corr = header.get("correlationId")
            action = (header.get("action") or payload.get("action") or "").strip()

            try:
                if action == "save":
                    resp = self._save_call(payload)
                elif action == "list":
                    resp = self._list_calls(payload)
                else:
                    resp = {"ok": False, "error": f"Acción no soportada: {action}"}
            except Exception as e:
                logging.exception("Fallo manejando REQUEST")
                resp = {"ok": False, "error": str(e)}

            envelope = {"ok": True, "data": resp} if resp.get("ok", False) else resp
            self._respond_direct(sender, envelope, corr)
            return

        if mtype == "DELIVERY_ACK":
            logging.info(f"✅ Entregado a {m.get('target')}")
            return

        if mtype in ("DIRECT", "EVENT", "BROADCAST"):
            logging.info(f"(info) {mtype} => {m.get('payload')}")
            return

        logging.debug(f"(ignorado) {m}")

    # ---------- Acciones ----------
    def _save_call(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normaliza al schema:
        required: fecha(date), hora(string), caller(string), depto(string), status(enum)
        opcionales: destination, duration, callerId(ObjectId)
        """
        # 1) Campos base desde el payload
        destination = (p.get("destination") or "").strip()
        duration = p.get("duration")
        caller_id_raw = p.get("callerId")  # puede venir string
        # Para cumplir el schema: caller debe ser STRING
        # - Si viene callerId, lo usamos como texto para 'caller'.
        # - Si no viene, usamos 'android-device' o lo que venga en callerId/caller.
        caller_str = str(caller_id_raw or p.get("caller") or "android-device")

        # (Opcional) también calculamos un ObjectId determinístico para guardarlo en callerId
        # sin romper el schema (callerId no es requerido).
        try:
            caller_oid = ObjectId(caller_id_raw) if caller_id_raw else None
        except Exception:
            import hashlib
            caller_oid = ObjectId(hashlib.sha1(caller_str.encode("utf-8")).hexdigest()[:24])

        # 2) depto: si viene, úsalo; si no, intenta derivarlo; si no, "N/A"
        import re
        depto = (p.get("depto") or "").strip()
        if not depto:
            if destination and re.fullmatch(r"[0-9A-Za-z\-]+", destination):
                depto = destination
            else:
                depto = "N/A"

        # 3) status normalizado (schema acepta: recibida | rechazada | sin_respuesta)
        status = map_status(p.get("status"), duration)

        # 4) fecha/hora (UTC)
        now = datetime.utcnow()
        fecha = now              # Date nativo
        hora = now.strftime("%H:%M:%S")

        # 5) Documento final que cumple el $jsonSchema
        doc = {
            "fecha": fecha,          # date
            "hora": hora,            # string
            "caller": caller_str,    # <- string requerido por el schema
            "depto": depto,          # string
            "status": status,        # enum válido
            # extras (opcionales)
            "destination": destination,
            "duration": duration,
        }
        # Guardamos callerId solo si lo calculamos (no es requerido)
        if caller_oid is not None:
            doc["callerId"] = caller_oid

        res = self.col.insert_one(doc)
        return {"ok": True, "id": str(res.inserted_id)}


    def _list_calls(self, p: Dict[str, Any]) -> Dict[str, Any]:
        limit = max(1, min(int(p.get("limit", 50)), 200))
        cur = self.col.find({}).sort([("fecha", -1), ("hora", -1)]).limit(limit)
        items = []
        for d in cur:
            d["id"] = str(d.pop("_id"))
            # serialización simple de fecha
            if isinstance(d.get("fecha"), datetime):
                d["fecha"] = d["fecha"].isoformat()
            items.append(d)
        return {"ok": True, "items": items, "count": len(items)}

    # ---------- ciclo de vida ----------
    def disconnect(self):
        logging.info("🔌 Desconectando…")
        self.running = False
        self.connected = False
        if self.socket:
            try: self.socket.close()
            except: pass
        logging.info("👋 Registro detenido")

def main():
    svc = RegistroLlamadasService()
    try:
        if not svc.init_db(): return
        if not svc.connect_bus(): return
        while svc.connected:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("\n⏹️ Deteniendo servicio…")
    finally:
        svc.disconnect()

if __name__ == "__main__":
    main()
