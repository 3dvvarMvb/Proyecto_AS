# Servicios/esb_lib.py
import os, socket, json, struct, threading, logging
from datetime import datetime, timezone
from typing import Optional, Callable, Dict

LEN_HDR = 4

def _now():
    return datetime.now(timezone.utc).isoformat()

def pack(obj: dict) -> bytes:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return struct.pack("!I", len(data)) + data

def recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk: return None
        buf += chunk
    return buf

def recv_frame(sock: socket.socket) -> Optional[dict]:
    hdr = recv_exact(sock, LEN_HDR)
    if hdr is None: return None
    (length,) = struct.unpack("!I", hdr)
    payload = recv_exact(sock, length)
    if payload is None: return None
    return json.loads(payload.decode("utf-8"))

class EsbService:
    """Base para servicios ESB (TCP nativo, enrutamiento por contenido)."""

    def __init__(self, client_id: str, service_name: str, bus_host: Optional[str]=None, bus_port: Optional[int]=None):
        self.client_id = client_id
        self.service_name = service_name
        self.bus_host = bus_host or os.getenv("BUS_HOST", "localhost")
        self.bus_port = int(bus_port or os.getenv("BUS_PORT", "5000"))
        self.sock: Optional[socket.socket] = None
        self.running = False
        self.logger = logging.getLogger(service_name)

    # --- conexión & registro ---
    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.bus_host, self.bus_port))
        self.send({"type":"REGISTER","client_id":self.client_id,"kind":"service","service":self.service_name})
        # no esperamos nada aquí: el loop leerá REGISTER_ACK
        self.running = True
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.logger.info(f"[{self.service_name}] conectado a ESB {self.bus_host}:{self.bus_port} como {self.client_id}")

    def stop(self):
        self.running = False
        try:
            if self.sock: self.sock.close()
        finally:
            self.sock = None

    # --- IO ---
    def send(self, obj: dict):
        if not self.sock: return
        obj.setdefault("timestamp", _now())
        self.sock.sendall(pack(obj))

    # --- API alto nivel ---
    def respond(self, reply_to: str, correlation_id: str, body: dict):
        self.send({"type":"RESPONSE","header":{"replyTo":reply_to,"correlationId":correlation_id},"body":body})

    def publish(self, topic: str, event: dict):
        self.send({"type":"PUBLISH","body":{"topic":topic,"event":event}})

    # --- loop ---
    def _read_loop(self):
        while self.running and self.sock:
            msg = recv_frame(self.sock)
            if msg is None: break
            self._handle(msg)

    def _handle(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "INVOKE":
            header = msg.get("header", {})
            body = msg.get("body", {}) or {}
            action = header.get("action", "")
            reply_to = header.get("replyTo")
            corr = header.get("correlationId", "")
            try:
                result = self.on_invoke(action, body, header)
                self.respond(reply_to, corr, {"ok": True, **(result or {})})
            except Exception as e:
                self.logger.exception("Error en INVOKE")
                self.respond(reply_to, corr, {"ok": False, "error": str(e)})
        # puedes loguear ACK/NACK/EVENT si hace falta
        elif mtype in ("ACK","NACK","REGISTER_ACK","EVENT"):
            self.logger.debug(f"{mtype}: {msg}")
        else:
            self.logger.warning(f"Tipo no manejado: {mtype}")

    # --- override en cada servicio ---
    def on_invoke(self, action: str, body: dict, header: dict) -> Optional[dict]:
        raise NotImplementedError
