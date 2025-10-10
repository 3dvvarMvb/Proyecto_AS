// Script de inicialización de MongoDB
// Se ejecuta automáticamente al crear el contenedor

print('========================================');
print('Iniciando configuración de MongoDB');
print('========================================');

// Conectar a la base de datos de administración
db = db.getSiblingDB('admin');

// Autenticar como admin
db.auth('admin', 'admin123');

// Crear la base de datos principal
db = db.getSiblingDB('arquitectura_software');

print('✅ Base de datos "arquitectura_software" creada');

// ==========================================
// COLECCIÓN: usuarios
// ==========================================
db.createCollection('usuarios', {
    validator: {
        $jsonSchema: {
            bsonType: "object",
            required: ["tipo_usuario", "correo", "rut"],
            properties: {
                tipo_usuario: {
                    enum: ["administrador", "conserje", "personal"],
                    description: "Tipo de usuario: administrador, conserje, personal - requerido"
                },
                correo: {
                    bsonType: "string",
                    pattern: "^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$",
                    description: "Dirección de email del usuario - requerido"
                },
                rut: {
                    bsonType: "string",
                    pattern: "^[0-9]{7,8}-[0-9Kk]{1}$",
                    description: "RUT del usuario en formato XXXXXXXX-X - requerido"
                }
            }
        }
    }
});

// Crear índices para usuarios
db.usuarios.createIndex({ "rut": 1 }, { unique: true });
db.usuarios.createIndex({ "correo": 1 }, { unique: true });
db.usuarios.createIndex({ "tipo_usuario": 1 });

print('✅ Colección "usuarios" creada con validación y índices');

// Insertar usuarios de prueba
db.usuarios.insertMany([
    {
        tipo_usuario: "administrador",
        correo: "admin@condominio.cl",
        rut: "12345678-9"
    },
    {
        tipo_usuario: "conserje",
        correo: "conserje@condominio.cl",
        rut: "98765432-1"
    },
    {
        tipo_usuario: "personal",
        correo: "personal@condominio.cl",
        rut: "11223344-5"
    }
]);

print('✅ Usuarios de prueba insertados (3 registros)');

// ==========================================
// COLECCIÓN: llamadas
// ==========================================
db.createCollection('llamadas', {
    validator: {
        $jsonSchema: {
            bsonType: "object",
            required: ["fecha", "hora", "caller", "depto", "status"],
            properties: {
                fecha: {
                    bsonType: "date",
                    description: "Fecha de la llamada - requerido"
                },
                hora: {
                    bsonType: "string",
                    pattern: "^([01][0-9]|2[0-3]):[0-5][0-9]$",
                    description: "Hora de la llamada en formato HH:MM - requerido"
                },
                caller: {
                    bsonType: "string",
                    description: "Nombre de quien realiza la llamada - requerido"
                },
                depto: {
                    bsonType: "string",
                    description: "Número de departamento - requerido"
                },
                status: {
                    enum: ["recibida", "rechazada", "sin respuesta"],
                    description: "Estado de la llamada: recibida, rechazada, sin respuesta - requerido"
                }
            }
        }
    }
});

// Crear índices para llamadas
db.llamadas.createIndex({ "fecha": -1 });
db.llamadas.createIndex({ "depto": 1, "fecha": -1 });
db.llamadas.createIndex({ "status": 1 });
db.llamadas.createIndex({ "caller": 1 });

print('✅ Colección "llamadas" creada con validación y índices');

// Insertar llamadas de prueba
var fechaHoy = new Date();
var fechaAyer = new Date(Date.now() - 86400000); // Ayer

db.llamadas.insertMany([
    {
        fecha: fechaHoy,
        hora: "09:30",
        caller: "Juan Pérez",
        depto: "101",
        status: "recibida"
    },
    {
        fecha: fechaHoy,
        hora: "10:15",
        caller: "María González",
        depto: "205",
        status: "sin respuesta"
    },
    {
        fecha: fechaAyer,
        hora: "14:45",
        caller: "Carlos Rodríguez",
        depto: "301",
        status: "rechazada"
    },
    {
        fecha: fechaAyer,
        hora: "16:20",
        caller: "Ana López",
        depto: "102",
        status: "recibida"
    }
]);

print('✅ Llamadas de prueba insertadas (4 registros)');

