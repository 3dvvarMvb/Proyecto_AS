# Servicios/admin/Administrador.py
import os, socket, json, threading, logging, time, secrets
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta, timezone
from pymongo import MongoClient, ASCENDING
from pymongo.errors import PyMongoError
from bson.objectid import ObjectId

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [ADMINISTRACIÓN] - %(levelname)s - %(message)s')

def _jsonline(o: dict) -> bytes: return (json.dumps(o, ensure_ascii=False) + "\n").encode("utf-8")
def _utcnow_iso() -> str: return datetime.now(timezone.utc).isoformat()
def _to_iso(dt): 
    if isinstance(dt, datetime):
        try: return dt.astimezone(timezone.utc).isoformat()
        except: return dt.isoformat()
    return None

class AdministracionService:
    """
    Acciones:
      verify_admin {username}
      get_user_info {username}
      admin_update_user {username, role?, active?, password?}
      admin_delete_user {username}
      generateReport {days?}
      system_status
    """
    def __init__(self,
        client_id="administracion_service",
        bus_host=os.getenv("BUS_HOST", "bus"),
        bus_port=int(os.getenv("BUS_PORT", "5000")),
        mongo_uri=os.getenv("MONGO_URI","mongodb://app_user:app_password_123@mongodb:27017/?authSource=admin"),
        users_db=os.getenv("USERS_DB","usuarios_db"),
        calls_db=os.getenv("CALLS_DB","llamadas_db"),
        msgs_db=os.getenv("MSGS_DB","mensajes_db"),
        run_tests: bool=False):
        self.client_id=client_id; self.bus_host=bus_host; self.bus_port=bus_port
        self.mongo_uri=mongo_uri; self.users_db=users_db; self.calls_db=calls_db; self.msgs_db=msgs_db
        self.socket: Optional[socket.socket]=None; self.connected=False; self.running=False
        self._mongo: Optional[MongoClient]=None; self.users=None; self.calls=None; self.msgs=None
        self._pending: Dict[str, Dict[str,Any]]={}; self._cv=threading.Condition()
        self.run_tests=run_tests

    # --- Mongo
    def _init_mongo(self):
        try:
            if self._mongo: return
            cli=MongoClient(self.mongo_uri, serverSelectionTimeoutMS=6000); cli.admin.command("ping")
            self._mongo=cli
            self.users=cli[self.users_db]["users"]; self.users.create_index([("username",ASCENDING)], unique=True)
            self.calls=cli[self.calls_db]["llamadas"]; self.calls.create_index([("fecha",ASCENDING),("hora",ASCENDING)])
            try: self.msgs=cli[self.msgs_db]["mensajes"]
            except: self.msgs=None
            logging.info("Mongo listo en Administración")
        except Exception as e:
            logging.warning(f"Administración sin Mongo (delegará): {e}")
            self._mongo=None; self.users=None; self.calls=None; self.msgs=None

    # --- BUS
    def connect(self)->bool:
        try:
            self._init_mongo()
            self.socket=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
            self.socket.connect((self.bus_host,self.bus_port))
            logging.info(f"Conectando al BUS en {self.bus_host}:{self.bus_port}")
            self.socket.sendall(_jsonline({"type":"REGISTER","client_id":self.client_id,"kind":"service","service":"Administracion"}))
            self.connected=True; self.running=True
            threading.Thread(target=self._listen,daemon=True).start()
            logging.info("Servicio de Administración iniciado")
            return True
        except Exception as e:
            logging.error(f"Error conectando al BUS: {e}"); return False

    def _send(self,obj:dict)->bool:
        if not self.connected or not self.socket: return False
        try: obj.setdefault("sender",self.client_id); self.socket.sendall(_jsonline(obj)); return True
        except Exception as e: logging.error(f"Error enviando: {e}"); self.connected=False; return False

    def _reply(self,target:str,data:dict,corr:Optional[str]):
        payload={"ok": True, "data": data} if not data.get("ok") else data  # si ya viene con ok, respeta
        out={"type":"DIRECT","target":target,"payload":payload,"service":"Administracion"}
        if corr: out["header"]={"correlationId":corr}
        self._send(out)

    def _listen(self):
        logging.info("Iniciando escucha de mensajes del BUS...")
        buf=""
        while self.running and self.connected and self.socket:
            try:
                chunk=self.socket.recv(65536)
                if not chunk: self.connected=False; break
                buf+=chunk.decode("utf-8")
                while "\n" in buf:
                    line,buf=buf.split("\n",1); line=line.strip()
                    if not line: continue
                    try: self._handle(json.loads(line))
                    except json.JSONDecodeError as e: logging.error(f"JSON NDJSON error: {e}")
            except Exception as e:
                if self.running: logging.error(f"Error recibiendo: {e}"); self.connected=False
                break
        logging.info("Listener detenido")

    def _handle(self,m:dict):
        t=m.get("type"); sender=m.get("sender","UNKNOWN")
        if t=="REQUEST":
            h=m.get("header") or {}; p=(m.get("payload") or {}).copy(); corr=h.get("correlationId")
            action=(h.get("action") or p.get("action") or "").strip();  p["action"]=action
            try: res=self._exec(p)
            except Exception as e: logging.exception("Error en REQUEST"); res={"ok":False,"error":str(e)}
            self._reply(sender, res, corr); return

        if t in ("DIRECT","RESPONSE") and sender=="autenticacion_service":
            h=m.get("header") or {}; corr=h.get("correlationId")
            if corr:
                with self._cv:
                    self._pending[corr]=m.get("payload",{}) or {}
                    self._cv.notify_all()
            return

        if t=="DELIVERY_ACK": logging.info(f"Mensaje entregado a {m.get('target')}"); return
        if t in ("BROADCAST","EVENT"): logging.info(f"{t} de {sender}: {m.get('payload')}"); return

    # --- acciones
    def _exec(self,p:Dict[str,Any])->Dict[str,Any]:
        a=p.get("action","")
        if a=="verify_admin": return self._verify_admin(p)
        if a=="get_user":
            # ✅ Nueva acción para obtener usuario completo con _id
            username=p.get("username")
            if not username: return {"ok":False,"error":"falta username"}
            if self.users is not None:
                try:
                    u=self.users.find_one({"username":username})
                    if u:
                        return {"ok":True,"user":{
                            "_id":str(u.get("_id")),
                            "id":str(u.get("_id")),
                            "username":u.get("username"),
                            "role":u.get("role","user"),
                            "active":bool(u.get("active",True)),
                            "created_at":_to_iso(u.get("created_at"))
                        }}
                    return {"ok":False,"error":"not_found"}
                except PyMongoError as e:
                    logging.warning(f"Mongo get_user falló: {e}")
                    return {"ok":False,"error":str(e)}
            return {"ok":False,"error":"mongo no disponible"}
        if a=="get_user_info":
            r=self._auth("get_user",{"username":p.get("username")})
            if r.get("ok"): 
                u=(r.get("data") or {}).get("user") or {}
                if not u: return {"ok":False,"error":"not_found"}
                return {"ok":True,"username":u.get("username"),"role":u.get("role","user"),
                        "active":bool(u.get("active",True)),"created_at":u.get("created_at")}
            return {"ok":False, "error": r.get("error") or r.get("message","auth_error")}
        if a=="admin_update_user":
            req={"username":p.get("username")}
            if "role" in p: req["role"]=p["role"]
            if "active" in p: req["active"]=bool(p["active"])
            if p.get("password"): req["password"]=str(p["password"])
            r=self._auth("update_user",req); return r if r.get("ok") else {"ok":False,**r}
        if a=="admin_delete_user":
            r=self._auth("delete_user",{"username":p.get("username")}); return r if r.get("ok") else {"ok":False,**r}
        if a=="generateReport": return self._report(p)
        if a=="system_status": return {"ok":True,"status":"ok","time":time.time()}
        return {"ok":False,"error":f"acción desconocida: {a}"}

    def _verify_admin(self,p:Dict[str,Any])->Dict[str,Any]:
        username=p.get("username") or p.get("admin_id")
        if not username: return {"ok":False,"error":"falta username"}
        if self.users is not None:
            try:
                u=self.users.find_one({"username":username})
                if u is not None:
                    return {"ok":True,"username":username,"is_admin":u.get("role")=="admin","active":bool(u.get("active",True))}
            except PyMongoError as e:
                logging.warning(f"Mongo verify_admin falló: {e}")
        r=self._auth("get_user",{"username":username}, timeout=6.0)
        if not r.get("ok"): return {"ok":False,"error":"no se pudo verificar"}
        u=(r.get("data") or {}).get("user")
        if not u: return {"ok":False,"error":"not_found"}
        return {"ok":True,"username":username,"is_admin":u.get("role")=="admin","active":bool(u.get("active",True))}

    def _auth(self,action:str,payload:Dict[str,Any],timeout:float=5.0)->Dict[str,Any]:
        corr=secrets.token_hex(8)
        msg={"type":"REQUEST","header":{"service":"Autenticacion","action":action,"correlationId":corr},"payload":dict(payload or {})}
        if not self._send(msg): return {"ok":False,"message":"fallo envío a Autenticación"}
        deadline=time.time()+timeout
        with self._cv:
            while time.time()<deadline:
                if corr in self._pending: return self._pending.pop(corr)
                self._cv.wait(timeout=max(0.0,deadline-time.time()))
        return {"ok":False,"message":"timeout esperando autenticación"}

    def _report(self,p:Dict[str,Any])->Dict[str,Any]:
        try: days=max(1,int(p.get("days",1)))
        except: days=1
        since=datetime.utcnow()-timedelta(days=days)
        calls_total=None; by_status={}; top_deptos=[]; recent=[]
        if self.calls is not None:
            try:
                q={"fecha":{"$gte":since}}
                calls_total=self.calls.count_documents(q)
                for d in self.calls.aggregate([{"$match":q},{"$group":{"_id":"$status","count":{"$sum":1}}}]):
                    by_status[d["_id"] or "desconocido"]=d["count"]
                for d in self.calls.aggregate([{"$match":q},{"$group":{"_id":"$depto","count":{"$sum":1}}},{"$sort":{"count":-1}},{"$limit":5}]):
                    top_deptos.append({"depto":d["_id"],"count":d["count"]})
                cur=self.calls.find(q).sort([("fecha",-1),("hora",-1)]).limit(20)
                for d in cur:
                    recent.append({"id":str(d.get("_id")),"fecha":_to_iso(d.get("fecha")),"hora":d.get("hora"),
                                   "caller":d.get("caller"),"depto":d.get("depto"),"status":d.get("status"),
                                   "destination":d.get("destination"),"duration":d.get("duration")})
            except PyMongoError as e:
                logging.warning(f"Mongo generateReport(calls) falló: {e}")
        msgs_total=None
        if self.msgs is not None:
            try: msgs_total=self.msgs.count_documents({"fecha":{"$gte":since}})
            except: msgs_total=None
        return {"ok":True,"generated_at":_utcnow_iso(),"window_days":days,
                "calls":{"total":calls_total,"by_status":by_status,"top_deptos":top_deptos,"recent":recent},
                "messages":{"total":msgs_total}}

    def disconnect(self):
        logging.info("Desconectando del BUS...")
        self.running=False; self.connected=False
        if self.socket:
            try: self.socket.close()
            except: pass
        if self._mongo:
            try: self._mongo.close()
            except: pass
        logging.info("Administración detenida")

def main():
    svc=AdministracionService()
    try:
        if svc.connect():
            while svc.connected: time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        svc.disconnect()

if __name__=="__main__":
    main()
