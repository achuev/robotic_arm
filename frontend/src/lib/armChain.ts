// СГЕНЕРИРОВАНО tools/build_arm_model.py — руками не править.
//
// Кинематика руки из deploy/so101_follower.generated.urdf: та же цепочка,
// по которой шлюз считает прямую кинематику, и та же, что публикует
// robot_state_publisher. Углы приходят в state.joints по WebSocket.
//
// Проверка правильности сборки — src/lib/armChain.test.ts: схват,
// посчитанный по этой цепочке, обязан сойтись с полем `ee`, которое
// сервер присылает независимо (у него TF от ROS).

export type Vec3 = [number, number, number];

export interface ChainJoint {
  name: string;
  type: 'revolute' | 'fixed';
  parent: string;
  child: string;
  /** Смещение сустава относительно родительского звена. */
  xyz: Vec3;
  /** Поворот в порядке URDF: roll-pitch-yaw, т.е. X→Y→Z. */
  rpy: Vec3;
  axis: Vec3;
  limit: [number, number] | null;
}

export interface ChainVisual {
  /** Звено, к которому прикреплён меш: их бывает несколько на звено. */
  link: string;
  /** Имя меша внутри arm.glb. */
  mesh: string;
  /** Смещение визуала относительно звена — НЕ ноль, легко забыть. */
  xyz: Vec3;
  rpy: Vec3;
  material: string;
}

/** Корень цепочки: у него нет родительского сустава. */
export const ROOT_LINK = 'base_link';

/** Звено, по которому сервер считает `ee` (TF base_link → gripper_frame_link). */
export const EE_LINK = 'gripper_frame_link';

export const MATERIALS: Record<string, { color: number; metalness: number; roughness: number }> = {
  '3d_printed': { color: 0xffd11f, metalness: 0.0, roughness: 0.75 },
  'sts3215': { color: 0x1a1a1a, metalness: 0.1, roughness: 0.45 },
};

export const JOINTS: ChainJoint[] = [
  { name: 'shoulder_pan', type: 'revolute', parent: 'base_link', child: 'shoulder_link',
    xyz: [0.0388353, -9e-09, 0.0624], rpy: [3.14159, 0, -3.14159], axis: [0, 0, 1.0], limit: [-1.91986, 1.91986] },
  { name: 'shoulder_lift', type: 'revolute', parent: 'shoulder_link', child: 'upper_arm_link',
    xyz: [-0.0303992, -0.0182778, -0.0542], rpy: [-1.5708, -1.5708, 0], axis: [0, 0, 1.0], limit: [-1.74533, 1.74533] },
  { name: 'elbow_flex', type: 'revolute', parent: 'upper_arm_link', child: 'lower_arm_link',
    xyz: [-0.11257, -0.028, 0], rpy: [0, 0, 1.5708], axis: [0, 0, 1.0], limit: [-1.69, 1.69] },
  { name: 'wrist_flex', type: 'revolute', parent: 'lower_arm_link', child: 'wrist_link',
    xyz: [-0.1349, 0.0052, 0], rpy: [0, 0, -1.5708], axis: [0, 0, 1.0], limit: [-1.65806, 1.65806] },
  { name: 'gripper_frame_joint', type: 'fixed', parent: 'gripper_link', child: 'gripper_frame_link',
    xyz: [-0.0079, -0.000218121, -0.0981274], rpy: [0, 3.14159, 0], axis: [0, 0, 1.0], limit: null },
  { name: 'gripper', type: 'revolute', parent: 'gripper_link', child: 'moving_jaw_so101_v1_link',
    xyz: [0.0202, 0.0188, -0.0234], rpy: [1.5708, -5.2e-08, 0], axis: [0, 0, 1.0], limit: [-0.174533, 1.74533] },
  { name: 'wrist_roll', type: 'revolute', parent: 'wrist_link', child: 'gripper_link',
    xyz: [0, -0.0611, 0.0181], rpy: [1.5708, 0.0486795, 3.14159], axis: [0, 0, 1.0], limit: [-2.74385, 2.84121] },
];

