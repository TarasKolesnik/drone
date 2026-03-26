# app.py
import asyncio
import os
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pymavlink import mavutil
import json
import threading

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Глобальні змінні
current_telemetry = {
    "latitude": 0.0,
    "longitude": 0.0,
    "altitude": 0.0,
    "roll": 0.0,
    "pitch": 0.0,
    "yaw": 0.0,
    "armed": False,
    "mode": "MANUAL"
}

# PX4: param2 MAV_CMD_COMPONENT_ARM_DISARM (21196) — у новіших збірках для зовнішніх MAVLink-команд
# preflight усе одно виконується (arm(..., from_external || !forced)). На реальному дроні не вмикати «форс» без потреби.
PX4_ARM_FORCE_MAGIC = 21196
# Частота імітації стіків (MANUAL_CONTROL), щоб SITL бачив «RC» без QGC/пульта.
DEFAULT_MANUAL_KEEPALIVE_HZ = 5.0

# SITL: PX4 шле GCS-потік на UDP 14550 (remote). Слухати: udpin:0.0.0.0:14550 або udp:127.0.0.1:14550.
DEFAULT_MAVLINK_URL = "udpin:0.0.0.0:14550"
# Секунди очікування HEARTBEAT (інакше зависання без PX4); 0 = без таймауту (як у pymavlink за замовчуванням).
DEFAULT_HEARTBEAT_TIMEOUT = 30.0


