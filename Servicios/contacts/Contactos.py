# contacts/Contactos.py
import socket
import json
import threading
import logging
import time
import os
import re
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from pymongo import MongoClient, ASCENDING
from pymongo.errors import PyMongoError, DuplicateKeyError
from bson.objectid import ObjectId

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [CONTACTOS] - %(levelname)s - %(message)s'
)

# ----------------- utilidades JSONL (NDJSON) -----------------

def _jsonline(obj: dict) -> bytes:
    """Devuelve b'<json>\\n' para protocolo NDJSON."""
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

def _safe_object_id(s: str) -> Optional[ObjectId]:
    try:
        return ObjectId(s)
    except Exception:
        return None

def _serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convierte un documento Mongo a tipos JSON serializables:
      - _id -> id (str)
      - datetime/date -> ISO string
    """
    if not doc:
        return {}
    out: Dict[str, Any] = {}
    for k, v in doc.items():
        if k == "_id":
            out["id"] = str(v)
            continue
        if isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        elif isinstance(v, ObjectId):
            out[k] = str(v)
        else:
            out[k] = v
    return out


class ContactosService:
    """
    Servicio Contactos alineado al validador de usuarios_db.contactos:
      required: nombre (string), departamento (string), telefono (string)
      email (string, opcional), tags (array<string>, opcional), createdAt (date, opcional)

    Protocolo BUS: NDJSON (una línea por JSON).
    Se registra como servicio lógico: "Contactos".
    """

    def __init__(
        self,
        client_id: str = os.getenv("CLIENT_ID", "contactos_service"),
        bus_host: str = os.getenv("BUS_HOST", "bus"),
        bus_port: int = int(os.getenv("BUS_PORT", "5000")),
        mongo_uri: str = os.getenv("MONGO_URI", "mongodb://app_user:app_password_123@mongodb:27017/?authSource=admin"),
        mongo_db: str = os.getenv("MONGO_DB", "usuarios_db"),
        mongo_coll: str = os.getenv("MONGO_COLL", "contactos")
    ):
        # BUS
        self.client_id = client_id
        self.bus_host = bus_host
        self.bus_port = bus_port
        self.socket: Optional[socket.socket] = None
        self.connected = False
        self.running = False

        # Mongo
        self.mongo_uri = mongo_uri
        self.mongo_db = mongo_db
        self.mongo_coll = mongo_coll
        self.db_client: Optional[MongoClient] = None
        self.col = None

    # ----------------- Mongo -----------------

    def init_db(self) -> bool:
        try:
            self.db_client = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=6000)
            self.db_client.admin.command("ping")
            db = self.db_client[self.mongo_db]
            self.col = db[self.mongo_coll]

            # Índices útiles
            self.col.create_index([("departamento", ASCENDING), ("nombre", ASCENDING)])

            logging.info("✅ MongoDB listo (Contactos)")
            return True
        except PyMongoError as e:
            logging.error(f"❌ Error conectando a MongoDB: {e}")
            return False

    # ----------------- BUS (NDJSON) -----------------

    def connect_bus(self) -> bool:
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            logging.info(f"🔌 Conectando al BUS en {self.bus_host}:{self.bus_port}")
            self.socket.connect((self.bus_host, self.bus_port))

            register_message = {
                "type": "REGISTER",
                "client_id": self.client_id,
                "kind": "service",
                "service": "Contactos",
            }
            # Registrar con NDJSON
            self.socket.sendall(_jsonline(register_message))

            # Leer REGISTER_ACK (NDJSON)
            self.socket.settimeout(2.0)
            buf = ""
            try:
                while "\n" not in buf:
                    chunk = self.socket.recv(4096).decode("utf-8")
                    if not chunk:
                        break
                    buf += chunk
            finally:
                self.socket.settimeout(None)

            if "\n" in buf:
                line, _rest = buf.split("\n", 1)
                line = line.strip()
                if line:
                    ack = json.loads(line)
                    if ack.get("type") == "REGISTER_ACK" and ack.get("status") == "success":
                        self.connected = True
                        self.running = True
                        threading.Thread(target=self._listen_messages, daemon=True).start()
                        logging.info("✅ Registrado en BUS como servicio 'Contactos'")
                        logging.info("🚀 Servicio de Contactos iniciado correctamente")
                        return True

            logging.error(f"❌ Error en registro BUS: {buf!r}")
            return False

        except Exception as e:
            logging.error(f"❌ Error conectando al BUS: {e}")
            return False

    def _listen_messages(self):
        logging.info("👂 Iniciando escucha de mensajes del BUS...")
        buf = ""
        while self.running and self.connected:
            try:
                data = self.socket.recv(65536)
                if not data:
                    logging.warning("⚠️ Conexión cerrada por el BUS")
                    self.connected = False
                    break

                buf += data.decode("utf-8")
                # Procesar NDJSON (una línea = un JSON)
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        message = json.loads(line)
                        self._handle_message(message)
                    except json.JSONDecodeError as e:
                        logging.error(f"Error decodificando JSON NDJSON: {e}")

            except Exception as e:
                if self.running:
                    logging.error(f"Error recibiendo mensaje: {e}")
                    self.connected = False
                break
        logging.info("🔇 Listener detenido")

    # ----------------- envío -----------------

    def _send(self, d: Dict[str, Any]) -> bool:
        if not self.connected:
            logging.error("❌ No conectado al BUS. No se puede enviar mensaje.")
            return False
        try:
            d.setdefault("sender", self.client_id)
            self.socket.sendall(_jsonline(d))   # NDJSON
            logging.info(f"📤 Mensaje enviado - Tipo: {d.get('type')}")
            return True
        except Exception as e:
            logging.error(f"❌ Error enviando mensaje: {e}")
            self.connected = False
            return False

    def _respond_direct(self, target: str, payload: Dict[str, Any], correlation_id: Optional[str] = None):
        """
        Responde como DIRECT (lo que espera el tester).
        Mantiene correlationId si vino en el REQUEST.
        """
        out: Dict[str, Any] = {
            "type": "DIRECT",
            "target": target,
            "payload": payload,
            "service": "Contactos",
        }
        if correlation_id:
            out["header"] = {"correlationId": correlation_id}
        self._send(out)

    def send_broadcast(self, payload: Dict[str, Any]) -> bool:
        # ⚠️ Solo tipos serializables (no datetime)
        return self._send({"type": "BROADCAST", "payload": payload})

    # ----------------- manejo de mensajes -----------------

    def _handle_message(self, message: Dict[str, Any]):
        mtype = message.get("type")
        sender = message.get("sender", "UNKNOWN")
        logging.info(f"📩 Mensaje recibido - Tipo: {mtype}, De: {sender}")

        if mtype == "REQUEST":
            header = message.get("header") or {}
            action = (header.get("action") or "").strip()   # <-- 📌 AHORA LEEMOS DEL HEADER
            payload = message.get("payload", {}) or {}
            corr = header.get("correlationId")

            try:
                resp_payload = self._handle_request(action, payload)
                # devolvemos directamente el cuerpo que la app espera (contacts/ok/…)
                envelope = resp_payload
            except Exception as e:
                logging.exception("Fallo manejando request (no controlado)")
                envelope = {"ok": False, "error": str(e)}

            self._respond_direct(sender, envelope, correlation_id=corr)
            return

        if mtype == "DIRECT":
            logging.info(f"📧 DIRECT de {sender}: {message.get('payload', {})}")
            return

        if mtype == "BROADCAST":
            logging.info(f"📣 BROADCAST de {sender}: {message.get('payload', {})}")
            return

        if mtype == "DELIVERY_ACK":
            logging.info(f"✅ Mensaje entregado a {message.get('target')}")
            return

        if mtype == "ERROR":
            logging.error(f"❌ Error del BUS: {message.get('message')}")
            return

        # Otros tipos (RESPONSE/EVENT/…) no se usan aquí
        logging.debug(f"(ignorado) {message}")

    # ----------------- CRUD & búsqueda -----------------

    def _require(self, obj: Dict[str, Any], keys: List[str]):
        for k in keys:
            if obj.get(k) in (None, ""):
                raise ValueError(f"Falta parámetro requerido: {k}")

    def _handle_request(self, action: str, p: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if action == "create_contact":
                return self._create_contact(p)
            if action == "get_contact":
                return self._get_contact(p)
            if action == "list_contacts":
                return self._list_contacts(p)
            if action == "update_contact":
                return self._update_contact(p)
            if action == "delete_contact":
                return self._delete_contact(p)
            if action == "search":
                return self._search(p)

            return {"ok": False, "error": f"Acción no soportada: {action}"}

        except DuplicateKeyError:
            return {"ok": False, "error": "Contacto duplicado"}
        except PyMongoError as e:
            return {"ok": False, "error": f"MongoError: {e}"}
        except Exception as e:
            logging.exception("Fallo manejando request")
            return {"ok": False, "error": str(e)}

    def _create_contact(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """
        Crea un contacto cumpliendo el $jsonSchema de usuarios_db.contactos:
        - Requeridos: nombre, departamento, telefono
        - Opcionales: email (string), tags (array<string>), createdAt (date)
        """
        nombre = p.get("nombre") or p.get("name")
        departamento = p.get("departamento")
        telefono = p.get("telefono") or p.get("phone")
        if not (nombre and departamento and telefono):
            raise ValueError("Se requieren: nombre, departamento, telefono")

        doc: Dict[str, Any] = {
            "nombre": str(nombre),
            "departamento": str(departamento),
            "telefono": str(telefono),
            "createdAt": datetime.utcnow(),  # guardamos como Date nativo
        }
        # Solo incluir email/tags si son válidos
        email = (p.get("email") or "").strip()
        if email:
            doc["email"] = email
        tags = p.get("tags")
        if isinstance(tags, list):
            doc["tags"] = [str(t) for t in tags]

        res = self.col.insert_one(doc)
        out = _serialize({**doc, "_id": res.inserted_id})

        # BROADCAST sin tipos no serializables
        self.send_broadcast({"event": "contacts.created", "contact_id": out["id"]})
        return {"ok": True, "contact": out}

    def _get_contact(self, p: Dict[str, Any]) -> Dict[str, Any]:
        cid = _safe_object_id(p.get("contact_id") or p.get("id") or "")
        if not cid:
            return {"ok": False, "error": "contact_id inválido"}
        doc = self.col.find_one({"_id": cid})
        if not doc:
            return {"ok": False, "error": "No encontrado"}
        return {"ok": True, "contact": _serialize(doc)}

    def _list_contacts(self, p: Dict[str, Any]) -> Dict[str, Any]:
        limit = max(1, min(int(p.get("limit", 50)), 200))
        page = max(1, int(p.get("page", 1)))
        skip = (page - 1) * limit
        cur = (
            self.col.find({})
            .sort([("departamento", ASCENDING), ("nombre", ASCENDING)])
            .skip(skip)
            .limit(limit)
        )
        items = [_serialize(d) for d in cur]
        total = self.col.count_documents({})
        return {"ok": True, "items": items, "page": page, "limit": limit, "total": total}

    def _update_contact(self, p: Dict[str, Any]) -> Dict[str, Any]:
        cid = _safe_object_id(p.get("contact_id") or p.get("id") or "")
        if not cid:
            return {"ok": False, "error": "contact_id inválido"}

        allowed = {"nombre", "departamento", "telefono", "email", "tags"}
        updates_raw = (p.get("updates") or {})
        updates = {k: v for k, v in updates_raw.items() if k in allowed}

        # No setear email=None; si viene vacío, lo removemos
        if "email" in updates and not (updates["email"] or "").strip():
            updates.pop("email")

        if "tags" in updates and not isinstance(updates["tags"], list):
            updates.pop("tags")

        if not updates:
            return {"ok": False, "error": "Nada para actualizar"}

        updates["_updatedAt"] = datetime.utcnow()

        res = self.col.find_one_and_update(
            {"_id": cid},
            {"$set": updates},
            return_document=True
        )
        if not res:
            return {"ok": False, "error": "No encontrado"}

        out = _serialize(res)
        self.send_broadcast({"event": "contacts.updated", "contact_id": out["id"]})
        return {"ok": True, "contact": out}

    def _delete_contact(self, p: Dict[str, Any]) -> Dict[str, Any]:
        cid = _safe_object_id(p.get("contact_id") or p.get("id") or "")
        if not cid:
            return {"ok": False, "error": "contact_id inválido"}

        res = self.col.delete_one({"_id": cid})
        if res.deleted_count == 0:
            return {"ok": False, "error": "No encontrado"}

        self.send_broadcast({"event": "contacts.deleted", "contact_id": str(cid)})
        return {"ok": True}

    def _search(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """
        Búsqueda simple:
          - q: term genérico (nombre/departamento/telefono, regex case-insensitive)
          - nombre, departamento: filtros específicos
          - limit: tope de resultados (1..200)
        Devuelve clave 'contacts' (lo que usa el tester).
        """
        q: Dict[str, Any] = {}
        term = (p.get("q") or "").strip()
        nombre = (p.get("nombre") or "").strip()
        depto = (p.get("departamento") or "").strip()

        ors: List[Dict[str, Any]] = []
        if term:
            rx_any = re.compile(re.escape(term), re.IGNORECASE)
            ors.extend([{"nombre": rx_any}, {"departamento": rx_any}, {"telefono": rx_any}])
        if nombre:
            ors.append({"nombre": re.compile(re.escape(nombre), re.IGNORECASE)})
        if depto:
            # Coincidencia exacta o por prefijo (ej: "402", "402A"…)
            rx_depto = re.compile(rf"^(?:{re.escape(depto)}$|{re.escape(depto)}.*)", re.IGNORECASE)
            ors.append({"departamento": rx_depto})

        if ors:
            q["$or"] = ors

        limit = max(1, min(int(p.get("limit", 50)), 200))
        cur = (
            self.col.find(q)
            .sort([("departamento", ASCENDING), ("nombre", ASCENDING)])
            .limit(limit)
        )
        items = [_serialize(d) for d in cur]
        return {"ok": True, "contacts": items}

    # ----------------- ciclo de vida -----------------

    def disconnect(self):
        logging.info("🔌 Desconectando del BUS/Mongo...")
        self.running = False
        self.connected = False
        if self.socket is not None:
            try:
                self.socket.close()
            except:
                pass
        if self.db_client is not None:
            try:
                self.db_client.close()
            except:
                pass
        logging.info("👋 Contactos detenido")


def main():
    svc = ContactosService()
    try:
        if not svc.init_db():
            logging.error("❌ No se pudo iniciar MongoDB")
            return
        if not svc.connect_bus():
            logging.error("❌ No se pudo registrar en el BUS")
            return

        while svc.connected:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("\n⏹️ Deteniendo servicio...")
    finally:
        svc.disconnect()


if __name__ == "__main__":
    main()
