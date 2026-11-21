import os, asyncio, datetime, secrets, base64, ipaddress, socket, logging, time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, select
from sqlalchemy.orm import declarative_base, sessionmaker
from pymodbus.client import AsyncModbusTcpClient

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./eumbms.db")
if DATABASE_URL.startswith("postgres://"): DATABASE_URL="postgresql+psycopg://"+DATABASE_URL[len("postgres://"):]
if DATABASE_URL.startswith("postgresql://"): DATABASE_URL="postgresql+psycopg://"+DATABASE_URL[len("postgresql://"):]
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread":False} if DATABASE_URL.startswith("sqlite") else {}, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()

class Site(Base):
    __tablename__="sites"
    id=Column(Integer,primary_key=True)
    name=Column(String(200),nullable=False)

class Device(Base):
    __tablename__="devices"
    id=Column(Integer,primary_key=True)
    site_id=Column(Integer,ForeignKey("sites.id"),nullable=False)
    name=Column(String(200),nullable=False)
    host=Column(String(255),nullable=False)
    port=Column(Integer,default=502)
    unit_id=Column(Integer,default=1)
    enabled=Column(Integer,default=0)

class Tag(Base):
    __tablename__="tags"
    id=Column(Integer,primary_key=True)
    device_id=Column(Integer,ForeignKey("devices.id"),nullable=False)
    name=Column(String(200),nullable=False)
    address=Column(Integer,nullable=False)
    scale=Column(Float,default=1)
    value=Column(Float,nullable=True)
    quality=Column(String(20),default="Uncertain")
    updated_at=Column(DateTime,nullable=True)

class Reading(Base):
    __tablename__="readings"
    id=Column(Integer,primary_key=True)
    tag_id=Column(Integer,ForeignKey("tags.id"),nullable=False)
    value=Column(Float,nullable=False)
    created_at=Column(DateTime,default=datetime.datetime.utcnow)

async def poll():
    while True:
        try:
            with Session() as db:
                devices=db.scalars(select(Device).where(Device.enabled==1)).all()
                for dev in devices:
                    client=AsyncModbusTcpClient(dev.host,port=dev.port,timeout=2)
                    try:
                        if not await client.connect():
                            raise ConnectionError("connection failed")
                        for tag in db.scalars(select(Tag).where(Tag.device_id==dev.id)).all():
                            try:
                                result=await client.read_holding_registers(tag.address,count=1,device_id=dev.unit_id)
                                if result.isError(): raise ValueError(str(result))
                                tag.value=result.registers[0]*tag.scale
                                tag.quality="Good"
                                tag.updated_at=datetime.datetime.utcnow()
                                db.add(Reading(tag_id=tag.id,value=tag.value))
                            except Exception:
                                tag.quality="Bad"
                    except Exception:
                        for tag in db.scalars(select(Tag).where(Tag.device_id==dev.id)).all():
                            tag.quality="Bad"
                    finally:
                        client.close()
                db.commit()
        except Exception as exc:
            print("poll error",repr(exc))
        await asyncio.sleep(10)

@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    task=asyncio.create_task(poll()) if os.getenv("ENABLE_POLLING","false").lower()=="true" else None
    yield
    if task:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

