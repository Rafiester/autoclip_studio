from __future__ import annotations

import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

from .paths import APP_ROOT, DATA_ROOT, MODEL_CACHE

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

try:
    import pynvml
except ImportError:  # pragma: no cover
    pynvml = None  # type: ignore[assignment]

MODELS = ("tiny", "base", "small", "small-q4", "medium", "large-v2", "large-v3", "large-v3-turbo", "large-v3-turbo-q4")
XXL_NAME = "faster-whisper-xxl.exe"
XXL_RELEASES = "https://github.com/Purfview/whisper-standalone-win/releases"
XXL_INSTALLS = DATA_ROOT / "speech-engines"
MODEL_FILES = ("config.json", "tokenizer.json", "vocabulary.txt", "vocabulary.json", "preprocessor_config.json")


@dataclass(frozen=True, slots=True)
class SpeechSettings:
    engine: str = "auto"
    compute_type: str = "auto"
    batch_size: int = 0
    language: str = ""
    vad: bool = True
    word_timestamps: bool = True
    cpu_fallback: bool = True
    model_directory: str = ""
    executable: str = ""
    export_srt: bool = False


@dataclass(frozen=True, slots=True)
class GpuInfo:
    name: str
    total_mib: int
    free_mib: int
    used_mib: int
    utilization: int | None
    app_used_mib: int | None = None
    app_active: bool = False
    temperature_c: int | None = None
    power_w: float | None = None
    telemetry_source: str = "nvidia-smi"


@dataclass(frozen=True, slots=True)
class Purfview:
    executable: Path
    version: str
    flags: frozenset[str]
    cuda: bool

    @property
    def model_directory(self) -> Path:
        return self.executable.parent / "_models"

    @property
    def identity(self) -> str:
        stat = self.executable.stat()
        return f"{self.version}:{self.executable}:{stat.st_size}:{stat.st_mtime_ns}"


def subtitle_edit_engine() -> Path | None:
    if os.name != "nt":
        return None
    for env_key in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env_key)
        if not base:
            continue
        path = Path(base) / "Subtitle Edit" / "SpeechToText" / "Purfview-Faster-Whisper-XXL" / XXL_NAME
        if path.is_file():
            return path
    return None


def mlx_whisper_available() -> bool:
    if sys.platform != "darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
        return False
    return importlib.util.find_spec("mlx_whisper") is not None and importlib.util.find_spec("mlx") is not None


def find_executable(configured: str = "") -> Path | None:
    if os.name != "nt" and not configured:
        return None
    if configured:
        candidate = Path(configured).expanduser().resolve()
        return candidate if os.name == "nt" or candidate.is_file() else None
    subtitle = subtitle_edit_engine()
    if subtitle:
        return subtitle
    data_hit = next(iter(sorted(XXL_INSTALLS.glob(f"*/{XXL_NAME}"), reverse=True)), None)
    if data_hit:
        return data_hit
    for local_candidate in (
        APP_ROOT / "tools" / "Purfview-Faster-Whisper-XXL" / XXL_NAME,
        APP_ROOT / "tools" / XXL_NAME,
    ):
        if local_candidate.is_file():
            return local_candidate
    return None


