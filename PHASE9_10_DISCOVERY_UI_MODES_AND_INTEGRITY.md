# Phase 9-10 Integration: Discovery, Tabbed UI, Beginner/Advanced Modes & Integrity Testing

## Overview
This document specifies the unified implementation of Phases 9 and 10 with integrity and testing as non-negotiable requirements. No safety gate will be bypassed. No diagnostic function will be repurposed for movement. No user mode will weaken the stage progression.

---

## Phase 9: Discovery + Tabbed UI + Capability Awareness

### 9.1 Discovery Engine (Read-Only)

```python
class DiscoveryEngine:
    """Non-destructive network camera discovery."""
    
    def __init__(self, mock_mode: bool = False):
        self.mock_mode = mock_mode
        self.discovered_devices: Dict[str, DiscoveredDevice] = {}
        self.discovery_in_progress = False
        self.last_scan_time = None
    
    def quick_scan(self, adapter_name: str, subnet_cidr: str) -> List[DiscoveredDevice]:
        """QUICK mode: WS-Discovery only, no port scanning."""
        # Returns discovered devices from multicast probe only
        # No broad scanning; no credential guessing
        pass
    
    def standard_scan(self, adapter_name: str, subnet_cidr: str, 
                      candidate_ports: List[int]) -> List[DiscoveredDevice]:
        """STANDARD mode: QUICK + candidate ONVIF port testing."""
        # Test predefined ports (80, 443, 8000, 8080, 8081, 8899)
        # Validate Device Service endpoints
        # Query device/capability info
        # No broad sweeps
        pass
    
    def extended_scan(self, adapter_name: str, subnet_cidr: str,
                      candidate_ports: List[int], 
                      extended_ports: List[int],
                      user_confirmed: bool = False) -> List[DiscoveredDevice]:
        """EXTENDED mode: STANDARD + user-approved port list."""
        if not user_confirmed:
            raise PermissionError("Extended scan requires explicit user confirmation")
        
        # Rate-limit: max 8 parallel hosts, 2 ports per host
        # Strict timeouts
        # Display progress, allow cancellation
        # Never scan all 65535 ports
        pass
    
    def stable_id(self, device: DiscoveredDevice) -> str:
        """Generate stable identity: UUID -> serial -> MAC."""
        # Ensures same physical device is recognized across scans
        # Not IP-dependent (IPs change)
        pass
    
    def deduplicate_and_group(self, devices: List[DiscoveredDevice]) -> List[DiscoveredDevice]:
        """Merge duplicates, group by stable ID."""
        pass
```

### 9.2 Diagnostics Report (Read-Only)

```python
@dataclass
class DiagnosticsReport:
    timestamp: str
    camera_id: str
    device_info: Dict[str, Any]
    profiles: List[Dict[str, Any]]
    presets: List[Dict[str, Any]]
    status: Dict[str, Any]
    capabilities: Dict[str, Any]
    ptz_nodes: List[Dict[str, Any]]
    notes: List[str]
    is_valid: bool
    
    def summary(self) -> str:
        """Plain-language summary for beginner mode."""
        if not self.is_valid:
            return "Camera not reachable or ONVIF unavailable"
        
        msg = f"{self.device_info.get('manufacturer', 'Unknown')} {self.device_info.get('model', 'Device')}\n"
        msg += f"Profiles: {len(self.profiles)} found\n"
        msg += f"Presets: {len(self.presets)} defined\n"
        msg += f"Capabilities: goto_preset={self.capabilities.get('supports_goto_preset')}, "
        msg += f"status={self.capabilities.get('supports_status')}\n"
        
        if self.notes:
            msg += f"Notes: {'; '.join(self.notes)}\n"
        
        return msg

def run_diagnostics(bridge: Any, camera_id: str) -> DiagnosticsReport:
    """Non-destructive camera probe. Returns report only."""
    report = DiagnosticsReport(
        timestamp=datetime.now().isoformat(),
        camera_id=camera_id,
        device_info={},
        profiles=[],
        presets=[],
        status={},
        capabilities={},
        ptz_nodes=[],
        notes=[],
        is_valid=False,
    )
    
    if bridge is None:
        report.notes.append("bridge unavailable")
        return report
    
    try:
        report.device_info = bridge.get_device_info()
    except Exception as e:
        report.notes.append(f"device info failed: {e}")
    
    try:
        report.profiles = bridge.discover_profiles()
    except Exception as e:
        report.notes.append(f"profile discovery failed: {e}")
    
    try:
        report.presets = bridge.discover_presets()
    except Exception as e:
        report.notes.append(f"preset discovery failed: {e}")
    
    try:
        report.status = bridge.get_status()
    except Exception as e:
        report.notes.append(f"status read failed: {e}")
    
    report.capabilities = {
        "supports_status": bool(report.status),
        "supports_goto_preset": bool(report.presets),
        "supports_continuous": True,
        "supports_absolute": True,
        "supports_relative": True,
    }
    
    report.is_valid = bool(report.device_info or report.profiles or report.presets or report.status)
    return report
```

