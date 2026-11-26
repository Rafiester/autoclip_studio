from __future__ import annotations

import sys

from autoclip_studio.performance import (
    HardwareMonitor,
    HardwareSnapshot,
    PerformanceSettings,
    parse_memory_pressure_output,
    parse_pmset_battery_output,
    resolve_profile_limits,
)
from autoclip_studio.platform_utils import detect_platform
from autoclip_studio.rendering import _ENCODER_PROBE_CACHE, hevc_videotoolbox_available, videotoolbox_available
from autoclip_studio.process import ProcessManager
from autoclip_studio.transcription import MLX_REPOS


def test_parse_memory_pressure_and_battery_outputs() -> None:
    label, sev = parse_memory_pressure_output("System-wide memory free percentage: 43%")
    assert sev == "normal" and "43%" in (label or "")

    label, sev = parse_memory_pressure_output("System-wide memory free percentage: 18%")
    assert sev == "warning" and "18%" in (label or "")

    label, sev = parse_memory_pressure_output("System-wide memory free percentage: 9%")
    assert sev == "critical" and "9%" in (label or "")

    assert parse_memory_pressure_output("unexpected output") == (None, "normal")

    src, pct, chg = parse_pmset_battery_output(
        "Now drawing from 'AC Power'\n -InternalBattery-0\t75%; charging; 1:25 remaining"
    )
    assert (src, pct, chg) == ("AC Power", 75, True)

    src, pct, chg = parse_pmset_battery_output(
        "Now drawing from 'Battery Power'\n -InternalBattery-0\t42%; discharging; 3:10 remaining"
    )
    assert (src, pct, chg) == ("Battery Power", 42, False)


def test_unified_memory_pressure_battery_and_thermal_adaptation() -> None:
    base = HardwareSnapshot(
        cpu_percent=25.0, cpu_physical=8, cpu_logical=8,
        cpu_perf_cores=4, cpu_eff_cores=4, gpu_cores=7,
        ram_total_mib=8192, ram_used_mib=4096, ram_available_mib=4096,
        unified_memory=True, gpu_backend="apple-silicon",
        memory_pressure_level="normal", thermal_pressure="Nominal",
        power_source="AC Power",
    )
    normal_turbo = resolve_profile_limits("turbo", base)
    assert normal_turbo.cpu_threads == 8
    assert normal_turbo.whisper_batch_size >= 4

    # Battery power scales threads down cleanly
    on_batt = HardwareSnapshot(
        cpu_percent=25.0, cpu_physical=8, cpu_logical=8,
        cpu_perf_cores=4, cpu_eff_cores=4,
        ram_total_mib=8192, ram_used_mib=4096, ram_available_mib=4096,
        unified_memory=True, gpu_backend="apple-silicon",
        power_source="Battery Power",
    )
    batt_turbo = resolve_profile_limits("turbo", on_batt)
    assert batt_turbo.cpu_threads <= normal_turbo.cpu_threads

    # Critical memory or thermal pressure throttles to safe minimum
    critical = HardwareSnapshot(
        cpu_percent=80.0, cpu_physical=8, cpu_logical=8,
        cpu_perf_cores=4, cpu_eff_cores=4,
        ram_total_mib=8192, ram_used_mib=7600, ram_available_mib=592,
        unified_memory=True, gpu_backend="apple-silicon",
        memory_pressure_level="critical", thermal_pressure="Critical",
    )
    crit_lim = resolve_profile_limits("turbo", critical)
    assert crit_lim.cpu_threads == 2
    assert crit_lim.whisper_batch_size == 1

    # Thermal protection pauses at stage boundaries on Critical pressure
    mon = HardwareMonitor()
    assert mon.should_pause_for_thermal(PerformanceSettings(thermal_protection=True), critical) is True
    assert mon.should_pause_for_thermal(PerformanceSettings(thermal_protection=True), base) is False


def test_mlx_quantized_repos_and_native_macos_telemetry() -> None:
    assert "large-v3-turbo" in MLX_REPOS
    assert "large-v3-turbo-q4" in MLX_REPOS
    assert "small-q4" in MLX_REPOS

    if sys.platform == "darwin":
        pinfo = detect_platform()
        assert pinfo.is_apple_silicon
        assert pinfo.is_rosetta is False
        snap = HardwareMonitor().snapshot()
        assert snap.unified_memory is True
        assert snap.cpu_perf_cores is not None and snap.cpu_perf_cores >= 1
        assert snap.gpu_cores is not None and snap.gpu_cores >= 1
        assert snap.thermal_pressure in {"Nominal", "Fair", "Serious", "Critical"}
        mgr = ProcessManager()
        _ENCODER_PROBE_CACHE.clear()
        assert videotoolbox_available(mgr) is True
        assert hevc_videotoolbox_available(mgr) is True
        assert len(_ENCODER_PROBE_CACHE) >= 2
