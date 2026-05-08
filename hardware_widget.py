#!/usr/bin/env python3
import os
import fcntl
import math
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango


UPDATE_MS = 1000
AUTO_COLLAPSE_MS = 900
SNAP_DISTANCE = 10
EXPANDED_SIZE = (780, 540)
COLLAPSED_SIZE = (18, 72)
LOCK_PATH = "/tmp/hardware-monitor-widget.lock"
DISK_PATH = "/"
NET_INTERFACES_EXCLUDE = {"lo"}
NET_HISTORY_LIMIT = 42
CONTENT_PADDING = 14
PANEL_GAP = 8
NETWORK_PANEL_WIDTH = 184
CONTENT_WIDTH = EXPANDED_SIZE[0] - CONTENT_PADDING * 2
HARDWARE_PANEL_WIDTH = CONTENT_WIDTH - NETWORK_PANEL_WIDTH - PANEL_GAP
METRIC_GAP = 20


@dataclass
class CpuSnapshot:
    total: int
    idle: int


@dataclass
class NetSnapshot:
    rx: int
    tx: int
    timestamp: float


def read_cpu_snapshot() -> CpuSnapshot | None:
    try:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        values = [int(value) for value in fields]
    except (OSError, IndexError, ValueError):
        return None

    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return CpuSnapshot(total=sum(values), idle=idle)


def cpu_percent(previous: CpuSnapshot | None, current: CpuSnapshot | None) -> float | None:
    if previous is None or current is None:
        return None
    total_delta = current.total - previous.total
    idle_delta = current.idle - previous.idle
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta)))


def cpu_frequency() -> str:
    paths = sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"))
    values = []
    for path in paths:
        try:
            values.append(int(path.read_text().strip()))
        except (OSError, ValueError):
            pass
    if not values:
        return "N/A"
    ghz = sum(values) / len(values) / 1_000_000
    return f"{ghz:.2f} GHz"


def memory_stats() -> tuple[float | None, str]:
    data = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            data[key] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None, "N/A"

    total = data.get("MemTotal")
    available = data.get("MemAvailable")
    if not total or available is None:
        return None, "N/A"
    used = total - available
    return 100.0 * used / total, f"{format_bytes_compact(used)}/{format_bytes_compact(total)}"


def disk_stats(show_available: bool = False) -> tuple[float | None, str]:
    try:
        stat = os.statvfs(DISK_PATH)
    except OSError:
        return None, "N/A"

    total = stat.f_blocks * stat.f_frsize
    used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
    available = stat.f_bavail * stat.f_frsize
    if total <= 0:
        return None, "N/A"
    percent_base = used + available
    percent = 100.0 * used / percent_base if percent_base > 0 else 0.0
    detail = f"可用 {format_bytes_compact(available)}" if show_available else f"{format_bytes_compact(used)} / {format_bytes_compact(total)}"
    return percent, detail


def home_disk_stats(show_available: bool = False) -> tuple[float | None, str]:
    try:
        stat = os.statvfs(str(Path.home()))
    except OSError:
        return None, "N/A"

    total = stat.f_blocks * stat.f_frsize
    used = (stat.f_blocks - stat.f_bfree) * stat.f_frsize
    available = stat.f_bavail * stat.f_frsize
    if total <= 0:
        return None, "N/A"
    percent_base = used + available
    percent = 100.0 * used / percent_base if percent_base > 0 else 0.0
    detail = f"可用 {format_bytes_compact(available)}" if show_available else f"{format_bytes_compact(used)} / {format_bytes_compact(total)}"
    return percent, detail


def battery_stats() -> tuple[float | None, str, str]:
    for battery in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        capacity_text = read_text(battery / "capacity")
        status = read_text(battery / "status") or "Unknown"
        try:
            capacity = float(capacity_text or "")
        except ValueError:
            continue
        status_map = {
            "Charging": "充电中",
            "Discharging": "使用中",
            "Full": "已充满",
            "Not charging": "未充电",
            "Unknown": "未知",
        }
        icon = "battery_ac" if status in {"Charging", "Full", "Not charging"} else "battery"
        return max(0, min(100, capacity)), status_map.get(status, status), icon
    return None, "N/A", "battery"


def read_net_snapshot() -> NetSnapshot | None:
    rx = 0
    tx = 0
    try:
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            name, values = line.split(":", 1)
            iface = name.strip()
            if iface in NET_INTERFACES_EXCLUDE:
                continue
            fields = values.split()
            rx += int(fields[0])
            tx += int(fields[8])
    except (OSError, ValueError, IndexError):
        return None
    return NetSnapshot(rx=rx, tx=tx, timestamp=time.monotonic())


def network_rate_values(previous: NetSnapshot | None, current: NetSnapshot | None) -> tuple[float | None, float | None]:
    if previous is None or current is None:
        return None, None
    elapsed = current.timestamp - previous.timestamp
    if elapsed <= 0:
        return None, None
    down = max(0, current.rx - previous.rx) / elapsed
    up = max(0, current.tx - previous.tx) / elapsed
    return down, up