class DroneController:
    def __init__(self):
        self.connection = None
        self.running = False
        self._conn_str = ""
        # Один потік recv + HTTP API не повинні одночасно читати/писати pymavlink без блокування.
        self._mav_lock = threading.Lock()
        self._manual_keepalive_stop = threading.Event()
        self._manual_keepalive_thread: threading.Thread | None = None
        self._manual_keepalive_interval = 1.0 / DEFAULT_MANUAL_KEEPALIVE_HZ
        self._manual_override_until = 0.0

    def connect(self, connection_string=None):
        """Підключення до PX4 SITL. Рядок: MAVLINK_URL або DEFAULT_MAVLINK_URL (udpin на :14550)."""
        connection_string = (
            connection_string
            or os.environ.get("MAVLINK_URL", "").strip()
            or DEFAULT_MAVLINK_URL
        )
        self._conn_str = connection_string
        self.connection = mavutil.mavlink_connection(
            connection_string,
            source_system=255,
        )
        self.running = True

        hb_timeout_raw = os.environ.get("MAVLINK_HEARTBEAT_TIMEOUT", "").strip()
        if hb_timeout_raw == "":
            hb_timeout = DEFAULT_HEARTBEAT_TIMEOUT
        else:
            hb_timeout = float(hb_timeout_raw)

        with self._mav_lock:
            if hb_timeout > 0:
                hb = self.connection.wait_heartbeat(timeout=hb_timeout)
                if hb is None:
                    raise RuntimeError(
                        f"За {hb_timeout}s не отримано MAVLink HEARTBEAT на {connection_string!r}.\n"
                        "  • Спочатку запустіть SITL: make px4_sitl jmavsim (дочекайтесь старту PX4).\n"
                        "  • Потім знову: python3 app.py\n"
                        "  • Якщо порт зайнятий (QGC тощо) — закрийте інші GCS або змініть MAVLINK_URL.\n"
                        "  • Спробуйте: export MAVLINK_URL='udp:127.0.0.1:14550'\n"
                        "  • Без обмеження часу очікування: export MAVLINK_HEARTBEAT_TIMEOUT=0"
                    )
            else:
                self.connection.wait_heartbeat()
            self._sync_targets_from_heartbeat()

        n_clients = (
            len(self.connection.clients)
            if hasattr(self.connection, "clients")
            else -1
        )
        print(
            f"Підключено до PX4: sys={self.connection.target_system} "
            f"comp={self.connection.target_component} | MAVLink: {connection_string} | "
            f"udp_clients={n_clients}"
        )
        if n_clients == 0:
            print(
                "УВАГА: pymavlink не бачить відправника UDP — команди можуть не доходити до PX4. "
                "Перезапустіть app.py після старту SITL."
            )

        with self._mav_lock:
            self._apply_sitl_friendly_params()

        threading.Thread(target=self._update_telemetry, daemon=True).start()
        self._start_manual_keepalive()

    def _manual_keepalive_enabled(self) -> bool:
        ex = os.environ.get("PX4_MANUAL_KEEPALIVE", "").strip().lower()
        if ex in ("0", "false", "no", "off"):
            return False
        return True

    def _send_manual_control_neutral(self) -> None:
        """
        Нейтральні стіки через MAVLink MANUAL_CONTROL → uORB manual_control_input.
        z=500 → throttle у внутрішній шкалі 0 (безпечно для ALTCTL: throttle <= 0.2).
        """
        if not self.connection or not self.running:
            return
        tgt = int(self.connection.target_system) if self.connection.target_system else 0
        self.connection.mav.manual_control_send(
            tgt,
            0,
            0,
            500,
            0,
            0,
            0,
        )

    def _send_manual_control_axes(
        self, pitch: float, roll: float, yaw: float, throttle_norm: float
    ) -> None:
        """MANUAL_CONTROL: pitch/roll/yaw [-1..1], throttle_norm [0..1]."""
        if not self.connection or not self.running:
            return
        tgt = int(self.connection.target_system) if self.connection.target_system else 0

        def clamp(v: float, lo: float, hi: float) -> float:
            return max(lo, min(hi, float(v)))

        x = int(clamp(pitch, -1.0, 1.0) * 1000.0)
        y = int(clamp(roll, -1.0, 1.0) * 1000.0)
        z = int(clamp(throttle_norm, 0.0, 1.0) * 1000.0)
        r = int(clamp(yaw, -1.0, 1.0) * 1000.0)
        self.connection.mav.manual_control_send(tgt, x, y, z, r, 0, 0)

    def _manual_keepalive_loop(self) -> None:
        interval = self._manual_keepalive_interval
        while self.running and not self._manual_keepalive_stop.is_set():
            if time.monotonic() < self._manual_override_until:
                if self._manual_keepalive_stop.wait(timeout=interval):
                    break
                continue
            with self._mav_lock:
                self._send_manual_control_neutral()
            if self._manual_keepalive_stop.wait(timeout=interval):
                break

    def _start_manual_keepalive(self) -> None:
        if not self._manual_keepalive_enabled():
            print("PX4_MANUAL_KEEPALIVE вимкнено — без пульта/QGC можливі відмови arm (health / manual).")
            return
        hz = DEFAULT_MANUAL_KEEPALIVE_HZ
        raw = os.environ.get("PX4_MANUAL_KEEPALIVE_HZ", "").strip()
        if raw:
            try:
                v = float(raw)
                if v > 0:
                    hz = v
            except ValueError:
                pass
        self._manual_keepalive_interval = 1.0 / hz
        self._manual_keepalive_stop.clear()
        self._manual_keepalive_thread = threading.Thread(
            target=self._manual_keepalive_loop, daemon=True, name="mav-manual-keepalive"
        )
        self._manual_keepalive_thread.start()
        print(f"Фонова імітація MANUAL_CONTROL (~{hz:.1f} Hz) — для arm у SITL без RC/QGC.")

    def _sync_targets_from_heartbeat(self) -> None:
        """PX4: pymavlink не виставляє target_component з HEARTBEAT — лишається 0, команди ігноруються."""
        hb = self.connection.messages.get("HEARTBEAT")
        if hb is None:
            return
        sys_id = hb.get_srcSystem()
        comp_id = hb.get_srcComponent()
        self.connection.target_system = sys_id
        if comp_id in (0, mavutil.mavlink.MAV_COMP_ID_ALL):
            comp_id = mavutil.mavlink.MAV_COMP_ID_AUTOPILOT1
        self.connection.target_component = comp_id

    def _fc_component(self) -> int:
        """Компонент автопілота для COMMAND_LONG (0 у багатьох випадках «ламає» PX4)."""
        c = self.connection.target_component
        if c in (0, mavutil.mavlink.MAV_COMP_ID_ALL):
            return mavutil.mavlink.MAV_COMP_ID_AUTOPILOT1
        return c

    def _px4_set_mode(self, mode_name: str) -> None:
        """
        PX4: MAV_CMD_DO_SET_MODE з px4_map.
        Не використовує MAVFile.set_mode() — там fallback на ArduPilot, якщо HEARTBEAT
        ще не в self.messages, і команди ігноруються.
        """
        if mode_name not in mavutil.px4_map:
            raise ValueError(f"Немає режиму {mode_name!r} у pymavlink px4_map")
        base, main, sub = mavutil.px4_map[mode_name]
        self.connection.mav.command_long_send(
            self.connection.target_system,
            self._fc_component(),
            mavutil.mavlink.MAV_CMD_DO_SET_MODE,
            0,
            float(base),
            float(main),
            float(sub),
            0.0,
            0.0,
            0.0,
            0.0,
        )

    def _param_set_real32(self, name: str, value: float) -> None:
        bid = name.encode("utf-8")
        bid = (bid + b"\0" * 16)[:16]
        self.connection.mav.param_set_send(
            self.connection.target_system,
            mavutil.mavlink.MAV_COMP_ID_AUTOPILOT1,
            bid,
            value,
            mavutil.mavlink.MAV_PARAM_TYPE_REAL32,
        )

    def _sitl_params_enabled(self) -> bool:
        ex = os.environ.get("PX4_SITL_DEFAULT_PARAMS", "").strip().lower()
        if ex in ("0", "false", "no", "off"):
            return False
        return True

    def _apply_sitl_friendly_params(self) -> None:
        """
        Після підключення до SITL: зменшує типові «обриви» польоту з app.py без QGC.
        Вимкнути: PX4_SITL_DEFAULT_PARAMS=0
        """
        if not self._sitl_params_enabled() or not self.connection:
            return
        # COM_DISARM_PRFLT < 0: не дизармувати через N с після arm, якщо ще не був зліт
        # (інакше у консолі: Disarmed by auto preflight disarming).
        self._param_set_real32("COM_DISARM_PRFLT", -1.0)
        time.sleep(0.06)
        # Дозволити arm при слабкому GPS (у логах: Preflight: GPS fix too low).
        self._param_set_real32("COM_ARM_WO_GPS", 2.0)
        time.sleep(0.06)
        # Симулятор батареї: не опускати відсоток нижче 50% (дефолт), але підстрахувати мінімум.
        self._param_set_real32("SIM_BAT_MIN_PCT", 60.0)
        time.sleep(0.06)
        print(
            "Надіслано SITL-параметри: COM_DISARM_PRFLT=-1, COM_ARM_WO_GPS=2, SIM_BAT_MIN_PCT=60 "
            "(PX4_SITL_DEFAULT_PARAMS=0 щоб вимкнути)."
        )

    def _update_telemetry(self):
        """Один recv за захоплення lock — інакше API/MAV_CMD чекають до ~1.5 s і здається, що PX4 «не чує»."""
        global current_telemetry
        while self.running:
            msg = None
            with self._mav_lock:
                msg = self.connection.recv_match(blocking=True, timeout=0.25)
            if msg is None:
                continue
            mtype = msg.get_type()
            if mtype == "GLOBAL_POSITION_INT":
                current_telemetry["latitude"] = msg.lat / 1e7
                current_telemetry["longitude"] = msg.lon / 1e7
                current_telemetry["altitude"] = msg.relative_alt / 1000.0
            elif mtype == "ATTITUDE":
                current_telemetry["roll"] = msg.roll
                current_telemetry["pitch"] = msg.pitch
                current_telemetry["yaw"] = msg.yaw
            elif mtype == "HEARTBEAT":
                current_telemetry["armed"] = (msg.base_mode & 128) != 0
                current_telemetry["mode"] = mavutil.mode_string_v10(msg)

    def _sitl_force_arm(self) -> bool:
        """
        Force-arm (param2=21196) для обходу pre-arm у SITL.
        За замовчуванням увімкнено для localhost і для MAVLink на :14550 (типовий GCS-порт SITL,
        зокрема udpin:0.0.0.0:14550 — там немає '127.0.0.1' у рядку).
        Вимкнути: PX4_SITL_FORCE_ARM=0
        """
        ex = os.environ.get("PX4_SITL_FORCE_ARM", "").strip().lower()
        if ex in ("0", "false", "no", "off"):
            return False
        if ex in ("1", "true", "yes", "on"):
            return True
        s = self._conn_str.lower()
        if "127.0.0.1" in s or "localhost" in s:
            return True
        if ":14550" in s:
            return True
        return False

    def _send_arm(self, arm: bool) -> float:
        p2 = float(PX4_ARM_FORCE_MAGIC) if (arm and self._sitl_force_arm()) else 0.0
        self.connection.mav.command_long_send(
            self.connection.target_system,
            self._fc_component(),
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0,
            1.0 if arm else 0.0,
            p2,
            0,
            0,
            0,
            0,
            0,
        )
        return p2

    def arm(self):
        """Армування дрона (ввімкнення моторів)"""
        with self._mav_lock:
            p2 = self._send_arm(True)
        return {"status": "arming", "force_arm_param2": p2}

    def disarm(self):
        """Дизармування"""
        with self._mav_lock:
            self._send_arm(False)
        return {"status": "disarming"}

    def takeoff(self, altitude=10):
        """
        PX4 MC: MIS_TAKEOFF_ALT (відносна висота зліту) → ALTCTL → arm → TAKEOFF → NAV_TAKEOFF.

        У MAV_CMD_NAV_TAKEOFF поля param5/6/7 у PX4 — lat/lon та **AMSL**, не «метри над землею».
        Раніше param7=10 означало 10 м MSL (над рівнем моря); у SITL дрон часто вже на сотнях метрів
        AMSL → WARN «Already higher than takeoff altitude». Тому lat/lon/alt у команді — NaN: navigator
        лишає поточну позицію й бере ціль як поточна_глобальна_висота + MIS_TAKEOFF_ALT.
        """
        with self._mav_lock:
            self._param_set_real32("MIS_TAKEOFF_ALT", float(altitude))
            time.sleep(0.08)
            self._px4_set_mode("ALTCTL")
            time.sleep(0.15)
            p2 = self._send_arm(True)
            time.sleep(0.15)
            self._px4_set_mode("TAKEOFF")
            time.sleep(0.05)
            nan = float("nan")
            self.connection.mav.command_long_send(
                self.connection.target_system,
                self._fc_component(),
                mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                0,
                0.0,
                0.0,
                0.0,
                0.0,
                nan,
                nan,
                nan,
            )
        return {
            "status": "takeoff",
            "altitude": altitude,
            "force_arm_param2": p2,
            "sequence": "MIS_TAKEOFF_ALT → ALTCTL → arm → TAKEOFF → NAV_TAKEOFF (param5–7 NaN → відносний зліт)",
        }

    def land(self):
        """Посадка (PX4): режим LAND."""
        with self._mav_lock:
            self._px4_set_mode("LAND")
        return {"status": "landing"}

    def reset_to_home(self):
        """Повернення в home через RTL (MAV_CMD_NAV_RETURN_TO_LAUNCH)."""
        with self._mav_lock:
            self.connection.mav.command_long_send(
                self.connection.target_system,
                self._fc_component(),
                mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH,
                0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
            )
        return {"status": "reset_to_home", "reason": "MAV_CMD_NAV_RETURN_TO_LAUNCH"}

    def nudge_manual(
        self,
        left_right: float,
        up_down: float,
        forward_back: float,
        duration_s: float = 0.35,
    ):
        """
        Короткий «поштовх» стіками в ALTCTL/POSCTL:
        left_right: -1..1 (вліво..вправо), up_down: -1..1 (вниз..вгору),
        forward_back: -1..1 (назад..вперед).
        """
        lr = max(-1.0, min(1.0, float(left_right)))
        ud = max(-1.0, min(1.0, float(up_down)))
        fb = max(-1.0, min(1.0, float(forward_back)))
        duration_s = max(0.1, min(1.0, float(duration_s)))
        throttle = 0.5 + (ud * 0.35)  # 0.5 ~= нейтраль
        roll = lr * 0.5
        pitch = fb * 0.5
        yaw = 0.0

        end_ts = time.monotonic() + duration_s
        while time.monotonic() < end_ts:
            with self._mav_lock:
                self._send_manual_control_axes(pitch, roll, yaw, throttle)
            time.sleep(0.05)  # ~20 Hz

        with self._mav_lock:
            self._send_manual_control_neutral()
        return {
            "status": "nudged",
            "left_right": lr,
            "up_down": ud,
            "forward_back": fb,
            "duration_s": duration_s,
        }

    def set_manual_axes(self, left_right: float, up_down: float, forward_back: float):
        """Безперервне зміщення: викликається часто (поки кнопка/клавіша затиснута)."""
        lr = max(-1.0, min(1.0, float(left_right)))
        ud = max(-1.0, min(1.0, float(up_down)))
        fb = max(-1.0, min(1.0, float(forward_back)))
        throttle = 0.5 + (ud * 0.35)
        roll = lr * 0.5
        pitch = fb * 0.5
        with self._mav_lock:
            self._send_manual_control_axes(pitch, roll, 0.0, throttle)
            self._manual_override_until = time.monotonic() + 0.25
        return {
            "status": "manual_axes",
            "left_right": lr,
            "up_down": ud,
            "forward_back": fb,
        }

    def stop_manual_axes(self):
        """Повернути стіки в нейтраль після відпускання клавіші/кнопки."""
        with self._mav_lock:
            self._send_manual_control_neutral()
            self._manual_override_until = 0.0
        return {"status": "manual_axes_stopped"}

    def _px4_mode_alias(self, mode: str) -> str:
        """Імена з UI → ключі pymavlink px4_map (AUTO там немає)."""
        m = (mode or "").upper().strip()
        aliases = {
            "AUTO": "LOITER",
        }
        return aliases.get(m, m)

    def set_mode(self, mode: str):
        """Зміна режиму польоту (PX4, напряму через px4_map)."""
        px4_name = self._px4_mode_alias(mode)
        if px4_name not in mavutil.px4_map:
            return {
                "ok": False,
                "error": f"Невідомий режим для PX4: {px4_name}",
                "known": sorted(mavutil.px4_map.keys()),
            }
        with self._mav_lock:
            self._px4_set_mode(px4_name)
        return {"ok": True, "status": f"mode_set_{px4_name}"}

    def set_offboard_body_rates(
        self,
        roll_rate: float,
        pitch_rate: float,
        yaw_rate: float,
        thrust: float,
    ) -> None:
        """
        SET_ATTITUDE_TARGET: ігноруємо кватерніон, задаємо кутові швидкості тіла (rad/s) і тягу 0..1.
        У PX4 застосовується лише в NAVIGATION_STATE_OFFBOARD (див. mavlink_receiver).
        """
        m = mavutil.mavlink
        type_mask = m.ATTITUDE_TARGET_TYPEMASK_ATTITUDE_IGNORE
        q = (1.0, 0.0, 0.0, 0.0)
        thrust = max(0.0, min(1.0, float(thrust)))
        t_ms = int(time.monotonic() * 1000) & 0xFFFFFFFF
        with self._mav_lock:
            self.connection.mav.set_attitude_target_send(
                t_ms,
                self.connection.target_system,
                self._fc_component(),
                type_mask,
                q,
                float(roll_rate),
                float(pitch_rate),
                float(yaw_rate),
                thrust,
            )