export const VISUALS: ChainVisual[] = [
  { link: 'base_link', mesh: 'base_motor_holder_so101_v1',
    xyz: [-0.00636471, -9.9441e-05, -0.0024], rpy: [1.5708, 0, 1.5708], material: '3d_printed' },
  { link: 'base_link', mesh: 'base_so101_v2',
    xyz: [-0.00636471, -9e-09, -0.0024], rpy: [1.5708, 0, 1.5708], material: '3d_printed' },
  { link: 'base_link', mesh: 'sts3215_03a_v1',
    xyz: [0.0263353, -9e-09, 0.0437], rpy: [0, 0, 0], material: 'sts3215' },
  { link: 'base_link', mesh: 'waveshare_mounting_plate_so101_v2',
    xyz: [-0.0309827, -0.000199441, 0.0474], rpy: [1.5708, 0, 1.5708], material: '3d_printed' },
  { link: 'shoulder_link', mesh: 'sts3215_03a_v1',
    xyz: [-0.0303992, 0.000422241, -0.0417], rpy: [1.5708, 1.5708, 0], material: 'sts3215' },
  { link: 'shoulder_link', mesh: 'motor_holder_so101_base_v1',
    xyz: [-0.0675992, -0.000177759, 0.0158499], rpy: [1.5708, -1.5708, 0], material: '3d_printed' },
  { link: 'shoulder_link', mesh: 'rotation_pitch_so101_v1',
    xyz: [0.0122008, 2.2241e-05, 0.0464], rpy: [-1.5708, 0, 0], material: '3d_printed' },
  { link: 'upper_arm_link', mesh: 'sts3215_03a_v1',
    xyz: [-0.11257, -0.0155, 0.0187], rpy: [-3.14159, 0, -1.5708], material: 'sts3215' },
  { link: 'upper_arm_link', mesh: 'upper_arm_so101_v1',
    xyz: [-0.065085, 0.012, 0.0182], rpy: [3.14159, 0, 0], material: '3d_printed' },
  { link: 'lower_arm_link', mesh: 'under_arm_so101_v1',
    xyz: [-0.0648499, -0.032, 0.0182], rpy: [3.14159, 0, 0], material: '3d_printed' },
  { link: 'lower_arm_link', mesh: 'motor_holder_so101_wrist_v1',
    xyz: [-0.0648499, -0.032, 0.018], rpy: [-3.14159, 0, 0], material: '3d_printed' },
  { link: 'lower_arm_link', mesh: 'sts3215_03a_v1',
    xyz: [-0.1224, 0.0052, 0.0187], rpy: [-3.14159, 0, -3.14159], material: 'sts3215' },
  { link: 'wrist_link', mesh: 'sts3215_03a_no_horn_v1',
    xyz: [0, -0.0424, 0.0306], rpy: [1.5708, 1.5708, 0], material: 'sts3215' },
  { link: 'wrist_link', mesh: 'wrist_roll_pitch_so101_v2',
    xyz: [0, -0.028, 0.0181], rpy: [-1.5708, -1.5708, 0], material: '3d_printed' },
  { link: 'gripper_link', mesh: 'sts3215_03a_v1',
    xyz: [0.0077, 0.0001, -0.0234], rpy: [-1.5708, 0, 0], material: 'sts3215' },
  { link: 'gripper_link', mesh: 'wrist_roll_follower_so101_v1',
    xyz: [0, -0.000218214, 0.000949706], rpy: [-3.14159, 0, 0], material: '3d_printed' },
  { link: 'moving_jaw_so101_v1_link', mesh: 'moving_jaw_so101_v1',
    xyz: [0, 0, 0.0189], rpy: [0, 0, 0], material: '3d_printed' },
];
