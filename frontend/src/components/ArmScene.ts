/**
 * Сцена three.js с рукой SO-101. Императивный слой, отдельно от React.
 *
 * React здесь не годится: состояние приходит десять раз в секунду, и
 * пересоздавать дерево объектов на каждый кадр — верный способ уронить fps на
 * телефоне. Компонент (`ArmView3D.tsx`) создаёт сцену один раз и потом только
 * зовёт `setJoints`.
 *
 * Загружается ленивым чанком: three.js весит около 160 КБ по сети, и лендингу
 * он не нужен — только экрану управления, и только если человек открыл вкладку
 * «модель».
 *
 * ОСИ. У ROS Z — вверх, у three.js — Y. Вместо того чтобы переписывать
 * кинематику, разворачивается корневая группа: так матрицы из `fk.ts`
 * остаются ровно теми же, что в URDF, и сверяются с эталоном напрямую.
 */

import {
  AmbientLight,
  Color,
  DirectionalLight,
  GridHelper,
  Group,
  Matrix4,
  Mesh,
  MeshStandardMaterial,
  PerspectiveCamera,
  Scene,
  Vector3,
  WebGLRenderer,
  type Object3D,
} from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
// Декодер meshopt лежит в самом three.js (7 КБ по сети) и собирается в чанк.
// Никаких обращений к CDN: стенд работает в локальной сети, интернета у него
// может не быть вовсе.
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';

import { VISUALS, MATERIALS } from '../lib/armChain';
import { visualTransforms } from '../lib/fk';

export interface ArmSceneTheme {
  background: number;
  grid: number;
  gridAccent: number;
}

export const THEMES: Record<'dark' | 'light', ArmSceneTheme> = {
  dark: { background: 0x0e1118, grid: 0x1f2733, gridAccent: 0x2c3746 },
  light: { background: 0xf6f7f9, grid: 0xd8dde5, gridAccent: 0xc2c9d4 },
};

/** Путь к модели. Лежит в `public/`, собирается tools/build_arm_model.py. */
const MODEL_URL = 'arm.glb';

export class ArmScene {
  private readonly renderer: WebGLRenderer;
  private readonly scene = new Scene();
  private readonly camera: PerspectiveCamera;
  private readonly controls: OrbitControls;
  private readonly root = new Group();
  /** По одному узлу на визуал, в порядке VISUALS. */
  private readonly visualNodes: Group[] = [];
  private readonly scratch = new Matrix4();

  private frame = 0;
  private disposed = false;
  private pending: Record<string, number> | null = null;
  private needsRender = true;

  constructor(canvas: HTMLCanvasElement, theme: ArmSceneTheme) {
    this.renderer = new WebGLRenderer({ canvas, antialias: true, alpha: false });
    // Телефоны бывают с devicePixelRatio 3–4; рендерить в таком разрешении
    // незачем — греется батарея, а разницы на этой геометрии не видно.
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

    this.camera = new PerspectiveCamera(38, 1, 0.01, 20);
    this.camera.position.set(0.55, 0.42, 0.55);

    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.12;
    this.controls.enablePan = false;      // прохожему панорама только мешает
    this.controls.minDistance = 0.25;
    this.controls.maxDistance = 1.6;
    // Под стол не пускаем: снизу рука выглядит как каша из мешей.
    this.controls.maxPolarAngle = Math.PI / 2 - 0.05;
    this.controls.target.set(0, 0.12, 0);
    this.controls.addEventListener('change', () => { this.needsRender = true; });

    // ROS: Z вверх, X вперёд. three.js: Y вверх. Отсюда поворот корня.
    this.root.rotation.x = -Math.PI / 2;
    this.scene.add(this.root);

    this.scene.add(new AmbientLight(0xffffff, 1.6));
    const key = new DirectionalLight(0xffffff, 2.2);
    key.position.set(0.6, 1.0, 0.5);
    this.scene.add(key);
    const fill = new DirectionalLight(0xffffff, 0.7);
    fill.position.set(-0.5, 0.3, -0.6);
    this.scene.add(fill);

    this.applyTheme(theme);
    this.controls.update();
  }

  applyTheme(theme: ArmSceneTheme): void {
    this.scene.background = new Color(theme.background);
    const old = this.scene.getObjectByName('grid');
    if (old) {
      this.scene.remove(old);
      (old as GridHelper).geometry.dispose();
    }
    const grid = new GridHelper(1.2, 12, theme.gridAccent, theme.grid);
    grid.name = 'grid';
    this.scene.add(grid);
    this.needsRender = true;
  }

