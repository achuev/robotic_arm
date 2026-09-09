/**
 * Проверка, что `public/arm.glb` и `armChain.ts` не разъехались.
 *
 * Оба файла делает один скрипт из одного URDF, но модель лежит в `public/`
 * и в сборку попадает как есть — если её забыть пересобрать, приложение
 * соберётся, типы сойдутся, а на экране не хватит звеньев. Ошибка видна
 * только глазами и только на той вкладке, куда мало кто зайдёт.
 *
 * GLB читается напрямую: это 12 байт заголовка, затем чанки, первый из
 * которых — JSON структуры glTF. Тянуть ради этого GLTFLoader (а с ним
 * половину three.js и подобие DOM) незачем.
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { VISUALS } from './armChain';

const GLB_PATH = fileURLToPath(new URL('../../public/arm.glb', import.meta.url));

/** Имена мешей из JSON-чанка GLB. */
function meshNames(): string[] {
  const buf = readFileSync(GLB_PATH);

  expect(buf.readUInt32LE(0)).toBe(0x46546c67); // 'glTF'
  expect(buf.readUInt32LE(4)).toBe(2);          // версия контейнера
  expect(buf.readUInt32LE(8)).toBe(buf.length); // длина совпадает с файлом

  const chunkLength = buf.readUInt32LE(12);
  const chunkType = buf.readUInt32LE(16);
  expect(chunkType).toBe(0x4e4f534a);           // 'JSON'

  const gltf = JSON.parse(buf.subarray(20, 20 + chunkLength).toString('utf8'));

  // Имя может стоять на меше (обычный экспорт) либо на узле (после
  // gltfpack -kn, который переносит его на родителя). Сцена умеет оба
  // варианта, поэтому и проверяем объединение.
  const named = [
    ...(gltf.meshes ?? []).map((m: { name?: string }) => m.name),
    ...(gltf.nodes ?? []).map((n: { name?: string }) => n.name),
  ];
  return named.filter((n): n is string => typeof n === 'string' && n.length > 0);
}

describe('arm.glb', () => {
  const names = meshNames();

  it('содержит меш для каждого звена цепочки', () => {
    for (const visual of VISUALS) {
      expect(names, `нет меша ${visual.mesh} для звена ${visual.link}`).toContain(visual.mesh);
    }
  });

  it('не тащит лишних мешей', () => {
    const needed = new Set(VISUALS.map((v) => v.mesh));
    for (const name of names) expect(needed.has(name)).toBe(true);
  });

  it('хранит уникальные меши, а не по одному на визуал', () => {
    // sts3215_03a_v1 используется пятью визуалами. Если в файле окажется
    // по мешу на визуал — модель потяжелела впустую.
    const unique = new Set(VISUALS.map((v) => v.mesh));
    expect(new Set(names).size).toBe(unique.size);
    expect(unique.size).toBeLessThan(VISUALS.length);
  });

  it('имена мешей переживают санитизацию GLTFLoader', () => {
    // three.js прогоняет имена узлов через PropertyBinding.sanitizeNodeName,
    // который заменяет '.', '-' и пробелы на '_'. Имя с точкой (например
    // с расширением .stl) в сцене окажется другим, поиск промахнётся, и рука
    // не появится БЕЗ единой ошибки в консоли. Один раз уже наступили.
    for (const name of names) expect(name).toMatch(/^[A-Za-z0-9_]+$/);
  });

  it('весит столько, сколько не жалко отдать телефону', () => {
    // Порог с запасом: сейчас ~1.7 МБ (663 КБ по сети). Срабатывает, если поднять
    // долю треугольников в build_arm_model.py и не заметит последствий.
    const bytes = readFileSync(GLB_PATH).length;
    expect(bytes).toBeLessThan(2_200_000);
  });
});
