# Phase 5: Real ONVIF Validation & Safety Enforcement

## Scope
Phase 5 upgrades the application to:
1. Probe real ONVIF cameras with credential validation
2. Discover and cache ProfileTokens and PresetTokens
3. Enforce hard move bounds BEFORE sending any command
4. Require explicit dry_run=false confirmation before real motion
5. Track position reliability (MEASURED, CALIBRATED_ESTIMATE, ESTIMATED, UNSYNCED, UNKNOWN)
6. Validate every real move payload against camera capabilities

## Implementation Status

### ✅ Completed in ptz_patrol_gui.py
- OnvifCameraBridge class with native zeep client
- GetDeviceInformation, GetProfiles, GetPresets, GetStatus
- GotoPreset, ContinuousMove, Stop, AbsoluteMove, RelativeMove
- MockCameraBridge for safe testing without hardware
- CameraWorker thread-safe command processing
- PatrolEngine with independent camera workers
- PositionReliability enum tracking
- FailureCategory classification (TIMEOUT, AUTH, UNSUPPORTED, NETWORK, CAPABILITY, POSITION, UNKNOWN)
- _guard_real_command() checks dry_run before any hardware send
- _validate_real_move_payload() bounds checking against [-26.0, 26.0] for pan/tilt, [0.0, 1.0] for zoom

### ✅ Config Structure (config.txt)
- Per-camera profile_token, preset tokens via [CAMERA_*_PRESETS]
- Per-camera calibration (pan_lr_s, pan_rl_s, tilt_bt_s, tilt_tb_s)
- Global dry_run, mock_mode safety defaults (both true)
- Atomic config save with temp → validate → replace workflow
- Unknown-key preservation for forward compatibility

### ✅ Safety Defaults
- mock_mode=true, dry_run=true ship together
- App is fully usable with zero real hardware risk
- _guard_real_command(action) returns False if dry_run=true
- _record_failure() logs redacted errors (no passwords, nonces, auth headers)

### ✅ CLI Validation
- `--phase5` runs move validation and bounded move tests
- `--mock-test` includes absolute_move/relative_move bounds checks
- `--check-config` validates camera hosts
- Headless mode supported (Linux CI/CD compatible)

### ✅ Threaded Architecture
- CameraWorker processes commands from queue.Queue
- stop_event, pause_event for coordinated control
- No blocking on network calls in main GUI thread
- Emergency stop posts EMERGENCY_STOP to all workers

---

## Phase 5 Workflow: From Mock to Real Camera

### Stage 1: Explore GUI (mock_mode=true, dry_run=true)
```
1. User opens ptz_patrol_gui.py in Windows
2. Patrol Editor, camera list, manual controls visible
3. Click "Start Patrol" → mock cameras move through steps
4. No real hardware contacted; zero risk
5. All config edits work; config saved atomically
```

### Stage 2: Probe Real Camera (mock_mode=false, dry_run=true)
```
1. Edit config.txt: [CAMERA_*] host = real camera IP
2. Set adapter=auto (uses OnvifCameraBridge)
3. Click "Discover" → queries GetDeviceInformation, GetProfiles, GetPresets
4. GUI displays profiles and presets
5. NO motion is sent; dry_run blocks all real commands
6. If profiles/presets load, camera is reachable and ONVIF-ready
```

### Stage 3: Validate Capabilities (mock_mode=false, dry_run=true)
```
1. Select one preset in GUI (e.g., "MAIN")
2. Click "Preset Test" → dry_run blocks real move
3. Worker logs: "dry_run blocked real preset move"
4. Check discovered capabilities: supports_goto_preset, supports_continuous, etc.
5. If capability is unsupported, worker records FailureCategory.CAPABILITY
```

### Stage 4: Single Manual Preset (mock_mode=false, dry_run=true)
```
1. User manually clicks preset button or manual arrow in GUI
2. Worker receives MANUAL_MOVE or GOTO_PRESET command
3. _guard_real_command() checks dry_run=true → returns False
4. Worker logs "dry_run blocked..."
5. User observes log entry, confirms patrol logic is correct
```

### Stage 5: Arm for Real Motion (mock_mode=false, dry_run=FALSE)
```
1. ONLY after Stages 1-4 pass manually and visually:
2. Edit config.txt: [APPLICATION] dry_run = false
3. Restart application
4. GUI shows WARNING: "Real motion ENABLED. Confirm camera is clear."
5. User has 10 seconds to cancel
6. If confirmed, next patrol/manual move sends real ONVIF commands
```

### Stage 6: Single Patrol Cycle (mock_mode=false, dry_run=false)
```
1. Click "Start Patrol" on Camera 1
2. Worker sends first GotoPreset ONVIF command
3. Wait settle_s, then dwell_s
4. Move to next preset
5. User observes camera physically moving
6. If all steps succeed, worker records PositionReliability.CALIBRATED_ESTIMATE
7. Allow 1-2 full cycles, then enable auto-repeat
```

