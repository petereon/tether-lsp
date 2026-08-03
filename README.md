# tether-lsp

A meta language server for `tether`'s mixed MCU/PC files. It slices each
open document into two virtual copies (MCU-bound code, PC-bound code —
see `src/tether_lsp/slicer_bridge.py`, which reuses `tether.slicer`'s
dependency-walking logic), feeds each copy to its own `pyright` subprocess
(`src/tether_lsp/subserver_manager.py`), and merges the two sets of
diagnostics back onto the one real document.

## Setup

```bash
uv sync
```

The MCU-side subserver needs MicroPython stubs (`machine`, `time.sleep_ms`,
...) on a path that's kept **outside** this project's own venv — anything
installed into `.venv` would leak into the PC-side subserver too (same
site-packages), which would silently defeat the whole point of running two
subservers (`import machine` should resolve for `@mcu.export` code and fail
for `@pc.export` code). Regenerate that directory with:

```bash
uv pip install --target .mcu-stubs --no-deps micropython-esp32-stubs micropython-stdlib-stubs
```

(swap `micropython-esp32-stubs` for whatever board package matches your
target). `subserver_manager.py` looks for `.mcu-stubs` next to the project
root by default, or `$TETHER_LSP_MCU_STUBS_PATH` if set.

## Running standalone

```bash
uv run tether-lsp
```

Talks LSP over stdio - see `tether-vscode` for a client that drives it.
