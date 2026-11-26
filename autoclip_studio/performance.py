"""Adaptive performance profiles, hardware monitoring, thermal guard, and Gaming Mode."""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

from .platform_utils import detect_platform

try:
    import psutil
except ImportError:  # pragma: no cover - optional fallback
    psutil = None  # type: ignore[assignment]

try:
    import pynvml
except ImportError:  # pragma: no cover - optional fallback
    pynvml = None  # type: ignore[assignment]


PROFILES: dict[str, str] = {
    "eco": "Eco — Lower CPU threads, below-normal background priority, conservative memory & tracking.",
    "balanced": "Balanced — Adaptive CPU/VRAM allocation with normal priority (Recommended).",
    "turbo": "Turbo — Maximum validated threads, batch sizes, and faster NVENC preset within hardware limits.",
}

_BELOW_NORMAL_PRIORITY_CLASS = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
_NORMAL_PRIORITY_CLASS = getattr(subprocess, "NORMAL_PRIORITY_CLASS", 0x00000020)


@dataclass(frozen=True, slots=True)
class PerformanceSettings:
    profile: str = "balanced"
    thermal_protection: bool = True
    gpu_temp_limit_c: int = 83
    gpu_temp_resume_c: int = 75
    gaming_mode: bool = False
    gaming_process: str = "TslGame.exe"


@dataclass(frozen=True, slots=True)
class HardwareSnapshot:
    cpu_percent: float | None = None
    cpu_physical: int = 1
    cpu_logical: int = 1
    ram_total_mib: int | None = None
    ram_used_mib: int | None = None
    ram_available_mib: int | None = None
    gpu_name: str | None = None
    gpu_util_percent: int | None = None
    vram_total_mib: int | None = None
    vram_used_mib: int | None = None
    vram_free_mib: int | None = None
    gpu_temp_c: int | None = None
    gpu_power_w: float | None = None
    cpu_temp_c: float | None = None
    gaming_active: bool = False
    unified_memory: bool = False
    gpu_backend: str = "none"
    cpu_perf_cores: int | None = None
    cpu_eff_cores: int | None = None
    gpu_cores: int | None = None
    swap_total_mib: int | None = None
    swap_used_mib: int | None = None
    memory_pressure: str | None = None
    memory_pressure_level: str = "normal"   # normal | warning | critical
    thermal_pressure: str | None = None     # Nominal | Fair | Serious | Critical
    battery_percent: int | None = None
    battery_charging: bool | None = None
    power_source: str | None = None         # AC Power | Battery Power
    rosetta_active: bool = False
    accel_backends: tuple[str, ...] = ()


def _sysctl_int(name: str) -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=2).stdout.strip()
        return int(out) if out.isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


