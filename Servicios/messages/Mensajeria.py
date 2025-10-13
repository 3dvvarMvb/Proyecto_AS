# messages/Mensajeria.py
import socket, json, threading, logging, time, os, hashlib
from datetime import datetime
from typing import Optional, Any, Dict
from pymongo import MongoClient, ASCENDING
from bson.objectid import ObjectId

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [MENSAJERÍA] - %(levelname)s - %(message)s')

def send_jsonline(sock: socket.socket, obj: dict):
    sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

def to_object_id_any(v: Any) -> Optional[ObjectId]:
    if v is None:
        return None
    if isinstance(v, ObjectId):
        return v
    try:
        return ObjectId(v)
    except Exception:
        h = hashlib.sha1(str(v).encode("utf-8")).hexdigest()[:24]
        return ObjectId(h)

class MensajeriaService:
    # ⬇️ Quitar el parámetro socket (lo creamos en connect)
    def __init__(self, client_id="mensajeria_service", bus_host=None, bus_port=None):
        self.client_id = client_id
        self.bus_host = bus_host or os.getenv("BUS_HOST","bus")
        self.bus_port = int(bus_port or os.getenv("BUS_PORT","5000"))
        self.socket: Optional[socket.socket] = None
        self.connected = False
        self.running = False

        self.mongo_uri = os.getenv(
            "MONGO_URI",
            "mongodb://app_user:app_password_123@mongodb:27017/mensajes_db?authSource=admin"
        )
        self.mongo_db  = os.getenv("MONGO_DB","mensajes_db")
        self.db = None
        self.msgs = None

    def _init_mongo(self):
        cli = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=6000)
        cli.admin.command("ping")
        self.db = cli[self.mongo_db]
        self.msgs = self.db["mensajes"]
        self.msgs.create_index([("sender",ASCENDING),("receiver",ASCENDING),("fecha",ASCENDING)])
        logging.info("✅ Mongo listo (Mensajería)")

    def connect(self):
        try:
            self._init_mongo()
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.bus_host, self.bus_port))
            logging.info(f"🔌 Conectando al BUS en {self.bus_host}:{self.bus_port}")

            # OJO: usa el mismo esquema/casing que espera tu BUS
            send_jsonline(self.socket, {
                "type": "REGISTER",           # si tu BUS usa 'register', cámbialo aquí
                "kind": "service",
                "service": "Mensajeria",
                "client_id": self.client_id
            })

            buf = ""
            while "\n" not in buf:
                data = self.socket.recv(1024)
                if not data:
                    raise RuntimeError("BUS cerró antes del ACK")
                buf += data.decode("utf-8")
            ack = json.loads(buf.split("\n", 1)[0])

            if ack.get("type") == "REGISTER_ACK" and ack.get("status") == "success":
                self.connected = True
                self.running = True
                threading.Thread(target=self._listen_messages, daemon=True).start()
                logging.info("✅ Registrado exitosamente en el BUS")
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
                chunk = self.socket.recv(4096).decode("utf-8")
                if not chunk:
                    self.connected = False
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    message = json.loads(line)
                    self._handle_message(message)
            except Exception as e:
                if self.running is not None:
                    logging.error(f"Error recibiendo: {e}")
                    self.connected = False
                break
        logging.info("🔇 Listener detenido")

    def _handle_message(self, message: dict):
        mtype = message.get("type")
        sender = message.get("sender", "UNKNOWN")
        if mtype == "REQUEST":
            payload = message.get("payload", {}) or {}
            corr = (message.get("header") or {}).get("correlationId")
            self._route_request(sender, payload, corr)

    def _reply(self, target: str, payload: dict, corr: Optional[str] = None):
        msg = { "type": "DIRECT", "target": target, "payload": payload }
        if corr:
            msg["header"] = {"correlationId": corr}
        # ⬇️ ESTA LÍNEA DEBE ESTAR DENTRO DEL MÉTODO (indentación correcta)
        send_jsonline(self.socket, msg)

    def _route_request(self, sender: str, p: dict, corr: Optional[str] = None):
        act = p.get("action")
        try:
            if act == "send":
                if self.msgs is None:
                    self._reply(sender, {"ok": False, "error": "Mongo no inicializado"}, corr)
                    return
                now = datetime.utcnow()
                sender_oid   = to_object_id_any(p.get("senderObjId")   or p.get("senderId"))
                receiver_oid = to_object_id_any(p.get("receiverObjId") or p.get("receiverId"))
                doc = {
                    "fecha": now,
                    "hora": now.strftime("%H:%M:%S"),
                    "sender": sender_oid,
                    "receiver": receiver_oid,
                    "mensaje": p.get("message", ""),
                    "deliveryStatus": "enviado",
                }
                mid = self.msgs.insert_one(doc).inserted_id
                self._reply(sender, {"ok": True, "messageId": str(mid), "status": "sent"}, corr)

            elif act == "getConversation":
                if self.msgs is None:
                    self._reply(sender, {"ok": False, "error": "Mongo no inicializado"}, corr)
                    return
                u1_id = to_object_id_any(p.get("user1ObjId") or p.get("user1"))
                u2_id = to_object_id_any(p.get("user2ObjId") or p.get("user2"))
                lim = int(p.get("limit", 50))
                cur = self.msgs.find({
                    "$or": [
                        {"sender": u1_id, "receiver": u2_id},
                        {"sender": u2_id, "receiver": u1_id},
                    ]
                }).sort("fecha", -1).limit(lim)
                items = [{
                    "from": str(d.get("sender")),
                    "to":   str(d.get("receiver")),
                    "text": d.get("mensaje"),
                    "ts":   d["fecha"].isoformat(),
                } for d in cur]
                self._reply(sender, {"ok": True, "messages": items, "hasMore": len(items) == lim}, corr)

            else:
                self._reply(sender, {"ok": True, "response_to": p, "status": "message_sent", "message": "OK"}, corr)

        except Exception as e:
            logging.exception("Error en mensajería")
            self._reply(sender, {"ok": False, "error": str(e)}, corr)

    def disconnect(self):
        self.running = False
        self.connected = False
        if self.socket is not None:
            try:
                self.socket.close()
            except:
                pass

def main():
    svc = MensajeriaService()
    try:
        if svc.connect():
            logging.info("🚀 Servicio de Mensajería iniciado")
            while svc.connected:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        svc.disconnect()

if __name__ == "__main__":
    main()
