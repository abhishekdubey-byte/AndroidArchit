from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

class DisplayInfo(BaseModel):
    width: int = 1080
    height: int = 2400
    density: int = 480

class BatteryInfo(BaseModel):
    level: int = 100
    charging: bool = False
    temperature: float = 25.0
    voltage: float = 4.0

class StorageInfo(BaseModel):
    total_gb: float = 0.0
    used_gb: float = 0.0
    free_gb: float = 0.0
    percent: int = 0

class NetworkInfo(BaseModel):
    type: str = "wifi"
    connected: bool = True
    ip: Optional[str] = "127.0.0.1"

class AppInfo(BaseModel):
    package_name: str
    app_name: str
    is_system: bool = False
    icon: Optional[str] = None

class DeviceErrorReport(BaseModel):
    device_id: str
    timestamp: float
    module: str
    operation: str
    error_code: str
    description: str
    suggested_resolution: str
    device_info: Dict[str, Any] = Field(default_factory=dict)
    stack_trace: Optional[str] = None

class Device(BaseModel):
    id: str
    serial: str
    name: str
    model: str
    brand: str
    manufacturer: str = "Unknown"
    android_version: str = "13"
    sdk_int: int = 33
    display: DisplayInfo = Field(default_factory=DisplayInfo)
    battery: BatteryInfo = Field(default_factory=BatteryInfo)
    storage: StorageInfo = Field(default_factory=StorageInfo)
    network: NetworkInfo = Field(default_factory=NetworkInfo)
    transport: str = "adb"  # "adb" or "companion_ws"
    relay_id: Optional[str] = None  # companion app id (links USB entry and remote connection)
    relay_online: bool = False
    app_edition: str = "full"  # "lite" = companion without Accessibility (no remote screen/taps)
    device_owner: bool = False  # companion app is Device Owner (silent install/uninstall), as reported by the app
    status: str = "online"  # "online", "busy", "offline"
    active_package: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    hardware_capabilities: Dict[str, Any] = Field(default_factory=dict)
    permissions: Dict[str, Any] = Field(default_factory=dict)
    power_and_lockscreen: Dict[str, Any] = Field(default_factory=dict)
    diagnostic_history: List[Dict[str, Any]] = Field(default_factory=list)
    last_seen: float = 0.0

class TouchAction(BaseModel):
    action: str = "tap"  # tap, swipe, long_press
    x: int
    y: int
    x2: Optional[int] = None
    y2: Optional[int] = None
    duration_ms: Optional[int] = 300

class KeyAction(BaseModel):
    key: str  # BACK, HOME, RECENTS, POWER, ENTER, VOLUME_UP, VOLUME_DOWN

class TextAction(BaseModel):
    text: str

class LaunchAppAction(BaseModel):
    # Android package names only: this value reaches `adb shell` (never let shell syntax through)
    package_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$", max_length=255)

class UINode(BaseModel):
    index: int
    text: str
    resource_id: str
    class_name: str
    package: str
    content_desc: str
    clickable: bool
    selected: Optional[bool] = None   # e.g. a liked WhatsApp status heart; None = the reader didn't report it
    checked: Optional[bool] = None
    bounds: List[int]  # [x1, y1, x2, y2]
    children: List['UINode'] = Field(default_factory=list)

class TaskStep(BaseModel):
    id: int
    description: str
    status: str = "pending"  # pending, executing, done, failed, waiting_approval
    action: Optional[str] = None
    result: Optional[str] = None

class Task(BaseModel):
    id: str
    instruction: str
    target_device_id: str
    status: str = "pending"  # pending, executing, waiting_approval, completed, failed
    steps: List[TaskStep] = Field(default_factory=list)
    approval_request: Optional[Dict[str, Any]] = None
    created_at: float
    completed_at: Optional[float] = None
    result_summary: Optional[str] = None

class ApprovalDecision(BaseModel):
    decision: str  # "approved", "rejected", "modified"
    modified_text: Optional[str] = None