def network_rates(previous: NetSnapshot | None, current: NetSnapshot | None) -> tuple[str, str]:
    down, up = network_rate_values(previous, current)
    if down is None or up is None:
        return "N/A", "N/A"
    return f"{format_bytes(down)}/s", f"{format_bytes(up)}/s"


def read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def should_show_temperature(chip: str, label: str) -> bool:
    text = f"{chip} {label}".lower()
    return any(key in text for key in ["package", "core", "nvme", "iwlwifi", "wifi", "composite", "sensor"])


def friendly_temperature_name(chip: str, label: str) -> str:
    text = f"{chip} {label}".lower()
    if "package" in text:
        return "CPU Package"
    if chip == "coretemp" and "core" in text:
        return "CPU Core Max"
    if "nvme" in text:
        return "NVMe"
    if "iwlwifi" in text or "wifi" in text:
        return "Wi-Fi"
    return label.replace("_", " ").strip() or chip


def friendly_zone_name(label: str) -> str:
    mapping = {
        "x86_pkg_temp": "CPU Package",
        "TCPU": "CPU",
        "TCPU_PCI": "CPU PCI",
        "iwlwifi_1": "Wi-Fi",
    }
    return mapping.get(label, label.replace("_", " "))


def temperature_items(gpu_temperature: str | None = None) -> list[tuple[str, float | None]]:
    readings: list[tuple[str, float | None]] = []

    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        chip = read_text(hwmon / "name") or hwmon.name
        for temp_input in sorted(hwmon.glob("temp*_input")):
            try:
                value = int(temp_input.read_text().strip()) / 1000
            except (OSError, ValueError):
                continue
            if not 0 < value < 130:
                continue
            prefix = temp_input.stem.removesuffix("_input")
            label = read_text(hwmon / f"{prefix}_label") or chip
            if should_show_temperature(chip, label):
                readings.append((friendly_temperature_name(chip, label), value))

    for path in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        try:
            value = int(path.read_text().strip()) / 1000
        except (OSError, ValueError):
            continue
        zone = path.parent
        label = read_text(zone / "type") or zone.name
        if 0 < value < 130 and label not in {"acpitz", "INT3400 Thermal"}:
            readings.append((friendly_zone_name(label), value))

    if gpu_temperature:
        match = re.search(r"(\d+)\s*C", gpu_temperature)
        readings.insert(0, ("GPU", float(match.group(1)) if match else None))

    deduped: dict[str, float | None] = {}
    for name, value in readings:
        if name not in deduped or (value is not None and (deduped[name] or 0) < value):
            deduped[name] = value

    priority = {
        "CPU Package": 0,
        "CPU Core Max": 1,
        "CPU": 2,
        "CPU PCI": 3,
        "GPU": 10,
        "NVMe": 11,
        "Wi-Fi": 12,
    }
    return sorted(deduped.items(), key=lambda item: (priority.get(item[0], 30), item[0]))[:10]


def gpu_stats() -> tuple[float | None, str, str | None]:
    if not shutil.which("nvidia-smi"):
        return None, "N/A", None
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, "N/A", None

    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    match = re.match(r"\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", line)
    if not match:
        return None, "N/A", None
    util, used, total, temp = [int(item) for item in match.groups()]
    return float(util), f"缓存 {used}/{total}MiB", f"GPU {temp} C"


def format_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_bytes_compact(value: float) -> str:
    units = ["B", "K", "M", "G", "T"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f}{unit}" if unit in {"B", "K", "M"} else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def color_for(percent: float | None) -> str:
    if percent is None:
        return "#8a8f98"
    if percent >= 85:
        return "#e25555"
    if percent >= 65:
        return "#e0a13a"
    return "#41b883"


def battery_color_for(percent: float | None) -> str:
    if percent is None:
        return "#8a8f98"
    if percent >= 70:
        return "#42d77d"
    if percent >= 35:
        return "#e5cf49"
    return "#e85b5b"


class RingMetric(Gtk.EventBox):
    def __init__(self, icon: str, title: str, on_click=None):
        super().__init__()
        self.percent: float | None = None
        self.icon = icon
        self.title_text = title
        self.on_click = on_click
        self.pulse = False
        self.value = "N/A"
        self.detail = "N/A"
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self.handle_click)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.add(self.box)
        self.image = Gtk.Image()
        self.image.set_size_request(96, 96)
        self.icon_image = Gtk.Image()
        self.icon_image.set_pixel_size(30)
        self.icon_image.set_from_file(str(write_metric_icon_svg(icon)))
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        title_row.set_halign(Gtk.Align.CENTER)
        self.title = Gtk.Label(label=title, xalign=0.5)
        self.detail_label = Gtk.Label(label="N/A", xalign=0.5)
        self.title.get_style_context().add_class("metric-title")
        self.detail_label.get_style_context().add_class("metric-value")
        self.detail_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.detail_label.set_max_width_chars(28)
        title_row.pack_start(self.icon_image, False, False, 0)
        title_row.pack_start(self.title, False, False, 0)
        self.box.pack_start(self.image, False, False, 0)
        self.box.pack_start(title_row, False, False, 0)
        self.box.pack_start(self.detail_label, False, False, 0)

    def set_metric(self, percent: float | None, value: str, detail: str | None = None) -> None:
        self.percent = percent
        self.value = value
        self.detail = detail or value
        self.render_metric(percent, value, self.detail, self.pulse)

    def set_icon(self, icon: str) -> None:
        if icon == self.icon:
            return
        self.icon = icon
        self.icon_image.set_from_file(str(write_metric_icon_svg(icon)))
        self.render_metric(self.percent, self.value, self.detail, self.pulse)

    def render_metric(self, percent: float | None, value: str, detail: str, pulse: bool = False) -> None:
        self.detail_label.set_text(self.detail)
        self.detail_label.set_text(detail)
        path = write_ring_svg(self.title_text, self.icon, percent, value, pulse)
        self.image.set_from_file(str(path))

    def handle_click(self, _widget, event) -> bool:
        if event.button == 1 and self.on_click is not None:
            self.on_click()
            return True
        return False

    def set_pulse(self, active: bool) -> None:
        self.pulse = active
        self.set_metric(self.percent, self.value, self.detail)