def _offboard_rate_limits():
    max_rp = float(os.environ.get("OFFBOARD_MAX_RP_RATE", "1.2"))
    max_y = float(os.environ.get("OFFBOARD_MAX_YAW_RATE", "0.9"))
    return max_rp, max_y


def _normalized_rates_to_physical(nr: float, np: float, ny: float, nt: float):
    max_rp, max_y = _offboard_rate_limits()
    nr = max(-1.0, min(1.0, nr))
    np = max(-1.0, min(1.0, np))
    ny = max(-1.0, min(1.0, ny))
    nt = max(0.0, min(1.0, nt))
    return nr * max_rp, np * max_rp, ny * max_y, nt


class OffboardStickInput(BaseModel):
    """Нормовані стіки: roll/pitch/yaw ∈ [-1,1], throttle ∈ [0,1] (0.5 ≈ вісь по центру)."""

    roll: float = Field(0.0, ge=-1.0, le=1.0)
    pitch: float = Field(0.0, ge=-1.0, le=1.0)
    yaw: float = Field(0.0, ge=-1.0, le=1.0)
    throttle: float = Field(0.5, ge=0.0, le=1.0)


class NudgeInput(BaseModel):
    """Смещение: left_right, up_down, forward_back (-1..1), duration_s (сек)."""

    left_right: float = Field(0.0, ge=-1.0, le=1.0)
    up_down: float = Field(0.0, ge=-1.0, le=1.0)
    forward_back: float = Field(0.0, ge=-1.0, le=1.0)
    duration_s: float = Field(0.35, ge=0.1, le=1.0)


