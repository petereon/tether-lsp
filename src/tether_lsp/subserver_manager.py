import os
import shutil
import subprocess
import sys
import threading
import json
import logging
from pathlib import Path
from pygls.lsp.server import LanguageServer
from lsprotocol.converters import get_converter
from lsprotocol.types import Diagnostic, PublishDiagnosticsParams

logger = logging.getLogger(__name__)
_converter = get_converter()


def _mcu_stub_path() -> str | None:
    """Directory of MicroPython `.pyi` stubs (`machine.pyi`, `time.pyi`, ...)
    for the MCU-side pyright subserver's `python.analysis.stubPath` - kept
    out of tether-lsp's own venv on purpose, since anything installed there
    would leak into the PC-side subserver too (same site-packages), defeating
    the whole "machine resolves under @mcu.export, not @pc.export" point of
    running two subservers. See implementation-plan.md's micropython-stubs
    open question - v1 assumes they're installed and just needs the path.
    """
    env_path = os.environ.get("TETHER_LSP_MCU_STUBS_PATH")
    if env_path:
        return env_path
    default = Path(__file__).resolve().parents[2] / ".mcu-stubs"
    return str(default) if default.is_dir() else None


def _project_python_path(root_path: str | None) -> str | None:
    """Absolute path to `<project root>/.venv/bin/python`, if it exists.

    Pyright's documented "auto-detect a sibling `.venv`" convention turned
    out to need a `pyrightconfig.json`/`pyproject.toml` marker to kick in -
    empirically, a bare `.venv` directory next to rootUri is NOT enough (
    verified against pyright-langserver directly: with nothing but rootUri
    pointing at a project containing only a `.venv`, `import tether` from
    that same venv's own site-packages still came back unresolved). Pushing
    `python.pythonPath` explicitly is what actually works.
    """
    if not root_path:
        return None
    candidate = Path(root_path) / ".venv" / "bin" / "python"
    return str(candidate) if candidate.is_file() else None


def _pyright_langserver_command() -> str:
    """Absolute path to `pyright-langserver` next to the running interpreter.

    A bare `"pyright-langserver"` relies on `$PATH` containing this venv's
    `bin/` - true under `uv run` (which prepends it for the whole process
    tree) but NOT when a client (e.g. the VS Code extension) spawns the
    `tether-lsp` console-script directly by its absolute venv path: that
    doesn't put the venv's `bin/` on `PATH`, so `subprocess.Popen` fails with
    `FileNotFoundError`. `sys.executable`'s own directory is always the
    right place to look, regardless of how this process was launched.
    """
    candidate = Path(sys.executable).parent / "pyright-langserver"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("pyright-langserver")
    if found:
        return found
    raise FileNotFoundError(
        "pyright-langserver not found next to the Python interpreter "
        f"({candidate}) or on $PATH - is `pyright` installed in this venv?"
    )


def _is_empty_result(result) -> bool:
    # Covers null, `[]` (definition/references with no hits), and `{}`.
    return result is None or (isinstance(result, (list, dict)) and len(result) == 0)


def _pick_result(mcu_result, pc_result):
    if not _is_empty_result(mcu_result):
        return mcu_result
    if not _is_empty_result(pc_result):
        return pc_result
    return None


