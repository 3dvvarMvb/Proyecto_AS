import socket
import json
import time
import uuid
import threading
import sys
from datetime import datetime
from bson.objectid import ObjectId

def send_jsonline(sock: socket.socket, obj: dict):
    """Envía un mensaje JSON terminado en newline"""
    sock.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

def recv_jsonline(sock: socket.socket, timeout=5.0) -> dict:
    """Recibe una línea JSON del socket"""
    sock.settimeout(timeout)
    buf = ""
    while "\n" not in buf:
        chunk = sock.recv(4096).decode("utf-8")
        if not chunk:
            raise RuntimeError("Socket cerrado")
        buf += chunk
    line, _ = buf.split("\n", 1)
    return json.loads(line.strip())

class InteractiveChatClient:
    def __init__(self, user_id: str, username: str, bus_host="localhost", bus_port=5000):
        self.bus_host = bus_host
        self.bus_port = bus_port
        self.socket = None
        self.user_id = user_id
        self.username = username
        self.client_id = f"chat_{uuid.uuid4().hex[:8]}"
        self.running = False
        self.events = []
        self.response_queue = []
        self.queue_lock = threading.Lock()
        self.conversation_history = []  # Historial de mensajes
        self.other_user_id = None
        self.other_username = None
        self.is_other_typing = False
        
    def connect(self):
        """Conecta al BUS y se registra como cliente"""
        print(f"\n🔌 Conectando al BUS en {self.bus_host}:{self.bus_port}...")
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self.bus_host, self.bus_port))
        
        # Registro en el BUS
        send_jsonline(self.socket, {
            "type": "REGISTER",
            "kind": "client",
            "client_id": self.client_id
        })
        
        ack = recv_jsonline(self.socket)
        if ack.get("type") == "REGISTER_ACK" and ack.get("status") == "success":
            print(f"✅ Registrado como {self.username} ({self.user_id[:8]}...)")
            
            # Iniciar listener de eventos
            self.running = True
            threading.Thread(target=self._listen_events, daemon=True).start()
            time.sleep(0.5)
            
            # Conectar al servicio
            try:
                self._send_action("connect", {"userId": self.user_id}, optional=True)
            except:
                pass
            
            return True
        else:
            print(f"❌ Error en registro: {ack}")
            return False
    
    def _listen_events(self):
        """Escucha eventos del servidor en tiempo real"""
        buf = ""
        while self.running:
            try:
                self.socket.settimeout(1.0)
                chunk = self.socket.recv(4096).decode("utf-8")
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    msg = json.loads(line)
                    
                    msg_type = msg.get("type")
                    
                    if msg_type == "DELIVERY_ACK":
                        continue
                    
                    if msg_type == "DIRECT":
                        payload = msg.get("payload", {})
                        
                        if "event" in payload:
                            event = payload["event"]
                            data = payload.get("data", {})
                            self.events.append({"event": event, "data": data})
                            self._handle_event(event, data)
                        else:
                            with self.queue_lock:
                                self.response_queue.append(msg)
                        
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"\n⚠️ Error en listener: {e}")
                break
    
    def _handle_event(self, event: str, data: dict):
        """Maneja eventos recibidos en tiempo real"""
        if event == "new_message":
            msg_from = data.get('from', '')
            msg_text = data.get('text', '')
            timestamp = data.get('timestamp', '')
            
            # Guardar en historial
            self.conversation_history.append({
                'from': msg_from,
                'text': msg_text,
                'timestamp': timestamp,
                'direction': 'received'
            })
            
            # Mostrar en consola
            print(f"\n💬 {self.other_username or msg_from[:8]}: {msg_text}")
            print(f"{'✍️  ' + self.other_username + ' está escribiendo...' if self.is_other_typing else ''}> ", end='', flush=True)
            
        elif event == "user_typing":
            user_typing = data.get('user_id', '')
            is_typing = data.get('is_typing', False)
            
            if user_typing == self.other_user_id:
                self.is_other_typing = is_typing
                if is_typing:
                    print(f"\n✍️  {self.other_username or user_typing[:8]} está escribiendo...")
                    print("> ", end='', flush=True)
                else:
                    # Limpiar indicador
                    pass
                    
        elif event == "message_read":
            msg_id = data.get('messageId', '')
            print(f"\n✓✓ Mensaje leído")
            print("> ", end='', flush=True)
    
    def _wait_for_response(self, correlation_id: str, timeout: float = 5.0) -> dict:
        """Espera la respuesta correcta de la cola"""
        start = time.time()
        
        while time.time() - start < timeout:
            with self.queue_lock:
                for i, msg in enumerate(self.response_queue):
                    header = msg.get("header", {})
                    if header.get("correlationId") == correlation_id:
                        self.response_queue.pop(i)
                        return msg
            time.sleep(0.1)
        
        raise TimeoutError("No se recibió respuesta en el tiempo esperado")
    
    def _send_action(self, action: str, payload: dict, optional: bool = False) -> dict:
        """Envía una acción y espera respuesta"""
        correlation_id = str(uuid.uuid4())
        
        request = {
            "type": "REQUEST",
            "service": "Mensajeria",
            "sender": self.client_id,
            "header": {"correlationId": correlation_id, "service": "Mensajeria"},
            "payload": {"action": action, **payload}
        }
        
        send_jsonline(self.socket, request)
        
        try:
            response = self._wait_for_response(correlation_id, timeout=5.0)
            return response.get("payload", {})
        except TimeoutError:
            if optional:
                return {"ok": False, "error": "timeout"}
            raise
    
    def send_message(self, receiver_id: str, message: str) -> bool:
        """Envía un mensaje"""
        try:
            payload = self._send_action("send", {
                "senderObjId": self.user_id,
                "receiverObjId": receiver_id,
                "message": message
            })
            
            if payload.get("ok"):
                # Guardar en historial
                self.conversation_history.append({
                    'from': self.user_id,
                    'text': message,
                    'timestamp': payload.get('timestamp', ''),
                    'direction': 'sent'
                })
                return True
            else:
                print(f"\n❌ Error: {payload.get('error')}")
                return False
        except Exception as e:
            print(f"\n❌ Excepción: {e}")
            return False
    
    def get_conversation(self, other_user_id: str):
        """Obtiene conversación"""
        try:
            payload = self._send_action("getConversation", {
                "user1ObjId": self.user_id,
                "user2ObjId": other_user_id,
                "limit": 50
            })
            
            if payload.get("ok"):
                messages = payload.get("messages", [])
                # Cargar historial
                for msg in messages:
                    self.conversation_history.append({
                        'from': msg.get('from'),
                        'text': msg.get('text'),
                        'timestamp': msg.get('ts'),
                        'direction': 'sent' if msg.get('from') == self.user_id else 'received'
                    })
                return messages
            else:
                return []
        except Exception as e:
            print(f"\n❌ Error obteniendo conversación: {e}")
            return []
    
    def set_typing(self, conversation_id: str, is_typing: bool):
        """Indica si está escribiendo"""
        try:
            self._send_action("typing", {
                "userId": self.user_id,
                "conversationId": conversation_id,
                "isTyping": is_typing
            }, optional=True)
        except:
            pass
    
    def show_conversation_summary(self):
        """Muestra el resumen de la conversación al finalizar"""
        print("\n" + "=" * 80)
        print(f"📜 HISTORIAL DE CONVERSACIÓN - {self.username}")
        print("=" * 80)
        
        if not self.conversation_history:
            print("No hay mensajes en el historial")
        else:
            for i, msg in enumerate(self.conversation_history, 1):
                direction = "➡️ " if msg['direction'] == 'sent' else "⬅️ "
                sender = self.username if msg['direction'] == 'sent' else (self.other_username or msg['from'][:8])
                timestamp = msg.get('timestamp', '')[:19]  # Solo fecha y hora
                print(f"{i}. {direction}[{sender}] ({timestamp})")
                print(f"   {msg['text']}")
                print()
        
        print("=" * 80)
    
    def heartbeat(self):
        """Envía heartbeat"""
        try:
            self._send_action("heartbeat", {"userId": self.user_id}, optional=True)
        except:
            pass
    
    def disconnect(self):
        """Desconecta del servicio"""
        self.running = False
        if self.socket:
            try:
                self._send_action("disconnect", {"userId": self.user_id}, optional=True)
                time.sleep(0.2)
            except:
                pass
            self.socket.close()