def svg_icon(icon: str) -> str:
    common = 'fill="none" stroke="#dfeaff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" opacity="0.92"'
    if icon == "cpu":
        pins = "".join(
            f'<line x1="{p}" y1="15" x2="{p}" y2="21" {common}/><line x1="{p}" y1="87" x2="{p}" y2="93" {common}/>'
            for p in (35, 45, 55, 65, 75)
        )
        side = "".join(
            f'<line x1="15" y1="{p}" x2="21" y2="{p}" {common}/><line x1="87" y1="{p}" x2="93" y2="{p}" {common}/>'
            for p in (35, 45, 55, 65, 75)
        )
        return f'<rect x="29" y="29" width="50" height="50" rx="9" {common}/><rect x="42" y="42" width="24" height="24" rx="5" {common}/>{pins}{side}'
    if icon == "memory":
        chips = "".join(f'<rect x="{x}" y="46" width="8" height="17" rx="2" {common}/>' for x in (36, 49, 62))
        return f'<rect x="25" y="35" width="58" height="38" rx="7" {common}/>{chips}<line x1="31" y1="78" x2="77" y2="78" {common}/>'
    if icon == "disk":
        return f'<ellipse cx="54" cy="34" rx="28" ry="10" {common}/><path d="M26 34v36c0 6 13 11 28 11s28-5 28-11V34" {common}/><path d="M26 52c0 6 13 11 28 11s28-5 28-11" {common}/>'
    if icon == "home":
        return f'<path d="M25 55 54 31l29 24" {common}/><path d="M33 53v28h42V53" {common}/><path d="M48 81V64h13v17" {common}/>'
    if icon == "gpu":
        return f'<rect x="24" y="39" width="51" height="30" rx="6" {common}/><circle cx="49" cy="54" r="10" {common}/><path d="M49 44v20M39 54h20" {common}/><path d="M75 48h10v12H75M30 75h13M55 75h13" {common}/>'
    if icon in {"battery", "battery_ac"}:
        bolt = f'<path d="M54 44 45 56h12l-5 10" {common}/>' if icon == "battery_ac" else ""
        charge_line = f'<path d="M40 54h21" {common}/>' if icon == "battery_ac" else ""
        return f'<rect x="24" y="38" width="54" height="32" rx="7" {common}/><path d="M80 48h7v12h-7" {common}/>{charge_line}{bolt}'
    return f'<circle cx="54" cy="54" r="24" {common}/>'


def write_metric_icon_svg(icon: str) -> Path:
    output_dir = Path("/tmp/hardware-monitor-widget-icons")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{icon}.svg"
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="30" height="30" viewBox="10 10 88 88">
  {svg_icon(icon)}
</svg>
"""
    path.write_text(svg)
    return path


def write_ring_svg(name: str, icon: str, percent: float | None, value: str, pulse: bool = False) -> Path:
    output_dir = Path("/tmp/hardware-monitor-widget-rings")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{icon}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', name)}"
    path = output_dir / f"{safe_name}.svg"
    pct = 0 if percent is None else max(0, min(100, percent))
    radius = 38
    cx = cy = 48
    circumference = 2 * math.pi * radius
    dash = circumference * pct / 100
    gap = circumference - dash
    color = battery_color_for(pct) if icon.startswith("battery") else color_for(pct)
    label = value if value != "N/A" else "--"
    pulse_ring = (
        f'<circle cx="{cx}" cy="{cy}" r="45" fill="none" stroke="#7dd3fc" stroke-width="2" opacity="0.65"/>'
        if pulse
        else ""
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">
  <defs>
    <filter id="glow" x="-40%" y="-40%" width="180%" height="180%">
      <feGaussianBlur stdDeviation="2.5" result="blur"/>
      <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>
  {pulse_ring}
  <circle cx="{cx}" cy="{cy}" r="{radius}" fill="rgba(255,255,255,0.04)" stroke="rgba(255,255,255,0.14)" stroke-width="9"/>
  <circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="{color}" stroke-width="9" stroke-linecap="round"
          stroke-dasharray="{dash:.2f} {gap:.2f}" transform="rotate(-90 {cx} {cy})" filter="url(#glow)"/>
  <text x="48" y="56" text-anchor="middle" font-family="Noto Sans CJK SC, Inter, sans-serif" font-size="23" font-weight="700" fill="#f7fbff">{label}</text>
  <text x="48" y="77" text-anchor="middle" font-family="Noto Sans CJK SC, Inter, sans-serif" font-size="11" fill="#aebbd0">%</text>
</svg>
"""
    path.write_text(svg)
    return path


