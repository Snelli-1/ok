# PTZ Patrol Controller - Unified Project Synthesis

## Core Vision (from all documents)
A **Windows-native multi-camera ONVIF PTZ patrol automation system** that:
- Runs independent, reusable patrols on multiple cameras
- Recovers gracefully from network/camera failures
- Uses preset-based positioning (primary) with bounded-pulse fallbacks
- Provides real-time GUI feedback without blocking
- Discovers and diagnoses cameras on authorized subnets
- Supports calibrated position tracking and optional video verification
- Maintains complete traceability via PROJECT_PROGRESS_LOG.txt

## Unified Architecture

### Single-File GUI (ptz_patrol_gui.py)
- **Tkinter/ttk** for Windows 10/11 native look
- **Tabbed interface**: Cameras | Patrol Editor | Preview | Discovery | Diagnostics | Settings
- **Thread-safe**: All camera/network work in independent CameraWorker threads
- **Config-driven**: All state loads from/saves to config.txt

### Configuration (config.txt)
- **INI format** via Python configparser
- **Atomic saves** (temp → validate → replace)
- **Unknown-key preservation** for forward compatibility
- **Per-camera**: host, port, credentials, profile tokens, presets
- **Per-patrol**: steps with execution numbers, speeds, timeouts
- **Global**: mock mode, dry-run, discovery settings, preview, logging

### Command Bridge (ptz_commands.bat)
- Start, stop, restart, diagnose the Python application
- Optional legacy CLI fallback (disabled by default)
- Does NOT implement SOAP/WS-Security itself

### Dependencies (requirements.txt)
- **onvif-zeep** or **onvif-python**: native ONVIF (primary)
- **Pillow**: snapshot decoding (optional)
- **OpenCV** / **VLC**: live preview backends (optional)

## Feature Lineage (from all versions)

### Phase 0-1: Complete MVP Architecture ✓
- Tkinter GUI with all major panels
- Config parser and atomic save
- Mock cameras (full/intermittent/limited profiles)
- Independent CameraWorker threads
- PriorityQueue-based command serialization
- Emergency Stop

### Phase 2-3: Mock/Dry-Run Safety ✓
- Mock mode for testing without hardware
- Dry-run protection against unwanted movement
- Single-instance PID guard

### Phase 4-5: Real ONVIF + Native Adapter ✓
- Native ONVIF library (zeep-based)
- ProfileToken and PresetToken discovery & storage
- Real GotoPreset, ContinuousMove, Stop, GetStatus
- SOAP error handling with retry/backoff

### Phase 6-8: Bounded-Pulse Movement Ownership ✓
- ContinuousMove always followed by Stop
- Hard limits enforced before sending
- Estimated position counters (pan/tilt clicks)
- UNSYNCED state when Stop fails
- Interruptible waits via threading.Event

### Phase 9-10: Read-Only Diagnostics ✓
- GetSystemDateAndTime, GetDeviceInformation
- GetProfiles, GetPresets, GetStatus (as evidence)
- PTZ node enumeration, capability flags
- Separate diagnostics tab (non-destructive)

### Phase 11-13: Position Tracking & Video ✓
- Calibrated step counts (pan/tilt per degree)
- Software estimated coordinates
- Optional snapshot/RTSP preview
- Status overlay (camera name, state, position, countdown)
- Latest-frame-only video buffering

### Phase 14-15: Discovery + Tabbed UI ✓
- WS-Discovery + ARP table inspection
- Bounded host/port scanning (QUICK/STANDARD/EXTENDED)
- Stable device identity (UUID → serial → MAC)
- Duplicate merging and grouping
- Task-oriented tabs (not one crowded page)

### Phase 16: Beginner/Advanced Modes ✓
- Beginner: plain language, safe defaults, templates
- Advanced: execution numbers, raw values, technical detail
- "What Needs Attention?" guidance

### Phase 17+: Office-Fan + Loop Modes
- TWO/THREE/FOUR_POSITION_FAN templates
- PING_PONG, RANDOM_SAFE, SCHEDULED_WINDOW loop modes
- Automatic wait/speed/dwell defaults
- Per-step execution numbers (10, 20, 30...)

### Phase 18: Optional Read Position / Save View
- GetStatus-based position reading (when reliable)
- SetPreset capability-aware
- Fine adjustment via RelativeMove (bounded)
- Dry-run position tracking (no hardware send)

## Safety Model

1. **Default Safe State**
   ```
   mock_mode = true
   dry_run = true
   ```
   → App is fully usable with zero hardware risk

