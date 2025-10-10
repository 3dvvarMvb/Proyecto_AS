import socket
import json
import threading
import logging
import time

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [MENSAJERÍA] - %(levelname)s - %(message)s'
)

class MensajeriaService:
    """
    Servicio de Mensajería que se conecta al Bus de Mensajes.
    Maneja el envío y recepción de mensajes entre usuarios.
    """
    
    def __init__(self, client_id: str = "mensajeria_service", bus_host: str = 'localhost', bus_port: int = 5000):
        """
        Inicializa el servicio de mensajería.
        
        Args:
            client_id (str): Identificador único del servicio
            bus_host (str): Dirección del servidor BUS
            bus_port (int): Puerto del servidor BUS
        """
        self.client_id = client_id
        self.bus_host = bus_host
        self.bus_port = bus_port
        self.socket = None
        self.connected = False
        self.running = False
        
    def connect(self):
        """
        Establece conexión con el BUS y registra el servicio.
        
        Returns:
            bool: True si la conexión fue exitosa, False en caso contrario
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
                logging.info(f"✅ Registrado exitosamente en el BUS: {ack.get('message')}")
                
                # Iniciar hilo para escuchar mensajes del BUS
                self.running = True
                listener_thread = threading.Thread(target=self._listen_messages)
                listener_thread.daemon = True
                listener_thread.start()
                
                return True
            else:
                logging.error(f"❌ Error en registro: {ack}")
                return False
                
        except ConnectionRefusedError:
            logging.error("❌ No se pudo conectar al BUS. ¿Está el servidor ejecutándose?")
            return False
        except Exception as e:
            logging.error(f"❌ Error conectando al BUS: {e}")
            return False
    
    def _listen_messages(self):
        """
        Escucha continuamente mensajes provenientes del BUS.
        Ejecuta en un hilo separado para no bloquear el programa.
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
                self._handle_message(message)
                
            except json.JSONDecodeError as e:
                logging.error(f"Error decodificando mensaje: {e}")
            except Exception as e:
                if self.running:
                    logging.error(f"Error recibiendo mensaje: {e}")
                    self.connected = False
                break
        
        logging.info("🔇 Listener detenido")
    
    def _handle_message(self, message: dict):
        """
        Procesa mensajes recibidos del BUS.
        
        Args:
            message (dict): Mensaje recibido en formato JSON
        """
        msg_type = message.get('type')
        sender = message.get('sender', 'UNKNOWN')
        
        logging.info(f"📩 Mensaje recibido - Tipo: {msg_type}, De: {sender}")
        logging.debug(f"Contenido: {message}")
        
        # Manejar diferentes tipos de mensajes
        if msg_type == 'BROADCAST':
            payload = message.get('payload', {})
            logging.info(f"📣 Broadcast de {sender}: {payload}")
            
        elif msg_type == 'DIRECT':
            payload = message.get('payload', {})
            logging.info(f"📧 Mensaje directo de {sender}: {payload}")
            
        elif msg_type == 'REQUEST':
            payload = message.get('payload', {})
            logging.info(f"🔔 Request de {sender}: {payload}")
            # Responder a requests de mensajería
            self._send_response(sender, payload)
            
        elif msg_type == 'DELIVERY_ACK':
            target = message.get('target')
            logging.info(f"✅ Mensaje entregado a {target}")
            
        elif msg_type == 'ERROR':
            error_msg = message.get('message')
            logging.error(f"❌ Error del BUS: {error_msg}")
    
    def _send_response(self, target: str, original_payload: dict):
        """
        Envía una respuesta a un REQUEST recibido.
        
        Args:
            target (str): Cliente que envió el request
            original_payload (dict): Payload del mensaje original
        """
        response = {
            'type': 'DIRECT',
            'target': target,
            'payload': {
                'response_to': original_payload,
                'status': 'message_sent',
                'message': 'Mensaje procesado por servicio de mensajería'
            }
        }
        self.send_message(response)
    
    def send_message(self, message: dict):
        """
        Envía un mensaje al BUS.
        
        Args:
            message (dict): Mensaje a enviar
        """
        if not self.connected:
            logging.error("❌ No conectado al BUS. No se puede enviar mensaje.")
            return False
        
        try:
            data = json.dumps(message).encode('utf-8')
            self.socket.send(data)
            logging.info(f"📤 Mensaje enviado - Tipo: {message.get('type')}")
            return True
        except Exception as e:
            logging.error(f"❌ Error enviando mensaje: {e}")
            self.connected = False
            return False
    
    def send_broadcast(self, payload: dict):
        """
        Envía un mensaje broadcast a todos los servicios conectados.
        
        Args:
            payload (dict): Datos a enviar en el broadcast
        """
        message = {
            'type': 'BROADCAST',
            'payload': payload
        }
        return self.send_message(message)
    
    def send_direct(self, target: str, payload: dict):
        """
        Envía un mensaje directo a un servicio específico.
        
        Args:
            target (str): ID del servicio destinatario
            payload (dict): Datos a enviar
        """
        message = {
            'type': 'DIRECT',
            'target': target,
            'payload': payload
        }
        return self.send_message(message)
    
    def send_request(self, target: str, payload: dict):
        """
        Envía una solicitud a otro servicio esperando respuesta.
        
        Args:
            target (str): ID del servicio destinatario
            payload (dict): Datos de la solicitud
        """
        message = {
            'type': 'REQUEST',
            'target': target,
            'payload': payload
        }
        return self.send_message(message)
    
    def test_communication(self):
        """
        Función de prueba para verificar comunicación con otros servicios.
        """
        logging.info("🧪 Iniciando pruebas de comunicación...")
        
        time.sleep(4)
        
        # Prueba 1: Broadcast
        logging.info("🧪 Prueba #1: Enviando broadcast...")
        self.send_broadcast({
            'test': 'Mensajería activa',
            'message': 'Sistema de mensajería operativo',
            'timestamp': time.time()
        })
        
        time.sleep(3)
        
        # Prueba 2: Request a Administración
        logging.info("🧪 Prueba #2: Enviando request a administracion_service...")
        self.send_request('administracion_service', {
            'action': 'get_user_info',
            'user_id': 'user_002'
        })
        
        time.sleep(3)
        
        # Prueba 3: Mensaje directo a Registro de Llamadas
        logging.info("🧪 Prueba #3: Enviando mensaje directo a registro_llamadas_service...")
        self.send_direct('registro_llamadas_service', {
            'action': 'log_message',
            'message_id': 'msg_12345'
        })
        
        logging.info("✅ Pruebas completadas")
    
    def disconnect(self):
        """
        Cierra la conexión con el BUS de forma ordenada.
        """
        logging.info("🔌 Desconectando del BUS...")
        self.running = False
        self.connected = False
        
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
        
        logging.info("👋 Desconectado del BUS")


def main():
    """
    Función principal para ejecutar el servicio de mensajería.
    """
    mensajeria = MensajeriaService(client_id="mensajeria_service")
    
    try:
        if mensajeria.connect():
            logging.info("🚀 Servicio de Mensajería iniciado correctamente")
            
            # Ejecutar pruebas de comunicación
            mensajeria.test_communication()
            
            # Mantener el servicio activo
            logging.info("⏳ Servicio activo. Presiona Ctrl+C para detener...")
            while mensajeria.connected:
                time.sleep(1)
        else:
            logging.error("❌ No se pudo iniciar el servicio")
            
    except KeyboardInterrupt:
        logging.info("\n⏹️ Deteniendo servicio...")
    finally:
        mensajeria.disconnect()


if __name__ == "__main__":
    main()