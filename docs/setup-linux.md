# Развёртывание стенда на Linux-ПК с нуля

Что и зачем ставится на машину, которая будет стоять под столом рядом с рукой.
Проверено на **Ubuntu 24.04.4 LTS (noble), x86_64** — той самой машине, где
стенд поднят и проверен целиком в режиме `hardware_type:=mock`.

Разделение простое: **стенд работает в Docker**, а ROS 2 на самой машине нужен
для отладки — посмотреть граф, потрогать топики, открыть RViz. Стенд без
нативного ROS поднимется; отлаживать его без него неудобно.

---

## 1. Docker — на нём работает стенд

Штатный репозиторий Docker, а не `docker.io` из Ubuntu: в дистрибутивном
пакете нет плагина `compose`, а оба compose-файла проекта написаны в формате
Compose Spec (без ключа `version:`, `build.context` смотрит наружу).

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
                        docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Проверка: `docker --version` → 29.x, `docker compose version` → v5.x.

### Работа без sudo — и почему это не действует сразу

```bash
sudo usermod -aG docker $USER
```

**Новая группа появляется только в новом сеансе входа.** В уже открытом
терминале `docker ps` будет отвечать `permission denied while trying to
connect to the docker API` — и это не сломанная установка, а незачитанная
группа. Либо перелогиньтесь, либо в текущем терминале:

```bash
sg docker -c 'docker compose -f deploy/compose.dev.yml ps'
```

---

## 2. ROS 2 Jazzy на самой машине — для отладки

Jazzy — потому что это дистрибутив под Ubuntu 24.04, и ровно он же лежит в
`deploy/Dockerfile.ros` (`FROM ros:jazzy-ros-base`). Разъезд версий между
машиной и образом даёт расхождение типов сообщений на ровном месте.

```bash
sudo add-apt-repository -y universe
sudo curl -fsSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key \
     -o /usr/share/keyrings/ros-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] \
http://packages.ros.org/ros2/ubuntu $(. /etc/os-release && echo $UBUNTU_CODENAME) main" \
  | sudo tee /etc/apt/sources.list.d/ros2.list > /dev/null
sudo apt-get update
sudo apt-get install -y ros-jazzy-desktop ros-dev-tools
```

`ros-jazzy-desktop` — это `ros2` CLI, RViz и `robot_state_publisher`, но **не**
`ros2_control` и **не** `xacro`. Пакеты, которые нужны этому проекту, ставятся
отдельно:

```bash
sudo apt-get install -y \
  ros-jazzy-ros2-control ros-jazzy-ros2-controllers ros-jazzy-controller-manager \
  ros-jazzy-joint-state-broadcaster ros-jazzy-forward-command-controller \
  ros-jazzy-joint-trajectory-controller ros-jazzy-ros2controlcli \
  ros-jazzy-xacro ros-jazzy-web-video-server ros-jazzy-tf-transformations \
  ros-jazzy-cv-bridge ros-jazzy-image-transport python3-colcon-common-extensions
```

`forward-command-controller` в списке не случайно: `follower.launch.py` по
умолчанию поднимает `arm_controller:=forward_controller`, а его тип не объявлен
ни в одном `package.xml` — без пакета контроллер не спавнится. Ровно тот же
список стоит в `deploy/Dockerfile.ros`, шаг 4.

Подключение (в `~/.bashrc` добавлять не обязательно, но удобно):

```bash
source /opt/ros/jazzy/setup.bash
```

### Заглянуть внутрь стенда

Контейнеры общаются по локальной петле своего сетевого пространства
(`ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST`), поэтому нативный `ros2 topic list`
на хосте графа стенда **не увидит** — и это правильно, стенд не должен
переговариваться с чужими нодами в гостевой сети. Внутрь ходят через контейнер:

```bash
docker compose -f deploy/compose.dev.yml exec ros /entrypoint.sh bash -lc 'ros2 node list'
```

Нативный ROS остаётся для своих экспериментов, RViz и разбора bag-файлов.

---

## 3. Node.js — сборка фронтенда

```bash
sudo apt-get install -y nodejs npm     # 18.x из noble, vite 6 и vitest 3 с ним работают
cd frontend && npm ci && npm run build
```

`frontend/dist` монтируется в nginx как том, в образ не копируется: пересборка
сайта не требует пересборки образов.

---

## 4. Python-окружения (не в контейнерах)

```bash
sudo apt-get install -y python3-venv

# шлюз: тесты и запуск без ROS (sim-бэкенд)
python3 -m venv gateway/.venv
gateway/.venv/bin/pip install -e 'gateway[dev]'

# табличка с QR
python3 -m venv deploy/.venv
deploy/.venv/bin/pip install -r deploy/requirements-qr.txt
```

