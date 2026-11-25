# AutoClip Studio — Overnight Optimization Plan

## Execution contract
Execute phases 1–3 in order in the existing Windows-native repository. Work autonomously within granted permissions; do not wait for routine design decisions. Inspect Git status, existing architecture, AGENTS.md and relevant installed skills only. Preserve uncommitted user changes; never reset, stash or overwrite unrelated work. Use targeted file reads; exclude virtual environments, media, model caches and build artifacts. Maintain a concise progress checklist in this document. Validate each phase before proceeding. If a noncritical item is blocked, record it and continue independent work; do not claim untested functionality works. Stop affected work on destructive changes, unexpected paid API calls or missing authorization. No paid API calls, driver changes, administrator escalation, Docker, WSL, BAT/CMD scripts or persistent services. Do not disable approval or security controls. Keep existing features and user data intact. Do not automatically push to remote.

## Phase 1 — Logo and application identity
- [x] Locate the two user-provided PNG, JPG and SVG assets in the workspace. Inspect their actual dimensions, alpha channel and contents; identify wordmark and symbol. If missing, record the blocker and do not invent replacement logos.
- [x] Preserve originals; centralize assets and resource path resolution. Use the wordmark in sidebar/header and setup/about screens where appropriate; use the symbol for compact navigation, window/taskbar icon and shortcut.
- [x] Preserve aspect ratio, transparency, colors and clear space. Avoid stretching, cropping, recoloring or adding decorative effects to the originals. Provide high-DPI scaling and a readable compact sidebar.
- [x] Generate a multi-resolution Windows ICO from the symbol (16, 24, 32, 48, 64, 128, 256 px where source quality permits). Set Qt application/window icons and consistent Windows application identity before window creation.
- [x] Update launcher icon using the existing build mechanism. Rebuild the lightweight launcher only once if embedded icon changes require it; keep source-code updates rebuild-free. No batch files or full environment bundling.
- [x] Validate launcher, taskbar, sidebar, setup wizard (if present), high-DPI behavior and resource paths from a different working directory. Do not overwrite a working executable until replacement is validated.

## Phase 2 — SaaS UI/UX optimization and brand system
- [x] Audit current Qt/PySide6 UI, shared styles, widgets and navigation. Use relevant installed UI/UX and motion skills selectively. Keep native desktop architecture; do not replace it with a web stack.

### Brand guidelines
| Token | Value | Usage |
|---|---|---|
| Obsidian Black | #101116 | Main background |
| Modern Red | #E53935 | Primary brand, selected navigation, primary actions |
| Accent Red | #FF5A4F | Highlights, active indicators, focus accents |
| Soft White | #F5F5F7 | Primary text |
| Surface (proposed) | #191B22 | Cards and panels |
| Elevated surface (proposed) | #22252E | Inputs and raised surfaces |
| Border (proposed) | #30343E | Subtle separators |
| Muted text (proposed) | #A3A8B5 | Secondary information |

Use Inter, Geist or Manrope if available/licensed, with Segoe UI fallback. Medium/semibold headings, compact spacing, readable body text. Adopt an 8px spacing rhythm, consistent 8–12px radii, restrained shadows and red accents. Avoid excessive gradients, glow and red backgrounds. Keep semantic success green, warning amber and error visually distinct from brand actions; verify contrast and keyboard focus. Do not recolor user-selected caption templates as part of branding.

- [x] Centralize design tokens, typography, reusable cards, buttons, inputs, dialogs, status indicators and progress components. Remove obsolete purple branding without changing semantic or user-customized colors.
- [x] Refine sidebar, Dashboard, New Project, Processing Queue, Generated Clips, Caption Templates, AI Provider Settings, Speech-to-Text and existing setup/component screens. Preserve navigation and functionality. Fix unintended rectangular label backgrounds, clipping, inconsistent padding and alignment.
- [x] Preserve YouTube metadata preview, IDM-style downloader progress, selectable/copyable logs, caption selection and animated previews, local/cloud STT, provider settings and setup progress wherever already implemented. If any are absent, record them as outside this optimization scope rather than silently implementing unrelated features.
- [x] Use responsive Qt layouts, sensible minimum window sizes, high-DPI scaling, clear loading/empty/error states, keyboard navigation and restrained animations. No perpetual animations or expensive effects when idle. Keep destructive actions distinct from primary actions.
- [x] Verify representative screens at common window sizes and 100/125/150% scaling where available. Capture screenshots locally if supported. Test existing workflows with fixtures, without paid API calls.

## Phase 3 — Performance optimization
Target: Windows 11, Ryzen 5 6600-series CPU, RTX 3050 laptop GPU, 16 GB dual-channel RAM. Detect actual hardware and VRAM instead of assuming capacity. Audit current worker architecture, STT, face tracking, FFmpeg, downloads, caching and analysis before changes.

