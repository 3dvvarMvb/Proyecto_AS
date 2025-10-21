import socket
import json
import time
import uuid
import threading
import sys
import os
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
        self.conversation_history = []
        self.other_user_id = None
        self.other_username = None
        self.other_client_id = None
        self.is_other_typing = False
        
        # ✅ Lista de usuarios conectados
        self.online_users = {}  # client_id -> {user_id, username}
        self.users_lock = threading.Lock()
        
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
            print(f"   Client ID: {self.client_id}")
            
            # Iniciar listener de eventos
            self.running = True
            threading.Thread(target=self._listen_events, daemon=True).start()
            time.sleep(0.5)
            
            # ✅ Conectar al servicio de mensajería
            try:
                response = self._send_action("connect", {"userId": self.user_id}, optional=True)
                if response.get("ok"):
                    print(f"✅ Conectado al servicio de mensajería")
            except Exception as e:
                print(f"⚠️ Error conectando al servicio: {e}")
            
            # ✅ Broadcast mi presencia (enviar info de usuario)
            self._broadcast_my_presence()
            
            return True
        else:
            print(f"❌ Error en registro: {ack}")
            return False
    
    def _broadcast_my_presence(self):
        """Envía un broadcast con mi información de usuario"""
        try:
            send_jsonline(self.socket, {
                "type": "BROADCAST",
                "event": "user_presence",
                "data": {
                    "client_id": self.client_id,
                    "user_id": self.user_id,
                    "username": self.username
                }
            })
        except Exception as e:
            print(f"⚠️ Error enviando presencia: {e}")
    
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
                    
                    # ✅ Manejar BROADCAST (usuarios conectados/desconectados)
                    if msg_type == "BROADCAST":
                        self._handle_broadcast(msg)
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
    
    def _handle_broadcast(self, msg: dict):
        """Maneja mensajes de broadcast del BUS"""
        event = msg.get("event")
        
        if event == "user_joined":
            client_id = msg.get("client_id")
            if client_id and client_id != self.client_id:
                # Solicitar información del usuario
                self._broadcast_my_presence()  # Responder con mi info
        
        elif event == "user_left":
            client_id = msg.get("client_id")
            with self.users_lock:
                if client_id in self.online_users:
                    user_info = self.online_users.pop(client_id)
                    # Solo mostrar si no estamos en chat activo
                    if not self.other_client_id:
                        print(f"\n👋 {user_info.get('username', client_id[:8])} se desconectó")
        
        elif event == "user_presence":
            data = msg.get("data", {})
            client_id = data.get("client_id")
            user_id = data.get("user_id")
            username = data.get("username")
            
            if client_id and client_id != self.client_id:
                with self.users_lock:
                    self.online_users[client_id] = {
                        "user_id": user_id,
                        "username": username or f"Usuario_{client_id[:8]}"
                    }
    
    def _handle_event(self, event: str, data: dict):
        """Maneja eventos recibidos en tiempo real"""
        if event == "new_message":
            msg_from = data.get('from', '')
            msg_text = data.get('text', '')
            timestamp = data.get('timestamp', '')
            message_id = data.get('messageId', '')  # ✅ Capturar message_id
            
            # Guardar en historial
            self.conversation_history.append({
                'from': msg_from,
                'text': msg_text,
                'timestamp': timestamp,
                'direction': 'received',
                'message_id': message_id  # ✅ Guardar ID
            })
            
            # Mostrar en consola
            print(f"\n💬 {self.other_username or msg_from[:8]}: {msg_text}")
            print(f"{'✍️  ' + self.other_username + ' está escribiendo...' if self.is_other_typing else ''}> ", end='', flush=True)
            
            # ✅ Marcar como leído automáticamente si estamos en chat activo con ese usuario
            if message_id and msg_from == self.other_user_id:
                # Enviar en un thread para no bloquear
                threading.Thread(
                    target=lambda: self.mark_as_read(message_id), 
                    daemon=True
                ).start()
            
        elif event == "user_typing":
            user_typing = data.get('user_id', '')
            is_typing = data.get('is_typing', False)
            
            if user_typing == self.other_user_id:
                self.is_other_typing = is_typing
                if is_typing:
                    print(f"\n✍️  {self.other_username or user_typing[:8]} está escribiendo...")
                    print("> ", end='', flush=True)
                    
        elif event == "message_read":
            msg_id = data.get('messageId', '')
            # ✅ Actualizar el historial local para marcar el mensaje como leído
            for msg in self.conversation_history:
                if msg.get('message_id') == msg_id and msg.get('direction') == 'sent':
                    msg['read'] = True
                    break
            
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
                    'direction': 'sent',
                    'message_id': payload.get('messageId', '')  # ✅ Guardar ID del mensaje enviado
                })
                return True
            else:
                print(f"\n❌ Error: {payload.get('error')}")
                return False
        except Exception as e:
            print(f"\n❌ Excepción: {e}")
            return False
    
    def get_conversation(self, other_user_id: str):
        """Obtiene conversación y marca mensajes recibidos como leídos"""
        try:
            payload = self._send_action("getConversation", {
                "user1ObjId": self.user_id,
                "user2ObjId": other_user_id,
                "limit": 50
            })
            
            if payload.get("ok"):
                messages = payload.get("messages", [])
                unread_message_ids = []
                
                # Cargar historial
                for msg in messages:
                    self.conversation_history.append({
                        'from': msg.get('from'),
                        'text': msg.get('text'),
                        'timestamp': msg.get('ts'),
                        'direction': 'sent' if msg.get('from') == self.user_id else 'received',
                        'message_id': msg.get('id')
                    })
                    
                    # ✅ Recopilar mensajes no leídos que son para mí
                    if (msg.get('from') == other_user_id and 
                        msg.get('to') == self.user_id and 
                        msg.get('readStatus') == 'no_leido'):
                        unread_message_ids.append(msg.get('id'))
                
                # ✅ Marcar todos los mensajes no leídos en batch
                if unread_message_ids:
                    def mark_all_as_read():
                        for msg_id in unread_message_ids:
                            try:
                                self.mark_as_read(msg_id)
                                time.sleep(0.1)  # Pequeño delay entre peticiones
                            except:
                                pass
                    
                    threading.Thread(target=mark_all_as_read, daemon=True).start()
                
                return messages
            else:
                return []
        except Exception as e:
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
    
    def mark_as_read(self, message_id: str):
        """Marca un mensaje como leído enviando REQUEST al servicio de Mensajería"""
        try:
            response = self._send_action("markRead", {"messageId": message_id}, optional=True)
            if response.get("ok"):
                pass  # Silencioso - el servicio notificará al remitente
        except Exception as e:
            pass  # Silencioso para no interrumpir el flujo
    
    def get_online_users_list(self):
        """Obtiene lista formateada de usuarios online"""
        with self.users_lock:
            return list(self.online_users.items())
    
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
                timestamp = msg.get('timestamp', '')[:19]
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