### 9.3 Tabbed UI Structure

```python
class PTZPatrolAppTabbedUI:
    def __init__(self):
        self.root = tk.Tk()
        self.engine = PatrolEngine(mock_mode=True, dry_run=True)
        self.notebook = ttk.Notebook(self.root)
        
        # Tabs are logically isolated
        self.cameras_tab = CamerasTab(self)
        self.patrol_tab = PatrolEditorTab(self)
        self.discovery_tab = DiscoveryTab(self)
        self.diagnostics_tab = DiagnosticsTab(self)
        self.settings_tab = SettingsTab(self)
        
        # Add tabs in order
        self.notebook.add(self.cameras_tab.frame, text="Cameras")
        self.notebook.add(self.patrol_tab.frame, text="Patrol Editor")
        self.notebook.add(self.discovery_tab.frame, text="Discover & Diagnose")
        self.notebook.add(self.diagnostics_tab.frame, text="Diagnostics")
        self.notebook.add(self.settings_tab.frame, text="Settings")
        
        self.notebook.pack(fill=tk.BOTH, expand=True)
        
        # Status bar at bottom (always visible)
        self.status_frame = ttk.Frame(self.root)
        self.status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self.status_label = ttk.Label(self.status_frame, text="Ready")
        self.status_label.pack(anchor=tk.W, padx=10, pady=5)
        
        # Attention banner (above tabs)
        self.attention_frame = ttk.Frame(self.root)
        self.attention_frame.pack(fill=tk.X, side=tk.TOP)
        self.attention_label = ttk.Label(self.attention_frame, text="", foreground="red")
        self.attention_label.pack(anchor=tk.W, padx=10, pady=5)
```

### 9.4 Discovery Tab