def _probe(command: list[str | Path], *, cwd: Path | None = None, timeout: int = 12) -> str:
    result = subprocess.run(
        command, cwd=cwd, encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return result.stdout.strip()


def _tracked_pids(extra_pids: tuple[int, ...] = ()) -> set[int]:
    pids: set[int] = {os.getpid(), *(int(p) for p in extra_pids if p and p > 0)}
    if psutil is not None:
        try:
            proc = psutil.Process(os.getpid())
            for child in proc.children(recursive=True):
                pids.add(child.pid)
        except Exception:
            pass
    return pids


def _nvml_process_vram(handle: object, total_bytes: int, tracked_pids: set[int]) -> tuple[int | None, bool]:
    """Return (app_used_mib, app_active) across compute + graphics NVML process lists."""
    if pynvml is None:
        return None, False
    total_app_bytes = 0
    saw_numeric = False
    active = False
    getters = (
        getattr(pynvml, "nvmlDeviceGetComputeRunningProcesses", None),
        getattr(pynvml, "nvmlDeviceGetGraphicsRunningProcesses", None),
    )
    seen_pids: set[int] = set()
    for getter in getters:
        if getter is None:
            continue
        try:
            items = getter(handle)
        except Exception:
            continue
        for item in items or ():
            pid = getattr(item, "pid", None)
            if pid is None or pid not in tracked_pids or pid in seen_pids:
                continue
            seen_pids.add(pid)
            active = True
            used_mem = getattr(item, "usedGpuMemory", None)
            if isinstance(used_mem, int) and 0 <= used_mem <= total_bytes * 2:
                total_app_bytes += used_mem
                saw_numeric = True
    return (int(total_app_bytes // (1024 * 1024)) if saw_numeric else None), active


def _nvidia_smi_path() -> str | None:
    if found := shutil.which("nvidia-smi"):
        return found
    if os.name == "nt":
        for cand in (
            Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
        ):
            if cand.is_file():
                return str(cand)
    return None


def gpu_info(tracked_pids: tuple[int, ...] = ()) -> GpuInfo | None:
    if sys.platform == "darwin":
        return None
    pids = _tracked_pids(tracked_pids)
    if pynvml is not None:
        inited = False
        try:
            pynvml.nvmlInit()
            inited = True
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            raw_name = pynvml.nvmlDeviceGetName(handle)
            name = raw_name.decode("utf-8", "replace") if isinstance(raw_name, bytes) else str(raw_name)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            total_mib = int(mem.total // (1024 * 1024))
            free_mib = int(mem.free // (1024 * 1024))
            used_mib = int(mem.used // (1024 * 1024))
            utilization = None
            try:
                utilization = int(pynvml.nvmlDeviceGetUtilizationRates(handle).gpu)
            except Exception:
                pass
            temp_c = None
            try:
                temp_c = int(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))
            except Exception:
                pass
            power_w = None
            try:
                power_w = round(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0, 1)
            except Exception:
                pass
            app_used_mib, app_active = _nvml_process_vram(handle, int(mem.total), pids)
            return GpuInfo(
                name, total_mib, free_mib, used_mib, utilization,
                app_used_mib=app_used_mib, app_active=app_active,
                temperature_c=temp_c, power_w=power_w, telemetry_source="nvml",
            )
        except Exception:
            pass
        finally:
            if inited:
                try:
                    pynvml.nvmlShutdown()
                except Exception:
                    pass

    smi = _nvidia_smi_path()
    if smi is None:
        return None
    try:
        output = _probe(
            [smi, "-i", "0", "--query-gpu=name,memory.total,memory.free,memory.used,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"], timeout=4,
        )
        parts = [field.strip() for field in output.splitlines()[0].split(",")]
        name, total, free, used = parts[0], int(parts[1]), int(parts[2]), int(parts[3])
        util = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
        temp = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else None
        return GpuInfo(name, total, free, used, util, temperature_c=temp, telemetry_source="nvidia-smi")
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def python_cuda() -> bool:
    if os.name == "nt" and shutil.which("nvidia-smi") is None and not os.environ.get("CUDA_PATH"):
        pass
    if sys.platform == "darwin":
        return False
    try:
        import ctranslate2
        if "float16" not in ctranslate2.get_supported_compute_types("cuda"):
            return False
        if os.name == "nt":
            import ctypes
            for library in ("cublas64_12.dll", "cudnn64_9.dll"):
                ctypes.WinDLL(library, winmode=0)
        return True
    except (ImportError, OSError, RuntimeError, ValueError):
        return False


def cuda_libraries(executable: Path) -> tuple[str, ...]:
    return tuple(sorted({path.name for pattern in ("cublas*.dll", "cudnn*.dll")
                         for path in executable.parent.rglob(pattern)}))


def inspect_purfview(executable: Path) -> Purfview:
    stat = executable.stat()
    return _inspect_purfview(executable, stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=8)
def _inspect_purfview(executable: Path, _size: int, _modified: int) -> Purfview:
    with TemporaryDirectory(prefix="autoclip-engine-check-") as directory:
        scratch = Path(directory)
        help_text = _probe([executable, "--help"], cwd=scratch)
        flags = frozenset(re.findall(r"(?<![\w-])--[\w-]+", help_text))
        required = {
            "--model", "--model_dir", "--device", "--compute_type", "--output_dir", "--output_format",
            "--task", "--language", "--vad_filter", "--word_timestamps", "--beam_size",
            "--condition_on_previous_text", "--version",
        }
        if not required <= flags or "json" not in help_text:
            raise RuntimeError("The executable does not expose the required Purfview XXL JSON transcription options.")
        version = _probe([executable, "--version"], cwd=scratch).splitlines()
        check = _probe([executable, "--checkcuda"], cwd=scratch) if "--checkcuda" in flags else ""
    cuda = re.search(r"CUDA device\s*:\s*([1-9]\d*)", check, re.IGNORECASE) is not None
    return Purfview(executable, version[-1] if version else "Version unavailable", flags, cuda)


def complete_model(path: Path) -> bool:
    return all((path / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json"))


def model_cache_root() -> Path:
    root = Path(os.environ.get("HF_HUB_CACHE", MODEL_CACHE)).expanduser().resolve()
    subtitle = subtitle_edit_engine()
    return MODEL_CACHE if subtitle and root.is_relative_to(subtitle.parent.resolve()) else root


def find_model(name: str, settings: SpeechSettings, executable: Path | None = None) -> Path | None:
    if Path(name).is_absolute() and complete_model(Path(name)):
        return Path(name)
    roots = []
    if settings.model_directory:
        custom = Path(settings.model_directory).expanduser()
        if complete_model(custom):
            folder_name = custom.name.removeprefix("faster-whisper-")
            if custom.parent.name == "snapshots":
                repository = custom.parent.parent.name.split("--")[-1].lower()
                folder_name = next((model for model in sorted(MODELS, key=len, reverse=True)
                                    if repository.endswith(f"-{model}")), folder_name)
            if folder_name not in MODELS or folder_name == name:
                return custom.resolve()
        roots.append(custom)
    if executable:
        roots.append(executable.parent / "_models")
    subtitle = subtitle_edit_engine()
    if subtitle:
        roots.append(subtitle.parent / "_models")
    roots.append(model_cache_root())
    for root in dict.fromkeys(roots):
        for candidate in (root / f"faster-whisper-{name}", root / name):
            if complete_model(candidate):
                return candidate.resolve()
        if root.is_dir():
            from faster_whisper.utils import download_model
            try:
                candidate = Path(download_model(name, cache_dir=str(root), local_files_only=True))
            except (ValueError, OSError):
                continue
            if complete_model(candidate):
                return candidate
    return None


def download_speech_model(name: str, settings: SpeechSettings) -> Path:
    existing = find_model(name, settings, find_executable(settings.executable))
    if existing:
        return existing
    from faster_whisper.utils import download_model
    return Path(download_model(name, cache_dir=str(model_cache_root())))


def stage_model(source: Path, workspace: Path, name: str) -> Path:
    root = workspace / "models"
    target = root / f"faster-whisper-{name}"
    target.mkdir(parents=True)
    try:
        os.link((source / "model.bin").resolve(), target / "model.bin")
    except OSError:
        try:
            (target / "model.bin").symlink_to((source / "model.bin").resolve())
        except OSError as error:
            raise RuntimeError("Cannot share this model without copying its weights. Use Python Faster-Whisper "
                               "or move the model to the AutoClip Studio drive.") from error
    for filename in MODEL_FILES:
        if (source / filename).is_file():
            shutil.copy2(source / filename, target / filename)
    return root


def choose_compute(device: str, requested: str, gpu: GpuInfo | None) -> str:
    if device == "cpu":
        return requested if requested in {"int8", "int8_float32", "float32"} else "int8"
    if requested != "auto":
        return requested
    return "int8_float16" if gpu is None or gpu.free_mib < 5500 else "float16"


def install_standalone(source: Path) -> Path:
    source = source.resolve()
    if not (source / XXL_NAME).is_file():
        raise ValueError(f"Choose the extracted Purfview folder containing {XXL_NAME}.")
    if XXL_INSTALLS.resolve().is_relative_to(source):
        raise ValueError("Choose the extracted engine folder outside AutoClip Studio's engine cache.")
    XXL_INSTALLS.mkdir(parents=True, exist_ok=True)
    destination = Path(mkdtemp(prefix="purfview-", dir=XXL_INSTALLS))
    try:
        shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("_models"))
        executable = destination / XXL_NAME
        inspect_purfview(executable)
        return executable
    except BaseException:
        if destination.resolve().parent == XXL_INSTALLS.resolve():
            shutil.rmtree(destination)
        raise


def inspect_speech(settings: SpeechSettings, model: str) -> str:
    from importlib.metadata import version
    gpu = gpu_info()
    if sys.platform == "darwin":
        mlx_state = "available" if mlx_whisper_available() else "unavailable (CPU fallback)"
        lines = [
            f"Apple Silicon MLX Whisper {mlx_state}",
            f"Python Faster-Whisper {version('faster-whisper')} (CPU fallback)",
        ]
    else:
        lines = [f"Python Faster-Whisper {version('faster-whisper')} · CUDA runtime "
                 f"{'detected' if python_cuda() else 'unavailable'}",
                 f"{gpu.name} · {gpu.free_mib:,}/{gpu.total_mib:,} MiB free" if gpu else "NVIDIA telemetry unavailable"]
    executable = find_executable(settings.executable)
    if executable:
        try:
            engine = inspect_purfview(executable)
            libraries = cuda_libraries(executable)
            lines.extend((f"Purfview · {engine.version} · CUDA device {'detected' if engine.cuda else 'unavailable'}",
                          f"CUDA libraries: {', '.join(libraries) if libraries else 'none beside the executable'}"))
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            lines.append(f"Purfview probe failed: {error}")
        subtitle = subtitle_edit_engine()
        if subtitle and subtitle == executable:
            lines.append("Using the existing Subtitle Edit installation")
    else:
        lines.append("Purfview XXL was not found")
    path = find_model(model, settings, executable)
    lines.append(f"{model}: {path}" if path else f"{model}: not downloaded")
    other_settings = settings
    if settings.model_directory and complete_model(Path(settings.model_directory).expanduser()):
        other_settings = replace(settings, model_directory="")
    available = [name for name in MODELS if (path if name == model else find_model(name, other_settings, executable))]
    lines.append("Available models: " + (", ".join(available) or "none"))
    return "\n".join(lines)
