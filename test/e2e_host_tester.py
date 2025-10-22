import socket, json, time, sys
import base64
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
                try:
                    if line.strip():
                        return json.loads(line)
                except json.JSONDecodeError:
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

# Helpers de assertions suaves (no rompen el flujo)
def expect_ok(resp, label=""):
    ok = (isinstance(resp, dict) and (resp.get("ok") is True))
    if not ok:
        print(f"  [WARN] {label} respuesta no OK: {resp}")
    return ok

def scenario_admin(sock):
    print("\n=== Escenario ADMIN ===")
    # 1) Crear admin
    print("1) create_user(admin10, role=admin)")
    r = req(sock, "Autenticacion", {"action":"create_user","username":"admin10","password":"secret123","role":"admin"})
    print("   →", r)

    # 2) Login admin
    print("2) login(admin10)")
    r = req(sock, "Autenticacion", {"action":"login","username":"admin10","password":"secret123"})
    print("   →", r)
    assert r["data"].get("token"), "login no retornó token"
    admin_token = r["data"]["token"]

    # 3) Verify_admin por Administración
    print("3) verify_admin(admin10)")
    r = req(sock, "Administracion", {"action":"verify_admin","username":"admin10"})
    print("   →", r)

    # 4) Contactos: crear + buscar
    print("4) contactos.create_contact & search")
    r = req(sock, "Contactos", {"action":"create_contact","nombre":"Juan Pérez2","departamento":"404","telefono":"+56 9 1211 1111"})
    print("   →", r)
    r = req(sock, "Contactos", {"action":"search","departamento":"801A","nombre":""})
    print("   →", r)
    print("   →", r)
    r = req(sock, "Contactos", {"action":"search","departamento":"802C","nombre":""})
    print("   →", r)
    print("   →", r)
    r = req(sock, "Contactos", {"action":"search","departamento":"802B","nombre":""})
    print("   →", r)
    print("   →", r)
    r = req(sock, "Contactos", {"action":"search","departamento":"802A","nombre":""})
    print("   →", r)

    # 5) Registro llamadas: record
    print("5) registro_llamadas.record")
    r = req(sock, "RegistroLlamadas", {"action":"record","callerId":"admin10","destination":"403","status":"recibida","duration":60})
    print("   →", r)

    # 6) Mensajería: enviar mensaje a alice
    print("6) mensajeria.send(admin10 → alice)")
    r = req(sock, "Mensajeria", {"action":"send","senderId":"admin10","receiverId":"alice","message":"Hola Alice, soy admin10"})
    print("   →", r)

    # 7) Reporte administración
    print("7) administracion.generateReport(últimos 2 días)")
    r = req(sock, "Administracion", {"action":"generateReport","days":2})
    print("   →", r)

    # 8) Validar sesión admin
    print("8) autenticacion.validate_session")
    r = req(sock, "Autenticacion", {"action":"validate_session","session_id":admin_token})
    print("   →", r)

    # 9) whoami (si tienes la versión extendida de Autenticación)
    print("9) autenticacion.whoami")
    r = req(sock, "Autenticacion", {"action":"whoami","token":admin_token})
    print("   →", r)

    # 10) refresh session (dos formas: renew=True o refresh_session)
    print("10) autenticacion.validate_session (renew=True)")
    r = req(sock, "Autenticacion", {"action":"validate_session","token":admin_token,"session_id":admin_token,"renew": True})
    print("   →", r)

    # 11) logout con token
    print("11) autenticacion.logout(token)")
    r = req(sock, "Autenticacion", {"action":"logout","token":admin_token})
    print("   →", r)

    # 12) validate post-logout (debe quedar inválido/expired)
    print("12) autenticacion.validate_session (post-logout)")
    r = req(sock, "Autenticacion", {"action":"validate_session","token":admin_token,"session_id":admin_token})
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

    # 3) verify_admin (debe ser falso)
    print("3) verify_admin(alice)")
    r = req(sock, "Administracion", {"action":"verify_admin","username":"alice"})
    print("   →", r)

    # 4) getConversation con admin1
    print("4) mensajeria.getConversation(alice, admin1)")
    r = req(sock, "Mensajeria", {"action":"getConversation","user1":"alice","user2":"admin1","limit":20})
    print("   →", r)

    # 5) Historial de llamadas
    print("5) registro_llamadas.history(alice)")
    r = req(sock, "RegistroLlamadas", {"action":"history","userId":"alice","limit":20})
    print("   →", r)

    # 6) validar sesión user
    print("6) autenticacion.validate_session")
    r = req(sock, "Autenticacion", {"action":"validate_session","session_id":alice_token})
    print("   →", r)
def scenario_contacts_import_export(sock):
    print("9) contactos.export (xlsx)")
    r = req(sock, "Contactos", {"action":"export","format":"xlsx"})
    print("   →", r)

    # Si quieres probar import con CSV (sin dependencias):
    print("10) contactos.import (csv->xlsx en app; aquí mandamos CSV BASE64 si tu servicio lo acepta)")
    csv = "nombre,departamento,telefono\nJuan Pérez,405,+56 9 1234 5678\nAna Soto,104,+56 9 2222 3333"
    b64 = base64.b64encode(csv.encode("utf-8")).decode("ascii")
    # Si tu servicio acepta CSV además de XLSX, agrega un flag, p. ej. format="csv"
    r = req(sock, "Contactos", {"action":"import","fileContent": b64, "format":"csv"})
    print("   →", r)

def main():
    s = connect_as_client()
    try:
        scenario_admin(s)
        #scenario_user(s)
        print("\n✅ E2E finalizado OK (revisa arriba cada respuesta)")
    finally:
        try: s.close()
        except: pass

if __name__ == "__main__":
    main()