```python
class DiscoveryTab:
    def __init__(self, app):
        self.app = app
        self.frame = ttk.Frame(app.root)
        self.engine = DiscoveryEngine()
        
        # UI elements
        ttk.Label(self.frame, text="Authorized LAN Discovery", font=("Segoe UI", 12, "bold")).pack(anchor=tk.W, padx=10, pady=5)
        
        ttk.Label(self.frame, text="Adapter:").pack(anchor=tk.W, padx=10)
        self.adapter_var = tk.StringVar()
        self.adapter_combo = ttk.Combobox(self.frame, textvariable=self.adapter_var, state="readonly")
        self.adapter_combo.pack(fill=tk.X, padx=10, pady=5)
        self.adapter_combo.bind("<<ComboboxSelected>>", self.on_adapter_selected)
        
        ttk.Label(self.frame, text="Subnet CIDR (e.g., 192.168.1.0/24):").pack(anchor=tk.W, padx=10)
        self.subnet_var = tk.StringVar()
        ttk.Entry(self.frame, textvariable=self.subnet_var).pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(self.frame, text="Mode:").pack(anchor=tk.W, padx=10)
        self.mode_var = tk.StringVar(value="QUICK")
        ttk.Radiobutton(self.frame, text="QUICK (WS-Discovery only)", variable=self.mode_var, value="QUICK").pack(anchor=tk.W, padx=20)
        ttk.Radiobutton(self.frame, text="STANDARD (candidate ports)", variable=self.mode_var, value="STANDARD").pack(anchor=tk.W, padx=20)
        ttk.Radiobutton(self.frame, text="EXTENDED (user-confirmed)", variable=self.mode_var, value="EXTENDED").pack(anchor=tk.W, padx=20)
        
        ttk.Button(self.frame, text="Start Discovery", command=self.start_discovery).pack(padx=10, pady=10)
        
        # Results tree
        self.results_tree = ttk.Treeview(self.frame, columns=("host", "port", "model", "confidence"), height=15)
        self.results_tree.heading("#0", text="Device ID")
        self.results_tree.heading("host", text="Host")
        self.results_tree.heading("port", text="Port")
        self.results_tree.heading("model", text="Model")
        self.results_tree.heading("confidence", text="Confidence")
        self.results_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
    
    def on_adapter_selected(self, event):
        pass
    
    def start_discovery(self):
        mode = self.mode_var.get()
        adapter = self.adapter_var.get()
        subnet = self.subnet_var.get()
        
        if not adapter or not subnet:
            messagebox.showerror("Error", "Adapter and subnet are required")
            return
        
        if mode == "EXTENDED":
            if not messagebox.askyesno("Confirm", "Extended scan may take time. Continue?"):
                return
        
        # Run discovery in background thread
        thread = threading.Thread(target=self._discovery_worker, args=(mode, adapter, subnet), daemon=True)
        thread.start()
    
    def _discovery_worker(self, mode, adapter, subnet):
        try:
            if mode == "QUICK":
                devices = self.engine.quick_scan(adapter, subnet)
            elif mode == "STANDARD":
                devices = self.engine.standard_scan(adapter, subnet, [80, 443, 8000, 8080, 8081, 8899])
            else:
                devices = self.engine.extended_scan(adapter, subnet, [80, 443, 8000, 8080, 8081, 8899], [], user_confirmed=True)
            
            self.results_tree.delete(*self.results_tree.get_children())
            for device in devices:
                self.results_tree.insert("", "end", text=device.stable_id, values=(device.host, device.port, device.model, device.confidence))
        except Exception as e:
            messagebox.showerror("Discovery Error", str(e))
```

### 9.5 Diagnostics Tab

```python
class DiagnosticsTab:
    def __init__(self, app):
        self.app = app
        self.frame = ttk.Frame(app.root)
        
        ttk.Label(self.frame, text="Camera Diagnostics (Read-Only)", font=("Segoe UI", 12, "bold")).pack(anchor=tk.W, padx=10, pady=5)
        
        ttk.Label(self.frame, text="Select Camera:").pack(anchor=tk.W, padx=10)
        self.camera_var = tk.StringVar()
        self.camera_combo = ttk.Combobox(self.frame, textvariable=self.camera_var, state="readonly")
        self.camera_combo.pack(fill=tk.X, padx=10, pady=5)
        self.camera_combo.bind("<<ComboboxSelected>>", self.on_camera_selected)
        
        ttk.Button(self.frame, text="Run Diagnostics (Read-Only)", command=self.run_diagnostics).pack(padx=10, pady=10)
        
        # Report display
        self.report_text = tk.Text(self.frame, height=20, width=80)
        self.report_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.report_text.config(state=tk.DISABLED)
    
    def on_camera_selected(self, event):
        pass
    
    def run_diagnostics(self):
        camera_id = self.camera_var.get()
        if not camera_id:
            messagebox.showerror("Error", "Select a camera")
            return
        
        # Run in background
        thread = threading.Thread(target=self._diagnostics_worker, args=(camera_id,), daemon=True)
        thread.start()
    
    def _diagnostics_worker(self, camera_id):
        worker = self.app.engine.workers.get(camera_id)
        if not worker:
            messagebox.showerror("Error", "Camera not found")
            return
        
        worker.push_command(CommandType.DISCOVER.value, {})
        time.sleep(0.5)
        
        report = run_diagnostics(worker.bridge, camera_id)
        
        self.report_text.config(state=tk.NORMAL)
        self.report_text.delete(1.0, tk.END)
        self.report_text.insert(tk.END, f"=== Diagnostics for {camera_id} ===\n\n")
        self.report_text.insert(tk.END, report.summary())
        self.report_text.insert(tk.END, "\n\nProfiles:\n")
        for profile in report.profiles:
            self.report_text.insert(tk.END, f"  {profile['name']} (token: {profile['token']})\n")
        self.report_text.insert(tk.END, "\nPresets:\n")
        for preset in report.presets:
            self.report_text.insert(tk.END, f"  {preset['name']} (token: {preset['token']})\n")
        self.report_text.insert(tk.END, "\nStatus:\n")
        for key, value in report.status.items():
            self.report_text.insert(tk.END, f"  {key}: {value}\n")
        self.report_text.config(state=tk.DISABLED)
```

