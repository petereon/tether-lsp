import logging
from pygls.lsp.server import LanguageServer
from lsprotocol.converters import get_converter
from lsprotocol.types import (
    TEXT_DOCUMENT_DID_OPEN,
    TEXT_DOCUMENT_DID_CHANGE,
    TEXT_DOCUMENT_DID_CLOSE,
    TEXT_DOCUMENT_DEFINITION,
    TEXT_DOCUMENT_HOVER,
    DidOpenTextDocumentParams,
    DidChangeTextDocumentParams,
    DidCloseTextDocumentParams,
    DefinitionParams,
    HoverParams,
)
from tether_lsp.slicer_bridge import generate_virtual_documents
from tether_lsp.subserver_manager import SubserverManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
_converter = get_converter()

server = LanguageServer("tether-lsp", "v0.1")
manager: SubserverManager | None = None


def _get_manager(ls: LanguageServer) -> SubserverManager:
    # Built lazily on first didOpen rather than at import time: pyright's
    # own venv auto-detection (an unadorned `.venv` next to the analyzed
    # file) only kicks in when the subserver is initialized with the real
    # workspace's rootUri - and that rootUri isn't known until the client's
    # `initialize` request lands, which happens well after module import.
    # `ls.workspace` raises until then too, so this can't run any earlier
    # than the first document event.
    global manager
    if manager is None:
        manager = SubserverManager(ls)
    return manager


@server.feature(TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LanguageServer, params: DidOpenTextDocumentParams):
    uri = params.text_document.uri
    source = params.text_document.text
    logger.info(f"Opened {uri}")

    mcu_source, pc_source = generate_virtual_documents(source)
    _get_manager(ls).update_virtual_document(uri, params.text_document.version, mcu_source, pc_source)

@server.feature(TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LanguageServer, params: DidChangeTextDocumentParams):
    uri = params.text_document.uri
    # For simplicity, we assume full text sync.
    # (In a production LSP, you'd apply incremental changes to a document buffer)
    if not params.content_changes:
        return

    source = params.content_changes[0].text
    logger.info(f"Changed {uri}")

    mcu_source, pc_source = generate_virtual_documents(source)
    # Re-using didOpen on the sub-server side effectively updates the buffer in pyright
    _get_manager(ls).update_virtual_document(uri, params.text_document.version, mcu_source, pc_source)

@server.feature(TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: LanguageServer, params: DidCloseTextDocumentParams):
    uri = params.text_document.uri
    logger.info(f"Closed {uri}")
    if manager is not None:
        manager.close_virtual_document(uri)

@server.feature(TEXT_DOCUMENT_DEFINITION)
def definition(ls: LanguageServer, params: DefinitionParams):
    # Both virtual documents keep the original file's line numbers (see
    # slicer_bridge's padding), so the position needs no translation -
    # forward_request queries both and keeps whichever wasn't looking at a
    # blanked-out `pass`.
    return _get_manager(ls).forward_request("textDocument/definition", _converter.unstructure(params))

@server.feature(TEXT_DOCUMENT_HOVER)
def hover(ls: LanguageServer, params: HoverParams):
    return _get_manager(ls).forward_request("textDocument/hover", _converter.unstructure(params))

def main():
    logger.info("Starting tether-lsp server...")
    server.start_io()

if __name__ == "__main__":
    main()
