# Goal Description
Build a "Meta Language Server" (`tether-lsp`) that sits between the editor and underlying Python language servers to provide accurate, environment-aware linting and autocomplete for `tether`'s mixed MCU/PC codebases.

## User Review Required
> [!IMPORTANT]
> The primary underlying LSP needs to be decided. Following your decision, we will use **`pyright`** as the underlying engine. We will use `pygls` as the framework for `tether-lsp` and spawn `pyright` subprocesses for the virtual documents.

## Open Questions
> [!WARNING]
> 1. Should we assume the user has `micropython-stubs` installed in their environment, or should `tether-lsp` attempt to bundle/download them automatically for the MCU environment? (For v1, we will assume they are installed and configure the path).
>
> **Resolved (v1):** assume-installed, path-configured - implemented in
> `subserver_manager.py`. Stubs must NOT go into `tether-lsp`'s own venv
> (they'd leak into the PC subserver via shared site-packages); install them
> with `uv pip install --target .mcu-stubs --no-deps <board>-stubs
> micropython-stdlib-stubs` and the MCU subserver picks up `.mcu-stubs`
> automatically (or `$TETHER_LSP_MCU_STUBS_PATH`). Wired via pyright's
> `python.analysis.stubPath` setting - `extraPaths` was tried first and
> doesn't work for this, since it only extends third-party module search;
> stdlib-shadowing names like `time` still resolve from pyright's bundled
> typeshed first and ignore `extraPaths` entirely. `stubPath` does override
> stdlib resolution. The setting only takes effect via the LSP
> `workspace/configuration` pull (declare `workspace.configuration: true` in
> the subserver's `initialize` capabilities and answer its request) - passing
> it through `initializationOptions` or a push-only
> `workspace/didChangeConfiguration` was tried first and silently ignored.

## Proposed Changes

### Project Setup
#### [MODIFY] [pyproject.toml](file:///Users/petervyboch/Projects/tether-lsp/pyproject.toml)
- Add dependencies: `pygls` for the Language Server framework, `pyright` for the type checking engine, and an editable install of `tether` to reuse the AST slicer.

### Core LSP Logic
#### [NEW] [server.py](file:///Users/petervyboch/Projects/tether-lsp/src/tether_lsp/server.py)
- Main entry point for the LSP using `pygls`.
- Handles `textDocument/didOpen`, `textDocument/didChange`, `textDocument/didClose`.
- Implements the "Virtual Document" router: hooking into file changes to trigger slicing.

#### [NEW] [slicer_bridge.py](file:///Users/petervyboch/Projects/tether-lsp/src/tether_lsp/slicer_bridge.py)
- Wraps `tether.slicer` to extract the `@mcu.export` and `@pc.export` blocks.
- **Padding:** Crucially, pads the sliced code with whitespace/newlines so that line numbers match the original document 1:1, allowing diagnostics to map back correctly.

#### [NEW] [subserver_manager.py](file:///Users/petervyboch/Projects/tether-lsp/src/tether_lsp/subserver_manager.py)
- Manages the lifecycle of the underlying **`pyright`** processes (one for MCU, one for PC).
- Forwards LSP JSON-RPC requests (like completions, hovers) to the appropriate subserver based on the cursor's line number.
- Merges `textDocument/publishDiagnostics` notifications from both subservers and forwards the combined list to the editor.

## Verification Plan
### Automated Tests
- Create a test suite that sends mock `didOpen` events and verifies that the `slicer_bridge` correctly generates padded virtual documents.
- Verify that diagnostic merging correctly handles empty diagnostic arrays from `pyright`.

### Manual Verification
- Install `tether-lsp` locally.
- Configure an editor (like VS Code or Neovim) to use `tether-lsp` for `.py` files in a sample `tether` project.
- Verify that `import machine` inside `@mcu.export` is valid, but inside `@pc.export` it correctly flags an import error.
