"""No-key end-to-end pipeline validation for a short speech video."""
from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

from autoclip_studio.config import SettingsStore
from autoclip_studio.database import Database
from autoclip_studio.models import ProjectOptions
from autoclip_studio.pipeline import Pipeline


def main(source_value: str) -> None:
    source = Path(source_value).resolve()
    root = source.parent / "autoclip-data"
    settings = SettingsStore(root / "settings.json")
    settings.data["hardware_encoding"] = True
    settings.data["output_dir"] = str(root / "output")
    settings.save()
    database = Database(root / "smoke.db")
    options = ProjectOptions(
        source=str(source), clip_count=1, min_duration=10, max_duration=45,
        resolution="360x640", face_tracking=False, captions=True,
        provider="gemini", model="gemini-2.5-flash", cache_enabled=True,
        whisper_model="tiny", whisper_device="cpu", output_dir=str(root / "output"),
    )
    project_id = database.create_project(str(source), asdict(options))
    clips = Pipeline(database, settings).run(
        project_id, options, lambda value, message: print(f"[{value:3d}%] {message}")
    )
    assert len(clips) == 1 and clips[0].path.exists()
    assert database.projects(1)[0]["status"] == "completed"
    print(f"Full pipeline passed: {clips[0].path}")


if __name__ == "__main__":
    main(sys.argv[1])
