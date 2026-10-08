# PTZ Patrol Controller 1.0 - Native Python ONVIF

## Architecture

    PTZ Patrol GUI -> CameraWorker -> OnvifCameraBridge -> ONVIF camera

The BAT file is no longer required for normal operation. The old CLI bridge remains only as an optional legacy fallback when a device cannot be operated through the native backend.

## Files

- `PTZ_Patrol_Controller_v1_0.py` - complete application
- `config_v1_0.txt` - safe default configuration
- `requirements_v1_0.txt` - Python dependencies
- `PROJECT_PROGRESS_LOG.txt` - append-only project history

If your upload/download workflow requires TXT, rename `PTZ_Patrol_Controller_v1_0_py.txt` to `PTZ_Patrol_Controller_v1_0.py`. Rename `config_v1_0.txt` to `config.txt` and `requirements_v1_0.txt` to `requirements.txt`.

## Installation on Windows 10/11

    py -3 -m venv venv
    venv\Scripts\activate
    py -3 -m pip install -r requirements.txt

## Safe validation before real hardware

    py -3 PTZ_Patrol_Controller_v1_0.py --version
    py -3 PTZ_Patrol_Controller_v1_0.py --check-config
    py -3 PTZ_Patrol_Controller_v1_0.py --check-device-profiles
    py -3 PTZ_Patrol_Controller_v1_0.py --discovery-mock-test
    py -3 PTZ_Patrol_Controller_v1_0.py --mock-test

The supplied config stays in `mock_mode = true` and `dry_run = true`. No camera movement is sent in that state.

## Switching one camera to native ONVIF

1. Keep `dry_run = true`.
2. Set the camera host, ONVIF port, username and password.
3. Set `adapter = onvif`, or leave `adapter = auto` and set global `mock_mode = false`.
4. Start the GUI and run Probe.
5. Verify profiles and preset tokens.
6. Test one preset while dry-run is still enabled.
7. Only then set `dry_run = false` and use Test One Cycle.

## Native commands implemented

- GetDeviceInformation and connection probe
- GetServices
- GetProfiles with automatic PTZ-capable profile selection
- GetPresets
- GetStatus including pan, tilt and zoom when supplied by the camera
- GotoPreset
- ContinuousMove with a bounded duration and explicit Stop
- Stop
- GotoHomePosition or configured home preset
- GetSnapshotUri and authenticated snapshot retrieval

## Compatibility notes

ONVIF devices vary. A camera may advertise ONVIF while omitting PTZ, GetStatus coordinates, snapshots, or some movement operations. Use the built-in capability probe and camera logs. Keep fixed cameras covering critical areas because a patrolling PTZ camera cannot observe every direction simultaneously.
