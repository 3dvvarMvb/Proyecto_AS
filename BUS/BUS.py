import os
import socket
import threading
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Set
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [BUS] - %(levelname)s - %(message)s'
)

def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()

def send_jsonline(sock: socket.socket, obj: dict):
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    sock.sendall(data)

class MessageBus:
    """
    ESB TCP (puerto único). Protocolo NDJSON (1 JSON por línea).
    Tipos: REGISTER/REGISTER_ACK, BROADCAST, DIRECT, REQUEST, RESPONSE, SUBSCRIBE/SUBSCRIBE_ACK, PUBLISH/EVENT, ERROR, DELIVERY_ACK
    - En REQUEST, si viene service en header.service o msg['service'], se hace routing por nombre.
    - Si viene target, se respeta.
    """

    def __init__(self, host: str = None, port: int = None):
        self.host = host or os.getenv("BUS_HOST", "0.0.0.0")
        self.port = int(port or os.getenv("BUS_PORT", "5000"))
        self.server_socket: socket.socket | None = None
        self.running = False

        # Conexiones y metadatos
        self.clients: Dict[str, socket.socket] = {}          # client_id -> socket
        self.client_meta: Dict[str, Dict] = {}               # client_id -> {kind, service}
        self.clients_lock = threading.Lock()

        # Registro lógico de servicios (por nombre de servicio)
        self.services: Dict[str, List[str]] = defaultdict(list)  # "Autenticacion" -> [client_ids]
        self.rr_index: Dict[str, int] = defaultdict(int)

        # Pub/Sub
        self.topic_subs: Dict[str, Set[str]] = defaultdict(set)  # topic -> set(client_id)

    # ------------------------- ciclo de vida servidor -------------------------

    def start(self):
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(64)

            self.running = True
            logging.info(f"ESB iniciado en {self.host}:{self.port}")

            while self.running:
                try:
                    client_socket, address = self.server_socket.accept()
                    t = threading.Thread(target=self._handle_client, args=(client_socket, address), daemon=True)
                    t.start()
                except Exception as e:
                    if self.running:
                        logging.error(f"Error aceptando conexión: {e}")
        except Exception as e:
            logging.error(f"Error iniciando ESB: {e}")
        finally:
            self.stop()

    def stop(self):
        self.running = False
        with self.clients_lock:
            for cid, s in list(self.clients.items()):
                try:
                    s.close()
                except:
                    pass
            self.clients.clear()
            self.client_meta.clear()
            self.services.clear()
            self.topic_subs.clear()
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
        logging.info("ESB detenido")

    # ------------------------- manejo de cliente -------------------------

    def _handle_client(self, client_socket: socket.socket, address: Tuple):
        client_id = None
        buf = ""
        try:
            # Espera REGISTER (NDJSON: una línea)
            while "\n" not in buf:
                data = client_socket.recv(4096)
                if not data:
                    client_socket.close()
                    return
                buf += data.decode("utf-8")

            line, buf = buf.split("\n", 1)
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                client_socket.close()
                return

            if message.get("type") != "REGISTER":
                client_socket.close()
                return

            client_id = message.get("client_id")
            kind = message.get("kind", "service")
            service_name = message.get("service")  # ej: "Autenticacion"

            if not client_id:
                client_socket.close()
                return

            with self.clients_lock:
                self.clients[client_id] = client_socket
                self.client_meta[client_id] = {"kind": kind, "service": service_name}
                if kind == "service" and service_name:
                    self.services[service_name].append(client_id)

            ack = {"type": "REGISTER_ACK", "status": "success", "message": f"Cliente {client_id} registrado", "timestamp": utcnow_iso()}
            send_jsonline(client_socket, ack)
            logging.info(f"Registrado: id={client_id} kind={kind} service={service_name} desde {address}")

            # ✅ Broadcast a todos los clientes que alguien se conectó
            if kind == "client":
                self._broadcast_user_joined(client_id, sender_id=client_id)

            # Loop de mensajes NDJSON
            while self.running:
                data = client_socket.recv(65536)
                if not data:
                    break
                buf += data.decode("utf-8")
                while "\n" in buf:
                    raw, buf = buf.split("\n", 1)
                    raw = raw.strip()
                    if raw:
                        self._process_message(client_id, raw)

        except Exception as e:
            logging.error(f"Error con cliente {client_id or address}: {e}")
        finally:
            if client_id:
                self._cleanup_client(client_id)
            try:
                client_socket.close()
            except:
                pass

    def _cleanup_client(self, client_id: str):
        with self.clients_lock:
            meta = self.client_meta.get(client_id, {})
            kind = meta.get("kind")
            service_name = meta.get("service")
            if kind == "service" and service_name and client_id in self.services.get(service_name, []):
                self.services[service_name] = [cid for cid in self.services[service_name] if cid != client_id]
            for topic in list(self.topic_subs.keys()):
                self.topic_subs[topic].discard(client_id)
            self.clients.pop(client_id, None)
            self.client_meta.pop(client_id, None)
        
        # ✅ Broadcast a todos que alguien se desconectó
        if kind == "client":
            self._broadcast_user_left(client_id)
        
        logging.info(f"🔌 Desconectado: {client_id}")

    # ------------------------- enrutamiento -------------------------

    def _process_message(self, sender_id: str, raw_json: str):
        try:
            msg = json.loads(raw_json)
        except json.JSONDecodeError:
            logging.error(f"JSON inválido de {sender_id}")
            return

        mtype = msg.get("type")
        logging.info(f"📨 {sender_id} → BUS : {mtype}")

        if mtype == "BROADCAST":
            self._broadcast(sender_id, msg)
            return

        if mtype == "DIRECT":
            target = msg.get("target")
            if target:
                self._send_to(sender_id, target, msg)
            else:
                logging.warning("DIRECT sin 'target'")
            return

        if mtype == "REQUEST":
            header = msg.get("header", {})
            svc = header.get("service") or msg.get("service")
            if svc:
                target_id = self._pick_service_instance(svc)
                if not target_id:
                    self._error_back(sender_id, f"No hay instancias registradas de servicio '{svc}'")
                    return
                # reenviamos como REQUEST al service instance elegido
                fwd = dict(msg)
                fwd["service"] = svc
                fwd["target"] = target_id
                self._send_to(sender_id, target_id, fwd)
            else:
                target = msg.get("target")
                if target:
                    self._send_to(sender_id, target, msg)
                else:
                    self._error_back(sender_id, "REQUEST sin 'service' ni 'target'")
            return

        if mtype == "RESPONSE":
            # Si un servicio nos manda RESPONSE con 'target'/'reply_to', reenviamos.
            target = msg.get("target") or msg.get("reply_to")
            if target:
                self._send_to(sender_id, target, msg)
            else:
                logging.warning("RESPONSE sin 'target'/'reply_to'")
            return

        if mtype == "SUBSCRIBE":
            body = msg.get("body", {})
            topic = body.get("topic")
            if topic:
                with self.clients_lock:
                    self.topic_subs[topic].add(sender_id)
                ack = {"type": "SUBSCRIBE_ACK", "topic": topic, "status": "ok", "timestamp": utcnow_iso()}
                self._send_raw(sender_id, ack)
            else:
                self._error_back(sender_id, "SUBSCRIBE sin 'topic'")
            return

        if mtype == "PUBLISH":
            body = msg.get("body", {})
            topic = body.get("topic")
            event = body.get("event")
            if topic and event is not None:
                self._publish(sender_id, topic, event)
            else:
                self._error_back(sender_id, "PUBLISH requiere 'topic' y 'event'")
            return

        logging.warning(f"Tipo no soportado: {mtype}")

    def _pick_service_instance(self, service_name: str) -> str | None:
        with self.clients_lock:
            instances = self.services.get(service_name, [])
            if not instances:
                return None
            idx = self.rr_index[service_name] % len(instances)
            self.rr_index[service_name] += 1
            return instances[idx]

    # ------------------------- helpers envío -------------------------

    def _broadcast(self, sender_id: str, msg: dict):
        msg = dict(msg)
        msg["sender"] = sender_id
        msg["timestamp"] = utcnow_iso()
        data = (json.dumps(msg) + "\n").encode("utf-8")
        with self.clients_lock:
            for cid, sock in self.clients.items():
                if cid != sender_id:
                    try:
                        sock.sendall(data)
                    except Exception as e:
                        logging.error(f"Error broadcast a {cid}: {e}")

    def _send_to(self, sender_id: str, target_id: str, msg: dict):
        msg = dict(msg)
        msg["sender"] = sender_id
        msg["timestamp"] = utcnow_iso()
        with self.clients_lock:
            sock = self.clients.get(target_id)
        if not sock:
            self._error_back(sender_id, f"Destino '{target_id}' no encontrado")
            return
        try:
            sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))
            logging.info(f"{sender_id} → {target_id} : {msg.get('type')}")
            # ACK al emisor para DIRECT y REQUEST
            if msg.get("type") in ("DIRECT", "REQUEST"):
                ack = {"type": "DELIVERY_ACK", "status": "delivered", "target": target_id, "timestamp": utcnow_iso()}
                self._send_raw(sender_id, ack)
        except Exception as e:
            logging.error(f"Error enviando a {target_id}: {e}")

    def _publish(self, sender_id: str, topic: str, event: dict):
        env = {
            "type": "EVENT",
            "topic": topic,
            "event": event,
            "sender": sender_id,
            "timestamp": utcnow_iso()
        }
        data = (json.dumps(env) + "\n").encode("utf-8")
        with self.clients_lock:
            subs = list(self.topic_subs.get(topic, set()))
            sockets = [(cid, self.clients.get(cid)) for cid in subs if self.clients.get(cid)]
        for cid, sock in sockets:
            try:
                sock.sendall(data)
            except Exception as e:
                logging.error(f"Error publicando a {cid}: {e}")

    def _error_back(self, sender_id: str, message: str):
        err = {"type": "ERROR", "message": message, "timestamp": utcnow_iso()}
        self._send_raw(sender_id, err)

    def _send_raw(self, client_id: str, obj: dict):
        with self.clients_lock:
            sock = self.clients.get(client_id)
        if not sock:
            return
        try:
            send_jsonline(sock, obj)
        except Exception as e:
            logging.error(f"Error enviando a {client_id}: {e}")

    # ✅ Broadcast de usuarios conectados/desconectados
    def _broadcast_user_joined(self, client_id: str, sender_id: str):
        """Notifica a todos los clientes que alguien se conectó"""
        msg = {
            "type": "BROADCAST",
            "event": "user_joined",
            "client_id": client_id,
            "timestamp": utcnow_iso()
        }
        with self.clients_lock:
            for cid, sock in self.clients.items():
                if cid != sender_id and self.client_meta.get(cid, {}).get("kind") == "client":
                    try:
                        send_jsonline(sock, msg)
                    except Exception as e:
                        logging.error(f"Error broadcasting user_joined a {cid}: {e}")
        logging.info(f"BROADCAST: {client_id} se unió")

    def _broadcast_user_left(self, client_id: str):
        """Notifica a todos los clientes que alguien se desconectó"""
        msg = {
            "type": "BROADCAST",
            "event": "user_left",
            "client_id": client_id,
            "timestamp": utcnow_iso()
        }
        with self.clients_lock:
            for cid, sock in self.clients.items():
                if self.client_meta.get(cid, {}).get("kind") == "client":
                    try:
                        send_jsonline(sock, msg)
                    except Exception as e:
                        logging.error(f"Error broadcasting user_left a {cid}: {e}")
        logging.info(f"BROADCAST: {client_id} se fue")


def main():
    bus = MessageBus()
    try:
        bus.start()
    except KeyboardInterrupt:
        logging.info("Deteniendo ESB...")
        bus.stop()

if __name__ == "__main__":
    main()