### Performance profiles
- [x] Add persistent Eco / Balanced / Turbo profiles; Balanced default. Eco: conservative CPU threads, memory-aware batch sizes, lower background priority where supported, and validated tracking sampling. Balanced: adaptive resource allocation and normal priority. Turbo: fastest validated settings within hardware limits, no unsafe priority or overclocking. Do not silently downgrade model, resolution, caption accuracy or output quality; expose quality tradeoffs explicitly.
- [x] Apply profiles to actual workers and subprocesses, not just UI labels. Changes affecting active jobs apply at safe boundaries; report when restart is required.

### Hardware monitoring and scheduling
- [x] Use psutil for CPU/RAM and NVIDIA NVML via nvidia-ml-py for GPU utilization, VRAM, temperature and power where supported. Poll approximately once per second when needed; stop unnecessary polling on shutdown. Show N/A for unavailable sensors, especially CPU temperature; never fabricate metrics.
- [x] Avoid concurrent heavy GPU jobs; coordinate local Whisper, tracking and rendering with a resource-aware queue. Bound CPU threads and memory; keep UI responsive. Release model references and subprocess resources when no longer needed. Handle CUDA OOM with bounded batch reduction and optional explicitly configured CPU fallback. Preserve cancellation and clean shutdown.
- [x] Add optional thermal protection with configurable thresholds, hysteresis and cooldown at safe job boundaries. Do not manipulate BIOS, fan speeds, voltage, clocks, Windows power plans or NitroSense. Never promise protection when sensors are unavailable.
- [x] Add optional Gaming Mode (off by default): detect TslGame.exe by process name only, defer new heavy jobs, checkpoint active work only where supported, and optionally resume after exit. Never inject into, suspend, modify or inspect game memory or interfere with anti-cheat.

### Pipeline efficiency
- [x] Reuse Subtitle Edit-inspired Purfview/Faster-Whisper implementation and compatible model caches. Benchmark supported CUDA compute types and batch sizes; avoid unnecessary audio conversion and repeated transcription. Preserve real word timestamps and subtitle accuracy. Cloud STT must remain available; never silently switch to paid services.
- [x] Optimize FFmpeg using validated NVENC paths and sensible CPU fallback, avoiding redundant encoding and disk I/O. Preserve crop, audio sync, captions and export quality. Benchmark tracking sampling and reuse detection results where valid.
- [x] Cache transcript, metadata and analysis using source fingerprints, relevant settings and schema versions. Invalidate correctly. Avoid repeated full-transcript API requests and redundant context; preserve highlight quality. Use fixtures for cloud tests and never log credentials.
- [x] Provide a compact performance settings panel with profile descriptions, live metrics, thermal and Gaming Mode controls. Keep monitoring lightweight and aligned with Phase 2 branding.

### Benchmark and acceptance
- [x] Use the same available local test clip and equivalent output settings for baseline and each profile. Measure wall time by stage, peak RAM/VRAM, CPU/GPU utilization, available temperatures, UI responsiveness and output quality. Record hardware, model, settings and sample duration. Do not invent results or claim improvements without measurements. If baseline or GPU testing is unavailable, document why.
- [x] Verify cancellation, repeated jobs, cache invalidation, missing sensors, simulated OOM, gaming detection and no orphan processes. Run existing tests and relevant smoke tests. Keep benchmark artifacts out of Git and avoid deleting user media.

## Phase 4 — Automatic clip thumbnail generation
- [x] Automatically extract and score candidate frames locally via FFmpeg/OpenCV using the final clip crop/aspect ratio, avoiding black/blurry/transition frames and falling back to midpoint.
- [x] Preserve original extracted frame separately from styled/preview thumbnails; support optional 1080x1920 export and optional title overlay using caption typography and red brand guidelines.
- [x] Store thumbnails in the project directory, persist metadata in SQLite, cache with invalidation, support existing clips without thumbnails, and delete thumbnail files on clip deletion.
- [x] Update Generated Clips UI with unique thumbnails, duration/status badges, loading skeleton, Regenerate Thumbnail, Select Frame timeline dialog, and Export Thumbnail actions.
- [x] Run generation asynchronously with profile-aware concurrency limits without blocking UI or consuming AI API tokens; validate multi-speaker, dark, fast-motion, and no-face clips.

## Final handoff
- [x] Update AGENTS.md with concise architectural changes and operational notes, if appropriate.
- [x] Record each phase as complete, partial or blocked, changed files, tests actually run, benchmark results, known limitations and any decisions made without user guidance.
- [x] Confirm no secrets, generated media, model weights or virtual environments were added to Git. Review diff for unrelated changes. Leave changes ready for user review; suggested commits: `feat: integrate AutoClip Studio brand assets`, `refactor: unify red SaaS design system`, `feat: optimize adaptive performance profiles`.
- [x] Confirm launcher starts the current source without routine rebuilds. If GUI or clean-install testing cannot be performed, state that explicitly.
