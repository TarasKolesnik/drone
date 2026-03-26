// src/App.tsx
import React, { useState, useEffect, useRef } from "react";
import "./App.css";

interface Telemetry {
  latitude: number;
  longitude: number;
  altitude: number;
  roll: number;
  pitch: number;
  yaw: number;
  armed: boolean;
  mode: string;
}

/** CRA dev (:3000) → API на :8000; продакшен з того ж хоста що й FastAPI — відносні шляхи */
function apiOrigin(): string {
  if (typeof window === "undefined") return "";
  return window.location.port === "3000" ? "http://127.0.0.1:8000" : "";
}

function App() {
  const [telemetry, setTelemetry] = useState<Telemetry | null>(null);
  const [connected, setConnected] = useState(false);
  const [commandLog, setCommandLog] = useState<string[]>([]);
  const [lastError, setLastError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  // Підключення WebSocket (через dev-proxy з :3000 на :8000 або той самий хост, що й API)
  useEffect(() => {
    const base = apiOrigin();
    const wsUrl = base
      ? `${base.replace(/^http/, "ws")}/ws/telemetry`
      : `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws/telemetry`;
    const ws = new WebSocket(wsUrl);
    ws.onopen = () => {
      console.log("WebSocket connected");
      setConnected(true);
    };
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      setTelemetry(data);
    };
    ws.onclose = () => {
      console.log("WebSocket disconnected");
      setConnected(false);
    };
    wsRef.current = ws;

    return () => ws.close();
  }, []);

  // API-запити
  const apiRequest = async (endpoint: string, options?: RequestInit) => {
    const response = await fetch(`${apiOrigin()}/api/${endpoint}`, options);
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    return response.json();
  };

  const pushLog = (line: string) => {
    setCommandLog((prev) => [line, ...prev].slice(0, 10));
  };

  /** Виконати команду до PX4 через app.py; показати відповідь або помилку в UI. */
  const runCommand = async (label: string, fn: () => Promise<unknown>) => {
    setLastError(null);
    try {
      const result = await fn();
      const text =
        typeof result === "object" && result !== null
          ? JSON.stringify(result)
          : String(result);
      pushLog(`✓ ${label}: ${text}`);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setLastError(`${label}: ${msg}`);
      pushLog(`✗ ${label}: ${msg}`);
    }
  };

  const arm = () => apiRequest("arm", { method: "POST" });
  const disarm = () => apiRequest("disarm", { method: "POST" });
  const takeoff = () => apiRequest("takeoff?altitude=10", { method: "POST" });
  const land = () => apiRequest("land", { method: "POST" });
  const resetToHome = () => apiRequest("reset", { method: "POST" });
  const nudge = (leftRight: number, upDown: number, forwardBack: number) =>
    apiRequest("nudge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        left_right: leftRight,
        up_down: upDown,
        forward_back: forwardBack,
        duration_s: 0.35,
      }),
    });
  const manualAxes = (leftRight: number, upDown: number, forwardBack: number) =>
    apiRequest("manual_axes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        left_right: leftRight,
        up_down: upDown,
        forward_back: forwardBack,
      }),
    });
  const stopManualAxes = () => apiRequest("manual_axes/stop", { method: "POST" });
  const setMode = (mode: string) =>
    apiRequest(`mode/${mode}`, { method: "POST" });

  /** Нормовані стіки для OFFBOARD: roll/pitch/yaw ∈ [-1,1], throttle ∈ [0,1]. */
  const sticksRef = useRef({ roll: 0, pitch: 0, yaw: 0, throttle: 0.5 });
  const [rollPct, setRollPct] = useState(0);
  const [pitchPct, setPitchPct] = useState(0);
  const [yawPct, setYawPct] = useState(0);
  const [throttlePct, setThrottlePct] = useState(50);
  const [offboardStream, setOffboardStream] = useState(false);
  const pressedArrowsRef = useRef<Set<string>>(new Set());
  const arrowIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const arrowRequestBusyRef = useRef(false);

  function offboardWsUrl(): string {
    const base = apiOrigin();
    if (base) {
      return `${base.replace(/^http/, "ws")}/ws/offboard_control`;
    }
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}/ws/offboard_control`;
  }

  useEffect(() => {
    if (!offboardStream) {
      return;
    }
    const ws = new WebSocket(offboardWsUrl());
    let iv: ReturnType<typeof setInterval> | undefined;
    ws.onopen = () => {
      iv = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          const s = sticksRef.current;
          ws.send(
            JSON.stringify({
              roll: s.roll,
              pitch: s.pitch,
              yaw: s.yaw,
              throttle: s.throttle,
            })
          );
        }
      }, 50);
    };
    ws.onerror = () => {
      setLastError("WebSocket OFFBOARD: помилка з’єднання з app.py");
    };
    return () => {
      if (iv !== undefined) {
        clearInterval(iv);
      }
      ws.close();
    };
  }, [offboardStream]);

  const syncStickRoll = (pct: number) => {
    setRollPct(pct);
    sticksRef.current.roll = pct / 100;
  };
  const syncStickPitch = (pct: number) => {
    setPitchPct(pct);
    sticksRef.current.pitch = pct / 100;
  };
  const syncStickYaw = (pct: number) => {
    setYawPct(pct);
    sticksRef.current.yaw = pct / 100;
  };
  const syncThrottle = (pct: number) => {
    setThrottlePct(pct);
    sticksRef.current.throttle = pct / 100;
  };

  const centerSticks = () => {
    syncStickRoll(0);
    syncStickPitch(0);
    syncStickYaw(0);
    syncThrottle(50);
  };

  useEffect(() => {
    const isMoveKey = (key: string, code: string) =>
      key === "ArrowUp" ||
      key === "ArrowDown" ||
      key === "ArrowLeft" ||
      key === "ArrowRight" ||
      code === "KeyW" ||
      code === "KeyS";

    const computeAxes = () => {
      const keys = pressedArrowsRef.current;
      let leftRight = 0;
      let upDown = 0;
      let forwardBack = 0;
      if (keys.has("ArrowLeft")) leftRight -= 1;
      if (keys.has("ArrowRight")) leftRight += 1;
      if (keys.has("ArrowUp")) upDown += 1;
      if (keys.has("ArrowDown")) upDown -= 1;
      if (keys.has("KeyW")) forwardBack += 1;
      if (keys.has("KeyS")) forwardBack -= 1;
      return { leftRight, upDown, forwardBack };
    };

    const tickManualAxes = () => {
      if (arrowRequestBusyRef.current) return;
      const { leftRight, upDown, forwardBack } = computeAxes();
      if (leftRight === 0 && upDown === 0 && forwardBack === 0) return;
      arrowRequestBusyRef.current = true;
      manualAxes(leftRight, upDown, forwardBack)
        .catch((e) => {
          const msg = e instanceof Error ? e.message : String(e);
          setLastError(`keyboard axes: ${msg}`);
        })
        .finally(() => {
          arrowRequestBusyRef.current = false;
        });
    };

    const startInterval = () => {
      if (arrowIntervalRef.current) return;
      tickManualAxes();
      arrowIntervalRef.current = setInterval(tickManualAxes, 80);
    };

    const stopInterval = () => {
      if (!arrowIntervalRef.current) return;
      clearInterval(arrowIntervalRef.current);
      arrowIntervalRef.current = null;
    };

    const shouldIgnore = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      return !!(
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      );
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (!isMoveKey(event.key, event.code) || shouldIgnore(event)) return;
      event.preventDefault();
      if (event.key.startsWith("Arrow")) {
        pressedArrowsRef.current.add(event.key);
      } else if (event.code === "KeyW" || event.code === "KeyS") {
        pressedArrowsRef.current.add(event.code);
      }
      startInterval();
    };

    const onKeyUp = (event: KeyboardEvent) => {
      if (!isMoveKey(event.key, event.code)) return;
      if (event.key.startsWith("Arrow")) {
        pressedArrowsRef.current.delete(event.key);
      } else if (event.code === "KeyW" || event.code === "KeyS") {
        pressedArrowsRef.current.delete(event.code);
      }
      if (pressedArrowsRef.current.size === 0) {
        stopInterval();
        stopManualAxes().catch(() => {});
      }
    };

    const onBlur = () => {
      pressedArrowsRef.current.clear();
      stopInterval();
      stopManualAxes().catch(() => {});
    };

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlur);
      stopInterval();
    };
  }, []);

  return (
    <div className="app">
      <header className="header">
        <h1>🚁 Віртуальний дрон</h1>
        <div className="header-status">
          <div className={`status ${connected ? "connected" : "disconnected"}`}>
            {connected ? "🟢 WS до API" : "🔴 Немає WS до app.py"}
          </div>
          <span className="status-caption">
            (це не PX4; автопілот іде через MAVLink на :14550)
          </span>
        </div>
      </header>

      {lastError && (
        <div className="error-strip" role="alert">
          {lastError}
        </div>
      )}

      <div className="main-panel">
        {/* jMAVSim — окреме Java-вікно, не HTTP-сторінка; у iframe :8080 нічого не буде */}
        <div className="simulation-window simulation-placeholder">
          <div className="simulation-placeholder-inner">
            <h2>Симулятор 3D</h2>
            <p>
              <strong>jMAVSim</strong> відкривається як окреме вікно програми (Java), а не
              як сайт. Вбудувати його в браузер через <code>iframe</code> неможливо.
            </p>
            <p>
              Запустіть <code>make px4_sitl jmavsim</code> і дивіться вікно симулятора на
              робочому столі.
            </p>
            <p className="sim-hint">
              Для перегляду в браузері використовуйте інший стек (наприклад, Gazebo з
              веб-стримом) або QGroundControl.
            </p>
            <hr className="sim-divider" />
            <h3 className="sim-subtitle">Як керувати з цієї сторінки</h3>
            <ol className="howto-list">
              <li>
                Термінал 1: <code>make px4_sitl jmavsim</code> — дочекайтесь старту PX4.
              </li>
              <li>
                Термінал 2: <code>python3 app.py</code> у каталозі PX4-Autopilot (після SITL).
              </li>
              <li>
                Дивіться рух у <strong>вікні jMAVSim</strong>, не в браузері.
              </li>
              <li>
                <strong>ЗЛІТ</strong> сам виставляє висоту, ALTCTL, arm і режим TAKEOFF. Можна
                спочатку <strong>ARM</strong>, потім <strong>ЗЛІТ</strong>.
              </li>
              <li>
                <strong>OFFBOARD</strong> + слайдери внизу: керування кутовими швидкостями (потік ~20 Hz
                через WebSocket). Спочатку <strong>ARM</strong> (і бажано зліт), натисніть{" "}
                <strong>OFFBOARD</strong>, увімкніть потік.
              </li>
              <li>
                Режими <strong>MANUAL / ALTCTL / POSCTL</strong> без OFFBOARD потребують пульта або QGC.
              </li>
              <li>
                Якщо після <strong>ARM</strong> у телеметрії довго <code>DISARMED</code> — PX4
                відхиляє arm (див. консоль PX4). Переконайтесь, що <code>app.py</code> підключений
                до <code>udpin:0.0.0.0:14550</code> і немає другої програми на цьому порту.
              </li>
            </ol>
          </div>
        </div>

        {/* Панель керування */}
        <div className="control-panel">
          <h2>Керування</h2>
          <div className="button-group">
            <button
              type="button"
              onClick={() => runCommand("ARM", arm)}
              className="btn arm"
            >
              🔓 ARM
            </button>
            <button
              type="button"
              onClick={() => runCommand("DISARM", disarm)}
              className="btn disarm"
            >
              🔒 DISARM
            </button>
            <button
              type="button"
              onClick={() => runCommand("TAKEOFF", takeoff)}
              className="btn takeoff"
            >
              🛫 ЗЛІТ
            </button>
            <button
              type="button"
              onClick={() => runCommand("LAND", land)}
              className="btn land"
            >
              🛬 ПОСАДКА
            </button>
            <button
              type="button"
              onClick={() => {
                centerSticks();
                runCommand("RESET HOME", resetToHome);
              }}
              className="btn reset"
            >
              ↩ RESET
            </button>
          </div>

          <h3>Режими польоту</h3>
          <p className="mode-note">
            Для керування зі слайдерів оберіть <strong>OFFBOARD</strong> (після ARM). Інакше RC/QGC.
          </p>
          <div className="mode-group">
            <button
              type="button"
              onClick={() => runCommand("MODE OFFBOARD", () => setMode("OFFBOARD"))}
              className="btn-mode-offboard"
            >
              OFFBOARD
            </button>
            <button
              type="button"
              onClick={() => runCommand("MODE MANUAL", () => setMode("MANUAL"))}
            >
              MANUAL
            </button>
            <button
              type="button"
              onClick={() => runCommand("MODE ALTCTL", () => setMode("ALTCTL"))}
            >
              ALTCTL
            </button>
            <button
              type="button"
              onClick={() => runCommand("MODE POSCTL", () => setMode("POSCTL"))}
            >
              POSCTL
            </button>
            <button
              type="button"
              onClick={() => runCommand("MODE AUTO→LOITER", () => setMode("AUTO"))}
            >
              AUTO
            </button>
          </div>

          <h3 className="log-heading">Смещение по осям</h3>
          <p className="mode-note">
            Работает как короткий импульс стиков (лучше в ALTCTL/POSCTL, после ARM).
          </p>
          <p className="kbd-hint">
            Гарячі клавіші: ← ↑ ↓ → + W/S (утримання = плавне зміщення, відпускання = стоп)
          </p>
          <div className="nudge-grid">
            <button type="button" onClick={() => runCommand("NUDGE UP", () => nudge(0, 1, 0))}>
              ⬆ Вверх
            </button>
            <button
              type="button"
              onClick={() => runCommand("NUDGE FORWARD", () => nudge(0, 0, 1))}
            >
              ⏩ Вперед
            </button>
            <button
              type="button"
              onClick={() => runCommand("NUDGE LEFT", () => nudge(-1, 0, 0))}
            >
              ⬅ Влево
            </button>
            <button
              type="button"
              onClick={() => runCommand("NUDGE RIGHT", () => nudge(1, 0, 0))}
            >
              ➡ Вправо
            </button>
            <button
              type="button"
              onClick={() => runCommand("NUDGE BACK", () => nudge(0, 0, -1))}
            >
              ⏪ Назад
            </button>
            <button
              type="button"
              onClick={() => runCommand("NUDGE DOWN", () => nudge(0, -1, 0))}
            >
              ⬇ Вниз
            </button>
          </div>

          <h3 className="log-heading">Відповіді API</h3>
          <ul className="command-log">
            {commandLog.length === 0 ? (
              <li className="command-log-empty">Натисніть кнопку — тут з’явиться JSON від app.py</li>
            ) : (
              commandLog.map((line, i) => (
                <li key={`${i}-${line.slice(0, 24)}`}>{line}</li>
              ))
            )}
          </ul>
        </div>

        {/* Панель телеметрії */}
        <div className="telemetry-panel">
          <h2>Телеметрія</h2>
          {telemetry && (
            <table>
              <tbody>
                <tr>
                  <td>📍 Широта:</td>
                  <td>{telemetry.latitude.toFixed(6)}</td>
                </tr>
                <tr>
                  <td>📍 Довгота:</td>
                  <td>{telemetry.longitude.toFixed(6)}</td>
                </tr>
                <tr>
                  <td>📏 Висота:</td>
                  <td>{telemetry.altitude.toFixed(1)} м</td>
                </tr>
                <tr>
                  <td>🔄 Крен:</td>
                  <td>{((telemetry.roll * 180) / Math.PI).toFixed(1)}°</td>
                </tr>
                <tr>
                  <td>⬆️ Тангаж:</td>
                  <td>{((telemetry.pitch * 180) / Math.PI).toFixed(1)}°</td>
                </tr>
                <tr>
                  <td>🧭 Курс:</td>
                  <td>{((telemetry.yaw * 180) / Math.PI).toFixed(1)}°</td>
                </tr>
                <tr>
                  <td>⚙️ Стан:</td>
                  <td>{telemetry.armed ? "ARMED" : "DISARMED"}</td>
                </tr>
                <tr>
                  <td>🎮 Режим:</td>
                  <td>{telemetry.mode}</td>
                </tr>
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="joystick-panel">
        <h3>Ручне керування (OFFBOARD)</h3>
        <p className="joystick-placeholder-note">
          Потік <code>SET_ATTITUDE_TARGET</code> ~20 Hz на <code>/ws/offboard_control</code>. Увімкніть
          після переходу в режим <strong>OFFBOARD</strong>. Зупинка потоку може викликати failsafe
          втрати OFFBOARD у PX4.
        </p>
        <div className="offboard-toolbar">
          <label className="offboard-toggle">
            <input
              type="checkbox"
              checked={offboardStream}
              onChange={(e) => setOffboardStream(e.target.checked)}
            />{" "}
            Потік команд (WebSocket)
          </label>
          <button type="button" className="btn-center-sticks" onClick={centerSticks}>
            Центрувати осі
          </button>
        </div>
        <div className="axis-sliders">
          <label className="axis-row">
            <span className="axis-name">Крен (roll)</span>
            <input
              type="range"
              min={-100}
              max={100}
              value={rollPct}
              onChange={(e) => syncStickRoll(Number(e.target.value))}
            />
            <span className="axis-value">{rollPct}</span>
          </label>
          <label className="axis-row">
            <span className="axis-name">Тангаж (pitch)</span>
            <input
              type="range"
              min={-100}
              max={100}
              value={pitchPct}
              onChange={(e) => syncStickPitch(Number(e.target.value))}
            />
            <span className="axis-value">{pitchPct}</span>
          </label>
          <label className="axis-row">
            <span className="axis-name">Рись (yaw)</span>
            <input
              type="range"
              min={-100}
              max={100}
              value={yawPct}
              onChange={(e) => syncStickYaw(Number(e.target.value))}
            />
            <span className="axis-value">{yawPct}</span>
          </label>
          <label className="axis-row">
            <span className="axis-name">Тяга (throttle)</span>
            <input
              type="range"
              min={0}
              max={100}
              value={throttlePct}
              onChange={(e) => syncThrottle(Number(e.target.value))}
            />
            <span className="axis-value">{throttlePct}%</span>
          </label>
        </div>
      </div>
    </div>
  );
}

export default App;