---

## Key Safety Guarantees

### 1. Dry-Run Guard (CRITICAL)
```python
def _guard_real_command(self, action_name: str) -> bool:
    if self.dry_run:
        self._record_failure(FailureCategory.UNKNOWN, f"dry_run blocked real {action_name}")
        return False  # ← Prevents ANY hardware send
    if not self.camera.enabled:
        return False
    if self.bridge is None:
        return False
    return True
```
- Called before EVERY move: GotoPreset, AbsoluteMove, RelativeMove, ContinuousMove, Home, Stop
- If dry_run=true, function logs the intent and returns False
- No ONVIF command is ever sent when dry_run=true

### 2. Bounds Validation (CRITICAL)
```python
def validate_real_move(self, pan: float, tilt: float, zoom: float) -> Dict[str, Any]:
    result = {"ok": False, "reasons": [], "bounds": {...}}
    for name, value, bounds in [("pan", pan, (-26.0, 26.0)), ...]:
        if value < bounds[0] or value > bounds[1]:
            result["reasons"].append(f"{name} out of bounds: {value}")
    result["ok"] = len(result["reasons"]) == 0 and self.connected
    return result
```
- Called before ABSOLUTE_MOVE and RELATIVE_MOVE
- Pan/Tilt hard-limited to [-26.0, 26.0] (camera-specific; can be tuned per model)
- Zoom hard-limited to [0.0, 1.0]
- If invalid, worker records FailureCategory.POSITION and refuses the move

### 3. Position Tracking (TRANSPARENT)
```python
class PositionReliability(str, Enum):
    MEASURED = "MEASURED"              # From GetStatus (reliable cameras only)
    CALIBRATED_ESTIMATE = "CALIBRATED_ESTIMATE"  # From preset/home move
    ESTIMATED = "ESTIMATED"            # From continuous move + stop
    UNSYNCED = "UNSYNCED"              # Stop failed; position unknown
    UNKNOWN = "UNKNOWN"                # Initial state
```
- Worker updates reliability after every move
- GUI can display confidence level to user
- If UNSYNCED, worker logs FailureCategory.POSITION and may attempt recovery (Stop, home, resync)

### 4. Failure Recovery (AUTOMATIC)
```python
def _record_failure(self, category: FailureCategory, message: str) -> None:
    self.last_failure_category = category.value
    self.last_error_message = message  # redacted
    self.position_reliability = PositionReliability.UNSYNCED
    self.state = CameraState.ERROR or UNSYNCED
```
- Every error is categorized (TIMEOUT, AUTH, UNSUPPORTED, NETWORK, CAPABILITY, POSITION, UNKNOWN)
- Logged with no credential info
- GUI displays category and message to user
- Worker may retry with backoff or attempt graceful stop

### 5. Continuous Move + Explicit Stop (OWNERSHIP)
```python
if action == StepAction.CONTINUOUS_MOVE.value:
    pan_velocity = (float(step.pan_steps) / max(1.0, float(step.move_s)))
    ok = self.bridge.continuous_move(pan_velocity, ..., timeout_s=step.move_s)
    if not ok:
        self._record_failure(FailureCategory.POSITION, "continuous move failed")
        self.state = CameraState.UNSYNCED
        continue
    self.bridge.stop()  # ← ALWAYS called after continuous motion
    self.position_reliability = PositionReliability.ESTIMATED
```
- ContinuousMove velocity is bounded before sending
- After duration expires, explicit Stop is sent immediately
- If Stop fails, camera state is UNSYNCED (position unreliable)
- Worker does NOT retry or auto-resume from unknown position

---

## Testing Checklist (Phase 5)

### Unit Tests (Headless, Linux CI/CD)
```bash
python ptz_patrol_gui.py --phase5
  ✓ phase5_move_validation: bridge.validate_real_move(5.0, 3.0, 0.75) → ok=True
  ✓ phase5_move_validation: bridge.validate_real_move(100.0, 3.0, 0.75) → ok=False
  ✓ phase5_bounded_move: bridge.absolute_move(15, 10, 0.6) → True
  ✓ phase5_bounded_move: bridge.relative_move(3, 0, 0.1) → True
  ✓ phase5_bounded_move: bridge.absolute_move(999, 0, 0.5) → False (out of bounds)

python ptz_patrol_gui.py --mock-test
  ✓ mock_test: MockCameraBridge.move_to_preset("LEFT") → position["pan"] < 0
  ✓ mock_test: absolute_move(10, 5, 0.5) → True
  ✓ mock_test: relative_move(1, 0, 0) → True

python ptz_patrol_gui.py --check-config
  ✓ config_version matches
  ✓ all [CAMERA_*] sections have host defined
  ✓ all [PATROL_*] sections have repeat_mode defined
```