2. **Staged Real Camera Introduction**
   1. mock=true, dry_run=true (explore GUI)
   2. mock=false, dry_run=true (probe real camera, no movement)
   3. Capability probe and dry-run single preset test
   4. mock=false, dry_run=false (armed for movement)
   5. Enable patrol with 1-2 cycles manually observed

3. **Failure Handling**
   - Recognize error category (timeout/auth/unsupported)
   - Handle: retry with backoff, attempt recovery
   - Log: redacted (no passwords, nonces, or auth headers)
   - Recover: Stop, optional home, check status, resume

4. **Emergency Stop**
   - Posts high-priority Stop command to all workers
   - GUI thread also calls stop() directly per camera
   - Interrupts all waits (dwell, settle, backoff)
   - Works even if a camera is hung

5. **Manual Override**
   - Pause selected camera for 2/5/15 min or indefinite
   - Resume policy: restart step / restart patrol / stay paused
   - Unlocks only the paused camera, others unaffected

## Data Models (core structures)

```python
# Enums
CameraState = {DISCONNECTED, CONNECTING, IDLE, MOVING, SETTLING, 
               DWELLING, PAUSED, MANUAL_OVERRIDE, RECOVERING, 
               STOPPING, ERROR}
StepAction = {GOTO_PRESET, WAIT, CONTINUOUS_MOVE, STOP, GOTO_HOME}
RepeatMode = {OFF, ONCE, FIXED_COUNT, FOREVER, PING_PONG, RANDOM_SAFE}

# Core dataclasses
CameraConfig(id, name, host, onvif_port, rtsp_port, adapter, 
             mock_profile, username, password, profile_token, 
             patrol, home_preset, connect_timeout, command_timeout, 
             retry_count, retry_delay, backoff_initial, backoff_max)

PresetMapping = {alias: token}  # e.g. {"MAIN": "1", "LEFT": "2"}

PatrolStep(exec_num, action, target, move_s, settle_s, dwell_s, 
           speed_override, enabled)

Patrol(camera_id, repeat_mode, repeat_count, steps: list[PatrolStep])

CameraCapabilities(supports_goto_preset, supports_absolute, 
                   supports_relative, supports_continuous, supports_status,
                   preset_speed_range, pan_range, tilt_range, zoom_range)

DiscoveredDevice(stable_id, display_name, endpoint_uuid, serial, mac_address,
                 manufacturer, model, firmware, last_ip, onvif_xaddr, 
                 rtsp_port, classification, confidence, last_seen_utc)
```

## Threading Model

```
Main GUI Thread (Tkinter.root.mainloop)
  └─ root.after() polls status_queue every 100ms
  └─ User clicks → posts Command to CameraWorker.command_queue
  └─ Never blocks, never touches network

CameraWorker Thread (per camera)
  ├─ Owns CameraController instance (sole accessor)
  ├─ Owns ONVIF adapter instance (sole accessor)
  ├─ Drains command_queue (MANUAL_MOVE, PAUSE, STOP, etc.)
  ├─ Runs state machine (IDLE → MOVING → SETTLING → DWELLING → next step)
  ├─ Interruptible waits via threading.Event
  ├─ On error: categorize, log (redacted), attempt recovery
  └─ Posts status events to GUI status_queue

PatrolEngine (manages all CameraWorkers)
  ├─ Owns workers dict {camera_id: CameraWorker}
  ├─ emergency_stop() posts Stop to all + calls controller.stop() directly
  └─ Config reload: rebuild affected workers

DiscoveryEngine (optional background)
  ├─ Runs in worker thread (non-blocking)
  ├─ WS-Discovery + ARP + bounded port scan
  └─ Posts results to discovery_queue for GUI display
```

## Key Operational Concepts

### Position Tracking (Calibration)
- **Measured**: pan_lr_s, pan_rl_s, tilt_bt_s, tilt_tb_s (seconds at speed 1.0)
- **Derived**: 360° pan ≈ 52 clicks, 180° ≈ 26 clicks (example V380 Pro)
- **Stored**: per-camera, per-axis, per-speed
- **Estimated**: software tracks pan/tilt click count from Stop position
- **Resync**: periodically move to known preset to confirm position

### Bounded Movement Ownership
```
User issues ContinuousMove(pan=0.3, move_s=5):
  1. Validate pan within [-26, +26] (camera-specific hard limits)
  2. Send ONVIF ContinuousMove(velocity={pan: 0.3})
  3. Wait move_s seconds
  4. Send ONVIF Stop (CRITICAL)
  5. If Stop fails → state = UNSYNCED (position unreliable)
  6. If Stop succeeds → update estimated position
```

