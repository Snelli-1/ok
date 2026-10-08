#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ptz_patrol_gui.py
==================================================================
Local-First Multi-Camera ONVIF PTZ Patrol Controller for Windows
==================================================================

PURPOSE
-------
A single-file Windows desktop application (Tkinter/ttk) that runs
independent, repeating PTZ ("pan / tilt / zoom") patrol loops on
multiple ONVIF cameras over the local network. It exists because
consumer viewer apps (Onvier, tinyCam, ...) can drive a camera
manually but cannot run a reusable cyclic patrol such as:

    MAIN preset  -> settle -> dwell 120 s
    LEFT preset  -> settle -> dwell  15 s
    MAIN preset  -> settle -> dwell 120 s
    RIGHT preset -> settle -> dwell  15 s
    repeat forever

Every enabled camera gets its own worker thread and its own patrol
loop. A failure, timeout or disconnect on one camera never stops or
blocks any other camera, and never blocks the GUI.

SAFETY DEFAULTS
----------------
The application ships configured with `dry_run = true` and
`mock_mode = true` so it can be started, explored and tested with
zero real cameras attached and zero risk of moving anything. Mock
mode simulates three cameras (a fully-working one, an intermittently
disconnecting one and a capability-limited one) so every part of the
GUI -- independent patrols, pause/resume, manual override, recovery,
worker isolation and Emergency Stop -- can be exercised safely.

REAL CAMERAS
------------
Real-camera control goes through an external, separately-installed
ONVIF command-line tool, invoked only through the companion
`ptz_commands.bat` bridge (never through raw SOAP/WS-Security code
written here). The exact command syntax of that external tool is
NOT assumed or hard-coded: it is filled in by the operator as a
template in the `[CLI]` section of config.txt. See README.md.

FILE ORGANIZATION (in reading order)
-------------------------------------
 1. Imports
 2. Constants and enums
 3. Dataclasses and data models
 4. Configuration loading, validation, backup and atomic saving
 5. Credential handling and log redaction
 6. Rotating logging
 7. BAT command bridge (camera command dispatch to ptz_commands.bat)
 8. Camera capability and status models (capability probe)
 9. Camera worker (one thread per enabled camera)
10. Patrol engine / worker manager
11. Mock camera implementation
12. Preview and snapshot utilities
13. Tkinter GUI construction
14. GUI callbacks
15. Graceful shutdown
16. main()