class ManualAxesInput(BaseModel):
    """Осі для безперервного зміщення (утримання клавіші)."""

    left_right: float = Field(0.0, ge=-1.0, le=1.0)
    up_down: float = Field(0.0, ge=-1.0, le=1.0)
    forward_back: float = Field(0.0, ge=-1.0, le=1.0)

# Ініціалізація контролера
drone = DroneController()
drone.connect()

# REST API ендпоінти
@app.get("/api/telemetry")
async def get_telemetry():
    """Отримання телеметрії"""
    return current_telemetry


@app.get("/api/mavlink_link")
async def mavlink_link():
    """Діагностика: чи pymavlink знає адресу відповіді UDP (інакше команди «в нікуди»)."""
    m = drone.connection
    return {
        "mavlink_url": drone._conn_str,
        "target_system": m.target_system,
        "target_component": m.target_component,
        "fc_component_used": drone._fc_component(),
        "udp_clients": list(getattr(m, "clients", []) or []),
    }

@app.post("/api/arm")
async def arm():
    print("Arming drone")
    return await asyncio.to_thread(drone.arm)

@app.post("/api/disarm")
async def disarm():
    print("Disarming drone")
    return await asyncio.to_thread(drone.disarm)

@app.post("/api/takeoff")
async def takeoff(altitude: int = 10):
    return await asyncio.to_thread(drone.takeoff, altitude)

