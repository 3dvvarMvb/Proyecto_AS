import socket
import json
import time
from bson import ObjectId

def test_mensajeria_service():
    """
    Script de prueba para el servicio de mensajería.
    """
    print("🧪 Iniciando pruebas del servicio de mensajería...")
    
    # Conectar al BUS
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(('localhost', 5000))
    
    # Registrarse
    register = {
        'type': 'REGISTER',
        'client_id': 'test_client'
    }
    sock.send(json.dumps(register).encode('utf-8'))
    response = sock.recv(1024).decode('utf-8')
    print(f"✅ Registrado: {response}\n")
    
    time.sleep(1)
    
    # ==========================================
    # PRUEBA 1: Enviar mensaje
    # ==========================================
    print("=" * 60)
    print("PRUEBA 1: Enviar mensaje")
    print("=" * 60)
    
    send_request = {
        'type': 'REQUEST',
        'target': 'mensajeria_service',
        'payload': {
            'operation': 'send',
            'senderId': '670868b75dd12e6a04b89cbb',  # Reemplazar con ID real
            'receiverId': '670868b75dd12e6a04b89cbc',  # Reemplazar con ID real
            'message': 'Hola! Este es un mensaje de prueba cifrado'
        }
    }
    
    sock.send(json.dumps(send_request).encode('utf-8'))
    print("📤 Mensaje de prueba enviado")
    
    # Esperar respuesta
    response = sock.recv(4096).decode('utf-8')
    resp_data = json.loads(response)
    print(f"📥 Respuesta recibida:")
    print(json.dumps(resp_data, indent=2))
    
    time.sleep(2)
    
    # ==========================================
    # PRUEBA 2: Obtener conversación
    # ==========================================
    print("\n" + "=" * 60)
    print("PRUEBA 2: Obtener conversación")
    print("=" * 60)
    
    get_conv_request = {
        'type': 'REQUEST',
        'target': 'mensajeria_service',
        'payload': {
            'operation': 'getConversation',
            'user1': '670868b75dd12e6a04b89cbb',  # Reemplazar con ID real
            'user2': '670868b75dd12e6a04b89cbc',  # Reemplazar con ID real
            'limit': 10
        }
    }
    
    sock.send(json.dumps(get_conv_request).encode('utf-8'))
    print("📤 Solicitud de conversación enviada")
    
    # Esperar respuesta
    response = sock.recv(8192).decode('utf-8')
    resp_data = json.loads(response)
    print(f"📥 Conversación recibida:")
    print(json.dumps(resp_data, indent=2))
    
    print("\n✅ Pruebas completadas")
    sock.close()

if __name__ == "__main__":
    test_mensajeria_service()