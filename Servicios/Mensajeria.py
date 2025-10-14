import socket
import json
import threading
import logging
import time
import os
import base64
from datetime import datetime
from typing import Dict, List, Optional, Any
from queue import Queue, Empty
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2
from cryptography.hazmat.backends import default_backend

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [MENSAJERÍA] - %(levelname)s - %(message)s'
)

class MessageEncryption:
    """
    Maneja el cifrado y descifrado de mensajes usando AES-256-GCM.
    """
    
    def __init__(self, master_key: str = "default_master_key_change_in_production"):
        """
        Inicializa el sistema de cifrado.
        
        Args:
            master_key (str): Clave maestra para derivar claves de cifrado
        """
        self.master_key = master_key.encode('utf-8')
        
    def _derive_key(self, salt: bytes) -> bytes:
        """
        Deriva una clave de 256 bits desde la clave maestra.
        
        Args:
            salt (bytes): Salt para la derivación de clave
            
        Returns:
            bytes: Clave derivada de 32 bytes (256 bits)
        """
        kdf = PBKDF2(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
            backend=default_backend()
        )
        return kdf.derive(self.master_key)
    
    def encrypt(self, plaintext: str) -> Dict[str, str]:
        """
        Cifra un mensaje usando AES-256-GCM.
        
        Args:
            plaintext (str): Mensaje en texto plano
            
        Returns:
            dict: Diccionario con ciphertext (base64), iv (base64) y alg
        """
        try:
            # Generar salt aleatorio
            salt = os.urandom(16)
            
            # Derivar clave
            key = self._derive_key(salt)
            
            # Crear instancia de AES-GCM
            aesgcm = AESGCM(key)
            
            # Generar IV aleatorio (12 bytes para GCM)
            iv = os.urandom(12)
            
            # Cifrar el mensaje
            plaintext_bytes = plaintext.encode('utf-8')
            ciphertext = aesgcm.encrypt(iv, plaintext_bytes, None)
            
            # Codificar en base64 para almacenamiento
            ciphertext_b64 = base64.b64encode(ciphertext).decode('utf-8')
            iv_b64 = base64.b64encode(iv + salt).decode('utf-8')  # Concatenar IV y salt
            
            return {
                'ciphertext': ciphertext_b64,
                'iv': iv_b64,
                'alg': 'AES-256-GCM'
            }
            
        except Exception as e:
            logging.error(f"Error cifrando mensaje: {e}")
            raise
    
    def decrypt(self, ciphertext_b64: str, iv_b64: str, alg: str) -> str:
        """
        Descifra un mensaje cifrado con AES-256-GCM.
        
        Args:
            ciphertext_b64 (str): Texto cifrado en base64
            iv_b64 (str): IV y salt concatenados en base64
            alg (str): Algoritmo usado (debe ser AES-256-GCM)
            
        Returns:
            str: Mensaje descifrado en texto plano
        """
        try:
            if alg != 'AES-256-GCM':
                raise ValueError(f"Algoritmo no soportado: {alg}")
            
            # Decodificar desde base64
            ciphertext = base64.b64decode(ciphertext_b64)
            iv_salt = base64.b64decode(iv_b64)
            
            # Separar IV (12 bytes) y salt (16 bytes)
            iv = iv_salt[:12]
            salt = iv_salt[12:]
            
            # Derivar clave
            key = self._derive_key(salt)
            
            # Crear instancia de AES-GCM
            aesgcm = AESGCM(key)
            
            # Descifrar
            plaintext_bytes = aesgcm.decrypt(iv, ciphertext, None)
            plaintext = plaintext_bytes.decode('utf-8')
            
            return plaintext
            
        except Exception as e:
            logging.error(f"Error descifrando mensaje: {e}")
            raise


