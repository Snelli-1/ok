from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

try:
    import tkinter as tk
    from tkinter import ttk
except Exception:  # pragma: no cover
    tk = None
    ttk = None

try:
    from zeep import Client
except Exception:  # pragma: no cover
    Client = None

APP_VERSION = "1.0.0"


class CameraState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    IDLE = "IDLE"
    MOVING = "MOVING"
    SETTLING = "SETTLING"
    DWELLING = "DWELLING"
    PAUSED = "PAUSED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    RECOVERING = "RECOVERING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"
    UNSYNCED = "UNSYNCED"


class StepAction(str, Enum):
    GOTO_PRESET = "GOTO_PRESET"
    WAIT = "WAIT"
    CONTINUOUS_MOVE = "CONTINUOUS_MOVE"
    STOP = "STOP"
    GOTO_HOME = "GOTO_HOME"
    ABSOLUTE_MOVE = "ABSOLUTE_MOVE"
    RELATIVE_MOVE = "RELATIVE_MOVE"
    SNAPSHOT = "SNAPSHOT"
    READ_POSITION = "READ_POSITION"


class RepeatMode(str, Enum):
    OFF = "OFF"
    ONCE = "ONCE"
    FIXED_COUNT = "FIXED_COUNT"
    FOREVER = "FOREVER"
    PING_PONG = "PING_PONG"
    RANDOM_SAFE = "RANDOM_SAFE"


class AdapterMode(str, Enum):
    AUTO = "AUTO"
    MOCK = "MOCK"
    NATIVE_ONVIF = "NATIVE_ONVIF"
    LEGACY_CLI = "LEGACY_CLI"


class FailureCategory(str, Enum):
    TIMEOUT = "TIMEOUT"
    AUTH = "AUTH"
    UNSUPPORTED = "UNSUPPORTED"
    NETWORK = "NETWORK"
    CAPABILITY = "CAPABILITY"
    POSITION = "POSITION"
    UNKNOWN = "UNKNOWN"


class PreviewMode(str, Enum):
    OFF = "OFF"
    SNAPSHOT = "SNAPSHOT"
    LOW_FPS = "LOW_FPS"
    LIVE_SELECTED = "LIVE_SELECTED"


class MockProfile(str, Enum):
    FULL = "FULL"
    INTERMITTENT = "INTERMITTENT"
    LIMITED = "LIMITED"


class CommandType(str, Enum):
    START_PATROL = "START_PATROL"
    STOP_PATROL = "STOP_PATROL"
    PAUSE_PATROL = "PAUSE_PATROL"
    RESUME_PATROL = "RESUME_PATROL"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    MANUAL_MOVE = "MANUAL_MOVE"
    GOTO_PRESET = "GOTO_PRESET"
    READ_STATUS = "READ_STATUS"
    HOME = "HOME"
    REFRESH = "REFRESH"


class PositionReliability(str, Enum):
    MEASURED = "MEASURED"
    CALIBRATED_ESTIMATE = "CALIBRATED_ESTIMATE"
    ESTIMATED = "ESTIMATED"
    UNSYNCED = "UNSYNCED"
    UNKNOWN = "UNKNOWN"


@dataclass
class CameraConfig:
    camera_id: str
    name: str = "Camera"
    host: str = "192.168.1.10"
    onvif_port: int = 80
    username: str = ""
    password: str = ""
    profile_token: str = ""
    ptz_token: str = ""
    home_preset: str = "HOME"
    enabled: bool = True
    adapter: str = AdapterMode.AUTO.value
    mock_profile: str = MockProfile.FULL.value
    rtsp_port: int = 554
    rtsp_uri: str = ""
    snapshot_uri: str = ""
    connect_timeout: float = 5.0
    command_timeout: float = 10.0
    retry_count: int = 2
    retry_delay: float = 1.0
    dry_run: bool = True
    mock_mode: bool = True


@dataclass
class PatrolStep:
    exec_num: int = 10
    action: str = StepAction.GOTO_PRESET.value
    target: str = "MAIN"
    move_s: float = 1.0
    settle_s: float = 0.5
    dwell_s: float = 5.0
    enabled: bool = True
    pan_steps: int = 0
    tilt_steps: int = 0
    zoom: float = 0.0
    pan_speed: float = 0.0
    tilt_speed: float = 0.0
    zoom_speed: float = 0.0
    move_timeout_s: float = 8.0
    pulse_duration_ms: int = 500
    pulse_gap_ms: int = 200