def main():
    print("=" * 80)
    print("💬 CHAT INTERACTIVO - Servicio de Mensajería")
    print("=" * 80)
    
    # Pedir datos del usuario
    username = input("\n👤 Tu nombre de usuario: ").strip()
    if not username:
        username = f"Usuario_{uuid.uuid4().hex[:4]}"
    
    # Pedir o generar user_id
    user_id_input = input("🆔 Tu User ID (ObjectId, Enter para generar uno nuevo): ").strip()
    if user_id_input:
        try:
            user_id = str(ObjectId(user_id_input))
        except:
            print("⚠️ ID inválido, generando uno nuevo...")
            user_id = str(ObjectId())
    else:
        user_id = str(ObjectId())
    
    print(f"\n✅ Tu User ID: {user_id}")
    print("📋 Comparte este ID con la otra persona para chatear")
    
    # Pedir el ID del otro usuario
    other_user_id = input("\n👥 User ID del destinatario: ").strip()
    if not other_user_id:
        print("❌ Necesitas el ID del otro usuario para chatear")
        return
    
    other_username = input("👥 Nombre del destinatario (opcional): ").strip()
    
    # Crear cliente
    client = InteractiveChatClient(user_id, username)
    client.other_user_id = other_user_id
    client.other_username = other_username or f"Usuario_{other_user_id[:8]}"
    
    # Conectar
    if not client.connect():
        print("❌ No se pudo conectar al servidor")
        return
    
    # Cargar conversación existente
    print("\n📥 Cargando historial de conversación...")
    client.get_conversation(other_user_id)
    
    if client.conversation_history:
        print(f"\n📜 Últimos mensajes:")
        for msg in client.conversation_history[-5:]:
            direction = "Tú" if msg['direction'] == 'sent' else client.other_username
            print(f"  {direction}: {msg['text']}")
    
    print("\n" + "=" * 80)
    print(f"💬 Chat con {client.other_username}")
    print("=" * 80)
    print("Escribe tus mensajes y presiona Enter para enviar")
    print("Ctrl+C para salir y ver el historial completo")
    print("-" * 80)
    
    # Thread para heartbeat
    def heartbeat_loop():
        while client.running:
            time.sleep(30)
            client.heartbeat()
    
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    
    # Variables para typing indicator
    conv_id = f"{user_id}_{other_user_id}"
    typing_timer = None
    is_typing_sent = False
    
    def stop_typing():
        nonlocal is_typing_sent
        if is_typing_sent:
            client.set_typing(conv_id, False)
            is_typing_sent = False
    
    try:
        while True:
            # Mostrar prompt
            prompt = f"{'✍️  ' + client.other_username + ' está escribiendo...' if client.is_other_typing else ''}\n> " if client.is_other_typing else "> "
            message = input(prompt).strip()
            
            if not message:
                continue
            
            # Cancelar timer de typing si existe
            if typing_timer:
                typing_timer.cancel()
            
            # Detener indicador de typing
            stop_typing()
            
            # Enviar mensaje
            if client.send_message(other_user_id, message):
                print(f"✓ Enviado")
            else:
                print(f"✗ Error al enviar")
            
    except KeyboardInterrupt:
        print("\n\n👋 Cerrando chat...")
        stop_typing()
        client.show_conversation_summary()
        client.disconnect()
        print("\n✅ Desconectado. ¡Hasta pronto!")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        client.disconnect()

if __name__ == "__main__":
    main()