class TemperatureBar(Gtk.Box):
    def __init__(self, name: str, temp: float | None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.set_size_request(68, -1)
        self.temp = temp
        self.image = Gtk.Image()
        self.image.set_size_request(28, 70)
        self.image.set_from_file(str(write_temperature_svg(name, temp)))
        self.name = Gtk.Label(label=name, xalign=0.5)
        self.name_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.name_area.set_size_request(-1, 30)
        self.value = Gtk.Label(label="N/A" if temp is None else f"{temp:.0f} C", xalign=0.5)
        self.name.get_style_context().add_class("temp-line")
        self.value.get_style_context().add_class("temp-value")
        self.name.set_line_wrap(True)
        self.name.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.name.set_lines(2)
        self.name.set_max_width_chars(8)
        self.name.set_justify(Gtk.Justification.CENTER)
        self.name.set_valign(Gtk.Align.CENTER)
        self.name_area.pack_start(self.name, True, True, 0)
        self.pack_start(self.image, False, False, 0)
        self.pack_start(self.name_area, False, False, 0)
        self.pack_start(self.value, False, False, 0)


def write_temperature_svg(name: str, temp: float | None) -> Path:
    output_dir = Path("/tmp/hardware-monitor-widget-temps")
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", name)
    path = output_dir / f"{safe_name}.svg"
    pct = 0 if temp is None else max(0, min(1, (temp - 25) / 75))
    fill_height = 58 * pct
    y = 64 - fill_height
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="28" height="70" viewBox="0 0 28 70">
  <defs>
    <linearGradient id="heat" x1="0" y1="64" x2="0" y2="6" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#2f78ff"/>
      <stop offset="24%" stop-color="#24d8e8"/>
      <stop offset="45%" stop-color="#3ee285"/>
      <stop offset="66%" stop-color="#f2d342"/>
      <stop offset="83%" stop-color="#ff9130"/>
      <stop offset="100%" stop-color="#f43f4b"/>
    </linearGradient>
    <clipPath id="bar"><rect x="8" y="6" width="12" height="58" rx="6"/></clipPath>
  </defs>
  <rect x="8" y="6" width="12" height="58" rx="6" fill="rgba(255,255,255,0.12)" stroke="rgba(255,255,255,0.18)"/>
  <g clip-path="url(#bar)">
    <rect x="8" y="{y:.2f}" width="12" height="{fill_height:.2f}" fill="url(#heat)"/>
  </g>
</svg>
"""
    path.write_text(svg)
    return path


class NetworkPanel(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        self.get_style_context().add_class("net-panel")
        self.title = Gtk.Label(label="网络", xalign=0)
        self.title.get_style_context().add_class("section-title")
        self.up_image = Gtk.Image()
        self.down_image = Gtk.Image()
        self.up_image.set_size_request(150, 104)
        self.down_image.set_size_request(150, 104)
        self.up_label = Gtk.Label(label="上传 N/A", xalign=0)
        self.down_label = Gtk.Label(label="下载 N/A", xalign=0)
        self.up_label.get_style_context().add_class("detail")
        self.down_label.get_style_context().add_class("detail")
        self.pack_start(self.title, False, False, 0)
        self.pack_start(self.up_label, False, False, 0)
        self.pack_start(self.up_image, False, False, 0)
        self.pack_start(self.down_label, False, False, 0)
        self.pack_start(self.down_image, False, False, 0)

    def set_network(self, down_history: list[float], up_history: list[float], down_text: str, up_text: str) -> None:
        self.up_image.set_from_file(str(write_network_chart_svg("upload", up_history, "#4f8dff")))
        self.down_image.set_from_file(str(write_network_chart_svg("download", down_history, "#43d17f")))
        self.up_label.set_text(f"上传 {up_text}")
        self.down_label.set_text(f"下载 {down_text}")


def write_network_chart_svg(name: str, history: list[float], color: str) -> Path:
    path = Path(f"/tmp/hardware-monitor-widget-network-{name}.svg")
    width = 150
    height = 104
    padding = 10
    max_value = max(history) if history else 1
    max_value = max(max_value, 1)

    padded = ([0.0] * (NET_HISTORY_LIMIT - len(history)) + history)[-NET_HISTORY_LIMIT:]
    usable_w = width - padding * 2
    usable_h = height - padding * 2
    coords = []
    for index, value in enumerate(padded):
        x = padding + usable_w * index / max(1, len(padded) - 1)
        y = height - padding - usable_h * min(1, value / max_value)
        coords.append(f"{x:.1f},{y:.1f}")
    points = " ".join(coords)

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <path d="M{padding} {height / 2} H{width - padding}" stroke="rgba(255,255,255,0.11)" stroke-width="1"/>
  <polyline points="{points}" fill="none" stroke="{color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""
    path.write_text(svg)
    return path


def write_collapsed_svg(down_history: list[float], up_history: list[float]) -> Path:
    path = Path("/tmp/hardware-monitor-widget-collapsed.svg")
    width, height = COLLAPSED_SIZE
    center = height / 2
    values = down_history + up_history
    max_value = max(values) if values else 1
    max_value = max(max_value, 1)
    up = min(1, (up_history[-1] if up_history else 0) / max_value)
    down = min(1, (down_history[-1] if down_history else 0) / max_value)
    up_h = max(4, 28 * up)
    down_h = max(4, 28 * down)
    up_color = mix_hex("#ef4444", "#3b82f6", up)
    down_color = mix_hex("#f59e0b", "#42d77d", down)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect x="0" y="0" width="{width}" height="{height}" rx="9" fill="rgba(14,20,31,0.74)"/>
  <line x1="{width / 2}" y1="{center}" x2="{width / 2}" y2="{center - up_h}" stroke="{up_color}" stroke-width="8" stroke-linecap="round" opacity="0.9"/>
  <line x1="{width / 2}" y1="{center}" x2="{width / 2}" y2="{center + down_h}" stroke="{down_color}" stroke-width="8" stroke-linecap="round" opacity="0.9"/>
  <circle cx="{width / 2}" cy="{center}" r="4" fill="#eef6ff" opacity="0.72"/>
</svg>
"""
    path.write_text(svg)
    return path


def write_background_svg(width: int, height: int) -> Path:
    path = Path("/tmp/hardware-monitor-widget-background.svg")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="rgba(14,20,31,0.74)"/>
      <stop offset="100%" stop-color="rgba(7,10,16,0.64)"/>
    </linearGradient>
  </defs>
  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="24" fill="url(#bg)" stroke="rgba(255,255,255,0.16)"/>
</svg>
"""
    path.write_text(svg)
    return path


def mix_hex(start: str, end: str, ratio: float) -> str:
    ratio = max(0, min(1, ratio))
    a = [int(start.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    b = [int(end.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    mixed = [round(a[i] + (b[i] - a[i]) * ratio) for i in range(3)]
    return "#" + "".join(f"{value:02x}" for value in mixed)


def rounded_rect(cr, x: float, y: float, width: float, height: float, radius: float) -> None:
    radius = min(radius, width / 2, height / 2)
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -1.5708, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, 1.5708)
    cr.arc(x + radius, y + height - radius, radius, 1.5708, 3.1416)
    cr.arc(x + radius, y + radius, radius, 3.1416, 4.7124)
    cr.close_path()


def hex_to_rgb(color: str) -> tuple[float, float, float]:
    color = color.lstrip("#")
    return tuple(int(color[index : index + 2], 16) / 255 for index in (0, 2, 4))


def temperature_rgb(temp: float) -> tuple[float, float, float]:
    ratio = max(0, min(1, (temp - 30) / 60))
    blue = (0.23, 0.61, 1.0)
    amber = (0.98, 0.68, 0.20)
    red = (0.96, 0.24, 0.28)
    if ratio < 0.58:
        local = ratio / 0.58
        return tuple(blue[i] + (amber[i] - blue[i]) * local for i in range(3))
    local = (ratio - 0.58) / 0.42
    return tuple(amber[i] + (red[i] - amber[i]) * local for i in range(3))


class HardwareWidget(Gtk.Window):
    def __init__(self):
        super().__init__(title="硬件监控")
        self.edge = "right"
        self.expanded = True
        self.snapped = False
        self.collapse_source_id: int | None = None
        self.last_down = "N/A"
        self.last_up = "N/A"
        self.down_history: list[float] = []
        self.up_history: list[float] = []
        self.disk_show_available = {"disk": False, "disk_home": False}
        self.memory_cleanup_source_id: int | None = None
        self.memory_pulse_source_id: int | None = None
        self.memory_pulse_count = 0
        self.memory_cleanup_start_percent = 0.0
        self.memory_cleanup_target_percent = 0.0
        self.memory_cleanup_target_detail = "N/A"
        self.memory_cleanup_current_detail = "N/A"
        self.dragging = False
        self.drag_start = (0, 0)
        self.window_start = (0, 0)

        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_default_size(*EXPANDED_SIZE)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_app_paintable(True)
        self.configure_transparency()
        self.add_events(
            Gdk.EventMask.ENTER_NOTIFY_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("destroy", Gtk.main_quit)
        self.connect("button-press-event", self.on_button_press)
        self.connect("button-release-event", self.on_button_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("enter-notify-event", self.on_enter)
        self.connect("leave-notify-event", self.on_leave)

        self.previous_cpu = read_cpu_snapshot()
        self.previous_net = read_net_snapshot()

        self.rows = {
            "cpu": RingMetric("cpu", "CPU"),
            "gpu": RingMetric("gpu", "GPU"),
            "memory": RingMetric("memory", "内存", self.clean_memory),
            "disk": RingMetric("disk", "系统盘", lambda: self.toggle_disk_detail("disk")),
            "disk_home": RingMetric("home", "主目录", lambda: self.toggle_disk_detail("disk_home")),
            "battery": RingMetric("battery", "电池"),
        }
        self.temp_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.temp_box.set_homogeneous(True)
        self.network_panel = NetworkPanel()
        self.network_panel.set_size_request(NETWORK_PANEL_WIDTH, -1)
        self.network_panel.set_vexpand(True)
        self.collapsed_image = Gtk.Image()
        self.collapsed_image.set_size_request(*COLLAPSED_SIZE)

        self.root = Gtk.Overlay()
        self.root.get_style_context().add_class("root")
        self.background_image = Gtk.Image()
        self.background_image.set_from_file(str(write_background_svg(*EXPANDED_SIZE)))
        self.root.add(self.background_image)
        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.content_box.set_border_width(CONTENT_PADDING)
        self.collapsed_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.collapsed_box.set_border_width(0)
        self.collapsed_box.get_style_context().add_class("collapsed")

        top_panel = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=PANEL_GAP)
        top_panel.set_hexpand(True)
        top_panel.set_halign(Gtk.Align.START)
        hardware_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        hardware_panel.set_size_request(HARDWARE_PANEL_WIDTH, -1)
        hardware_panel.set_halign(Gtk.Align.START)
        hardware_panel.get_style_context().add_class("hardware-panel")
        top_metrics = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=METRIC_GAP)
        bottom_metrics = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=METRIC_GAP)
        top_metrics.set_homogeneous(True)
        bottom_metrics.set_homogeneous(True)
        for key in ("cpu", "gpu", "memory"):
            top_metrics.pack_start(self.rows[key], True, True, 0)
        for key in ("disk", "disk_home", "battery"):
            bottom_metrics.pack_start(self.rows[key], True, True, 0)
        hardware_panel.pack_start(top_metrics, True, True, 0)
        hardware_panel.pack_start(bottom_metrics, True, True, 0)
        top_panel.pack_start(hardware_panel, False, False, 0)
        top_panel.pack_start(self.network_panel, False, False, 0)

        temp_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        temp_panel.set_size_request(CONTENT_WIDTH, -1)
        temp_panel.set_halign(Gtk.Align.START)
        temp_panel.get_style_context().add_class("temp-panel")
        temp_title = Gtk.Label(label="温度", xalign=0)
        temp_title.get_style_context().add_class("section-title")
        temp_panel.pack_start(temp_title, False, False, 0)
        temp_panel.pack_start(self.temp_box, True, True, 0)
        self.content_box.pack_start(top_panel, False, True, 0)
        self.content_box.pack_start(temp_panel, False, True, 0)

        self.collapsed_box.pack_start(self.collapsed_image, True, True, 0)
        self.root.add_overlay(self.content_box)
        self.root.add_overlay(self.collapsed_box)
        self.collapsed_box.hide()

        self.add(self.root)
        self.install_css()
        self.refresh()
        self.show_all()
        self.collapsed_box.hide()
        self.keep_above()
        GLib.timeout_add(UPDATE_MS, self.refresh)

    def configure_transparency(self) -> None:
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)

    def install_css(self) -> None:
        css = b"""
        window {
            background: transparent;
            border-radius: 24px;
        }
        .root {
            color: #f7fbff;
            font-family: Inter, Noto Sans CJK SC, Noto Sans, sans-serif;
            background: transparent;
            border: 0;
            border-radius: 24px;
            box-shadow: none;
        }
        .root.collapsed-root {
            background: transparent;
            border: 0;
            box-shadow: none;
        }
        .title {
            font-size: 22px;
            font-weight: 700;
        }
        .app-title {
            color: #f7fbff;
            font-size: 16px;
            font-weight: 800;
        }
        .section-title, .metric-title {
            color: #e7eef8;
            font-size: 13px;
            font-weight: 700;
        }
        .metric-value, .detail, .subtle {
            color: #b9c6d8;
            font-size: 12px;
        }
        .temp-line {
            color: #dce7f5;
            font-size: 12px;
        }
        .hardware-panel, .temp-panel, .net-panel {
            background: rgba(255, 255, 255, 0.055);
            border: 1px solid rgba(255, 255, 255, 0.11);
            border-radius: 18px;
            padding: 8px;
        }
        .net-panel {
            min-width: 166px;
        }
        .temp-value {
            color: #f0f6ff;
            font-size: 12px;
            font-weight: 700;
        }
        levelbar.temp-meter trough {
            background: rgba(255, 255, 255, 0.13);
            border-radius: 7px;
            min-width: 14px;
        }
        levelbar.temp-meter block.empty {
            background: transparent;
            border: 0;
        }
        progressbar trough {
            background: rgba(255, 255, 255, 0.16);
            border-radius: 5px;
            min-height: 9px;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        screen = Gdk.Screen.get_default()
        Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def keep_above(self) -> None:
        self.set_keep_above(True)

    def toggle_disk_detail(self, key: str) -> None:
        self.disk_show_available[key] = not self.disk_show_available[key]
        self.refresh()

    def clean_memory(self) -> None:
        if self.memory_cleanup_source_id is not None:
            GLib.source_remove(self.memory_cleanup_source_id)
        if self.memory_pulse_source_id is not None:
            GLib.source_remove(self.memory_pulse_source_id)
        current_percent = self.rows["memory"].percent or 0.0
        self.memory_cleanup_start_percent = current_percent
        self.memory_cleanup_target_percent = max(0.0, current_percent - 12.0)
        self.memory_cleanup_target_detail = self.rows["memory"].detail
        self.memory_cleanup_current_detail = "清理中..."
        self.rows["memory"].render_metric(current_percent, f"{current_percent:.0f}", self.memory_cleanup_current_detail, True)
        self.memory_pulse_count = 0
        self.memory_pulse_source_id = GLib.timeout_add(25, self.animate_memory_cleanup)
        try:
            subprocess.Popen(
                [
                    "sh",
                    "-c",
                    "sync; if command -v pkexec >/dev/null 2>&1; then echo 3 | pkexec tee /proc/sys/vm/drop_caches >/dev/null; fi",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass
        self.memory_cleanup_source_id = GLib.timeout_add(1300, self.finish_memory_cleanup)

    def animate_memory_cleanup(self) -> bool:
        self.memory_pulse_count += 1
        ramp_frames = 18
        fall_frames = 30
        if self.memory_pulse_count <= ramp_frames:
            progress = self.ease_in_out_cubic(self.memory_pulse_count / ramp_frames)
            display_percent = self.memory_cleanup_start_percent + (100.0 - self.memory_cleanup_start_percent) * progress
        else:
            progress = self.ease_in_out_cubic(min(1.0, (self.memory_pulse_count - ramp_frames) / fall_frames))
            display_percent = 100.0 + (self.memory_cleanup_target_percent - 100.0) * progress
        self.rows["memory"].render_metric(display_percent, f"{display_percent:.0f}", self.memory_cleanup_current_detail, True)
        if self.memory_pulse_count >= ramp_frames + fall_frames:
            self.memory_pulse_source_id = None
            return False
        return True

    def ease_out_cubic(self, value: float) -> float:
        value = max(0.0, min(1.0, value))
        return 1 - pow(1 - value, 3)

    def ease_in_out_cubic(self, value: float) -> float:
        value = max(0.0, min(1.0, value))
        if value < 0.5:
            return 4 * value * value * value
        return 1 - pow(-2 * value + 2, 3) / 2

    def finish_memory_cleanup(self) -> bool:
        self.memory_cleanup_source_id = None
        if self.memory_pulse_source_id is not None:
            GLib.source_remove(self.memory_pulse_source_id)
            self.memory_pulse_source_id = None
        self.rows["memory"].set_pulse(False)
        self.refresh()
        return False

    def on_button_press(self, _widget, event) -> bool:
        if event.button == 1:
            if not self.expanded:
                self.expand()
            self.dragging = True
            self.drag_start = (int(event.x_root), int(event.y_root))
            self.window_start = self.get_position()
            return True
        if event.button == 3:
            menu = Gtk.Menu()
            quit_item = Gtk.MenuItem(label="退出")
            quit_item.connect("activate", lambda _item: Gtk.main_quit())
            menu.append(quit_item)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def on_button_release(self, _widget, _event) -> bool:
        self.dragging = False
        if self.snap_if_near_edge():
            self.collapse()
        else:
            self.snapped = False
        return False

    def on_motion(self, _widget, event) -> bool:
        if not self.dragging:
            return False
        dx = int(event.x_root) - self.drag_start[0]
        dy = int(event.y_root) - self.drag_start[1]
        self.move(self.window_start[0] + dx, self.window_start[1] + dy)
        return True

    def on_enter(self, _widget, _event) -> bool:
        self.cancel_collapse()
        if self.snapped:
            self.expand()
        return False

    def on_leave(self, _widget, _event) -> bool:
        if self.snapped:
            self.schedule_collapse()
        return False

    def cancel_collapse(self) -> None:
        if self.collapse_source_id is not None:
            GLib.source_remove(self.collapse_source_id)
            self.collapse_source_id = None

    def schedule_collapse(self) -> None:
        self.cancel_collapse()
        self.collapse_source_id = GLib.timeout_add(AUTO_COLLAPSE_MS, self.collapse)

    def expand(self) -> bool:
        if self.expanded:
            self.keep_above()
            return False
        x, y = self.get_position()
        self.expanded = True
        self.root.get_style_context().remove_class("collapsed-root")
        self.background_image.show()
        self.collapsed_box.hide()
        self.content_box.show_all()
        self.resize(*EXPANDED_SIZE)
        self.move_expanded_to_edge(x, y)
        self.keep_above()
        return False

    def collapse(self) -> bool:
        self.collapse_source_id = None
        if not self.expanded or not self.snapped:
            return False
        self.expanded = False
        self.content_box.hide()
        self.background_image.hide()
        self.root.get_style_context().add_class("collapsed-root")
        self.collapsed_box.show_all()
        self.resize(*COLLAPSED_SIZE)
        self.move_collapsed_to_edge()
        self.keep_above()
        return False

    def get_monitor_geometry(self) -> Gdk.Rectangle:
        screen = self.get_screen()
        display = screen.get_display()
        monitor = display.get_monitor_at_window(self.get_window()) if self.get_window() else display.get_monitor(0)
        return monitor.get_geometry()

    def snap_if_near_edge(self, force: bool = False) -> bool:
        x, y = self.get_position()
        width, height = self.get_size()
        geo = self.get_monitor_geometry()
        distances = {
            "left": abs(x - geo.x),
            "right": abs((geo.x + geo.width) - (x + width)),
        }
        edge, distance = min(distances.items(), key=lambda item: item[1])
        if force or distance <= SNAP_DISTANCE:
            self.edge = edge
            self.snapped = True
            if self.expanded:
                self.move_expanded_to_edge(x, y)
            else:
                self.move_collapsed_to_edge()
            return True
        self.snapped = False
        return False
    def move_expanded_to_edge(self, x: int, y: int) -> None:
        geo = self.get_monitor_geometry()
        width, height = EXPANDED_SIZE
        x = min(max(x, geo.x), geo.x + geo.width - width)
        y = min(max(y, geo.y), geo.y + geo.height - height)
        if self.edge == "left":
            x = geo.x
        elif self.edge == "right":
            x = geo.x + geo.width - width
        self.move(x, y)

    def move_collapsed_to_edge(self) -> None:
        geo = self.get_monitor_geometry()
        width, height = COLLAPSED_SIZE
        x, y = self.get_position()
        x = min(max(x, geo.x), geo.x + geo.width - width)
        y = min(max(y, geo.y), geo.y + geo.height - height)
        if self.edge == "left":
            x = geo.x
        elif self.edge == "right":
            x = geo.x + geo.width - width
        self.move(x, y)

    def refresh_temperatures(self, gpu_temperature: str | None) -> None:
        for child in self.temp_box.get_children():
            self.temp_box.remove(child)
        items = temperature_items(gpu_temperature)
        if not items:
            label = Gtk.Label(label="N/A", xalign=0)
            label.get_style_context().add_class("temp-line")
            self.temp_box.pack_start(label, False, False, 0)
        for name, temp in items:
            self.temp_box.pack_start(TemperatureBar(name, temp), True, True, 0)
        self.temp_box.show_all()

    def refresh(self) -> bool:
        current_cpu = read_cpu_snapshot()
        cpu = cpu_percent(self.previous_cpu, current_cpu)
        self.previous_cpu = current_cpu
        cpu_value = "N/A" if cpu is None else f"{cpu:.0f}"
        self.rows["cpu"].set_metric(cpu, cpu_value, cpu_frequency())

        memory_percent, memory_text = memory_stats()
        memory_value = "N/A" if memory_percent is None else f"{memory_percent:.0f}"
        if self.memory_cleanup_source_id is None:
            self.rows["memory"].set_metric(memory_percent, memory_value, memory_text)
        else:
            self.memory_cleanup_target_detail = memory_text
            self.memory_cleanup_current_detail = "清理中..."
            if memory_percent is not None:
                self.memory_cleanup_target_percent = memory_percent

        disk_percent, disk_text = disk_stats(self.disk_show_available["disk"])
        disk_value = "N/A" if disk_percent is None else f"{disk_percent:.0f}"
        self.rows["disk"].set_metric(disk_percent, disk_value, disk_text)

        home_percent, home_text = home_disk_stats(self.disk_show_available["disk_home"])
        home_value = "N/A" if home_percent is None else f"{home_percent:.0f}"
        self.rows["disk_home"].set_metric(home_percent, home_value, home_text)

        gpu_percent, gpu_text, gpu_temperature = gpu_stats()
        gpu_value = "N/A" if gpu_percent is None else f"{gpu_percent:.0f}"
        self.rows["gpu"].set_metric(gpu_percent, gpu_value, gpu_text)

        battery_percent, battery_text, battery_icon = battery_stats()
        self.rows["battery"].set_icon(battery_icon)
        battery_value = "N/A" if battery_percent is None else f"{battery_percent:.0f}"
        self.rows["battery"].set_metric(battery_percent, battery_value, battery_text)

        current_net = read_net_snapshot()
        down_value, up_value = network_rate_values(self.previous_net, current_net)
        if down_value is None or up_value is None:
            self.last_down, self.last_up = "N/A", "N/A"
        else:
            self.down_history = (self.down_history + [down_value])[-NET_HISTORY_LIMIT:]
            self.up_history = (self.up_history + [up_value])[-NET_HISTORY_LIMIT:]
            self.last_down = f"{format_bytes(down_value)}/s"
            self.last_up = f"{format_bytes(up_value)}/s"
        self.previous_net = current_net
        self.network_panel.set_network(self.down_history, self.up_history, self.last_down, self.last_up)
        self.collapsed_image.set_from_file(str(write_collapsed_svg(self.down_history, self.up_history)))
        self.refresh_temperatures(gpu_temperature)
        self.keep_above()
        return True


def main() -> int:
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("硬件监控已经在运行。")
        return 0

    HardwareWidget()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