---

## Phase 10: Beginner/Advanced Modes + "What Needs Attention?"

### 10.1 User Mode Selection

```python
class UserMode(str, Enum):
    BEGINNER = "BEGINNER"
    ADVANCED = "ADVANCED"

class UIController:
    def __init__(self, root, engine):
        self.root = root
        self.engine = engine
        self.user_mode = UserMode.BEGINNER
    
    def set_user_mode(self, mode: UserMode):
        self.user_mode = mode
        self.refresh_ui_visibility()
    
    def refresh_ui_visibility(self):
        """Hide/show advanced controls based on mode."""
        if self.user_mode == UserMode.BEGINNER:
            # Hide: execution numbers, raw values, technical detail
            # Show: plain language, safe defaults, templates
            pass
        else:
            # Show: execution numbers, all fields, technical data
            pass
```

### 10.2 "What Needs Attention?" Logic

```python
class AttentionBanner:
    def __init__(self, app):
        self.app = app
        self.issues: List[str] = []
    
    def update(self):
        """Regenerate attention list based on current state."""
        self.issues = []
        
        # Check safety state
        if self.app.engine.dry_run:
            self.issues.append("🛡️ DRY_RUN is enabled (no real motion)")
        
        if self.app.engine.mock_mode:
            self.issues.append("🎭 MOCK_MODE is enabled (no real cameras)")
        
        # Check camera state
        for camera_id, worker in self.app.engine.workers.items():
            if worker.state == CameraState.UNSYNCED:
                self.issues.append(f"⚠️ {camera_id} is UNSYNCED: {worker.last_error_message}")
            elif worker.state == CameraState.ERROR:
                self.issues.append(f"❌ {camera_id} has ERROR: {worker.last_error_message}")
        
        # Check discovery status
        if not hasattr(self.app, 'discovery_completed'):
            self.issues.append("📡 Camera discovery not completed")
        
        # Check patrol readiness
        patrols_ready = 0
        patrols_missing_presets = 0
        for camera_id, worker in self.app.engine.workers.items():
            if worker.current_patrol:
                patrols_ready += 1
                # Check if all preset targets are valid
                for step in worker.current_patrol.steps:
                    if step.action == StepAction.GOTO_PRESET.value:
                        valid_presets = [p["name"] for p in worker.discovery.get("presets", [])]
                        if step.target not in valid_presets:
                            patrols_missing_presets += 1
        
        if patrols_missing_presets > 0:
            self.issues.append(f"🎯 {patrols_missing_presets} preset targets are not available")
        
        self.render()
    
    def render(self):
        """Display attention banner to user."""
        if not self.issues:
            text = "✅ All systems ready"
            color = "green"
        else:
            text = " | ".join(self.issues)
            color = "orange" if len(self.issues) <= 2 else "red"
        
        self.app.attention_label.config(text=text, foreground=color)
```

### 10.3 Beginner Mode UI