class Subserver:
    def __init__(
        self,
        name: str,
        command: list[str],
        manager: 'SubserverManager',
        root_uri: str | None = None,
        python_path: str | None = None,
        stub_path: str | None = None,
    ):
        self.name = name
        self.manager = manager
        # Settings pyright pulls back on `workspace/configuration` requests -
        # see _handle_message. `pythonPath` is what makes it use the real
        # project venv (see _project_python_path); `analysis.stubPath` is
        # the MCU-only override that additionally makes hardware modules
        # resolve there (unlike `extraPaths`, which only adds to third-party
        # search - a stdlib name like `time` still resolves from pyright's
        # bundled typeshed first, ignoring extraPaths entirely). Both
        # empirically verified against pyright-langserver directly before
        # wiring them in here - see implementation-plan.md's open question.
        self._python_settings: dict = {}
        if python_path:
            self._python_settings["pythonPath"] = python_path
        if stub_path:
            # reportMissingModuleSource fires whenever a stub (.pyi) has no
            # matching real .py source behind it - always true for these
            # MCU stubs, since `machine` etc. are C modules baked into
            # MicroPython firmware with no installable source to point at.
            # Not actionable, so silenced here specifically (PC's own
            # `machine` resolution failure must stay a real, visible error).
            self._python_settings["analysis"] = {
                "stubPath": stub_path,
                "diagnosticSeverityOverrides": {"reportMissingModuleSource": "none"},
            }

        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.request_id = 0
        # Tracks our own outstanding requests (definition/hover/...) so their
        # responses - which arrive on the reader thread - can be handed back
        # to whichever caller is blocked waiting in request_and_wait().
        self._id_lock = threading.Lock()
        self._pending: dict[int, threading.Event] = {}
        self._results: dict[int, dict] = {}

        self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.reader_thread.start()

        # Send initialize. `workspace.configuration: true` is required for
        # pyright to pull settings via `workspace/configuration` at all - the
        # push-only `workspace/didChangeConfiguration` notification below is
        # not observed to take effect on its own.
        self.send_request("initialize", {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": {
                "workspace": {
                    "configuration": True,
                    "didChangeConfiguration": {"dynamicRegistration": True},
                }
            },
        })
        self.send_notification("initialized", {})
        if self._python_settings:
            self.send_notification("workspace/didChangeConfiguration", {
                "settings": {"python": self._python_settings}
            })

    def send_notification(self, method: str, params: dict):
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params
        }
        self._write(msg)

    def send_request(self, method: str, params: dict):
        self.request_id += 1
        msg = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": params
        }
        self._write(msg)
        return self.request_id

    def send_tracked_request(self, method: str, params: dict) -> int:
        """Send a request, registering it so `wait_for` can later retrieve
        its response. Split from waiting so `SubserverManager.forward_request`
        can fire this at both subservers before blocking on either - the
        event must be registered before `_write` returns control to the
        reader thread, or a fast subserver could deliver its response before
        `_pending` even knows to expect it.
        """
        with self._id_lock:
            self.request_id += 1
            req_id = self.request_id
            self._pending[req_id] = threading.Event()
        self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return req_id

    def wait_for(self, req_id: int, timeout: float = 5.0) -> dict | None:
        with self._id_lock:
            event = self._pending.get(req_id)
        if event is None or not event.wait(timeout):
            with self._id_lock:
                self._pending.pop(req_id, None)
                self._results.pop(req_id, None)
            return None
        with self._id_lock:
            self._pending.pop(req_id, None)
            return self._results.pop(req_id, None)

    def _write(self, msg: dict):
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        if self.process.stdin:
            self.process.stdin.write(header + body)
            self.process.stdin.flush()

    def _read_loop(self):
        while True:
            if not self.process.stdout:
                break
            line = self.process.stdout.readline()
            if not line:
                break
            line = line.decode("utf-8")
            if line.startswith("Content-Length:"):
                length = int(line.split(":")[1].strip())
                # Read the empty line
                self.process.stdout.readline()
                # Read body
                body = self.process.stdout.read(length)
                try:
                    self._handle_message(json.loads(body))
                except json.JSONDecodeError:
                    pass

    def _handle_message(self, msg: dict):
        if "method" not in msg and "id" in msg:
            # A response to one of OUR requests (initialize, or anything sent
            # via send_tracked_request) - distinguished from an incoming
            # request/notification FROM the subserver by having no "method".
            req_id = msg["id"]
            with self._id_lock:
                event = self._pending.get(req_id)
                if event is not None:
                    self._results[req_id] = msg.get("result")
                    event.set()
            return
        if msg.get("method") == "textDocument/publishDiagnostics":
            params = msg.get("params", {})
            uri = params.get("uri", "")
            diagnostics = params.get("diagnostics", [])
            self.manager.on_diagnostics(self.name, uri, diagnostics)
        elif msg.get("method") == "workspace/configuration" and "id" in msg:
            # pyright pulls settings per requested section instead of relying
            # solely on the didChangeConfiguration push - an unanswered pull
            # request here is what silently no-ops the stubPath setting.
            items = msg.get("params", {}).get("items", [])
            result = []
            for item in items:
                section = item.get("section", "")
                if section == "python":
                    result.append(self._python_settings)
                elif section == "python.analysis":
                    result.append(self._python_settings.get("analysis", {}))
                else:
                    result.append(None)
            self._write({"jsonrpc": "2.0", "id": msg["id"], "result": result})


