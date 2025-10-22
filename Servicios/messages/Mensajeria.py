import socket, json, threading, logging, time, os, hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Any, Dict, Set
from pymongo import MongoClient, ASCENDING
from bson.objectid import ObjectId
from collections import defaultdict

# ✅ Zona horaria de Chile
zona_chile = ZoneInfo("America/Santiago")

# ✅ Logging solo para mensajes importantes
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [MENSAJERÍA] - %(message)s')

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

    def _init_mongo(self):
        cli = MongoClient(self.mongo_uri, serverSelectionTimeoutMS=6000)
        cli.admin.command("ping")
        self.db = cli[self.mongo_db]
        self.msgs = self.db[self.mongo_coll]
        self.msgs.create_index([("sender",ASCENDING),("receiver",ASCENDING),("fecha",ASCENDING)])
        logging.info(f"✅ Mongo listo (Mensajería) → DB: {self.mongo_db}, colección: {self.mongo_coll}")

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
                
                logging.info(f"✅ Servicio de Mensajería registrado en el BUS")
                
                # ✅ Iniciar threads de gestión
                threading.Thread(target=self._listen_messages, daemon=True).start()
                threading.Thread(target=self._heartbeat_monitor, daemon=True).start()
                threading.Thread(target=self._typing_timeout_monitor, daemon=True).start()
                
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
                now = datetime.now(zona_chile)
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
                now = datetime.now(zona_chile)
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
        logging.info("👂 Servicio de mensajería escuchando...")
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
                    
                    try:
                        message = json.loads(line)
                        self._handle_message(message)
                    except json.JSONDecodeError as je:
                        logging.error(f"❌ Error parseando JSON: {je}")
                        
            except Exception as e:
                if self.running:
                    logging.error(f"❌ Error en socket: {e}")
                    self.connected = False
                break
        logging.info("🔇 Listener detenido")

    def _handle_message(self, message: dict):
        mtype = message.get("type")
        sender = message.get("sender", "UNKNOWN")
        
        if mtype == "REQUEST":
            payload = message.get("payload", {}) or {}
            header = message.get("header") or {}
            corr = header.get("correlationId")
            # ✅ El action puede venir en header o en payload
            action = header.get("action") or payload.get("action")
            self._route_request(sender, payload, corr, action)
        elif mtype == "BROADCAST":
            pass  # Ignorar broadcasts silenciosamente
        elif mtype == "DIRECT":
            pass  # Ignorar directs silenciosamente
        else:
            logging.warning(f"⚠️ Tipo de mensaje desconocido: {mtype}")

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
                "last_seen": datetime.now(zona_chile),
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

    def _route_request(self, sender: str, p: dict, corr: Optional[str] = None, action: Optional[str] = None):
        # ✅ El action puede venir como parámetro o dentro del payload
        act = action or p.get("action")
        
        try:
            # ✅ Heartbeat
            if act == "heartbeat":
                user_id = p.get("userId")
                if user_id:
                    with self.users_lock:
                        if user_id in self.online_users:
                            self.online_users[user_id]["last_seen"] = datetime.now(zona_chile)
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
                            self.typing_status[conversation_id][user_id] = datetime.now(zona_chile)
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
                    
                now = datetime.now(zona_chile)
                sender_oid   = to_object_id_any(p.get("senderObjId")   or p.get("senderId"))
                receiver_oid = to_object_id_any(p.get("receiverObjId") or p.get("receiverId"))
                
                doc = {
                    "fecha": now,
                    "hora": now.strftime("%H:%M:%S"),
                    "sender": sender_oid,
                    "receiver": receiver_oid,
                    "mensaje": p.get("message", ""),
                    "deliveryStatus": "enviado",
                    "readStatus": "no_leido"
                }
                mid = self.msgs.insert_one(doc).inserted_id
                
                # ✅ ACK al remitente
                self._reply(sender, {
                    "ok": True, 
                    "messageId": str(mid), 
                    "status": "sent",
                    "timestamp": now.isoformat()
                }, corr)
                
                # ✅ Notificar al destinatario en tiempo real
                receiver_str = str(receiver_oid)
                logging.info(f"� {str(sender_oid)[:8]}... → {receiver_str[:8]}...: {p.get('message', '')[:50]}")
                
                self._broadcast_to_user(receiver_str, "new_message", {
                    "messageId": str(mid),
                    "from": str(sender_oid),
                    "to": receiver_str,
                    "text": p.get("message", ""),
                    "timestamp": now.isoformat()
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
                
                items = [{
                    "id": str(d["_id"]),
                    "from": str(d.get("sender")),
                    "to":   str(d.get("receiver")),
                    "text": d.get("mensaje"),
                    "ts":   d["fecha"].isoformat(),
                    "deliveryStatus": d.get("deliveryStatus", "sent"),
                    "readStatus": d.get("readStatus", "unread")
                } for d in cur]
                
                self._reply(sender, {
                    "ok": True, 
                    "messages": items, 
                    "hasMore": len(items) == lim
                }, corr)

            # ✅ Obtener TODOS los mensajes de la BD
            elif act == "get_all_messages":
                if self.msgs is None:
                    self._reply(sender, {"ok": False, "error": "Mongo no inicializado"}, corr)
                    return
                
                try:
                    # Parámetros opcionales
                    limit = int(p.get("limit", 1000))  # Límite por defecto: 1000
                    skip = int(p.get("skip", 0))       # Para paginación
                    sort_order = p.get("sortOrder", "desc")  # "asc" o "desc"
                    
                    # Filtros opcionales
                    filters = {}
                    if p.get("senderId"):
                        filters["sender"] = to_object_id_any(p.get("senderId"))
                    if p.get("receiverId"):
                        filters["receiver"] = to_object_id_any(p.get("receiverId"))
                    if p.get("readStatus"):
                        filters["readStatus"] = p.get("readStatus")
                    if p.get("deliveryStatus"):
                        filters["deliveryStatus"] = p.get("deliveryStatus")
                    
                    # Consultar base de datos
                    sort_direction = -1 if sort_order == "desc" else 1
                    cursor = self.msgs.find(filters).sort("fecha", sort_direction).skip(skip).limit(limit)
                    
                    # Formatear resultados
                    messages = []
                    for doc in cursor:
                        messages.append({
                            "id": str(doc["_id"]),
                            "from": str(doc.get("sender")),
                            "to": str(doc.get("receiver")),
                            "text": doc.get("mensaje", ""),
                            "timestamp": doc["fecha"].isoformat(),
                            "hora": doc.get("hora", ""),
                            "deliveryStatus": doc.get("deliveryStatus", "sent"),
                            "readStatus": doc.get("readStatus", "unread")
                        })
                    
                    # Contar total de documentos (para paginación)
                    total_count = self.msgs.count_documents(filters)
                    
                    self._reply(sender, {
                        "ok": True,
                        "messages": messages,
                        "total": total_count,
                        "count": len(messages),
                        "hasMore": (skip + len(messages)) < total_count
                    }, corr)
                    
                except Exception as e:
                    logging.error(f"❌ Error obteniendo todos los mensajes: {e}")
                    self._reply(sender, {"ok": False, "error": str(e)}, corr)

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
                        "timestamp": datetime.now(zona_chile).isoformat()
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