@lru_cache(maxsize=1)
def _apple_static_hardware() -> tuple[int | None, int | None, int | None]:
    """Read static Apple Silicon P-core, E-core, and GPU core counts once (zero sudo)."""
    if sys.platform != "darwin":
        return None, None, None
    p_cores = _sysctl_int("hw.perflevel0.physicalcpu")
    e_cores = _sysctl_int("hw.perflevel1.physicalcpu")
    gpu_cores = None
    try:
        out = subprocess.run(
            ["ioreg", "-rc", "AGXAccelerator"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        match = re.search(r'"gpu-core-count"\s*=\s*(\d+)', out)
        if match:
            gpu_cores = int(match.group(1))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return p_cores, e_cores, gpu_cores


def parse_memory_pressure_output(output: str) -> tuple[str | None, str]:
    """Parse `memory_pressure -Q` output into (display_label, severity_level)."""
    match = re.search(r"memory free percentage:\s*(\d+)%", output, re.IGNORECASE)
    if not match:
        return None, "normal"
    free_pct = int(match.group(1))
    if free_pct <= 12:
        return f"Critical ({free_pct}% free)", "critical"
    if free_pct <= 25:
        return f"Warning ({free_pct}% free)", "warning"
    return f"Nominal ({free_pct}% free)", "normal"


def parse_pmset_battery_output(output: str) -> tuple[str | None, int | None, bool | None]:
    """Parse `pmset -g batt` output into (power_source, battery_percent, is_charging)."""
    power_source = None
    if "AC Power" in output:
        power_source = "AC Power"
    elif "Battery Power" in output:
        power_source = "Battery Power"
    pct_match = re.search(r"(\d{1,3})%", output)
    pct = int(pct_match.group(1)) if pct_match else None
    charging = None
    lowered = output.lower()
    if "discharging" in lowered:
        charging = False
    elif "charging" in lowered or "charged" in lowered or "finishing charge" in lowered:
        charging = True
    return power_source, pct, charging


def _read_macos_thermal_state() -> str | None:
    """Read NSProcessInfo.processInfo.thermalState via Objective-C runtime (zero subprocess, zero sudo)."""
    if sys.platform != "darwin":
        return None
    try:
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        msg_ptr = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
        msg_int = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
        cls = objc.objc_getClass(b"NSProcessInfo")
        if not cls:
            return None
        info = msg_ptr(cls, objc.sel_registerName(b"processInfo"))
        if not info:
            return None
        state = int(msg_int(info, objc.sel_registerName(b"thermalState")))
        return {0: "Nominal", 1: "Fair", 2: "Serious", 3: "Critical"}.get(state)
    except Exception:
        return None


@lru_cache(maxsize=1)
def detect_acceleration_backends() -> tuple[str, ...]:
    """Detect available hardware acceleration backends once without fabricating support."""
    pinfo = detect_platform()
    backends: list[str] = []
    if pinfo.is_macos:
        try:
            from .speech_engine import mlx_whisper_available
            if mlx_whisper_available():
                backends.append("MLX Metal")
        except Exception:
            pass
        try:
            from .process import ProcessManager
            from .rendering import hevc_videotoolbox_available, videotoolbox_available
            mgr = ProcessManager()
            if videotoolbox_available(mgr):
                backends.append("VideoToolbox H.264")
            if hevc_videotoolbox_available(mgr):
                backends.append("VideoToolbox HEVC")
        except Exception:
            pass
    else:
        try:
            from .speech_engine import python_cuda
            if python_cuda():
                backends.append("CUDA")
        except Exception:
            pass
        try:
            from .process import ProcessManager
            from .rendering import nvenc_available
            if nvenc_available(ProcessManager()):
                backends.append("NVENC H.264")
        except Exception:
            pass
    return tuple(backends)


@dataclass(frozen=True, slots=True)
class ProfileLimits:
    profile: str
    cpu_threads: int
    ffmpeg_threads: int
    whisper_batch_size: int
    tracking_sample_interval: float
    nvenc_preset: str
    cpu_preset: str
    subprocess_priority_flags: int


def resolve_profile_limits(profile: str, snapshot: HardwareSnapshot) -> ProfileLimits:
    """Compute validated CPU, GPU, and I/O limits from the selected profile and real hardware."""
    selected = profile if profile in PROFILES else "balanced"
    physical = max(1, snapshot.cpu_physical)
    logical = max(physical, snapshot.cpu_logical)
    vram_mib = snapshot.vram_total_mib or 0
    free_vram_mib = snapshot.vram_free_mib if snapshot.vram_free_mib is not None else vram_mib
    avail_ram_mib = snapshot.ram_available_mib if snapshot.ram_available_mib is not None else 4096
    total_ram_mib = snapshot.ram_total_mib if snapshot.ram_total_mib is not None else 16384

    if snapshot.unified_memory:
        perf_cores = max(2, snapshot.cpu_perf_cores or max(2, physical // 2))
        avail_ratio = (avail_ram_mib / max(1, total_ram_mib)) if total_ram_mib else 0.5
        high_pressure = (
            snapshot.memory_pressure_level == "critical"
            or snapshot.thermal_pressure == "Critical"
        )
        elevated_pressure = (
            snapshot.memory_pressure_level == "warning"
            or snapshot.thermal_pressure == "Serious"
            or avail_ratio < 0.20
            or (snapshot.swap_used_mib is not None and snapshot.swap_used_mib >= 2048)
        )
        on_battery = snapshot.power_source == "Battery Power"

        if high_pressure:
            return ProfileLimits(
                profile=selected,
                cpu_threads=max(2, perf_cores // 2),
                ffmpeg_threads=max(2, perf_cores // 2),
                whisper_batch_size=1,
                tracking_sample_interval=1.25,
                nvenc_preset="videotoolbox",
                cpu_preset="medium",
                subprocess_priority_flags=0,
            )

        if selected == "eco" or (on_battery and selected == "balanced" and elevated_pressure):
            return ProfileLimits(
                profile=selected,
                cpu_threads=max(2, min(perf_cores, physical // 2)),
                ffmpeg_threads=max(2, min(perf_cores, physical // 2)),
                whisper_batch_size=1 if elevated_pressure else 2,
                tracking_sample_interval=1.0,
                nvenc_preset="videotoolbox",
                cpu_preset="medium",
                subprocess_priority_flags=0,
            )
        if selected == "turbo":
            threads = max(2, perf_cores if (elevated_pressure or on_battery) else logical)
            batch = 2 if elevated_pressure else (6 if avail_ram_mib >= 6000 else (4 if avail_ram_mib >= 3500 else 2))
            return ProfileLimits(
                profile="turbo",
                cpu_threads=threads,
                ffmpeg_threads=threads,
                whisper_batch_size=batch,
                tracking_sample_interval=0.75 if elevated_pressure else 0.5,
                nvenc_preset="videotoolbox",
                cpu_preset="fast",
                subprocess_priority_flags=0,
            )
        # Balanced (default)
        threads = max(2, perf_cores if (elevated_pressure or on_battery) else physical)
        batch = 1 if elevated_pressure else (4 if avail_ram_mib >= 4500 else 2)
        return ProfileLimits(
            profile="balanced",
            cpu_threads=threads,
            ffmpeg_threads=threads,
            whisper_batch_size=batch,
            tracking_sample_interval=0.85 if elevated_pressure else 0.75,
            nvenc_preset="videotoolbox",
            cpu_preset="medium",
            subprocess_priority_flags=0,
        )

    if selected == "eco":
        cpu_threads = max(2, physical // 2)
        ffmpeg_threads = max(2, physical // 2)
        batch = 4 if vram_mib >= 5500 and free_vram_mib >= 2500 else 2
        if avail_ram_mib < 2500:
            batch = min(batch, 2)
        return ProfileLimits(
            profile="eco",
            cpu_threads=cpu_threads,
            ffmpeg_threads=ffmpeg_threads,
            whisper_batch_size=batch,
            tracking_sample_interval=1.0,
            nvenc_preset="p6",
            cpu_preset="medium",
            subprocess_priority_flags=_BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0,
        )

    if selected == "turbo":
        cpu_threads = max(2, logical - 1 if logical >= 8 else logical)
        ffmpeg_threads = max(2, logical)
        if vram_mib >= 5500 and free_vram_mib >= 3200:
            batch = 12
        elif vram_mib >= 3800:
            batch = 6
        else:
            batch = 4
        if avail_ram_mib < 2500:
            batch = min(batch, 4)
        return ProfileLimits(
            profile="turbo",
            cpu_threads=cpu_threads,
            ffmpeg_threads=ffmpeg_threads,
            whisper_batch_size=batch,
            tracking_sample_interval=0.5,
            nvenc_preset="p4",
            cpu_preset="fast",
            subprocess_priority_flags=_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0,
        )

    # Balanced (default)
    cpu_threads = max(2, min(logical, max(physical, logical // 2 + 2)))
    ffmpeg_threads = max(2, physical)
    if vram_mib >= 5500 and free_vram_mib >= 2800:
        batch = 8
    elif vram_mib >= 3800:
        batch = 4
    else:
        batch = 2
    if avail_ram_mib < 2500:
        batch = min(batch, 2)
    return ProfileLimits(
        profile="balanced",
        cpu_threads=cpu_threads,
        ffmpeg_threads=ffmpeg_threads,
        whisper_batch_size=batch,
        tracking_sample_interval=0.75,
        nvenc_preset="p5",
        cpu_preset="medium",
        subprocess_priority_flags=_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0,
    )


def is_process_running(process_name: str) -> bool:
    """Detect a target process by name only; never inspects process memory."""
    target = process_name.strip().lower()
    if not target or psutil is None:
        return False
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").strip().lower()
            if name == target:
                return True
    except (psutil.Error, OSError):
        return False
    return False


class HardwareMonitor:
    """Lightweight hardware telemetry reader using psutil and NVIDIA NVML."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nvml_ready = False
        self._nvml_failed = False
        self._gpu_handle = None
        self._thermal_paused = False
        self._macos_probe_ts = 0.0
        self._macos_mem_pressure: tuple[str | None, str] = (None, "normal")
        self._macos_power: tuple[str | None, int | None, bool | None] = (None, None, None)
        if psutil is not None:
            try:
                psutil.cpu_percent(interval=None)
            except Exception:
                pass

    def _ensure_nvml(self) -> bool:
        if self._nvml_ready:
            return True
        if self._nvml_failed or pynvml is None:
            return False
        try:
            pynvml.nvmlInit()
            self._gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._nvml_ready = True
            return True
        except Exception:
            self._nvml_failed = True
            self._gpu_handle = None
            return False

    def snapshot(self, gaming_process: str = "TslGame.exe") -> HardwareSnapshot:
        cpu_percent: float | None = None
        cpu_physical = os.cpu_count() or 1
        cpu_logical = os.cpu_count() or 1
        ram_total_mib = ram_used_mib = ram_available_mib = None
        swap_total_mib = swap_used_mib = None

        if psutil is not None:
            try:
                cpu_percent = float(psutil.cpu_percent(interval=None))
                cpu_physical = psutil.cpu_count(logical=False) or cpu_logical
                cpu_logical = psutil.cpu_count(logical=True) or cpu_physical
                vm = psutil.virtual_memory()
                ram_total_mib = int(vm.total // (1024 * 1024))
                ram_used_mib = int((vm.total - vm.available) // (1024 * 1024))
                ram_available_mib = int(vm.available // (1024 * 1024))
                sw = psutil.swap_memory()
                swap_total_mib = int(sw.total // (1024 * 1024))
                swap_used_mib = int(sw.used // (1024 * 1024))
            except Exception:
                pass

        gpu_name = None
        gpu_util_percent = vram_total_mib = vram_used_mib = vram_free_mib = gpu_temp_c = None
        gpu_power_w = None
        pinfo = detect_platform()
        unified_memory = pinfo.is_apple_silicon
        gpu_backend = "apple-silicon" if pinfo.is_apple_silicon else "none"
        cpu_perf_cores = cpu_eff_cores = gpu_cores = None
        memory_pressure = None
        memory_pressure_level = "normal"
        thermal_pressure = None
        battery_percent = None
        battery_charging = None
        power_source = None

        if pinfo.is_macos:
            cpu_perf_cores, cpu_eff_cores, gpu_cores = _apple_static_hardware()
            if pinfo.is_apple_silicon:
                core_suffix = f", {gpu_cores}-core GPU" if gpu_cores else ""
                gpu_name = f"{pinfo.cpu_brand} (Apple Silicon{core_suffix})"
            thermal_pressure = _read_macos_thermal_state()
            now = time.monotonic()
            if now - self._macos_probe_ts >= 5.0:
                self._macos_probe_ts = now
                try:
                    mp_out = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True, timeout=2).stdout
                    self._macos_mem_pressure = parse_memory_pressure_output(mp_out)
                except (OSError, subprocess.SubprocessError):
                    pass
                try:
                    pm_out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=2).stdout
                    self._macos_power = parse_pmset_battery_output(pm_out)
                except (OSError, subprocess.SubprocessError):
                    pass
            memory_pressure, memory_pressure_level = self._macos_mem_pressure
            power_source, battery_percent, battery_charging = self._macos_power

        if not pinfo.is_macos:
            with self._lock:
                if self._ensure_nvml() and self._gpu_handle is not None:
                    gpu_backend = "nvidia"
                    try:
                        raw_name = pynvml.nvmlDeviceGetName(self._gpu_handle)
                        gpu_name = raw_name.decode("utf-8", "replace") if isinstance(raw_name, bytes) else str(raw_name)
                    except Exception:
                        pass
                    try:
                        mem = pynvml.nvmlDeviceGetMemoryInfo(self._gpu_handle)
                        vram_total_mib = int(mem.total // (1024 * 1024))
                        vram_used_mib = int(mem.used // (1024 * 1024))
                        vram_free_mib = int(mem.free // (1024 * 1024))
                    except Exception:
                        pass
                    try:
                        util = pynvml.nvmlDeviceGetUtilizationRates(self._gpu_handle)
                        gpu_util_percent = int(util.gpu)
                    except Exception:
                        pass
                    try:
                        gpu_temp_c = int(pynvml.nvmlDeviceGetTemperature(self._gpu_handle, pynvml.NVML_TEMPERATURE_GPU))
                    except Exception:
                        pass
                    try:
                        gpu_power_w = round(pynvml.nvmlDeviceGetPowerUsage(self._gpu_handle) / 1000.0, 1)
                    except Exception:
                        pass

        gaming_active = is_process_running(gaming_process) if (gaming_process and pinfo.is_windows) else False
        return HardwareSnapshot(
            cpu_percent=cpu_percent,
            cpu_physical=cpu_physical,
            cpu_logical=cpu_logical,
            ram_total_mib=ram_total_mib,
            ram_used_mib=ram_used_mib,
            ram_available_mib=ram_available_mib,
            gpu_name=gpu_name,
            gpu_util_percent=gpu_util_percent,
            vram_total_mib=vram_total_mib,
            vram_used_mib=vram_used_mib,
            vram_free_mib=vram_free_mib,
            gpu_temp_c=gpu_temp_c,
            gpu_power_w=gpu_power_w,
            cpu_temp_c=None,  # Unavailable via standard user-mode APIs without fabricating
            gaming_active=gaming_active,
            unified_memory=unified_memory,
            gpu_backend=gpu_backend,
            cpu_perf_cores=cpu_perf_cores,
            cpu_eff_cores=cpu_eff_cores,
            gpu_cores=gpu_cores,
            swap_total_mib=swap_total_mib,
            swap_used_mib=swap_used_mib,
            memory_pressure=memory_pressure,
            memory_pressure_level=memory_pressure_level,
            thermal_pressure=thermal_pressure,
            battery_percent=battery_percent,
            battery_charging=battery_charging,
            power_source=power_source,
            rosetta_active=pinfo.is_rosetta,
            accel_backends=detect_acceleration_backends(),
        )

    def should_pause_for_thermal(self, settings: PerformanceSettings, snap: HardwareSnapshot) -> bool:
        if not settings.thermal_protection:
            self._thermal_paused = False
            return False
        if snap.unified_memory:
            if snap.thermal_pressure == "Critical" or snap.memory_pressure_level == "critical":
                self._thermal_paused = True
            elif snap.thermal_pressure in (None, "Nominal", "Fair") and snap.memory_pressure_level != "critical":
                self._thermal_paused = False
            return self._thermal_paused
        if snap.gpu_temp_c is None:
            self._thermal_paused = False
            return False
        if not self._thermal_paused and snap.gpu_temp_c >= settings.gpu_temp_limit_c:
            self._thermal_paused = True
        elif self._thermal_paused and snap.gpu_temp_c <= settings.gpu_temp_resume_c:
            self._thermal_paused = False
        return self._thermal_paused

    def wait_if_constrained(
        self,
        settings: PerformanceSettings,
        cancel_event: threading.Event,
        log: Callable[[str], None] | None = None,
        *,
        poll_seconds: float = 1.0,
    ) -> None:
        """Pause at safe job boundaries for thermal cooldown or active Gaming Mode."""
        announced_gaming = False
        announced_thermal = False
        while not cancel_event.is_set():
            snap = self.snapshot(settings.gaming_process if settings.gaming_mode else "")
            gaming_pause = settings.gaming_mode and snap.gaming_active
            thermal_pause = self.should_pause_for_thermal(settings, snap)
            if not gaming_pause and not thermal_pause:
                if (announced_gaming or announced_thermal) and log:
                    log("System constraints cleared; resuming processing")
                return
            if gaming_pause and not announced_gaming:
                announced_gaming = True
                if log:
                    log(f"Gaming Mode active ({settings.gaming_process} detected); deferring heavy job until exit")
            if thermal_pause and not announced_thermal:
                announced_thermal = True
                if log:
                    log(
                        f"Thermal protection active (GPU {snap.gpu_temp_c}°C >= {settings.gpu_temp_limit_c}°C); "
                        f"cooling down to {settings.gpu_temp_resume_c}°C"
                    )
            cancel_event.wait(poll_seconds)

    def shutdown(self) -> None:
        with self._lock:
            if self._nvml_ready and pynvml is not None:
                try:
                    pynvml.nvmlShutdown()
                except Exception:
                    pass
            self._nvml_ready = False
            self._gpu_handle = None
