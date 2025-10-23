import socket, json, threading, logging, time, os
from datetime import datetime
from typing import Any, Dict, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [LLAMADAS] - %(levelname)s - %(message)s")

def _jsonline(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

class LlamadasService:
    def __init__(
        self,
        client_id: str = os.getenv("CLIENT_ID", "llamadas_service"),
        bus_host: str = os.getenv("BUS_HOST", "bus"),
        bus_port: int = int(os.getenv("BUS_PORT", "5000")),
    ):
        self.client_id = client_id
        self.bus_host = bus_host
        self.bus_port = bus_port
        self.socket: Optional[socket.socket] = None
        self.connected = False
        self.running = False

    # ---------------- BUS ----------------
    def connect_bus(self) -> bool:
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            logging.info(f"Conectando al BUS en {self.bus_host}:{self.bus_port}")
            self.socket.connect((self.bus_host, self.bus_port))

            reg = {"type": "REGISTER", "client_id": self.client_id, "kind": "service", "service": "Llamadas"}
            self.socket.sendall(_jsonline(reg))

            # esperar REGISTER_ACK (NDJSON)
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
                        threading.Thread(target=self._listen, daemon=True).start()
                        logging.info("Registrado en BUS como servicio 'Llamadas'")
                        logging.info("Servicio de Llamadas iniciado correctamente")
                        return True

            logging.error(f"Error de registro BUS: {buf!r}")
            return False

        except Exception as e:
            logging.error(f"Error conectando al BUS: {e}")
            return False

    def _listen(self):
        logging.info("Iniciando escucha de mensajes del BUS...")
        buf = ""
        while self.running and self.connected:
            try:
                data = self.socket.recv(65536)
                if not data:
                    logging.warning("Conexión cerrada por el BUS")
                    self.connected = False
                    break
                buf += data.decode("utf-8")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        self._handle(msg)
                    except json.JSONDecodeError as e:
                        logging.error(f"Error JSON NDJSON: {e}")
            except Exception as e:
                if self.running:
                    logging.error(f"Error recibiendo: {e}")
                    self.connected = False
                break
        logging.info("Listener detenido")

    def _send(self, d: Dict[str, Any]) -> bool:
        if not self.connected:
            logging.error("No conectado al BUS")
            return False
        try:
            d.setdefault("sender", self.client_id)
            self.socket.sendall(_jsonline(d))
            logging.info(f"Mensaje enviado - Tipo: {d.get('type')}")
            return True
        except Exception as e:
            logging.error(f"Error enviando: {e}")
            self.connected = False
            return False

    def _respond_direct(self, target: str, payload: Dict[str, Any], corr: Optional[str]):
        out = {"type": "DIRECT", "target": target, "payload": payload, "service": "Llamadas"}
        if corr:
            out["header"] = {"correlationId": corr}
        self._send(out)

    # Fire-and-forget hacia RegistroLlamadas
    def _notify_registro_llamadas(self, registro: Dict[str, Any]):
        """
        Envía un REQUEST a RegistroLlamadas con acción 'save'.
        No esperamos respuesta para no bloquear al cliente móvil.
        """
        req = {
            "type": "REQUEST",
            "header": {"service": "RegistroLlamadas", "action": "save"},
            "payload": registro
        }
        self._send(req)

    # --------------- Handler ---------------
    def _handle(self, m: Dict[str, Any]):
        mtype = m.get("type")
        sender = m.get("sender", "UNKNOWN")
        logging.info(f"Mensaje recibido - Tipo: {mtype}, De: {sender}")

        if mtype == "REQUEST":
            payload = m.get("payload", {}) or {}
            header = m.get("header") or {}
            corr = header.get("correlationId")

            try:
                action = (header.get("action") or payload.get("action") or "").strip()
                if action == "record":
                    resp = self._record_call(payload)
                else:
                    resp = {"ok": False, "error": f"Acción no soportada: {action}"}
            except Exception as e:
                logging.exception("Fallo manejando REQUEST")
                resp = {"ok": False, "error": str(e)}

            # DIRECT al cliente (Android espera el payload sin bloquear guardado)
            self._respond_direct(sender, resp, corr)
            return

        if mtype == "DELIVERY_ACK":
            logging.info(f"Entregado a {m.get('target')}")
            return

        if mtype in ("DIRECT", "BROADCAST", "EVENT"):
            logging.info(f"(info) {mtype} => {m.get('payload')}")
            return

        logging.debug(f"(ignorado) {m}")

    # --------------- Acción: record ---------------
    def _record_call(self, p: Dict[str, Any]) -> Dict[str, Any]:
        destination = (p.get("destination") or "").strip()
        if not destination:
            return {"ok": False, "error": "destination requerido"}

        status = (p.get("status") or "attempted").strip()
        duration = p.get("duration")
        caller = (p.get("callerId") or "unknown").strip()
        depto = (p.get("depto") or "").strip()
        now_iso = datetime.utcnow().isoformat()

        # Notificar registro (no bloquea)
        self._notify_registro_llamadas({
            "destination": destination,
            "status": status,
            "duration": duration,
            "callerId": caller,
            "timestamp": now_iso,
            "depto": depto or None
        })

        # Respuesta "amigable" para Android (status al top-level)
        return {
            "ok": True,
            "status": status,
            "destination": destination,
            "duration": duration,
            "callerId": caller,
            "depto": depto or None,
            "ts": now_iso
        }

    # --------------- ciclo de vida ---------------
    def disconnect(self):
        logging.info("Desconectando...")
        self.running = False
        self.connected = False
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
        logging.info("Llamadas detenido")


def main():
    svc = LlamadasService()
    try:
        if not svc.connect_bus():
            return
        while svc.connected:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("\nDeteniendo servicio...")
    finally:
        svc.disconnect()

if __name__ == "__main__":
    main()