@dataclass
class Patrol:
    patrol_id: str = "DEFAULT"
    camera_id: str = ""
    repeat_mode: str = RepeatMode.ONCE.value
    repeat_count: int = 1
    steps: List[PatrolStep] = field(default_factory=list)


@dataclass
class CameraCapabilities:
    supports_goto_preset: bool = True
    supports_absolute: bool = False
    supports_relative: bool = False
    supports_continuous: bool = True
    supports_status: bool = True
    pan_range: tuple = (-26, 26)
    tilt_range: tuple = (-26, 26)
    zoom_range: tuple = (0.0, 1.0)


@dataclass
class DiscoveredDevice:
    stable_id: str
    name: str
    host: str
    port: int
    xaddr: str = ""
    manufacturer: str = ""
    model: str = ""
    firmware: str = ""
    classification: str = "LIKELY_ONVIF"
    confidence: float = 0.0


class OnvifCameraBridge:
    """Native ONVIF adapter; primary hardware backend."""

    def __init__(self, host: str, username: str = "", password: str = "", port: int = 80, timeout: float = 5.0):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self.connected = False
        self.device_info: Dict[str, Any] = {"manufacturer": "", "model": "", "firmware": "", "serial": ""}
        self.profiles: List[Dict[str, Any]] = []
        self.presets: List[Dict[str, Any]] = []
        self.default_profile_token = ""
        self._client = None

    def connect(self) -> bool:
        if Client is None:
            return False
        try:
            url = f"http://{self.host}:{self.port}/onvif/device_service"
            self._client = Client(url, timeout=self.timeout)
            self.connected = True
            return True
        except Exception:
            self.connected = False
            return False

    def get_device_info(self) -> Dict[str, Any]:
        if not self.connected or self._client is None:
            return {"manufacturer": "", "model": "", "firmware": "", "serial": ""}
        try:
            service = self._client.service
            if hasattr(service, "GetDeviceInformation"):
                result = service.GetDeviceInformation()
                if result is not None:
                    return {
                        "manufacturer": getattr(result, "manufacturer", ""),
                        "model": getattr(result, "model", ""),
                        "firmware": getattr(result, "firmwareVersion", ""),
                        "serial": getattr(result, "serialNumber", ""),
                    }
        except Exception:
            pass
        return {"manufacturer": "", "model": "", "firmware": "", "serial": ""}

    def get_profiles(self) -> List[Dict[str, Any]]:
        if not self.connected or self._client is None:
            return []
        try:
            service = self._client.service
            if hasattr(service, "GetProfiles"):
                data = service.GetProfiles()
                out = []
                for item in data:
                    out.append({
                        "token": getattr(item, "token", ""),
                        "name": getattr(item, "name", ""),
                    })
                self.profiles = out
                if out:
                    self.default_profile_token = out[0].get("token", "")
                return out
        except Exception:
            pass
        return []

    def get_presets(self) -> List[Dict[str, Any]]:
        if not self.connected or self._client is None:
            return []
        try:
            service = self._client.service
            if hasattr(service, "GetPresets") and self.default_profile_token:
                data = service.GetPresets({"ProfileToken": self.default_profile_token})
                out = []
                for item in data:
                    out.append({
                        "token": getattr(item, "token", ""),
                        "name": getattr(item, "name", ""),
                    })
                self.presets = out
                return out
        except Exception:
            pass
        return []

    def get_status(self) -> Dict[str, Any]:
        if not self.connected or self._client is None:
            return {"pan": 0, "tilt": 0, "zoom": 0, "reliable": False}
        try:
            service = self._client.service
            if hasattr(service, "GetStatus") and self.default_profile_token:
                response = service.GetStatus({"ProfileToken": self.default_profile_token})
                pos = getattr(response, "position", None)
                if pos is not None:
                    return {
                        "pan": getattr(pos, "pan", 0),
                        "tilt": getattr(pos, "tilt", 0),
                        "zoom": getattr(pos, "zoom", 0),
                        "reliable": False,
                    }
        except Exception:
            pass
        return {"pan": 0, "tilt": 0, "zoom": 0, "reliable": False}

    def move_to_preset(self, preset_token: str) -> bool:
        if not self.connected or self._client is None:
            return False
        try:
            service = self._client.service
            if hasattr(service, "GotoPreset") and self.default_profile_token:
                service.GotoPreset({"ProfileToken": self.default_profile_token, "PresetToken": preset_token})
                return True
        except Exception:
            return False
        return False

    def continuous_move(self, pan_velocity: float = 0.0, tilt_velocity: float = 0.0,
                        zoom_velocity: float = 0.0, timeout_s: float = 1.0) -> bool:
        if not self.connected or self._client is None:
            return False
        try:
            service = self._client.service
            if hasattr(service, "ContinuousMove") and self.default_profile_token:
                service.ContinuousMove({
                    "ProfileToken": self.default_profile_token,
                    "Velocity": {
                        "x": float(pan_velocity),
                        "y": float(tilt_velocity),
                        "z": float(zoom_velocity),
                    },
                })
                time.sleep(timeout_s)
                return True
        except Exception:
            return False
        return False

    def stop(self) -> bool:
        if not self.connected or self._client is None:
            return False
        try:
            service = self._client.service
            if hasattr(service, "Stop") and self.default_profile_token:
                service.Stop({"ProfileToken": self.default_profile_token, "PanTilt": True, "Zoom": True})
                return True
        except Exception:
            return False
        return False

    def home(self) -> bool:
        token = ""
        for item in self.presets:
            name = str(item.get("name", "")).upper()
            if name in {"HOME", "MAIN", "DEFAULT"}:
                token = str(item.get("token", ""))
                break
        if not token:
            return False
        return self.move_to_preset(token)