---

## 5. Поднять и проверить

```bash
cd frontend && npm run build && cd ..
docker compose -f deploy/compose.dev.yml build      # первая сборка долгая
docker compose -f deploy/compose.dev.yml up -d
```

Дальше — [`docs/runbook.md`](runbook.md) §3: быстрый круг проверок.

Адрес для QR — адрес машины **в сети зала**, не `localhost`:

```bash
hostname -I | awk '{print $1}'
deploy/.venv/bin/python deploy/qr.py "http://<этот-ip>:8080" -o tablet.pdf
```

---

## 6. Ловушки, на которые уже наступили

| Симптом | Причина | Лечение |
|---|---|---|
| `docker ps` → `permission denied ... docker.sock` сразу после `usermod -aG docker` | Группа читается при входе в сеанс | Перелогиниться или `sg docker -c '...'` |
| `tsc: Permission denied`, `npm run build` не идёт; в `deploy/.venv/bin/python` вместо симлинка текст, начинающийся с `XSym` | Дерево скопировано с macOS/SMB-шары: симлинки приехали заглушками XSym, а venv'ы указывают на `/Library/Frameworks/Python.framework` | `rm -rf frontend/node_modules */.venv`, затем `npm ci` и пересоздать venv'ы. Найти все следы: `grep -rl --binary-files=text -m1 '^XSym' .` |
| `dpkg -l \| grep '^ii  ros-jazzy'` показывает 0, хотя `/opt/ros/jazzy` уже есть | apt ещё в фазе configure: файлы распакованы, пакеты в состоянии `iU` | Дождаться выхода `apt-get`; проверять по `^ii`, а не по наличию каталога |
| `ros2 doctor` жалуется `Error importing numpy: you should not try to import numpy from its source directory` | То же самое: половина ROS-пакетов ещё не сконфигурирована | Дождаться конца установки |
| `ros-jazzy-desktop` встал, а `ros2 launch so101_bringup ...` не находит контроллеры | В `desktop` нет `ros2_control` и `xacro` | Доставить список из §2 |

---

## 7. 3D-модель руки на сайте

Модель (`frontend/public/arm.glb`) и кинематика для неё
(`frontend/src/lib/armChain.ts`) **генерируются**, в репозиторий кладётся
результат. Пересобирать нужно только если менялся URDF руки или её меши.

```bash
python3 -m venv tools/.venv-mesh                       # один раз
tools/.venv-mesh/bin/pip install trimesh numpy fast_simplification scipy
cd frontend && npm install && cd ..                    # нужен gltfpack

tools/.venv-mesh/bin/python tools/build_arm_model.py
cd frontend && npm run build
```

После пересборки URDF стоит обновить и эталон для тестов — он снимается
с поднятого стенда:

```bash
gateway/.venv/bin/python tools/capture_fk_fixtures.py
cd frontend && npm test
```

### Из чего складывается вес

| | по сети (gzip) |
|---|---|
| основной бандл сайта | 76 КБ — **не растёт**, 3D в него не входит |
| чанк `ArmScene` (three.js + декодер meshopt) | 164 КБ |
| `arm.glb` | 175 КБ |

Последние две строки грузятся, только когда посетитель открыл вкладку
«Модель». Стенд раздаёт их по локальной сети, так что это доли секунды.

### Грабли, на которые наступили

Все три дают одинаковый симптом — **модель не появляется, ошибок нет**.

| Причина | Как выглядит | Лечение |
|---|---|---|
| Имя меша содержит точку (`sts3215_03a_v1.stl`) | GLTFLoader прогоняет имена через `sanitizeNodeName`, точка становится `_`, поиск промахивается | Имена без расширения; проверяется тестом в `armModel.test.ts` |
| У звена берётся один визуал вместо всех | Рука собирается из одних сервоприводов, висящих в воздухе: у `base_link` четыре визуала, у остальных по два-три | Обходить `link.findall("visual")`; проверяется тестом «рука собрана, а не рассыпана» |
| Из сжатой модели вытаскивается голая геометрия | `gltfpack` квантует вершины, а масштаб распаковки кладёт в узел. Без него деталь оказывается в тысячи раз больше сцены и уходит за кадр. Попытка «запечь» масштаб через `applyMatrix4` портит квантованный атрибут | Клонировать узел целиком, а не геометрию |

Отдельно: `gltfpack` без флагов `-kn -km` сливает меши с общим материалом
в один и выбрасывает имена — файл меньше, но сцене нечего искать.