  /** Загружает GLB и раскладывает детали по визуалам. */
  async load(base: string): Promise<void> {
    // Модель сжата gltfpack -cc (см. tools/build_arm_model.py): без
    // декодера загрузчик молча отдаст сцену без единого меша.
    const loader = new GLTFLoader().setMeshoptDecoder(MeshoptDecoder);
    const gltf = await loader.loadAsync(base + MODEL_URL);
    if (this.disposed) return;

    // Узлы модели по имени: тринадцать штук на семнадцать визуалов —
    // sts3215 переиспользуется пятью, ради этого в файле и лежат уникальные
    // меши, а не по одному на визуал.
    //
    // Берём УЗЕЛ ЦЕЛИКОМ и клонируем его, а не вытаскиваем голую геометрию.
    // Причина: gltfpack квантует координаты вершин в целые числа, а масштаб
    // распаковки кладёт в трансформацию узла. Вытащенная отдельно геометрия
    // теряет этот масштаб, а попытка «запечь» его через applyMatrix4 портит
    // квантованный атрибут — деталь исчезает без единой ошибки. Клон узла
    // сохраняет и трансформацию, и семантику атрибутов.
    //
    // Имя ищется у узла ЛИБО у ближайшего именованного предка, причём предок
    // важнее: GLTFLoader не оставляет безымянные меши безымянными, он
    // раздаёт им `mesh_0`, `mesh_1`, … и «своё имя, иначе родительское»
    // подхватило бы этот мусор.
    const templates = new Map<string, Object3D>();
    const collect = (obj: Object3D, inherited: string) => {
      const name = inherited || obj.name;
      if (name && !templates.has(name) && hasMesh(obj)) templates.set(name, obj);
      for (const child of obj.children) collect(child, name);
    };
    collect(gltf.scene, '');

    if (!templates.size) {
      // Сообщение важнее, чем кажется: «модель не появилась» без списка имён
      // отлаживается часами — файл при этом целый и грузится успешно.
      console.warn('[arm3d] в модели не нашлось ни одной именованной детали');
    }

    const materials = new Map<string, MeshStandardMaterial>();
    for (const [name, spec] of Object.entries(MATERIALS)) {
      materials.set(name, new MeshStandardMaterial({
        color: spec.color,
        metalness: spec.metalness,
        roughness: spec.roughness,
      }));
    }

    for (const visual of VISUALS) {
      const template = templates.get(visual.mesh);
      if (!template) {
        console.warn(
          `[arm3d] в модели нет детали ${visual.mesh} для ${visual.link}; ` +
          `есть: ${[...templates.keys()].join(', ') || '(ничего)'}`,
        );
      }
      const group = new Group();
      group.matrixAutoUpdate = false;      // матрицу задаём сами, из FK
      if (template) {
        const instance = template.clone();
        // Материалы из URDF, а не из GLB: там их нет, а цвета звеньев
        // (жёлтый пластик, чёрные сервоприводы) заданы в описании робота.
        const material = materials.get(visual.material);
        instance.traverse((obj) => {
          const mesh = obj as Mesh;
          if (mesh.isMesh && material) mesh.material = material;
        });
        group.add(instance);
      }
      this.root.add(group);
      // Узел заводится даже без детали: индексы обязаны совпадать с VISUALS,
      // иначе матрицы разъедутся по чужим мешам.
      this.visualNodes.push(group);
    }

    this.setJoints(this.pending ?? {});
    this.frameCamera();
    this.needsRender = true;
  }

  /** Новые углы суставов. Дёшево: только пересчёт матриц, без аллокаций дерева. */
  setJoints(values: Record<string, number>): void {
    this.pending = values;
    if (!this.visualNodes.length) return;

    const matrices = visualTransforms(values);
    for (let i = 0; i < matrices.length; i++) {
      const group = this.visualNodes[i];
      const matrix = matrices[i];
      if (!group) continue;
      // fk.ts отдаёт строчный порядок — ровно тот, что принимает set().
      this.scratch.set(
        matrix[0], matrix[1], matrix[2], matrix[3],
        matrix[4], matrix[5], matrix[6], matrix[7],
        matrix[8], matrix[9], matrix[10], matrix[11],
        matrix[12], matrix[13], matrix[14], matrix[15],
      );
      group.matrix.copy(this.scratch);
      group.matrixWorldNeedsUpdate = true;
    }
    this.needsRender = true;
  }

  /**
   * Ставит камеру на всю рабочую зону руки.
   *
   * Рамка НЕ зависит от текущей позы. Кадрирование по габариту загруженной
   * модели выглядит аккуратнее в первый момент, но привязывает вид к той позе,
   * в которой рука оказалась при открытии вкладки: стоит повернуть основание —
   * и она наполовину уезжает за край. Здесь взята сфера вокруг всей зоны
   * досягаемости, и рука остаётся в кадре в любой позе.
   */
  private frameCamera(): void {
    // Вылет руки около 0.40 м (проверено по габариту модели), плюс запас.
    const REACH = 0.46;
    // Смотрим в середину рабочей высоты, а не в основание: иначе рука
    // занимает верхнюю половину кадра, а нижняя уходит под пустой стол.
    const target = new Vector3(0, 0.16, 0);
    this.controls.target.copy(target);
    const dir = new Vector3(1, 0.62, 1).normalize();
    this.camera.position.copy(target).addScaledVector(dir, REACH * 1.75);
    this.controls.update();
  }

  resize(width: number, height: number): void {
    if (width <= 0 || height <= 0) return;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.needsRender = true;
  }

  start(): void {
    const tick = () => {
      if (this.disposed) return;
      this.frame = requestAnimationFrame(tick);
      // Инерция орбиты сама просит кадры, пока затухает.
      const damping = this.controls.update();
      if (this.needsRender || damping) {
        this.renderer.render(this.scene, this.camera);
        this.needsRender = false;
      }
    };
    this.frame = requestAnimationFrame(tick);
  }

  dispose(): void {
    this.disposed = true;
    cancelAnimationFrame(this.frame);
    this.controls.dispose();
    this.scene.traverse((obj) => {
      const mesh = obj as Mesh;
      if (mesh.isMesh) {
        mesh.geometry?.dispose();
        const m = mesh.material;
        if (Array.isArray(m)) m.forEach((x) => x.dispose());
        else m?.dispose();
      }
    });
    this.renderer.dispose();
  }
}

/** Содержит ли узел хоть один меш — пустые группы нам не нужны. */
function hasMesh(obj: Object3D): boolean {
  let found = false;
  obj.traverse((o) => { if ((o as Mesh).isMesh) found = true; });
  return found;
}

/** Есть ли WebGL. На отказ показываем камеру, а не пустой чёрный прямоугольник. */
export function webglAvailable(): boolean {
  try {
    const canvas = document.createElement('canvas');
    return !!(canvas.getContext('webgl2') || canvas.getContext('webgl'));
  } catch {
    return false;
  }
}
