import socket
import json
import threading
import logging
import time

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [ADMINISTRACIÓN] - %(levelname)s - %(message)s'
)

class AdministracionService:
    """
    Servicio de Administración que se conecta al Bus de Mensajes.
    Maneja la gestión de usuarios, configuraciones y supervisión del sistema.
    """
    
    def __init__(self, client_id: str = "administracion_service", bus_host: str = 'localhost', bus_port: int = 5000):
        """
        Inicializa el servicio de administración.
        
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
            # Responder a requests de administración
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
                'status': 'admin_ok',
                'message': 'Operación procesada por administración',
                'user_data': {'name': 'Usuario Test', 'role': 'admin'}
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
        
        time.sleep(5)
        
        # Prueba 1: Broadcast de estado del sistema
        logging.info("🧪 Prueba #1: Enviando broadcast de estado del sistema...")
        self.send_broadcast({
            'test': 'Administración activa',
            'message': 'Sistema de administración supervisando',
            'system_status': 'healthy',
            'timestamp': time.time()
        })
        
        time.sleep(3)
        
        # Prueba 2: Request a Autenticación
        logging.info("🧪 Prueba #2: Enviando request a autenticacion_service...")
        self.send_request('autenticacion_service', {
            'action': 'verify_admin',
            'admin_id': 'admin_001'
        })
        
        time.sleep(3)
        
        # Prueba 3: Mensaje directo a Llamadas
        logging.info("🧪 Prueba #3: Enviando mensaje directo a llamadas_service...")
        self.send_direct('llamadas_service', {
            'action': 'get_call_stats',
            'period': 'today'
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
    Función principal para ejecutar el servicio de administración.
    """
    administracion = AdministracionService(client_id="administracion_service")
    
    try:
        if administracion.connect():
            logging.info("🚀 Servicio de Administración iniciado correctamente")
            
            # Ejecutar pruebas de comunicación
            administracion.test_communication()
            
            # Mantener el servicio activo
            logging.info("⏳ Servicio activo. Presiona Ctrl+C para detener...")
            while administracion.connected:
                time.sleep(1)
        else:
            logging.error("❌ No se pudo iniciar el servicio")
            
    except KeyboardInterrupt:
        logging.info("\n⏹️ Deteniendo servicio...")
    finally:
        administracion.disconnect()


if __name__ == "__main__":
    main()