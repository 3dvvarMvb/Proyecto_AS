import socket
import threading
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Tuple

# Configuración de logging para rastrear actividad del bus
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class MessageBus:
    """
    Servidor TCP/IP que actúa como bus de mensajes central.
    Permite comunicación bidireccional entre múltiples clientes conectados.
    """
    
    def __init__(self, host: str = 'localhost', port: int = 5000):
        """
        Inicializa el servidor del bus de mensajes.
        
        Args:
            host (str): Dirección IP del servidor (default: localhost)
            port (int): Puerto de escucha del servidor (default: 5000)
        """
        self.host = host
        self.port = port
        self.server_socket = None
        self.clients: Dict[str, socket.socket] = {}  # {client_id: socket}
        self.clients_lock = threading.Lock()  # Para operaciones thread-safe
        self.running = False
        
    def start(self):
        """
        Inicia el servidor TCP/IP y comienza a escuchar conexiones entrantes.
        Crea un socket, lo vincula al host:port y acepta conexiones en un loop.
        """
        try:
            # Crear socket TCP/IP
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            # Vincular socket a dirección y puerto
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(5)  # Máximo 5 conexiones en cola
            
            self.running = True
            logging.info(f"🚌 Bus de mensajes iniciado en {self.host}:{self.port}")
            
            # Loop principal para aceptar conexiones
            while self.running:
                try:
                    client_socket, address = self.server_socket.accept()
                    logging.info(f"📥 Nueva conexión desde {address}")
                    
                    # Crear hilo para manejar el cliente
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client_socket, address)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                except Exception as e:
                    if self.running:
                        logging.error(f"Error aceptando conexión: {e}")
                        
        except Exception as e:
            logging.error(f"Error iniciando servidor: {e}")
        finally:
            self.stop()
    
    def _handle_client(self, client_socket: socket.socket, address: Tuple):
        """
        Maneja la comunicación con un cliente específico.
        Ejecuta en un hilo separado para cada cliente.
        
        Args:
            client_socket: Socket del cliente conectado
            address: Tupla (IP, puerto) del cliente
        """
        client_id = None
        
        try:
            # Recibir identificador del cliente
            data = client_socket.recv(1024).decode('utf-8')
            message = json.loads(data)
            
            if message.get('type') == 'REGISTER':
                client_id = message.get('client_id')
                
                # Registrar cliente
                with self.clients_lock:
                    self.clients[client_id] = client_socket
                
                # Confirmar registro
                response = {
                    'type': 'REGISTER_ACK',
                    'status': 'success',
                    'message': f'Cliente {client_id} registrado exitosamente'
                }
                client_socket.send(json.dumps(response).encode('utf-8'))
                logging.info(f"✅ Cliente registrado: {client_id}")
                
                # Loop de recepción de mensajes
                while self.running:
                    data = client_socket.recv(4096).decode('utf-8')
                    
                    if not data:
                        break
                    
                    # Procesar mensaje recibido
                    self._process_message(client_id, data)
                    
        except Exception as e:
            logging.error(f"Error manejando cliente {client_id}: {e}")
        finally:
            # Limpiar al desconectar
            if client_id:
                with self.clients_lock:
                    if client_id in self.clients:
                        del self.clients[client_id]
                logging.info(f"🔌 Cliente desconectado: {client_id}")
            
            client_socket.close()
    
    def _process_message(self, sender_id: str, data: str):
        """
        Procesa mensajes recibidos de los clientes y los enruta según el tipo.
        
        Args:
            sender_id (str): ID del cliente que envió el mensaje
            data (str): Datos JSON recibidos
        """
        try:
            message = json.loads(data)
            msg_type = message.get('type')
            
            logging.info(f"📨 Mensaje de {sender_id}: {msg_type}")
            
            # Enrutamiento según tipo de mensaje
            if msg_type == 'BROADCAST':
                # Enviar a todos los clientes excepto el emisor
                self._broadcast(sender_id, message)
                
            elif msg_type == 'DIRECT':
                # Enviar a un cliente específico
                target = message.get('target')
                self._send_direct(sender_id, target, message)
                
            elif msg_type == 'REQUEST':
                # Solicitud que espera respuesta
                self._handle_request(sender_id, message)
                
            else:
                logging.warning(f"Tipo de mensaje desconocido: {msg_type}")
                
        except json.JSONDecodeError:
            logging.error(f"Error decodificando JSON de {sender_id}")
        except Exception as e:
            logging.error(f"Error procesando mensaje: {e}")
    
    def _broadcast(self, sender_id: str, message: dict):
        """
        Envía un mensaje a todos los clientes conectados excepto al emisor.
        
        Args:
            sender_id (str): ID del cliente que originó el mensaje
            message (dict): Mensaje a transmitir
        """
        message['sender'] = sender_id
        # CORREGIDO: Usar datetime.now(timezone.utc) en lugar de datetime.utcnow()
        message['timestamp'] = datetime.now(timezone.utc).isoformat()
        data = json.dumps(message).encode('utf-8')
        
        with self.clients_lock:
            for client_id, client_socket in self.clients.items():
                if client_id != sender_id:
                    try:
                        client_socket.send(data)
                        logging.info(f"📤 Broadcast enviado a {client_id}")
                    except Exception as e:
                        logging.error(f"Error enviando a {client_id}: {e}")
    
    def _send_direct(self, sender_id: str, target_id: str, message: dict):
        """
        Envía un mensaje directamente a un cliente específico.
        
        Args:
            sender_id (str): ID del cliente emisor
            target_id (str): ID del cliente destinatario
            message (dict): Mensaje a enviar
        """
        message['sender'] = sender_id
        # CORREGIDO: Usar datetime.now(timezone.utc) en lugar de datetime.utcnow()
        message['timestamp'] = datetime.now(timezone.utc).isoformat()
        
        with self.clients_lock:
            if target_id in self.clients:
                try:
                    data = json.dumps(message).encode('utf-8')
                    self.clients[target_id].send(data)
                    logging.info(f"📧 Mensaje directo: {sender_id} → {target_id}")
                    
                    # Confirmar al emisor
                    ack = {
                        'type': 'DELIVERY_ACK',
                        'status': 'delivered',
                        'target': target_id
                    }
                    self.clients[sender_id].send(json.dumps(ack).encode('utf-8'))
                    
                except Exception as e:
                    logging.error(f"Error en mensaje directo: {e}")
            else:
                # Cliente no encontrado
                error = {
                    'type': 'ERROR',
                    'message': f'Cliente {target_id} no encontrado'
                }
                self.clients[sender_id].send(json.dumps(error).encode('utf-8'))
    
    def _handle_request(self, sender_id: str, message: dict):
        """
        Maneja solicitudes que requieren respuesta de otro cliente.
        
        Args:
            sender_id (str): ID del cliente que hace la solicitud
            message (dict): Mensaje de solicitud con target y datos
        """
        target_id = message.get('target')
        message['sender'] = sender_id
        # CORREGIDO: Usar datetime.now(timezone.utc) en lugar de datetime.utcnow()
        message['timestamp'] = datetime.now(timezone.utc).isoformat()
        
        with self.clients_lock:
            if target_id in self.clients:
                try:
                    data = json.dumps(message).encode('utf-8')
                    self.clients[target_id].send(data)
                    logging.info(f"🔄 Request: {sender_id} → {target_id}")
                except Exception as e:
                    logging.error(f"Error en request: {e}")
    
    def get_connected_clients(self) -> List[str]:
        """
        Retorna lista de IDs de clientes conectados.
        
        Returns:
            List[str]: Lista de identificadores de clientes activos
        """
        with self.clients_lock:
            return list(self.clients.keys())
    
    def stop(self):
        """
        Detiene el servidor y cierra todas las conexiones.
        """
        self.running = False
        
        # Cerrar conexiones de clientes
        with self.clients_lock:
            for client_socket in self.clients.values():
                try:
                    client_socket.close()
                except:
                    pass
            self.clients.clear()
        
        # Cerrar socket del servidor
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
        
        logging.info("🛑 Bus de mensajes detenido")


def main():
    """
    Función principal para ejecutar el bus de mensajes.
    """
    bus = MessageBus(host='localhost', port=5000)
    
    try:
        bus.start()
    except KeyboardInterrupt:
        logging.info("\n🔚 Deteniendo servidor...")
        bus.stop()


if __name__ == "__main__":
    main()