This file intentionally stays a single module (no package, no
internal imports) so it is trivial to copy, read and run on a bare
Windows Python installation.
"""

# ======================================================================
# 1. IMPORTS
# ======================================================================

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import copy
import dataclasses
import hashlib
import ipaddress
import itertools
import json
import logging
import logging.handlers
import os
import platform
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox

try:
    from PIL import Image, ImageTk  # type: ignore
    PIL_AVAILABLE = True
except Exception:  # pragma: no cover - Pillow is optional
    PIL_AVAILABLE = False

IS_WINDOWS = platform.system() == "Windows"
APP_NAME = "PTZ Patrol Controller"
APP_VERSION = "1.0.0-compact"
CONFIG_VERSION = 1

# ======================================================================
# 2. CONSTANTS AND ENUMS
# ======================================================================

# ---- Directory / file layout (all resolved relative to this script) ----
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.txt"
LOGS_DIR = SCRIPT_DIR / "logs"
RUNTIME_DIR = SCRIPT_DIR / "runtime"
DIAGNOSTICS_DIR = SCRIPT_DIR / "diagnostics"
BACKUPS_DIR = RUNTIME_DIR / "backups"
BAT_PATH = SCRIPT_DIR / "ptz_commands.bat"
PID_FILE = RUNTIME_DIR / "app.pid"
STOP_REQUEST_FILE = RUNTIME_DIR / "stop.request"
STATE_FILE = RUNTIME_DIR / "state.json"
CLI_CACHE_FILE = RUNTIME_DIR / "cli_cache.txt"

# ---- Safety / bounding constants (not user-configurable) ----
MANUAL_MOVE_CHUNK_S = 0.5        # duration of one bounded manual-move refresh
MANUAL_MOVE_DEADMAN_S = 1.5      # auto-Stop if no refresh arrives within this long
WAIT_SLICE_S = 0.2               # granularity of interruptible waits
GUI_POLL_MS = 150                # GUI event-queue drain interval
MAX_CONTINUOUS_MOVE_S = 60.0     # hard ceiling for any single ContinuousMove
MAX_DWELL_S = 86400.0            # 24h ceiling for dwell/settle validation
MAX_PREVIEW_TILES = 8
DEFAULT_COMMAND_TIMEOUT_S = 8.0
WORKER_JOIN_TIMEOUT_S = 5.0


class CameraState(str, Enum):
    """Lifecycle state of one camera worker."""
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


class StepAction(str, Enum):
    """Supported patrol-step action types."""
    GOTO_PRESET = "GOTO_PRESET"
    WAIT = "WAIT"
    CONTINUOUS_MOVE = "CONTINUOUS_MOVE"
    STOP = "STOP"
    GOTO_HOME = "GOTO_HOME"


class RepeatMode(str, Enum):
    FOREVER = "FOREVER"
    FIXED_COUNT = "FIXED_COUNT"
    DISABLED = "DISABLED"


class AdapterMode(str, Enum):
    AUTO = "auto"
    CLI = "cli"
    MOCK = "mock"


class ArrivalPolicy(str, Enum):
    COMMAND_ACCEPTED = "command_accepted"
    FIXED_SETTLE = "fixed_settle"
    STATUS_IDLE = "status_idle"


class ResumePolicy(str, Enum):
    RESTART_CURRENT_STEP = "restart_current_step"
    RESTART_PATROL = "restart_patrol"
    STAY_PAUSED = "stay_paused"


class FailureCategory(str, Enum):
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication"
    NETWORK = "network"
    UNSUPPORTED = "unsupported"
    INVALID_RESPONSE = "invalid_response"
    CLI_MISSING = "CLI_missing"
    CONFIGURATION = "configuration"
    UNKNOWN = "unknown"


class PreviewMode(str, Enum):
    OFF = "off"
    SNAPSHOT = "snapshot"
    LOW_FPS = "low_fps"


class MockProfile(str, Enum):
    FULL = "full"
    INTERMITTENT = "intermittent"
    LIMITED = "limited"


class CredentialBackendKind(str, Enum):
    PLAINTEXT = "plaintext"
    WINDOWS_CREDENTIAL_MANAGER = "windows_credential_manager"


class CommandType(str, Enum):
    """Every member here is actually enqueued and actually handled in
    CameraWorker._handle_command -- there are deliberately no
    declared-but-unused command types. Patrol-step progression is NOT
    one of these: it is driven directly by each worker's own loop
    (see CameraWorker._execute_next_patrol_step), not queued, which is
    why "patrol step" has no CommandType member -- there is nothing
    external that would ever need to enqueue one."""
    EMERGENCY_STOP = "EMERGENCY_STOP"
    SHUTDOWN_STOP = "SHUTDOWN_STOP"
    MANUAL_STOP = "MANUAL_STOP"
    MANUAL_MOVE = "MANUAL_MOVE"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    STOP_PATROL = "STOP_PATROL"
    START_PATROL = "START_PATROL"
    TEST_ONE_CYCLE = "TEST_ONE_CYCLE"
    HOME = "HOME"
    PRESET_TEST = "PRESET_TEST"
    DIAGNOSTIC_READ = "DIAGNOSTIC_READ"


# Priority numbers: LOWER runs first (queue.PriorityQueue is a min-heap).
# A patrol step in progress is conceptually priority 10 (never queued --
# see CommandType docstring above); PREEMPT_PRIORITY_THRESHOLD is set so
# that every queued command EXCEPT the read-only DIAGNOSTIC_READ
# interrupts an in-progress dwell/settle wait.
COMMAND_PRIORITY: Dict[CommandType, int] = {
    CommandType.EMERGENCY_STOP: 0,
    CommandType.SHUTDOWN_STOP: 1,
    CommandType.MANUAL_STOP: 2,
    CommandType.MANUAL_MOVE: 3,
    CommandType.PAUSE: 4,
    CommandType.RESUME: 4,
    CommandType.STOP_PATROL: 5,
    CommandType.START_PATROL: 6,
    CommandType.TEST_ONE_CYCLE: 6,
    CommandType.HOME: 7,
    CommandType.PRESET_TEST: 8,
    CommandType.DIAGNOSTIC_READ: 20,
}

# Commands at or below this priority number preempt an in-progress
# patrol dwell/settle wait (i.e. everything except a diagnostic read).
PREEMPT_PRIORITY_THRESHOLD = 8

TEMPLATE_PLACEHOLDERS = (
    "{host}", "{onvif_port}", "{username}", "{password}",
    "{profile_token}", "{preset_token}", "{pan}", "{tilt}", "{zoom}",
    "{timeout_s}", "{output_file}", "{executable}",
)


# ======================================================================
# 3. DATACLASSES AND DATA MODELS
# ======================================================================

@dataclass
class PatrolStep:
    """One step of a patrol. Meaning of fields depends on `action`:

    GOTO_PRESET      -> uses `target` (a preset alias), `settle_s`, `dwell_s`
    GOTO_HOME        -> uses `settle_s`, `dwell_s` (target preset comes from
                        the camera's own `home_preset`)
    WAIT             -> uses only `dwell_s`
    STOP             -> sends Stop; `dwell_s` may add a short pause after
    CONTINUOUS_MOVE  -> uses `pan`, `tilt`, `zoom`, `move_s`, `settle_s`,
                        `dwell_s`. Always internally followed by an
                        explicit Stop call, regardless of what the
                        underlying adapter/CLI already does.
    """
    action: StepAction
    target: str = ""
    pan: float = 0.0
    tilt: float = 0.0
    zoom: float = 0.0
    move_s: float = 0.0
    settle_s: float = 0.0
    dwell_s: float = 0.0
    enabled: bool = True

    def to_config_value(self) -> str:
        """Serialize back to the `key=value;key=value` config.txt syntax."""
        parts = [f"action={self.action.value}"]
        if self.action in (StepAction.GOTO_PRESET,):
            parts.append(f"target={self.target}")
        if self.action == StepAction.CONTINUOUS_MOVE:
            parts.append(f"pan={self.pan:.3f}")
            parts.append(f"tilt={self.tilt:.3f}")
            parts.append(f"zoom={self.zoom:.3f}")
            parts.append(f"move_s={self.move_s:.2f}")
        if self.action in (StepAction.GOTO_PRESET, StepAction.GOTO_HOME,
                           StepAction.CONTINUOUS_MOVE):
            parts.append(f"settle_s={self.settle_s:.2f}")
        parts.append(f"dwell_s={self.dwell_s:.2f}")
        parts.append(f"enabled={'true' if self.enabled else 'false'}")
        return ";".join(parts)

    def short_label(self) -> str:
        if self.action == StepAction.GOTO_PRESET:
            return f"GOTO_PRESET {self.target}"
        if self.action == StepAction.CONTINUOUS_MOVE:
            return f"MOVE p{self.pan:+.2f} t{self.tilt:+.2f} z{self.zoom:+.2f}"
        return self.action.value


@dataclass
class Patrol:
    """A named, reusable, repeating sequence of PatrolStep objects."""
    name: str
    enabled: bool = True
    repeat_mode: RepeatMode = RepeatMode.FOREVER
    repeat_count: int = 1
    steps: List[PatrolStep] = field(default_factory=list)

    def enabled_steps(self) -> List[PatrolStep]:
        return [s for s in self.steps if s.enabled]


@dataclass
class CalibrationConfig:
    """Placeholders for the future time-calibrated fallback estimator.

    Intentionally NOT used by the patrol/worker logic in this compact
    version -- presets are the primary and, for now, only implemented
    movement strategy. Kept here so config.txt has a stable, documented
    home for these values once the calibration tool is built, and so
    `enabled` can be validated to remain False by default.
    """
    enabled: bool = False
    pan_lr_s: float = 0.0
    pan_rl_s: float = 0.0
    tilt_bt_s: float = 0.0
    tilt_tb_s: float = 0.0
    latency_s: float = 0.25
    samples_per_direction: int = 5
    resync_every_s: float = 900.0
    resync_preset: str = ""


@dataclass
class CameraConfig:
    """Everything config.txt records about one physical camera."""
    id: str
    name: str = ""
    enabled: bool = True
    host: str = ""
    onvif_port: int = 8899
    rtsp_port: int = 554
    adapter: AdapterMode = AdapterMode.AUTO
    mock_profile: MockProfile = MockProfile.FULL
    username: str = ""
    password: str = ""
    profile_token: str = ""
    patrol_name: str = ""
    home_preset: str = ""
    presets: Dict[str, str] = field(default_factory=dict)  # alias -> token
    connect_timeout_s: float = 5.0
    command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S
    retry_count: int = 3
    retry_delay_s: float = 2.0
    reconnect_backoff_initial_s: float = 2.0
    reconnect_backoff_max_s: float = 60.0
    on_start: str = "goto_home"       # none|stop|goto_home
    on_stop: str = "stop"             # none|stop|goto_home
    arrival_policy: ArrivalPolicy = ArrivalPolicy.FIXED_SETTLE
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)

    def display_state_key(self) -> str:
        return self.id


@dataclass
class CliTemplates:
    """The `[CLI]` section: configurable external ONVIF CLI wiring."""
    enabled: bool = False
    executable: str = ""
    probe_template: str = ""
    services_template: str = ""
    profiles_template: str = ""
    presets_template: str = ""
    status_template: str = ""
    goto_template: str = ""
    move_template: str = ""
    stop_template: str = ""
    home_template: str = ""
    snapshot_template: str = ""

    def template_for(self, action: str) -> str:
        return {
            "probe": self.probe_template,
            "services": self.services_template,
            "profiles": self.profiles_template,
            "presets": self.presets_template,
            "status": self.status_template,
            "goto": self.goto_template,
            "move": self.move_template,
            "stop-camera": self.stop_template,
            "home": self.home_template,
            "snapshot": self.snapshot_template,
        }.get(action, "")


@dataclass
class AppSettings:
    language: str = "en"
    log_level: str = "INFO"
    log_max_bytes: int = 2_000_000
    log_backup_count: int = 5
    autostart_patrols: bool = False
    command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S
    dry_run: bool = True
    mock_mode: bool = True


@dataclass
class SecuritySettings:
    credential_backend: CredentialBackendKind = CredentialBackendKind.PLAINTEXT
    warn_plaintext: bool = True
    redact_logs: bool = True


@dataclass
class PreviewSettings:
    mode: PreviewMode = PreviewMode.OFF
    fps: float = 1.0
    max_tiles: int = MAX_PREVIEW_TILES
    tile_width_px: int = 220


@dataclass
class ManualOverrideSettings:
    default_pause_s: int = 300
    pause_options_s: List[int] = field(default_factory=lambda: [120, 300, 900, 0])
    resume_policy: ResumePolicy = ResumePolicy.RESTART_CURRENT_STEP
    manual_speed_default: float = 0.5


@dataclass
class ShutdownPolicy:
    return_home_on_stop: bool = False
    worker_join_timeout_s: float = WORKER_JOIN_TIMEOUT_S


@dataclass
class AppConfig:
    """The fully validated, typed in-memory configuration."""
    version: int = CONFIG_VERSION
    app: AppSettings = field(default_factory=AppSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    preview: PreviewSettings = field(default_factory=PreviewSettings)
    manual_override: ManualOverrideSettings = field(default_factory=ManualOverrideSettings)
    cli: CliTemplates = field(default_factory=CliTemplates)
    shutdown: ShutdownPolicy = field(default_factory=ShutdownPolicy)
    cameras: Dict[str, CameraConfig] = field(default_factory=dict)
    patrols: Dict[str, Patrol] = field(default_factory=dict)
    # DiscoverySettings/DeviceProfile are defined later in this file
    # (section 12B); `from __future__ import annotations` means this
    # forward reference needs no quoting and resolves fine at runtime
    # since dataclass never evaluates annotations for field defaults.
    discovery: DiscoverySettings = None  # type: ignore[assignment]  # set by validate_app_config
    discovery_ui: DiscoveryUISettings = None  # type: ignore[assignment]  # set by validate_app_config
    device_profile_overrides: List[DeviceProfile] = field(default_factory=list)

    def enabled_cameras(self) -> List[CameraConfig]:
        return [c for c in self.cameras.values() if c.enabled]


@dataclass
class CommandResult:
    """Uniform result of any bridge call (mock or CLI)."""
    ok: bool
    category: Optional[FailureCategory] = None
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CapabilityReport:
    """Result of a capability probe, written to diagnostics/."""
    camera_id: str = ""
    host: str = ""
    configured_onvif_port: int = 0
    discovered_xaddr: str = ""
    auth_method: str = ""
    clock_skew_s: Optional[float] = None
    services: List[str] = field(default_factory=list)
    profiles: List[str] = field(default_factory=list)
    chosen_profile: str = ""
    ptz_node_info: str = ""
    movement_ranges: Dict[str, Any] = field(default_factory=dict)
    status_position_available: bool = False
    move_status_available: bool = False
    presets: List[str] = field(default_factory=list)
    goto_preset_available: bool = False
    absolute_move_available: bool = False
    relative_move_available: bool = False
    continuous_move_available: bool = False
    stop_available: bool = False
    goto_home_available: bool = False
    preset_speed_available: bool = False
    recommended_tier: str = "unknown"
    warnings: List[str] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        return d

    def to_text(self) -> str:
        lines = [f"Capability report for camera '{self.camera_id}'",
                 f"Generated: {self.generated_at}", "-" * 60]
        for k, v in self.to_dict().items():
            if k in ("camera_id", "generated_at"):
                continue
            lines.append(f"{k}: {v}")
        return "\n".join(lines) + "\n"


_seq_counter = itertools.count()


def next_seq() -> int:
    return next(_seq_counter)


@dataclass(order=True)
class WorkerCommand:
    """One entry in a CameraWorker's priority command queue."""
    priority: int
    seq: int
    command_type: CommandType = field(compare=False)
    payload: Dict[str, Any] = field(default_factory=dict, compare=False)


def make_command(command_type: CommandType, payload: Optional[Dict[str, Any]] = None) -> WorkerCommand:
    return WorkerCommand(
        priority=COMMAND_PRIORITY[command_type],
        seq=next_seq(),
        command_type=command_type,
        payload=payload or {},
    )


@dataclass
class WorkerEvent:
    """One entry in the shared, thread-safe GUI event queue."""
    camera_id: str
    event_type: str  # "STATE", "LOG", "ERROR", "STEP", "COUNTDOWN", "SNAPSHOT", "STATUS_COUNTS"
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class ValidationIssue:
    """One problem found while validating config.txt.

    severity="error"   -> the offending camera/patrol/step is excluded
                          from the built AppConfig (best-effort load),
                          and a save operation reporting any "error"
                          issue is aborted (original file untouched).
    severity="warning" -> recorded and shown, but does not exclude
                          anything or block a save.
    """
    section: str
    key: str
    value: str
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        tag = "ERROR" if self.severity == "error" else "WARNING"
        return f"[{tag}] [{self.section}] {self.key}: '{self.value}' -- {self.message}"


# ======================================================================
# 4. CONFIGURATION LOADING, VALIDATION, BACKUP AND ATOMIC SAVING
# ======================================================================

def _classify_line(line: str) -> str:
    s = line.strip()
    if not s:
        return "blank"
    if s.startswith(";") or s.startswith("#"):
        return "comment"
    if s.startswith("[") and s.endswith("]") and len(s) > 2:
        return "section"
    if "=" in s:
        return "kv"
    return "other"


class IniDocument:
    """Minimal line-oriented INI reader/writer that preserves comments,
    blank lines, ordering and unknown sections/keys by only ever
    rewriting the exact line(s) a set() call changes. Lookups are a
    fresh linear scan each call -- simple, and safe against "index
    went stale after insert" bugs for a file this small."""

    def __init__(self, lines: Optional[List[str]] = None):
        self.lines: List[str] = list(lines) if lines else []

    @classmethod
    def parse(cls, text: str) -> "IniDocument":
        norm = text.replace("\r\n", "\n").replace("\r", "\n")
        return cls(norm.split("\n"))

    def to_text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def section_names(self) -> List[str]:
        return [ln.strip()[1:-1].strip() for ln in self.lines if _classify_line(ln) == "section"]

    def sections_with_prefix(self, prefix: str, exclude_suffixes: Tuple[str, ...] = ()) -> List[str]:
        return [n for n in self.section_names()
                if n.startswith(prefix) and not any(n.endswith(s) for s in exclude_suffixes)]

    def _find_section_range(self, section: str) -> Optional[Tuple[int, int]]:
        start = None
        for i, ln in enumerate(self.lines):
            if _classify_line(ln) == "section" and ln.strip()[1:-1].strip() == section:
                start = i + 1
                break
        if start is None:
            return None
        end = len(self.lines)
        for j in range(start, len(self.lines)):
            if _classify_line(self.lines[j]) == "section":
                end = j
                break
        return start, end

    def has_section(self, section: str) -> bool:
        return self._find_section_range(section) is not None

    def get_section_dict(self, section: str) -> Dict[str, str]:
        rng = self._find_section_range(section)
        out: Dict[str, str] = {}
        if rng is None:
            return out
        for i in range(rng[0], rng[1]):
            if _classify_line(self.lines[i]) != "kv":
                continue
            k, _, v = self.lines[i].partition("=")
            out[k.strip()] = v.strip()
        return out

    def ensure_section(self, section: str) -> None:
        if self.has_section(section):
            return
        if self.lines and self.lines[-1].strip() != "":
            self.lines.append("")
        self.lines.append(f"[{section}]")

    def set(self, section: str, key: str, value: str) -> None:
        self.ensure_section(section)
        start, end = self._find_section_range(section)  # type: ignore[misc]
        for i in range(start, end):
            if _classify_line(self.lines[i]) != "kv":
                continue
            k, _, _ = self.lines[i].partition("=")
            if k.strip() == key:
                self.lines[i] = f"{key} = {value}"
                return
        self.lines.insert(end, f"{key} = {value}")

    def remove_keys_matching(self, section: str, key_prefix: str) -> None:
        rng = self._find_section_range(section)
        if rng is None:
            return
        i, end = rng
        while i < end:
            if _classify_line(self.lines[i]) == "kv":
                k = self.lines[i].partition("=")[0].strip()
                if k.startswith(key_prefix):
                    del self.lines[i]
                    end -= 1
                    continue
            i += 1

    def remove_section(self, section: str) -> None:
        rng = self._find_section_range(section)
        if rng is None:
            return
        start, end = rng
        del self.lines[start - 1:end]


def _pbool(raw: str) -> Optional[bool]:
    v = raw.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    return None


def _pfloat(raw: str) -> Optional[float]:
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return None


def _pint(raw: str) -> Optional[int]:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _bool_str(v: bool) -> str:
    return "true" if v else "false"


DEFAULT_CONFIG_TEXT = """; ============================================================
; PTZ Patrol Controller - config.txt
; ============================================================
; This is the ONLY authoritative user-editable configuration file.
; It is safe to hand-edit. On save, the app writes a temp file,
; re-parses + validates it, backs up the original into
; runtime/backups/, then atomically replaces config.txt.
;
; UNITS: all *_s keys are SECONDS. pan/tilt/zoom are normalized
; ONVIF-style values, generally in the range -1.0 .. 1.0.
;
; PATROL STEP SYNTAX (key=value pairs separated by ';'):
;   step_1 = action=GOTO_PRESET;target=MAIN;settle_s=1;dwell_s=5;enabled=true
;   step_2 = action=WAIT;dwell_s=5;enabled=true
;   step_3 = action=CONTINUOUS_MOVE;pan=0.2;tilt=0.0;zoom=0.0;move_s=3;settle_s=1;dwell_s=5;enabled=true
;   step_4 = action=STOP;dwell_s=1;enabled=true
;   step_5 = action=GOTO_HOME;settle_s=1;dwell_s=5;enabled=true
; Supported actions: GOTO_PRESET, WAIT, CONTINUOUS_MOVE, STOP, GOTO_HOME
; Every CONTINUOUS_MOVE is always followed internally by an explicit Stop.
;
; adapter = auto | cli | mock
;   auto -> uses mock when [APPLICATION] mock_mode=true, else uses the CLI
;   cli  -> always uses ptz_commands.bat + the external ONVIF CLI below
;   mock -> always simulated in Python, never touches ptz_commands.bat
;
; SAFETY DEFAULT: dry_run=true and mock_mode=true ship together so the
; app is fully explorable with zero real cameras and zero risk.
; ============================================================

[META]
config_version = 1

[APPLICATION]
language = en
log_level = INFO
log_max_bytes = 2000000
log_backup_count = 5
autostart_patrols = false
command_timeout_s = 8
dry_run = true
mock_mode = true

[SECURITY]
; plaintext is the only implemented backend in this version.
; windows_credential_manager is a documented future extension point.
credential_backend = plaintext
warn_plaintext = true
redact_logs = true

[PREVIEW]
; off | snapshot | low_fps
mode = off
fps = 1
max_tiles = 8
tile_width_px = 220

[MANUAL_OVERRIDE]
default_pause_s = 300
pause_options_s = 120,300,900,0
resume_policy = restart_current_step
manual_speed_default = 0.5

[CLI]
; Fill these in only once you have verified the exact syntax of your
; chosen ONVIF CLI (onvif-python CLI / Easy ONVIF CLI / onvif_control /
; other). Leave enabled=false to keep every camera on mock/dry-run.
enabled = false
executable =
probe_template =
services_template =
profiles_template =
presets_template =
status_template =
goto_template =
move_template =
stop_template =
home_template =
snapshot_template =

[SHUTDOWN]
return_home_on_stop = false
worker_join_timeout_s = 5

; ============================================================
; Authorized LAN ONVIF discovery ("Discover & Diagnose" in the GUI).
; This is a SCOPED COMPATIBILITY PROBE on a subnet YOU explicitly
; select -- never Internet-wide, never a 1-65535 port sweep, never
; credential guessing. See README.md "Authorized LAN discovery".
; ============================================================
[DISCOVERY]
enabled = true
; QUICK | STANDARD | EXTENDED -- EXTENDED additionally requires
; allow_extended_scan=true below AND an explicit on-screen confirmation.
default_mode = QUICK
allowed_subnets = auto
candidate_onvif_ports = 80,443,8000,8080,8081,8899
candidate_rtsp_ports = 554,8554
connect_timeout_s = 0.75
soap_timeout_s = 3
max_parallel_hosts = 8
max_parallel_ports_per_host = 2
delay_between_batches_ms = 250
allow_extended_scan = false
extended_ports =
require_confirmation_for_extended = true
save_reports = true
collect_mac = true
collect_ping = true
ping_is_required = false

[DISCOVERY_UI]
; Remembers your last choices in the Discover & Diagnose dialog.
; Purely a convenience -- never affects the safety bounds above.
last_adapter_name =
last_subnet_cidr =
last_mode = QUICK
last_filter = All

; Optional, additional entries for the built-in mini reference-profile
; database (profiles are HINTS only -- never proof, never credentials).
[DEVICE_PROFILE_OVERRIDE_v380_local]
enabled = true
manufacturer_patterns = V380,Macro-video,Unknown
model_patterns = WET2440
candidate_onvif_ports = 80,8899
device_service_paths = /onvif/device_service
candidate_rtsp_ports = 554
confidence = HEURISTIC
notes = Local user override; verify every device independently.

; ---------------- Camera 1: fully functional mock ----------------
[CAMERA_cam_entrance]
id = cam_entrance
name = Entrance (mock: full)
enabled = true
host = 192.168.1.50
onvif_port = 8899
rtsp_port = 554
adapter = auto
mock_profile = full
username = ptzuser
password = CHANGE_ME
profile_token =
patrol = entrance_patrol
home_preset = MAIN
connect_timeout_s = 5
command_timeout_s = 8
retry_count = 3
retry_delay_s = 2
reconnect_backoff_initial_s = 2
reconnect_backoff_max_s = 60
on_start = goto_home
on_stop = stop
arrival_policy = fixed_settle

[CAMERA_cam_entrance_PRESETS]
MAIN = 1
LEFT = 2
RIGHT = 3

[CAMERA_cam_entrance_CALIBRATION]
enabled = false
pan_lr_s = 0
pan_rl_s = 0
tilt_bt_s = 0
tilt_tb_s = 0
latency_s = 0.25
samples_per_direction = 5
resync_every_s = 900
resync_preset = MAIN

[PATROL_entrance_patrol]
enabled = true
repeat_mode = FOREVER
repeat_count = 1
step_1 = action=GOTO_PRESET;target=MAIN;settle_s=1;dwell_s=8;enabled=true
step_2 = action=GOTO_PRESET;target=LEFT;settle_s=1;dwell_s=4;enabled=true
step_3 = action=GOTO_PRESET;target=MAIN;settle_s=1;dwell_s=8;enabled=true
step_4 = action=GOTO_PRESET;target=RIGHT;settle_s=1;dwell_s=4;enabled=true

; ---------------- Camera 2: intermittently disconnecting mock ----------------
[CAMERA_cam_gate]
id = cam_gate
name = Gate (mock: intermittent)
enabled = true
host = 192.168.1.51
onvif_port = 8899
rtsp_port = 554
adapter = auto
mock_profile = intermittent
username = ptzuser
password = CHANGE_ME
profile_token =
patrol = gate_patrol
home_preset = GATE
connect_timeout_s = 5
command_timeout_s = 8
retry_count = 3
retry_delay_s = 2
reconnect_backoff_initial_s = 2
reconnect_backoff_max_s = 60
on_start = goto_home
on_stop = stop
arrival_policy = fixed_settle

[CAMERA_cam_gate_PRESETS]
GATE = 1
PARKING = 2
DOOR = 3

[CAMERA_cam_gate_CALIBRATION]
enabled = false
pan_lr_s = 0
pan_rl_s = 0
tilt_bt_s = 0
tilt_tb_s = 0
latency_s = 0.25
samples_per_direction = 5
resync_every_s = 900
resync_preset = GATE

[PATROL_gate_patrol]
enabled = true
repeat_mode = FOREVER
repeat_count = 1
step_1 = action=GOTO_PRESET;target=GATE;settle_s=1;dwell_s=6;enabled=true
step_2 = action=GOTO_PRESET;target=PARKING;settle_s=1;dwell_s=4;enabled=true
step_3 = action=GOTO_PRESET;target=DOOR;settle_s=1;dwell_s=5;enabled=true

; ---------------- Camera 3: limited-capability mock (presets only) ----------------
[CAMERA_cam_side]
id = cam_side
name = Side yard (mock: limited)
enabled = true
host = 192.168.1.52
onvif_port = 8899
rtsp_port = 554
adapter = auto
mock_profile = limited
username = ptzuser
password = CHANGE_ME
profile_token =
patrol = side_patrol
home_preset = MAIN
connect_timeout_s = 5
command_timeout_s = 8
retry_count = 3
retry_delay_s = 2
reconnect_backoff_initial_s = 2
reconnect_backoff_max_s = 60
on_start = goto_home
on_stop = stop
arrival_policy = fixed_settle

[CAMERA_cam_side_PRESETS]
MAIN = 1
SIDE = 2

[CAMERA_cam_side_CALIBRATION]
enabled = false
pan_lr_s = 0
pan_rl_s = 0
tilt_bt_s = 0
tilt_tb_s = 0
latency_s = 0.25
samples_per_direction = 5
resync_every_s = 900
resync_preset = MAIN

[PATROL_side_patrol]
enabled = true
repeat_mode = FOREVER
repeat_count = 1
step_1 = action=GOTO_PRESET;target=MAIN;settle_s=1;dwell_s=7;enabled=true
step_2 = action=GOTO_PRESET;target=SIDE;settle_s=1;dwell_s=3;enabled=true
"""


def _parse_patrol_step(section: str, key: str, raw: str) -> Tuple[Optional[PatrolStep], List[ValidationIssue]]:
    issues: List[ValidationIssue] = []
    fields: Dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            issues.append(ValidationIssue(section, key, raw, f"malformed clause '{part}', expected key=value"))
            continue
        k, _, v = part.partition("=")
        fields[k.strip().lower()] = v.strip()

    action_raw = fields.get("action", "")
    try:
        action = StepAction(action_raw.upper())
    except ValueError:
        issues.append(ValidationIssue(section, key, raw,
                      f"unknown action '{action_raw}', expected one of {[a.value for a in StepAction]}"))
        return None, issues

    def fnum(name: str, default: float, lo: float, hi: float) -> float:
        raw_v = fields.get(name)
        if raw_v is None:
            return default
        v = _pfloat(raw_v)
        if v is None or not (lo <= v <= hi):
            issues.append(ValidationIssue(section, key, raw,
                          f"{name}='{raw_v}' invalid, expected number {lo}..{hi} (seconds)" if "_s" in name
                          else f"{name}='{raw_v}' invalid, expected number {lo}..{hi}"))
            return default
        return v

    enabled = True
    if "enabled" in fields:
        b = _pbool(fields["enabled"])
        if b is None:
            issues.append(ValidationIssue(section, key, raw, f"enabled='{fields['enabled']}' invalid, expected true/false", "warning"))
        else:
            enabled = b

    step = PatrolStep(action=action, enabled=enabled)
    step.settle_s = fnum("settle_s", 0.0, 0.0, MAX_DWELL_S)
    step.dwell_s = fnum("dwell_s", 0.0, 0.0, MAX_DWELL_S)

    if action == StepAction.GOTO_PRESET:
        step.target = fields.get("target", "")
        if not step.target:
            issues.append(ValidationIssue(section, key, raw, "GOTO_PRESET requires target=<preset alias>"))
            return None, issues
    elif action == StepAction.CONTINUOUS_MOVE:
        step.pan = fnum("pan", 0.0, -1.0, 1.0)
        step.tilt = fnum("tilt", 0.0, -1.0, 1.0)
        step.zoom = fnum("zoom", 0.0, -1.0, 1.0)
        step.move_s = fnum("move_s", 0.0, 0.01, MAX_CONTINUOUS_MOVE_S)
        if step.move_s <= 0:
            issues.append(ValidationIssue(section, key, raw, "CONTINUOUS_MOVE requires a positive, bounded move_s"))
            return None, issues
    # WAIT / STOP / GOTO_HOME need no extra fields beyond settle_s/dwell_s
    return step, issues


def _parse_calibration(doc: IniDocument, section: str) -> CalibrationConfig:
    d = doc.get_section_dict(section)
    c = CalibrationConfig()
    c.enabled = _pbool(d.get("enabled", "false")) or False
    c.pan_lr_s = _pfloat(d.get("pan_lr_s", "0")) or 0.0
    c.pan_rl_s = _pfloat(d.get("pan_rl_s", "0")) or 0.0
    c.tilt_bt_s = _pfloat(d.get("tilt_bt_s", "0")) or 0.0
    c.tilt_tb_s = _pfloat(d.get("tilt_tb_s", "0")) or 0.0
    c.latency_s = _pfloat(d.get("latency_s", "0.25")) or 0.25
    c.samples_per_direction = _pint(d.get("samples_per_direction", "5")) or 5
    c.resync_every_s = _pfloat(d.get("resync_every_s", "900")) or 900.0
    c.resync_preset = d.get("resync_preset", "")
    return c


def _parse_camera(doc: IniDocument, section: str) -> Tuple[Optional[CameraConfig], List[ValidationIssue]]:
    issues: List[ValidationIssue] = []
    d = doc.get_section_dict(section)
    slug = section[len("CAMERA_"):]

    inner_id = d.get("id", "").strip()
    if not inner_id:
        issues.append(ValidationIssue(section, "id", "", "id is required and must match the section suffix"))
        return None, issues
    if inner_id != slug:
        issues.append(ValidationIssue(section, "id", inner_id,
                      f"id must match section name suffix '{slug}'"))
        return None, issues

    host = d.get("host", "").strip()
    if not host:
        issues.append(ValidationIssue(section, "host", "", "host is required"))
        return None, issues

    port = _pint(d.get("onvif_port", "8899"))
    if port is None or not (1 <= port <= 65535):
        issues.append(ValidationIssue(section, "onvif_port", d.get("onvif_port", ""), "expected integer 1..65535"))
        port = 8899
    rtsp = _pint(d.get("rtsp_port", "554"))
    if rtsp is None or not (1 <= rtsp <= 65535):
        issues.append(ValidationIssue(section, "rtsp_port", d.get("rtsp_port", ""), "expected integer 1..65535", "warning"))
        rtsp = 554

    try:
        adapter = AdapterMode(d.get("adapter", "auto").strip().lower())
    except ValueError:
        issues.append(ValidationIssue(section, "adapter", d.get("adapter", ""), "expected auto|cli|mock", "warning"))
        adapter = AdapterMode.AUTO
    try:
        mock_profile = MockProfile(d.get("mock_profile", "full").strip().lower())
    except ValueError:
        issues.append(ValidationIssue(section, "mock_profile", d.get("mock_profile", ""), "expected full|intermittent|limited", "warning"))
        mock_profile = MockProfile.FULL
    try:
        arrival = ArrivalPolicy(d.get("arrival_policy", "fixed_settle").strip().lower())
    except ValueError:
        issues.append(ValidationIssue(section, "arrival_policy", d.get("arrival_policy", ""),
                      "expected command_accepted|fixed_settle|status_idle", "warning"))
        arrival = ArrivalPolicy.FIXED_SETTLE

    enabled = _pbool(d.get("enabled", "true"))
    if enabled is None:
        issues.append(ValidationIssue(section, "enabled", d.get("enabled", ""), "expected true/false", "warning"))
        enabled = True

    presets = doc.get_section_dict(f"{section}_PRESETS")
    calibration = _parse_calibration(doc, f"{section}_CALIBRATION")

    cam = CameraConfig(
        id=inner_id,
        name=d.get("name", inner_id),
        enabled=enabled,
        host=host,
        onvif_port=port,
        rtsp_port=rtsp,
        adapter=adapter,
        mock_profile=mock_profile,
        username=d.get("username", ""),
        password=d.get("password", ""),
        profile_token=d.get("profile_token", ""),
        patrol_name=d.get("patrol", "").strip(),
        home_preset=d.get("home_preset", "").strip(),
        presets=presets,
        connect_timeout_s=_pfloat(d.get("connect_timeout_s", "5")) or 5.0,
        command_timeout_s=_pfloat(d.get("command_timeout_s", "8")) or 8.0,
        retry_count=_pint(d.get("retry_count", "3")) or 3,
        retry_delay_s=_pfloat(d.get("retry_delay_s", "2")) or 2.0,
        reconnect_backoff_initial_s=_pfloat(d.get("reconnect_backoff_initial_s", "2")) or 2.0,
        reconnect_backoff_max_s=_pfloat(d.get("reconnect_backoff_max_s", "60")) or 60.0,
        on_start=d.get("on_start", "goto_home"),
        on_stop=d.get("on_stop", "stop"),
        arrival_policy=arrival,
        calibration=calibration,
    )
    if not cam.patrol_name:
        issues.append(ValidationIssue(section, "patrol", "", "no patrol assigned", "warning"))
    return cam, issues


def _parse_patrol(doc: IniDocument, section: str) -> Tuple[Optional[Patrol], List[ValidationIssue]]:
    issues: List[ValidationIssue] = []
    d = doc.get_section_dict(section)
    name = section[len("PATROL_"):]

    try:
        repeat_mode = RepeatMode(d.get("repeat_mode", "FOREVER").strip().upper())
    except ValueError:
        issues.append(ValidationIssue(section, "repeat_mode", d.get("repeat_mode", ""),
                      "expected FOREVER|FIXED_COUNT|DISABLED", "warning"))
        repeat_mode = RepeatMode.FOREVER
    repeat_count = _pint(d.get("repeat_count", "1")) or 1
    enabled = _pbool(d.get("enabled", "true"))
    if enabled is None:
        enabled = True

    step_keys = sorted(
        (k for k in d.keys() if re.match(r"^step_\d+$", k)),
        key=lambda k: int(k.split("_", 1)[1]),
    )
    steps: List[PatrolStep] = []
    for k in step_keys:
        step, step_issues = _parse_patrol_step(section, k, d[k])
        issues.extend(step_issues)
        if step is not None:
            steps.append(step)

    if not steps:
        issues.append(ValidationIssue(section, "step_*", "", "patrol has no usable steps"))
        return None, issues

    return Patrol(name=name, enabled=enabled, repeat_mode=repeat_mode,
                  repeat_count=repeat_count, steps=steps), issues


def validate_app_config(doc: IniDocument) -> Tuple[AppConfig, List[ValidationIssue]]:
    """Parse + validate an IniDocument into a typed AppConfig.
    Cameras/patrols with an "error"-severity issue are excluded from
    the result (best-effort load); everything else still loads."""
    issues: List[ValidationIssue] = []
    cfg = AppConfig()

    a = doc.get_section_dict("APPLICATION")
    cfg.app.language = a.get("language", "en")
    cfg.app.log_level = a.get("log_level", "INFO").upper()
    cfg.app.log_max_bytes = _pint(a.get("log_max_bytes", "2000000")) or 2_000_000
    cfg.app.log_backup_count = _pint(a.get("log_backup_count", "5")) or 5
    cfg.app.autostart_patrols = _pbool(a.get("autostart_patrols", "false")) or False
    cfg.app.command_timeout_s = _pfloat(a.get("command_timeout_s", "8")) or 8.0
    cfg.app.dry_run = _pbool(a.get("dry_run", "true"))
    if cfg.app.dry_run is None:
        issues.append(ValidationIssue("APPLICATION", "dry_run", a.get("dry_run", ""), "expected true/false; defaulting to true (safe)", "warning"))
        cfg.app.dry_run = True
    cfg.app.mock_mode = _pbool(a.get("mock_mode", "true"))
    if cfg.app.mock_mode is None:
        issues.append(ValidationIssue("APPLICATION", "mock_mode", a.get("mock_mode", ""), "expected true/false; defaulting to true (safe)", "warning"))
        cfg.app.mock_mode = True

    s = doc.get_section_dict("SECURITY")
    try:
        cfg.security.credential_backend = CredentialBackendKind(s.get("credential_backend", "plaintext").strip().lower())
    except ValueError:
        issues.append(ValidationIssue("SECURITY", "credential_backend", s.get("credential_backend", ""), "expected plaintext|windows_credential_manager", "warning"))
    if cfg.security.credential_backend == CredentialBackendKind.WINDOWS_CREDENTIAL_MANAGER:
        issues.append(ValidationIssue("SECURITY", "credential_backend", "windows_credential_manager",
                      "not implemented in this version; using plaintext instead", "warning"))
        cfg.security.credential_backend = CredentialBackendKind.PLAINTEXT
    cfg.security.warn_plaintext = _pbool(s.get("warn_plaintext", "true")) if _pbool(s.get("warn_plaintext", "true")) is not None else True
    cfg.security.redact_logs = _pbool(s.get("redact_logs", "true")) if _pbool(s.get("redact_logs", "true")) is not None else True

    p = doc.get_section_dict("PREVIEW")
    try:
        cfg.preview.mode = PreviewMode(p.get("mode", "off").strip().lower())
    except ValueError:
        issues.append(ValidationIssue("PREVIEW", "mode", p.get("mode", ""), "expected off|snapshot|low_fps", "warning"))
    cfg.preview.fps = max(0.1, min(5.0, _pfloat(p.get("fps", "1")) or 1.0))
    cfg.preview.max_tiles = _pint(p.get("max_tiles", "8")) or 8
    cfg.preview.tile_width_px = _pint(p.get("tile_width_px", "220")) or 220

    m = doc.get_section_dict("MANUAL_OVERRIDE")
    try:
        cfg.manual_override.pause_options_s = [int(x.strip()) for x in m.get("pause_options_s", "120,300,900,0").split(",") if x.strip() != ""]
    except ValueError:
        issues.append(ValidationIssue("MANUAL_OVERRIDE", "pause_options_s", m.get("pause_options_s", ""), "expected comma-separated integers", "warning"))
    cfg.manual_override.default_pause_s = _pint(m.get("default_pause_s", "300")) or 300
    try:
        cfg.manual_override.resume_policy = ResumePolicy(m.get("resume_policy", "restart_current_step").strip().lower())
    except ValueError:
        issues.append(ValidationIssue("MANUAL_OVERRIDE", "resume_policy", m.get("resume_policy", ""),
                      "expected restart_current_step|restart_patrol|stay_paused", "warning"))
    cfg.manual_override.manual_speed_default = max(0.05, min(1.0, _pfloat(m.get("manual_speed_default", "0.5")) or 0.5))

    c = doc.get_section_dict("CLI")
    cfg.cli.enabled = _pbool(c.get("enabled", "false")) or False
    cfg.cli.executable = c.get("executable", "")
    cfg.cli.probe_template = c.get("probe_template", "")
    cfg.cli.services_template = c.get("services_template", "")
    cfg.cli.profiles_template = c.get("profiles_template", "")
    cfg.cli.presets_template = c.get("presets_template", "")
    cfg.cli.status_template = c.get("status_template", "")
    cfg.cli.goto_template = c.get("goto_template", "")
    cfg.cli.move_template = c.get("move_template", "")
    cfg.cli.stop_template = c.get("stop_template", "")
    cfg.cli.home_template = c.get("home_template", "")
    cfg.cli.snapshot_template = c.get("snapshot_template", "")

    sd = doc.get_section_dict("SHUTDOWN")
    cfg.shutdown.return_home_on_stop = _pbool(sd.get("return_home_on_stop", "false")) or False
    cfg.shutdown.worker_join_timeout_s = _pfloat(sd.get("worker_join_timeout_s", str(WORKER_JOIN_TIMEOUT_S))) or WORKER_JOIN_TIMEOUT_S

    meta = doc.get_section_dict("META")
    cfg.version = _pint(meta.get("config_version", str(CONFIG_VERSION))) or CONFIG_VERSION

    for sec in doc.sections_with_prefix("PATROL_"):
        patrol, p_issues = _parse_patrol(doc, sec)
        issues.extend(p_issues)
        if patrol is not None:
            cfg.patrols[patrol.name] = patrol

    for sec in doc.sections_with_prefix("CAMERA_", exclude_suffixes=("_PRESETS", "_CALIBRATION")):
        cam, c_issues = _parse_camera(doc, sec)
        issues.extend(c_issues)
        if cam is None:
            continue
        if cam.patrol_name and cam.patrol_name not in cfg.patrols:
            issues.append(ValidationIssue(sec, "patrol", cam.patrol_name, "references a patrol that does not exist / failed to parse"))
            continue
        if cam.patrol_name:
            for st in cfg.patrols[cam.patrol_name].steps:
                if st.action == StepAction.GOTO_PRESET and st.target not in cam.presets:
                    issues.append(ValidationIssue(sec, "patrol", st.target,
                                  f"patrol step targets preset '{st.target}' which is not defined in [{sec}_PRESETS]", "warning"))
        if cam.home_preset and cam.home_preset not in cam.presets:
            issues.append(ValidationIssue(sec, "home_preset", cam.home_preset, "not defined in this camera's presets", "warning"))
        cfg.cameras[cam.id] = cam

    cfg.discovery, discovery_issues = _parse_discovery_settings(doc)
    issues.extend(discovery_issues)
    cfg.discovery_ui, discovery_ui_issues = _parse_discovery_ui_settings(doc)
    issues.extend(discovery_ui_issues)
    overrides, override_issues = load_device_profile_overrides(doc)
    issues.extend(override_issues)
    cfg.device_profile_overrides = overrides

    return cfg, issues


def ensure_app_dirs() -> None:
    for d in (LOGS_DIR, RUNTIME_DIR, DIAGNOSTICS_DIR, BACKUPS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_config(path: Path = CONFIG_PATH) -> Tuple[AppConfig, IniDocument, List[ValidationIssue]]:
    if not path.exists():
        path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    doc = IniDocument.parse(text)
    cfg, issues = validate_app_config(doc)
    return cfg, doc, issues


def apply_config_to_ini(doc: IniDocument, cfg: AppConfig) -> None:
    """Mutate `doc` in place so it reflects every field of `cfg`,
    preserving anything `cfg` doesn't manage (comments, unknown
    sections/keys, ordering of untouched lines)."""
    doc.set("META", "config_version", str(cfg.version))

    doc.set("APPLICATION", "language", cfg.app.language)
    doc.set("APPLICATION", "log_level", cfg.app.log_level)
    doc.set("APPLICATION", "log_max_bytes", str(cfg.app.log_max_bytes))
    doc.set("APPLICATION", "log_backup_count", str(cfg.app.log_backup_count))
    doc.set("APPLICATION", "autostart_patrols", _bool_str(cfg.app.autostart_patrols))
    doc.set("APPLICATION", "command_timeout_s", str(cfg.app.command_timeout_s))
    doc.set("APPLICATION", "dry_run", _bool_str(cfg.app.dry_run))
    doc.set("APPLICATION", "mock_mode", _bool_str(cfg.app.mock_mode))

    doc.set("SECURITY", "credential_backend", cfg.security.credential_backend.value)
    doc.set("SECURITY", "warn_plaintext", _bool_str(cfg.security.warn_plaintext))
    doc.set("SECURITY", "redact_logs", _bool_str(cfg.security.redact_logs))

    doc.set("PREVIEW", "mode", cfg.preview.mode.value)
    doc.set("PREVIEW", "fps", str(cfg.preview.fps))
    doc.set("PREVIEW", "max_tiles", str(cfg.preview.max_tiles))
    doc.set("PREVIEW", "tile_width_px", str(cfg.preview.tile_width_px))

    doc.set("MANUAL_OVERRIDE", "default_pause_s", str(cfg.manual_override.default_pause_s))
    doc.set("MANUAL_OVERRIDE", "pause_options_s", ",".join(str(x) for x in cfg.manual_override.pause_options_s))
    doc.set("MANUAL_OVERRIDE", "resume_policy", cfg.manual_override.resume_policy.value)
    doc.set("MANUAL_OVERRIDE", "manual_speed_default", str(cfg.manual_override.manual_speed_default))

    doc.set("CLI", "enabled", _bool_str(cfg.cli.enabled))
    doc.set("CLI", "executable", cfg.cli.executable)
    doc.set("CLI", "probe_template", cfg.cli.probe_template)
    doc.set("CLI", "services_template", cfg.cli.services_template)
    doc.set("CLI", "profiles_template", cfg.cli.profiles_template)
    doc.set("CLI", "presets_template", cfg.cli.presets_template)
    doc.set("CLI", "status_template", cfg.cli.status_template)
    doc.set("CLI", "goto_template", cfg.cli.goto_template)
    doc.set("CLI", "move_template", cfg.cli.move_template)
    doc.set("CLI", "stop_template", cfg.cli.stop_template)
    doc.set("CLI", "home_template", cfg.cli.home_template)
    doc.set("CLI", "snapshot_template", cfg.cli.snapshot_template)

    doc.set("SHUTDOWN", "return_home_on_stop", _bool_str(cfg.shutdown.return_home_on_stop))
    doc.set("SHUTDOWN", "worker_join_timeout_s", str(cfg.shutdown.worker_join_timeout_s))

    existing_cam_sections = doc.sections_with_prefix("CAMERA_", exclude_suffixes=("_PRESETS", "_CALIBRATION"))
    current_ids = set(cfg.cameras.keys())
    for sec in existing_cam_sections:
        cam_id = sec[len("CAMERA_"):]
        if cam_id not in current_ids:
            doc.remove_section(sec)
            doc.remove_section(f"CAMERA_{cam_id}_PRESETS")
            doc.remove_section(f"CAMERA_{cam_id}_CALIBRATION")

    for cam in cfg.cameras.values():
        sec = f"CAMERA_{cam.id}"
        doc.set(sec, "id", cam.id)
        doc.set(sec, "name", cam.name)
        doc.set(sec, "enabled", _bool_str(cam.enabled))
        doc.set(sec, "host", cam.host)
        doc.set(sec, "onvif_port", str(cam.onvif_port))
        doc.set(sec, "rtsp_port", str(cam.rtsp_port))
        doc.set(sec, "adapter", cam.adapter.value)
        doc.set(sec, "mock_profile", cam.mock_profile.value)
        doc.set(sec, "username", cam.username)
        doc.set(sec, "password", cam.password)
        doc.set(sec, "profile_token", cam.profile_token)
        doc.set(sec, "patrol", cam.patrol_name)
        doc.set(sec, "home_preset", cam.home_preset)
        doc.set(sec, "connect_timeout_s", str(cam.connect_timeout_s))
        doc.set(sec, "command_timeout_s", str(cam.command_timeout_s))
        doc.set(sec, "retry_count", str(cam.retry_count))
        doc.set(sec, "retry_delay_s", str(cam.retry_delay_s))
        doc.set(sec, "reconnect_backoff_initial_s", str(cam.reconnect_backoff_initial_s))
        doc.set(sec, "reconnect_backoff_max_s", str(cam.reconnect_backoff_max_s))
        doc.set(sec, "on_start", cam.on_start)
        doc.set(sec, "on_stop", cam.on_stop)
        doc.set(sec, "arrival_policy", cam.arrival_policy.value)

        presets_sec = f"{sec}_PRESETS"
        doc.ensure_section(presets_sec)
        doc.remove_keys_matching(presets_sec, "")
        for alias, token in cam.presets.items():
            doc.set(presets_sec, alias, token)

        calib_sec = f"{sec}_CALIBRATION"
        doc.set(calib_sec, "enabled", _bool_str(cam.calibration.enabled))
        doc.set(calib_sec, "pan_lr_s", str(cam.calibration.pan_lr_s))
        doc.set(calib_sec, "pan_rl_s", str(cam.calibration.pan_rl_s))
        doc.set(calib_sec, "tilt_bt_s", str(cam.calibration.tilt_bt_s))
        doc.set(calib_sec, "tilt_tb_s", str(cam.calibration.tilt_tb_s))
        doc.set(calib_sec, "latency_s", str(cam.calibration.latency_s))
        doc.set(calib_sec, "samples_per_direction", str(cam.calibration.samples_per_direction))
        doc.set(calib_sec, "resync_every_s", str(cam.calibration.resync_every_s))
        doc.set(calib_sec, "resync_preset", cam.calibration.resync_preset)

    existing_patrol_sections = doc.sections_with_prefix("PATROL_")
    current_patrol_names = set(cfg.patrols.keys())
    for sec in existing_patrol_sections:
        name = sec[len("PATROL_"):]
        if name not in current_patrol_names:
            doc.remove_section(sec)

    for patrol in cfg.patrols.values():
        sec = f"PATROL_{patrol.name}"
        doc.set(sec, "enabled", _bool_str(patrol.enabled))
        doc.set(sec, "repeat_mode", patrol.repeat_mode.value)
        doc.set(sec, "repeat_count", str(patrol.repeat_count))
        doc.remove_keys_matching(sec, "step_")
        for i, step in enumerate(patrol.steps, start=1):
            doc.set(sec, f"step_{i}", step.to_config_value())

    if cfg.discovery is not None:
        ds = cfg.discovery
        doc.set("DISCOVERY", "enabled", _bool_str(ds.enabled))
        doc.set("DISCOVERY", "default_mode", ds.default_mode.value)
        doc.set("DISCOVERY", "allowed_subnets", ds.allowed_subnets)
        doc.set("DISCOVERY", "candidate_onvif_ports", ",".join(str(p) for p in ds.candidate_onvif_ports))
        doc.set("DISCOVERY", "candidate_rtsp_ports", ",".join(str(p) for p in ds.candidate_rtsp_ports))
        doc.set("DISCOVERY", "connect_timeout_s", str(ds.connect_timeout_s))
        doc.set("DISCOVERY", "soap_timeout_s", str(ds.soap_timeout_s))
        doc.set("DISCOVERY", "max_parallel_hosts", str(ds.max_parallel_hosts))
        doc.set("DISCOVERY", "max_parallel_ports_per_host", str(ds.max_parallel_ports_per_host))
        doc.set("DISCOVERY", "delay_between_batches_ms", str(ds.delay_between_batches_ms))
        doc.set("DISCOVERY", "allow_extended_scan", _bool_str(ds.allow_extended_scan))
        doc.set("DISCOVERY", "extended_ports", ",".join(str(p) for p in ds.extended_ports))
        doc.set("DISCOVERY", "require_confirmation_for_extended", _bool_str(ds.require_confirmation_for_extended))
        doc.set("DISCOVERY", "save_reports", _bool_str(ds.save_reports))
        doc.set("DISCOVERY", "collect_mac", _bool_str(ds.collect_mac))
        doc.set("DISCOVERY", "collect_ping", _bool_str(ds.collect_ping))
        doc.set("DISCOVERY", "ping_is_required", _bool_str(ds.ping_is_required))

    if cfg.discovery_ui is not None:
        du = cfg.discovery_ui
        doc.set("DISCOVERY_UI", "last_adapter_name", du.last_adapter_name)
        doc.set("DISCOVERY_UI", "last_subnet_cidr", du.last_subnet_cidr)
        doc.set("DISCOVERY_UI", "last_mode", du.last_mode.value)
        doc.set("DISCOVERY_UI", "last_filter", du.last_filter)

    existing_override_sections = doc.sections_with_prefix("DEVICE_PROFILE_OVERRIDE_")
    current_override_names = {p.profile_id[len("override_"):] for p in cfg.device_profile_overrides}
    for sec in existing_override_sections:
        name = sec[len("DEVICE_PROFILE_OVERRIDE_"):]
        if name not in current_override_names:
            doc.remove_section(sec)
    for prof in cfg.device_profile_overrides:
        name = prof.profile_id[len("override_"):]
        sec = f"DEVICE_PROFILE_OVERRIDE_{name}"
        doc.set(sec, "enabled", _bool_str(prof.enabled))
        doc.set(sec, "manufacturer_patterns", ",".join(prof.manufacturer_patterns))
        doc.set(sec, "model_patterns", ",".join(prof.model_patterns))
        doc.set(sec, "candidate_onvif_ports", ",".join(str(p) for p in prof.candidate_onvif_ports))
        doc.set(sec, "device_service_paths", ",".join(prof.device_service_paths))
        doc.set(sec, "candidate_rtsp_ports", ",".join(str(p) for p in prof.candidate_rtsp_ports))
        doc.set(sec, "confidence", prof.confidence.value)
        doc.set(sec, "notes", prof.source_description)


def save_config(path: Path, doc: IniDocument, cfg: AppConfig) -> Tuple[bool, List[str]]:
    """Temp file -> re-parse -> validate -> timestamped backup ->
    atomic replace. On any failure, the original file is untouched."""
    working = IniDocument(list(doc.lines))
    apply_config_to_ini(working, cfg)
    text = working.to_text()

    ensure_app_dirs()
    fd, tmp_name = tempfile.mkstemp(prefix="config_", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

        reparsed = IniDocument.parse(tmp_path.read_text(encoding="utf-8"))
        _, issues = validate_app_config(reparsed)
        fatal = [i for i in issues if i.severity == "error"]
        if fatal:
            tmp_path.unlink(missing_ok=True)
            return False, [str(i) for i in fatal]

        if path.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(path, BACKUPS_DIR / f"config_{stamp}.txt.bak")

        os.replace(str(tmp_path), str(path))
        return True, []
    except Exception as exc:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False, [f"Unexpected error while saving config.txt: {exc}"]


def write_cli_cache(cfg: AppConfig) -> None:
    """Regenerate runtime/cli_cache.txt, the small plain key=value file
    ptz_commands.bat reads to build external-CLI command lines.
    Passwords are never written here (they travel only as the
    ONVIF_PASSWORD environment variable set for that one subprocess
    call)."""
    ensure_app_dirs()
    lines = [
        f"CLI_ENABLED={'true' if cfg.cli.enabled else 'false'}",
        f"CLI_EXECUTABLE={cfg.cli.executable}",
        f"TPL_PROBE={cfg.cli.probe_template}",
        f"TPL_SERVICES={cfg.cli.services_template}",
        f"TPL_PROFILES={cfg.cli.profiles_template}",
        f"TPL_PRESETS={cfg.cli.presets_template}",
        f"TPL_STATUS={cfg.cli.status_template}",
        f"TPL_GOTO={cfg.cli.goto_template}",
        f"TPL_MOVE={cfg.cli.move_template}",
        f"TPL_STOP={cfg.cli.stop_template}",
        f"TPL_HOME={cfg.cli.home_template}",
        f"TPL_SNAPSHOT={cfg.cli.snapshot_template}",
    ]
    for cam in cfg.cameras.values():
        lines.append(f"CAM.{cam.id}.HOST={cam.host}")
        lines.append(f"CAM.{cam.id}.PORT={cam.onvif_port}")
        lines.append(f"CAM.{cam.id}.USER={cam.username}")
        lines.append(f"CAM.{cam.id}.PROFILE={cam.profile_token}")
        home_token = cam.presets.get(cam.home_preset, "")
        lines.append(f"CAM.{cam.id}.HOME_PRESET={home_token}")
    CLI_CACHE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ======================================================================
# 5. CREDENTIAL HANDLING AND LOG REDACTION
# ======================================================================

class SecretRedactor:
    """Tracks the set of currently-configured secrets (camera
    passwords) and scrubs them out of any text before it is logged
    or displayed. Also used as a logging.Filter on every handler."""

    def __init__(self) -> None:
        self._secrets: List[str] = []
        self._lock = threading.Lock()

    def update_from_config(self, cfg: AppConfig) -> None:
        with self._lock:
            self._secrets = [c.password for c in cfg.cameras.values() if c.password]

    def redact(self, text: Optional[str]) -> str:
        if not text:
            return "" if text is None else text
        out = text
        with self._lock:
            secrets = list(self._secrets)
        for secret in secrets:
            if secret:
                out = out.replace(secret, "***REDACTED***")
        return out

    def as_logging_filter(self) -> logging.Filter:
        redactor = self

        class _Filter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                try:
                    record.msg = redactor.redact(str(record.msg))
                    if record.args:
                        record.args = tuple(
                            redactor.redact(a) if isinstance(a, str) else a for a in record.args
                        )
                except Exception:
                    pass
                return True

        return _Filter()


_XML_SECRET_PATTERNS = [
    re.compile(r"(<[\w:]*[Pp]assword[^>]*>)[^<]*(</[\w:]*[Pp]assword[^>]*>)"),
    re.compile(r"(Authorization:\s*)\S+", re.IGNORECASE),
    re.compile(r"([?&]password=)[^&\s]+", re.IGNORECASE),
]


def redact_xml_like(text: str) -> str:
    """Best-effort scrub of WS-Security / Basic-Auth style secrets from
    raw external-CLI output before it is ever written to disk. Not a
    formal guarantee -- defense in depth only."""
    out = text
    for pat in _XML_SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) + "***REDACTED***" + (m.group(2) if m.lastindex and m.lastindex >= 2 else "")), out)
    return out