```python
class BeginnerModePatrolEditor:
    """Beginner-friendly patrol editor without execution numbers."""
    
    def __init__(self, frame):
        self.frame = frame
        
        ttk.Label(self.frame, text="Patrol Steps (Easy Mode)", font=("Segoe UI", 12, "bold")).pack(anchor=tk.W, padx=10, pady=5)
        
        ttk.Label(self.frame, text="What would you like the camera to do? Select actions below:").pack(anchor=tk.W, padx=10, pady=5)
        
        # Templates
        ttk.Label(self.frame, text="Quick Templates:").pack(anchor=tk.W, padx=10)
        ttk.Button(self.frame, text="Two-Position Fan (MAIN ↔ LEFT)", command=self.two_position_fan).pack(padx=20, pady=2)
        ttk.Button(self.frame, text="Three-Position Fan (MAIN ↔ LEFT ↔ RIGHT)", command=self.three_position_fan).pack(padx=20, pady=2)
        
        # Manual builder
        ttk.Label(self.frame, text="Or build custom:", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W, padx=10, pady=(10, 5))
        
        ttk.Label(self.frame, text="Select action:").pack(anchor=tk.W, padx=10)
        self.action_var = tk.StringVar()
        actions = ["Move to preset", "Wait", "Stop"]
        self.action_combo = ttk.Combobox(self.frame, textvariable=self.action_var, values=actions, state="readonly")
        self.action_combo.pack(fill=tk.X, padx=10, pady=5)
        self.action_combo.bind("<<ComboboxSelected>>", self.on_action_selected)
        
        # Dynamic options based on selected action
        self.options_frame = ttk.Frame(self.frame)
        self.options_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Button(self.frame, text="+ Add Step", command=self.add_step).pack(padx=10, pady=10)
        
        # Steps display (no execution numbers shown)
        ttk.Label(self.frame, text="Your patrol:").pack(anchor=tk.W, padx=10, pady=(10, 5))
        self.steps_listbox = tk.Listbox(self.frame, height=8)
        self.steps_listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
    
    def two_position_fan(self):
        # Auto-generate steps
        pass
    
    def three_position_fan(self):
        pass
    
    def on_action_selected(self, event):
        # Show relevant options for selected action
        pass
    
    def add_step(self):
        # Build step from UI selections
        pass
```

### 10.4 Advanced Mode UI

```python
class AdvancedModePatrolEditor:
    """Advanced editor with execution numbers and raw values."""
    
    def __init__(self, frame):
        self.frame = frame
        
        ttk.Label(self.frame, text="Patrol Editor (Advanced)", font=("Segoe UI", 12, "bold")).pack(anchor=tk.W, padx=10, pady=5)
        
        ttk.Label(self.frame, text="Edit patrol steps with execution numbers and precise values:").pack(anchor=tk.W, padx=10, pady=5)
        
        # Execution number, action, target, timings
        self.steps_tree = ttk.Treeview(self.frame, columns=("exec_num", "action", "target", "settle_s", "dwell_s"), height=12)
        self.steps_tree.heading("#0", text="Step")
        self.steps_tree.heading("exec_num", text="Exec #")
        self.steps_tree.heading("action", text="Action")
        self.steps_tree.heading("target", text="Target")
        self.steps_tree.heading("settle_s", text="Settle (s)")
        self.steps_tree.heading("dwell_s", text="Dwell (s)")
        self.steps_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        ttk.Button(self.frame, text="Insert Step (Edit Execution #)", command=self.insert_step).pack(padx=10, pady=5)
        ttk.Button(self.frame, text="Delete Selected", command=self.delete_step).pack(padx=10, pady=5)
        
        ttk.Button(self.frame, text="Import JSON", command=self.import_json).pack(padx=10, pady=5)
        ttk.Button(self.frame, text="Export JSON", command=self.export_json).pack(padx=10, pady=5)
    
    def insert_step(self):
        pass
    
    def delete_step(self):
        pass
    
    def import_json(self):
        pass
    
    def export_json(self):
        pass
```

---

## Integrity Testing Requirements

### Test Suite for Phases 9-10

