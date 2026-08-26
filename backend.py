import os
from datetime import date,timedelta
import httpx
from fastapi import FastAPI,HTTPException,Query
from fastapi.middleware.cors import CORSMiddleware
BASE="https://v3.football.api-sports.io";BET365="Bet365"
app=FastAPI(title="MACANALIZ PRO API")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["GET","OPTIONS"],allow_headers=["*"])
client=httpx.AsyncClient(timeout=20); bcache=None
def key():
 k=os.getenv("API_FOOTBALL_KEY","").strip()
 if not k: raise HTTPException(503,"API_FOOTBALL_KEY Render Environment Variable olarak eklenmemiş.")
 return k
async def af(path,params=None):
 try:r=await client.get(BASE+path,params=params or {},headers={"x-apisports-key":key(),"Accept":"application/json"})
 except httpx.RequestError as e:raise HTTPException(502,f"API-Football bağlantı hatası: {e}")
 try:d=r.json()
 except:raise HTTPException(502,f"API-Football JSON döndürmedi. HTTP {r.status_code}")
 if d.get("errors"):
  e=d["errors"];raise HTTPException(400," | ".join(map(str,e.values())) if isinstance(e,dict) else str(e))
 if r.status_code>=400:raise HTTPException(r.status_code,f"API-Football HTTP {r.status_code}")
 return d
@app.get("/")
async def root():return {"status":"ok","bet365_only":True}
@app.get("/api/health")
async def health():return {"status":"ok","bet365_only":True}
@app.get("/api/bet365")
async def bookmaker():
 global bcache
 if bcache:return {"connected":True,"bookmaker":bcache}
 d=await af("/odds/bookmakers");rows=d.get("response") or []
 b=next((x for x in rows if str(x.get("name","")).strip().lower()=="bet365"),None)
 if not b:b=next((x for x in rows if "bet365" in str(x.get("name","")).lower()),None)
 if not b:raise HTTPException(404,"Bet365 bu API hesabında bulunamadı.")
 bcache={"id":b["id"],"name":b.get("name","Bet365")};return {"connected":True,"bookmaker":bcache}
@app.get("/api/fixtures")
async def fixtures(ids:str=Query("39,140,135,78,61,203"),days:int=Query(7,ge=1,le=14),season:int|None=None):
 try: leagues=[int(x.strip()) for x in ids.split(",") if x.strip()]
 except:raise HTTPException(400,"Lig ID listesi hatalı.")
 a=date.today();z=a+timedelta(days=days);out=[];errors=[]
 for lid in leagues:
  p={"league":lid,"from":a.isoformat(),"to":z.isoformat(),"timezone":"Europe/Istanbul"}
  if season is not None:p["season"]=season
  try:out += (await af("/fixtures",p)).get("response") or []
  except HTTPException as e:errors.append({"league_id":lid,"error":str(e.detail)})
 u={x.get("fixture",{}).get("id"):x for x in out if x.get("fixture",{}).get("id")}
 return {"live":True,"count":len(u),"fixtures":list(u.values()),"errors":errors}
def winner(bm):
 for bet in bm.get("bets",[]) or []:
  n=str(bet.get("name","")).lower()
  if "match winner" in n or n in ("1x2","fulltime result"):
   v={}
   for x in bet.get("values",[]) or []:
    try:o=float(x.get("odd"))
    except:continue
    q=str(x.get("value","")).lower()
    if q in ("home","1"):v["home"]=o
    elif q in ("draw","x"):v["draw"]=o
    elif q in ("away","2"):v["away"]=o
   if len(v)==3:return v
 return None
@app.get("/api/odds")
async def odds(fixture:int=Query(...,ge=1)):
 b=await bookmaker();d=await af("/odds",{"fixture":fixture,"bookmaker":b["bookmaker"]["id"]})
 for row in d.get("response",[]) or []:
  for bm in row.get("bookmakers",[]) or []:
   if "bet365" in str(bm.get("name","")).lower():
    o=winner(bm);return {"available":bool(o),"fixture_id":fixture,"bookmaker":bm.get("name","Bet365"),"bookmaker_id":bm.get("id"),"odds":o,"update":bm.get("update")}
 return {"available":False,"fixture_id":fixture,"bookmaker":"Bet365","bookmaker_id":b["bookmaker"]["id"],"odds":None}
@app.on_event("shutdown")
async def close():await client.aclose()