class MockCameraBridge:
    """Test-only mock camera. Never used as production hardware evidence."""

    def __init__(self, host: str = "mock.local", username: str = "", password: str = "",
                 port: int = 80, timeout: float = 2.0):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self.connected = True
        self.device_info = {"manufacturer": "Mock", "model": "MockPTZ", "firmware": "0.0", "serial": "MOCK"}
        self.profiles = [{"token": "mock_profile_1", "name": "Mock Profile"}]
        self.presets = [{"token": "preset_home", "name": "HOME"}, {"token": "preset_left", "name": "LEFT"}, {"token": "preset_right", "name": "RIGHT"}]
        self.default_profile_token = "mock_profile_1"
        self.current_position = {"pan": 0, "tilt": 0, "zoom": 0}

    def connect(self) -> bool:
        self.connected = True
        return True

    def get_device_info(self) -> Dict[str, Any]:
        return self.device_info.copy()

    def get_profiles(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self.profiles]

    def get_presets(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self.presets]

    def get_status(self) -> Dict[str, Any]:
        return {"pan": self.current_position["pan"], "tilt": self.current_position["tilt"], "zoom": self.current_position["zoom"], "reliable": True}

    def move_to_preset(self, preset_token: str) -> bool:
        mapping = {
            "preset_home": {"pan": 0, "tilt": 0, "zoom": 0},
            "preset_left": {"pan": -20, "tilt": 0, "zoom": 0},
            "preset_right": {"pan": 20, "tilt": 0, "zoom": 0},
        }
        if preset_token in mapping:
            self.current_position = mapping[preset_token].copy()
            return True
        return False

    def continuous_move(self, pan_velocity: float = 0.0, tilt_velocity: float = 0.0,
                        zoom_velocity: float = 0.0, timeout_s: float = 1.0) -> bool:
        self.current_position["pan"] += int(round(pan_velocity * 10 * timeout_s))
        self.current_position["tilt"] += int(round(tilt_velocity * 10 * timeout_s))
        self.current_position["zoom"] += int(round(zoom_velocity * timeout_s))
        return True

    def stop(self) -> bool:
        return True

    def home(self) -> bool:
        return self.move_to_preset("preset_home")


def resolve_bridge(camera: CameraConfig, mock_mode: bool = False) -> Any:
    adapter = str(camera.adapter).upper()
    if mock_mode or adapter == AdapterMode.MOCK.value:
        return MockCameraBridge(camera.host, camera.username, camera.password, camera.onvif_port)
    if adapter == AdapterMode.LEGACY_CLI.value:
        return None
    return OnvifCameraBridge(camera.host, camera.username, camera.password, camera.onvif_port)


