#!/usr/bin/env python3
"""Управление рукой из терминала — через шлюз, по тому же протоколу, что и сайт.

    deploy/.venv/bin/python deploy/arm.py state
    deploy/.venv/bin/python deploy/arm.py watch
    deploy/.venv/bin/python deploy/arm.py move shoulder_pan 0.3
    deploy/.venv/bin/python deploy/arm.py jog  elbow_flex -0.2
    deploy/.venv/bin/python deploy/arm.py gripper 0.5
    deploy/.venv/bin/python deploy/arm.py preset wave
    deploy/.venv/bin/python deploy/arm.py shell

ПОЧЕМУ ЧЕРЕЗ ШЛЮЗ, А НЕ ПО ШИНЕ НАПРЯМУЮ. Последовательный порт руки держит
драйвер внутри контейнера, и держит монопольно: пока стенд поднят, второй
процесс порт не откроет. Ходить в обход шлюза можно только при погашенном
стенде — для этого есть deploy/jog_joint.py и deploy/servo_scan.py.

Здесь же путь, который работает при живом стенде и заодно уважает очередь:
если рукой управляет посетитель, терминал встанет в очередь, а не отберёт
ход. Отобрать можно осознанно — флагом --force, он требует ADMIN_TOKEN.

ЧТО ВАЖНО ЗНАТЬ ПРО ПРОТОКОЛ (полностью — docs/api.md):
  * цели АБСОЛЮТНЫЕ, в радианах; потерянный пакет не разъезжает состояние;
  * молчание дольше watchdog_timeout_sec обрывает ход — отсюда ping раз в
    секунду;
  * ход без срока, пока в очереди пусто; как только кто-то встал, приходит
    повторный granted уже со сроком.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

import websockets

DEFAULT_URL = "http://localhost:8080"
#: Считаем, что сустав доехал. Ограничитель скорости ведёт цель плавно, и
#: точного совпадения не будет никогда — нужен допуск.
ARRIVED_EPS = 0.02
#: Сколько ждать доезда, прежде чем сдаться. Сустав может упереться в предел.
ARRIVE_TIMEOUT = 8.0


def die(msg: str, code: int = 1) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------- REST
def rest(base: str, path: str, method: str = "GET",
         token: Optional[str] = None) -> Dict[str, Any]:
    req = urllib.request.Request(
        base + path, method=method, data=b"" if method == "POST" else None)
    if token:
        req.add_header("X-Admin-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        die(f"{path} → HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
    except urllib.error.URLError as e:
        die(f"стенд недоступен на {base}: {e.reason}\n"
            f"   Поднят ли он? docker compose -f deploy/compose.prod.yml ps")
    raise AssertionError("недостижимо")


def ws_url(base: str) -> str:
    return base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"


# ------------------------------------------------------------------- клиент
class Arm:
    """Одно соединение со шлюзом на время работы команды."""

    def __init__(self, base: str, force: bool = False) -> None:
        self.base = base.rstrip("/")
        self.force = force
        self.ws: Any = None
        self.joints: Dict[str, float] = {}
        self.limits: Dict[str, Dict[str, float]] = {}
        self.controlling = False
        self._granted = asyncio.Event()
        self._state = asyncio.Event()
        self._enqueued = False
        self._last_queue_pos: Optional[int] = None

    async def __aenter__(self) -> "Arm":
        cfg = rest(self.base, "/api/config")
        self.limits = {j["name"]: j for j in cfg["joints"]}
        token = rest(self.base, "/api/session", "POST")["token"]
        self.ws = await websockets.connect(ws_url(self.base))
        await self.send("hello", token=token)
        self._reader = asyncio.create_task(self._read_loop())
        self._pinger = asyncio.create_task(self._ping_loop(cfg["watchdog_timeout_sec"]))
        await asyncio.wait_for(self._state.wait(), timeout=10)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        # Ход надо отдать явно: иначе рука останется занятой до таймаута
        # бездействия, и следующий человек упрётся в «управляет кто-то другой».
        if self.controlling:
            try:
                await self.send("release")
            except Exception:
                pass
        for t in (self._pinger, self._reader):
            t.cancel()
        if self.ws:
            await self.ws.close()

    async def send(self, t: str, **fields: Any) -> None:
        await self.ws.send(json.dumps({"t": t, **fields}))

    async def _ping_loop(self, watchdog_sec: float) -> None:
        # С запасом вдвое: пакет может задержаться, а цена промаха — потерянный ход.
        period = max(0.5, watchdog_sec / 2)
        while True:
            await asyncio.sleep(period)
            try:
                await self.send("ping")
            except Exception:
                return

    async def _read_loop(self) -> None:
        async for raw in self.ws:
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            t = m.get("t")
            if t == "state":
                self.joints = m.get("joints") or {}
                self._state.set()
            elif t == "granted":
                self.controlling = True
                self._granted.set()
            elif t == "revoked":
                self.controlling = False
                print(f"\n⚠️  ход потерян: {m.get('reason')}", file=sys.stderr)
            elif t == "queue" and not self.controlling:
                pos = m.get("position")
                # queue приходит на любое изменение очереди, в том числе чужое.
                # Печатаем, только когда сдвинулись мы сами, иначе строка
                # повторяется без нового смысла.
                if pos and pos > 0 and pos != self._last_queue_pos:
                    self._last_queue_pos = pos
                    print(f"   в очереди: {pos}-й, ожидание ~{m.get('eta_sec')} с")
            elif t == "error":
                extra = {k: v for k, v in m.items() if k not in ("t", "code", "message")}
                print(f"❌ {m.get('code')}: {m.get('message')} {extra or ''}", file=sys.stderr)

    async def take_control(self, wait_sec: float) -> None:
        """Получить ход: встать в очередь и дождаться. С --force — отобрать."""
        if self.controlling:
            return
        if self.force:
            token = os.environ.get("ADMIN_TOKEN")
            if not token:
                die("--force требует ADMIN_TOKEN в окружении")
            rest(self.base, "/api/admin/kick", "POST", token=token)
            print("   оператор снят (--force)")
        if not self._enqueued:
            await self.send("enqueue")
            self._enqueued = True
        try:
            await asyncio.wait_for(self._granted.wait(), timeout=wait_sec)
        except asyncio.TimeoutError:
            die(f"ход не получен за {wait_sec:.0f} с — рукой управляет кто-то другой.\n"
                f"   Ждать дольше: --wait 300. Отобрать: --force (нужен ADMIN_TOKEN)")

    def resolve(self, joint: str, value: str, *, relative: bool) -> float:
        if joint not in self.limits:
            die(f"нет такого сустава: {joint}\n   Есть: {', '.join(self.limits)}")
        lim = self.limits[joint]
        try:
            v = float(value)
        except ValueError:
            die(f"не число: {value}")
        if relative:
            v += self.joints.get(joint, 0.0)
        lo, hi = lim["min"], lim["max"]
        if not (lo <= v <= hi):
            clamped = max(lo, min(hi, v))
            print(f"   {v:+.3f} вне предела [{lo:+.3f}, {hi:+.3f}] → {clamped:+.3f}")
            v = clamped
        return v

    async def wait_arrival(self, joint: str, target: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ARRIVE_TIMEOUT
        while loop.time() < deadline:
            cur = self.joints.get(joint)
            if cur is not None and abs(cur - target) <= ARRIVED_EPS:
                print(f"   доехал: {joint} = {cur:+.3f}")
                return
            await asyncio.sleep(0.1)
        cur = self.joints.get(joint)
        print(f"   ⚠️ за {ARRIVE_TIMEOUT:.0f} с не доехал: {joint} = "
              f"{cur:+.3f} при цели {target:+.3f}" if cur is not None else "   ⚠️ нет телеметрии")


# ------------------------------------------------------------------ команды
def print_state(joints: Dict[str, float], limits: Dict[str, Any]) -> None:
    for name in limits:
        v = joints.get(name)
        if v is None:
            continue
        lim = limits[name]
        lo, hi = lim["min"], lim["max"]
        frac = (v - lo) / (hi - lo) if hi > lo else 0.0
        bar = "█" * round(frac * 24)
        print(f"  {name:<14} {v:+7.3f}  {bar:<24} {lim.get('label','')}")


async def cmd_state(a: Arm, _: argparse.Namespace) -> None:
    print_state(a.joints, a.limits)


async def cmd_watch(a: Arm, _: argparse.Namespace) -> None:
    print("Телеметрия 10 Гц. Ctrl-C — выход.\n")
    lines = len([n for n in a.limits if n in a.joints])
    first = True
    while True:
        if not first:
            sys.stdout.write(f"\033[{lines}A")
        print_state(a.joints, a.limits)
        first = False
        await asyncio.sleep(0.2)


async def cmd_move(a: Arm, ns: argparse.Namespace) -> None:
    # `move` — абсолютная цель, `jog` — смещение от текущего. Разными
    # командами, а не флагом: иначе знак минуса читается то как «влево»,
    # то как «в позицию -0.2», и ошибиться можно молча.
    target = a.resolve(ns.joint, ns.value, relative=(ns.cmd == "jog"))
    await a.take_control(ns.wait)
    print(f"   set_joint {ns.joint} → {target:+.3f} рад")
    await a.send("set_joint", joint=ns.joint, position=target)
    await a.wait_arrival(ns.joint, target)


async def cmd_gripper(a: Arm, ns: argparse.Namespace) -> None:
    v = float(ns.value)
    if not 0.0 <= v <= 1.0:
        die("значение схвата — от 0 (закрыт) до 1 (открыт)")
    await a.take_control(ns.wait)
    print(f"   gripper → {v:.2f}")
    await a.send("gripper", value=v)
    await a.wait_arrival("gripper", v)


async def cmd_preset(a: Arm, ns: argparse.Namespace) -> None:
    await a.take_control(ns.wait)
    print(f"   preset {ns.name}")
    await a.send("preset", name=ns.name)
    # Жест играется несколькими точками; ждём, пока движение утихнет.
    await asyncio.sleep(1.0)
    prev = dict(a.joints)
    for _ in range(100):
        await asyncio.sleep(0.3)
        if all(abs(a.joints.get(k, 0) - prev.get(k, 0)) < ARRIVED_EPS for k in a.joints):
            break
        prev = dict(a.joints)
    print("   жест доигран")


async def cmd_shell(a: Arm, ns: argparse.Namespace) -> None:
    await a.take_control(ns.wait)
    print("Ход получен. Команды:\n"
          "  <сустав> <рад>   в абсолютную позицию\n"
          "  j <сустав> <рад> сместить относительно текущей\n"
          "  g <0..1>         схват     p <жест>  жест\n"
          "  s                состояние q         выход\n")
    while True:
        line = (await asyncio.to_thread(sys.stdin.readline)).strip()
        if not line or line in ("q", "quit", "exit"):
            return
        parts = line.split()
        try:
            if parts[0] == "s":
                print_state(a.joints, a.limits)
            elif parts[0] == "g":
                await a.send("gripper", value=float(parts[1]))
            elif parts[0] == "p":
                await a.send("preset", name=parts[1])
            elif parts[0] == "j":
                target = a.resolve(parts[1], parts[2], relative=True)
                await a.send("set_joint", joint=parts[1], position=target)
            else:
                target = a.resolve(parts[0], parts[1], relative=False)
                await a.send("set_joint", joint=parts[0], position=target)
        except (IndexError, ValueError):
            print("   не разобрал; примеры: shoulder_pan 0.3 | j elbow_flex -0.2 | "
                  "g 0.5 | p wave | s | q")


COMMANDS = {
    "state": cmd_state, "watch": cmd_watch, "move": cmd_move, "jog": cmd_move,
    "gripper": cmd_gripper, "preset": cmd_preset, "shell": cmd_shell,
}


def main() -> None:
    p = argparse.ArgumentParser(
        description="Управление рукой SO-101 из терминала через шлюз.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("ПОЧЕМУ")[0])
    p.add_argument("--url", default=os.environ.get("SO101_URL", DEFAULT_URL),
                   help=f"адрес стенда (по умолчанию {DEFAULT_URL})")
    p.add_argument("--wait", type=float, default=60.0,
                   help="сколько секунд ждать своего хода (по умолчанию 60)")
    p.add_argument("--force", action="store_true",
                   help="снять текущего оператора; требует ADMIN_TOKEN в окружении")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state", help="текущие положения суставов")
    sub.add_parser("watch", help="живая телеметрия")
    m = sub.add_parser("move", help="сустав в АБСОЛЮТНУЮ позицию, рад")
    m.add_argument("joint"); m.add_argument("value")
    j = sub.add_parser("jog", help="сместить сустав ОТНОСИТЕЛЬНО текущего, рад")
    j.add_argument("joint"); j.add_argument("value")
    g = sub.add_parser("gripper", help="схват, 0 закрыт … 1 открыт")
    g.add_argument("value")
    pr = sub.add_parser("preset", help="жест: home wave nod shake bow")
    pr.add_argument("name")
    sub.add_parser("shell", help="интерактивный режим")
    ns = p.parse_args()

    async def run() -> None:
        async with Arm(ns.url, force=ns.force) as a:
            await COMMANDS[ns.cmd](a, ns)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
