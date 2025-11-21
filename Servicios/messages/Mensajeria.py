import socket, json, threading, logging, time, os, hashlib
from datetime import datetime, timezone
from typing import Optional, Any, Dict, Set
from pymongo import MongoClient, ASCENDING
from bson.objectid import ObjectId
from collections import defaultdict
from zoneinfo import ZoneInfo

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
    def __init__(self, client_id="mensajeria_service", bus_host=None, bus_port=None):
        self.client_id = client_id
        self.bus_host = bus_host or os.getenv("BUS_HOST","bus")
        self.bus_port = int(bus_port or os.getenv("BUS_PORT","5000"))
        self.socket: Optional[socket.socket] = None
        self.connected = False
        self.running = False

        # ✅ Gestión de usuarios conectados
        self.online_users: Dict[str, Dict] = {}  # user_id -> {client_id, last_seen, status}
        self.users_lock = threading.Lock()
        
        # ✅ Sistema de rooms/canales
        self.rooms: Dict[str, Set[str]] = defaultdict(set)  # room_id -> set(user_ids)
        self.rooms_lock = threading.Lock()
        
        # ✅ Indicador de "escribiendo"
        self.typing_status: Dict[str, Dict] = {}  # conversation_id -> {user_id: timestamp}
        self.typing_lock = threading.Lock()
        
        # ✅ Heartbeat
        self.heartbeat_interval = 30  # segundos
        self.heartbeat_timeout = 60   # segundos sin heartbeat = offline
        
        # MongoDB
        self.mongo_uri = os.getenv(
            "MONGO_URI",
            "mongodb://app_user:app_password_123@mongodb:27017/mensajes_db?authSource=admin"
        )
        self.mongo_db  = os.getenv("MONGO_DB","mensajes_db")
        self.mongo_coll = os.getenv("MONGO_COLL", "mensajes")
        self.db = None
        self.msgs = None
        try:
            tz_name = os.getenv("LOCAL_TZ", "America/Santiago")
            self.local_tz = ZoneInfo(tz_name)
            logging.info(f"🕒 Zona horaria local Mensajería: {tz_name}")
        except Exception:
            logging.warning("⚠️ ZoneInfo no disponible, usando UTC")
            self.local_tz = timezone.utc

    def _init_mongo(self):
        cli = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=6000)
        cli.admin.command("ping")
        self.db = cli[self.mongo_db]
        self.msgs = self.db[self.mongo_coll]
        self.msgs.create_index([("sender",ASCENDING),("receiver",ASCENDING),("fecha",ASCENDING)])
        logging.info(f"✅ Mongo listo (Mensajería) → DB: {self.mongo_db}, colección: {self.mongo_coll}")

    def _safe_str(self, value: Any) -> Optional[str]:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return None

    def _build_user_info(self, doc: Dict[str, Any], prefix: str) -> Dict[str, Any]:
        oid = doc.get(prefix)
        user_id = self._safe_str(doc.get(f"{prefix}UserId"))
        username = self._safe_str(doc.get(f"{prefix}Username")) or user_id
        info = {
            "id": str(oid) if oid else None,
            "userId": user_id or (str(oid) if oid else None),
            "username": username or (str(oid) if oid else "unknown"),
        }
        client_key = f"{prefix}ClientId"
        if doc.get(client_key):
            info["clientId"] = doc[client_key]
        return info

    def _serialize_message_doc(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        sender_info = self._build_user_info(doc, "sender")
        receiver_info = self._build_user_info(doc, "receiver")
        text = doc.get("mensaje") or doc.get("message") or ""
        ts = doc.get("fecha")
        ts_str = None
        ts_millis = doc.get("tsMillis")
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_str = ts.isoformat()
            if ts_millis is None:
                ts_millis = int(ts.timestamp() * 1000)
        if ts_str is None:
            ts = ts or datetime.now(self.local_tz)
            if isinstance(ts, datetime):
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=self.local_tz)
                ts_str = ts.isoformat()
                if ts_millis is None:
                    ts_millis = int(ts.timestamp() * 1000)
            else:
                ts_str = str(ts)
        payload = {
            "id": str(doc.get("_id")),
            "from": sender_info["username"],
            "fromId": sender_info.get("userId") or sender_info.get("id"),
            "to": receiver_info["username"],
            "toId": receiver_info.get("userId") or receiver_info.get("id"),
            "text": text,
            "ts": ts_str,
            "tsMillis": ts_millis,
            "sender": sender_info,
            "receiver": receiver_info,
            "deliveryStatus": doc.get("deliveryStatus", "sent"),
            "readStatus": doc.get("readStatus", "unread")
        }
        if doc.get("hora"):
            payload["hora"] = doc["hora"]
        return payload

    def _parse_date(self, value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value)
            except Exception:
                return None
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
                try:
                    return datetime.strptime(value, fmt)
                except Exception:
                    continue
            try:
                return datetime.fromisoformat(value)
            except Exception:
                return None
        return None

    def connect(self):
        try:
            self._init_mongo()
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.bus_host, self.bus_port))
            logging.info(f"🔌 Conectando al BUS en {self.bus_host}:{self.bus_port}")

            send_jsonline(self.socket, {
                "type": "REGISTER",
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
                
                # ✅ Iniciar threads de gestión
                threading.Thread(target=self._listen_messages, daemon=True).start()
                threading.Thread(target=self._heartbeat_monitor, daemon=True).start()
                threading.Thread(target=self._typing_timeout_monitor, daemon=True).start()
                
                logging.info("✅ Registrado exitosamente en el BUS")
                return True
            else:
                logging.error(f"❌ Error en registro: {ack}")
                return False
        except Exception as e:
            logging.error(f"❌ Error conectando al BUS: {e}")
            return False

    # ✅ Monitor de heartbeat
    def _heartbeat_monitor(self):
        while self.running:
            try:
                now = datetime.utcnow()
                with self.users_lock:
                    offline_users = []
                    for user_id, info in self.online_users.items():
                        last_seen = info.get("last_seen")
                        if last_seen and (now - last_seen).total_seconds() > self.heartbeat_timeout:
                            offline_users.append(user_id)
                    
                    for user_id in offline_users:
                        self._set_user_offline(user_id)
                        
                time.sleep(self.heartbeat_interval)
            except Exception as e:
                logging.error(f"Error en heartbeat monitor: {e}")

    # ✅ Monitor de timeout de "escribiendo"
    def _typing_timeout_monitor(self):
        while self.running:
            try:
                now = datetime.utcnow()
                with self.typing_lock:
                    for conv_id, users in list(self.typing_status.items()):
                        for user_id, timestamp in list(users.items()):
                            if (now - timestamp).total_seconds() > 5:  # 5 segundos de timeout
                                del self.typing_status[conv_id][user_id]
                                self._broadcast_typing_status(conv_id, user_id, False)
                        
                        if not self.typing_status[conv_id]:
                            del self.typing_status[conv_id]
                            
                time.sleep(1)
            except Exception as e:
                logging.error(f"Error en typing monitor: {e}")

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
                if self.running:
                    logging.error(f"Error recibiendo: {e}")
                    self.connected = False
                break
        logging.info("🔇 Listener detenido")

    def _handle_message(self, message: dict):
        mtype = message.get("type")
        sender = message.get("sender", "UNKNOWN")
        
        if mtype == "REQUEST":
            payload = dict(message.get("payload", {}) or {})
            header = message.get("header") or {}
            if "action" not in payload and header.get("action"):
                payload["action"] = header["action"]
            corr = header.get("correlationId")
            self._route_request(sender, payload, corr)

    def _reply(self, target: str, payload: dict, corr: Optional[str] = None):
        msg = { "type": "DIRECT", "target": target, "payload": payload }
        if corr:
            msg["header"] = {"correlationId": corr}
        send_jsonline(self.socket, msg)

    # ✅ Broadcasting a usuarios específicos
    def _broadcast_to_user(self, user_id: str, event_type: str, data: dict):
        """Envía evento a un usuario específico si está online"""
        with self.users_lock:
            user_info = self.online_users.get(user_id)
        if user_info:
            client_id = user_info.get("client_id")
            if client_id:
                try:
                    self._reply(client_id, {
                        "event": event_type,
                        "data": data
                    })
                    logging.info(f"📤 Evento '{event_type}' enviado a usuario {user_id[:8]}... (client: {client_id})")
                except Exception as e:
                    logging.error(f"❌ Error enviando evento a {user_id}: {e}")
        else:
            logging.warning(f"⚠️ Usuario {user_id[:8]}... no está online")

    # ✅ Broadcasting a una sala/canal
    def _broadcast_to_room(self, room_id: str, event_type: str, data: dict, exclude_user: str = None):
        """Envía evento a todos los usuarios de una sala"""
        with self.rooms_lock:
            users = self.rooms.get(room_id, set())
            for user_id in users:
                if user_id != exclude_user:
                    self._broadcast_to_user(user_id, event_type, data)

    # ✅ Notificar estado de "escribiendo"
    def _broadcast_typing_status(self, conversation_id: str, user_id: str, is_typing: bool):
        """Notifica a otros usuarios que alguien está escribiendo"""
        parts = conversation_id.split("_")
        if len(parts) == 2:
            other_user = parts[1] if parts[0] == user_id else parts[0]
            self._broadcast_to_user(other_user, "user_typing", {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "is_typing": is_typing
            })

    # ✅ Gestión de usuarios online/offline
    def _set_user_online(self, user_id: str, client_id: str):
        with self.users_lock:
            was_offline = user_id not in self.online_users
            self.online_users[user_id] = {
                "client_id": client_id,
                "last_seen": datetime.utcnow(),
                "status": "online"
            }
            if was_offline:
                logging.info(f"👤 Usuario {user_id} → ONLINE")
                self._broadcast_user_status(user_id, "online")

    def _set_user_offline(self, user_id: str):
        with self.users_lock:
            if user_id in self.online_users:
                del self.online_users[user_id]
                logging.info(f"👤 Usuario {user_id} → OFFLINE")
                self._broadcast_user_status(user_id, "offline")

    def _broadcast_user_status(self, user_id: str, status: str):
        """Notifica cambio de estado a contactos/salas relevantes"""
        # Aquí podrías consultar contactos del usuario y notificarles
        pass

    def _route_request(self, sender: str, p: dict, corr: Optional[str] = None):
        act = p.get("action")
        try:
            # ✅ Heartbeat
            if act == "heartbeat":
                user_id = p.get("userId")
                if user_id:
                    with self.users_lock:
                        if user_id in self.online_users:
                            self.online_users[user_id]["last_seen"] = datetime.utcnow()
                self._reply(sender, {"ok": True, "pong": True}, corr)

            # ✅ Conectar usuario
            elif act == "connect":
                user_id = p.get("userId")
                if user_id:
                    self._set_user_online(user_id, sender)
                    self._reply(sender, {"ok": True, "status": "connected"}, corr)
                else:
                    self._reply(sender, {"ok": False, "error": "userId requerido"}, corr)

            # ✅ Desconectar usuario
            elif act == "disconnect":
                user_id = p.get("userId")
                if user_id:
                    self._set_user_offline(user_id)
                    self._reply(sender, {"ok": True, "status": "disconnected"}, corr)

            # ✅ Usuario escribiendo
            elif act == "typing":
                user_id = p.get("userId")
                conversation_id = p.get("conversationId")
                is_typing = p.get("isTyping", True)
                
                if user_id and conversation_id:
                    with self.typing_lock:
                        if is_typing:
                            if conversation_id not in self.typing_status:
                                self.typing_status[conversation_id] = {}
                            self.typing_status[conversation_id][user_id] = datetime.utcnow()
                        else:
                            if conversation_id in self.typing_status:
                                self.typing_status[conversation_id].pop(user_id, None)
                    
                    self._broadcast_typing_status(conversation_id, user_id, is_typing)
                    self._reply(sender, {"ok": True}, corr)
    
            # ✅ Enviar mensaje con ACK y notificación
            elif act == "send":
                if self.msgs is None:
                    self._reply(sender, {"ok": False, "error": "Mongo no inicializado"}, corr)
                    return
                    
                now = datetime.now(timezone.utc)
                now_local = now.astimezone(self.local_tz)
                sender_raw = self._safe_str(
                    p.get("senderObjId") or p.get("senderId") or p.get("sender") or p.get("from")
                )
                receiver_raw = self._safe_str(
                    p.get("receiverObjId") or p.get("receiverId") or p.get("receiver") or p.get("to")
                )
                sender_oid   = to_object_id_any(sender_raw or p.get("senderObjId"))
                receiver_oid = to_object_id_any(receiver_raw or p.get("receiverObjId"))
                sender_username = self._safe_str(p.get("senderUsername") or p.get("senderName")) or sender_raw or "unknown"
                receiver_username = self._safe_str(p.get("receiverUsername") or p.get("receiverName")) or receiver_raw or "unknown"
                sender_identifier = sender_raw or (str(sender_oid) if sender_oid else "")
                receiver_identifier = receiver_raw or (str(receiver_oid) if receiver_oid else "")
                
                # ✅ Agregar logs para debugging
                logging.info(f"📨 Enviando mensaje: {str(sender_oid)[:8]}... → {str(receiver_oid)[:8]}...")
                
                doc = {
                    "fecha": now_local,
                    "hora": now_local.strftime("%H:%M:%S"),
                    "timestamp": now_local.isoformat(),
                    "tsMillis": int(now_local.timestamp() * 1000),
                    "sender": sender_oid,
                    "receiver": receiver_oid,
                    "mensaje": p.get("message", ""),
                    "deliveryStatus": "enviado",
                    "readStatus": "no_leido",
                    "senderUsername": sender_username,
                    "receiverUsername": receiver_username,
                    "senderClientId": sender
                }
                if sender_raw:
                    doc["senderUserId"] = sender_raw
                if receiver_raw:
                    doc["receiverUserId"] = receiver_raw
                receiver_client = self._safe_str(p.get("receiverClientId") or p.get("receiverClient"))
                if receiver_client:
                    doc["receiverClientId"] = receiver_client
                mid = self.msgs.insert_one(doc).inserted_id
                
                # ✅ ACK al remitente
                self._reply(sender, {
                    "ok": True, 
                    "messageId": str(mid), 
                    "status": "sent",
                    "timestamp": now.isoformat()
                }, corr)
                
                # ✅ Log antes de notificar
                logging.info(f"🔔 Intentando notificar a usuario {receiver_identifier[:8]}...")
                
                with self.users_lock:
                    logging.info(f"📋 Usuarios online: {list(self.online_users.keys())}")
                
                # ✅ Notificar al destinatario en tiempo real
                self._broadcast_to_user(receiver_identifier, "new_message", {
                    "messageId": str(mid),
                    "from": str(sender_oid) if sender_oid else sender_identifier,
                    "fromUsername": sender_username,
                    "fromUserId": sender_raw,
                    "to": receiver_identifier,
                    "toUsername": receiver_username,
                    "toUserId": receiver_raw,
                    "text": p.get("message", ""),
                    "timestamp": now_local.isoformat()
                })
    
            # ✅ Marcar mensaje como entregado
            elif act == "markDelivered":
                message_id = p.get("messageId")
                if message_id and self.msgs is not None:  # ✅ Cambiar: self.msgs → self.msgs is not None
                    result = self.msgs.update_one(
                        {"_id": ObjectId(message_id)},
                        {"$set": {"deliveryStatus": "entregado"}}
                    )
                    self._reply(sender, {"ok": True, "modified": result.modified_count}, corr)

            # ✅ Marcar mensaje como leído
            elif act == "markRead":
                message_id = p.get("messageId")
                if message_id and self.msgs is not None:  # ✅ Cambiar: self.msgs → self.msgs is not None
                    msg = self.msgs.find_one_and_update(
                        {"_id": ObjectId(message_id)},
                        {"$set": {"readStatus": "leido"}},
                        return_document=True
                    )
                    if msg:
                        # Notificar al remitente que su mensaje fue leído
                        self._broadcast_to_user(str(msg["sender"]), "message_read", {
                            "messageId": message_id,
                            "readBy": str(msg["receiver"])
                        })
                    self._reply(sender, {"ok": True}, corr)
                                        
            # ✅ Obtener conversación
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
                
                items = [self._serialize_message_doc(d) for d in cur]
                
                self._reply(sender, {
                    "ok": True, 
                    "messages": items, 
                    "hasMore": len(items) == lim
                }, corr)
            
            # ✅ Listado completo para consola/admin
            elif act == "get_all_messages":
                if self.msgs is None:
                    self._reply(sender, {"ok": False, "error": "Mongo no inicializado"}, corr)
                    return
                lim = int(p.get("limit", 500))
                lim = max(1, min(lim, 2000))
                query: Dict[str, Any] = {}
                user_filter = self._safe_str(p.get("user") or p.get("username") or p.get("userId"))
                if user_filter:
                    ors = []
                    uid = to_object_id_any(user_filter)
                    if uid:
                        ors.append({"sender": uid})
                        ors.append({"receiver": uid})
                    ors.append({"senderUserId": user_filter})
                    ors.append({"receiverUserId": user_filter})
                    query["$or"] = ors
                date_cond: Dict[str, Any] = {}
                start = self._parse_date(p.get("from") or p.get("fromDate"))
                end = self._parse_date(p.get("to") or p.get("toDate"))
                if start:
                    date_cond["$gte"] = start
                if end:
                    date_cond["$lte"] = end
                if date_cond:
                    query["fecha"] = date_cond
                cur = self.msgs.find(query or {}).sort("fecha", -1).limit(lim)
                items = [self._serialize_message_doc(d) for d in cur]
                self._reply(sender, {"ok": True, "messages": items, "count": len(items)}, corr)

            # ✅ Unirse a sala/canal
            elif act == "joinRoom":
                room_id = p.get("roomId")
                user_id = p.get("userId")
                if room_id and user_id:
                    with self.rooms_lock:
                        self.rooms[room_id].add(user_id)
                    logging.info(f"👥 Usuario {user_id} se unió a sala {room_id}")
                    self._reply(sender, {"ok": True, "room": room_id}, corr)

            # ✅ Salir de sala/canal
            elif act == "leaveRoom":
                room_id = p.get("roomId")
                user_id = p.get("userId")
                if room_id and user_id:
                    with self.rooms_lock:
                        self.rooms[room_id].discard(user_id)
                    logging.info(f"👥 Usuario {user_id} salió de sala {room_id}")
                    self._reply(sender, {"ok": True}, corr)

            # ✅ Enviar mensaje a sala
            elif act == "sendToRoom":
                room_id = p.get("roomId")
                user_id = p.get("userId")
                message = p.get("message")
                
                if room_id and user_id and message:
                    self._broadcast_to_room(room_id, "room_message", {
                        "roomId": room_id,
                        "from": user_id,
                        "text": message,
                        "timestamp": datetime.utcnow().isoformat()
                    }, exclude_user=user_id)
                    self._reply(sender, {"ok": True}, corr)

            else:
                self._reply(sender, {"ok": False, "error": f"Acción desconocida: {act}"}, corr)

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
            logging.info("🚀 Servicio de Mensajería en Tiempo Real iniciado")
            while svc.connected:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        svc.disconnect()

if __name__ == "__main__":
    main()
