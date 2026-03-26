# Drone Control: WEB UI -> Backend: Python MAVLink (WebSocket) -> Virtual drone: PX4 SITL (jMAVSim)

Локальний проєкт для керування віртуальним дроном у **PX4 SITL + jMAVSim** через:

- **FastAPI backend** (`app.py`) — MAVLink (WebSocket) команди до PX4
- **React UI** (`drone-ui`) — кнопки, слайдери, керування з клавіатури

> Важливо: jMAVSim відкривається як **окреме desktop-вікно** (Java), не всередині браузера.

Компоненти:

1. PX4 SITL + jMAVSim — емуляція автопілота та 3D-візуалізація дрона
2. Python Backend (FastAPI) — проксі між вебом та MAVLink (WebSocket), обробка команд
3. Web UI — інтерфейс керування

---

## 1) Вимоги

- macOS / Linux
- Python 3.10+
- Node.js 18+ та npm
- CMake, Ninja, компілятор C/C++
- Java JDK + Ant (для `jMAVSim`)

---

## 2) Install (перший запуск)

### 2.1 Python середовище для PX4 і backend

```bash
cd /path/to/drone
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r Tools/setup/requirements.txt
python -m pip install "empy==3.3.4" pyros-genmsg
```

> `empy==3.3.4` потрібен для сумісності PX4-генераторів (щоб не було `module 'em' has no attribute 'RAW_OPT'`).

### 2.2 jMAVSim залежності (macOS/Homebrew)

```bash
brew install openjdk ant
echo 'export PATH="/opt/homebrew/opt/openjdk/bin:$PATH"' >> ~/.bash_profile
source ~/.bash_profile
java -version
javac -version
ant -version
```

Якщо `jmavsim` таргет не зʼявляється, пересконфігуруйте PX4:

```bash
rm -rf build/px4_sitl_default
make px4_sitl jmavsim
```

---

## 3) Інструкція запуску (обовʼязково в такому порядку)

### Термінал 1 — PX4 SITL + jMAVSim

```bash
make px4_sitl jmavsim
```

Дочекайтесь у консолі PX4 повідомлень типу `Ready for takeoff`.

### Термінал 2 — Backend API

```bash
python3 app.py
```

API стартує на `http://127.0.0.1:8000`.
Якщо папки `static` немає — це ок, backend запуститься без вбудованої роздачі frontend.

### Термінал 3 — Frontend UI

```bash
cd drone-ui
npm install
npm start
```

UI відкривається на `http://127.0.0.1:3000`.

---

## 4) Швидкий сценарій польоту

1. Відкрити UI на `http://127.0.0.1:3000`
2. Натиснути `ARM`
3. Натиснути `ЗЛІТ`
4. Для ручного керування:
   - або `OFFBOARD` + слайдери
   - або зміщення кнопками/клавіатурою (див. нижче)
5. `RESET` — повернення додому (RTL)
6. `LAND` — посадка

---

## 5) Керування з UI

### Кнопки

- `ARM` / `DISARM`
- `ЗЛІТ` / `ПОСАДКА`
- `RESET` — повернення в Home (RTL)
- Перемикання режимів (`OFFBOARD`, `MANUAL`, `ALTCTL`, `POSCTL`, `AUTO`)

### Смещения по осям

Доступні імпульсні кнопки:

- `⬆ Вверх` / `⬇ Вниз`
- `⬅ Влево` / `➡ Вправо`
- `⏩ Вперед` / `⏪ Назад`

### Клавіатура (утримання = плавний рух)

- `←/→` — вліво/вправо
- `↑/↓` — вгору/вниз
- `W/S` — вперед/назад

Поки клавіша затиснута, команди йдуть безперервно; після відпускання — одразу стоп (нейтраль).

---

## 6) Основні API ендпоінти

- `GET /api/telemetry` — телеметрія
- `GET /api/mavlink_link` — діагностика MAVLink каналу
- `POST /api/arm`
- `POST /api/disarm`
- `POST /api/takeoff?altitude=10`
- `POST /api/land`
- `POST /api/reset` — RTL (Return to Launch)
- `POST /api/mode/{mode}`
- `POST /api/nudge` — короткий імпульс зміщення
- `POST /api/manual_axes` — безперервне зміщення
- `POST /api/manual_axes/stop` — стоп безперервного зміщення
- `POST /api/offboard_rates` — одиничний кадр OFFBOARD
- `WS /ws/telemetry`
- `WS /ws/offboard_control`

---

## 7) Типові проблеми та рішення

### 1) Дрон не реагує на команди

- Переконайтесь, що `app.py` запущений **після** старту SITL.
- Перевірте `GET /api/mavlink_link` (має бути активний лінк).
- Подивіться консоль PX4 на `Arming denied` / `Preflight Fail`.

### 2) У браузері “нема симулятора”

- Це нормально: jMAVSim не рендериться в браузері.
- Дивіться окреме вікно jMAVSim.

### 3) `ninja: error: unknown target 'jmavsim'`

- Встановіть `openjdk` і `ant`.
- Очистіть конфігурацію: `rm -rf build/px4_sitl_default`.
- Зберіть знову: `make px4_sitl jmavsim`.

### 4) OFFBOARD не працює

- Спочатку `ARM`.
- Переведіть у режим `OFFBOARD`.
- Увімкніть потік команд у UI (WebSocket).

### 5) Автодизарм

- Якщо PX4 показує preflight/failsafe — спершу усунути причину за логом PX4.

---

## 8) Нотатки для розробки

- Backend файл: `app.py`
- Frontend файл: `drone-ui/src/App.tsx`
- Стилі: `drone-ui/src/App.css`

Перевірка після змін:

```bash
# backend
python3 -m py_compile app.py

# frontend
cd drone-ui
npx tsc --noEmit
```