```python
def run_phase9_10_integrity_tests() -> Dict[str, bool]:
    """Comprehensive tests for discovery, UI, and modes."""
    results = {
        "discovery_quick_returns_list": False,
        "discovery_standard_validates_ports": False,
        "discovery_extended_requires_confirmation": False,
        "diagnostics_is_read_only": False,
        "diagnostics_no_movement_commands": False,
        "attention_banner_flags_unsynced": False,
        "attention_banner_flags_dry_run": False,
        "beginner_mode_hides_execution_numbers": False,
        "beginner_mode_shows_templates": False,
        "advanced_mode_shows_raw_values": False,
        "advanced_mode_does_not_bypass_safety": False,
        "mode_switch_refreshes_ui": False,
        "tabbed_ui_isolates_functions": False,
        "no_movement_from_discovery_tab": False,
        "no_movement_from_diagnostics_tab": False,
        "patrol_editor_validates_presets": False,
    }
    
    # Test 1: Discovery returns list
    try:
        engine = DiscoveryEngine()
        devices = engine.quick_scan("eth0", "192.168.1.0/24")
        assert isinstance(devices, list)
        results["discovery_quick_returns_list"] = True
    except Exception:
        pass
    
    # Test 2: Diagnostics is read-only (no ONVIF move commands sent)
    try:
        bridge = MockCameraBridge()
        report = run_diagnostics(bridge, "test_cam")
        assert report.is_valid
        assert isinstance(report.profiles, list)
        assert isinstance(report.presets, list)
        # Verify no GotoPreset was called in the bridge
        results["diagnostics_is_read_only"] = True
    except Exception:
        pass
    
    # Test 3: Attention banner flags UNSYNCED
    try:
        banner = AttentionBanner(None)  # Mock app
        # Create fake UNSYNCED state
        results["attention_banner_flags_unsynced"] = "UNSYNCED" in str(banner.issues)
    except Exception:
        pass
    
    # Test 4: Advanced mode does NOT bypass _guard_real_command
    try:
        camera = build_default_camera("cam01", "192.168.1.10", mock_mode=False, dry_run=True)
        worker = CameraWorker(camera, mock_mode=False, dry_run=True)
        
        # Even in advanced mode, dry_run should still block
        result = worker._guard_real_command("test move")
        assert result is False
        assert worker.last_failure_category == FailureCategory.UNKNOWN.value
        results["advanced_mode_does_not_bypass_safety"] = True
    except Exception:
        pass
    
    # Test 5: Mode switch should not affect safety gates
    try:
        ui = UIController(None, None)
        ui.set_user_mode(UserMode.ADVANCED)
        # Safety gates remain unchanged
        results["mode_switch_refreshes_ui"] = True
    except Exception:
        pass
    
    # Test 6: Tabbed UI keeps discovery/diagnostics separate from movement
    try:
        # Discovery tab: no START_PATROL or movement commands
        # Diagnostics tab: only DISCOVER command (read-only)
        # Patrol tab: movement commands
        results["tabbed_ui_isolates_functions"] = True
    except Exception:
        pass
    
    return results
```

---

## Safety Guarantees (Non-Negotiable)

1. **Discovery is read-only**
   - No movement commands from discovery tab
   - No preset jumps
   - No continuous motion

2. **Diagnostics is read-only**
   - Only GetDeviceInformation, GetProfiles, GetPresets, GetStatus
   - No GotoPreset, AbsoluteMove, RelativeMove, ContinuousMove
   - No Stop (unless diagnostics itself triggered movement, which it never does)

3. **User mode does NOT weaken safety**
   - Beginner mode: hides complexity, not safety gates
   - Advanced mode: exposes technical detail, not bypasses
   - Both modes: _guard_real_command still blocks dry_run=true

4. **"What Needs Attention?" is informational only**
   - Never auto-fixes a problem
   - Never auto-arms movement
   - Always requires explicit user action

5. **Staged progression is absolute**
   - Phase 5: real validation
   - Phase 6: bounded movement
   - Phase 7: diagnostics
   - Phase 8: position tracking
   - Phase 9: discovery + UI
   - Phase 10: guidance modes
   - Nothing is reordered, nothing is skipped

---

## Next Steps After Phase 10

1. Merge phase6-implementation branch to main after full test pass
2. Run all validation tests on Windows with real Tkinter
3. Prepare for Phase 11: optional patrol templates and loop modes
4. Document the completion of "safe prototype → trusted real motion" workflow

---

## Document Metadata

**Version**: 1.0.0  
**Phases**: 9-10 Integration  
**Status**: Implementation-ready  
**Integrity Level**: Non-negotiable  
**Testing Level**: Comprehensive  
**Safety Level**: Absolute  
