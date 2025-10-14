// MongoDB/mongo-init.js
print('=== Init Mongo (multi-DB) ===');

// ---------- CLUSTER: usuarios ----------
db = db.getSiblingDB('usuarios_db');
db.createCollection('usuarios', {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["tipo_usuario","correo","rut"],
    properties: {
      tipo_usuario: { enum: ["administrador","conserje","personal"] },
      correo: { bsonType: "string", pattern: "^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$" },
      rut:    { bsonType: "string", pattern: "^[0-9]{7,8}-[0-9Kk]{1}$" }
    }
  } }
});
db.usuarios.createIndex({ rut: 1 }, { unique: true });
db.usuarios.createIndex({ correo: 1 }, { unique: true });
db.usuarios.createIndex({ tipo_usuario: 1 });

db.usuarios.insertMany([
  { tipo_usuario:"administrador", correo:"admin@condominio.cl",   rut:"12345678-9" },
  { tipo_usuario:"conserje",      correo:"conserje@condominio.cl", rut:"98765432-1" },
  { tipo_usuario:"personal",      correo:"personal@condominio.cl", rut:"11223344-5" }
]);

// Contactos (lo usará el servicio-contactos apuntando a usuarios_db/contactos)
db.createCollection('contactos', {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["nombre","departamento","telefono"],
    properties: {
      nombre:       { bsonType: "string" },
      departamento: { bsonType: "string" },
      telefono:     { bsonType: "string" },
      email:        { bsonType: "string" },
      tags:         { bsonType: "array", items: { bsonType: "string" } },
      createdAt:    { bsonType: "date" }
    }
  } }
});
db.contactos.createIndex({ departamento: 1, nombre: 1 });
db.contactos.createIndex({ nombre: "text", departamento: "text" });
db.contactos.insertMany([
  { nombre: "Juan Pérez",  departamento: "101", telefono: "+56 9 1111 1111", createdAt: new Date() },
  { nombre: "Ana Gómez",   departamento: "402", telefono: "+56 9 2222 2222", createdAt: new Date() }
]);

// ---------- CLUSTER: llamadas ----------
db = db.getSiblingDB('llamadas_db');
db.createCollection('llamadas', {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["fecha","hora","caller","depto","status"],
    properties: {
      fecha:   { bsonType: "date" },
      hora:    { bsonType: "string" },
      caller:  { bsonType: "string" },
      callerId:{ bsonType: "objectId" },
      depto:   { bsonType: "string" },
      duracion:{ bsonType: "int" },
      status:  { enum:["recibida","rechazada","sin_respuesta"] }
    }
  } }
});
db.llamadas.createIndex({ fecha: -1 });
db.llamadas.createIndex({ depto: 1, fecha: -1 });
db.llamadas.createIndex({ status: 1 });
db.llamadas.createIndex({ caller: 1, fecha: -1 });

db.llamadas.insertOne({
  fecha: new Date(),
  hora:  "10:15",
  caller: "admin@condominio.cl",
  depto: "402",
  duracion: 120,
  status: "recibida"
});

// ---------- CLUSTER: mensajes ----------
db = db.getSiblingDB('mensajes_db');
db.createCollection('mensajes', {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["fecha","hora","sender","receiver","mensaje"],
    properties: {
      fecha:    { bsonType: "date" },
      hora:     { bsonType: "string" },
      sender:   { bsonType: "objectId" },
      receiver: { bsonType: "objectId" },
      mensaje:  { bsonType: "string" },
      ciphertext: { bsonType: "string" },
      iv:         { bsonType: "string" },
      alg:        { bsonType: "string" },
      deliveryStatus: { enum:["enviado","entregado","leido"] }
    }
  } }
});
db.mensajes.createIndex({ fecha: -1 });
db.mensajes.createIndex({ sender: 1, fecha: -1 });
db.mensajes.createIndex({ receiver: 1, fecha: -1 });
db.mensajes.createIndex({ sender: 1, receiver: 1, fecha: -1 });

db.mensajes.insertOne({
  fecha: new Date(),
  hora:  "11:30",
  sender: ObjectId(),
  receiver: ObjectId(),
  mensaje: "Bienvenido al sistema",
  deliveryStatus: "enviado"
});

// ---------- Usuario de aplicación (roles por DB que usaremos) ----------
db = db.getSiblingDB('admin');
db.createUser({
  user: "app_user",
  pwd: "app_password_123",
  roles: [
    { role: "readWrite", db: "usuarios_db"  },
    { role: "readWrite", db: "llamadas_db"  },
    { role: "readWrite", db: "mensajes_db" },
    { role: "readWrite", db: "arquitectura_db" } // por si decides usar esta para contactos
  ]
});

print('✅ Init multi-DB listo');
