# tests/e2e_host_tester.py
import socket, json, time, sys
from typing import Optional

BUS_HOST = "127.0.0.1"
BUS_PORT = 5000
CLIENT_ID = "host_full_tester"

# Mapa nombre-servicio -> client_id esperado (para reconocer respuestas)
SERVICE_ID = {
    "Autenticacion":   "autenticacion_service",
    "Administracion":  "administracion_service",
    "RegistroLlamadas":"registro_llamadas_service",
    "Mensajeria":      "mensajeria_service",
    "Contactos":       "contactos_service",
    "Llamadas":        "llamadas_service",
}

# ---------- NDJSON helpers ----------
def send_jsonline(sock: socket.socket, obj: dict):
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    sock.sendall(data)

def recv_line(sock: socket.socket, timeout: float = 10.0) -> Optional[dict]:
    sock.settimeout(timeout)
    buf = ""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return None
            buf += chunk.decode("utf-8")
            if "\n" in buf:
                line, rest = buf.split("\n", 1)
                # deja el resto en un buffer global? mantenlo local (una línea por lectura)
                try:
                    if line.strip():
                        return json.loads(line)
                except json.JSONDecodeError:
                    # ignora líneas inválidas
                    return None
    except socket.timeout:
        return None

def wait_direct_from(sock: socket.socket, expected_sender: str, timeout: float = 8.0):
    """Espera un DIRECT desde expected_sender. Ignora ACK/BROADCAST/etc."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        msg = recv_line(sock, timeout=timeout)
        if not msg:
            continue
        mtype = msg.get("type")
        sender = msg.get("sender")
        if mtype == "DELIVERY_ACK":
            print(f"  [ACK] {msg}")
            continue
        if mtype == "DIRECT" and sender == expected_sender:
            return msg
        # puedes ver otros mensajes y seguir esperando
        print(f"  [info] recibido {mtype} de {sender}: {msg.get('payload')}")
    return None

# ---------- Cliente host ----------
def connect_as_client():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((BUS_HOST, BUS_PORT))
    send_jsonline(s, {"type":"REGISTER","client_id":CLIENT_ID,"kind":"client","service":"E2EHost"})
    ack = recv_line(s, timeout=5)
    if not ack or ack.get("type") != "REGISTER_ACK":
        raise RuntimeError(f"REGISTER_ACK inválido: {ack}")
    print(f"✅ Conectado al BUS como {CLIENT_ID}")
    return s

def req(sock: socket.socket, service_name: str, payload: dict, wait: bool = True, timeout: float = 8.0):
    """Envía REQUEST al servicio por nombre (routing del BUS)."""
    send_jsonline(sock, {"type":"REQUEST","service":service_name,"payload":payload})
    if not wait:
        return None
    msg = wait_direct_from(sock, SERVICE_ID[service_name], timeout=timeout)
    if not msg:
        raise RuntimeError(f"Sin respuesta de {service_name} en {timeout}s")
    return msg.get("payload")

# ---------- Escenarios ----------
def scenario_admin(sock):
    print("\n=== Escenario ADMIN ===")
    # 1) Crear admin
    print("1) create_user(admin1, role=admin)")
    r = req(sock, "Autenticacion", {"action":"create_user","username":"admin1","password":"secret123","role":"admin"})
    print("   →", r)

    # 2) Login admin (contrato 'login' del informe)
    print("2) login(admin1)")
    r = req(sock, "Autenticacion", {"action":"login","username":"admin1","password":"secret123"})
    print("   →", r)
    assert r["data"].get("token"), "login no retornó token"
    admin_token = r["data"]["token"]

    # 3) Verify_admin por Administración
    print("3) verify_admin(admin1)")
    r = req(sock, "Administracion", {"action":"verify_admin","username":"admin1"})
    print("   →", r)

    # 4) Contactos: crear + buscar por contrato 'search'
    print("4) contactos.create_contact & search")
    r = req(sock, "Contactos", {"action":"create_contact","nombre":"Juan Pérez","departamento":"402","telefono":"+56 9 1111 1111"})
    print("   →", r)
    r = req(sock, "Contactos", {"action":"search","departamento":"402","nombre":""})
    print("   →", r)

    # 5) Registro llamadas: record
    print("5) registro_llamadas.record")
    r = req(sock, "RegistroLlamadas", {"action":"record","callerId":"admin1","destination":"402","status":"recibida","duration":60})
    print("   →", r)

    # 6) Mensajería: enviar mensaje a usuario normal alice (lo crearemos en escenario_user)
    print("6) mensajeria.send(admin1 → alice)")
    r = req(sock, "Mensajeria", {"action":"send","senderId":"admin1","receiverId":"alice","message":"Hola Alice, soy admin1"})
    print("   →", r)

    # 7) Reporte administración
    print("7) administracion.generateReport(últimos 2 días)")
    r = req(sock, "Administracion", {"action":"generateReport","days":2})
    print("   →", r)
    # 8) Validar sesión admin
    print("8) autenticacion.validate_session")
    r = req(sock, "Autenticacion", {"action":"validate_session","session_id":admin_token})
    print("   →", r)

def scenario_user(sock):
    print("\n=== Escenario USUARIO ===")
    # 1) Crear usuario normal
    print("1) create_user(alice, role=user)")
    r = req(sock, "Autenticacion", {"action":"create_user","username":"alice","password":"alice123","role":"user"})
    print("   →", r)

    # 2) Login
    print("2) login(alice)")
    r = req(sock, "Autenticacion", {"action":"login","username":"alice","password":"alice123"})
    print("   →", r)
    assert r["data"].get("token"), "login no retornó token"
    alice_token = r["data"]["token"]

    # 3) verify_admin (debe ser falso/no admin)
    print("3) verify_admin(alice)")
    r = req(sock, "Administracion", {"action":"verify_admin","username":"alice"})
    print("   →", r)

    # 4) getConversation con admin1
    print("4) mensajeria.getConversation(alice, admin1)")
    r = req(sock, "Mensajeria", {"action":"getConversation","user1":"alice","user2":"admin1","limit":20})
    print("   →", r)

    # 5) Historial de llamadas (si las hay)
    print("5) registro_llamadas.history(alice)")
    r = req(sock, "RegistroLlamadas", {"action":"history","userId":"alice","limit":20})
    print("   →", r)

    # 6) validar sesión user
    print("6) autenticacion.validate_session")
    r = req(sock, "Autenticacion", {"action":"validate_session","session_id":alice_token})
    print("   →", r)

def main():
    s = connect_as_client()
    try:
        # Corre escenarios
        scenario_admin(s)
        scenario_user(s)
        print("\n✅ E2E finalizado OK (revisa arriba cada respuesta)")
    finally:
        try: s.close()
        except: pass

if __name__ == "__main__":
    main()