### Preset-First Strategy
1. Try GOTO_PRESET (most reliable)
2. Fall back to ABSOLUTE_MOVE if supported and coordinates known
3. Fall back to bounded CONTINUOUS_MOVE + Stop
4. Time-calibrated estimation only as last resort

### Execution Numbers (Operational Sequence)
```
Patrol steps use stable exec_num:
  10 GOTO_PRESET MAIN
  20 WAIT 120
  30 GOTO_PRESET LEFT
  40 WAIT 15
  # Insert new step here as 35
  35 WAIT 10
```
- GUI shows "Step 30 of 40 (exec 35 inserted)"
- Execution order is by exec_num, not display order
- Allows safe insertion/reordering without renumbering existing steps

## Discovery Modes

### QUICK
- Enumerate Windows IPv4 adapters
- User selects authorized adapter + subnet
- Send WS-Discovery probes
- Inspect Windows ARP cache
- Merge results
- No broad port scanning

### STANDARD
- Everything in QUICK +
- Test configured candidate ONVIF ports (80, 443, 8000, 8080, 8081, 8899)
- Validate Device Service endpoints
- Query device/capability info
- Generate compatibility reports

### EXTENDED
- Everything in STANDARD +
- Only with user confirmation warning
- Use user-provided or config-approved extra port list
- Rate-limit requests (max 8 parallel hosts, 2 ports per host)
- Strict timeouts
- Display progress, allow cancellation
- Never scan all 65535 ports

## Files & Ownership

| File | Owner | Responsibility |
|------|-------|-----------------|
| `ptz_patrol_gui.py` | Entire app | GUI, config, workers, ONVIF, logging, discovery, video |
| `ptz_commands.bat` | Windows launcher | Start, stop, restart, diagnose (no patrol logic) |
| `config.txt` | User + app | Cameras, patrols, settings (saved atomically) |
| `requirements.txt` | Python package mgr | Dependencies (onvif-zeep, Pillow, optional VLC/OpenCV) |
| `README.md` | User docs | Installation, usage, troubleshooting, safety |
| `PROJECT_PROGRESS_LOG.txt` | Development | Append-only handover ledger (never deleted) |
| `logs/app.log` | Runtime | Application lifecycle and errors |
| `logs/camera_*.log` | Runtime | Per-camera diagnostics and state |
| `diagnostics/` | Runtime | Discovery reports, capability dumps, environment info |
| `runtime/` | Runtime | state.json, PID file, backups, CLI cache |

## Validation & Testing

### Static (no hardware)
- `python -m py_compile ptz_patrol_gui.py`
- `python -m pyflakes ptz_patrol_gui.py`
- Config parser unit tests
- Mock-mode complete patrol test

### Headless (Windows, no GUI render)
- `python ptz_patrol_gui.py --version`
- `python ptz_patrol_gui.py --check-config`
- `python ptz_patrol_gui.py --check-device-profiles`
- `python ptz_patrol_gui.py --mock-test`
- `python ptz_patrol_gui.py --discovery-mock-test`
- `python ptz_patrol_gui.py --loop-mode-test`

### Real Camera (Windows, manual)
1. Start with `mock_mode=true, dry_run=true`
2. Run `--diagnose` to find cameras
3. Probe one camera: `--check-config --dry-run`
4. Single preset test: manually click in GUI
5. Enable patrol for 1-2 cycles, observe
6. Set `dry_run=false` only after manual confirmation

## Known Limitations & Future Work

- **V380 Pro firmware variation**: Every device tested independently
- **GetStatus position values**: Often structurally valid but non-changing (unreliable)
- **Multicast WS-Discovery**: May fail across VLANs/firewalls
- **Live RTSP**: Decoder latency and frame drops possible; latest-frame-only buffers
- **Preview optional**: Default OFF to minimize CPU load
- **Snapshot backend**: Falls back to status-only if decode fails
- **No firmware modification**: Only read-only diagnostics
- **No credential brute-force**: Only user-entered credentials on authorized devices

## Next Immediate Actions (from PROJECT_PROGRESS_LOG)

1. ✓ Architecture finalized (this document)
2. → Complete ptz_patrol_gui.py single-file implementation
3. → Finish ptz_commands.bat command dispatcher
4. → Validate config.txt parser / atomic save
5. → Run --mock-test and all headless validators
6. → Create Windows test checkpoint ZIP
7. → Prepare for optional real-camera Windows testing