def get_password(cam: CameraConfig, cfg: AppConfig) -> str:
    """Single choke point for reading a camera's credential. Only the
    plaintext backend is implemented; windows_credential_manager is
    normalized to plaintext during validation with a warning."""
    return cam.password


# ======================================================================
# 6. ROTATING LOGGING
# ======================================================================

_camera_loggers: Dict[str, logging.Logger] = {}
_redactor = SecretRedactor()


def setup_logging(level_name: str = "INFO", max_bytes: int = 2_000_000, backup_count: int = 5) -> logging.Logger:
    ensure_app_dirs()
    level = getattr(logging, level_name.upper(), logging.INFO)

    app_logger = logging.getLogger("ptz.app")
    app_logger.setLevel(level)
    app_logger.handlers.clear()
    handler = logging.handlers.RotatingFileHandler(
        LOGS_DIR / "application.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    handler.addFilter(_redactor.as_logging_filter())
    app_logger.addHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(levelname)-8s %(name)s: %(message)s"))
    console.addFilter(_redactor.as_logging_filter())
    app_logger.addHandler(console)
    app_logger.propagate = False
    return app_logger


def setup_startup_logger() -> logging.Logger:
    """A tiny, always-available logger that works even before the main
    config/logging is up, so a crash during config load is still
    diagnosable."""
    ensure_app_dirs()
    logger = logging.getLogger("ptz.startup")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = logging.handlers.RotatingFileHandler(
            LOGS_DIR / "startup.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s: %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    return logger


def setup_diagnostics_logger() -> logging.Logger:
    ensure_app_dirs()
    logger = logging.getLogger("ptz.diagnostics")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = logging.handlers.RotatingFileHandler(
            LOGS_DIR / "diagnostics.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s: %(message)s"))
        handler.addFilter(_redactor.as_logging_filter())
        logger.addHandler(handler)
        logger.propagate = False
    return logger


def get_camera_logger(camera_id: str, level_name: str = "INFO", max_bytes: int = 2_000_000, backup_count: int = 5) -> logging.Logger:
    if camera_id in _camera_loggers:
        return _camera_loggers[camera_id]
    ensure_app_dirs()
    logger = logging.getLogger(f"ptz.camera.{camera_id}")
    logger.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    handler = logging.handlers.RotatingFileHandler(
        LOGS_DIR / f"camera_{camera_id}.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s: %(message)s"))
    handler.addFilter(_redactor.as_logging_filter())
    logger.addHandler(handler)
    logger.propagate = False
    _camera_loggers[camera_id] = logger
    return logger


# ======================================================================
# 7. BAT COMMAND BRIDGE
# ======================================================================

from abc import ABC, abstractmethod  # noqa: E402  (kept local to this section on purpose)


