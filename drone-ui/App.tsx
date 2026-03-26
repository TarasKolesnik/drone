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

function apiOrigin(): string {
  if (typeof window === "undefined") return "";
  return window.location.port === "3000" ? "http://127.0.0.1:8000" : "";
}

function App() {
  const [telemetry, setTelemetry] = useState<Telemetry | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

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

  const arm = () => apiRequest("arm", { method: "POST" });
  const disarm = () => apiRequest("disarm", { method: "POST" });
  const takeoff = () => apiRequest("takeoff?altitude=10", { method: "POST" });
  const land = () => apiRequest("land", { method: "POST" });
  const setMode = (mode: string) =>
    apiRequest(`mode/${mode}`, { method: "POST" });

  return (
    <div className="app">
      <header className="header">
        <h1>🚁 Віртуальний дрон</h1>
        <div className={`status ${connected ? "connected" : "disconnected"}`}>
          {connected ? "🟢 Підключено" : "🔴 Не підключено"}
        </div>
      </header>

      <div className="main-panel">
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
          </div>
        </div>

        {/* Панель керування */}
        <div className="control-panel">
          <h2>Керування</h2>
          <div className="button-group">
            <button onClick={arm} className="btn arm">
              🔓 ARM
            </button>
            <button onClick={disarm} className="btn disarm">
              🔒 DISARM
            </button>
            <button onClick={takeoff} className="btn takeoff">
              🛫 ЗЛІТ
            </button>
            <button onClick={land} className="btn land">
              🛬 ПОСАДКА
            </button>
          </div>

          <h3>Режими польоту</h3>
          <div className="mode-group">
            <button onClick={() => setMode("MANUAL")}>MANUAL</button>
            <button onClick={() => setMode("ALTCTL")}>ALTCTL</button>
            <button onClick={() => setMode("POSCTL")}>POSCTL</button>
            <button onClick={() => setMode("AUTO")}>AUTO</button>
          </div>
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

      {/* Джойстик для ручного керування */}
      <div className="joystick-panel">
        <h3>Ручне керування</h3>
        <div className="joystick-grid">
          <div className="joystick">
            <div className="joystick-label">Pitch / Roll</div>
            {/* Тут можна додати canvas-джойстик */}
          </div>
          <div className="joystick">
            <div className="joystick-label">Yaw / Throttle</div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;