// ==========================================
// COLECCIÓN: mensajes
// ==========================================
db.createCollection('mensajes', {
    validator: {
        $jsonSchema: {
            bsonType: "object",
            required: ["fecha", "hora", "sender", "receiver", "mensaje"],
            properties: {
                fecha: {
                    bsonType: "date",
                    description: "Fecha de envío del mensaje - requerido"
                },
                hora: {
                    bsonType: "string",
                    pattern: "^([01][0-9]|2[0-3]):[0-5][0-9]$",
                    description: "Hora de envío del mensaje en formato HH:MM - requerido"
                },
                sender: {
                    bsonType: "objectId",
                    description: "ID de quien envía el mensaje (ObjectId de usuarios) - requerido"
                },
                receiver: {
                    bsonType: "objectId",
                    description: "ID de quien recibe el mensaje (ObjectId de usuarios) - requerido"
                },
                mensaje: {
                    bsonType: "string",
                    maxLength: 5000,
                    description: "Contenido del mensaje - requerido"
                }
            }
        }
    }
});

// Crear índices para mensajes
db.mensajes.createIndex({ "fecha": -1, "hora": -1 });
db.mensajes.createIndex({ "sender": 1, "fecha": -1 });
db.mensajes.createIndex({ "receiver": 1, "fecha": -1 });
db.mensajes.createIndex({ "sender": 1, "receiver": 1 });

print('✅ Colección "mensajes" creada con validación y índices');

// Obtener IDs de usuarios para los mensajes de prueba
var usuario1 = db.usuarios.findOne({ rut: "12345678-9" });
var usuario2 = db.usuarios.findOne({ rut: "98765432-1" });
var usuario3 = db.usuarios.findOne({ rut: "11223344-5" });

// Insertar mensajes de prueba
if (usuario1 && usuario2 && usuario3) {
    db.mensajes.insertMany([
        {
            fecha: fechaHoy,
            hora: "08:30",
            sender: usuario1._id,
            receiver: usuario2._id,
            mensaje: "Buenos días, necesito hablar contigo sobre el mantenimiento del edificio"
        },
        {
            fecha: fechaHoy,
            hora: "09:15",
            sender: usuario2._id,
            receiver: usuario1._id,
            mensaje: "Claro, estoy disponible después de las 10:00"
        },
        {
            fecha: fechaAyer,
            hora: "15:30",
            sender: usuario3._id,
            receiver: usuario2._id,
            mensaje: "¿Cuál es el horario de la sala de eventos?"
        },
        {
            fecha: fechaAyer,
            hora: "15:45",
            sender: usuario2._id,
            receiver: usuario3._id,
            mensaje: "La sala está disponible de lunes a viernes de 8:00 a 22:00"
        }
    ]);
    
    print('✅ Mensajes de prueba insertados (4 registros)');
} else {
    print('⚠️ No se pudieron insertar mensajes de prueba - falta información de usuarios');
}

// ==========================================
// CREAR USUARIO DE APLICACIÓN
// ==========================================
db = db.getSiblingDB('admin');
db.createUser({
    user: "app_user",
    pwd: "app_password_123",
    roles: [
        {
            role: "readWrite",
            db: "arquitectura_software"
        }
    ]
});

print('✅ Usuario de aplicación "app_user" creado');

// ==========================================
// RESUMEN Y ESTADÍSTICAS
// ==========================================
db = db.getSiblingDB('arquitectura_software');

print('\n========================================');
print('CONFIGURACIÓN COMPLETADA');
print('========================================');
print('Base de datos: arquitectura_software');
print('\nColecciones creadas:');
print('  📋 usuarios: ' + db.usuarios.countDocuments() + ' documentos');
print('     - Índices: rut (único), correo (único), tipo_usuario');
print('     - Validación: tipo_usuario, correo, rut');
print('');
print('  📞 llamadas: ' + db.llamadas.countDocuments() + ' documentos');
print('     - Índices: fecha, depto+fecha, status, caller');
print('     - Validación: fecha, hora, caller, depto, status');
print('');
print('  💬 mensajes: ' + db.mensajes.countDocuments() + ' documentos');
print('     - Índices: fecha+hora, sender+fecha, receiver+fecha, sender+receiver');
print('     - Validación: fecha, hora, sender, receiver, mensaje');
print('========================================');
print('');

// Mostrar ejemplo de cada colección
print('📄 EJEMPLO DE DOCUMENTOS:');
print('');
print('--- Usuario ---');
printjson(db.usuarios.findOne());
print('');
print('--- Llamada ---');
printjson(db.llamadas.findOne());
print('');
print('--- Mensaje ---');
printjson(db.mensajes.findOne());
print('');
print('========================================');
print('✅ INICIALIZACIÓN COMPLETA');
print('========================================');