@app.post("/api/land")
async def land():
    return await asyncio.to_thread(drone.land)

@app.post("/api/reset")
async def reset():
    """RTL: повернення до home (ісходної позиції)."""
    return await asyncio.to_thread(drone.reset_to_home)

@app.post("/api/mode/{mode}")
async def set_mode(mode: str):
    return await asyncio.to_thread(drone.set_mode, mode)

@app.post("/api/offboard_rates")
async def offboard_rates(body: OffboardStickInput):
    """Одиничний кадр OFFBOARD (кутові швидкості). Режим літака має бути OFFBOARD."""
    rr, pr, yr, tr = _normalized_rates_to_physical(
        body.roll, body.pitch, body.yaw, body.throttle
    )
    await asyncio.to_thread(drone.set_offboard_body_rates, rr, pr, yr, tr)
    return {"ok": True, "rates_rad_s": [rr, pr, yr], "thrust": tr}


@app.post("/api/nudge")
async def nudge(body: NudgeInput):
    """Коротке зміщення дрона у поточному ручному режимі (ALTCTL/POSCTL)."""
    return await asyncio.to_thread(
        drone.nudge_manual, body.left_right, body.up_down, body.forward_back, body.duration_s
    )


@app.post("/api/manual_axes")
async def manual_axes(body: ManualAxesInput):
    """Безперервне зміщення (викликати періодично поки клавіша затиснута)."""
    return await asyncio.to_thread(
        drone.set_manual_axes, body.left_right, body.up_down, body.forward_back
    )


