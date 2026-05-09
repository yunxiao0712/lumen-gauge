import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


DISK_PATH = "/"
NET_INTERFACES_EXCLUDE = {"lo"}


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
    total, available = memory_totals()
    if not total or available is None:
        return None, "N/A"
    used = total - available
    return 100.0 * used / total, f"{format_bytes_compact(used)}/{format_bytes_compact(total)}"


def memory_used_bytes() -> int | None:
    total, available = memory_totals()
    if not total or available is None:
        return None
    return total - available


def memory_totals() -> tuple[int | None, int | None]:
    data = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            data[key] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None, None

    total = data.get("MemTotal")
    available = data.get("MemAvailable")
    return total, available


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