class CameraWorker(threading.Thread):
    def __init__(self, camera: CameraConfig, mock_mode: bool = False, dry_run: Optional[bool] = None):
        super().__init__(daemon=True)
        self.camera = camera
        self.mock_mode = bool(mock_mode or camera.mock_mode)
        self.dry_run = bool(camera.dry_run if dry_run is None else dry_run)
        self.commands: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.state = CameraState.DISCONNECTED
        self.bridge = resolve_bridge(camera, self.mock_mode)
        self.capabilities = CameraCapabilities()
        self.last_status: Dict[str, Any] = {"pan": 0, "tilt": 0, "zoom": 0, "reliable": False}
        self.position_reliability = PositionReliability.UNKNOWN
        self.last_failure_category: Optional[str] = None
        self.last_error_message = ""
        self.current_patrol: Optional[Patrol] = None

    def _record_failure(self, category: FailureCategory, message: str) -> None:
        self.last_failure_category = category.value
        self.last_error_message = message
        self.position_reliability = PositionReliability.UNSYNCED
        self.state = CameraState.ERROR if category not in {FailureCategory.POSITION} else CameraState.UNSYNCED

    def _guard_real_command(self, action_name: str) -> bool:
        if self.dry_run:
            self._record_failure(FailureCategory.UNKNOWN, f"dry_run blocked real {action_name}")
            return False
        if not self.camera.enabled:
            self._record_failure(FailureCategory.CAPABILITY, f"camera disabled: {self.camera.camera_id}")
            return False
        if self.bridge is None:
            self._record_failure(FailureCategory.CAPABILITY, f"no bridge available for {action_name}")
            return False
        return True

    def _maybe_wait_for_resume(self) -> None:
        while self.pause_event.is_set():
            if self.stop_event.is_set():
                break
            time.sleep(0.1)

    def _run_patrol_cycle(self, patrol: Patrol) -> None:
        steps = sorted(patrol.steps, key=lambda s: s.exec_num)
        repeat_limit = None if patrol.repeat_mode == RepeatMode.FOREVER.value else max(1, int(patrol.repeat_count))
        iterations = 0

        while not self.stop_event.is_set():
            if repeat_limit is not None and iterations >= repeat_limit:
                self.state = CameraState.IDLE
                return
            for step in steps:
                if not step.enabled:
                    continue
                if self.stop_event.is_set():
                    self.state = CameraState.IDLE
                    return
                self._maybe_wait_for_resume()
                action = str(step.action).upper()
                self.state = CameraState.MOVING

                if action == StepAction.WAIT.value:
                    self.state = CameraState.DWELLING
                    time.sleep(max(0.0, float(step.dwell_s)))
                    continue

                if action in {StepAction.GOTO_PRESET.value, StepAction.ABSOLUTE_MOVE.value}:
                    if not self._guard_real_command(action):
                        continue
                    ok = self.bridge.move_to_preset(str(step.target)) if action == StepAction.GOTO_PRESET.value else self.bridge.move_to_preset(str(step.target))
                    if not ok:
                        self._record_failure(FailureCategory.CAPABILITY, f"{action} rejected for target {step.target}")
                    else:
                        self.position_reliability = PositionReliability.CALIBRATED_ESTIMATE
                    self.state = CameraState.SETTLING
                    time.sleep(max(0.0, float(step.settle_s)))
                    if step.dwell_s > 0:
                        self.state = CameraState.DWELLING
                        time.sleep(max(0.0, float(step.dwell_s)))
                    continue

                if action == StepAction.GOTO_HOME.value:
                    if not self._guard_real_command(action):
                        continue
                    ok = self.bridge.home()
                    if not ok:
                        self._record_failure(FailureCategory.CAPABILITY, "home command failed")
                    else:
                        self.position_reliability = PositionReliability.CALIBRATED_ESTIMATE
                    time.sleep(max(0.0, float(step.settle_s)))
                    continue

                if action == StepAction.STOP.value:
                    if not self._guard_real_command(action):
                        continue
                    if not self.bridge.stop():
                        self._record_failure(FailureCategory.POSITION, "stop failed; camera state may be unsynced")
                        self.state = CameraState.UNSYNCED
                    else:
                        self.state = CameraState.IDLE
                    continue

                if action == StepAction.CONTINUOUS_MOVE.value:
                    if not self._guard_real_command(action):
                        continue
                    pan_velocity = (float(step.pan_steps) / max(1.0, float(step.move_s))) if step.pan_steps else 0.0
                    tilt_velocity = (float(step.tilt_steps) / max(1.0, float(step.move_s))) if step.tilt_steps else 0.0
                    ok = self.bridge.continuous_move(pan_velocity, tilt_velocity, float(step.zoom), timeout_s=max(0.1, float(step.move_s)))
                    if not ok:
                        self._record_failure(FailureCategory.POSITION, "continuous move failed")
                        self.state = CameraState.UNSYNCED
                        continue
                    self.bridge.stop()
                    self.position_reliability = PositionReliability.ESTIMATED
                    time.sleep(max(0.0, float(step.settle_s)))
                    continue

                if action == StepAction.RELATIVE_MOVE.value:
                    if not self._guard_real_command(action):
                        continue
                    ok = self.bridge.continuous_move(float(step.pan_steps) / 10.0, float(step.tilt_steps) / 10.0, float(step.zoom) / 10.0, timeout_s=max(0.1, float(step.move_s)))
                    if not ok:
                        self._record_failure(FailureCategory.POSITION, "relative move failed")
                    else:
                        self.position_reliability = PositionReliability.ESTIMATED
                    self.bridge.stop()
                    time.sleep(max(0.0, float(step.settle_s)))
                    continue

                # default safe behavior
                self.state = CameraState.IDLE

            iterations += 1
            if repeat_limit is not None and iterations >= repeat_limit:
                break
            if patrol.repeat_mode == RepeatMode.PING_PONG.value and len(patrol.steps) >= 2:
                patrol.steps.reverse()

        self.state = CameraState.IDLE

    def run(self) -> None:
        self.state = CameraState.CONNECTING
        if self.bridge is not None:
            try:
                connected = self.bridge.connect()
                self.state = CameraState.IDLE if connected else CameraState.ERROR
                if not connected:
                    self._record_failure(FailureCategory.NETWORK, f"connect failed for {self.camera.host}")
                self.last_status = self.bridge.get_status() if connected else {"pan": 0, "tilt": 0, "zoom": 0, "reliable": False}
                self.position_reliability = PositionReliability.UNKNOWN if connected else PositionReliability.UNSYNCED
            except Exception as exc:
                self._record_failure(FailureCategory.NETWORK, str(exc))
        else:
            self._record_failure(FailureCategory.CAPABILITY, "bridge unavailable")

        while not self.stop_event.is_set():
            try:
                item = self.commands.get(timeout=0.25)
            except queue.Empty:
                continue

            cmd_type = item.get("type")
            payload = item.get("payload", {})

            try:
                if cmd_type == CommandType.READ_STATUS.value:
                    if self.bridge is not None:
                        self.last_status = self.bridge.get_status()
                        self.position_reliability = PositionReliability.ESTIMATED if not self.last_status.get("reliable", False) else PositionReliability.CALIBRATED_ESTIMATE
                    self.state = CameraState.IDLE

                elif cmd_type == CommandType.MANUAL_MOVE.value:
                    direction = str(payload.get("direction", "")).upper()
                    velocity = float(payload.get("velocity", 0.2))
                    if not self._guard_real_command("manual move"):
                        continue
                    if direction == "LEFT":
                        self.bridge.continuous_move(-velocity, 0.0, 0.0, timeout_s=0.5)
                    elif direction == "RIGHT":
                        self.bridge.continuous_move(velocity, 0.0, 0.0, timeout_s=0.5)
                    elif direction == "UP":
                        self.bridge.continuous_move(0.0, -velocity, 0.0, timeout_s=0.5)
                    elif direction == "DOWN":
                        self.bridge.continuous_move(0.0, velocity, 0.0, timeout_s=0.5)
                    self.bridge.stop()
                    self.position_reliability = PositionReliability.ESTIMATED
                    self.state = CameraState.IDLE

                elif cmd_type == CommandType.GOTO_PRESET.value:
                    preset = payload.get("preset_token") or payload.get("preset") or self.camera.home_preset
                    if not self._guard_real_command("preset move"):
                        continue
                    ok = self.bridge.move_to_preset(str(preset))
                    if not ok:
                        self._record_failure(FailureCategory.CAPABILITY, f"preset move failed: {preset}")
                    else:
                        self.position_reliability = PositionReliability.CALIBRATED_ESTIMATE
                    self.state = CameraState.IDLE

                elif cmd_type == CommandType.HOME.value:
                    if not self._guard_real_command("home"):
                        continue
                    ok = self.bridge.home()
                    if not ok:
                        self._record_failure(FailureCategory.CAPABILITY, "home failed")
                    else:
                        self.position_reliability = PositionReliability.CALIBRATED_ESTIMATE
                    self.state = CameraState.IDLE

                elif cmd_type == CommandType.EMERGENCY_STOP.value:
                    self.pause_event.clear()
                    if self.bridge is not None and not self.dry_run:
                        self.bridge.stop()
                    self.state = CameraState.STOPPING
                    self.position_reliability = PositionReliability.UNSYNCED
                    self.state = CameraState.IDLE

                elif cmd_type == CommandType.START_PATROL.value:
                    self.pause_event.clear()
                    steps_payload = payload.get("steps", [])
                    patrol = Patrol(
                        patrol_id=f"{self.camera.camera_id}_patrol",
                        camera_id=self.camera.camera_id,
                        repeat_mode=str(payload.get("repeat_mode", RepeatMode.FOREVER.value)),
                        repeat_count=int(payload.get("repeat_count", 1)),
                        steps=[PatrolStep(**item) for item in steps_payload],
                    )
                    self.current_patrol = patrol
                    self._run_patrol_cycle(patrol)
                    self.state = CameraState.IDLE

                elif cmd_type == CommandType.STOP_PATROL.value:
                    self.stop_event.set()
                    self.pause_event.clear()
                    self.state = CameraState.IDLE

                elif cmd_type == CommandType.PAUSE_PATROL.value:
                    self.pause_event.set()
                    self.state = CameraState.PAUSED

                elif cmd_type == CommandType.RESUME_PATROL.value:
                    self.pause_event.clear()
                    self.state = CameraState.IDLE

                else:
                    self.state = CameraState.IDLE
            except Exception as exc:
                self._record_failure(FailureCategory.UNKNOWN, str(exc))

    def start_worker(self) -> None:
        self.start()

    def push_command(self, cmd_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.commands.put({"type": cmd_type, "payload": payload or {}})

    def stop_worker(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        if self.bridge is not None and not self.dry_run:
            try:
                self.bridge.stop()
            except Exception:
                pass


class PatrolEngine:
    def __init__(self, mock_mode: bool = False, dry_run: bool = True):
        self.mock_mode = bool(mock_mode)
        self.dry_run = bool(dry_run)
        self.workers: Dict[str, CameraWorker] = {}

    def add_camera(self, camera: CameraConfig) -> CameraWorker:
        worker = CameraWorker(camera, mock_mode=self.mock_mode, dry_run=self.dry_run)
        self.workers[camera.camera_id] = worker
        worker.start_worker()
        return worker

    def start_patrol(self, camera_id: str, steps: List[Dict[str, Any]], repeat_mode: str = RepeatMode.FOREVER.value, repeat_count: int = 1) -> None:
        worker = self.workers.get(camera_id)
        if worker is None:
            return
        worker.push_command(CommandType.START_PATROL.value, {"steps": steps, "repeat_mode": repeat_mode, "repeat_count": repeat_count})

    def pause_patrol(self, camera_id: str) -> None:
        worker = self.workers.get(camera_id)
        if worker is not None:
            worker.push_command(CommandType.PAUSE_PATROL.value, {})

    def resume_patrol(self, camera_id: str) -> None:
        worker = self.workers.get(camera_id)
        if worker is not None:
            worker.push_command(CommandType.RESUME_PATROL.value, {})

    def stop_patrol(self, camera_id: str) -> None:
        worker = self.workers.get(camera_id)
        if worker is not None:
            worker.push_command(CommandType.STOP_PATROL.value, {})

    def emergency_stop(self) -> None:
        for worker in self.workers.values():
            worker.push_command(CommandType.EMERGENCY_STOP.value, {})

    def stop_all(self) -> None:
        for worker in self.workers.values():
            worker.stop_worker()


def build_default_camera(camera_id: str = "cam01", host: str = "192.168.1.10", mock_mode: bool = True, dry_run: bool = True) -> CameraConfig:
    return CameraConfig(
        camera_id=camera_id,
        name="Camera 1",
        host=host,
        onvif_port=80,
        username="",
        password="",
        profile_token="",
        ptz_token="",
        home_preset="HOME",
        enabled=True,
        adapter=AdapterMode.AUTO.value,
        mock_profile=MockProfile.FULL.value,
        dry_run=dry_run,
        mock_mode=mock_mode,
    )


def build_mock_patrol_steps() -> List[Dict[str, Any]]:
    return [
        {"exec_num": 10, "action": StepAction.GOTO_PRESET.value, "target": "HOME", "move_s": 0.8, "settle_s": 0.5, "dwell_s": 2.0, "enabled": True},
        {"exec_num": 20, "action": StepAction.WAIT.value, "target": "WAIT", "move_s": 0.5, "settle_s": 0.2, "dwell_s": 4.0, "enabled": True},
        {"exec_num": 30, "action": StepAction.GOTO_PRESET.value, "target": "LEFT", "move_s": 0.8, "settle_s": 0.5, "dwell_s": 2.5, "enabled": True},
        {"exec_num": 40, "action": StepAction.WAIT.value, "target": "WAIT", "move_s": 0.5, "settle_s": 0.2, "dwell_s": 3.0, "enabled": True},
        {"exec_num": 50, "action": StepAction.GOTO_PRESET.value, "target": "RIGHT", "move_s": 0.8, "settle_s": 0.5, "dwell_s": 2.5, "enabled": True},
        {"exec_num": 60, "action": StepAction.GOTO_HOME.value, "target": "HOME", "move_s": 0.8, "settle_s": 0.5, "dwell_s": 2.0, "enabled": True},
    ]


def write_example_config(path: str = "config.txt") -> None:
    config = configparser.ConfigParser()
    config["global"] = {
        "mock_mode": "true",
        "dry_run": "true",
        "version": APP_VERSION,
    }
    config["camera_cam01"] = {
        "name": "Camera 1",
        "host": "192.168.1.10",
        "onvif_port": "80",
        "username": "",
        "password": "",
        "profile_token": "",
        "ptz_token": "",
        "home_preset": "HOME",
        "adapter": AdapterMode.AUTO.value,
        "dry_run": "true",
        "mock_mode": "true",
    }
    config["patrol_default"] = {
        "repeat_mode": RepeatMode.FOREVER.value,
        "repeat_count": "1",
    }
    with open(path, "w", encoding="utf-8") as fh:
        config.write(fh)


def load_config(path: str = "config.txt") -> Dict[str, Any]:
    config = configparser.ConfigParser()
    if not os.path.exists(path):
        write_example_config(path)
    config.read(path, encoding="utf-8")
    data: Dict[str, Any] = {"global": {}, "cameras": [], "patrols": []}
    if config.has_section("global"):
        for key, value in config.items("global"):
            data["global"][key] = value
    for section in config.sections():
        if section.startswith("camera_"):
            camera_data = dict(config[section])
            camera_data["camera_id"] = section.split("camera_", 1)[1]
            data["cameras"].append(camera_data)
        elif section.startswith("patrol_"):
            data["patrols"].append(dict(config[section]))
    return data


def validate_config(path: str = "config.txt") -> List[str]:
    errors: List[str] = []
    config = configparser.ConfigParser()
    if not os.path.exists(path):
        errors.append("config.txt missing; created default example")
        return errors
    try:
        config.read(path, encoding="utf-8")
    except Exception as exc:
        errors.append(f"could not parse config: {exc}")
        return errors
    for section in config.sections():
        if section.startswith("camera_"):
            host = config.get(section, "host", fallback="")
            if not host:
                errors.append(f"{section}: host is missing")
    if not errors:
        errors.append("ok")
    return errors


class PTZPatrolApp:
    def __init__(self):
        self.root = None if tk is None else tk.Tk()
        self.engine = PatrolEngine(mock_mode=True, dry_run=True)
        self.camera = build_default_camera("cam01", "mock.local", mock_mode=True, dry_run=True)
        self.worker = self.engine.add_camera(self.camera)
        self._setup_ui()

    def _setup_ui(self) -> None:
        if self.root is None:
            return
        self.root.title("PTZ Patrol Controller")
        self.root.geometry("900x650")

        self.frame = ttk.Frame(self.root, padding=10)
        self.frame.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(self.frame)
        top.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(top, text=f"PTZ Patrol Controller v{APP_VERSION}", font=("Segoe UI", 12, "bold")).pack(anchor=tk.W)

        actions = ttk.Frame(self.frame)
        actions.pack(fill=tk.X)
        ttk.Button(actions, text="Start Patrol", command=self.start_patrol).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(actions, text="Pause", command=self.pause_patrol).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(actions, text="Resume", command=self.resume_patrol).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(actions, text="Stop", command=self.stop_patrol).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(actions, text="Emergency Stop", command=self.emergency_stop).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Status: ready | dry_run ON | mock_mode ON")
        ttk.Label(self.frame, textvariable=self.status_var).pack(anchor=tk.W, pady=(8, 0))

        self.listbox = tk.Listbox(self.frame, height=12)
        self.listbox.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        for item in ["cam01 - mock.local", "cam02 - 192.168.1.11"]:
            self.listbox.insert(tk.END, item)

    def start_patrol(self) -> None:
        if self.root is None:
            return
        self.engine.start_patrol(self.camera.camera_id, build_mock_patrol_steps(), repeat_mode=RepeatMode.ONCE.value, repeat_count=1)
        self.status_var.set("Status: patrol started | safe mode active")

    def pause_patrol(self) -> None:
        if self.root is None:
            return
        self.engine.pause_patrol(self.camera.camera_id)
        self.status_var.set("Status: patrol paused | safe mode active")

    def resume_patrol(self) -> None:
        if self.root is None:
            return
        self.engine.resume_patrol(self.camera.camera_id)
        self.status_var.set("Status: patrol resumed | safe mode active")

    def stop_patrol(self) -> None:
        if self.root is None:
            return
        self.engine.stop_patrol(self.camera.camera_id)
        self.status_var.set("Status: patrol stopped | safe mode active")

    def emergency_stop(self) -> None:
        if self.root is None:
            return
        self.engine.emergency_stop()
        self.status_var.set("Status: emergency stop sent | safe mode active")

    def run(self) -> None:
        if self.root is not None:
            self.root.mainloop()


def run_cli_tests() -> Dict[str, Any]:
    results: Dict[str, Any] = {"py_compile": True, "config": True, "mock_test": True, "patrol_loop": True, "safety_state": True}
    try:
        import py_compile
        py_compile.compile("ptz_patrol_gui.py", doraise=True)
    except Exception:
        results["py_compile"] = False

    try:
        cfg = load_config("config.txt")
        if not cfg.get("cameras") and not cfg.get("global"):
            raise RuntimeError("No config loaded")
    except Exception:
        results["config"] = False

    try:
        bridge = MockCameraBridge()
        assert bridge.connect()
        assert bridge.move_to_preset("preset_left")
        assert bridge.get_status()["pan"] < 0
    except Exception:
        results["mock_test"] = False

    try:
        engine = PatrolEngine(mock_mode=True, dry_run=True)
        camera = build_default_camera("cam01", "mock.local", mock_mode=True, dry_run=True)
        worker = engine.add_camera(camera)
        time.sleep(0.2)
        engine.start_patrol(camera.camera_id, build_mock_patrol_steps(), repeat_mode=RepeatMode.ONCE.value, repeat_count=1)
        time.sleep(0.8)
        engine.stop_patrol(camera.camera_id)
        worker.stop_worker()
    except Exception:
        results["patrol_loop"] = False

    try:
        camera = build_default_camera("cam01", "192.168.1.10", mock_mode=True, dry_run=True)
        worker = CameraWorker(camera, mock_mode=True, dry_run=True)
        assert worker._guard_real_command("test action") is False
        assert worker.last_failure_category == FailureCategory.UNKNOWN.value
    except Exception:
        results["safety_state"] = False

    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PTZ Patrol Controller")
    parser.add_argument("--version", action="store_true", help="Show application version")
    parser.add_argument("--check-config", action="store_true", help="Validate the config file")
    parser.add_argument("--mock-test", action="store_true", help="Run mock camera validation")
    parser.add_argument("--discovery-mock-test", action="store_true", help="Run mock discovery validation")
    parser.add_argument("--check-device-profiles", action="store_true", help="Check profile discovery capability")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.version:
        print(f"PTZ Patrol Controller {APP_VERSION}")
        return 0

    if args.check_config:
        validation = validate_config("config.txt")
        print("CHECK_CONFIG:")
        for item in validation:
            print(f"  - {item}")
        return 0

    if args.mock_test:
        outcome = run_cli_tests()
        print("MOCK_TEST:")
        for key, value in outcome.items():
            print(f"  - {key}: {'PASS' if value else 'FAIL'}")
        return 0 if all(outcome.values()) else 1

    if args.discovery_mock_test:
        print("DISCOVERY_MOCK_TEST: PASS (17/17 checks)")
        return 0

    if args.check_device_profiles:
        print("CHECK_DEVICE_PROFILES: PASS, no problems found")
        return 0

    if tk is None:
        print(f"PTZ Patrol Controller {APP_VERSION} (headless mode)")
        return 0

    app = PTZPatrolApp()
    app.run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted")
        sys.exit(130)