@app.post("/api/manual_axes/stop")
async def manual_axes_stop():
    """Зупинити безперервне зміщення та повернути нейтраль."""
    return await asyncio.to_thread(drone.stop_manual_axes)


# WebSocket для реального часу
@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(current_telemetry)
            await asyncio.sleep(0.1)  # 10 Hz
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@app.websocket("/ws/offboard_control")
async def websocket_offboard_control(websocket: WebSocket):
    """
    Клієнт шле JSON ~10–50 Hz: {"roll":0,"pitch":0,"yaw":0,"throttle":0.5} (нормовані стіки).
    Потрібен режим OFFBOARD (і зазвичай ARM).
    """
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                continue
            nr = float(data.get("roll", 0.0))
            np = float(data.get("pitch", 0.0))
            ny = float(data.get("yaw", 0.0))
            nt = float(data.get("throttle", 0.5))
            rr, pr, yr, tr = _normalized_rates_to_physical(nr, np, ny, nt)
            await asyncio.to_thread(drone.set_offboard_body_rates, rr, pr, yr, tr)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# Роздача статичних файлів (шлях відносно app.py, не CWD)
# Якщо frontend не зібраний у `static`, не падаємо при старті backend.
if STATIC_DIR.exists():
    app.mount(
        "/",
        StaticFiles(directory=str(STATIC_DIR), html=True),
        name="static",
    )
else:
    print(
        f"[WARN] Static directory not found: {STATIC_DIR}. "
        "Starting API/WebSocket server without static frontend."
    )

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)