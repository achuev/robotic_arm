#!/usr/bin/env python3
"""Снимает эталон для проверки 3D-вида: пары «углы суставов → положение схвата».

    gateway/.venv/bin/python tools/capture_fk_fixtures.py

Источник истины — TF от ROS (`base_link` → `gripper_frame_link`), который шлюз
кладёт в поле `ee` каждого сообщения `state`. Фронтенд считает то же самое
своей цепочкой из URDF, и `frontend/src/lib/armChain.test.ts` сверяет одно с
другим. Пока эти два числа сходятся, 3D-вид показывает руку там же, где она
на самом деле.

Требует ПОДНЯТОГО стенда (docker compose -f deploy/compose.dev.yml up -d).
Берёт ФАКТИЧЕСКИЕ углы из ответа, а не заданные: ограничитель скорости мог не
довести сустав до цели, и тогда эталон был бы самопротиворечивым.

Перезапускать после любого изменения URDF руки.
"""
import asyncio, json, urllib.request
import websockets
BASE="http://localhost:8080"; WS="ws://localhost:8080/ws"
POSES = [
    ("нули",  {"shoulder_pan":0.0,"shoulder_lift":0.0,"elbow_flex":0.0,"wrist_flex":0.0,"wrist_roll":0.0}),
    ("home",  {"shoulder_pan":0.0,"shoulder_lift":-0.9,"elbow_flex":0.9,"wrist_flex":0.0,"wrist_roll":0.0}),
    ("wave",  {"shoulder_pan":0.0,"shoulder_lift":-1.2,"elbow_flex":0.6,"wrist_flex":0.0,"wrist_roll":1.2}),
    ("поворот основания", {"shoulder_pan":0.8,"shoulder_lift":-0.9,"elbow_flex":0.9,"wrist_flex":0.0,"wrist_roll":0.0}),
    ("кисть согнута", {"shoulder_pan":-0.5,"shoulder_lift":-0.7,"elbow_flex":0.5,"wrist_flex":-0.6,"wrist_roll":0.3}),
    ("вразнобой", {"shoulder_pan":0.35,"shoulder_lift":-1.0,"elbow_flex":1.1,"wrist_flex":0.4,"wrist_roll":-1.5}),
]
def session():
    r=urllib.request.Request(BASE+"/api/session",method="POST",data=b"{}",
                             headers={"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(r).read())["token"]
async def drain(ws,s):
    st=None
    try:
        async with asyncio.timeout(s):
            async for raw in ws:
                m=json.loads(raw)
                if m["t"]=="state": st=m
    except (TimeoutError,asyncio.TimeoutError): pass
    return st
async def main():
    ws=await websockets.connect(WS)
    await ws.send(json.dumps({"t":"hello","token":session()})); await drain(ws,1)
    await ws.send(json.dumps({"t":"enqueue"})); await drain(ws,2)
    out=[]
    for label,pose in POSES:
        for _ in range(45):
            await ws.send(json.dumps({"t":"set_joints","positions":pose}))
            await asyncio.sleep(0.1)
        st=await drain(ws,1.5)
        # берём ФАКТИЧЕСКИЕ углы: лимитер мог не довести до цели
        out.append({"label":label,"joints":{k:round(v,6) for k,v in st["joints"].items()},
                    "ee":{k:round(v,6) for k,v in st["ee"].items()}})
        print(f"{label:<20} ee = ({st['ee']['x']:+.5f}, {st['ee']['y']:+.5f}, {st['ee']['z']:+.5f})")
    await ws.send(json.dumps({"t":"release"})); await drain(ws,1); await ws.close()
    open("frontend/src/lib/armChain.fixtures.json","w").write(json.dumps(out,indent=2,ensure_ascii=False)+"\n")
    print("\n→ frontend/src/lib/armChain.fixtures.json")
asyncio.run(main())