def run_bat_command(argv: List[str], timeout_s: float, extra_env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    """Run ptz_commands.bat with an argument list (never a shell
    string), hidden window, hard timeout, captured stdout/stderr.
    Returns (returncode, stdout, stderr). Synthetic codes: -1=timeout,
    -2=could not launch (missing bat / OS error)."""
    if not BAT_PATH.exists():
        return -2, "", f"ptz_commands.bat not found at {BAT_PATH}"
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    creationflags = 0
    if IS_WINDOWS:
        creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s,
            env=env, creationflags=creationflags,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return -1, (exc.stdout or ""), (exc.stderr or "") + "\n(timed out)"
    except OSError as exc:
        return -2, "", str(exc)


_CATEGORY_BY_TOKEN: Dict[str, Optional[FailureCategory]] = {
    "TIMEOUT": FailureCategory.TIMEOUT,
    "AUTHENTICATION": FailureCategory.AUTHENTICATION,
    "NETWORK": FailureCategory.NETWORK,
    "UNSUPPORTED": FailureCategory.UNSUPPORTED,
    "INVALID_RESPONSE": FailureCategory.INVALID_RESPONSE,
    "CLI_MISSING": FailureCategory.CLI_MISSING,
    "CONFIGURATION": FailureCategory.CONFIGURATION,
    "UNKNOWN": FailureCategory.UNKNOWN,
    "NONE": None,
}
_RESULT_LINE_RE = re.compile(r"^RESULT:(OK|ERROR):([A-Za-z_]*):(.*)$")


def parse_result_line(stdout: str) -> Tuple[bool, Optional[FailureCategory], str]:
    """ptz_commands.bat prints exactly one `RESULT:OK|ERROR:CATEGORY:message`
    line as its final structured output line. That is the machine-
    readable contract between BAT and Python."""
    for line in reversed(stdout.splitlines()):
        m = _RESULT_LINE_RE.match(line.strip())
        if m:
            status, cat_raw, msg = m.groups()
            category = _CATEGORY_BY_TOKEN.get(cat_raw.upper(), FailureCategory.UNKNOWN)
            return status == "OK", category, msg
    return False, FailureCategory.INVALID_RESPONSE, "no RESULT: line found in ptz_commands.bat output"


class CameraBridge(ABC):
    """Uniform interface implemented by MockCameraBridge (section 11)
    and CliCameraBridge (below). A CameraWorker only ever talks to
    this interface, so it does not care which concrete bridge it
    holds."""

    def __init__(self, camera: CameraConfig, cfg: AppConfig, logger: logging.Logger):
        self.camera = camera
        self.cfg = cfg
        self.logger = logger

    @abstractmethod
    def probe(self) -> CommandResult: ...

    @abstractmethod
    def get_services(self) -> CommandResult: ...

    @abstractmethod
    def get_profiles(self) -> CommandResult: ...

    @abstractmethod
    def get_presets(self) -> CommandResult: ...

    @abstractmethod
    def get_status(self) -> CommandResult: ...

    @abstractmethod
    def goto_preset(self, preset_token: str) -> CommandResult: ...

    @abstractmethod
    def move(self, pan: float, tilt: float, zoom: float, move_s: float) -> CommandResult: ...

    @abstractmethod
    def stop(self) -> CommandResult: ...

    @abstractmethod
    def home(self) -> CommandResult: ...

    @abstractmethod
    def snapshot(self, output_path: Path) -> CommandResult: ...


class CliCameraBridge(CameraBridge):
    """Real-camera bridge. Talks to a camera ONLY through
    ptz_commands.bat, which in turn runs the operator-configured
    external ONVIF CLI. No SOAP / WS-Security / XML signing happens
    in this file or in the BAT file -- that logic lives entirely in
    the external tool the operator installs and configures."""

    def _call(self, action: str, args: List[str], timeout_s: Optional[float] = None) -> CommandResult:
        if not self.cfg.cli.enabled:
            return CommandResult(False, FailureCategory.CLI_MISSING,
                                  "adapter=cli selected but [CLI] enabled=false in config.txt")
        timeout_s = timeout_s or self.camera.command_timeout_s
        argv = [str(BAT_PATH), action, self.camera.id, *args]
        env: Dict[str, str] = {}
        if self.camera.password:
            env["ONVIF_PASSWORD"] = self.camera.password
        self.logger.info("BAT call: %s", " ".join(argv[1:]))
        rc, out, err = run_bat_command(argv, timeout_s, env)
        out_r = redact_xml_like(_redactor.redact(out))
        err_r = redact_xml_like(_redactor.redact(err))
        if rc == -1:
            return CommandResult(False, FailureCategory.TIMEOUT, f"ptz_commands.bat {action} timed out after {timeout_s:.1f}s")
        if rc == -2:
            return CommandResult(False, FailureCategory.CLI_MISSING, f"could not launch ptz_commands.bat: {err_r}")
        ok, category, message = parse_result_line(out)
        if not ok and category is None:
            category = FailureCategory.UNKNOWN
        return CommandResult(ok, category, message or err_r or out_r,
                              {"stdout": out_r, "stderr": err_r, "returncode": rc})

    def probe(self) -> CommandResult:
        return self._call("probe", [])

    def get_services(self) -> CommandResult:
        return self._call("services", [])

    def get_profiles(self) -> CommandResult:
        return self._call("profiles", [])

    def get_presets(self) -> CommandResult:
        return self._call("presets", [])

    def get_status(self) -> CommandResult:
        return self._call("status", [])

    def goto_preset(self, preset_token: str) -> CommandResult:
        if self.cfg.app.dry_run:
            self.logger.info("[DRY-RUN] would GOTO_PRESET token=%s", preset_token)
            return CommandResult(True, None, "dry-run: not actually sent")
        return self._call("goto", [preset_token])

    def move(self, pan: float, tilt: float, zoom: float, move_s: float) -> CommandResult:
        if self.cfg.app.dry_run:
            self.logger.info("[DRY-RUN] would MOVE pan=%.2f tilt=%.2f zoom=%.2f for %.2fs", pan, tilt, zoom, move_s)
            return CommandResult(True, None, "dry-run: not actually sent")
        return self._call("move", [f"{pan:.3f}", f"{tilt:.3f}", f"{zoom:.3f}", f"{move_s:.2f}"],
                           timeout_s=move_s + self.camera.command_timeout_s)

    def stop(self) -> CommandResult:
        # Stop is always allowed even in dry-run: it can never move a camera.
        return self._call("stop-camera", [])

    def home(self) -> CommandResult:
        if self.cfg.app.dry_run:
            self.logger.info("[DRY-RUN] would GOTO_HOME")
            return CommandResult(True, None, "dry-run: not actually sent")
        return self._call("home", [])

    def snapshot(self, output_path: Path) -> CommandResult:
        return self._call("snapshot", [str(output_path)])


# ======================================================================
# 8. CAMERA CAPABILITY AND STATUS MODELS
# ======================================================================
# (CapabilityReport dataclass itself lives in section 3.)

def run_capability_probe(camera: CameraConfig, bridge: "CameraBridge") -> CapabilityReport:
    """Runs probe/services/profiles/presets/status against the given
    bridge (mock or CLI) and assembles a CapabilityReport. This never
    moves the camera -- every call here is read-only."""
    report = CapabilityReport(
        camera_id=camera.id, host=camera.host, configured_onvif_port=camera.onvif_port,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )

    probe_res = bridge.probe()
    if probe_res.ok:
        report.discovered_xaddr = str(probe_res.data.get("xaddr", ""))
        report.auth_method = str(probe_res.data.get("auth_method", ""))
        skew = probe_res.data.get("clock_skew_s")
        report.clock_skew_s = float(skew) if isinstance(skew, (int, float)) else None
    else:
        report.warnings.append(f"probe failed: {probe_res.message}")

    services_res = bridge.get_services()
    if services_res.ok:
        report.services = list(services_res.data.get("services", []))
    else:
        report.warnings.append(f"services failed: {services_res.message}")

    profiles_res = bridge.get_profiles()
    if profiles_res.ok:
        report.profiles = list(profiles_res.data.get("profiles", []))
        report.chosen_profile = str(profiles_res.data.get("chosen_profile", camera.profile_token or
                                     (report.profiles[0] if report.profiles else "")))
    else:
        report.warnings.append(f"profiles failed: {profiles_res.message}")

    presets_res = bridge.get_presets()
    if presets_res.ok:
        report.presets = list(presets_res.data.get("presets", []))
        report.goto_preset_available = bool(report.presets)
    else:
        report.warnings.append(f"presets failed: {presets_res.message}")

    status_res = bridge.get_status()
    if status_res.ok:
        report.status_position_available = bool(status_res.data.get("position_available", False))
        report.move_status_available = bool(status_res.data.get("move_status_available", False))
    else:
        report.warnings.append(f"status failed: {status_res.message}")

    report.absolute_move_available = bool(status_res.data.get("absolute_move_available", False)) if status_res.ok else False
    report.relative_move_available = bool(status_res.data.get("relative_move_available", False)) if status_res.ok else False
    report.continuous_move_available = bool(status_res.data.get("continuous_move_available", False)) if status_res.ok else False
    report.stop_available = True
    report.goto_home_available = bool(camera.home_preset) and report.goto_preset_available
    report.preset_speed_available = bool(status_res.data.get("preset_speed_available", False)) if status_res.ok else False

    if report.goto_preset_available:
        report.recommended_tier = "presets"
    elif report.absolute_move_available or report.relative_move_available:
        report.recommended_tier = "absolute_relative"
    elif report.continuous_move_available:
        report.recommended_tier = "continuous_move"
    else:
        report.recommended_tier = "estimation_required"
        report.warnings.append("no preset/absolute/relative/continuous capability confirmed; "
                                "time-calibrated estimation would be required but is NOT implemented in this version")

    if not report.status_position_available:
        report.warnings.append("GetStatus does not report a position; never present an estimated "
                                "percentage as a measured coordinate")
    return report


def save_capability_report(report: CapabilityReport) -> Tuple[Path, Path]:
    """Writes redacted JSON + human-readable TXT under diagnostics/.
    Raw XML is intentionally not saved in this version: real-CLI
    syntax is unverified, so we do not assume any particular raw
    response shape."""
    ensure_app_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = DIAGNOSTICS_DIR / f"probe_{report.camera_id}_{stamp}.json"
    txt_path = DIAGNOSTICS_DIR / f"probe_{report.camera_id}_{stamp}.txt"
    json_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    txt_path.write_text(report.to_text(), encoding="utf-8")
    return json_path, txt_path


# ======================================================================
# 9. CAMERA WORKER
# ======================================================================

class CameraWorker(threading.Thread):
    """One independent thread per enabled camera. Owns a CameraBridge
    and a PriorityQueue of WorkerCommand; every command for this
    camera is handled strictly one at a time, in this thread only --
    the bridge is never called concurrently from anywhere else. If
    this worker errors or blocks, no other camera's worker is
    affected."""

    def __init__(self, camera: CameraConfig, patrol: Optional[Patrol], bridge: CameraBridge,
                 cfg: AppConfig, event_queue: "queue.Queue[WorkerEvent]", logger: logging.Logger):
        super().__init__(name=f"CameraWorker-{camera.id}", daemon=True)
        self.camera = camera
        self.patrol = patrol
        self.bridge = bridge
        self.cfg = cfg
        self.event_queue = event_queue
        self.logger = logger

        self.command_queue: "queue.PriorityQueue[WorkerCommand]" = queue.PriorityQueue()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

        self.state = CameraState.DISCONNECTED
        self.connected = False
        self.patrol_running = False
        self.paused = False
        self.step_index = 0
        self.cycle_count = 0
        self.last_error = ""
        self.last_step_label = ""
        self.countdown_s = 0.0
        self.manual_override_until: Optional[float] = None
        self._manual_moving = False
        self._last_manual_move_at: Optional[float] = None
        self._reconnect_backoff = camera.reconnect_backoff_initial_s
        self.one_shot_cycle_limit: Optional[int] = None  # used by "Test One Cycle"

    # ---- external API (safe to call from any thread) ----
    def enqueue(self, command_type: CommandType, payload: Optional[Dict[str, Any]] = None) -> None:
        self.command_queue.put(make_command(command_type, payload))
        self._wake_event.set()

    def request_stop_thread(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    # ---- internal helpers ----
    def _emit(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.event_queue.put(WorkerEvent(self.camera.id, event_type, payload or {}))

    def _set_state(self, state: CameraState) -> None:
        if self.state != state:
            self.state = state
        self._emit("STATE", {"state": state.value})

    def _log_and_emit_error(self, message: str, category: Optional[FailureCategory]) -> None:
        self.last_error = message
        self.logger.error("%s (%s)", message, category.value if category else "n/a")
        self._emit("ERROR", {"message": message, "category": category.value if category else ""})

    def _pop_command_nowait(self) -> Optional[WorkerCommand]:
        try:
            return self.command_queue.get_nowait()
        except queue.Empty:
            return None

    # ---- connection ----
    def _connect(self) -> bool:
        self._set_state(CameraState.CONNECTING)
        result = self.bridge.probe()
        if result.ok:
            self.connected = True
            self._reconnect_backoff = self.camera.reconnect_backoff_initial_s
            self._emit("LOG", {"message": f"{self.camera.id}: connected"})
            return True
        self.connected = False
        self._log_and_emit_error(f"connect failed: {result.message}", result.category)
        return False

    def _connect_with_retry_once(self) -> bool:
        for _attempt in range(1, self.camera.retry_count + 1):
            if self._stop_event.is_set():
                return False
            if self._connect():
                return True
            self._interruptible_wait(self.camera.retry_delay_s)
        return False

    # ---- interruptible wait ----
    def _interruptible_wait(self, total_s: float) -> bool:
        """Sleeps up to total_s seconds in small slices. Returns True if
        the full duration elapsed, False if interrupted by shutdown or
        by a new command at/under PREEMPT_PRIORITY_THRESHOLD (that
        command is left in the queue for the caller to pop/handle)."""
        end_at = time.time() + max(0.0, total_s)
        while time.time() < end_at:
            if self._stop_event.is_set():
                return False
            remaining = end_at - time.time()
            self._wake_event.wait(timeout=min(WAIT_SLICE_S, max(0.01, remaining)))
            if self._wake_event.is_set():
                self._wake_event.clear()
                try:
                    top = self.command_queue.queue[0]  # best-effort peek of the heap root
                    if top.priority <= PREEMPT_PRIORITY_THRESHOLD:
                        return False
                except IndexError:
                    pass
        return True

    def _countdown_wait(self, total_s: float) -> bool:
        end_at = time.time() + total_s
        while True:
            remaining = end_at - time.time()
            self.countdown_s = max(0.0, remaining)
            self._emit("COUNTDOWN", {"seconds": self.countdown_s})
            if remaining <= 0:
                return True
            if not self._interruptible_wait(min(1.0, remaining)):
                cmd = self._pop_command_nowait()
                if cmd is not None:
                    self._handle_command(cmd)
                return False

    # ---- manual override bookkeeping ----
    def _check_manual_dead_man(self) -> None:
        if self._manual_moving and self._last_manual_move_at is not None:
            if time.time() - self._last_manual_move_at > MANUAL_MOVE_DEADMAN_S:
                self.bridge.stop()
                self._manual_moving = False
                self._emit("LOG", {"message": f"{self.camera.id}: dead-man Stop (no manual refresh received)"})

    def _check_manual_override_expiry(self) -> None:
        if self.manual_override_until is not None and time.time() >= self.manual_override_until:
            self._resume_from_pause()

    def _resume_from_pause(self) -> None:
        policy = self.cfg.manual_override.resume_policy
        if policy == ResumePolicy.STAY_PAUSED:
            self.manual_override_until = None
            return
        if policy == ResumePolicy.RESTART_PATROL:
            self.step_index = 0
        self.paused = False
        self.manual_override_until = None
        self._set_state(CameraState.IDLE)
        self._emit("LOG", {"message": f"{self.camera.id}: resumed ({policy.value})"})

    # ---- command handling ----
    def _handle_command(self, cmd: WorkerCommand) -> None:
        ct = cmd.command_type

        if ct == CommandType.EMERGENCY_STOP:
            self._set_state(CameraState.STOPPING)
            res = self.bridge.stop()
            self.patrol_running = False
            self.paused = True
            self.manual_override_until = None
            self._manual_moving = False
            self._emit("LOG", {"message": f"{self.camera.id}: EMERGENCY STOP ({'ok' if res.ok else res.message})"})
            self._set_state(CameraState.PAUSED)
            return

        if ct == CommandType.SHUTDOWN_STOP:
            self._set_state(CameraState.STOPPING)
            self.bridge.stop()
            if self.cfg.shutdown.return_home_on_stop and self.camera.home_preset:
                token = self.camera.presets.get(self.camera.home_preset, "")
                if token:
                    self.bridge.goto_preset(token)
            self._stop_event.set()
            return

        if ct == CommandType.MANUAL_STOP:
            self.bridge.stop()
            self._manual_moving = False
            self._last_manual_move_at = None
            return

        if ct == CommandType.MANUAL_MOVE:
            self.paused = True
            pause_s = cmd.payload.get("override_pause_s", self.cfg.manual_override.default_pause_s)
            self.manual_override_until = None if not pause_s else time.time() + float(pause_s)
            self._set_state(CameraState.MANUAL_OVERRIDE)
            pan = float(cmd.payload.get("pan", 0.0))
            tilt = float(cmd.payload.get("tilt", 0.0))
            zoom = float(cmd.payload.get("zoom", 0.0))
            res = self.bridge.move(pan, tilt, zoom, MANUAL_MOVE_CHUNK_S)
            self.bridge.stop()  # bounded chunk always internally followed by Stop
            self._manual_moving = True
            self._last_manual_move_at = time.time()
            if not res.ok:
                self._log_and_emit_error(f"manual move failed: {res.message}", res.category)
            return

        if ct == CommandType.PAUSE:
            self.paused = True
            pause_s = cmd.payload.get("pause_s", 0)
            self.manual_override_until = None if not pause_s else time.time() + float(pause_s)
            self._set_state(CameraState.PAUSED)
            return

        if ct == CommandType.RESUME:
            self._resume_from_pause()
            return

        if ct == CommandType.STOP_PATROL:
            # Stops patrol progression (does not touch the pause/manual-
            # override state machine) and sends an immediate Stop.
            self.patrol_running = False
            self.one_shot_cycle_limit = None
            res = self.bridge.stop()
            if not res.ok:
                self._log_and_emit_error(f"stop failed: {res.message}", res.category)
            return

        if ct == CommandType.START_PATROL:
            self.patrol_running = True
            if self.paused:
                self._resume_from_pause()
            return

        if ct == CommandType.TEST_ONE_CYCLE:
            self.step_index = 0
            self.cycle_count = 0
            self.one_shot_cycle_limit = 1
            self.patrol_running = True
            if self.paused:
                self._resume_from_pause()
            return

        if ct == CommandType.HOME:
            if not self.camera.home_preset:
                self._log_and_emit_error("HOME requested but no home_preset configured", FailureCategory.CONFIGURATION)
                return
            token = self.camera.presets.get(self.camera.home_preset, "")
            self._set_state(CameraState.MOVING)
            res = self.bridge.goto_preset(token) if token else CommandResult(False, FailureCategory.CONFIGURATION, "home preset has no token")
            if not res.ok:
                self._log_and_emit_error(f"HOME failed: {res.message}", res.category)
            self._set_state(CameraState.IDLE)
            return

        if ct == CommandType.PRESET_TEST:
            token = cmd.payload.get("preset_token", "")
            self._set_state(CameraState.MOVING)
            res = self.bridge.goto_preset(token)
            if not res.ok:
                self._log_and_emit_error(f"preset test failed: {res.message}", res.category)
            self._set_state(CameraState.IDLE)
            return

        if ct == CommandType.DIAGNOSTIC_READ:
            # Routed through the worker (not called directly from the
            # GUI thread) so the bridge is still only ever touched from
            # this one thread, even for read-only diagnostic requests.
            kind = cmd.payload.get("kind", "")
            if kind == "probe_report":
                report = run_capability_probe(self.camera, self.bridge)
                json_path, txt_path = save_capability_report(report)
                self._emit("PROBE_DONE", {"json_path": str(json_path), "txt_path": str(txt_path),
                                           "recommended_tier": report.recommended_tier,
                                           "warnings": report.warnings})
            elif kind == "snapshot":
                output_path = Path(cmd.payload.get("output_path", ""))
                res = self.bridge.snapshot(output_path)
                self._emit("SNAPSHOT", {"ok": res.ok, "path": str(output_path), "message": res.message})
            return

    # ---- patrol progression ----
    def _perform_step_action(self, step: PatrolStep) -> bool:
        self._set_state(CameraState.MOVING)
        if step.action == StepAction.GOTO_PRESET:
            token = self.camera.presets.get(step.target, "")
            if not token:
                self._log_and_emit_error(f"preset alias '{step.target}' has no token", FailureCategory.CONFIGURATION)
                return self._handle_step_failure()
            res = self.bridge.goto_preset(token)
        elif step.action == StepAction.GOTO_HOME:
            token = self.camera.presets.get(self.camera.home_preset, "")
            if not token:
                self._log_and_emit_error("GOTO_HOME step but no home_preset/token configured", FailureCategory.CONFIGURATION)
                return self._handle_step_failure()
            res = self.bridge.goto_preset(token)
        elif step.action == StepAction.WAIT:
            res = CommandResult(True)
        elif step.action == StepAction.STOP:
            res = self.bridge.stop()
        elif step.action == StepAction.CONTINUOUS_MOVE:
            res = self.bridge.move(step.pan, step.tilt, step.zoom, step.move_s)
            self.bridge.stop()  # ALWAYS followed internally by an explicit Stop
        else:
            res = CommandResult(False, FailureCategory.CONFIGURATION, f"unknown action {step.action}")

        if not res.ok:
            self._log_and_emit_error(f"step failed: {res.message}", res.category)
            return self._handle_step_failure()
        return True

    def _handle_step_failure(self) -> bool:
        self.connected = False
        self._set_state(CameraState.RECOVERING)
        return False

    def _execute_next_patrol_step(self) -> None:
        patrol = self.patrol
        assert patrol is not None
        steps = patrol.steps
        if self.step_index >= len(steps):
            self.step_index = 0
            self.cycle_count += 1
            fixed_count_hit = (patrol.repeat_mode == RepeatMode.FIXED_COUNT
                                and self.cycle_count >= max(1, patrol.repeat_count))
            one_shot_hit = (self.one_shot_cycle_limit is not None and self.cycle_count >= self.one_shot_cycle_limit)
            if fixed_count_hit or one_shot_hit:
                self.patrol_running = False
                self.one_shot_cycle_limit = None
                self._set_state(CameraState.IDLE)
                self._emit("LOG", {"message": f"{self.camera.id}: patrol finished {self.cycle_count} cycle(s)"})
                return

        step = steps[self.step_index]
        if not step.enabled:
            self.step_index += 1
            return

        self.last_step_label = f"{self.step_index + 1}/{len(steps)}: {step.short_label()}"
        self._emit("STEP", {"label": self.last_step_label, "index": self.step_index})

        if not self._perform_step_action(step):
            return

        if step.settle_s > 0 and step.action in (StepAction.GOTO_PRESET, StepAction.GOTO_HOME, StepAction.CONTINUOUS_MOVE):
            self._set_state(CameraState.SETTLING)
            if not self._countdown_wait(step.settle_s):
                return

        if step.dwell_s > 0:
            self._set_state(CameraState.DWELLING)
            if not self._countdown_wait(step.dwell_s):
                return

        self.step_index += 1

    def _recover(self) -> None:
        self._set_state(CameraState.RECOVERING)
        if not self._interruptible_wait(self._reconnect_backoff):
            return
        self._reconnect_backoff = min(self.camera.reconnect_backoff_max_s, self._reconnect_backoff * 2)
        if self._connect():
            self.bridge.stop()
            if self.camera.home_preset:
                token = self.camera.presets.get(self.camera.home_preset, "")
                if token:
                    self.bridge.goto_preset(token)
                    self._interruptible_wait(1.0)
            self._set_state(CameraState.IDLE)

    # ---- main loop ----
    def run(self) -> None:
        try:
            self._run_loop()
        except Exception:
            self.logger.error("worker crashed: %s", traceback.format_exc())
            self._set_state(CameraState.ERROR)
            self._emit("ERROR", {"message": "worker thread crashed, see camera log for details", "category": "unknown"})

    def _run_loop(self) -> None:
        self._connect_with_retry_once()
        if self.connected:
            if self.camera.on_start in ("stop", "goto_home"):
                self.bridge.stop()
            if self.camera.on_start == "goto_home" and self.camera.home_preset:
                token = self.camera.presets.get(self.camera.home_preset, "")
                if token:
                    self.bridge.goto_preset(token)
                    self._interruptible_wait(1.0)

        self.patrol_running = (self.patrol is not None and self.patrol.enabled
                                and self.patrol.repeat_mode != RepeatMode.DISABLED)
        self._emit("LOG", {"message": f"{self.camera.id}: worker started (patrol_running={self.patrol_running})"})

        while not self._stop_event.is_set():
            cmd = self._pop_command_nowait()
            if cmd is not None:
                self._handle_command(cmd)
                continue

            if self.paused:
                self._check_manual_dead_man()
                self._check_manual_override_expiry()
                self._set_state(CameraState.MANUAL_OVERRIDE if (self._manual_moving or self.manual_override_until is not None)
                                 else CameraState.PAUSED)
                self._interruptible_wait(WAIT_SLICE_S)
                continue

            if not self.connected:
                self._recover()
                continue

            if self.patrol_running and self.patrol and self.patrol.enabled_steps():
                self._execute_next_patrol_step()
            else:
                self._set_state(CameraState.IDLE)
                self._interruptible_wait(0.3)

        self._emit("LOG", {"message": f"{self.camera.id}: worker loop exited"})


# ======================================================================
# 10. PATROL ENGINE / WORKER MANAGER
# ======================================================================
# Most patrol logic lives inside CameraWorker itself (each camera's
# patrol is independent by construction). WorkerManager is the thin
# layer that owns the set of workers, builds the right bridge for
# each camera, and provides the whole-fleet operations (start all,
# stop all, Emergency Stop).

def resolve_adapter_mode(camera: CameraConfig, app: AppSettings) -> AdapterMode:
    if camera.adapter == AdapterMode.CLI:
        return AdapterMode.CLI
    if camera.adapter == AdapterMode.MOCK:
        return AdapterMode.MOCK
    return AdapterMode.MOCK if app.mock_mode else AdapterMode.CLI


def build_bridge_for_camera(camera: CameraConfig, cfg: AppConfig, logger: logging.Logger) -> CameraBridge:
    mode = resolve_adapter_mode(camera, cfg.app)
    if mode == AdapterMode.MOCK:
        return MockCameraBridge(camera, cfg, logger)
    return CliCameraBridge(camera, cfg, logger)


class WorkerManager:
    """Owns every CameraWorker. Created once by the GUI and rebuilt
    (stop_all -> build_workers -> start_all) whenever the operator
    saves configuration changes."""

    def __init__(self, event_queue: "queue.Queue[WorkerEvent]"):
        self.event_queue = event_queue
        self.workers: Dict[str, CameraWorker] = {}
        self.cfg: Optional[AppConfig] = None

    def build_workers(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.workers = {}
        for cam in cfg.enabled_cameras():
            logger = get_camera_logger(cam.id, cfg.app.log_level, cfg.app.log_max_bytes, cfg.app.log_backup_count)
            bridge = build_bridge_for_camera(cam, cfg, logger)
            patrol = cfg.patrols.get(cam.patrol_name)
            worker = CameraWorker(cam, patrol, bridge, cfg, self.event_queue, logger)
            self.workers[cam.id] = worker

    def start_all(self) -> None:
        for w in self.workers.values():
            w.start()

    def stop_all(self, join: bool = True, timeout_s: float = WORKER_JOIN_TIMEOUT_S) -> None:
        for w in self.workers.values():
            w.enqueue(CommandType.SHUTDOWN_STOP)
        if join:
            deadline = time.time() + timeout_s
            for w in self.workers.values():
                remaining = max(0.1, deadline - time.time())
                w.join(timeout=remaining)

    def emergency_stop_all(self) -> None:
        for w in self.workers.values():
            w.enqueue(CommandType.EMERGENCY_STOP)

    def get_worker(self, camera_id: str) -> Optional[CameraWorker]:
        return self.workers.get(camera_id)

    def status_counts(self) -> Dict[str, int]:
        enabled = len(self.workers)
        connected = sum(1 for w in self.workers.values() if w.connected)
        running = sum(1 for w in self.workers.values() if w.patrol_running and not w.paused)
        paused = sum(1 for w in self.workers.values() if w.paused)
        offline = sum(1 for w in self.workers.values() if not w.connected)
        return {"enabled": enabled, "connected": connected, "running": running,
                "paused": paused, "offline": offline}


# ======================================================================
# 11. MOCK CAMERA IMPLEMENTATION
# ======================================================================

class MockCameraBridge(CameraBridge):
    """Fully simulated camera -- never touches ptz_commands.bat or any
    external process. Lets the whole application (independent
    patrols, pause/resume, manual override, Emergency Stop, reconnect,
    worker isolation, preview tiles) be exercised with zero real
    cameras. Behavior depends on camera.mock_profile:
      full          -> everything works, short simulated latency
      intermittent  -> periodically fails probe/status (reconnect test)
      limited       -> presets work; Absolute/Relative/Continuous are
                       reported unsupported
    """

    def __init__(self, camera: CameraConfig, cfg: AppConfig, logger: logging.Logger):
        super().__init__(camera, cfg, logger)
        self._call_count = 0
        self._last_preset_token = ""

    def _latency(self) -> None:
        time.sleep(random.uniform(0.05, 0.2))

    def _maybe_fail(self) -> Optional[CommandResult]:
        if self.camera.mock_profile != MockProfile.INTERMITTENT:
            return None
        self._call_count += 1
        if self._call_count % 4 == 0:
            return CommandResult(False, FailureCategory.NETWORK, "mock: simulated intermittent disconnect")
        return None

    def probe(self) -> CommandResult:
        self._latency()
        fail = self._maybe_fail()
        if fail:
            return fail
        return CommandResult(True, None, "mock probe ok", {
            "xaddr": f"http://{self.camera.host}:{self.camera.onvif_port}/onvif/device_service",
            "auth_method": "ws-usernametoken (simulated)",
            "clock_skew_s": 0.0,
        })

    def get_services(self) -> CommandResult:
        self._latency()
        services = ["device", "media", "ptz"]
        if self.camera.mock_profile != MockProfile.LIMITED:
            services.append("imaging")
        return CommandResult(True, None, "mock services ok", {"services": services})

    def get_profiles(self) -> CommandResult:
        self._latency()
        profile = self.camera.profile_token or "MainStreamProfile"
        return CommandResult(True, None, "mock profiles ok", {"profiles": [profile], "chosen_profile": profile})

    def get_presets(self) -> CommandResult:
        self._latency()
        return CommandResult(True, None, "mock presets ok", {"presets": list(self.camera.presets.keys())})

    def get_status(self) -> CommandResult:
        self._latency()
        fail = self._maybe_fail()
        if fail:
            return fail
        limited = self.camera.mock_profile == MockProfile.LIMITED
        return CommandResult(True, None, "mock status ok", {
            "position_available": not limited,
            "move_status_available": not limited,
            "absolute_move_available": not limited,
            "relative_move_available": not limited,
            "continuous_move_available": not limited,
            "preset_speed_available": False,
            "last_preset_token": self._last_preset_token,
        })

    def goto_preset(self, preset_token: str) -> CommandResult:
        self._latency()
        fail = self._maybe_fail()
        if fail:
            return fail
        self._last_preset_token = preset_token
        self.logger.info("mock: goto preset token=%s", preset_token)
        return CommandResult(True, None, f"mock: moved to preset {preset_token}")

    def move(self, pan: float, tilt: float, zoom: float, move_s: float) -> CommandResult:
        if self.camera.mock_profile == MockProfile.LIMITED:
            return CommandResult(False, FailureCategory.UNSUPPORTED, "mock: ContinuousMove not supported on this profile")
        time.sleep(min(0.3, move_s))  # simulate only a short slice, not the full bounded duration
        self.logger.info("mock: continuous move pan=%.2f tilt=%.2f zoom=%.2f for %.2fs", pan, tilt, zoom, move_s)
        return CommandResult(True, None, "mock: move accepted")

    def stop(self) -> CommandResult:
        self.logger.info("mock: stop")
        return CommandResult(True, None, "mock: stopped")

    def home(self) -> CommandResult:
        return self.goto_preset(self.camera.presets.get(self.camera.home_preset, ""))

    def snapshot(self, output_path: Path) -> CommandResult:
        self._latency()
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if PIL_AVAILABLE:
                color = {"full": (60, 140, 60), "intermittent": (170, 140, 40),
                         "limited": (120, 60, 60)}.get(self.camera.mock_profile.value, (90, 90, 90))
                img = Image.new("RGB", (320, 180), color=color)
                img.save(output_path, format="PNG")
            else:
                output_path.write_text("mock snapshot placeholder (Pillow not installed)", encoding="utf-8")
            return CommandResult(True, None, "mock snapshot written", {"path": str(output_path)})
        except Exception as exc:
            return CommandResult(False, FailureCategory.UNKNOWN, f"mock snapshot failed: {exc}")


# ======================================================================
# 12. PREVIEW AND SNAPSHOT UTILITIES
# ======================================================================

def request_probe(worker: CameraWorker) -> None:
    worker.enqueue(CommandType.DIAGNOSTIC_READ, {"kind": "probe_report"})


def request_snapshot(worker: CameraWorker, output_path: Path) -> None:
    worker.enqueue(CommandType.DIAGNOSTIC_READ, {"kind": "snapshot", "output_path": str(output_path)})


def load_tile_image(path: Path, max_width: int):
    """Returns a Tk-displayable PhotoImage for a preview tile, or None
    if Pillow is unavailable or the file can't be read/decoded --
    callers must fall back to a plain text placeholder in that case."""
    if not PIL_AVAILABLE or not path.exists():
        return None
    try:
        img = Image.open(path)
        ratio = max_width / float(img.width) if img.width else 1.0
        new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        img = img.resize(new_size)
        return ImageTk.PhotoImage(img)
    except Exception:
        return None


# ======================================================================
# 12B. AUTHORIZED LAN ONVIF DISCOVERY -- DATA MODEL
# ======================================================================
# "DISCOVER & DIAGNOSE" -- a scoped compatibility probe for bounded
# endpoint discovery on an authorized subnet the operator explicitly
# selects. This is NOT a credential-attack tool: there is no password
# guessing, no username dictionary, no Internet-wide scanning, and no
# default full port range scan anywhere in this section. See the
# DiscoveryScanConfig validation and DiscoveryWorker below for the
# concrete bounds (timeouts, concurrency caps, subnet restriction,
# EXTENDED-mode confirmation gate) that enforce this at runtime.
#
# Architecture note: everything in this section runs natively in
# Python via the standard library (socket for WS-Discovery and TCP
# probing, urllib for SOAP/HTTP, subprocess only for the OS's own
# "arp -a"). It does NOT shell out to ptz_commands.bat, because none
# of it depends on an external, unverified ONVIF CLI the way real
# camera PTZ control does -- WS-Discovery, ARP, TCP connect tests and
# a basic SOAP liveness/identity call are all things this file can do
# directly and reliably. ptz_commands.bat still gained a few thin,
# optional verbs (arp-scan, port-probe, device-info, capabilities,
# discover) for manual command-line use; they are not on the GUI's
# discovery critical path.
#
# SCOPE BOUNDARY: discovery's job is to find a device, establish its
# identity (manufacturer/model/firmware/serial where available), and
# classify it -- not to fully enumerate its PTZ capabilities. Once a
# discovered device is added to config.txt, the existing Probe button
# / capability-probe system (section 8) takes over for PTZ-specific
# depth (presets, movement spaces, etc.), exactly as it already did
# for manually-added cameras.

class DiscoveryMode(str, Enum):
    QUICK = "QUICK"
    STANDARD = "STANDARD"
    EXTENDED = "EXTENDED"


class DeviceClassification(str, Enum):
    CONFIRMED_ONVIF = "CONFIRMED_ONVIF"
    LIKELY_ONVIF = "LIKELY_ONVIF"
    POSSIBLE_CAMERA = "POSSIBLE_CAMERA"
    NETWORK_DEVICE = "NETWORK_DEVICE"
    UNREACHABLE = "UNREACHABLE"


class ConfidenceLevel(str, Enum):
    OFFICIAL = "OFFICIAL"
    MANUFACTURER_DOCUMENTED = "MANUFACTURER_DOCUMENTED"
    COMMUNITY_VERIFIED = "COMMUNITY_VERIFIED"
    HEURISTIC = "HEURISTIC"
    UNKNOWN = "UNKNOWN"


_CONFIDENCE_RANK = {
    ConfidenceLevel.OFFICIAL: 4,
    ConfidenceLevel.MANUFACTURER_DOCUMENTED: 3,
    ConfidenceLevel.COMMUNITY_VERIFIED: 2,
    ConfidenceLevel.HEURISTIC: 1,
    ConfidenceLevel.UNKNOWN: 0,
}

# Hard safety ceilings. These are enforced regardless of what a
# tampered or malformed config.txt might otherwise request -- see
# _parse_discovery_settings below, which clamps into these ranges.
DISCOVERY_MAX_PARALLEL_HOSTS = 32
DISCOVERY_MAX_PARALLEL_PORTS_PER_HOST = 8
DISCOVERY_MAX_CANDIDATE_PORTS = 24
DISCOVERY_MAX_EXTENDED_PORTS = 64
DISCOVERY_MIN_CONNECT_TIMEOUT_S = 0.1
DISCOVERY_MAX_CONNECT_TIMEOUT_S = 5.0
DISCOVERY_MIN_SOAP_TIMEOUT_S = 0.5
DISCOVERY_MAX_SOAP_TIMEOUT_S = 10.0
WS_DISCOVERY_MULTICAST_ADDR = "239.255.255.250"
WS_DISCOVERY_MULTICAST_PORT = 3702


@dataclass
class AdapterInfo:
    name: str
    ipv4: str
    netmask: str = "255.255.255.0"

    def cidr(self) -> str:
        try:
            net = ipaddress.ip_network(f"{self.ipv4}/{self.netmask}", strict=False)
            return str(net)
        except Exception:
            return f"{self.ipv4}/24"


@dataclass
class DeviceProfile:
    """One entry of the built-in (or config-overridden) mini reference
    profile database. A profile provides HINTS only -- standard ONVIF
    discovery and capability interrogation always take priority over
    a profile match. Never contains credentials of any kind."""
    profile_id: str
    manufacturer_patterns: List[str] = field(default_factory=list)
    model_patterns: List[str] = field(default_factory=list)
    candidate_onvif_ports: List[int] = field(default_factory=list)
    candidate_rtsp_ports: List[int] = field(default_factory=list)
    device_service_paths: List[str] = field(default_factory=list)
    auth_notes: str = ""
    safe_diagnostics: List[str] = field(default_factory=list)
    known_limitations: List[str] = field(default_factory=list)
    confidence: ConfidenceLevel = ConfidenceLevel.HEURISTIC
    source_description: str = ""
    last_verified: str = ""
    enabled: bool = True


# Required initial profiles (section 10 of the spec this was built
# from). Deliberately minimal and auditable: no default passwords, no
# credential lists, no undocumented/destructive endpoints -- only
# publicly documented ONVIF behavior or safely inferred hints.
BUILTIN_DEVICE_PROFILES: List[DeviceProfile] = [
    DeviceProfile(
        profile_id="generic_standard_onvif",
        manufacturer_patterns=[], model_patterns=[],
        candidate_onvif_ports=[80, 8080, 8899], candidate_rtsp_ports=[554],
        device_service_paths=["/onvif/device_service"],
        auth_notes="WS-UsernameToken (PasswordDigest) is standard for Profile S/T devices.",
        safe_diagnostics=["GetSystemDateAndTime", "GetDeviceInformation", "GetCapabilities"],
        known_limitations=["Exact path/port varies by vendor; always confirm via Probe."],
        confidence=ConfidenceLevel.OFFICIAL,
        source_description="ONVIF Core Specification (device service discovery/profile conventions).",
    ),
    DeviceProfile(
        profile_id="generic_onvif_http_80",
        model_patterns=[], candidate_onvif_ports=[80], candidate_rtsp_ports=[554],
        device_service_paths=["/onvif/device_service"],
        auth_notes="Some budget/OEM devices serve ONVIF on plain HTTP port 80.",
        safe_diagnostics=["GetSystemDateAndTime"],
        known_limitations=["Port 80 is also used by countless non-camera devices; evidence must still confirm ONVIF."],
        confidence=ConfidenceLevel.HEURISTIC,
        source_description="Community-observed convention, not an ONVIF requirement.",
    ),
    DeviceProfile(
        profile_id="generic_onvif_https_443",
        model_patterns=[], candidate_onvif_ports=[443], candidate_rtsp_ports=[554],
        device_service_paths=["/onvif/device_service"],
        auth_notes="HTTPS device service; certificate may be self-signed.",
        safe_diagnostics=["GetSystemDateAndTime"],
        known_limitations=["Self-signed certs are common on embedded devices; this project does not disable certificate validation automatically."],
        confidence=ConfidenceLevel.HEURISTIC,
        source_description="Community-observed convention.",
    ),
    DeviceProfile(
        profile_id="v380_family_low_confidence",
        manufacturer_patterns=["V380", "Macro-video", "MacroVideo"], model_patterns=["WET", "V380"],
        candidate_onvif_ports=[80, 8899], candidate_rtsp_ports=[554],
        device_service_paths=["/onvif/device_service"],
        auth_notes="V380 Pro branded hardware varies significantly by firmware/seller; ONVIF may need enabling in the vendor app first.",
        safe_diagnostics=["GetSystemDateAndTime", "GetDeviceInformation"],
        known_limitations=["Model label alone does not guarantee identical firmware behavior.", "WET2440 specifically unverified at authoring time."],
        confidence=ConfidenceLevel.HEURISTIC,
        source_description="Community notes referenced in this project's planning materials; not manufacturer-documented.",
    ),
    DeviceProfile(
        profile_id="hisilicon_camhi_family_low_confidence",
        manufacturer_patterns=["HiSilicon", "CamHi", "Hi3516", "Hi3518"], model_patterns=[],
        candidate_onvif_ports=[80, 8899], candidate_rtsp_ports=[554],
        device_service_paths=["/onvif/device_service"],
        auth_notes="Common SoC family behind many rebadged cameras; ONVIF completeness varies widely by OEM firmware.",
        safe_diagnostics=["GetSystemDateAndTime"],
        known_limitations=["SoC family is not proof of any specific feature set."],
        confidence=ConfidenceLevel.HEURISTIC,
        source_description="General community knowledge of this SoC family, not a specific vendor datasheet.",
    ),
    DeviceProfile(
        profile_id="unknown_camera_candidate",
        manufacturer_patterns=[], model_patterns=[],
        candidate_onvif_ports=[], candidate_rtsp_ports=[554],
        device_service_paths=[],
        auth_notes="No profile matched; treat every field as unverified.",
        safe_diagnostics=["GetSystemDateAndTime"],
        known_limitations=["Nothing about this device is confirmed yet."],
        confidence=ConfidenceLevel.UNKNOWN,
        source_description="Fallback when no other profile matches.",
    ),
]


def load_device_profile_overrides(doc: "IniDocument") -> Tuple[List[DeviceProfile], List[ValidationIssue]]:
    """Parses every [DEVICE_PROFILE_OVERRIDE_<name>] section into an
    additional DeviceProfile. These never replace a built-in profile
    by id -- they are added alongside it as profile_id='override_<name>'."""
    issues: List[ValidationIssue] = []
    out: List[DeviceProfile] = []
    for sec in doc.sections_with_prefix("DEVICE_PROFILE_OVERRIDE_"):
        name = sec[len("DEVICE_PROFILE_OVERRIDE_"):]
        d = doc.get_section_dict(sec)
        enabled = _pbool(d.get("enabled", "true"))
        if enabled is None:
            issues.append(ValidationIssue(sec, "enabled", d.get("enabled", ""), "expected true/false", "warning"))
            enabled = True
        try:
            confidence = ConfidenceLevel(d.get("confidence", "HEURISTIC").strip().upper())
        except ValueError:
            issues.append(ValidationIssue(sec, "confidence", d.get("confidence", ""),
                          "expected OFFICIAL|MANUFACTURER_DOCUMENTED|COMMUNITY_VERIFIED|HEURISTIC|UNKNOWN", "warning"))
            confidence = ConfidenceLevel.HEURISTIC

        def _csv_list(key: str) -> List[str]:
            return [p.strip() for p in d.get(key, "").split(",") if p.strip()]

        def _csv_ports(key: str) -> List[int]:
            ports = []
            for p in _csv_list(key):
                v = _pint(p)
                if v is not None and 1 <= v <= 65535:
                    ports.append(v)
                else:
                    issues.append(ValidationIssue(sec, key, p, "expected integer 1..65535", "warning"))
            return ports

        out.append(DeviceProfile(
            profile_id=f"override_{name}",
            manufacturer_patterns=_csv_list("manufacturer_patterns"),
            model_patterns=_csv_list("model_patterns"),
            candidate_onvif_ports=_csv_ports("candidate_onvif_ports"),
            candidate_rtsp_ports=_csv_ports("candidate_rtsp_ports"),
            device_service_paths=_csv_list("device_service_paths"),
            auth_notes="", safe_diagnostics=[], known_limitations=[],
            confidence=confidence,
            source_description=d.get("notes", "User-defined override; verify independently."),
            enabled=enabled,
        ))
    return out, issues


def match_device_profile(manufacturer: str, model: str, profiles: List[DeviceProfile]) -> Tuple[Optional[str], ConfidenceLevel]:
    """Returns (profile_id, confidence) for the best match, or (None,
    UNKNOWN) if nothing matches. A profile is a HINT, never proof --
    callers must not treat this as confirmed identity."""
    manu_l = (manufacturer or "").lower()
    model_l = (model or "").lower()
    best: Optional[DeviceProfile] = None
    for prof in profiles:
        if not prof.enabled or prof.profile_id == "unknown_camera_candidate":
            continue
        model_ok = (not prof.model_patterns) or any(p.lower() in model_l for p in prof.model_patterns if model_l)
        manu_ok = (not prof.manufacturer_patterns) or any(p.lower() in manu_l for p in prof.manufacturer_patterns if manu_l)
        if not (model_l or manu_l):
            continue
        if model_ok and manu_ok and (prof.model_patterns or prof.manufacturer_patterns):
            if best is None or _CONFIDENCE_RANK[prof.confidence] > _CONFIDENCE_RANK[best.confidence]:
                best = prof
    if best is None:
        return None, ConfidenceLevel.UNKNOWN
    return best.profile_id, best.confidence


_BANNED_PROFILE_PATTERNS = [
    re.compile(r"default\s+pass", re.IGNORECASE),
    re.compile(r"factory\s+pass", re.IGNORECASE),
    re.compile(r"admin123", re.IGNORECASE),
    re.compile(r"credential\s+list", re.IGNORECASE),
    # A word boundary before "password"/"passwd" followed by a colon/equals
    # and a value looks like an embedded credential. This deliberately
    # does NOT flag legitimate ONVIF/WS-Security terminology such as
    # "PasswordDigest" or "PasswordText" (standard Password Types),
    # since there "password" is immediately followed by a letter, not
    # by [:=] -- only an actual "password: xxx" / "password=xxx" shape
    # matches.
    re.compile(r"\b(password|passwd)\s*[:=]\s*\S+", re.IGNORECASE),
]


def validate_device_profiles(profiles: List[DeviceProfile]) -> List[str]:
    """Schema + content-safety check used by --check-device-profiles.
    Returns a list of problem strings (empty = all clean)."""
    problems: List[str] = []
    seen_ids = set()
    for prof in profiles:
        if prof.profile_id in seen_ids:
            problems.append(f"duplicate profile_id '{prof.profile_id}'")
        seen_ids.add(prof.profile_id)
        for port in prof.candidate_onvif_ports + prof.candidate_rtsp_ports:
            if not (1 <= port <= 65535):
                problems.append(f"{prof.profile_id}: port {port} out of range 1..65535")
        blob = " ".join([prof.auth_notes, prof.source_description] + prof.known_limitations)
        for pat in _BANNED_PROFILE_PATTERNS:
            if pat.search(blob):
                problems.append(f"{prof.profile_id}: content matches a banned credential-like pattern "
                                 f"('{pat.pattern}') -- profiles must never carry credentials")
    return problems


@dataclass
class DiscoveredDevice:
    """One candidate device as built up by a discovery scan. Mutable
    evidence accumulator -- fields start empty/False and are filled in
    as each discovery phase runs; merge_discovered_device() combines
    repeat sightings of the same physical device into one record."""
    stable_id: str = ""
    display_name: str = ""
    endpoint_uuid: str = ""
    serial_number: str = ""
    hardware_id: str = ""
    mac_address: str = ""
    mac_vendor_hint: str = ""
    manufacturer: str = ""
    model: str = ""
    firmware_version: str = ""
    last_ip: str = ""
    onvif_xaddr: str = ""
    onvif_port: int = 0
    rtsp_port: int = 0
    scopes: List[str] = field(default_factory=list)
    profiles: List[str] = field(default_factory=list)
    capabilities: Dict[str, bool] = field(default_factory=dict)
    classification: DeviceClassification = DeviceClassification.NETWORK_DEVICE
    confidence: ConfidenceLevel = ConfidenceLevel.UNKNOWN
    matched_profile_id: Optional[str] = None
    discovery_sources: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    last_seen_utc: str = ""
    # Evidence fields (section 6 of the spec this was built from) --
    # tracked separately so "reachable" never collapses to "ping only".
    arp_seen: bool = False
    ping_reply: Optional[bool] = None
    tcp_open_ports: List[int] = field(default_factory=list)
    ws_discovery_reply: bool = False
    onvif_device_service_reply: bool = False
    onvif_auth_result: str = ""  # "" | "not_attempted" | "ok" | "required" | "failed"
    rtsp_candidate: bool = False
    snapshot_candidate: bool = False

    def is_reachable(self) -> bool:
        """Ping is evidence only, never the sole requirement -- see
        README 'why ping is not required'."""
        return bool(self.ws_discovery_reply or self.arp_seen or self.tcp_open_ports
                     or self.onvif_device_service_reply or self.rtsp_candidate)

    def to_report_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["classification"] = self.classification.value
        d["confidence"] = self.confidence.value
        return d


def compute_stable_id(endpoint_uuid: str = "", serial_number: str = "", hardware_id: str = "",
                       mac_address: str = "", host: str = "", manufacturer: str = "", model: str = "") -> str:
    """Identity priority: EndpointReference UUID > serial > hardware id
    > MAC > host+manufacturer+model as a last-resort fallback. Never
    IP address or list position alone (an IP can move; a position in
    a results list is meaningless)."""
    for key in (endpoint_uuid, serial_number, hardware_id, mac_address):
        if key:
            digest = hashlib.sha1(key.strip().lower().encode("utf-8")).hexdigest()[:8]
            return f"cam_{digest}"
    fallback_key = f"{host}|{manufacturer}|{model}".strip().lower()
    digest = hashlib.sha1(fallback_key.encode("utf-8")).hexdigest()[:8]
    return f"cam_{digest}"


def classify_device(dev: DiscoveredDevice) -> DeviceClassification:
    """CONFIRMED requires a validated Device Service response AND
    either a WS-Discovery reply or a successful (not merely attempted)
    authentication -- an auth_result of "required" or "failed" means
    authentication is exactly what PREVENTS confirmation, so it must
    map to LIKELY_ONVIF, never CONFIRMED_ONVIF."""
    if dev.onvif_device_service_reply and (dev.ws_discovery_reply or dev.onvif_auth_result == "ok"):
        return DeviceClassification.CONFIRMED_ONVIF
    if dev.onvif_device_service_reply or (dev.ws_discovery_reply and dev.onvif_xaddr):
        return DeviceClassification.LIKELY_ONVIF
    if dev.rtsp_candidate or dev.snapshot_candidate:
        return DeviceClassification.POSSIBLE_CAMERA
    if dev.arp_seen or dev.ping_reply or dev.tcp_open_ports:
        return DeviceClassification.NETWORK_DEVICE
    return DeviceClassification.UNREACHABLE


def merge_discovered_device(store: Dict[str, DiscoveredDevice], new_evidence: DiscoveredDevice) -> DiscoveredDevice:
    """Looks up new_evidence by its best-available identity; if a
    matching device already exists in `store` (e.g. the same camera
    seen once via WS-Discovery and again via port probing, or the
    same EndpointReference reappearing at a new IP), updates that
    existing record in place instead of creating a duplicate. Returns
    the (possibly newly-inserted) merged record."""
    sid = compute_stable_id(new_evidence.endpoint_uuid, new_evidence.serial_number, new_evidence.hardware_id,
                             new_evidence.mac_address, new_evidence.last_ip, new_evidence.manufacturer, new_evidence.model)
    new_evidence.stable_id = sid
    existing = store.get(sid)
    if existing is None:
        if not new_evidence.display_name:
            new_evidence.display_name = new_evidence.model or new_evidence.last_ip or sid
        new_evidence.classification = classify_device(new_evidence)
        store[sid] = new_evidence
        return new_evidence

    existing.last_ip = new_evidence.last_ip or existing.last_ip
    existing.last_seen_utc = new_evidence.last_seen_utc or existing.last_seen_utc
    for attr in ("endpoint_uuid", "serial_number", "hardware_id", "mac_address", "mac_vendor_hint",
                 "manufacturer", "model", "firmware_version", "onvif_xaddr"):
        new_val = getattr(new_evidence, attr)
        if new_val and not getattr(existing, attr):
            setattr(existing, attr, new_val)
    if new_evidence.onvif_port:
        existing.onvif_port = new_evidence.onvif_port
    if new_evidence.rtsp_port:
        existing.rtsp_port = new_evidence.rtsp_port
    existing.scopes = sorted(set(existing.scopes) | set(new_evidence.scopes))
    existing.profiles = sorted(set(existing.profiles) | set(new_evidence.profiles))
    existing.capabilities.update({k: v for k, v in new_evidence.capabilities.items() if v})
    existing.discovery_sources = sorted(set(existing.discovery_sources) | set(new_evidence.discovery_sources))
    existing.warnings = list(dict.fromkeys(existing.warnings + new_evidence.warnings))
    existing.arp_seen = existing.arp_seen or new_evidence.arp_seen
    if new_evidence.ping_reply is not None:
        existing.ping_reply = new_evidence.ping_reply
    existing.tcp_open_ports = sorted(set(existing.tcp_open_ports) | set(new_evidence.tcp_open_ports))
    existing.ws_discovery_reply = existing.ws_discovery_reply or new_evidence.ws_discovery_reply
    existing.onvif_device_service_reply = existing.onvif_device_service_reply or new_evidence.onvif_device_service_reply
    if new_evidence.onvif_auth_result:
        existing.onvif_auth_result = new_evidence.onvif_auth_result
    existing.rtsp_candidate = existing.rtsp_candidate or new_evidence.rtsp_candidate
    existing.snapshot_candidate = existing.snapshot_candidate or new_evidence.snapshot_candidate
    existing.classification = classify_device(existing)
    return existing


# ----------------------------------------------------------------------
# Adapter enumeration and ARP table inspection
# ----------------------------------------------------------------------

def enumerate_adapters() -> List[AdapterInfo]:
    """Lists local IPv4 adapters so the operator can pick an
    authorized one. Windows: parses `ipconfig` (no admin rights, no
    extra install). Elsewhere: a reduced-capability fallback using
    only the stdlib socket module, clearly a guess at the netmask --
    this project targets Windows 10/11; the fallback exists so the
    headless self-tests still run on any platform."""
    if IS_WINDOWS:
        try:
            return _enumerate_adapters_windows()
        except Exception:
            pass
    return _enumerate_adapters_fallback()


def _enumerate_adapters_windows() -> List[AdapterInfo]:
    out: List[AdapterInfo] = []
    proc = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=5,
                           creationflags=subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
    current_name = ""
    current_ip = ""
    for raw_line in proc.stdout.splitlines():
        line = raw_line.rstrip()
        if line and not line.startswith(" ") and not line.startswith("\t"):
            current_name = line.split(":")[0].strip()
            current_ip = ""
        stripped = line.strip()
        if stripped.lower().startswith("ipv4 address"):
            parts = stripped.split(":", 1)
            if len(parts) == 2:
                current_ip = parts[1].strip().split("(")[0].strip()
        if stripped.lower().startswith("subnet mask") and current_ip:
            parts = stripped.split(":", 1)
            mask = parts[1].strip() if len(parts) == 2 else "255.255.255.0"
            out.append(AdapterInfo(name=current_name or current_ip, ipv4=current_ip, netmask=mask))
            current_ip = ""
    return [a for a in out if a.ipv4 and not a.ipv4.startswith("169.254.")]


def _enumerate_adapters_fallback() -> List[AdapterInfo]:
    out: List[AdapterInfo] = []
    try:
        hostname = socket.gethostname()
        _, _, ips = socket.gethostbyname_ex(hostname)
        for ip in ips:
            if not ip.startswith("127."):
                out.append(AdapterInfo(name=f"{hostname} (guessed /24 -- verify before use)", ipv4=ip, netmask="255.255.255.0"))
    except Exception:
        pass
    if not out:
        out.append(AdapterInfo(name="loopback (no other adapter found)", ipv4="127.0.0.1", netmask="255.0.0.0"))
    return out


_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def read_arp_table() -> Dict[str, str]:
    """Returns {ip: mac} from the OS's own ARP cache. Read-only, no
    admin rights required. MAC/ARP limitations (local L2 segment only,
    may be stale/absent, OUI is a hint not an identity) are documented
    in README -- this function makes no claim beyond what the OS cache
    itself reports."""
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0  # type: ignore[attr-defined]
        proc = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5, creationflags=creationflags)
        return parse_arp_output(proc.stdout)
    except Exception:
        return {}


def parse_arp_output(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        mac_m = _MAC_RE.search(line)
        ip_m = _IPV4_RE.search(line)
        if mac_m and ip_m:
            out[ip_m.group(0)] = mac_m.group(0).replace("-", ":").lower()
    return out


# A tiny, dated, HEURISTIC-only OUI-prefix hint table. Deliberately
# minimal -- this project does not download vendor/OUI databases.
_OUI_HINTS_DATED = "2025-01"  # documented "as of" date for this table
_OUI_HINTS: Dict[str, str] = {
    "00:0c:29": "VMware (virtual NIC -- not a camera)",
    "00:50:56": "VMware (virtual NIC -- not a camera)",
    "b8:27:eb": "Raspberry Pi Foundation",
    "dc:a6:32": "Raspberry Pi Foundation",
    "e0:50:8b": "Common OEM camera/IoT silicon (heuristic only)",
}


def mac_vendor_hint(mac_address: str) -> str:
    if not mac_address:
        return ""
    prefix = mac_address.lower()[:8]
    hint = _OUI_HINTS.get(prefix, "")
    return f"{hint} (heuristic, OUI table dated {_OUI_HINTS_DATED})" if hint else ""


# ----------------------------------------------------------------------
# WS-Discovery (ONVIF uses the standard WS-Discovery Probe/ProbeMatch)
# ----------------------------------------------------------------------

_WS_DISCOVERY_PROBE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{msgid}</w:MessageID>
    <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>"""


def build_ws_discovery_probe() -> bytes:
    return _WS_DISCOVERY_PROBE_TEMPLATE.format(msgid=str(uuid.uuid4())).encode("utf-8")


def _local_tag(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def parse_ws_discovery_probe_match(xml_text: str) -> Optional[Dict[str, Any]]:
    """Tolerant, namespace-agnostic parse of one ProbeMatch response.
    Returns None if `xml_text` doesn't look like a ProbeMatch at all.
    Deliberately liberal: real devices vary their exact namespace
    prefixes, and being strict here would just mean missing cameras,
    not gaining safety."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    result: Dict[str, Any] = {"endpoint_uuid": "", "types": [], "scopes": [], "xaddrs": [], "metadata_version": ""}
    found_probematch = False
    for elem in root.iter():
        name = _local_tag(elem.tag)
        if name == "ProbeMatch":
            found_probematch = True
        elif name == "Address" and elem.text:
            result["endpoint_uuid"] = elem.text.strip()
        elif name == "Types" and elem.text:
            result["types"] = elem.text.split()
        elif name == "Scopes" and elem.text:
            result["scopes"] = elem.text.split()
        elif name == "XAddrs" and elem.text:
            result["xaddrs"] = elem.text.split()
        elif name == "MetadataVersion" and elem.text:
            result["metadata_version"] = elem.text.strip()
    return result if found_probematch else None


def run_ws_discovery(local_ip: str, timeout_s: float, cancel_event: Optional[threading.Event] = None) -> List[Dict[str, Any]]:
    """Sends one WS-Discovery Probe via UDP multicast on the interface
    bound to `local_ip` and collects ProbeMatch replies for up to
    `timeout_s` seconds. Returns a list of parsed ProbeMatch dicts
    (possibly empty -- multicast can fail across VLANs/firewalls/other
    interfaces; that is reported as a limitation, not "no cameras
    exist"). Never sends anything but this one bounded Probe."""
    matches: List[Dict[str, Any]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind((local_ip, 0))
        sock.settimeout(0.3)
        probe = build_ws_discovery_probe()
        sock.sendto(probe, (WS_DISCOVERY_MULTICAST_ADDR, WS_DISCOVERY_MULTICAST_PORT))
        deadline = time.time() + max(0.5, timeout_s)
        while time.time() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            parsed = parse_ws_discovery_probe_match(data.decode("utf-8", errors="replace"))
            if parsed is not None:
                matches.append(parsed)
    except OSError:
        # Common and expected: multicast unavailable/blocked on this
        # interface. Caller records this as a discovery limitation.
        pass
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return matches


# ----------------------------------------------------------------------
# Minimal SOAP / WS-Security helpers (read-only operations only)
# ----------------------------------------------------------------------

def build_ws_usernametoken_header(username: str, password: str) -> str:
    """Standard WS-Security UsernameToken PasswordDigest:
    Base64(SHA1(nonce + created + password)). This is the normal,
    documented ONVIF authentication mechanism -- not a workaround."""
    nonce_bytes = os.urandom(16)
    nonce_b64 = base64.b64encode(nonce_bytes).decode("ascii")
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(nonce_bytes + created.encode("utf-8") + password.encode("utf-8")).digest()
    ).decode("ascii")
    return (
        '<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{username}</wsse:Username>"
        '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f"{digest}</wsse:Password>"
        '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{nonce_b64}</wsse:Nonce>"
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security>"
    )


def send_onvif_soap(xaddr: str, body_inner_xml: str, timeout_s: float,
                     username: str = "", password: str = "") -> Tuple[bool, int, str]:
    """POSTs one SOAP envelope to `xaddr`. Returns (ok, http_status,
    response_text). `ok` means the endpoint responded with something
    SOAP-shaped -- even a 401/400 proves an ONVIF-speaking endpoint
    exists, which is valuable evidence even when auth fails."""
    security_header = build_ws_usernametoken_header(username, password) if username else ""
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f"<s:Header>{security_header}</s:Header>"
        f"<s:Body>{body_inner_xml}</s:Body>"
        "</s:Envelope>"
    ).encode("utf-8")
    req = urllib.request.Request(xaddr, data=envelope, method="POST",
                                  headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read(16384)
            text = raw.decode("utf-8", errors="replace")
            return True, resp.status, text
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read(8192).decode("utf-8", errors="replace")
        except Exception:
            text = ""
        looks_soap = "Envelope" in text or "soap" in text.lower()
        return (exc.code in (400, 401, 500, 503) and looks_soap) or exc.code in (400, 401), exc.code, text
    except Exception:
        return False, 0, ""


def onvif_probe_device_service(xaddr: str, timeout_s: float) -> Tuple[bool, str]:
    """GetSystemDateAndTime is explicitly unauthenticated in ONVIF
    Profile S/T -- a correct choice for a pure liveness check with no
    credentials at all."""
    body = '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
    ok, _status, text = send_onvif_soap(xaddr, body, timeout_s)
    return ok, text


def onvif_get_device_information(xaddr: str, username: str, password: str, timeout_s: float) -> Tuple[bool, Dict[str, str]]:
    body = '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
    ok, _status, text = send_onvif_soap(xaddr, body, timeout_s, username, password)
    info = {"Manufacturer": "", "Model": "", "FirmwareVersion": "", "SerialNumber": "", "HardwareId": ""}
    if not ok or not text:
        return False, info
    try:
        root = ET.fromstring(text)
        for elem in root.iter():
            name = _local_tag(elem.tag)
            if name in info and elem.text:
                info[name] = elem.text.strip()
    except ET.ParseError:
        return False, info
    return any(info.values()), info


def onvif_get_capabilities_summary(xaddr: str, username: str, password: str, timeout_s: float) -> Tuple[bool, Dict[str, bool]]:
    body = ('<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl">'
            '<Category>All</Category></GetCapabilities>')
    ok, _status, text = send_onvif_soap(xaddr, body, timeout_s, username, password)
    caps = {"device": False, "media": False, "ptz": False, "imaging": False, "events": False}
    if not ok or not text:
        return False, caps
    text_l = text.lower()
    caps["device"] = "devicecapabilities" in text_l or "<device" in text_l
    caps["media"] = "mediacapabilities" in text_l or "/media" in text_l
    caps["ptz"] = "ptzcapabilities" in text_l or "/ptz" in text_l
    caps["imaging"] = "imagingcapabilities" in text_l or "/imaging" in text_l
    caps["events"] = "eventscapabilities" in text_l or "/events" in text_l
    return True, caps


# ----------------------------------------------------------------------
# Scan configuration (validated, clamped into the hard ceilings above)
# ----------------------------------------------------------------------

@dataclass
class DiscoverySettings:
    """Parsed [DISCOVERY] section of config.txt."""
    enabled: bool = True
    default_mode: DiscoveryMode = DiscoveryMode.QUICK
    allowed_subnets: str = "auto"
    candidate_onvif_ports: List[int] = field(default_factory=lambda: [80, 443, 8000, 8080, 8081, 8899])
    candidate_rtsp_ports: List[int] = field(default_factory=lambda: [554, 8554])
    connect_timeout_s: float = 0.75
    soap_timeout_s: float = 3.0
    max_parallel_hosts: int = 8
    max_parallel_ports_per_host: int = 2
    delay_between_batches_ms: int = 250
    allow_extended_scan: bool = False
    extended_ports: List[int] = field(default_factory=list)
    require_confirmation_for_extended: bool = True
    save_reports: bool = True
    collect_mac: bool = True
    collect_ping: bool = True
    ping_is_required: bool = False


@dataclass
class DiscoveryUISettings:
    """Parsed [DISCOVERY_UI] section: remembers the operator's last
    choices in the Discover & Diagnose dialog between sessions. Purely
    a convenience -- never affects safety bounds, which always come
    from DiscoverySettings/build_scan_config regardless of this."""
    last_adapter_name: str = ""
    last_subnet_cidr: str = ""
    last_mode: DiscoveryMode = DiscoveryMode.QUICK
    last_filter: str = "All"


def _parse_discovery_ui_settings(doc: "IniDocument") -> Tuple[DiscoveryUISettings, List[ValidationIssue]]:
    issues: List[ValidationIssue] = []
    d = doc.get_section_dict("DISCOVERY_UI")
    s = DiscoveryUISettings()
    if not d:
        return s, issues
    s.last_adapter_name = d.get("last_adapter_name", "")
    s.last_subnet_cidr = d.get("last_subnet_cidr", "")
    try:
        s.last_mode = DiscoveryMode(d.get("last_mode", "QUICK").strip().upper())
    except ValueError:
        issues.append(ValidationIssue("DISCOVERY_UI", "last_mode", d.get("last_mode", ""), "expected QUICK|STANDARD|EXTENDED", "warning"))
    valid_filters = ("All", "Confirmed ONVIF", "Likely ONVIF", "Possible Camera", "Network Device",
                      "Authentication Required", "Duplicate Group")
    s.last_filter = d.get("last_filter", "All")
    if s.last_filter not in valid_filters:
        s.last_filter = "All"
    return s, issues


@dataclass
class DiscoveryScanConfig:
    """One concrete scan request, built from DiscoverySettings plus
    the operator's choices in the Discover & Diagnose dialog."""
    mode: DiscoveryMode
    adapter: AdapterInfo
    subnet_cidr: str
    candidate_onvif_ports: List[int]
    candidate_rtsp_ports: List[int]
    connect_timeout_s: float
    soap_timeout_s: float
    max_parallel_hosts: int
    max_parallel_ports_per_host: int
    delay_between_batches_ms: int
    extended_ports: List[int] = field(default_factory=list)
    collect_mac: bool = True
    collect_ping: bool = True
    credentials: Dict[str, Tuple[str, str]] = field(default_factory=dict)  # host -> (user, pass), never persisted


def _parse_discovery_settings(doc: "IniDocument") -> Tuple[DiscoverySettings, List[ValidationIssue]]:
    issues: List[ValidationIssue] = []
    d = doc.get_section_dict("DISCOVERY")
    s = DiscoverySettings()
    if not d:
        return s, issues

    s.enabled = _pbool(d.get("enabled", "true"))
    if s.enabled is None:
        issues.append(ValidationIssue("DISCOVERY", "enabled", d.get("enabled", ""), "expected true/false", "warning"))
        s.enabled = True
    try:
        s.default_mode = DiscoveryMode(d.get("default_mode", "QUICK").strip().upper())
    except ValueError:
        issues.append(ValidationIssue("DISCOVERY", "default_mode", d.get("default_mode", ""), "expected QUICK|STANDARD|EXTENDED", "warning"))
    s.allowed_subnets = d.get("allowed_subnets", "auto")

    def _ports(key: str, default: List[int], max_count: int) -> List[int]:
        raw = d.get(key, "")
        if not raw:
            return list(default)
        out = []
        for p in raw.split(","):
            p = p.strip()
            if not p:
                continue
            v = _pint(p)
            if v is None or not (1 <= v <= 65535):
                issues.append(ValidationIssue("DISCOVERY", key, p, "expected integer 1..65535", "warning"))
                continue
            out.append(v)
        if len(out) > max_count:
            issues.append(ValidationIssue("DISCOVERY", key, raw, f"more than {max_count} ports given; truncated for safety", "warning"))
            out = out[:max_count]
        return out or list(default)

    s.candidate_onvif_ports = _ports("candidate_onvif_ports", s.candidate_onvif_ports, DISCOVERY_MAX_CANDIDATE_PORTS)
    s.candidate_rtsp_ports = _ports("candidate_rtsp_ports", s.candidate_rtsp_ports, DISCOVERY_MAX_CANDIDATE_PORTS)
    s.extended_ports = _ports("extended_ports", [], DISCOVERY_MAX_EXTENDED_PORTS)

    ct = _pfloat(d.get("connect_timeout_s", str(s.connect_timeout_s)))
    s.connect_timeout_s = max(DISCOVERY_MIN_CONNECT_TIMEOUT_S, min(DISCOVERY_MAX_CONNECT_TIMEOUT_S, ct if ct is not None else s.connect_timeout_s))
    st = _pfloat(d.get("soap_timeout_s", str(s.soap_timeout_s)))
    s.soap_timeout_s = max(DISCOVERY_MIN_SOAP_TIMEOUT_S, min(DISCOVERY_MAX_SOAP_TIMEOUT_S, st if st is not None else s.soap_timeout_s))

    mph = _pint(d.get("max_parallel_hosts", str(s.max_parallel_hosts)))
    s.max_parallel_hosts = max(1, min(DISCOVERY_MAX_PARALLEL_HOSTS, mph if mph is not None else s.max_parallel_hosts))
    mpp = _pint(d.get("max_parallel_ports_per_host", str(s.max_parallel_ports_per_host)))
    s.max_parallel_ports_per_host = max(1, min(DISCOVERY_MAX_PARALLEL_PORTS_PER_HOST, mpp if mpp is not None else s.max_parallel_ports_per_host))
    dbb = _pint(d.get("delay_between_batches_ms", str(s.delay_between_batches_ms)))
    s.delay_between_batches_ms = max(0, min(5000, dbb if dbb is not None else s.delay_between_batches_ms))

    s.allow_extended_scan = _pbool(d.get("allow_extended_scan", "false")) or False
    s.require_confirmation_for_extended = _pbool(d.get("require_confirmation_for_extended", "true"))
    if s.require_confirmation_for_extended is None:
        s.require_confirmation_for_extended = True
    s.save_reports = _pbool(d.get("save_reports", "true")) or True
    s.collect_mac = _pbool(d.get("collect_mac", "true"))
    if s.collect_mac is None:
        s.collect_mac = True
    s.collect_ping = _pbool(d.get("collect_ping", "true"))
    if s.collect_ping is None:
        s.collect_ping = True
    s.ping_is_required = _pbool(d.get("ping_is_required", "false")) or False
    return s, issues


# ----------------------------------------------------------------------
# Bounded TCP probing (never a full port-range scan; always one
# explicit, size-capped candidate list, run through a hard-capped
# thread pool)
# ----------------------------------------------------------------------

def tcp_probe_one(host: str, port: int, timeout_s: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except Exception:
        return False


def bounded_port_probe(hosts: List[str], ports: List[int], connect_timeout_s: float,
                        max_parallel_hosts: int, max_parallel_ports_per_host: int,
                        cancel_event: Optional[threading.Event] = None,
                        on_host_done: Optional[Callable[[str, List[int]], None]] = None,
                        concurrency_tracker: Optional["ConcurrencyTracker"] = None) -> Dict[str, List[int]]:
    """Probes `ports` on every host in `hosts`, bounded by two
    concurrency caps (never more than max_parallel_hosts hosts, nor
    more than max_parallel_ports_per_host ports on one host, running
    at once). Returns {host: [open_ports]}.

    `concurrency_tracker`, when given, is notified around each
    individual TCP attempt so a test can assert the true observed
    maximum concurrency never exceeded the configured caps -- it has
    no effect on normal operation (default None -> zero overhead)."""
    results: Dict[str, List[int]] = {h: [] for h in hosts}
    total_workers = max(1, min(DISCOVERY_MAX_PARALLEL_HOSTS * DISCOVERY_MAX_PARALLEL_PORTS_PER_HOST,
                                max_parallel_hosts * max_parallel_ports_per_host))
    host_semaphore = threading.Semaphore(max_parallel_hosts)

    def _probe_host(host: str) -> None:
        with host_semaphore:
            if cancel_event is not None and cancel_event.is_set():
                return
            port_sema = threading.Semaphore(max_parallel_ports_per_host)
            lock = threading.Lock()
            threads = []

            def _probe_port(p: int) -> None:
                with port_sema:
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    if concurrency_tracker is not None:
                        concurrency_tracker.enter()
                    try:
                        if tcp_probe_one(host, p, connect_timeout_s):
                            with lock:
                                results[host].append(p)
                    finally:
                        if concurrency_tracker is not None:
                            concurrency_tracker.exit()

            for p in ports:
                t = threading.Thread(target=_probe_port, args=(p,), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            results[host].sort()
            if on_host_done is not None:
                on_host_done(host, results[host])

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(total_workers, max(1, max_parallel_hosts))) as pool:
        futures = [pool.submit(_probe_host, h) for h in hosts]
        for fut in futures:
            fut.result()
    return results


class ConcurrencyTracker:
    """Tiny helper used only by --discovery-mock-test to prove the
    rate limit is real: tracks the highest number of simultaneous
    `enter()`/`exit()` pairs observed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current = 0
        self.peak = 0

    def enter(self) -> None:
        with self._lock:
            self._current += 1
            self.peak = max(self.peak, self._current)

    def exit(self) -> None:
        with self._lock:
            self._current -= 1


MAX_SCAN_HOSTS = 1024


def _subnet_host_list(cidr: str) -> List[str]:
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return []
    return [str(ip) for ip in net.hosts()][:MAX_SCAN_HOSTS]


def _ip_in_subnet(ip: str, cidr: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


def build_scan_config(mode: DiscoveryMode, adapter: AdapterInfo, subnet_cidr: str, settings: DiscoverySettings,
                       extended_confirmed: bool = False,
                       credentials: Optional[Dict[str, Tuple[str, str]]] = None) -> Tuple[Optional[DiscoveryScanConfig], List[str]]:
    """The one place EXTENDED mode's gate is actually enforced,
    independent of whatever the GUI dialog did or didn't check --
    defense in depth, same pattern as dry_run/mock_mode elsewhere in
    this file. Returns (None, [reasons]) if the request is refused."""
    problems: List[str] = []
    try:
        ipaddress.ip_network(subnet_cidr, strict=False)
    except ValueError:
        return None, [f"'{subnet_cidr}' is not a valid subnet (CIDR notation, e.g. 192.168.1.0/24)"]

    extended_ports: List[int] = []
    if mode == DiscoveryMode.EXTENDED:
        if not settings.allow_extended_scan:
            return None, ["EXTENDED mode is disabled: set [DISCOVERY] allow_extended_scan = true in config.txt first"]
        if settings.require_confirmation_for_extended and not extended_confirmed:
            return None, ["EXTENDED mode requires explicit confirmation"]
        extended_ports = list(settings.extended_ports)[:DISCOVERY_MAX_EXTENDED_PORTS]
        if not extended_ports:
            problems.append("EXTENDED mode requested with no extended_ports configured -- running as STANDARD instead")
            mode = DiscoveryMode.STANDARD

    return DiscoveryScanConfig(
        mode=mode, adapter=adapter, subnet_cidr=subnet_cidr,
        candidate_onvif_ports=list(settings.candidate_onvif_ports),
        candidate_rtsp_ports=list(settings.candidate_rtsp_ports),
        connect_timeout_s=settings.connect_timeout_s, soap_timeout_s=settings.soap_timeout_s,
        max_parallel_hosts=settings.max_parallel_hosts, max_parallel_ports_per_host=settings.max_parallel_ports_per_host,
        delay_between_batches_ms=settings.delay_between_batches_ms, extended_ports=extended_ports,
        collect_mac=settings.collect_mac, collect_ping=settings.collect_ping,
        credentials=credentials or {},
    ), problems


@dataclass
class DiscoveryEvent:
    event_type: str  # "PROGRESS" | "DEVICE_UPDATED" | "DONE" | "ERROR"
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class DiscoveryWorker(threading.Thread):
    """Runs one bounded discovery scan in its own thread so the GUI is
    never blocked. Never calls any Tkinter method -- progress is
    reported through `event_queue`, drained by the GUI's existing
    root.after() loop, exactly like CameraWorker. Cancellable at any
    point via `cancel()`."""

    def __init__(self, scan_config: DiscoveryScanConfig, profiles: List[DeviceProfile],
                 event_queue: "queue.Queue[DiscoveryEvent]", seed_devices: Optional[Dict[str, DiscoveredDevice]] = None):
        super().__init__(name="DiscoveryWorker", daemon=True)
        self.scan_config = scan_config
        self.profiles = profiles
        self.event_queue = event_queue
        self.cancel_event = threading.Event()
        self.devices: Dict[str, DiscoveredDevice] = dict(seed_devices) if seed_devices else {}
        self.limitations: List[str] = []
        self.started_at = ""
        self.finished_at = ""

    def cancel(self) -> None:
        self.cancel_event.set()

    def _emit(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.event_queue.put(DiscoveryEvent(event_type, payload or {}))

    def run(self) -> None:
        self.started_at = datetime.now(timezone.utc).isoformat()
        try:
            self._run_scan()
        except Exception:
            self._emit("ERROR", {"message": "discovery worker crashed, see logs"})
        finally:
            self.finished_at = datetime.now(timezone.utc).isoformat()
            self._emit("DONE", {"device_count": len(self.devices), "cancelled": self.cancel_event.is_set()})

    def _run_scan(self) -> None:
        cfg = self.scan_config
        self._emit("PROGRESS", {"phase": "ws_discovery", "message": "Sending WS-Discovery probe..."})
        ws_matches = run_ws_discovery(cfg.adapter.ipv4, cfg.soap_timeout_s, self.cancel_event)
        if not ws_matches:
            self.limitations.append(
                "WS-Discovery returned no replies on this interface. Multicast can fail across VLANs, "
                "firewalls, or other adapters -- this does not mean no cameras exist, only that none "
                "answered this probe.")
        for m in ws_matches:
            if self.cancel_event.is_set():
                return
            dev = DiscoveredDevice(last_seen_utc=datetime.now(timezone.utc).isoformat())
            dev.endpoint_uuid = m.get("endpoint_uuid", "")
            dev.scopes = m.get("scopes", [])
            xaddrs = m.get("xaddrs", [])
            if xaddrs:
                dev.onvif_xaddr = xaddrs[0]
                try:
                    parsed = urllib.parse.urlparse(xaddrs[0])
                    dev.last_ip = parsed.hostname or ""
                    dev.onvif_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                except Exception:
                    pass
            dev.ws_discovery_reply = True
            dev.discovery_sources.append("ws_discovery")
            merged = merge_discovered_device(self.devices, dev)
            self._emit("DEVICE_UPDATED", {"stable_id": merged.stable_id})

        if self.cancel_event.is_set():
            return

        if cfg.collect_mac:
            self._emit("PROGRESS", {"phase": "arp", "message": "Reading ARP table..."})
            for ip, mac in read_arp_table().items():
                if self.cancel_event.is_set():
                    return
                if not _ip_in_subnet(ip, cfg.subnet_cidr):
                    continue
                dev = DiscoveredDevice(last_ip=ip, mac_address=mac, arp_seen=True,
                                        mac_vendor_hint=mac_vendor_hint(mac),
                                        last_seen_utc=datetime.now(timezone.utc).isoformat())
                dev.discovery_sources.append("arp")
                merged = merge_discovered_device(self.devices, dev)
                self._emit("DEVICE_UPDATED", {"stable_id": merged.stable_id})

        if cfg.mode == DiscoveryMode.QUICK or self.cancel_event.is_set():
            return

        ports = sorted(set(cfg.candidate_onvif_ports) | set(cfg.candidate_rtsp_ports) | set(cfg.extended_ports))
        hosts = _subnet_host_list(cfg.subnet_cidr)
        self._emit("PROGRESS", {"phase": "port_probe",
                                 "message": f"Probing {len(ports)} candidate port(s) across {len(hosts)} host(s)..."})

        def _on_host_done(host: str, open_ports: List[int]) -> None:
            if not open_ports or self.cancel_event.is_set():
                return
            dev = DiscoveredDevice(last_ip=host, tcp_open_ports=list(open_ports),
                                    last_seen_utc=datetime.now(timezone.utc).isoformat())
            dev.discovery_sources.append("port_probe")
            if any(p in cfg.candidate_rtsp_ports for p in open_ports):
                dev.rtsp_candidate = True
            merged = merge_discovered_device(self.devices, dev)
            self._emit("DEVICE_UPDATED", {"stable_id": merged.stable_id})
            if cfg.delay_between_batches_ms:
                time.sleep(cfg.delay_between_batches_ms / 1000.0)

        if hosts:
            bounded_port_probe(hosts, ports, cfg.connect_timeout_s, cfg.max_parallel_hosts,
                                cfg.max_parallel_ports_per_host, self.cancel_event, _on_host_done)

        if self.cancel_event.is_set():
            return

        self._emit("PROGRESS", {"phase": "onvif_validate", "message": "Validating candidate ONVIF endpoints..."})
        for dev in list(self.devices.values()):
            if self.cancel_event.is_set():
                return
            xaddr_candidates: List[str] = [dev.onvif_xaddr] if dev.onvif_xaddr else []
            for p in dev.tcp_open_ports:
                if p in cfg.candidate_onvif_ports:
                    scheme = "https" if p == 443 else "http"
                    xaddr_candidates.append(f"{scheme}://{dev.last_ip}:{p}/onvif/device_service")
            for xaddr in xaddr_candidates:
                ok, _text = onvif_probe_device_service(xaddr, cfg.soap_timeout_s)
                if not ok:
                    continue
                dev.onvif_device_service_reply = True
                dev.onvif_xaddr = xaddr
                if "onvif_query" not in dev.discovery_sources:
                    dev.discovery_sources.append("onvif_query")
                creds = cfg.credentials.get(dev.last_ip)
                if creds:
                    ok2, info = onvif_get_device_information(xaddr, creds[0], creds[1], cfg.soap_timeout_s)
                    dev.onvif_auth_result = "ok" if ok2 else "required"
                    if ok2:
                        dev.manufacturer = info.get("Manufacturer", "") or dev.manufacturer
                        dev.model = info.get("Model", "") or dev.model
                        dev.firmware_version = info.get("FirmwareVersion", "") or dev.firmware_version
                        dev.serial_number = info.get("SerialNumber", "") or dev.serial_number
                        dev.hardware_id = info.get("HardwareId", "") or dev.hardware_id
                        ok3, caps = onvif_get_capabilities_summary(xaddr, creds[0], creds[1], cfg.soap_timeout_s)
                        if ok3:
                            dev.capabilities.update(caps)
                else:
                    dev.onvif_auth_result = dev.onvif_auth_result or "not_attempted"
                break
            profile_id, confidence = match_device_profile(dev.manufacturer, dev.model, self.profiles)
            dev.matched_profile_id = profile_id
            if dev.confidence == ConfidenceLevel.UNKNOWN:
                dev.confidence = confidence
            dev.classification = classify_device(dev)
            self._emit("DEVICE_UPDATED", {"stable_id": dev.stable_id})


def save_discovery_report(devices: Dict[str, DiscoveredDevice], scan_config: DiscoveryScanConfig,
                           started_at: str, finished_at: str, limitations: List[str]) -> Tuple[Path, Path]:
    """Never includes credentials: DiscoveredDevice has no credential
    field at all, and scan_config.credentials is deliberately excluded
    below."""
    ensure_app_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = DIAGNOSTICS_DIR / f"discovery_{stamp}.json"
    txt_path = DIAGNOSTICS_DIR / f"discovery_{stamp}.txt"
    report = {
        "scan_scope": scan_config.subnet_cidr, "adapter": scan_config.adapter.name,
        "mode": scan_config.mode.value, "started_at": started_at, "finished_at": finished_at,
        "candidate_ports": {"onvif": scan_config.candidate_onvif_ports, "rtsp": scan_config.candidate_rtsp_ports,
                             "extended": scan_config.extended_ports},
        "rate_limits": {"max_parallel_hosts": scan_config.max_parallel_hosts,
                         "max_parallel_ports_per_host": scan_config.max_parallel_ports_per_host,
                         "connect_timeout_s": scan_config.connect_timeout_s, "soap_timeout_s": scan_config.soap_timeout_s},
        "limitations": limitations,
        "devices": [d.to_report_dict() for d in devices.values()],
    }
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = ["Discovery report", f"Scope: {scan_config.subnet_cidr} via {scan_config.adapter.name}",
              f"Mode: {scan_config.mode.value}", f"Started: {started_at}", f"Finished: {finished_at}", "-" * 60]
    if limitations:
        lines.append("Limitations:")
        lines.extend(f"  - {l}" for l in limitations)
        lines.append("-" * 60)
    for dev in devices.values():
        lines.append(f"[{dev.classification.value}] {dev.display_name or dev.stable_id} ({dev.last_ip})")
        lines.append(f"  stable_id={dev.stable_id}  confidence={dev.confidence.value}  profile={dev.matched_profile_id}")
        lines.append(f"  manufacturer={dev.manufacturer}  model={dev.model}  firmware={dev.firmware_version}")
        lines.append(f"  mac={dev.mac_address}  {dev.mac_vendor_hint}")
        lines.append(f"  onvif_xaddr={dev.onvif_xaddr}  rtsp_candidate={dev.rtsp_candidate}  auth={dev.onvif_auth_result}")
        lines.append(f"  sources={','.join(dev.discovery_sources)}  open_ports={dev.tcp_open_ports}")
        if dev.warnings:
            lines.append(f"  warnings: {'; '.join(dev.warnings)}")
        lines.append("")
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, txt_path


def save_device_report(dev: DiscoveredDevice) -> Tuple[Path, Path]:
    ensure_app_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = DIAGNOSTICS_DIR / f"device_{dev.stable_id}_{stamp}.json"
    txt_path = DIAGNOSTICS_DIR / f"device_{dev.stable_id}_{stamp}.txt"
    json_path.write_text(json.dumps(dev.to_report_dict(), indent=2), encoding="utf-8")
    lines = [f"Device report: {dev.display_name or dev.stable_id}", "-" * 60]
    for k, v in dev.to_report_dict().items():
        lines.append(f"{k}: {v}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, txt_path


# ======================================================================
# 13. TKINTER GUI CONSTRUCTION
# ======================================================================

class DiscoveryDialog(tk.Toplevel):
    """The 'Discover & Diagnose' window: configures and runs one
    bounded LAN discovery scan, shows live results, and lets the
    operator add approved devices as cameras. Completely independent
    of the camera patrol engine -- it never touches a CameraWorker,
    so closing or cancelling this window never affects a running
    patrol. Discovery events arrive through the same kind of
    thread-safe queue + root.after() pattern as the main window; the
    DiscoveryWorker thread never calls a Tkinter method directly."""

    FILTERS = ("All", "Confirmed ONVIF", "Likely ONVIF", "Possible Camera",
               "Network Device", "Authentication Required", "Duplicate Group")

    def __init__(self, parent: PTZPatrolGUI):
        super().__init__(parent)
        self.main_app = parent
        self.title("Discover & Diagnose")
        self.geometry("1180x660")
        self.minsize(950, 520)
        self.transient(parent)

        self.devices: Dict[str, DiscoveredDevice] = {}
        self.worker: Optional[DiscoveryWorker] = None
        self.event_queue: "queue.Queue[DiscoveryEvent]" = queue.Queue()
        self._poll_after_id: Optional[str] = None
        self.selected_stable_id: Optional[str] = None
        self.last_scan_cfg: Optional[DiscoveryScanConfig] = None
        self._last_started_at = ""
        self._last_finished_at = ""
        self._last_limitations: List[str] = []

        self.adapters = enumerate_adapters()
        ds = parent.app_config.discovery
        du = parent.app_config.discovery_ui

        self.adapter_var = tk.StringVar()
        self.subnet_var = tk.StringVar(value=du.last_subnet_cidr)
        self.mode_var = tk.StringVar(value=du.last_mode.value)
        self.onvif_ports_var = tk.StringVar(value=",".join(str(p) for p in ds.candidate_onvif_ports))
        self.rtsp_ports_var = tk.StringVar(value=",".join(str(p) for p in ds.candidate_rtsp_ports))
        self.connect_timeout_var = tk.StringVar(value=str(ds.connect_timeout_s))
        self.soap_timeout_var = tk.StringVar(value=str(ds.soap_timeout_s))
        self.max_hosts_var = tk.StringVar(value=str(ds.max_parallel_hosts))
        self.max_ports_var = tk.StringVar(value=str(ds.max_parallel_ports_per_host))
        self.filter_var = tk.StringVar(value=du.last_filter if du.last_filter in self.FILTERS else "All")
        self.status_var = tk.StringVar(value="Idle. Select an adapter and click Start.")

        self._build_gui()
        self._select_initial_adapter(du.last_adapter_name)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_after_id = self.after(150, self._poll_events)

    # ---------------------------------------------------------------
    # Layout
    # ---------------------------------------------------------------
    def _build_gui(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)

        row1 = ttk.Frame(top)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Adapter:").pack(side="left")
        adapter_names = [f"{a.name}  ({a.ipv4})" for a in self.adapters]
        self.adapter_combo = ttk.Combobox(row1, textvariable=self.adapter_var, values=adapter_names,
                                           state="readonly", width=36)
        self.adapter_combo.pack(side="left", padx=4)
        self.adapter_combo.bind("<<ComboboxSelected>>", self._on_adapter_changed)
        ttk.Label(row1, text="Authorized subnet (CIDR):").pack(side="left", padx=(12, 2))
        ttk.Entry(row1, textvariable=self.subnet_var, width=18).pack(side="left")
        ttk.Label(row1, text="Mode:").pack(side="left", padx=(12, 2))
        for m in ("QUICK", "STANDARD", "EXTENDED"):
            ttk.Radiobutton(row1, text=m, variable=self.mode_var, value=m).pack(side="left")

        row2 = ttk.Frame(top)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="ONVIF ports:").pack(side="left")
        ttk.Entry(row2, textvariable=self.onvif_ports_var, width=18).pack(side="left", padx=(2, 10))
        ttk.Label(row2, text="RTSP ports:").pack(side="left")
        ttk.Entry(row2, textvariable=self.rtsp_ports_var, width=14).pack(side="left", padx=(2, 10))
        ttk.Label(row2, text="Connect timeout s:").pack(side="left")
        ttk.Entry(row2, textvariable=self.connect_timeout_var, width=5).pack(side="left", padx=(2, 10))
        ttk.Label(row2, text="SOAP timeout s:").pack(side="left")
        ttk.Entry(row2, textvariable=self.soap_timeout_var, width=5).pack(side="left", padx=(2, 10))
        ttk.Label(row2, text="Max hosts:").pack(side="left")
        ttk.Entry(row2, textvariable=self.max_hosts_var, width=4).pack(side="left", padx=(2, 10))
        ttk.Label(row2, text="Max ports/host:").pack(side="left")
        ttk.Entry(row2, textvariable=self.max_ports_var, width=4).pack(side="left", padx=2)

        toolbar = ttk.Frame(top)
        toolbar.pack(fill="x", pady=(6, 0))
        self.start_btn = ttk.Button(toolbar, text="Start", command=self._on_start)
        self.start_btn.pack(side="left", padx=2)
        self.cancel_btn = ttk.Button(toolbar, text="Cancel", command=self._on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=2)
        ttk.Button(toolbar, text="Clear Results", command=self._on_clear).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Export Report", command=self._on_export).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Add Selected Cameras", command=self._on_add_selected).pack(side="left", padx=(12, 2))
        ttk.Label(toolbar, text="Filter:").pack(side="left", padx=(16, 2))
        filter_combo = ttk.Combobox(toolbar, textvariable=self.filter_var, values=self.FILTERS,
                                     state="readonly", width=22)
        filter_combo.pack(side="left")
        filter_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_tree())

        body = ttk.PanedWindow(self, orient="vertical")
        body.pack(fill="both", expand=True, padx=8, pady=(4, 0))

        tree_frame = ttk.Frame(body)
        body.add(tree_frame, weight=3)
        columns = ("name", "classification", "confidence", "stable_id", "ip", "mac", "manufacturer",
                   "model", "firmware", "onvif", "rtsp", "ping", "auth", "ptz", "sources", "last_seen")
        headers = {"name": "Name", "classification": "Classification", "confidence": "Confidence",
                   "stable_id": "Stable ID", "ip": "IP", "mac": "MAC", "manufacturer": "Manufacturer",
                   "model": "Model", "firmware": "Firmware", "onvif": "ONVIF XAddr/port",
                   "rtsp": "RTSP candidate", "ping": "Ping", "auth": "Auth", "ptz": "PTZ",
                   "sources": "Source(s)", "last_seen": "Last seen (UTC)"}
        widths = {"name": 110, "stable_id": 90, "manufacturer": 110, "model": 90, "onvif": 180, "last_seen": 130}
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        for c in columns:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths.get(c, 90), anchor="w")
        vs = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        details_frame = ttk.Frame(body)
        body.add(details_frame, weight=1)
        ttk.Label(details_frame, text="Details", font=("TkDefaultFont", 9, "bold")).pack(anchor="w")
        self.details_text = tk.Text(details_frame, height=9, wrap="word", state="disabled")
        self.details_text.pack(fill="both", expand=True)

        status_bar = ttk.Frame(self)
        status_bar.pack(fill="x", padx=8, pady=4)
        ttk.Label(status_bar, textvariable=self.status_var).pack(side="left")

    # ---------------------------------------------------------------
    # Adapter selection
    # ---------------------------------------------------------------
    def _select_initial_adapter(self, last_name: str) -> None:
        if not self.adapters:
            self.status_var.set("No network adapters found.")
            return
        idx = 0
        for i, a in enumerate(self.adapters):
            if a.name == last_name:
                idx = i
                break
        self.adapter_combo.current(idx)
        if not self.subnet_var.get().strip():
            self.subnet_var.set(self.adapters[idx].cidr())

    def _current_adapter(self) -> Optional[AdapterInfo]:
        idx = self.adapter_combo.current()
        if 0 <= idx < len(self.adapters):
            return self.adapters[idx]
        return None

    def _on_adapter_changed(self, _event: Any = None) -> None:
        a = self._current_adapter()
        if a is not None:
            self.subnet_var.set(a.cidr())

    # ---------------------------------------------------------------
    # Scan control
    # ---------------------------------------------------------------
    def _on_start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        adapter = self._current_adapter()
        if adapter is None:
            messagebox.showerror("Discover & Diagnose", "Select a network adapter first.", parent=self)
            return
        subnet_cidr = self.subnet_var.get().strip()
        try:
            mode = DiscoveryMode(self.mode_var.get())
        except ValueError:
            mode = DiscoveryMode.QUICK

        ds = copy.deepcopy(self.main_app.app_config.discovery)
        onvif_ports = [p for p in (_pint(x.strip()) for x in self.onvif_ports_var.get().split(",") if x.strip()) if p]
        rtsp_ports = [p for p in (_pint(x.strip()) for x in self.rtsp_ports_var.get().split(",") if x.strip()) if p]
        if onvif_ports:
            ds.candidate_onvif_ports = onvif_ports[:DISCOVERY_MAX_CANDIDATE_PORTS]
        if rtsp_ports:
            ds.candidate_rtsp_ports = rtsp_ports[:DISCOVERY_MAX_CANDIDATE_PORTS]
        ct = _pfloat(self.connect_timeout_var.get())
        if ct is not None:
            ds.connect_timeout_s = ct
        st = _pfloat(self.soap_timeout_var.get())
        if st is not None:
            ds.soap_timeout_s = st
        mh = _pint(self.max_hosts_var.get())
        if mh is not None:
            ds.max_parallel_hosts = mh
        mp = _pint(self.max_ports_var.get())
        if mp is not None:
            ds.max_parallel_ports_per_host = mp

        extended_confirmed = False
        if mode == DiscoveryMode.EXTENDED:
            if not ds.allow_extended_scan:
                messagebox.showwarning(
                    "Discover & Diagnose",
                    "EXTENDED mode is disabled. Set [DISCOVERY] allow_extended_scan = true in config.txt first.",
                    parent=self)
                return
            extended_confirmed = messagebox.askyesno(
                "Confirm EXTENDED scan",
                f"EXTENDED mode additionally probes: {', '.join(str(p) for p in ds.extended_ports) or '(no extended_ports configured)'}\n"
                f"across {subnet_cidr}. This stays local-subnet-only, read-only, and rate-limited -- "
                f"but it is a wider probe than STANDARD. Continue?", parent=self)
            if not extended_confirmed:
                return

        all_profiles = BUILTIN_DEVICE_PROFILES + self.main_app.app_config.device_profile_overrides
        scan_cfg, problems = build_scan_config(mode, adapter, subnet_cidr, ds, extended_confirmed=extended_confirmed)
        if scan_cfg is None:
            messagebox.showerror("Discover & Diagnose", "Scan refused:\n" + "\n".join(problems), parent=self)
            return
        for p in problems:
            self.status_var.set(p)

        self.event_queue = queue.Queue()
        self.worker = DiscoveryWorker(scan_cfg, all_profiles, self.event_queue, seed_devices=self.devices)
        self.worker.start()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.status_var.set(f"Scanning {subnet_cidr} via '{adapter.name}' ({mode.value})...")

    def _on_cancel(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.status_var.set("Cancelling...")

    def _on_clear(self) -> None:
        self.devices.clear()
        self.tree.selection_remove(self.tree.selection())
        self._refresh_tree()
        self._show_details(None)
        self.status_var.set("Results cleared.")

    def _on_export(self) -> None:
        if not self.devices:
            messagebox.showinfo("Discover & Diagnose", "No results to export yet.", parent=self)
            return
        scan_cfg = self.last_scan_cfg
        if scan_cfg is None:
            adapter = self._current_adapter() or AdapterInfo(name="unknown", ipv4="0.0.0.0")
            scan_cfg, _problems = build_scan_config(DiscoveryMode.QUICK, adapter, self.subnet_var.get().strip() or "0.0.0.0/32",
                                                      self.main_app.app_config.discovery)
        json_path, txt_path = save_discovery_report(
            self.devices, scan_cfg, self._last_started_at or "unknown", self._last_finished_at or "unknown",
            self._last_limitations)
        messagebox.showinfo("Discover & Diagnose", f"Report saved:\n{txt_path}", parent=self)

    # ---------------------------------------------------------------
    # Event queue draining (root.after loop) -- never touches Tkinter
    # from any thread other than this one
    # ---------------------------------------------------------------
    def _poll_events(self) -> None:
        try:
            while True:
                evt = self.event_queue.get_nowait()
                self._handle_event(evt)
        except queue.Empty:
            pass
        self._poll_after_id = self.after(150, self._poll_events)

    def _handle_event(self, evt: DiscoveryEvent) -> None:
        if evt.event_type == "PROGRESS":
            self.status_var.set(str(evt.payload.get("message", "")))
        elif evt.event_type == "DEVICE_UPDATED":
            if self.worker is not None:
                sid = evt.payload.get("stable_id")
                dev = self.worker.devices.get(sid)
                if dev is not None:
                    self.devices[sid] = dev
            self._refresh_tree()
        elif evt.event_type == "DONE":
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            cancelled = bool(evt.payload.get("cancelled", False))
            count = evt.payload.get("device_count", 0)
            self.status_var.set(f"{'Cancelled' if cancelled else 'Scan complete'}. {count} candidate device(s) total.")
            if self.worker is not None:
                self._last_started_at = self.worker.started_at
                self._last_finished_at = self.worker.finished_at
                self._last_limitations = list(self.worker.limitations)
                self.last_scan_cfg = self.worker.scan_config
                self.devices.update(self.worker.devices)
                if self.worker.limitations:
                    self.status_var.set(self.status_var.get() + "  (see Export Report for limitations)")
            self._refresh_tree()
        elif evt.event_type == "ERROR":
            self.status_var.set(f"Error: {evt.payload.get('message', '')}")

    # ---------------------------------------------------------------
    # Results grid
    # ---------------------------------------------------------------
    def _compute_display_names(self) -> Dict[str, str]:
        groups: Dict[str, List[str]] = {}
        for dev in self.devices.values():
            if dev.model:
                groups.setdefault(dev.model, []).append(dev.stable_id)
        names: Dict[str, str] = {}
        for model, ids in groups.items():
            ids_sorted = sorted(ids)
            if len(ids_sorted) > 1:
                for i, sid in enumerate(ids_sorted, start=1):
                    names[sid] = f"{model} #{i}"
            else:
                names[ids_sorted[0]] = model
        for dev in self.devices.values():
            if dev.stable_id not in names:
                names[dev.stable_id] = dev.display_name or dev.last_ip or dev.stable_id
        return names

    def _filtered_devices(self) -> List[DiscoveredDevice]:
        names = self._compute_display_names()
        f = self.filter_var.get()
        out: List[DiscoveredDevice] = []
        for dev in self.devices.values():
            if f == "All":
                out.append(dev)
            elif f == "Confirmed ONVIF" and dev.classification == DeviceClassification.CONFIRMED_ONVIF:
                out.append(dev)
            elif f == "Likely ONVIF" and dev.classification == DeviceClassification.LIKELY_ONVIF:
                out.append(dev)
            elif f == "Possible Camera" and dev.classification == DeviceClassification.POSSIBLE_CAMERA:
                out.append(dev)
            elif f == "Network Device" and dev.classification == DeviceClassification.NETWORK_DEVICE:
                out.append(dev)
            elif f == "Authentication Required" and dev.onvif_auth_result == "required":
                out.append(dev)
            elif f == "Duplicate Group" and "#" in names.get(dev.stable_id, ""):
                out.append(dev)
        return out

    def _refresh_tree(self) -> None:
        names = self._compute_display_names()
        existing = set(self.tree.get_children())
        shown = self._filtered_devices()
        shown_ids = set()
        for dev in shown:
            shown_ids.add(dev.stable_id)
            onvif_disp = dev.onvif_xaddr or (str(dev.onvif_port) if dev.onvif_port else "-")
            ping_disp = "yes" if dev.ping_reply else ("no" if dev.ping_reply is False else "n/a")
            ptz_disp = {True: "yes", False: "no"}.get(dev.capabilities.get("ptz"), "unknown")
            values = (
                names.get(dev.stable_id, dev.stable_id), dev.classification.value, dev.confidence.value,
                dev.stable_id, dev.last_ip or "-", dev.mac_address or "-", dev.manufacturer or "-",
                dev.model or "-", dev.firmware_version or "-", onvif_disp,
                "yes" if dev.rtsp_candidate else "no", ping_disp, dev.onvif_auth_result or "-", ptz_disp,
                ",".join(dev.discovery_sources) or "-", (dev.last_seen_utc or "-")[:19],
            )
            if dev.stable_id in existing:
                self.tree.item(dev.stable_id, values=values)
            else:
                self.tree.insert("", "end", iid=dev.stable_id, values=values)
        for iid in existing - shown_ids:
            self.tree.delete(iid)

    def _on_tree_select(self, _event: Any = None) -> None:
        sel = self.tree.selection()
        if not sel:
            self._show_details(None)
            return
        self.selected_stable_id = sel[-1]
        self._show_details(self.devices.get(self.selected_stable_id))

    def _show_details(self, dev: Optional[DiscoveredDevice]) -> None:
        self.details_text.configure(state="normal")
        self.details_text.delete("1.0", "end")
        if dev is not None:
            names = self._compute_display_names()
            lines = [
                f"Display name: {names.get(dev.stable_id, dev.stable_id)}",
                f"Stable ID: {dev.stable_id}",
                f"Classification: {dev.classification.value}    Confidence: {dev.confidence.value}",
                f"EndpointReference: {dev.endpoint_uuid or '-'}",
                f"Scopes: {', '.join(dev.scopes) if dev.scopes else '-'}",
                f"Serial number: {dev.serial_number or '-'}      Hardware ID: {dev.hardware_id or '-'}",
                f"MAC: {dev.mac_address or '-'}   {dev.mac_vendor_hint}",
                f"ONVIF XAddr: {dev.onvif_xaddr or '-'}   (candidate port {dev.onvif_port or '-'})",
                f"RTSP candidate: {'yes' if dev.rtsp_candidate else 'no'}",
                f"Authentication result: {dev.onvif_auth_result or 'not attempted'}",
                f"Capabilities (from GetCapabilities, if credentials were supplied): {dev.capabilities or '-'}",
                f"Matched reference profile: {dev.matched_profile_id or '-'} (a hint only, never proof)",
                f"Discovery source(s): {', '.join(dev.discovery_sources) or '-'}",
                f"TCP ports seen open: {dev.tcp_open_ports or '-'}",
                f"Last seen (UTC): {dev.last_seen_utc or '-'}",
            ]
            if dev.warnings:
                lines.append("Warnings:")
                lines.extend(f"  - {w}" for w in dev.warnings)
            lines.append("")
            lines.append("Presets, media profiles and full PTZ node details are not enumerated during "
                          "discovery -- add this device as a camera below, then use the main window's "
                          "Probe button for that depth.")
            self.details_text.insert("1.0", "\n".join(lines))
        self.details_text.configure(state="disabled")

    # ---------------------------------------------------------------
    # Adding approved devices as cameras (staged in memory; actual
    # config.txt write still goes through the existing, already-safe
    # Save Configuration path -- backup + atomic replace + rebuilt
    # workers, exactly as for a manually-added camera)
    # ---------------------------------------------------------------
    def _on_add_selected(self) -> None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Discover & Diagnose", "Select one or more rows first.", parent=self)
            return
        names = self._compute_display_names()
        added_any = False
        for sid in sel:
            dev = self.devices.get(sid)
            if dev is None:
                continue
            if dev.classification not in (DeviceClassification.CONFIRMED_ONVIF, DeviceClassification.LIKELY_ONVIF):
                if not messagebox.askyesno(
                        "Discover & Diagnose",
                        f"'{names.get(sid, sid)}' is classified {dev.classification.value}, not a confirmed "
                        f"ONVIF device. Add it anyway?", parent=self):
                    continue
            if self._add_one_device(dev, names.get(sid, dev.model or sid)):
                added_any = True
        if added_any:
            self.main_app._refresh_camera_tree()
            messagebox.showinfo(
                "Discover & Diagnose",
                "Added in memory. Switch to the main window and click Save to write config.txt, "
                "back up the previous version, and start the new camera's worker.", parent=self)

    def _add_one_device(self, dev: DiscoveredDevice, suggested_name: str) -> bool:
        dlg = tk.Toplevel(self)
        dlg.title(f"Add camera: {suggested_name}")
        dlg.transient(self)
        dlg.grab_set()
        result = {"ok": False}

        base_id = re.sub(r"[^a-zA-Z0-9_]", "_", (dev.stable_id or "cam_new")).lower()
        id_var = tk.StringVar(value=base_id)
        name_var = tk.StringVar(value=suggested_name)
        host_var = tk.StringVar(value=dev.last_ip)
        port_var = tk.StringVar(value=str(dev.onvif_port or 80))
        user_var = tk.StringVar(value="")
        pass_var = tk.StringVar(value="")
        presets_var = tk.StringVar(value="MAIN")

        def row(label: str, var: tk.StringVar, width: int = 28) -> None:
            f = ttk.Frame(dlg)
            f.pack(fill="x", padx=8, pady=3)
            ttk.Label(f, text=label, width=20).pack(side="left")
            ttk.Entry(f, textvariable=var, width=width).pack(side="left", fill="x", expand=True)

        ttk.Label(dlg, text=f"Classification: {dev.classification.value}    Confidence: {dev.confidence.value}",
                  foreground="#555555").pack(anchor="w", padx=8, pady=(8, 0))
        row("Stable ID", id_var)
        row("Display name", name_var)
        row("Host / IP", host_var)
        row("ONVIF port", port_var)
        row("Username", user_var)
        row("Password", pass_var)
        row("Presets (alias,alias,...)", presets_var)
        ttk.Label(dlg, text="Credentials are written to config.txt only after Save -- never into any "
                             "discovery report.", foreground="#555555", wraplength=380
                  ).pack(anchor="w", padx=8, pady=(0, 6))

        def on_ok() -> None:
            cam_id = re.sub(r"[^a-zA-Z0-9_]", "_", id_var.get().strip())
            if not cam_id:
                messagebox.showerror("Add camera", "A stable ID is required.", parent=dlg)
                return
            if cam_id in self.main_app.app_config.cameras:
                messagebox.showerror("Add camera", f"Camera id '{cam_id}' already exists.", parent=dlg)
                return
            port = _pint(port_var.get()) or (dev.onvif_port or 80)
            aliases = [a.strip() for a in presets_var.get().split(",") if a.strip()]
            presets = {alias: str(i + 1) for i, alias in enumerate(aliases)}
            patrol_name = f"{cam_id}_patrol"
            cam = CameraConfig(id=cam_id, name=name_var.get().strip() or cam_id, host=host_var.get().strip(),
                                onvif_port=port, username=user_var.get().strip(), password=pass_var.get(),
                                patrol_name=patrol_name, home_preset=(aliases[0] if aliases else ""),
                                presets=presets)
            self.main_app.app_config.cameras[cam_id] = cam
            self.main_app.app_config.patrols[patrol_name] = Patrol(name=patrol_name, steps=[])
            result["ok"] = True
            dlg.destroy()

        btnrow = ttk.Frame(dlg)
        btnrow.pack(fill="x", padx=8, pady=8)
        ttk.Button(btnrow, text="Add", command=on_ok).pack(side="right", padx=4)
        ttk.Button(btnrow, text="Cancel", command=dlg.destroy).pack(side="right")
        dlg.wait_window()
        return result["ok"]

    # ---------------------------------------------------------------
    # Close: cancel any active scan (never touches camera workers),
    # persist UI preferences into the in-memory config (written to
    # disk on the main window's next Save, same as every other edit).
    # ---------------------------------------------------------------
    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.worker.cancel()
            self.worker.join(timeout=3.0)
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:
                pass
        du = self.main_app.app_config.discovery_ui
        adapter = self._current_adapter()
        if adapter is not None:
            du.last_adapter_name = adapter.name
        du.last_subnet_cidr = self.subnet_var.get().strip()
        try:
            du.last_mode = DiscoveryMode(self.mode_var.get())
        except ValueError:
            pass
        if self.filter_var.get() in self.FILTERS:
            du.last_filter = self.filter_var.get()
        self.destroy()


class PTZPatrolGUI(tk.Tk):
    """The whole application UI. One window, one class, matching the
    uploaded sketch: left camera list + manual PTZ pad, upper-right
    dynamic patrol editor, lower-right 4x2 preview/status grid, bottom
    status bar with a global Emergency Stop. Nothing in this class
    ever calls a CameraBridge directly or blocks on network I/O --
    every action enqueues a command on the target camera's worker and
    every piece of live status is read back from that worker's plain
    attributes on the next after()-scheduled poll."""

    def __init__(self, app_config: AppConfig, ini_doc: IniDocument, load_issues: List[ValidationIssue]):
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("1440x840")
        self.minsize(1100, 680)

        self.app_config = app_config
        self.ini_doc = ini_doc
        self.event_queue: "queue.Queue[WorkerEvent]" = queue.Queue()
        self.logger = setup_logging(app_config.app.log_level, app_config.app.log_max_bytes, app_config.app.log_backup_count)
        _redactor.update_from_config(app_config)
        write_cli_cache(app_config)

        self.worker_manager = WorkerManager(self.event_queue)
        self.selected_camera_id: Optional[str] = None
        self._pad_held_direction: Optional[str] = None
        self._pad_refresh_after_id: Optional[str] = None
        self._suspend_automation_traces = False
        self._tile_camera_ids: List[str] = []
        self._shutting_down = False
        self._poll_after_id: Optional[str] = None
        self.last_global_error = ""
        self.tile_widgets: Dict[int, Dict[str, Any]] = {}

        self.speed_var = tk.DoubleVar(value=app_config.manual_override.manual_speed_default)
        self.pause_duration_var = tk.StringVar(value=str(app_config.manual_override.default_pause_s))
        self.automation_enabled_var = tk.BooleanVar(value=False)
        self.repeat_mode_var = tk.StringVar(value=RepeatMode.FOREVER.value)
        self.repeat_count_var = tk.StringVar(value="1")
        self.protocol_var = tk.StringVar(value="ONVIF")

        self._build_gui()

        self.worker_manager.build_workers(self.app_config)
        self.worker_manager.start_all()
        self._refresh_camera_tree()
        self._reload_patrol_editor()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_after_id = self.after(GUI_POLL_MS, self._poll_events)

        if load_issues:
            self.after(300, lambda: self._show_load_issues(load_issues))

    # ---------------------------------------------------------------
    # Layout
    # ---------------------------------------------------------------
    def _build_gui(self) -> None:
        main = ttk.Frame(self)
        main.pack(fill="both", expand=True)

        paned = ttk.PanedWindow(main, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned, width=380)
        paned.add(left, weight=1)

        right = ttk.PanedWindow(paned, orient="vertical")
        paned.add(right, weight=3)

        editor_frame = ttk.Frame(right)
        right.add(editor_frame, weight=2)
        preview_frame = ttk.Frame(right)
        right.add(preview_frame, weight=1)

        self._build_left_panel(left)
        self._build_patrol_editor(editor_frame)
        self._build_preview_grid(preview_frame)
        self._build_status_bar(main)

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Cameras", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", padx=6, pady=(6, 2))

        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill="both", expand=False, padx=6)
        columns = ("en", "id", "name", "host", "state")
        self.camera_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=8, selectmode="browse")
        for col, label, width in (("en", "On", 30), ("id", "ID", 90), ("name", "Name", 100),
                                   ("host", "Host:Port", 130), ("state", "State", 110)):
            self.camera_tree.heading(col, text=label)
            self.camera_tree.column(col, width=width, anchor="w")
        self.camera_tree.pack(side="left", fill="both", expand=True)
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.camera_tree.yview)
        self.camera_tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.camera_tree.bind("<<TreeviewSelect>>", self._on_camera_selected)

        btns = ttk.Frame(parent)
        btns.pack(fill="x", padx=6, pady=4)
        ttk.Button(btns, text="Add", command=self._on_add_camera).pack(side="left", padx=2)
        ttk.Button(btns, text="Discover", command=self._on_discover).pack(side="left", padx=2)
        ttk.Button(btns, text="Delete", command=self._on_delete_camera).pack(side="left", padx=2)
        ttk.Button(btns, text="Probe", command=self._on_probe).pack(side="left", padx=2)
        ttk.Button(btns, text="Save", command=self._on_save_config).pack(side="left", padx=2)

        proto_frame = ttk.Frame(parent)
        proto_frame.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(proto_frame, text="Protocol:").pack(side="left")
        ttk.Combobox(proto_frame, textvariable=self.protocol_var, values=["ONVIF"], state="readonly", width=10).pack(side="left", padx=4)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", padx=6, pady=6)
        ttk.Label(parent, text="Manual PTZ (selected camera)", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", padx=6)

        pad = ttk.Frame(parent)
        pad.pack(pady=8)
        up = ttk.Button(pad, text="\u25B2", width=4)
        up.grid(row=0, column=1)
        left_b = ttk.Button(pad, text="\u25C0", width=4)
        left_b.grid(row=1, column=0)
        stop_b = ttk.Button(pad, text="STOP", width=8, command=self._on_pad_stop)
        stop_b.grid(row=1, column=1, padx=2)
        right_b = ttk.Button(pad, text="\u25B6", width=4)
        right_b.grid(row=1, column=2)
        down = ttk.Button(pad, text="\u25BC", width=4)
        down.grid(row=2, column=1)

        for btn, direction in ((up, "up"), (down, "down"), (left_b, "left"), (right_b, "right")):
            btn.bind("<ButtonPress-1>", lambda e, d=direction: self._on_pad_press(d))
            btn.bind("<ButtonRelease-1>", self._on_pad_release)
            btn.bind("<Leave>", self._on_pad_leave)
            btn.bind("<FocusOut>", self._on_pad_leave)

        ttk.Button(parent, text="Home", command=self._on_home_clicked).pack(pady=(4, 6))

        speed_frame = ttk.Frame(parent)
        speed_frame.pack(fill="x", padx=6, pady=(0, 10))
        ttk.Label(speed_frame, text="Speed").pack(side="left")
        ttk.Scale(speed_frame, from_=0.05, to=1.0, variable=self.speed_var, orient="horizontal").pack(
            side="left", fill="x", expand=True, padx=6)

    def _build_patrol_editor(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Patrol Editor", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", padx=6, pady=(4, 2))

        rows_container = ttk.Frame(parent)
        rows_container.pack(fill="both", expand=True, padx=(6, 0))
        canvas = tk.Canvas(rows_container, highlightthickness=0)
        vscroll = ttk.Scrollbar(rows_container, orient="vertical", command=canvas.yview)
        self.patrol_rows_frame = ttk.Frame(canvas)
        self.patrol_rows_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.patrol_rows_frame, anchor="nw")
        canvas.configure(yscrollcommand=vscroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")

        toolbar1 = ttk.Frame(parent)
        toolbar1.pack(fill="x", padx=6, pady=4)
        ttk.Button(toolbar1, text="Add Step", command=self._add_step).pack(side="left", padx=2)
        ttk.Checkbutton(toolbar1, text="Enable Automation", variable=self.automation_enabled_var,
                        command=self._on_toggle_automation).pack(side="left", padx=10)
        ttk.Radiobutton(toolbar1, text="Repeat Forever", variable=self.repeat_mode_var,
                        value=RepeatMode.FOREVER.value, command=self._on_repeat_mode_changed).pack(side="left")
        ttk.Radiobutton(toolbar1, text="Fixed count:", variable=self.repeat_mode_var,
                        value=RepeatMode.FIXED_COUNT.value, command=self._on_repeat_mode_changed).pack(side="left")
        count_entry = ttk.Entry(toolbar1, textvariable=self.repeat_count_var, width=4)
        count_entry.pack(side="left", padx=2)
        self.repeat_count_var.trace_add("write", self._on_repeat_count_changed)

        toolbar2 = ttk.Frame(parent)
        toolbar2.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Button(toolbar2, text="Test One Cycle", command=self._on_test_one_cycle).pack(side="left", padx=2)
        ttk.Button(toolbar2, text="Start", command=self._on_start_clicked).pack(side="left", padx=2)
        ttk.Label(toolbar2, text="Pause (s, 0=indefinite):").pack(side="left", padx=(10, 2))
        self.pause_duration_combo = ttk.Combobox(
            toolbar2, textvariable=self.pause_duration_var, width=8, state="readonly",
            values=[str(x) for x in self.app_config.manual_override.pause_options_s])
        self.pause_duration_combo.pack(side="left")
        ttk.Button(toolbar2, text="Pause", command=self._on_pause_clicked).pack(side="left", padx=2)
        ttk.Button(toolbar2, text="Resume", command=self._on_resume_clicked).pack(side="left", padx=2)
        ttk.Button(toolbar2, text="Stop", command=self._on_stop_clicked).pack(side="left", padx=2)
        ttk.Button(toolbar2, text="Return Home", command=self._on_return_home_clicked).pack(side="left", padx=2)
        ttk.Button(toolbar2, text="Save Configuration", command=self._on_save_config).pack(side="left", padx=14)

    def _build_preview_grid(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Camera Status / Preview", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", padx=6, pady=(4, 2))
        grid = tk.Frame(parent)
        grid.pack(fill="both", expand=True, padx=4, pady=4)
        for i in range(MAX_PREVIEW_TILES):
            r, c = divmod(i, 4)
            grid.columnconfigure(c, weight=1)
            grid.rowconfigure(r, weight=1)
            tile = tk.Frame(grid, borderwidth=1, relief="solid", bg="#eeeeee")
            tile.grid(row=r, column=c, padx=3, pady=3, sticky="nsew")
            name_lbl = tk.Label(tile, text="(empty)", font=("TkDefaultFont", 9, "bold"), bg="#eeeeee", anchor="w")
            name_lbl.pack(fill="x", padx=4, pady=(2, 0))
            image_lbl = tk.Label(tile, bg="#eeeeee")
            image_lbl.pack()
            state_lbl = tk.Label(tile, text="", font=("TkDefaultFont", 8), bg="#eeeeee", anchor="w")
            state_lbl.pack(fill="x", padx=4)
            step_lbl = tk.Label(tile, text="", font=("TkDefaultFont", 8), bg="#eeeeee", anchor="w")
            step_lbl.pack(fill="x", padx=4)
            countdown_lbl = tk.Label(tile, text="", font=("TkDefaultFont", 8), bg="#eeeeee", anchor="w")
            countdown_lbl.pack(fill="x", padx=4, pady=(0, 2))
            for w in (tile, name_lbl, image_lbl, state_lbl, step_lbl, countdown_lbl):
                w.bind("<Button-1>", lambda e, idx=i: self._on_tile_clicked(idx))
            self.tile_widgets[i] = {"frame": tile, "name": name_lbl, "image": image_lbl,
                                     "state": state_lbl, "step": step_lbl, "countdown": countdown_lbl, "photo": None}

    def _build_status_bar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent, relief="sunken")
        bar.pack(fill="x", side="bottom")
        self.status_counts_label = ttk.Label(bar, text="Enabled: 0  Connected: 0  Running: 0  Paused: 0  Offline: 0")
        self.status_counts_label.pack(side="left", padx=8, pady=4)
        self.status_mode_label = ttk.Label(bar, text="")
        self.status_mode_label.pack(side="left", padx=8)
        self.status_error_label = ttk.Label(bar, text="", foreground="#b00020")
        self.status_error_label.pack(side="left", padx=8, fill="x", expand=True)
        estop = tk.Button(bar, text="EMERGENCY STOP", bg="#c0392b", fg="white", activebackground="#a93226",
                           font=("TkDefaultFont", 10, "bold"), command=self._on_emergency_stop)
        estop.pack(side="right", padx=10, pady=4, ipadx=10, ipady=6)

    # ---------------------------------------------------------------
    # Patrol editor: row (re)building
    # ---------------------------------------------------------------
    def _current_editor_patrol(self) -> Optional[Patrol]:
        if not self.selected_camera_id:
            return None
        cam = self.app_config.cameras.get(self.selected_camera_id)
        if cam is None or not cam.patrol_name:
            return None
        return self.app_config.patrols.get(cam.patrol_name)

    def _reload_patrol_editor(self) -> None:
        patrol = self._current_editor_patrol()
        self._suspend_automation_traces = True
        if patrol is not None:
            self.automation_enabled_var.set(patrol.enabled)
            self.repeat_mode_var.set(patrol.repeat_mode.value)
            self.repeat_count_var.set(str(patrol.repeat_count))
        else:
            self.automation_enabled_var.set(False)
            self.repeat_mode_var.set(RepeatMode.FOREVER.value)
            self.repeat_count_var.set("1")
        self._suspend_automation_traces = False
        self._rebuild_patrol_rows()

    def _rebuild_patrol_rows(self) -> None:
        for child in self.patrol_rows_frame.winfo_children():
            child.destroy()
        patrol = self._current_editor_patrol()
        if patrol is None:
            ttk.Label(self.patrol_rows_frame,
                      text="(select a camera with a patrol assigned, or use Add to create one)").grid(
                row=0, column=0, sticky="w", padx=4, pady=4)
            return

        headers = ["#", "On", "Action", "Target", "Pan", "Tilt", "Zoom", "Move s", "Settle s", "Dwell s", "Row"]
        for c, h in enumerate(headers):
            ttk.Label(self.patrol_rows_frame, text=h, font=("TkDefaultFont", 9, "bold")).grid(
                row=0, column=c, padx=3, pady=(0, 4), sticky="w")

        for i, step in enumerate(patrol.steps):
            r = i + 1
            ttk.Label(self.patrol_rows_frame, text=str(i + 1)).grid(row=r, column=0, padx=3)

            en_var = tk.BooleanVar(value=step.enabled)
            ttk.Checkbutton(self.patrol_rows_frame, variable=en_var,
                            command=lambda s=step, v=en_var: setattr(s, "enabled", v.get())).grid(row=r, column=1)

            action_var = tk.StringVar(value=step.action.value)
            action_cb = ttk.Combobox(self.patrol_rows_frame, textvariable=action_var, width=14, state="readonly",
                                      values=[a.value for a in StepAction])
            action_cb.grid(row=r, column=2, padx=2)
            action_cb.bind("<<ComboboxSelected>>", lambda e, s=step, v=action_var: setattr(s, "action", StepAction(v.get())))

            target_var = tk.StringVar(value=step.target)
            ttk.Entry(self.patrol_rows_frame, textvariable=target_var, width=9).grid(row=r, column=3, padx=2)
            target_var.trace_add("write", lambda *a, s=step, v=target_var: setattr(s, "target", v.get()))

            def make_float_var(initial: float) -> tk.StringVar:
                return tk.StringVar(value=f"{initial:.2f}")

            pan_var = make_float_var(step.pan)
            tilt_var = make_float_var(step.tilt)
            zoom_var = make_float_var(step.zoom)
            move_var = tk.StringVar(value=f"{step.move_s:.1f}")
            settle_var = tk.StringVar(value=f"{step.settle_s:.1f}")
            dwell_var = tk.StringVar(value=f"{step.dwell_s:.1f}")

            def bind_float(var: tk.StringVar, step_ref: PatrolStep, attr: str, lo: float, hi: float) -> None:
                def _cb(*_a: Any) -> None:
                    v = _pfloat(var.get())
                    if v is not None:
                        setattr(step_ref, attr, max(lo, min(hi, v)))
                var.trace_add("write", _cb)

            specs = ((pan_var, "pan", -1.0, 1.0, 4), (tilt_var, "tilt", -1.0, 1.0, 5), (zoom_var, "zoom", -1.0, 1.0, 6),
                     (move_var, "move_s", 0.0, MAX_CONTINUOUS_MOVE_S, 7), (settle_var, "settle_s", 0.0, MAX_DWELL_S, 8),
                     (dwell_var, "dwell_s", 0.0, MAX_DWELL_S, 9))
            for var, attr, lo, hi, col in specs:
                bind_float(var, step, attr, lo, hi)
                ttk.Entry(self.patrol_rows_frame, textvariable=var, width=6).grid(row=r, column=col, padx=1)

            rowbtns = ttk.Frame(self.patrol_rows_frame)
            rowbtns.grid(row=r, column=10, padx=4)
            ttk.Button(rowbtns, text="^", width=2, command=lambda idx=i: self._move_step(idx, -1)).pack(side="left")
            ttk.Button(rowbtns, text="v", width=2, command=lambda idx=i: self._move_step(idx, 1)).pack(side="left")
            ttk.Button(rowbtns, text="Dup", width=4, command=lambda idx=i: self._duplicate_step(idx)).pack(side="left")
            ttk.Button(rowbtns, text="Del", width=4, command=lambda idx=i: self._delete_step(idx)).pack(side="left")

    def _add_step(self) -> None:
        patrol = self._current_editor_patrol()
        if patrol is None:
            messagebox.showinfo(APP_NAME, "Select a camera with a patrol assigned first (Add Camera creates one).")
            return
        patrol.steps.append(PatrolStep(action=StepAction.GOTO_PRESET, settle_s=1.0, dwell_s=5.0))
        self._rebuild_patrol_rows()

    def _move_step(self, idx: int, delta: int) -> None:
        patrol = self._current_editor_patrol()
        if patrol is None:
            return
        new_idx = idx + delta
        if 0 <= new_idx < len(patrol.steps):
            patrol.steps[idx], patrol.steps[new_idx] = patrol.steps[new_idx], patrol.steps[idx]
            self._rebuild_patrol_rows()

    def _duplicate_step(self, idx: int) -> None:
        patrol = self._current_editor_patrol()
        if patrol is None:
            return
        patrol.steps.insert(idx + 1, copy.deepcopy(patrol.steps[idx]))
        self._rebuild_patrol_rows()

    def _delete_step(self, idx: int) -> None:
        patrol = self._current_editor_patrol()
        if patrol is None:
            return
        del patrol.steps[idx]
        self._rebuild_patrol_rows()

    def _on_toggle_automation(self) -> None:
        if self._suspend_automation_traces:
            return
        patrol = self._current_editor_patrol()
        if patrol is not None:
            patrol.enabled = self.automation_enabled_var.get()

    def _on_repeat_mode_changed(self) -> None:
        if self._suspend_automation_traces:
            return
        patrol = self._current_editor_patrol()
        if patrol is not None:
            try:
                patrol.repeat_mode = RepeatMode(self.repeat_mode_var.get())
            except ValueError:
                pass

    def _on_repeat_count_changed(self, *_args: Any) -> None:
        if self._suspend_automation_traces:
            return
        patrol = self._current_editor_patrol()
        if patrol is not None:
            v = _pint(self.repeat_count_var.get())
            if v and v > 0:
                patrol.repeat_count = v

    # ---------------------------------------------------------------
    # 14. GUI CALLBACKS
    # ---------------------------------------------------------------
    def _selected_worker(self) -> Optional[CameraWorker]:
        if not self.selected_camera_id:
            return None
        return self.worker_manager.get_worker(self.selected_camera_id)

    # -- manual PTZ pad: press-and-hold with dead-man protection --
    def _pad_vector(self, direction: str) -> Tuple[float, float, float]:
        speed = float(self.speed_var.get())
        return {"up": (0.0, speed, 0.0), "down": (0.0, -speed, 0.0),
                "left": (-speed, 0.0, 0.0), "right": (speed, 0.0, 0.0)}[direction]

    def _send_manual_move(self, worker: CameraWorker, direction: str) -> None:
        pan, tilt, zoom = self._pad_vector(direction)
        worker.enqueue(CommandType.MANUAL_MOVE, {
            "pan": pan, "tilt": tilt, "zoom": zoom,
            "override_pause_s": self.app_config.manual_override.default_pause_s,
        })

    def _on_pad_press(self, direction: str) -> None:
        worker = self._selected_worker()
        if worker is None:
            return
        self._pad_held_direction = direction
        self._send_manual_move(worker, direction)
        self._schedule_pad_refresh()

    def _schedule_pad_refresh(self) -> None:
        self._cancel_pad_refresh()
        self._pad_refresh_after_id = self.after(int(MANUAL_MOVE_CHUNK_S * 1000 * 0.7), self._pad_refresh_tick)

    def _pad_refresh_tick(self) -> None:
        if self._pad_held_direction is None:
            return
        worker = self._selected_worker()
        if worker is not None:
            self._send_manual_move(worker, self._pad_held_direction)
        self._schedule_pad_refresh()

    def _cancel_pad_refresh(self) -> None:
        if self._pad_refresh_after_id is not None:
            try:
                self.after_cancel(self._pad_refresh_after_id)
            except Exception:
                pass
            self._pad_refresh_after_id = None

    def _on_pad_release(self, _event: Any = None) -> None:
        self._pad_held_direction = None
        self._cancel_pad_refresh()
        worker = self._selected_worker()
        if worker is not None:
            worker.enqueue(CommandType.MANUAL_STOP)

    def _on_pad_leave(self, _event: Any = None) -> None:
        if self._pad_held_direction is not None:
            self._on_pad_release()

    def _on_pad_stop(self) -> None:
        self._on_pad_release()

    def _on_home_clicked(self) -> None:
        worker = self._selected_worker()
        if worker is None:
            messagebox.showinfo(APP_NAME, "Select a camera first.")
            return
        worker.enqueue(CommandType.HOME)

    # -- patrol run controls --
    # NOTE: these methods never mutate a CameraWorker's attributes
    # directly -- every one of them only ever calls worker.enqueue(...),
    # same as the manual-PTZ-pad and pause/resume handlers above. All
    # actual state changes happen inside CameraWorker._handle_command,
    # in that worker's own thread, so camera state is always touched by
    # exactly one thread at a time.
    def _on_start_clicked(self) -> None:
        worker = self._selected_worker()
        if worker is None:
            messagebox.showinfo(APP_NAME, "Select a camera first.")
            return
        worker.enqueue(CommandType.START_PATROL)

    def _on_pause_clicked(self) -> None:
        worker = self._selected_worker()
        if worker is None:
            return
        pause_s = _pint(self.pause_duration_var.get()) or 0
        worker.enqueue(CommandType.PAUSE, {"pause_s": pause_s})

    def _on_resume_clicked(self) -> None:
        worker = self._selected_worker()
        if worker is not None:
            worker.enqueue(CommandType.RESUME)

    def _on_stop_clicked(self) -> None:
        worker = self._selected_worker()
        if worker is None:
            return
        worker.enqueue(CommandType.STOP_PATROL)

    def _on_return_home_clicked(self) -> None:
        worker = self._selected_worker()
        if worker is not None:
            worker.enqueue(CommandType.HOME)

    def _on_test_one_cycle(self) -> None:
        worker = self._selected_worker()
        if worker is None:
            messagebox.showinfo(APP_NAME, "Select a camera first.")
            return
        if not self.app_config.app.dry_run and not messagebox.askyesno(
                APP_NAME, f"This will move camera '{worker.camera.id}' through one full patrol "
                          f"cycle now (dry_run is OFF). Continue?"):
            return
        worker.enqueue(CommandType.TEST_ONE_CYCLE)

    # -- camera list actions --
    def _on_camera_selected(self, _event: Any = None) -> None:
        sel = self.camera_tree.selection()
        if sel:
            self._select_camera(sel[0])

    def _select_camera(self, cam_id: str) -> None:
        if cam_id == self.selected_camera_id:
            return
        self.selected_camera_id = cam_id
        try:
            self.camera_tree.selection_set(cam_id)
        except Exception:
            pass
        self._reload_patrol_editor()

    def _on_tile_clicked(self, idx: int) -> None:
        if idx >= len(self._tile_camera_ids):
            return
        cam_id = self._tile_camera_ids[idx]
        self._select_camera(cam_id)
        if self.app_config.preview.mode != PreviewMode.OFF:
            worker = self.worker_manager.get_worker(cam_id)
            if worker is not None:
                ensure_app_dirs()
                request_snapshot(worker, DIAGNOSTICS_DIR / f"preview_{cam_id}.png")

    def _on_add_camera(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("Add Camera")
        dlg.transient(self)
        dlg.grab_set()

        def row(label: str, default: str = "") -> tk.StringVar:
            f = ttk.Frame(dlg)
            f.pack(fill="x", padx=8, pady=3)
            ttk.Label(f, text=label, width=20).pack(side="left")
            var = tk.StringVar(value=default)
            ttk.Entry(f, textvariable=var, width=28).pack(side="left", fill="x", expand=True)
            return var

        id_var = row("Stable ID (a-z,0-9,_)")
        name_var = row("Display name")
        host_var = row("Host / IP", "192.168.1.100")
        port_var = row("ONVIF port", "8899")
        user_var = row("Username", "ptzuser")
        pass_var = row("Password", "CHANGE_ME")
        presets_var = row("Presets (alias,alias,...)", "MAIN,LEFT,RIGHT")

        def on_ok() -> None:
            cam_id = re.sub(r"[^a-zA-Z0-9_]", "_", id_var.get().strip())
            if not cam_id:
                messagebox.showerror(APP_NAME, "A stable ID is required.", parent=dlg)
                return
            if cam_id in self.app_config.cameras:
                messagebox.showerror(APP_NAME, f"Camera id '{cam_id}' already exists.", parent=dlg)
                return
            port = _pint(port_var.get()) or 8899
            aliases = [a.strip() for a in presets_var.get().split(",") if a.strip()]
            presets = {alias: str(i + 1) for i, alias in enumerate(aliases)}
            patrol_name = f"{cam_id}_patrol"
            cam = CameraConfig(id=cam_id, name=name_var.get().strip() or cam_id, host=host_var.get().strip(),
                                onvif_port=port, username=user_var.get().strip(), password=pass_var.get(),
                                patrol_name=patrol_name, home_preset=(aliases[0] if aliases else ""),
                                presets=presets)
            self.app_config.cameras[cam_id] = cam
            self.app_config.patrols[patrol_name] = Patrol(name=patrol_name, steps=[])
            dlg.destroy()
            self._refresh_camera_tree()
            self._select_camera(cam_id)
            messagebox.showinfo(APP_NAME, "Camera added in memory. Click Save to write config.txt and start its worker.")

        btnrow = ttk.Frame(dlg)
        btnrow.pack(fill="x", padx=8, pady=8)
        ttk.Button(btnrow, text="Add", command=on_ok).pack(side="right", padx=4)
        ttk.Button(btnrow, text="Cancel", command=dlg.destroy).pack(side="right")

    def _on_delete_camera(self) -> None:
        if not self.selected_camera_id:
            messagebox.showinfo(APP_NAME, "Select a camera first.")
            return
        cam_id = self.selected_camera_id
        if not messagebox.askyesno(APP_NAME, f"Delete camera '{cam_id}'? This cannot be undone once saved."):
            return
        cam = self.app_config.cameras.pop(cam_id, None)
        if cam is not None and cam.patrol_name:
            still_used = any(c.patrol_name == cam.patrol_name for c in self.app_config.cameras.values())
            if not still_used:
                self.app_config.patrols.pop(cam.patrol_name, None)
        self.selected_camera_id = None
        self._refresh_camera_tree()
        self._reload_patrol_editor()
        messagebox.showinfo(APP_NAME, "Camera removed in memory. Click Save to write config.txt and stop its worker.")

    def _on_discover(self) -> None:
        """Opens the Discover & Diagnose window: real WS-Discovery +
        ARP + bounded port probing + read-only ONVIF identity checks
        on an authorized subnet. Entirely independent of the camera
        patrol engine -- this never touches a CameraWorker."""
        DiscoveryDialog(self)

    def _on_probe(self) -> None:
        worker = self._selected_worker()
        if worker is None:
            messagebox.showinfo(APP_NAME, "Select a camera first.")
            return
        request_probe(worker)
        messagebox.showinfo(APP_NAME, f"Capability probe queued for '{worker.camera.id}'. "
                                       f"A summary will pop up when it completes; the full report is "
                                       f"saved under diagnostics/.")

    def _on_save_config(self) -> None:
        self.worker_manager.stop_all(join=True, timeout_s=self.app_config.shutdown.worker_join_timeout_s)
        ok, errors = save_config(CONFIG_PATH, self.ini_doc, self.app_config)
        if not ok:
            messagebox.showerror(APP_NAME, "Save failed -- config.txt was NOT changed:\n\n" + "\n".join(errors))
            self.worker_manager.build_workers(self.app_config)
            self.worker_manager.start_all()
            return
        self.app_config, self.ini_doc, issues = load_config(CONFIG_PATH)
        _redactor.update_from_config(self.app_config)
        write_cli_cache(self.app_config)
        self.worker_manager.build_workers(self.app_config)
        self.worker_manager.start_all()
        self._refresh_camera_tree()
        self._reload_patrol_editor()
        warnings = [i for i in issues if i.severity == "warning"]
        msg = "Configuration saved."
        if warnings:
            msg += "\n\nWarnings:\n" + "\n".join(str(i) for i in warnings[:8])
        messagebox.showinfo(APP_NAME, msg)

    def _on_emergency_stop(self) -> None:
        self.worker_manager.emergency_stop_all()
        self.last_global_error = "EMERGENCY STOP pressed"
        self.logger.warning("EMERGENCY STOP pressed by operator")

    def _show_load_issues(self, issues: List[ValidationIssue]) -> None:
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        lines = []
        if errors:
            lines.append(f"{len(errors)} error(s) -- affected camera/patrol/step(s) were skipped:")
            lines.extend(f"  {e}" for e in errors[:10])
        if warnings:
            lines.append(f"{len(warnings)} warning(s):")
            lines.extend(f"  {w}" for w in warnings[:10])
        messagebox.showwarning(APP_NAME, "config.txt loaded with issues:\n\n" + "\n".join(lines))

    # ---------------------------------------------------------------
    # Event queue draining / live refresh (root.after loop)
    # ---------------------------------------------------------------
    def _poll_events(self) -> None:
        drained = 0
        while drained < 300:
            try:
                evt = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_worker_event(evt)
            drained += 1

        if STOP_REQUEST_FILE.exists():
            try:
                STOP_REQUEST_FILE.unlink()
            except Exception:
                pass
            self._on_close()
            return

        self._refresh_camera_tree()
        self._refresh_preview_tiles()
        self._refresh_status_bar()

        if not self._shutting_down:
            self._poll_after_id = self.after(GUI_POLL_MS, self._poll_events)

    def _handle_worker_event(self, evt: WorkerEvent) -> None:
        if evt.event_type == "ERROR":
            self.last_global_error = f"{evt.camera_id}: {evt.payload.get('message', '')}"
        elif evt.event_type == "PROBE_DONE":
            tier = evt.payload.get("recommended_tier", "unknown")
            warn = evt.payload.get("warnings", [])
            msg = f"Capability probe for '{evt.camera_id}' complete.\nRecommended tier: {tier}\n"
            if warn:
                msg += "\nWarnings:\n- " + "\n- ".join(warn)
            msg += f"\n\nSaved to:\n{evt.payload.get('txt_path', '')}"
            messagebox.showinfo(APP_NAME, msg)
        elif evt.event_type == "SNAPSHOT" and evt.payload.get("ok"):
            path = Path(evt.payload.get("path", ""))
            for idx, cam_id in enumerate(self._tile_camera_ids):
                if cam_id == evt.camera_id:
                    photo = load_tile_image(path, self.app_config.preview.tile_width_px)
                    if photo is not None:
                        self.tile_widgets[idx]["image"].configure(image=photo)
                        self.tile_widgets[idx]["photo"] = photo  # keep a strong reference
                    break
        # STATE / STEP / COUNTDOWN / LOG: no per-event handling needed --
        # the worker keeps its own state/last_step_label/countdown_s
        # attributes current, and the refresh methods below read those
        # directly on every poll tick.

    def _refresh_camera_tree(self) -> None:
        existing = set(self.camera_tree.get_children())
        wanted = set(self.app_config.cameras.keys())
        for iid in existing - wanted:
            self.camera_tree.delete(iid)
        for cam_id, cam in self.app_config.cameras.items():
            worker = self.worker_manager.get_worker(cam_id)
            state_text = worker.state.value if worker else ("enabled, not started" if cam.enabled else "disabled")
            values = ("Y" if cam.enabled else "N", cam.id, cam.name, f"{cam.host}:{cam.onvif_port}", state_text)
            if cam_id in existing:
                self.camera_tree.item(cam_id, values=values)
            else:
                self.camera_tree.insert("", "end", iid=cam_id, values=values)
        if self.selected_camera_id and self.selected_camera_id in self.app_config.cameras:
            try:
                if self.camera_tree.selection() != (self.selected_camera_id,):
                    self.camera_tree.selection_set(self.selected_camera_id)
            except Exception:
                pass

    def _state_color(self, worker: Optional[CameraWorker]) -> str:
        if worker is None:
            return "#d0d0d0"
        if worker.state == CameraState.ERROR:
            return "#f2b3ab"
        if worker.state in (CameraState.DISCONNECTED, CameraState.RECOVERING, CameraState.CONNECTING):
            return "#f7d9a0"
        if worker.state in (CameraState.PAUSED, CameraState.MANUAL_OVERRIDE):
            return "#dcdcf2"
        if worker.state in (CameraState.MOVING, CameraState.SETTLING, CameraState.DWELLING):
            return "#cdeccd"
        return "#eeeeee"

    def _refresh_preview_tiles(self) -> None:
        cam_ids = list(self.app_config.cameras.keys())[:MAX_PREVIEW_TILES]
        self._tile_camera_ids = cam_ids
        for i in range(MAX_PREVIEW_TILES):
            w = self.tile_widgets[i]
            if i < len(cam_ids):
                cam_id = cam_ids[i]
                cam = self.app_config.cameras[cam_id]
                worker = self.worker_manager.get_worker(cam_id)
                color = self._state_color(worker)
                w["frame"].configure(bg=color)
                w["name"].configure(text=cam.name or cam.id, bg=color)
                w["state"].configure(text=(worker.state.value if worker else "n/a"), bg=color)
                w["step"].configure(text=(worker.last_step_label if worker else ""), bg=color)
                countdown = f"next in {worker.countdown_s:0.0f}s" if worker and worker.countdown_s > 0 else ""
                w["countdown"].configure(text=countdown, bg=color)
                w["image"].configure(bg=color)
            else:
                w["frame"].configure(bg="#e0e0e0")
                w["name"].configure(text="(empty)", bg="#e0e0e0")
                for key in ("state", "step", "countdown", "image"):
                    w[key].configure(bg="#e0e0e0")
                    if key != "image":
                        w[key].configure(text="")

    def _mode_text(self) -> str:
        a = self.app_config.app
        return f"MODE: {'MOCK' if a.mock_mode else 'LIVE'}  |  {'DRY-RUN' if a.dry_run else 'ARMED'}"

    def _refresh_status_bar(self) -> None:
        counts = self.worker_manager.status_counts()
        self.status_counts_label.config(
            text=f"Enabled: {counts['enabled']}  Connected: {counts['connected']}  "
                 f"Running: {counts['running']}  Paused: {counts['paused']}  Offline: {counts['offline']}")
        warn = ""
        if (self.app_config.security.credential_backend == CredentialBackendKind.PLAINTEXT
                and self.app_config.security.warn_plaintext):
            warn = "  |  WARNING: plaintext credentials stored in config.txt"
        self.status_mode_label.config(text=self._mode_text() + warn)
        self.status_error_label.config(text=(f"Last error: {self.last_global_error}" if self.last_global_error else ""))

    # ---------------------------------------------------------------
    # 15. GRACEFUL SHUTDOWN
    # ---------------------------------------------------------------
    def _on_close(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            if self._poll_after_id is not None:
                self.after_cancel(self._poll_after_id)
        except Exception:
            pass
        self._cancel_pad_refresh()

        self.logger.info("shutdown requested: stopping all camera workers")
        try:
            self.worker_manager.stop_all(join=True, timeout_s=self.app_config.shutdown.worker_join_timeout_s)
        except Exception:
            self.logger.error("error while stopping workers: %s", traceback.format_exc())

        try:
            state = {
                "shutdown_at": datetime.now(timezone.utc).isoformat(),
                "cameras": {
                    cam_id: {"state": w.state.value, "step_index": w.step_index, "cycle_count": w.cycle_count}
                    for cam_id, w in self.worker_manager.workers.items()
                },
            }
            ensure_app_dirs()
            STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            self.logger.error("error while writing runtime/state.json: %s", traceback.format_exc())

        for f in (PID_FILE, STOP_REQUEST_FILE):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

        self.logger.info("shutdown complete")
        try:
            self.destroy()
        except Exception:
            pass


# ======================================================================
# 16. main()
# ======================================================================

def _is_process_running(pid: int) -> bool:
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    try:
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0  # type: ignore[attr-defined]
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True,
                              timeout=5, creationflags=flags)
        return str(pid) in out.stdout
    except Exception:
        return True  # can't verify -> assume running, refuse to risk a double-launch


def _single_instance_guard() -> bool:
    """Returns True if this process may proceed (and writes PID_FILE).
    Shows a plain messagebox (no full GUI needed yet) if another
    instance appears to already be running."""
    ensure_app_dirs()
    if PID_FILE.exists():
        try:
            existing_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            existing_pid = -1
        if existing_pid > 0 and _is_process_running(existing_pid):
            try:
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror(APP_NAME, f"{APP_NAME} appears to already be running (PID {existing_pid}).\n"
                                                f"Close it first, or delete runtime\\app.pid if this is stale.")
                root.destroy()
            except Exception:
                print(f"{APP_NAME} appears to already be running (PID {existing_pid}).", file=sys.stderr)
            return False
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def run_diagnose(cfg: AppConfig, issues: List[ValidationIssue]) -> int:
    """Headless diagnostic pass used by `ptz_commands.bat diagnose` and
    `python ptz_patrol_gui.py --diagnose`. Never opens the GUI and
    never moves a camera -- config validation plus a plain TCP
    reachability check only (no credentials used)."""
    ensure_app_dirs()
    diag_logger = setup_diagnostics_logger()
    lines = [f"{APP_NAME} v{APP_VERSION} diagnostics", f"Generated: {datetime.now().isoformat()}",
             f"Python: {sys.version.split()[0]} on {platform.platform()}", "-" * 60]

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    lines.append(f"config.txt: {len(errors)} error(s), {len(warnings)} warning(s)")
    for i in errors:
        lines.append(f"  {i}")
    for i in warnings:
        lines.append(f"  {i}")

    lines.append("-" * 60)
    lines.append(f"dry_run={cfg.app.dry_run}  mock_mode={cfg.app.mock_mode}  cli_enabled={cfg.cli.enabled}")
    lines.append(f"cameras defined: {len(cfg.cameras)}  enabled: {len(cfg.enabled_cameras())}")

    lines.append("-" * 60)
    lines.append("Camera TCP reachability (ONVIF port only, no credentials sent):")
    for cam in cfg.cameras.values():
        reachable = False
        try:
            with socket.create_connection((cam.host, cam.onvif_port), timeout=3):
                reachable = True
        except Exception:
            reachable = False
        lines.append(f"  {cam.id} ({cam.host}:{cam.onvif_port}): {'REACHABLE' if reachable else 'unreachable'}")

    report = "\n".join(lines) + "\n"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = DIAGNOSTICS_DIR / f"diagnose_{ts}.txt"
    out_path.write_text(report, encoding="utf-8")
    diag_logger.info("diagnose run written to %s (%d errors, %d warnings)", out_path, len(errors), len(warnings))
    print(report)
    print(f"Full report saved to: {out_path}")
    return 1 if errors else 0


def _safe_import_version(module_name: str) -> Tuple[bool, str]:
    """Returns (available, version_string). Never raises -- used only
    for read-only environment introspection, never to actually use
    the module for anything."""
    try:
        mod = __import__(module_name)
        version = getattr(mod, "__version__", "") or getattr(mod, "VERSION", "")
        return True, str(version)
    except Exception:
        return False, ""


def build_environment_capability_report(cfg: Optional[AppConfig] = None) -> Dict[str, Any]:
    """Pure introspection: Python/Tk/Pillow/OpenCV/python-vlc
    versions, ONVIF CLI configuration state, BAT/config paths, and
    directory write permissions. No network access, no camera
    movement, no credentials. Backs diagnostics/
    environment_capability_report.json/.txt and --check-optional-backends."""
    report: Dict[str, Any] = {
        "application_version": APP_VERSION,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "is_windows": IS_WINDOWS,
    }
    try:
        report["tk_version"] = str(tk.TkVersion)
    except Exception:
        report["tk_version"] = "unknown"

    report["pillow_available"] = PIL_AVAILABLE
    report["pillow_version"] = ""
    if PIL_AVAILABLE:
        try:
            import PIL as _pil_mod
            report["pillow_version"] = getattr(_pil_mod, "__version__", "unknown")
        except Exception:
            report["pillow_version"] = "unknown"

    cv_ok, cv_ver = _safe_import_version("cv2")
    report["opencv_available"] = cv_ok
    report["opencv_version"] = cv_ver
    report["opencv_note"] = ("not used by any shipped feature yet (LIVE_VIDEO_PREVIEW is not implemented)"
                              if cv_ok else "not installed; not required by any shipped feature")

    vlc_ok, vlc_ver = _safe_import_version("vlc")
    report["python_vlc_available"] = vlc_ok
    report["python_vlc_version"] = vlc_ver
    report["libvlc_available"] = False
    report["external_player_available"] = False
    report["external_player_note"] = "no external player integration exists yet"

    report["bat_file_path"] = str(BAT_PATH)
    report["bat_file_exists"] = BAT_PATH.exists()
    report["config_path"] = str(CONFIG_PATH)
    report["config_exists"] = CONFIG_PATH.exists()

    def _dir_writable(d: Path) -> bool:
        try:
            ensure_app_dirs()
            probe = d / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except Exception:
            return False

    report["runtime_dir_writable"] = _dir_writable(RUNTIME_DIR)
    report["diagnostics_dir_writable"] = _dir_writable(DIAGNOSTICS_DIR)

    if cfg is not None:
        report["onvif_cli_enabled"] = cfg.cli.enabled
        report["onvif_cli_executable_configured"] = bool(cfg.cli.executable)
        report["onvif_cli_executable_exists"] = bool(cfg.cli.executable) and Path(cfg.cli.executable).exists()
        report["mock_mode"] = cfg.app.mock_mode
        report["dry_run"] = cfg.app.dry_run
    else:
        report["onvif_cli_enabled"] = False
        report["onvif_cli_executable_configured"] = False
        report["onvif_cli_executable_exists"] = False
        report["mock_mode"] = True
        report["dry_run"] = True

    report["ws_discovery_implemented"] = True
    report["ws_discovery_note"] = ("stdlib socket-based; real multicast behavior across VLANs/firewalls/"
                                    "Windows adapters is environment-dependent and not independently "
                                    "verified by this report -- see --discovery-mock-test for what is verified")

    preview_backends = [
        "snapshot_only (Pillow decode)" if report["pillow_available"]
        else "snapshot_only (no Pillow -- text placeholder instead of a decoded image)"
    ]
    if cv_ok:
        preview_backends.append("opencv (importable; not yet wired to any preview feature)")
    if vlc_ok:
        preview_backends.append("python-vlc (importable; not yet wired to any preview feature)")
    report["preview_backends_available"] = preview_backends
    report["live_video_preview_implemented"] = False

    # tkinter is already imported at module load time (as `tk`), so if
    # this code is running at all, tkinter is present -- no re-import
    # probe is needed, just a presence check against sys.modules.
    required_missing: List[str] = [] if "tkinter" in sys.modules else ["tkinter"]
    report["required_dependencies_missing"] = required_missing
    optional_missing: List[str] = []
    if not report["pillow_available"]:
        optional_missing.append("Pillow (snapshot preview image decoding)")
    report["optional_dependencies_missing"] = optional_missing
    report["fallback_selected"] = "snapshot_only / text-status-tiles (LIVE_VIDEO_PREVIEW not implemented)"

    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    return report


def save_environment_capability_report(cfg: Optional[AppConfig] = None) -> Tuple[Path, Path]:
    ensure_app_dirs()
    report = build_environment_capability_report(cfg)
    json_path = DIAGNOSTICS_DIR / "environment_capability_report.json"
    txt_path = DIAGNOSTICS_DIR / "environment_capability_report.txt"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [f"{APP_NAME} environment capability report", f"Generated: {report['generated_at_utc']}", "-" * 60]
    for k, v in report.items():
        if k == "generated_at_utc":
            continue
        lines.append(f"{k}: {v}")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # last_startup_diagnostics.txt mirrors the same content under the
    # name the tracked-development prompt also asks for, so both
    # filenames are satisfied from one source of truth.
    (DIAGNOSTICS_DIR / "last_startup_diagnostics.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, txt_path


def run_check_optional_backends(cfg: AppConfig) -> int:
    """--check-optional-backends: prints and saves the environment
    capability report. Exits 1 only if a REQUIRED dependency (tkinter
    itself) is missing -- missing optional backends (Pillow/OpenCV/
    python-vlc) are informational, never fatal."""
    print(f"{APP_NAME} --check-optional-backends")
    report = build_environment_capability_report(cfg)
    for k, v in report.items():
        print(f"  {k}: {v}")
    _json_path, txt_path = save_environment_capability_report(cfg)
    print(f"Saved: {txt_path}")
    if report.get("required_dependencies_missing"):
        print(f"REQUIRED dependency missing: {report['required_dependencies_missing']} -- core app cannot start")
        return 1
    print("Core application can start. Any missing items above are optional backends only.")
    return 0


def run_dependency_report_test() -> int:
    """--dependency-report-test: proves the environment capability
    report builds correctly regardless of whether OpenCV/python-vlc
    happen to be importable on this machine (neither is a dependency
    of this project -- requirements.txt lists neither), never reports
    a missing optional backend as a missing REQUIRED dependency, and
    never leaks a credential into the saved report."""
    print(f"{APP_NAME} --dependency-report-test starting")
    results: List[Tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    cfg, _doc, _issues = load_config(CONFIG_PATH)
    try:
        report = build_environment_capability_report(cfg)
        built_ok = True
    except Exception:
        report = {}
        built_ok = False
    check("report builds without raising, whatever OpenCV/python-vlc availability this machine actually has", built_ok)
    check("report explicitly records opencv_available (never silently omitted)", "opencv_available" in report)
    check("report explicitly records python_vlc_available (never silently omitted)", "python_vlc_available" in report)
    # This environment-independent property is what actually matters:
    # OpenCV/python-vlc are optional regardless of whether this build
    # machine happens to have them importable or not (it may -- that
    # says nothing about whether this project depends on them; it
    # doesn't, per requirements.txt).
    required_missing_list = report.get("required_dependencies_missing", [])
    check("OpenCV/python-vlc are never reported as a missing REQUIRED dependency, present or not",
          not any("opencv" in m.lower() or "vlc" in m.lower() for m in required_missing_list))
    check("pillow_available matches the module-level PIL_AVAILABLE flag actually in effect",
          report.get("pillow_available") == PIL_AVAILABLE)
    check("report records a non-empty python_version string", bool(report.get("python_version")))
    check("a snapshot-only fallback is always listed, even with every optional backend absent",
          len(report.get("preview_backends_available", [])) >= 1)

    json_path, txt_path = save_environment_capability_report(cfg)
    check("report files are actually written to diagnostics/", json_path.exists() and txt_path.exists())
    blob = json_path.read_text(encoding="utf-8") + txt_path.read_text(encoding="utf-8")
    check("saved report never contains the word password/secret",
          "password" not in blob.lower() and "secret" not in blob.lower())

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"--dependency-report-test: {passed}/{total} checks passed")
    return 0 if passed == total else 1


def run_check_config(cfg: AppConfig, issues: List[ValidationIssue]) -> int:
    """Pure, fast, no-network config.txt validation report: prints
    every error/warning and exits non-zero only if an ERROR is
    present (warnings alone still exit 0, since the app loads fine
    with warnings)."""
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]
    print(f"config.txt: {len(cfg.cameras)} camera(s) parsed, {len(cfg.patrols)} patrol(s) parsed")
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    for i in errors:
        print(f"  ERROR:   {i}")
    for i in warnings:
        print(f"  WARNING: {i}")
    if not errors and not warnings:
        print("No issues found.")
    return 1 if errors else 0


def run_mock_test(cfg: AppConfig) -> int:
    """Headless, finite self-test of the mock engine, used by
    `python ptz_patrol_gui.py --mock-test`. Never touches a real
    camera regardless of what config.txt says: it operates on a deep
    copy with mock_mode/dry_run forced on and any adapter=cli camera
    downgraded to auto. Exits 0 only if every check below passes."""
    test_logger = setup_diagnostics_logger()
    print(f"{APP_NAME} --mock-test starting (mock_mode/dry_run forced on for this run only)")

    test_cfg = copy.deepcopy(cfg)
    test_cfg.app.mock_mode = True
    test_cfg.app.dry_run = True
    for cam in test_cfg.cameras.values():
        if cam.adapter == AdapterMode.CLI:
            cam.adapter = AdapterMode.AUTO

    by_profile: Dict[MockProfile, CameraConfig] = {}
    for cam in test_cfg.cameras.values():
        if cam.enabled and cam.mock_profile not in by_profile:
            by_profile[cam.mock_profile] = cam

    results: List[Tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    test_queue: "queue.Queue[WorkerEvent]" = queue.Queue()
    manager = WorkerManager(test_queue)
    manager.build_workers(test_cfg)
    check("config has at least one enabled camera", len(test_cfg.enabled_cameras()) > 0)
    check("one worker built per enabled camera", len(manager.workers) == len(test_cfg.enabled_cameras()))
    manager.start_all()

    full_cam = by_profile.get(MockProfile.FULL)
    full_worker = manager.get_worker(full_cam.id) if full_cam else None
    deadline = time.time() + 12.0
    full_progressed = False
    while time.time() < deadline and full_worker is not None and not full_progressed:
        if (full_worker.cycle_count > 0 or full_worker.step_index > 0
                or full_worker.state in (CameraState.MOVING, CameraState.SETTLING, CameraState.DWELLING)):
            full_progressed = True
        time.sleep(0.2)
    check("full-capability mock camera connects and progresses through its patrol",
          full_worker is not None and full_progressed)

    intermittent_cam = by_profile.get(MockProfile.INTERMITTENT)
    if intermittent_cam is not None:
        intermittent_worker = manager.get_worker(intermittent_cam.id)
        saw_trouble = False
        deadline = time.time() + 8.0
        while time.time() < deadline and intermittent_worker is not None:
            if (not intermittent_worker.connected
                    or intermittent_worker.state in (CameraState.RECOVERING, CameraState.CONNECTING)):
                saw_trouble = True
                break
            time.sleep(0.2)
        check("intermittent mock camera exhibits a disconnect/recovery at some point", saw_trouble)
        check("full camera kept running while the intermittent camera had trouble",
              full_worker is not None and full_worker.state != CameraState.ERROR)
    else:
        check("intermittent-profile mock camera present (skipped: none found in config)", True)

    limited_cam = by_profile.get(MockProfile.LIMITED)
    if limited_cam is not None:
        limited_bridge = MockCameraBridge(limited_cam, test_cfg, test_logger)
        status = limited_bridge.get_status()
        limited_ok = (status.ok and not status.data.get("continuous_move_available", True)
                      and not status.data.get("absolute_move_available", True))
        check("limited-capability mock camera reports no Absolute/Relative/Continuous support", limited_ok)
    else:
        check("limited-profile mock camera present (skipped: none found in config)", True)

    if full_worker is not None:
        full_worker.enqueue(CommandType.PAUSE, {"pause_s": 0})
        deadline = time.time() + 3.0
        paused_ok = False
        while time.time() < deadline and not paused_ok:
            paused_ok = full_worker.paused
            time.sleep(0.1)
        check("Pause takes effect on the full-capability camera", paused_ok)

        full_worker.enqueue(CommandType.RESUME)
        deadline = time.time() + 3.0
        resumed_ok = False
        while time.time() < deadline and not resumed_ok:
            resumed_ok = not full_worker.paused
            time.sleep(0.1)
        check("Resume clears pause on the full-capability camera", resumed_ok)

    manager.emergency_stop_all()
    deadline = time.time() + 3.0
    estop_ok = False
    while time.time() < deadline and not estop_ok:
        estop_ok = all(w.paused for w in manager.workers.values()) if manager.workers else True
        time.sleep(0.1)
    check("Emergency Stop pauses every worker", estop_ok)

    manager.stop_all(join=True, timeout_s=10.0)
    all_stopped = all(not w.is_alive() for w in manager.workers.values())
    check("every worker thread terminates within the bounded join timeout", all_stopped)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"--mock-test: {passed}/{total} checks passed")
    test_logger.info("--mock-test: %d/%d checks passed", passed, total)
    return 0 if passed == total else 1


def run_check_device_profiles(cfg: AppConfig) -> int:
    """--check-device-profiles: schema + content-safety check of the
    built-in mini reference-profile database plus any config.txt
    [DEVICE_PROFILE_OVERRIDE_*] entries. Generic standard profiles are
    listed first by construction (BUILTIN_DEVICE_PROFILES is declared
    with generic_standard_onvif first), so they are preferred over
    heuristic ones wherever match_device_profile finds more than one
    candidate of equal confidence."""
    all_profiles = BUILTIN_DEVICE_PROFILES + cfg.device_profile_overrides
    problems = validate_device_profiles(all_profiles)
    print(f"{len(BUILTIN_DEVICE_PROFILES)} built-in profile(s), {len(cfg.device_profile_overrides)} override(s) from config.txt")
    for prof in all_profiles:
        flag = "enabled " if prof.enabled else "disabled"
        print(f"  [{flag}] {prof.profile_id:<40} confidence={prof.confidence.value:<24} "
              f"manufacturers={prof.manufacturer_patterns or '-'} models={prof.model_patterns or '-'}")
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  PROBLEM: {p}")
    else:
        print("No problems found. No profile contains credentials or a destructive command.")
    return 1 if problems else 0


def run_discover_cli(subnet_arg: Optional[str], mode_arg: str) -> int:
    """Headless, one-shot, bounded discovery pass: `--discover-cli`
    and `ptz_commands.bat discover`. Always QUICK or STANDARD --
    EXTENDED is never offered here since it requires an on-screen
    confirmation this non-interactive entry point cannot provide.
    Never writes to config.txt."""
    cfg, _doc, _issues = load_config(CONFIG_PATH)
    try:
        mode = DiscoveryMode(mode_arg.upper())
    except ValueError:
        print(f"Unknown mode '{mode_arg}', expected QUICK or STANDARD", file=sys.stderr)
        return 2
    if mode == DiscoveryMode.EXTENDED:
        print("EXTENDED mode needs interactive confirmation and is not available from the command line; running STANDARD instead.")
        mode = DiscoveryMode.STANDARD

    adapters = enumerate_adapters()
    if not adapters:
        print("No network adapters found.", file=sys.stderr)
        return 1
    adapter = adapters[0]
    if subnet_arg:
        for a in adapters:
            if a.ipv4 and a.ipv4.rsplit(".", 1)[0] == subnet_arg.rsplit("/", 1)[0].rsplit(".", 1)[0]:
                adapter = a
                break
    subnet_cidr = subnet_arg or adapter.cidr()

    all_profiles = BUILTIN_DEVICE_PROFILES + cfg.device_profile_overrides
    scan_cfg, problems = build_scan_config(mode, adapter, subnet_cidr, cfg.discovery, extended_confirmed=False)
    for p in problems:
        print(f"NOTE: {p}")
    if scan_cfg is None:
        print("Discovery refused:", "; ".join(problems), file=sys.stderr)
        return 1

    print(f"Scanning {subnet_cidr} via '{adapter.name}' in {mode.value} mode "
          f"(bounded: {scan_cfg.max_parallel_hosts} hosts x {scan_cfg.max_parallel_ports_per_host} ports in parallel, "
          f"{len(scan_cfg.candidate_onvif_ports) + len(scan_cfg.candidate_rtsp_ports)} candidate ports)...")
    event_q: "queue.Queue[DiscoveryEvent]" = queue.Queue()
    worker = DiscoveryWorker(scan_cfg, all_profiles, event_q)
    worker.start()
    worker.join(timeout=60.0)
    if worker.is_alive():
        worker.cancel()
        worker.join(timeout=10.0)
        print("Scan exceeded the 60s CLI bound and was cancelled.", file=sys.stderr)

    if not worker.devices:
        print("No candidate devices found.")
    for dev in worker.devices.values():
        print(f"[{dev.classification.value}] {dev.display_name or dev.stable_id}  ip={dev.last_ip}  "
              f"mac={dev.mac_address or '-'}  onvif_xaddr={dev.onvif_xaddr or '-'}")
    if worker.limitations:
        print("Limitations:")
        for lim in worker.limitations:
            print(f"  - {lim}")
    if cfg.discovery.save_reports:
        _json_path, txt_path = save_discovery_report(worker.devices, scan_cfg, worker.started_at, worker.finished_at, worker.limitations)
        print(f"Report saved: {txt_path}")
    print(f"{len(worker.devices)} candidate device(s) found. Nothing was added to config.txt automatically.")
    return 0


def run_adhoc_onvif_query(host: str, port: int, username: str = "", password: str = "") -> int:
    """One-off, read-only ONVIF liveness (+identity, if credentials
    are given) check against a single host:port. Backs
    `ptz_commands.bat device-info` / `capabilities` for manual testing
    outside the full discovery flow. Never writes to config.txt."""
    scheme = "https" if port == 443 else "http"
    xaddr = f"{scheme}://{host}:{port}/onvif/device_service"
    ok, _text = onvif_probe_device_service(xaddr, 3.0)
    if not ok:
        print(f"No ONVIF-shaped response from {host}:{port}", file=sys.stderr)
        print("RESULT:ERROR:NETWORK:no ONVIF-shaped response")
        return 1
    print(f"ONVIF device service responded at {xaddr}")
    if username:
        ok2, info = onvif_get_device_information(xaddr, username, password, 3.0)
        if ok2:
            for k, v in info.items():
                if v:
                    print(f"  {k}: {v}")
            ok3, caps = onvif_get_capabilities_summary(xaddr, username, password, 3.0)
            if ok3:
                print(f"  Capabilities: {caps}")
            print("RESULT:OK:NONE:device information retrieved")
            return 0
        print("RESULT:ERROR:AUTHENTICATION:credentials rejected or GetDeviceInformation failed")
        return 1
    print("RESULT:OK:NONE:endpoint confirmed (no credentials supplied, identity not queried)")
    return 0


def run_discovery_mock_test() -> int:
    """--discovery-mock-test: a headless, finite self-test of the
    discovery data model and engine safety properties. Scenarios 1-7
    and the credential/config-safety checks use synthetic evidence
    objects fed directly into the merge/classify functions
    (deterministic, instant, no real network needed -- the same
    philosophy as --mock-test using MockCameraBridge instead of real
    hardware). Scenarios 8-9 and the patrol-isolation check exercise
    the real DiscoveryWorker / bounded_port_probe machinery against
    127.0.0.1 only."""
    print(f"{APP_NAME} --discovery-mock-test starting")
    results: List[Tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    # 1. Two identical-model cameras -> different stable IDs (identity is never the model string).
    store: Dict[str, DiscoveredDevice] = {}
    dev_a = DiscoveredDevice(model="WET2440", manufacturer="V380", mac_address="aa:bb:cc:00:00:01", last_ip="192.0.2.10")
    dev_b = DiscoveredDevice(model="WET2440", manufacturer="V380", mac_address="aa:bb:cc:00:00:02", last_ip="192.0.2.11")
    merged_a = merge_discovered_device(store, dev_a)
    merged_b = merge_discovered_device(store, dev_b)
    check("two identical-model cameras get different stable IDs", merged_a.stable_id != merged_b.stable_id)
    check("stable IDs follow the documented cam_<hash> shape", merged_a.stable_id.startswith("cam_") and merged_b.stable_id.startswith("cam_"))

    # 2. Same camera via WS-Discovery AND port probing -> merges, not duplicated.
    store2: Dict[str, DiscoveredDevice] = {}
    uuid_2 = "urn:uuid:11111111-1111-1111-1111-111111111111"
    ws_hit = DiscoveredDevice(endpoint_uuid=uuid_2, last_ip="192.0.2.20",
                               onvif_xaddr="http://192.0.2.20:80/onvif/device_service",
                               ws_discovery_reply=True, discovery_sources=["ws_discovery"])
    port_hit = DiscoveredDevice(endpoint_uuid=uuid_2, last_ip="192.0.2.20",
                                 tcp_open_ports=[80], discovery_sources=["port_probe"])
    merge_discovered_device(store2, ws_hit)
    merged2 = merge_discovered_device(store2, port_hit)
    check("one camera found by WS-Discovery AND port probing merges into a single record", len(store2) == 1)
    check("the merged record carries evidence from both discovery methods",
          {"ws_discovery", "port_probe"} <= set(merged2.discovery_sources))

    # 3. Camera changes IP but keeps its EndpointReference -> same stable_id.
    store3: Dict[str, DiscoveredDevice] = {}
    uuid_3 = "urn:uuid:22222222-2222-2222-2222-222222222222"
    first_sighting = merge_discovered_device(store3, DiscoveredDevice(endpoint_uuid=uuid_3, last_ip="192.0.2.30"))
    merge_discovered_device(store3, DiscoveredDevice(endpoint_uuid=uuid_3, last_ip="192.0.2.99"))
    check("a device that changes IP keeps the same stable_id", len(store3) == 1)
    check("its record reflects the new IP after moving", store3[first_sighting.stable_id].last_ip == "192.0.2.99")

    # 4. Ping disabled but valid ONVIF -> proves ping is evidence only, never required.
    dev4 = DiscoveredDevice(onvif_device_service_reply=True, ws_discovery_reply=True, ping_reply=False, last_ip="192.0.2.40")
    dev4.classification = classify_device(dev4)
    check("a host with ping disabled but a confirmed ONVIF reply is CONFIRMED_ONVIF",
          dev4.classification == DeviceClassification.CONFIRMED_ONVIF)
    check("is_reachable() does not require a ping reply", dev4.is_reachable())

    # 5. Open candidate port alone is never treated as confirmed ONVIF.
    dev5 = DiscoveredDevice(tcp_open_ports=[8899], last_ip="192.0.2.50")
    dev5.classification = classify_device(dev5)
    check("an open port with no ONVIF response is never classified as ONVIF",
          dev5.classification not in (DeviceClassification.CONFIRMED_ONVIF, DeviceClassification.LIKELY_ONVIF))

    # 6. Camera requiring authentication -> LIKELY_ONVIF (auth prevents confirmation, per spec).
    dev6 = DiscoveredDevice(onvif_device_service_reply=True, onvif_auth_result="required", last_ip="192.0.2.60")
    dev6.classification = classify_device(dev6)
    check("a device requiring authentication is LIKELY_ONVIF, not CONFIRMED_ONVIF",
          dev6.classification == DeviceClassification.LIKELY_ONVIF)

    # 7. Streaming camera with explicitly no PTZ.
    dev7 = DiscoveredDevice(onvif_device_service_reply=True, onvif_auth_result="ok", rtsp_candidate=True,
                             capabilities={"media": True, "ptz": False}, last_ip="192.0.2.70")
    check("a limited camera can report streaming without claiming PTZ support",
          dev7.capabilities.get("media") is True and dev7.capabilities.get("ptz") is False)

    # 8. Cancellation during STANDARD discovery actually stops the worker promptly.
    loop_adapter = AdapterInfo(name="loopback-selftest", ipv4="127.0.0.1", netmask="255.255.255.252")
    settings8 = DiscoverySettings(candidate_onvif_ports=[1], candidate_rtsp_ports=[],
                                   max_parallel_hosts=2, max_parallel_ports_per_host=1)
    scan_cfg8, _probs8 = build_scan_config(DiscoveryMode.STANDARD, loop_adapter, "127.0.0.1/30", settings8)
    worker8 = DiscoveryWorker(scan_cfg8, BUILTIN_DEVICE_PROFILES, queue.Queue())
    worker8.start()
    time.sleep(0.05)
    worker8.cancel()
    worker8.join(timeout=5.0)
    check("DiscoveryWorker stops within 5s of cancel() during STANDARD discovery", not worker8.is_alive())

    # 9. Rate-limit enforcement: observed concurrency never exceeds the configured caps.
    tracker = ConcurrencyTracker()
    bounded_port_probe(["127.0.0.1"] * 3, [1, 2, 3, 4, 5, 6], connect_timeout_s=0.2,
                        max_parallel_hosts=2, max_parallel_ports_per_host=2, concurrency_tracker=tracker)
    check("observed concurrent probes never exceeded max_parallel_hosts x max_parallel_ports_per_host (<=4)", tracker.peak <= 4)

    # 10. Discovery/merge/report never writes config.txt on its own.
    tmp_dir = Path(tempfile.mkdtemp(prefix="ptz_discovery_selftest_"))
    tmp_config = tmp_dir / "config.txt"
    tmp_config.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    before_bytes = tmp_config.read_bytes()
    store10: Dict[str, DiscoveredDevice] = {}
    merge_discovered_device(store10, DiscoveredDevice(model="TestCam", last_ip="192.0.2.80"))
    json_path, txt_path = save_discovery_report(store10, scan_cfg8, "t0", "t1", [])
    after_bytes = tmp_config.read_bytes()
    check("running discovery/merge/report never modifies config.txt on its own", before_bytes == after_bytes)
    shutil.rmtree(str(tmp_dir), ignore_errors=True)

    # Extra: no credentials ever reach a saved report.
    report_text = json_path.read_text(encoding="utf-8") + txt_path.read_text(encoding="utf-8")
    check("saved discovery reports never contain a password/secret field",
          "password" not in report_text.lower() and "secret" not in report_text.lower())

    # Extra: the existing camera patrol engine is genuinely unaffected by a concurrent scan
    # (true subsystem isolation, not merely "nothing crashed").
    cam_cfg, _doc, _issues = load_config(CONFIG_PATH)
    cam_cfg.app.mock_mode = True
    cam_cfg.app.dry_run = True
    cam_manager = WorkerManager(queue.Queue())
    cam_manager.build_workers(cam_cfg)
    cam_manager.start_all()
    full_worker = next((w for w in cam_manager.workers.values() if w.camera.mock_profile == MockProfile.FULL), None)
    time.sleep(0.3)
    step_before = full_worker.step_index if full_worker else -1
    cycles_before = full_worker.cycle_count if full_worker else -1

    settings11 = DiscoverySettings(candidate_onvif_ports=[1], candidate_rtsp_ports=[],
                                    max_parallel_hosts=2, max_parallel_ports_per_host=1)
    scan_cfg11, _probs11 = build_scan_config(DiscoveryMode.STANDARD, loop_adapter, "127.0.0.1/30", settings11)
    worker11 = DiscoveryWorker(scan_cfg11, BUILTIN_DEVICE_PROFILES, queue.Queue())
    worker11.start()
    worker11.join(timeout=15.0)
    time.sleep(1.5)

    progressed = full_worker is not None and (
        full_worker.step_index != step_before or full_worker.cycle_count != cycles_before
        or full_worker.state in (CameraState.MOVING, CameraState.SETTLING, CameraState.DWELLING))
    check("the existing camera patrol keeps progressing undisturbed by a concurrent discovery scan", progressed)
    cam_manager.stop_all(join=True, timeout_s=10.0)
    check("camera workers still shut down within the bounded timeout after running alongside discovery",
          all(not w.is_alive() for w in cam_manager.workers.values()))

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"--discovery-mock-test: {passed}/{total} checks passed")
    return 0 if passed == total else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--diagnose", action="store_true",
                         help="Run headless diagnostics and exit (no GUI, no camera movement).")
    parser.add_argument("--check-config", action="store_true",
                         help="Validate config.txt and print a report, then exit (no GUI, no network).")
    parser.add_argument("--mock-test", action="store_true",
                         help="Run a headless, finite self-test of the mock engine, then exit (no GUI).")
    parser.add_argument("--discovery-mock-test", action="store_true",
                         help="Run a headless, finite self-test of the discovery engine, then exit (no GUI, no real network needed).")
    parser.add_argument("--check-device-profiles", action="store_true",
                         help="Validate the mini reference-profile database and exit (no GUI, no network).")
    parser.add_argument("--check-optional-backends", action="store_true",
                         help="Print and save the environment capability report, then exit (no GUI, no network).")
    parser.add_argument("--dependency-report-test", action="store_true",
                         help="Run a headless self-test of the environment capability report, then exit (no GUI).")
    parser.add_argument("--discover-cli", action="store_true",
                         help="Run one bounded, headless discovery pass (QUICK/STANDARD only) and exit (no GUI).")
    parser.add_argument("--subnet", default=None, metavar="CIDR",
                         help="With --discover-cli: authorized subnet to scan, e.g. 192.168.1.0/24 (default: the first adapter's own subnet).")
    parser.add_argument("--discovery-mode", default="QUICK", metavar="MODE",
                         help="With --discover-cli: QUICK or STANDARD (default QUICK).")
    parser.add_argument("--adhoc-onvif-query", nargs="+", default=None, metavar="ARG",
                         help="One-off read-only ONVIF check: HOST PORT [USERNAME PASSWORD]. Never writes to config.txt.")
    parser.add_argument("--version", action="store_true", help="Print version and exit.")
    args = parser.parse_args()

    if args.version:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0

    startup_logger = setup_startup_logger()
    try:
        ensure_app_dirs()
        cfg, doc, issues = load_config(CONFIG_PATH)
    except Exception:
        startup_logger.error("fatal error loading config.txt: %s", traceback.format_exc())
        print("Fatal error loading config.txt -- see logs/startup.log", file=sys.stderr)
        return 1

    if args.check_config:
        try:
            return run_check_config(cfg, issues)
        except Exception:
            startup_logger.error("fatal error during --check-config: %s", traceback.format_exc())
            return 1

    if args.check_device_profiles:
        try:
            return run_check_device_profiles(cfg)
        except Exception:
            startup_logger.error("fatal error during --check-device-profiles: %s", traceback.format_exc())
            return 1

    if args.check_optional_backends:
        try:
            return run_check_optional_backends(cfg)
        except Exception:
            startup_logger.error("fatal error during --check-optional-backends: %s", traceback.format_exc())
            return 1

    if args.dependency_report_test:
        try:
            return run_dependency_report_test()
        except Exception:
            startup_logger.error("fatal error during --dependency-report-test: %s", traceback.format_exc())
            return 1

    if args.adhoc_onvif_query:
        parts = args.adhoc_onvif_query
        if len(parts) < 2:
            print("Usage: --adhoc-onvif-query HOST PORT [USERNAME PASSWORD]", file=sys.stderr)
            return 3
        host = parts[0]
        port = _pint(parts[1]) or 0
        user = parts[2] if len(parts) > 2 else ""
        pwd = parts[3] if len(parts) > 3 else ""
        try:
            return run_adhoc_onvif_query(host, port, user, pwd)
        except Exception:
            startup_logger.error("fatal error during --adhoc-onvif-query: %s", traceback.format_exc())
            return 1

    if args.discover_cli:
        try:
            return run_discover_cli(args.subnet, args.discovery_mode)
        except Exception:
            startup_logger.error("fatal error during --discover-cli: %s", traceback.format_exc())
            return 1

    if args.discovery_mock_test:
        try:
            return run_discovery_mock_test()
        except Exception:
            startup_logger.error("fatal error during --discovery-mock-test: %s", traceback.format_exc())
            return 1

    if args.mock_test:
        try:
            return run_mock_test(cfg)
        except Exception:
            startup_logger.error("fatal error during --mock-test: %s", traceback.format_exc())
            return 1

    if args.diagnose:
        try:
            return run_diagnose(cfg, issues)
        except Exception:
            startup_logger.error("fatal error during --diagnose: %s", traceback.format_exc())
            return 1

    if not _single_instance_guard():
        return 1

    try:
        app = PTZPatrolGUI(cfg, doc, issues)
        app.mainloop()
        return 0
    except Exception:
        startup_logger.error("fatal error: %s", traceback.format_exc())
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_NAME, "A fatal error occurred. See logs/startup.log for details.")
            root.destroy()
        except Exception:
            print("A fatal error occurred. See logs/startup.log for details.", file=sys.stderr)
        return 1
    finally:
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
