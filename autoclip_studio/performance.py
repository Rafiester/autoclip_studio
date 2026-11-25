"""Adaptive performance profiles, hardware monitoring, thermal guard, and Gaming Mode."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

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

        if psutil is not None:
            try:
                cpu_percent = float(psutil.cpu_percent(interval=None))
                cpu_physical = psutil.cpu_count(logical=False) or cpu_logical
                cpu_logical = psutil.cpu_count(logical=True) or cpu_physical
                vm = psutil.virtual_memory()
                ram_total_mib = int(vm.total // (1024 * 1024))
                ram_used_mib = int((vm.total - vm.available) // (1024 * 1024))
                ram_available_mib = int(vm.available // (1024 * 1024))
            except Exception:
                pass

        gpu_name = None
        gpu_util_percent = vram_total_mib = vram_used_mib = vram_free_mib = gpu_temp_c = None
        gpu_power_w = None

        with self._lock:
            if self._ensure_nvml() and self._gpu_handle is not None:
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

        gaming_active = is_process_running(gaming_process) if gaming_process else False
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
            cpu_temp_c=None,  # Unavailable via standard user-mode Windows APIs without fabricating
            gaming_active=gaming_active,
        )

    def should_pause_for_thermal(self, settings: PerformanceSettings, snap: HardwareSnapshot) -> bool:
        if not settings.thermal_protection or snap.gpu_temp_c is None:
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