class SubserverManager:
    def __init__(self, ls: LanguageServer):
        self.ls = ls
        self.diagnostics_cache = {} # uri -> { 'mcu': [], 'pc': [] }
        
        # Real project root/venv, so pyright resolves the project's own
        # packages (e.g. `tether` itself) instead of falling back to
        # whatever's on $PATH - see _project_python_path.
        root_uri = ls.workspace.root_uri
        python_path = _project_python_path(ls.workspace.root_path)
        if python_path is None:
            logger.warning(
                f"No .venv/bin/python found under workspace root "
                f"({ls.workspace.root_path}) - diagnostics may not see "
                "project dependencies like `tether` itself."
            )

        # MCU additionally gets micropython-stubs as its analysis stubPath
        # (so `import machine` resolves there and nowhere else); PC stays on
        # just the project venv with no override.
        stub_path = _mcu_stub_path()
        if stub_path is None:
            logger.warning(
                "No MCU stub path found (checked $TETHER_LSP_MCU_STUBS_PATH and "
                "<repo>/.mcu-stubs) - MCU-side diagnostics won't know about "
                "machine/micropython modules."
            )
        pyright_cmd = _pyright_langserver_command()
        self.mcu_server = Subserver(
            "mcu", [pyright_cmd, "--stdio"], self,
            root_uri=root_uri, python_path=python_path, stub_path=stub_path,
        )
        self.pc_server = Subserver(
            "pc", [pyright_cmd, "--stdio"], self,
            root_uri=root_uri, python_path=python_path,
        )
        
    def update_virtual_document(self, uri: str, version: int, mcu_source: str, pc_source: str):
        # Send didOpen to subservers. Pyright will use this in-memory source instead of disk.
        self.mcu_server.send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "python",
                "version": version,
                "text": mcu_source
            }
        })
        self.pc_server.send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "python",
                "version": version,
                "text": pc_source
            }
        })

    def close_virtual_document(self, uri: str):
        for server in [self.mcu_server, self.pc_server]:
            server.send_notification("textDocument/didClose", {
                "textDocument": {"uri": uri}
            })
        if uri in self.diagnostics_cache:
            del self.diagnostics_cache[uri]

    def forward_request(self, method: str, params: dict, timeout: float = 5.0):
        """Send a position-based request (definition, hover, ...) to both
        virtual documents and return whichever answered with something real.

        Both virtual documents share the original file's line numbers (the
        whole point of slicer_bridge's padding), so the caller's position
        needs no translation - but exactly one of the two subservers has
        real code at that position (the other sees a blanked-out `pass`),
        so its response is the one worth keeping. `_pick_result` picks the
        non-empty one; if both are non-empty (shared/common code, outside
        either blanked region) MCU wins arbitrarily.
        """
        mcu_id = self.mcu_server.send_tracked_request(method, params)
        pc_id = self.pc_server.send_tracked_request(method, params)
        mcu_result = self.mcu_server.wait_for(mcu_id, timeout)
        pc_result = self.pc_server.wait_for(pc_id, timeout)
        return _pick_result(mcu_result, pc_result)

    def on_diagnostics(self, server_name: str, uri: str, diagnostics: list):
        if uri not in self.diagnostics_cache:
            self.diagnostics_cache[uri] = {"mcu": [], "pc": []}
            
        self.diagnostics_cache[uri][server_name] = diagnostics
        
        # Merge and publish
        merged = self.diagnostics_cache[uri]["mcu"] + self.diagnostics_cache[uri]["pc"]

        # pygls 2.x's LanguageServer has no publish_diagnostics(uri, list) shortcut -
        # text_document_publish_diagnostics takes a typed PublishDiagnosticsParams, so
        # raw dicts from the pyright subservers' wire JSON need structuring first.
        diagnostics = [_converter.structure(d, Diagnostic) for d in merged]
        self.ls.text_document_publish_diagnostics(
            PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
        )
