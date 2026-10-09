# Phase 8: Position Tracking & Optional Video Preview

## Purpose
Phase 8 adds the next operational layer after phase 7 diagnostics: software-based position tracking and optional preview feedback. This keeps the controller usable and understandable even when the camera does not report fully reliable PTZ status.

## Scope
This phase introduces:
1. Estimated pan/tilt/zoom position tracking
2. Calibration metadata for per-camera motion measurements
3. Optional preview/snapshot output for visual verification
4. UI overlay of camera state, reliability, countdown, and estimated position
5. Safe fallback logic when GetStatus is not reliable

## Core design

### 1) Position model
The controller must track a camera’s position in a software state object, not only the raw ONVIF response.

```python
@dataclass
class PositionState:
    pan: float = 0.0
    tilt: float = 0.0
    zoom: float = 0.0
    reliability: PositionReliability = PositionReliability.UNKNOWN
    source: str = "unknown"  # measured, calibrated, estimated, unsynced
```

This allows:
- measured values from GetStatus when available
- calibrated estimates from preset/home moves
- estimated values from bounded-pulse or relative moves
- unsynced state when stop or status confirmation fails

### 2) Calibration values
Per camera, the system should store:
- pan_lr_s
- pan_rl_s
- tilt_bt_s
- tilt_tb_s
- latency_s
- samples_per_direction
- resync_every_s
- resync_preset

These values are not proof of exact location; they are input to the position estimator.

### 3) Position reliability model
Use the same reliability model already in place:
- MEASURED
- CALIBRATED_ESTIMATE
- ESTIMATED
- UNSYNCED
- UNKNOWN

Only `MEASURED` is treated as trusted status. All others are treated as estimate-only.

### 4) Video preview (optional)
If a user enables preview, the controller may do one of:
- latest-frame-only snapshot refresh
- low-fps preview stream
- selected camera view only
- no preview if disabled

Preview mode must remain optional and never block patrol logic.

---

## Behavior rules

### Safe rule: never overstate certainty
If `GetStatus` is missing, stale, or non-changing, the app must not treat it as actual position truth.

### Safe rule: use estimated values without confidence claim
A `relative_move` or `continuous_move` may update estimated pan/tilt state only if:
- the command succeeded
- the stop succeeded
- the resulting position is still bounded and consistent

If stop fails, set `UNSYNCED` immediately.

### Safe rule: preview is non-blocking
Preview and snapshot jobs must run asynchronously and never block the worker queue or patrol engine.

---

## Implementation guidance

### A. Worker-level tracking
Each `CameraWorker` should maintain:
- `last_position: PositionState`
- `estimated_position: PositionState`
- `last_status_raw`
- `last_status_reliability`

The worker should update this state in these cases:
- after `GetStatus`
- after `GotoPreset`
- after `Home`
- after `AbsoluteMove` / `RelativeMove`
- after `ContinuousMove` + `Stop`
- when a failure occurs

### B. GUI overlay
If the GUI is used, show on each camera tile:
- camera name
- state (IDLE, MOVING, PAUSED, UNSYNCED, ERROR)
- position estimate
- reliability badge
- countdown or dwell time

### C. Preview threading
Preview should run in a separate thread or background refresh loop, not in the main patrol thread.

---

## Validation checklist for Phase 8

### Functional validation
- `GetStatus` with reliable response updates `PositionState.pan/tilt/zoom`
- preset move updates `reliability = CALIBRATED_ESTIMATE`
- continuous move + stop updates `reliability = ESTIMATED`
- failed stop sets `reliability = UNSYNCED`
- UI overlay shows position and state even with mock data
- preview disabled means no stream/waiting overhead

### Mock tests
```python
# Example validation checks
assert worker.position_reliability == PositionReliability.UNKNOWN at startup
assert worker.last_position.reliability in {UNKNOWN, ESTIMATED, CALIBRATED_ESTIMATE}
assert worker.safe_to_move() is False when state == UNSYNCED
```

---

## Safety requirement
This phase must never claim that a camera is physically at a location unless it has a trustworthy measurement or an explicit confirmed move. The system should always label status as measured vs estimated.

## Phase 8 status
Status: ready for implementation as the next stage after position-safe movement and diagnostics.

Next step: integrate position tracking into `CameraWorker`, then add optional visual preview handling in the GUI.