app=FastAPI(title="EUM BMS PRO",version="0.2.0",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
AUTH_ATTEMPTS=defaultdict(deque)
ADMIN_USER=os.getenv("ADMIN_USER","")
ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD","")
if not ADMIN_USER or len(ADMIN_PASSWORD)<20:
    raise RuntimeError("ADMIN_USER and ADMIN_PASSWORD (minimum 20 characters) must be configured")
@app.middleware("http")
async def require_auth(request:Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    client_ip=request.client.host if request.client else "unknown"
    now=time.monotonic()
    attempts=AUTH_ATTEMPTS[client_ip]
    while attempts and now-attempts[0]>300: attempts.popleft()
    if len(attempts)>=10:
        return JSONResponse({"detail":"Too many authentication attempts"},status_code=429,headers={"Retry-After":"300"})
    auth=request.headers.get("authorization","")
    try:
        if not auth.startswith("Basic "): raise ValueError()
        user,pwd=base64.b64decode(auth[6:],validate=True).decode("utf-8").split(":",1)
        if not (secrets.compare_digest(user,ADMIN_USER) and secrets.compare_digest(pwd,ADMIN_PASSWORD)):raise ValueError()
    except (ValueError,UnicodeError):
        attempts.append(now)
        return JSONResponse({"detail":"Authentication required"},status_code=401,headers={"WWW-Authenticate":'Basic realm="EUM BMS PRO"',"Cache-Control":"no-store"})
    attempts.clear()
    response=await call_next(request)
    response.headers["Cache-Control"]="no-store"
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["X-Frame-Options"]="DENY"
    response.headers["Referrer-Policy"]="no-referrer"
    response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    return response

class SiteIn(BaseModel):
    name:str=Field(min_length=1,max_length=200)
class DeviceIn(BaseModel):
    site_id:int
    name:str
    host:str=Field(min_length=1,max_length=255)
    port:int=Field(default=502,ge=1,le=65535)
    unit_id:int=Field(default=1,ge=0,le=247)
class TagIn(BaseModel):
    device_id:int
    name:str
    address:int=Field(ge=0,le=65535)
    scale:float=Field(default=1.0,ge=-1000000,le=1000000)

@app.get("/health")
def health(): return {"status":"ok","version":"0.2.0"}
@app.get("/api/sites")
def sites():
    with Session() as db: return [{"id":s.id,"name":s.name} for s in db.scalars(select(Site)).all()]
@app.post("/api/sites")
def create_site(data:SiteIn):
    with Session() as db:
        obj=Site(name=data.name);db.add(obj);db.commit();db.refresh(obj)
        return {"id":obj.id,"name":obj.name}
@app.get("/api/devices")
def devices():
    with Session() as db:
        return [{"id":d.id,"site_id":d.site_id,"name":d.name,"host":d.host,"port":d.port,"enabled":bool(d.enabled)} for d in db.scalars(select(Device)).all()]
@app.post("/api/devices")
def create_device(data:DeviceIn):
    with Session() as db:
        if not db.get(Site,data.site_id):raise HTTPException(404,"Site not found")
        try:
            ip=ipaddress.ip_address(data.host)
            if not ip.is_private: raise ValueError()
        except ValueError:
            raise HTTPException(400,"Only private IP addresses are permitted for field devices")
        obj=Device(**data.model_dump());db.add(obj);db.commit();db.refresh(obj)
        return {"id":obj.id,"name":obj.name,"enabled":False}
@app.get("/api/tags")
def tags():
    with Session() as db:
        return [{"id":t.id,"device_id":t.device_id,"name":t.name,"address":t.address,"value":t.value,"quality":t.quality,"updated_at":t.updated_at} for t in db.scalars(select(Tag)).all()]
@app.post("/api/tags")
def create_tag(data:TagIn):
    with Session() as db:
        if not db.get(Device,data.device_id):raise HTTPException(404,"Device not found")
        obj=Tag(**data.model_dump());db.add(obj);db.commit();db.refresh(obj)
        return {"id":obj.id,"name":obj.name}
@app.get("/",response_class=HTMLResponse)
def index():
    return """<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EUM BMS PRO</title><style>body{font-family:system-ui;background:#0c1526;color:#e5edf7;margin:0}header{background:#14243c;padding:22px 6%;border-bottom:1px solid #30415a}main{max-width:1100px;margin:32px auto;padding:0 20px}h1{margin:0}small{color:#9ab0cb}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:18px}.card{background:#17283f;padding:24px;border:1px solid #30415a;border-radius:14px}strong{font-size:30px;color:#71d4ef}input,button,select{padding:11px;border-radius:8px;border:1px solid #41536a;background:#243650;color:white;margin:4px}button{background:#1465ac;cursor:pointer}table{width:100%;border-collapse:collapse;margin-top:14px}td,th{text-align:left;padding:12px;border-bottom:1px solid #30415a}</style><header><h1>EUM BMS PRO</h1><small>이음BMS · 통합 전력관제 및 원격검침 플랫폼 · 초기 개발 버전</small></header><main><div class="grid"><div class="card">사업장<br><strong id="sc">0</strong></div><div class="card">등록 장치<br><strong id="dc">0</strong></div><div class="card">태그<br><strong id="tc">0</strong></div><div class="card">통신 정상 태그<br><strong id="gc">0</strong></div></div><h2>사업장 등록</h2><form id="site"><input name="name" placeholder="사업장 이름" required><button>추가</button></form><h2>Modbus TCP 장치 등록</h2><form id="device"><select name="site_id" id="sites"></select><input name="name" placeholder="장치명" required><input name="host" placeholder="IP 또는 호스트" required><input name="port" type="number" value="502"><button>등록</button></form><h2>레지스터 태그 등록</h2><form id="tag"><select name="device_id" id="devices"></select><input name="name" placeholder="태그명" required><input name="address" type="number" min="0" placeholder="0-based 주소" required><input name="scale" type="number" step="any" value="1"><button>등록</button></form><h2>실시간 태그</h2><small>수집은 서버 ENABLE_POLLING=true 설정 시 시작됩니다. 장치는 기본적으로 비활성화됩니다.</small><table><thead><tr><th>태그</th><th>값</th><th>품질</th><th>최종 수집</th></tr></thead><tbody id="rows"></tbody></table></main><script>async function req(path,opts){const r=await fetch(path,opts);if(!r.ok)throw Error(await r.text());return r.json()}async function refresh(){const [s,d,t]=await Promise.all(['/api/sites','/api/devices','/api/tags'].map(x=>req(x)));sc.textContent=s.length;dc.textContent=d.length;tc.textContent=t.length;gc.textContent=t.filter(x=>x.quality==='Good').length;sites.innerHTML=s.map(x=>'<option value="'+x.id+'">'+esc(x.name)+'</option>').join('');devices.innerHTML=d.map(x=>'<option value="'+x.id+'">'+esc(x.name)+'</option>').join('');rows.innerHTML=t.map(x=>'<tr><td>'+esc(x.name)+'</td><td>'+(x.value??'—')+'</td><td>'+esc(x.quality)+'</td><td>'+(x.updated_at||'—')+'</td></tr>').join('')}function esc(x){return String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}for(const id of ['site','device','tag'])document.getElementById(id).onsubmit=async e=>{e.preventDefault();const obj=Object.fromEntries(new FormData(e.target));for(const k of ['site_id','device_id','port','address','scale'])if(obj[k]!==undefined)obj[k]=Number(obj[k]);try{await req('/api/'+(id==='site'?'sites':id==='device'?'devices':'tags'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});e.target.reset();refresh()}catch(err){alert(err.message)}};refresh();setInterval(refresh,10000)</script></html>"""