def clear_screen():
    """Limpia la pantalla de la consola"""
    os.system('cls' if os.name == 'nt' else 'clear')

def show_user_menu(client):
    """Muestra menú de selección de usuarios"""
    while client.running:
        clear_screen()
        print("=" * 80)
        print(f"👤 {client.username} - Selecciona con quién chatear")
        print("=" * 80)
        
        users = client.get_online_users_list()
        
        if not users:
            print("\n⏳ Esperando que otros usuarios se conecten...")
            print("\nActualizando en 5 segundos... (Ctrl+C para salir)")
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                return None
            continue
        
        print("\n📋 Usuarios conectados:\n")
        for idx, (client_id, user_info) in enumerate(users, 1):
            username = user_info.get('username', 'Desconocido')
            user_id = user_info.get('user_id', '')
            print(f"  {idx}. {username} (ID: {user_id[:12]}...)")
        
        print(f"\n  0. Actualizar lista")
        print(f"  Q. Salir")
        print("-" * 80)
        
        try:
            choice = input("\n👉 Selecciona un número: ").strip().lower()
            
            if choice == 'q':
                return None
            
            if choice == '0':
                continue
            
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(users):
                    selected_client_id, selected_user = users[idx]
                    return {
                        'client_id': selected_client_id,
                        'user_id': selected_user.get('user_id'),
                        'username': selected_user.get('username')
                    }
                else:
                    print("❌ Opción inválida")
                    time.sleep(1)
            except ValueError:
                print("❌ Debes ingresar un número")
                time.sleep(1)
                
        except KeyboardInterrupt:
            return None
    
    return None

def chat_loop(client, other_user):
    """Loop principal del chat"""
    client.other_client_id = other_user['client_id']
    client.other_user_id = other_user['user_id']
    client.other_username = other_user['username']
    
    # Limpiar historial previo
    client.conversation_history = []
    
    # Cargar conversación existente
    print("\n📥 Cargando historial de conversación...")
    client.get_conversation(client.other_user_id)
    
    clear_screen()
    print("=" * 80)
    print(f"💬 Chat con {client.other_username}")
    print("=" * 80)
    
    if client.conversation_history:
        print(f"\n📜 Últimos mensajes:")
        for msg in client.conversation_history[-5:]:
            direction = "Tú" if msg['direction'] == 'sent' else client.other_username
            print(f"  {direction}: {msg['text']}")
    
    print("\n" + "-" * 80)
    print("Escribe tus mensajes y presiona Enter para enviar")
    print("Escribe '/menu' para volver al menú de usuarios")
    print("Ctrl+C para salir")
    print("-" * 80 + "\n")
    
    conv_id = f"{client.user_id}_{client.other_user_id}"
    
    try:
        while True:
            prompt = "> "
            message = input(prompt).strip()
            
            if not message:
                continue
            
            # Comando especial para volver al menú
            if message.lower() == '/menu':
                return True  # Volver al menú
            
            # Enviar mensaje
            if client.send_message(client.other_user_id, message):
                print(f"✓ Enviado")
            else:
                print(f"✗ Error al enviar")
    
    except KeyboardInterrupt:
        return False  # Salir completamente

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
    print("⏳ Conectando y descubriendo usuarios...")
    
    # Crear cliente
    client = InteractiveChatClient(user_id, username)
    
    # Conectar
    if not client.connect():
        print("❌ No se pudo conectar al servidor")
        return
    
    # Esperar un poco para recibir broadcasts de otros usuarios
    time.sleep(2)
    
    # Thread para heartbeat
    def heartbeat_loop():
        while client.running:
            time.sleep(30)
            client.heartbeat()
    
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    
    # Thread para broadcast periódico de presencia
    def presence_loop():
        while client.running:
            time.sleep(10)
            client._broadcast_my_presence()
    
    threading.Thread(target=presence_loop, daemon=True).start()
    
    try:
        while True:
            # Mostrar menú de usuarios
            selected_user = show_user_menu(client)
            
            if selected_user is None:
                # Usuario quiere salir
                break
            
            # Entrar al chat
            continue_menu = chat_loop(client, selected_user)
            
            if not continue_menu:
                # Salir completamente
                break
    
    except KeyboardInterrupt:
        pass
    finally:
        print("\n\n👋 Cerrando chat...")
        if client.conversation_history:
            client.show_conversation_summary()
        client.disconnect()
        print("\n✅ Desconectado. ¡Hasta pronto!")

if __name__ == "__main__":
    main()
