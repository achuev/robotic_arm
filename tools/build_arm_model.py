#!/usr/bin/env python3
"""Сборка 3D-модели руки для сайта: URDF + STL → GLB + TypeScript-цепочка.

    tools/.venv-mesh/bin/python tools/build_arm_model.py

Делает две вещи из одного источника (`deploy/so101_follower.generated.urdf`),
чтобы геометрия и кинематика не разъехались:

  1. `frontend/public/arm.glb`      — пять уникальных мешей, децимированных
  2. `frontend/src/lib/armChain.ts` — суставы, звенья, материалы

ПОЧЕМУ ПЯТЬ, А НЕ СЕМЬ. Визуалов в URDF семь, но `sts3215_03a_v1.stl`
используется тремя звеньями (плечо, предплечье, схват). В GLB он лежит один
раз, а на сцене инстанцируется — иначе платили бы за него трижды.

ПОЧЕМУ ПЕРЕД ДЕЦИМАЦИЕЙ `merge_vertices()`. STL не хранит связность: это
«суп треугольников», где у каждой грани свои три вершины. Упрощатель на таком
входе не может схлопнуть ребро и молча возвращает исходный меш — на 142 000
треугольников это выглядит как «децимация не работает». После сшивки вершин
те же меши ужимаются до ~40 000.

ИМЕНА МЕШЕЙ БЕЗ РАСШИРЕНИЯ (MESH_NAME_NOTE). GLTFLoader прогоняет имена узлов
через `PropertyBinding.sanitizeNodeName`, который заменяет '.', '-' и пробелы
на '_'. Меш, названный `sts3215_03a_v1.stl`, приезжает в сцену как
`sts3215_03a_v1_stl`, и поиск по исходному имени молча не находит ничего —
рука просто не появляется, без единой ошибки. Поэтому в GLB и в armChain.ts
кладётся основа имени, без расширения.

ЕДИНИЦЫ. URDF задан в метрах, `scale` у мешей не выставлен. Скрипт проверяет
габарит базы: если он вылезает за разумные рамки, значит STL в миллиметрах и
модель приехала бы в сто раз больше сцены.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Tuple

try:
    import numpy as np
    import trimesh
    import fast_simplification
except ImportError:
    sys.exit(
        "Нужны trimesh, numpy, fast_simplification:\n"
        "    python3 -m venv tools/.venv-mesh\n"
        "    tools/.venv-mesh/bin/pip install trimesh numpy fast_simplification"
    )

REPO = Path(__file__).resolve().parent.parent
URDF = REPO / "deploy" / "so101_follower.generated.urdf"
MESH_DIR = REPO / "upstream" / "so101_description" / "meshes"
OUT_GLB = REPO / "frontend" / "public" / "arm.glb"
OUT_TS = REPO / "frontend" / "src" / "lib" / "armChain.ts"

#: Доля треугольников после упрощения. 0.08 упирается в предел качества
#: (получается ~40 000 на все пять мешей) — ниже упрощатель уже не идёт,
#: а выше растёт вес без заметной разницы на экране телефона.
KEEP = 0.08

#: Габарит самой большой детали, метры. Санитарная проверка единиц.
SANE_EXTENT_M = 0.5


def f3(raw: str | None, default=(0.0, 0.0, 0.0)) -> Tuple[float, float, float]:
    if not raw:
        return default
    parts = [float(x) for x in raw.split()]
    if len(parts) != 3:
        raise SystemExit(f"ожидались три числа, получено {raw!r}")
    return (parts[0], parts[1], parts[2])


def num(x: float) -> str:
    """Короткая запись: URDF полон значений вида 8.32667e-17 — это ноль."""
    if abs(x) < 1e-12:
        return "0"
    return repr(round(x, 9))


def vec(v) -> str:
    return "[" + ", ".join(num(c) for c in v) + "]"


# --------------------------------------------------------------------------- URDF
def parse_urdf() -> Tuple[List[dict], List[dict], Dict[str, tuple]]:
    if not URDF.is_file():
        raise SystemExit(f"нет {URDF}")
    root = ET.parse(URDF).getroot()

    materials: Dict[str, tuple] = {}
    for m in root.iter("material"):
        c = m.find("color")
        if c is not None and m.get("name") and m.get("name") not in materials:
            rgba = [float(x) for x in c.get("rgba").split()]
            materials[m.get("name")] = tuple(rgba)

    links: List[dict] = []
    for link in root.findall("link"):
        # ВСЕ визуалы звена, а не первый. У base_link их четыре: держатель
        # мотора, корпус, сам сервопривод и монтажная плата. Взять только
        # первый — значит собрать руку из одних сервоприводов, висящих в
        # воздухе, и при этом не получить ни одной ошибки.
        for vis in link.findall("visual"):
            mesh = vis.find("geometry/mesh")
            if mesh is None:
                continue
            origin = vis.find("origin")
            mat = vis.find("material")
            scale = mesh.get("scale")
            if scale and any(abs(s - 1.0) > 1e-9 for s in f3(scale)):
                raise SystemExit(f"{link.get('name')}: mesh scale={scale} не поддержан")
            links.append({
                "name": link.get("name"),
                # без расширения: см. MESH_NAME_NOTE ниже
                "mesh": Path(mesh.get("filename")).stem,
                "file": Path(mesh.get("filename")).name,
                "xyz": f3(origin.get("xyz") if origin is not None else None),
                "rpy": f3(origin.get("rpy") if origin is not None else None),
                "material": mat.get("name") if mat is not None else None,
            })

    joints: List[dict] = []
    for j in root.findall("joint"):
        origin = j.find("origin")
        axis = j.find("axis")
        limit = j.find("limit")
        joints.append({
            "name": j.get("name"),
            "type": j.get("type"),
            "parent": j.find("parent").get("link"),
            "child": j.find("child").get("link"),
            "xyz": f3(origin.get("xyz") if origin is not None else None),
            "rpy": f3(origin.get("rpy") if origin is not None else None),
            "axis": f3(axis.get("xyz") if axis is not None else None, (0.0, 0.0, 1.0)),
            "limit": (
                (float(limit.get("lower")), float(limit.get("upper")))
                if limit is not None and limit.get("lower") is not None else None
            ),
        })
    return links, joints, materials


# --------------------------------------------------------------------------- GLB
def build_glb(links: List[dict]) -> None:
    unique = sorted({(l["mesh"], l["file"]) for l in links})
    print(f"Уникальных мешей: {len(unique)} (визуалов {len(links)})")

    scene = trimesh.Scene()
    total_before = total_after = 0
    for name, filename in unique:
        path = MESH_DIR / filename
        if not path.is_file():
            raise SystemExit(f"нет меша {path}")
        m = trimesh.load(path, force="mesh")
        m.merge_vertices()                       # без этого упрощатель бессилен
        before = len(m.faces)

        v, fc = fast_simplification.simplify(
            m.vertices.astype(np.float32), m.faces.astype(np.int32),
            target_reduction=1.0 - KEEP,
        )
        d = trimesh.Trimesh(vertices=v, faces=fc)
        d.fix_normals()

        extent = float(np.max(d.extents))
        if extent > SANE_EXTENT_M:
            raise SystemExit(
                f"{filename}: габарит {extent:.3f} м — похоже, STL в миллиметрах, "
                "а URDF в метрах. Модель приехала бы в 1000 раз больше."
            )

        total_before += before
        total_after += len(d.faces)
        print(f"  {filename:<38} {before:>7} → {len(d.faces):>6} треуг.  габарит {extent*100:5.1f} см")
        # geom_name задаёт имя меша в GLB — по нему фронтенд его и находит.
        scene.add_geometry(d, geom_name=name, node_name=name)

    OUT_GLB.parent.mkdir(parents=True, exist_ok=True)
    data = scene.export(file_type="glb")
    OUT_GLB.write_bytes(data)
    print(f"\n{OUT_GLB.relative_to(REPO)}")
    print(f"  треугольников: {total_before} → {total_after}")
    print(f"  без сжатия: {len(data)/1024:.0f} KB на диске, "
          f"{len(gzip.compress(data))/1024:.0f} KB по сети")

    compress(len(data))


def compress(raw_size: int) -> None:
    """Сжимает GLB через gltfpack (meshopt).

    Даёт примерно четырёхкратный выигрыш при ТОЙ ЖЕ геометрии: экономия идёт
    от квантования вершин и упаковки индексов, а не от выбрасывания
    треугольников. Децимация к этому моменту уже упёрлась в предел — меши
    из CAD неманифолдные, и упрощатель ниже ~94 000 треугольников не идёт,
    сколько ни проси.

    Флага `-si` (доп. упрощение) здесь намеренно НЕТ: он даёт ещё вдвое, но
    уже заметен на схвате.

    `-kn` и `-km` обязательны. По умолчанию gltfpack СЛИВАЕТ меши с общим
    материалом в один и выбрасывает имена — файл становится ещё меньше, но
    сцена перестаёт находить детали по имени, и рука не появляется вовсе.

    Читать такой файл умеет только загрузчик с MeshoptDecoder — он подключён
    в ArmScene.ts и лежит локально в three.js, без обращений к CDN.
    """
    gltfpack = shutil.which("gltfpack") or str(REPO / "frontend/node_modules/.bin/gltfpack")
    if not Path(gltfpack).exists():
        print("  ⚠️  gltfpack не найден — модель осталась несжатой.")
        print("      cd frontend && npm install")
        return

    packed = OUT_GLB.with_suffix(".packed.glb")
    result = subprocess.run(
        [gltfpack, "-i", str(OUT_GLB), "-o", str(packed), "-cc", "-kn", "-km"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not packed.exists():
        print(f"  ⚠️  gltfpack отказался: {result.stderr.strip()[:200]}")
        packed.unlink(missing_ok=True)
        return

    data = packed.read_bytes()
    packed.replace(OUT_GLB)
    print(f"  meshopt:    {len(data)/1024:.0f} KB на диске, "
          f"{len(gzip.compress(data))/1024:.0f} KB по сети "
          f"(было {raw_size/1024:.0f} KB)")


# ---------------------------------------------------------------------------- TS
def build_ts(links: List[dict], joints: List[dict], materials: Dict[str, tuple]) -> None:
    used_materials = {l["material"] for l in links if l["material"]}
    lines = [
        "// СГЕНЕРИРОВАНО tools/build_arm_model.py — руками не править.",
        "//",
        "// Кинематика руки из deploy/so101_follower.generated.urdf: та же цепочка,",
        "// по которой шлюз считает прямую кинематику, и та же, что публикует",
        "// robot_state_publisher. Углы приходят в state.joints по WebSocket.",
        "//",
        "// Проверка правильности сборки — src/lib/armChain.test.ts: схват,",
        "// посчитанный по этой цепочке, обязан сойтись с полем `ee`, которое",
        "// сервер присылает независимо (у него TF от ROS).",
        "",
        "export type Vec3 = [number, number, number];",
        "",
        "export interface ChainJoint {",
        "  name: string;",
        "  type: 'revolute' | 'fixed';",
        "  parent: string;",
        "  child: string;",
        "  /** Смещение сустава относительно родительского звена. */",
        "  xyz: Vec3;",
        "  /** Поворот в порядке URDF: roll-pitch-yaw, т.е. X→Y→Z. */",
        "  rpy: Vec3;",
        "  axis: Vec3;",
        "  limit: [number, number] | null;",
        "}",
        "",
        "export interface ChainVisual {",
        "  /** Звено, к которому прикреплён меш: их бывает несколько на звено. */",
        "  link: string;",
        "  /** Имя меша внутри arm.glb. */",
        "  mesh: string;",
        "  /** Смещение визуала относительно звена — НЕ ноль, легко забыть. */",
        "  xyz: Vec3;",
        "  rpy: Vec3;",
        "  material: string;",
        "}",
        "",
        "/** Корень цепочки: у него нет родительского сустава. */",
        "export const ROOT_LINK = 'base_link';",
        "",
        "/** Звено, по которому сервер считает `ee` (TF base_link → gripper_frame_link). */",
        "export const EE_LINK = 'gripper_frame_link';",
        "",
        "export const MATERIALS: Record<string, { color: number; metalness: number; roughness: number }> = {",
    ]
    for name in sorted(used_materials):
        r, g, b, _a = materials[name]
        hexcol = "0x%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
        # Сервоприводы — крашеный пластик с бликом, печатные детали — матовые.
        metal, rough = (0.1, 0.45) if name == "sts3215" else (0.0, 0.75)
        lines.append(f"  '{name}': {{ color: {hexcol}, metalness: {metal}, roughness: {rough} }},")
    lines += ["};", "", "export const JOINTS: ChainJoint[] = ["]
    for j in joints:
        lim = "null" if j["limit"] is None else f"[{num(j['limit'][0])}, {num(j['limit'][1])}]"
        lines.append(
            f"  {{ name: '{j['name']}', type: '{j['type']}', "
            f"parent: '{j['parent']}', child: '{j['child']}',\n"
            f"    xyz: {vec(j['xyz'])}, rpy: {vec(j['rpy'])}, "
            f"axis: {vec(j['axis'])}, limit: {lim} }},"
        )
    lines += ["];", "", "export const VISUALS: ChainVisual[] = ["]
    for l in links:
        lines.append(
            f"  {{ link: '{l['name']}', mesh: '{l['mesh']}',\n"
            f"    xyz: {vec(l['xyz'])}, rpy: {vec(l['rpy'])}, material: '{l['material']}' }},"
        )
    lines += ["];", ""]

    OUT_TS.parent.mkdir(parents=True, exist_ok=True)
    OUT_TS.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n{OUT_TS.relative_to(REPO)}")
    print(f"  суставов: {len(joints)} (подвижных {sum(1 for j in joints if j['type']=='revolute')}), "
          f"визуалов: {len(links)} на {len({l['name'] for l in links})} звеньев")


def main() -> int:
    links, joints, materials = parse_urdf()
    build_glb(links)
    build_ts(links, joints, materials)
    print("\nГотово. Пересобрать сайт: cd frontend && npm run build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