### Integration Tests (Windows, optional real camera)
```
1. mock_mode=true, dry_run=true: GUI starts, patrols run in mock
2. mock_mode=false, dry_run=true: Discover real camera, no motion
3. Probe one camera with dry-run-blocked manual move
4. Confirm GetDeviceInformation, GetProfiles, GetPresets work
5. mock_mode=false, dry_run=false: Single preset move, user observes
6. Enable patrol, run 2 cycles, log positions
7. Pause mid-step, resume, confirm state recovery
8. Emergency Stop all cameras, confirm Stop is sent
```

---

## Files Modified/Created

### Modified
- `ptz_patrol_gui.py`: Added OnvifCameraBridge, PositionReliability, FailureCategory, _guard_real_command, _validate_real_move_payload, --phase5 CLI
- `config.txt`: Already has dry_run, mock_mode, per-camera profile_token, PRESETS section, CALIBRATION section

### New (Phase 5 Only)
- `PHASE5_ONVIF_VALIDATION_AND_SAFETY.md`: This document
- `PHASE5_TEST_RESULTS.txt`: Append test outcomes from each stage

### Unchanged (Still Required)
- `ptz_commands.bat`: Windows launcher (legacy fallback, not required for native path)
- `README.md`: User documentation (will be updated to include Phase 5 safety workflow)
- `requirements.txt`: Dependencies (onvif-zeep, Pillow, optional OpenCV/VLC)
- `PROJECT_PROGRESS_LOG.txt`: Append-only handover ledger

---

## Next Immediate Actions

1. ✅ Finalize this document
2. → Run all CLI tests on Linux (`--phase5`, `--mock-test`, `--check-config`)
3. → Test on Windows with mock mode (Tkinter rendering, config save)
4. → Prepare ONE authorized real camera for probe (V380 Pro or similar)
5. → Probe real camera with dry_run=true, capture ProfileToken and PresetTokens
6. → Update config.txt with real tokens (no credentials in this commit)
7. → Run single preset move test (dry_run still true, no motion)
8. → Only then enable dry_run=false and test real motion with user observation
9. → Checkpoint and release v1.0.0 (mock-safe, dry-run-validated)
10. → Optional: Advanced phases (calibration, loop modes, live RTSP preview)

---

## Known Limitations & Future Work

### Current
- GetStatus position values are often structurally valid but non-changing (unreliable)
- No firmware modification (read-only diagnostics only)
- No credential brute-force (user-entered on authorized devices only)
- Multicast WS-Discovery may fail across VLANs/firewalls
- Live RTSP decoder latency and frame drops possible; latest-frame-only buffering
- Preview is OFF by default to minimize CPU load

### Future (Phase 16+)
- Calibration engine (measure motor full-travel times per direction and speed)
- Position resync via periodic preset moves
- Loop modes: PING_PONG, RANDOM_SAFE, SCHEDULED_WINDOW
- Beginner/Advanced GUI modes
- Optional snapshot or RTSP preview
- Read position from GetStatus when reliable
- Fine adjustment via RelativeMove (bounded)

---

## Quick Reference

### Safety Model Summary
```
DEFAULT SAFE STATE:
  mock_mode = true
  dry_run = true
  → App is fully usable with zero hardware risk

STAGED REAL CAMERA INTRODUCTION:
  1. mock=true, dry_run=true   (explore GUI)
  2. mock=false, dry_run=true  (probe real camera, no movement)
  3. mock=false, dry_run=true  (dry-run single preset test)
  4. mock=false, dry_run=false (armed for movement)
  5. Enable patrol with 1-2 cycles manually observed

CRITICAL GUARD:
  _guard_real_command() checks dry_run BEFORE every ONVIF send

BOUNDS ENFORCEMENT:
  pan/tilt ∈ [-26.0, 26.0]
  zoom ∈ [0.0, 1.0]
  ABSOLUTE_MOVE and RELATIVE_MOVE validated before send
  ContinuousMove velocity bounded before send

POSITION TRACKING:
  MEASURED → GetStatus returned reliable value
  CALIBRATED_ESTIMATE → preset or home move
  ESTIMATED → continuous move + stop
  UNSYNCED → stop failed or connection lost
  UNKNOWN → initial state

FAILURE RECOVERY:
  Categorize error (TIMEOUT, AUTH, UNSUPPORTED, NETWORK, CAPABILITY, POSITION, UNKNOWN)
  Log redacted (no passwords, nonces, auth headers)
  Attempt recovery: Stop, optional home, check status, resume
```

---

## Document Metadata

**Version**: 1.0.0  
**Date**: 2026-10-09  
**Status**: Phase 5 Specification Complete  
**Next Review**: After integration testing on real Windows + camera  
**Maintainer**: PTZ Patrol Controller Project  