class MessageQueue:
    """
    Cola de mensajes thread-safe para procesar mensajes de forma asíncrona.
    """
    
    def __init__(self, max_size: int = 1000):
        """
        Inicializa la cola de mensajes.
        
        Args:
            max_size (int): Tamaño máximo de la cola
        """
        self.queue = Queue(maxsize=max_size)
        self.processing = False
        self.stats = {
            'enqueued': 0,
            'processed': 0,
            'failed': 0
        }
        self.stats_lock = threading.Lock()
    
    def enqueue(self, message: Dict[str, Any]) -> bool:
        """
        Agrega un mensaje a la cola.
        
        Args:
            message (dict): Mensaje a encolar
            
        Returns:
            bool: True si se encoló exitosamente
        """
        try:
            self.queue.put(message, block=False)
            with self.stats_lock:
                self.stats['enqueued'] += 1
            logging.info(f"📥 Mensaje encolado. Cola actual: {self.queue.qsize()}")
            return True
        except Exception as e:
            logging.error(f"Error encolando mensaje: {e}")
            return False
    
    def dequeue(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        """
        Obtiene el siguiente mensaje de la cola.
        
        Args:
            timeout (float): Tiempo máximo de espera en segundos
            
        Returns:
            dict o None: Mensaje o None si la cola está vacía
        """
        try:
            message = self.queue.get(timeout=timeout)
            return message
        except Empty:
            return None
    
    def mark_processed(self, success: bool = True):
        """
        Marca un mensaje como procesado.
        
        Args:
            success (bool): True si se procesó exitosamente
        """
        with self.stats_lock:
            if success:
                self.stats['processed'] += 1
            else:
                self.stats['failed'] += 1
        self.queue.task_done()
    
    def is_empty(self) -> bool:
        """Verifica si la cola está vacía."""
        return self.queue.empty()
    
    def size(self) -> int:
        """Retorna el tamaño actual de la cola."""
        return self.queue.qsize()
    
    def get_stats(self) -> Dict[str, int]:
        """Retorna estadísticas de la cola."""
        with self.stats_lock:
            return self.stats.copy()


class MensajeriaService:
    """
    Servicio de Mensajería que gestiona el envío, recepción y almacenamiento
    de mensajes cifrados.
    """
    
    def __init__(
        self, 
        client_id: str = "mensajeria_service",
        bus_host: str = None,
        bus_port: int = 5000,
        mongo_uri: str = None
    ):
        """
        Inicializa el servicio de mensajería.
        
        Args:
            client_id (str): Identificador único del servicio
            bus_host (str): Dirección del servidor BUS
            bus_port (int): Puerto del servidor BUS
            mongo_uri (str): URI de conexión a MongoDB
        """
        self.client_id = client_id
        self.bus_host = bus_host or os.getenv('BUS_HOST', 'localhost')
        self.bus_port = bus_port
        self.mongo_uri = mongo_uri or os.getenv('MONGO_URI', 
            'mongodb://app_user:app_password_123@localhost:27017/arquitectura_software?authSource=admin')
        
        # Componentes del servicio
        self.socket = None
        self.connected = False
        self.running = False
        
        # MongoDB
        self.mongo_client = None
        self.db = None
        self.messages_collection = None
        
        # Cifrado
        self.encryption = MessageEncryption()
        
        # Cola de mensajes
        self.message_queue = MessageQueue()
        
        # Threads
        self.listener_thread = None
        self.queue_processor_thread = None
        
    def connect_mongodb(self) -> bool:
        """
        Conecta a MongoDB y configura las colecciones.
        
        Returns:
            bool: True si la conexión fue exitosa
        """
        try:
            logging.info("🔗 Conectando a MongoDB...")
            self.mongo_client = MongoClient(self.mongo_uri)
            self.db = self.mongo_client['arquitectura_software']
            self.messages_collection = self.db['mensajes']
            
            # Verificar conexión
            self.mongo_client.admin.command('ping')
            logging.info("✅ Conexión a MongoDB establecida")
            return True
            
        except Exception as e:
            logging.error(f"❌ Error conectando a MongoDB: {e}")
            return False
    
    def connect_bus(self) -> bool:
        """
        Establece conexión con el BUS y registra el servicio.
        
        Returns:
            bool: True si la conexión fue exitosa
        """
        try:
            # Crear socket TCP/IP
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.connect((self.bus_host, self.bus_port))
            
            logging.info(f"🔌 Conectando al BUS en {self.bus_host}:{self.bus_port}")
            
            # Enviar mensaje de registro
            register_message = {
                'type': 'REGISTER',
                'client_id': self.client_id
            }
            self.socket.send(json.dumps(register_message).encode('utf-8'))
            
            # Esperar confirmación
            response = self.socket.recv(1024).decode('utf-8')
            ack = json.loads(response)
            
            if ack.get('type') == 'REGISTER_ACK' and ack.get('status') == 'success':
                self.connected = True
                logging.info(f"✅ Registrado exitosamente en el BUS")
                return True
            else:
                logging.error(f"❌ Error en registro: {ack}")
                return False
                
        except Exception as e:
            logging.error(f"❌ Error conectando al BUS: {e}")
            return False
    
    def start(self) -> bool:
        """
        Inicia el servicio de mensajería.
        
        Returns:
            bool: True si el servicio se inició correctamente
        """
        # Conectar a MongoDB
        if not self.connect_mongodb():
            return False
        
        # Conectar al BUS
        if not self.connect_bus():
            return False
        
        self.running = True
        
        # Iniciar thread de escucha del BUS
        self.listener_thread = threading.Thread(target=self._listen_bus_messages)
        self.listener_thread.daemon = True
        self.listener_thread.start()
        
        # Iniciar thread de procesamiento de cola
        self.queue_processor_thread = threading.Thread(target=self._process_message_queue)
        self.queue_processor_thread.daemon = True
        self.queue_processor_thread.start()
        
        logging.info("🚀 Servicio de Mensajería iniciado correctamente")
        return True
    
    def _listen_bus_messages(self):
        """
        Escucha continuamente mensajes provenientes del BUS.
        Ejecuta en un hilo separado.
        """
        logging.info("👂 Iniciando escucha de mensajes del BUS...")
        
        while self.running and self.connected:
            try:
                # Recibir datos del BUS
                data = self.socket.recv(4096).decode('utf-8')
                
                if not data:
                    logging.warning("⚠️ Conexión cerrada por el BUS")
                    self.connected = False
                    break
                
                # Procesar mensaje recibido
                message = json.loads(data)
                self._handle_bus_message(message)
                
            except json.JSONDecodeError as e:
                logging.error(f"Error decodificando mensaje: {e}")
            except Exception as e:
                if self.running:
                    logging.error(f"Error recibiendo mensaje: {e}")
                    self.connected = False
                break
        
        logging.info("🔇 Listener del BUS detenido")
    
    def _handle_bus_message(self, message: Dict[str, Any]):
        """
        Procesa mensajes recibidos del BUS.
        
        Args:
            message (dict): Mensaje recibido en formato JSON
        """
        msg_type = message.get('type')
        sender = message.get('sender', 'UNKNOWN')
        payload = message.get('payload', {})
        
        logging.info(f"📩 Mensaje del BUS - Tipo: {msg_type}, De: {sender}")
        
        # Enrutar según tipo de mensaje
        if msg_type == 'REQUEST':
            operation = payload.get('operation')
            
            if operation == 'send':
                # Operación de envío de mensaje
                self._handle_send_message(sender, payload)
                
            elif operation == 'getConversation':
                # Operación de obtención de conversación
                self._handle_get_conversation(sender, payload)
                
            else:
                logging.warning(f"Operación desconocida: {operation}")
                
        elif msg_type == 'DIRECT':
            # Mensaje directo
            logging.info(f"📧 Mensaje directo de {sender}: {payload}")
            
        elif msg_type == 'BROADCAST':
            # Broadcast
            logging.info(f"📣 Broadcast de {sender}: {payload}")
            
        elif msg_type == 'DELIVERY_ACK':
            target = message.get('target')
            logging.info(f"✅ Mensaje entregado a {target}")
            
        elif msg_type == 'ERROR':
            error_msg = message.get('message')
            logging.error(f"❌ Error del BUS: {error_msg}")
    
    def _handle_send_message(self, sender: str, payload: Dict[str, Any]):
        """
        Maneja la operación de envío de mensaje.
        Valida, cifra y encola el mensaje.
        
        Args:
            sender (str): ID del cliente que envía el request
            payload (dict): Datos del mensaje
        """
        try:
            # Validar estructura del mensaje
            sender_id = payload.get('senderId')
            receiver_id = payload.get('receiverId')
            message_text = payload.get('message')
            
            if not all([sender_id, receiver_id, message_text]):
                self._send_error_response(sender, "Faltan campos requeridos: senderId, receiverId, message")
                return
            
            # Validar que los ObjectIds sean válidos
            try:
                sender_oid = ObjectId(sender_id)
                receiver_oid = ObjectId(receiver_id)
            except Exception:
                self._send_error_response(sender, "IDs de usuario inválidos")
                return
            
            # Cifrar el mensaje
            encrypted = self.encryption.encrypt(message_text)
            
            # Crear documento para MongoDB
            message_doc = {
                'timestamp': datetime.utcnow(),
                'hora': datetime.utcnow().strftime('%H:%M'),
                'sender': sender_oid,
                'receiver': receiver_oid,
                'ciphertext': encrypted['ciphertext'],
                'iv': encrypted['iv'],
                'alg': encrypted['alg'],
                'deliveryStatus': 'enviado'
            }
            
            # Insertar en MongoDB
            result = self.messages_collection.insert_one(message_doc)
            message_id = str(result.inserted_id)
            
            logging.info(f"💾 Mensaje guardado en BD: {message_id}")
            
            # Encolar mensaje para envío al cliente
            queue_message = {
                'messageId': message_id,
                'senderId': sender_id,
                'receiverId': receiver_id,
                'timestamp': message_doc['timestamp'].isoformat(),
                'encrypted': encrypted,
                'bus_sender': sender
            }
            
            if self.message_queue.enqueue(queue_message):
                # Responder al BUS indicando que el mensaje fue recibido
                self._send_success_response(sender, message_id, 'enviado')
            else:
                self._send_error_response(sender, "Error encolando mensaje")
                
        except Exception as e:
            logging.error(f"Error procesando envío de mensaje: {e}")
            self._send_error_response(sender, f"Error interno: {str(e)}")
    
    def _handle_get_conversation(self, sender: str, payload: Dict[str, Any]):
        """
        Maneja la operación de obtención de conversación entre dos usuarios.
        
        Args:
            sender (str): ID del cliente que envía el request
            payload (dict): Datos de la solicitud (user1, user2, limit)
        """
        try:
            user1_id = payload.get('user1')
            user2_id = payload.get('user2')
            limit = payload.get('limit', 50)
            
            if not all([user1_id, user2_id]):
                self._send_error_response(sender, "Faltan campos requeridos: user1, user2")
                return
            
            # Validar ObjectIds
            try:
                user1_oid = ObjectId(user1_id)
                user2_oid = ObjectId(user2_id)
            except Exception:
                self._send_error_response(sender, "IDs de usuario inválidos")
                return
            
            # Buscar mensajes entre ambos usuarios
            messages = self.messages_collection.find({
                '$or': [
                    {'sender': user1_oid, 'receiver': user2_oid},
                    {'sender': user2_oid, 'receiver': user1_oid}
                ]
            }).sort('timestamp', DESCENDING).limit(limit)
            
            # Preparar respuesta
            messages_list = []
            for msg in messages:
                # Descifrar mensaje
                try:
                    decrypted_text = self.encryption.decrypt(
                        msg['ciphertext'],
                        msg['iv'],
                        msg['alg']
                    )
                except Exception as e:
                    logging.error(f"Error descifrando mensaje {msg['_id']}: {e}")
                    decrypted_text = "[Error descifrando mensaje]"
                
                messages_list.append({
                    'messageId': str(msg['_id']),
                    'timestamp': msg['timestamp'].isoformat(),
                    'hora': msg.get('hora', ''),
                    'senderId': str(msg['sender']),
                    'receiverId': str(msg['receiver']),
                    'message': decrypted_text,
                    'deliveryStatus': msg.get('deliveryStatus', 'enviado')
                })
            
            # Invertir para orden cronológico
            messages_list.reverse()
            
            # Verificar si hay más mensajes
            total_count = self.messages_collection.count_documents({
                '$or': [
                    {'sender': user1_oid, 'receiver': user2_oid},
                    {'sender': user2_oid, 'receiver': user1_oid}
                ]
            })
            
            has_more = total_count > limit
            
            # Enviar respuesta
            response = {
                'type': 'DIRECT',
                'target': sender,
                'payload': {
                    'operation': 'getConversation',
                    'status': 'success',
                    'messages': messages_list,
                    'hasMore': has_more,
                    'total': total_count
                }
            }
            
            self._send_to_bus(response)
            logging.info(f"📤 Conversación enviada: {len(messages_list)} mensajes")
            
        except Exception as e:
            logging.error(f"Error obteniendo conversación: {e}")
            self._send_error_response(sender, f"Error interno: {str(e)}")
    
    def _process_message_queue(self):
        """
        Procesa la cola de mensajes de forma continua.
        Envía los mensajes a los clientes destinatarios.
        Ejecuta en un hilo separado.
        """
        logging.info("🔄 Iniciando procesador de cola de mensajes...")
        
        while self.running:
            try:
                # Obtener mensaje de la cola
                message = self.message_queue.dequeue(timeout=1.0)
                
                if message is None:
                    continue
                
                logging.info(f"🔄 Procesando mensaje de la cola: {message.get('messageId')}")
                
                # Simular envío al cliente (en producción aquí irían a un servicio real)
                success = self._deliver_message_to_client(message)
                
                # Marcar como procesado
                self.message_queue.mark_processed(success)
                
                if success:
                    # Actualizar estado en BD
                    self._update_delivery_status(message['messageId'], 'entregado')
                else:
                    logging.warning(f"⚠️ Fallo al entregar mensaje {message['messageId']}")
                
            except Exception as e:
                logging.error(f"Error procesando cola de mensajes: {e}")
                time.sleep(1)
        
        logging.info("🔇 Procesador de cola detenido")
    
    def _deliver_message_to_client(self, message: Dict[str, Any]) -> bool:
        """
        Entrega un mensaje al cliente destinatario.
        NOTA: Esta es una función placeholder. En producción, esto se conectaría
        a un sistema de notificaciones real (WebSocket, Push, etc.)
        
        Args:
            message (dict): Mensaje a entregar
            
        Returns:
            bool: True si se entregó exitosamente
        """
        try:
            logging.info(f"📬 Entregando mensaje a cliente {message['receiverId']}")
            
            # TODO: Implementar entrega real al cliente
            # Por ahora, simulamos una entrega exitosa
            time.sleep(0.1)  # Simular latencia de red
            
            # En producción, aquí se enviaría vía WebSocket, Firebase, etc.
            # Ejemplo: websocket_manager.send_to_user(message['receiverId'], message)
            
            logging.info(f"✅ Mensaje {message['messageId']} entregado")
            return True
            
        except Exception as e:
            logging.error(f"Error entregando mensaje: {e}")
            return False
    
    def _update_delivery_status(self, message_id: str, status: str):
        """
        Actualiza el estado de entrega de un mensaje en MongoDB.
        
        Args:
            message_id (str): ID del mensaje
            status (str): Nuevo estado (enviado, entregado, leido)
        """
        try:
            self.messages_collection.update_one(
                {'_id': ObjectId(message_id)},
                {'$set': {'deliveryStatus': status}}
            )
            logging.info(f"📝 Estado actualizado para mensaje {message_id}: {status}")
        except Exception as e:
            logging.error(f"Error actualizando estado: {e}")
    
    def _send_success_response(self, target: str, message_id: str, delivery_status: str):
        """
        Envía una respuesta exitosa al cliente a través del BUS.
        
        Args:
            target (str): ID del cliente destinatario
            message_id (str): ID del mensaje procesado
            delivery_status (str): Estado de entrega
        """
        response = {
            'type': 'DIRECT',
            'target': target,
            'payload': {
                'operation': 'send',
                'status': 'success',
                'messageId': message_id,
                'deliveryStatus': delivery_status
            }
        }
        self._send_to_bus(response)
    
    def _send_error_response(self, target: str, error_message: str):
        """
        Envía una respuesta de error al cliente a través del BUS.
        
        Args:
            target (str): ID del cliente destinatario
            error_message (str): Mensaje de error
        """
        response = {
            'type': 'DIRECT',
            'target': target,
            'payload': {
                'status': 'error',
                'message': error_message
            }
        }
        self._send_to_bus(response)
    
    def _send_to_bus(self, message: Dict[str, Any]) -> bool:
        """
        Envía un mensaje al BUS.
        
        Args:
            message (dict): Mensaje a enviar
            
        Returns:
            bool: True si se envió exitosamente
        """
        if not self.connected:
            logging.error("❌ No conectado al BUS")
            return False
        
        try:
            data = json.dumps(message).encode('utf-8')
            self.socket.send(data)
            logging.debug(f"📤 Mensaje enviado al BUS - Tipo: {message.get('type')}")
            return True
        except Exception as e:
            logging.error(f"❌ Error enviando mensaje al BUS: {e}")
            self.connected = False
            return False
    
    def get_queue_stats(self) -> Dict[str, Any]:
        """
        Obtiene estadísticas de la cola de mensajes.
        
        Returns:
            dict: Estadísticas de la cola
        """
        stats = self.message_queue.get_stats()
        stats['current_size'] = self.message_queue.size()
        stats['is_empty'] = self.message_queue.is_empty()
        return stats
    
    def stop(self):
        """
        Detiene el servicio de mensajería de forma ordenada.
        """
        logging.info("🛑 Deteniendo servicio de mensajería...")
        self.running = False
        self.connected = False
        
        # Esperar a que los threads terminen
        if self.listener_thread and self.listener_thread.is_alive():
            self.listener_thread.join(timeout=2)
        
        if self.queue_processor_thread and self.queue_processor_thread.is_alive():
            self.queue_processor_thread.join(timeout=2)
        
        # Cerrar conexiones
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
        
        if self.mongo_client:
            try:
                self.mongo_client.close()
            except:
                pass
        
        # Mostrar estadísticas finales
        stats = self.get_queue_stats()
        logging.info(f"📊 Estadísticas finales de la cola: {stats}")
        
        logging.info("👋 Servicio de mensajería detenido")


def main():
    """
    Función principal para ejecutar el servicio de mensajería.
    """
    servicio = MensajeriaService(client_id="mensajeria_service")
    
    try:
        if servicio.start():
            logging.info("⏳ Servicio activo. Presiona Ctrl+C para detener...")
            
            # Mantener el servicio activo
            while servicio.running and servicio.connected:
                time.sleep(1)
                
                # Mostrar estadísticas cada 30 segundos
                if int(time.time()) % 30 == 0:
                    stats = servicio.get_queue_stats()
                    logging.info(f"📊 Stats - Cola: {stats['current_size']} | "
                               f"Procesados: {stats['processed']} | "
                               f"Fallidos: {stats['failed']}")
        else:
            logging.error("❌ No se pudo iniciar el servicio")
            
    except KeyboardInterrupt:
        logging.info("\n⏹️ Deteniendo servicio...")
    finally:
        servicio.stop()


if __name__ == "__main__":
    main()