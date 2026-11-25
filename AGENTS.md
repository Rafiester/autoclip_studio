# OShorts development instructions

## Select skills at the start of each task

1. Identify the requested outcome, the code it affects, and the platform or format of the deliverable. Select skills by that combination.
2. Apply any skill the user explicitly requests. Otherwise, use the most specific match below. Add a second skill only when it covers a distinct part of the work. A routine bug fix, test, build change, or documentation edit can proceed with zero skills.
3. Announce the selected skill the first time you apply it, then read its linked `SKILL.md` before using its workflow. Read referenced files or run skill utilities only as needed for the current task. Resolve their paths from the installed skill directory and use PowerShell and the project Python when applicable.
4. Reevaluate the selection if the task changes. If a local skill is missing, look for the same skill in the active skill catalog. If no copy is available, use the repository and platform guidance when the task can proceed without it.

| Task | Skill |
| --- | --- |
| Review or improve desktop interaction, accessibility, layout, or usability | [ui-ux-pro-max](.agents/skills/ui-ux-pro-max/SKILL.md). Use targeted UX guidance that applies to Qt Widgets. |
| Establish a new visual direction or substantially redesign an existing interface | [frontend-design](.agents/skills/frontend-design/SKILL.md). Implement desktop changes in the existing PySide6 UI. |
| Establish shared design tokens or component specifications | [design-system](.agents/skills/design-system/SKILL.md). Apply the concept through the target stack, such as Qt stylesheets. |
| Style a web deliverable using Tailwind, shadcn/ui, or canvas | [ui-styling](.agents/skills/ui-styling/SKILL.md). |
| Profile Python latency, CPU, memory, I/O, or pipeline orchestration | [python-performance-optimization](.agents/skills/python-performance-optimization/SKILL.md). |
| Improve agent context selection, retrieval, or token use | [context-optimization](.agents/skills/context-optimization/SKILL.md). |
| Create or audit brand voice, identity guidelines, or brand consistency | [brand](.agents/skills/brand/SKILL.md). |
| Produce a logo, icon set, corporate identity package, or social imagery | [design](.agents/skills/design/SKILL.md). Use the dedicated skill for a standalone banner, deck, or token system. |
| Produce a banner for promotion, social media, web, or print | [banner-design](.agents/skills/banner-design/SKILL.md). |
| Produce an HTML presentation | [slides](.agents/skills/slides/SKILL.md). |

For work focused on FFmpeg, encoders, or GPU compatibility, inspect the bundled binaries and native Windows runtime and consult the relevant primary documentation. Use the Python performance skill when the task also covers Python profiling or orchestration.

## Preserve the Windows application

- Develop and validate in native Windows PowerShell or cmd with the existing PySide6 app, project-local Python, and media tools. Keep the source launchers and PyInstaller OneDir workflow working. WSL and Docker are outside the supported workflow.
- Make incremental changes. Preserve existing user workflows, CPU fallbacks, clip and caption behavior, exported data, settings, SQLite compatibility, Windows Credential Manager integration, and owned-process cleanup unless the task explicitly changes a behavior.
- For optimization work, present a prioritized plan before editing application source. Use comparable inputs, record cold and warm state, and measure the affected stages. For hardware work, distinguish advertised support from a successful runtime test and check the CPU path.
- Use `.\.venv\Scripts\python.exe -m pytest` for the unit suite and `.\.venv\Scripts\python.exe -m tests.integration_smoke` for media changes. Check the relevant native window or packaged entry point when changing UI, startup, shutdown, or dependencies.

## Commit completed changes

- After validating each task that changes repository files, create a local Git commit for the task before reporting completion. Stage only the task's changes unless the user asks to include other work. Include the commit hash in the handoff. Push only when the user requests it.
