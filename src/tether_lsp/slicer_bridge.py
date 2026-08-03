import ast
from tether.slicer import (
    _tether_import_aliases,
    _is_mcu_decorator,
    _is_pc_decorator,
    _referenced_names,
    _collect_bindings,
    _top_level_decorated_functions
)

def _decorated_side(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    if any(_is_mcu_decorator(dec, aliases) for dec in node.decorator_list):
        return "mcu"
    if any(_is_pc_decorator(dec, aliases) for dec in node.decorator_list):
        return "pc"
    return None


def _closure(roots: list[ast.stmt], bindings: dict, aliases: dict[str, str], side: str) -> dict:
    """Transitive dependency closure starting from `roots`, stopping at any
    function decorated for the OTHER side rather than walking into its body.

    Without that stop, a helper reachable only through the other side's
    dispatch boundary gets pulled in anyway - e.g. computing the PC closure
    from `if __name__ == "__main__":` (a real PC root) walks into `blink()`
    (an @mcu.export function it calls) and, without this guard, would keep
    walking into whatever `blink()` itself references - including
    `_get_led()`, a plain helper that only ever runs inside `blink()`'s body
    and is never reachable from real PC code. `blink()`'s own presence still
    belongs in the closure (its signature line stays visible either way);
    only recursing into ITS references is what's wrong here.
    """
    included = {}
    pending = list(roots)
    while pending:
        node = pending.pop()
        if node in included:
            continue
        included[node] = None
        if _decorated_side(node, aliases) not in (None, side):
            continue
        for name in _referenced_names(node):
            dep = bindings.get(name)
            if dep is not None:
                pending.append(dep)
    return included


def generate_virtual_documents(source: str) -> tuple[str, str]:
    """
    Returns (mcu_source, pc_source).
    Replaces excluded bodies or statements with whitespace to preserve line numbers.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # If there's a syntax error, we can't slice it. Just return the source as is
        # so the underlying LSP can report the syntax error on the exact line.
        return source, source

    aliases = _tether_import_aliases(tree)

    bindings = {}
    _collect_bindings(tree, None, bindings, visited_files=set())
    mcu_exported = _top_level_decorated_functions(tree, aliases, _is_mcu_decorator)
    pc_exported = _top_level_decorated_functions(tree, aliases, _is_pc_decorator)

    included_mcu = _closure(mcu_exported, bindings, aliases, "mcu")

    # PC roots: every @pc.export function, plus any top-level statement that
    # isn't itself a function/class definition - module-level code (imports,
    # assignments, `if __name__ == "__main__":`) runs unconditionally on the
    # PC by construction, so whatever it references is genuinely PC-reachable.
    # A plain `def` is deliberately NOT auto-included here: it only belongs
    # in the closure if something actually calls it.
    pc_roots = list(pc_exported) + [
        node for node in tree.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    included_pc = _closure(pc_roots, bindings, aliases, "pc")

    mcu_lines = source.split('\n')
    pc_lines = source.split('\n')

    for node in tree.body:
        is_mcu_func = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(_is_mcu_decorator(dec, aliases) for dec in node.decorator_list)
        is_pc_func = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(_is_pc_decorator(dec, aliases) for dec in node.decorator_list)

        # MCU Document mutations
        if is_pc_func:
            _blank_body(mcu_lines, node)
            _make_mcu_side_pc_export_awaitable(mcu_lines, node)
        elif node not in included_mcu and not is_mcu_func and not _is_tether_namespace_import(node, aliases):
            # If it's a top-level statement (like `import os`) that the MCU doesn't need,
            # we blank out the whole statement to avoid false "import missing" errors.
            #
            # The `from tether import mcu, pc` import itself is excluded from
            # tether.slicer's own bindings graph on purpose (it's PC-only - the
            # real device bundle never carries it), so it never shows up as a
            # dependency of any @mcu.export function and would get blanked here
            # too. But every decorator line (`@mcu.export`, `@pc.export`) still
            # references the `mcu`/`pc` names in the virtual document text - so
            # blanking it made pyright report them "not defined" on every
            # decorated function. Always keep it, regardless of the bundling
            # dependency graph.
            _blank_node_fully(mcu_lines, node)

        # PC Document mutations
        if is_mcu_func:
            _blank_body(pc_lines, node)
        elif node not in included_pc and not is_pc_func and not _is_tether_namespace_import(node, aliases):
            # Symmetric case: a plain helper reachable only from inside an
            # @mcu.export function's body (e.g. `_get_led()`, which only
            # `blink()` calls) - real PC code never reaches it, so its
            # hardware-only imports shouldn't be analyzed as PC-side errors.
            _blank_node_fully(pc_lines, node)

    return "\n".join(mcu_lines), "\n".join(pc_lines)

def _is_tether_namespace_import(node: ast.AST, aliases: dict[str, str]) -> bool:
    """True for the `from tether import mcu, pc` (or aliased) statement that
    `aliases` was built from - see generate_virtual_documents' MCU-side note.
    """
    if not isinstance(node, ast.ImportFrom):
        return False
    return any((alias.asname or alias.name) in aliases for alias in node.names)


def _make_mcu_side_pc_export_awaitable(lines: list[str], node: ast.AST):
    """On the real device, a `@pc.export`-decorated function is replaced at
    call sites with an auto-generated async RPC proxy (tether-lsp's own
    subserver_manager.py docstring: "MCU side gets an auto-generated proxy
    stub") - device-bound code correctly `await`s it, and this file's own
    slicer.py excludes the plain-`def` body from the MCU bundle for exactly
    that reason. But `_PcNamespace.export` genuinely returns `fn` completely
    unchanged (a plain sync function, since that's what's accurate when
    this same file runs directly on the PC) - so without this rewrite, the
    MCU virtual document (which reuses the PC-typed decorator) sees a
    synchronous callable and flags the device-bound code's own
    `await log_progress(...)` as invalid. Prefixing the `def` line with
    `async` here only affects the MCU document's view of the callable's
    type; the body is already blanked to `pass` regardless, and the PC
    document is untouched.
    """
    if not isinstance(node, ast.FunctionDef):
        return  # already async (AsyncFunctionDef), nothing to fix
    line = lines[node.lineno - 1]
    idx = line.index("def ")
    lines[node.lineno - 1] = line[:idx] + "async " + line[idx:]


def _blank_body(lines: list[str], node: ast.AST):
    if not hasattr(node, 'body') or not node.body:
        return
    start = node.body[0].lineno - 1
    end = node.body[-1].end_lineno
    
    indent = " " * node.body[0].col_offset
    lines[start] = indent + "pass"
    for i in range(start + 1, end):
        lines[i] = ""

def _blank_node_fully(lines: list[str], node: ast.AST):
    if not hasattr(node, 'lineno') or not hasattr(node, 'end_lineno'):
        return
    start = node.lineno - 1
    end = node.end_lineno
    for i in range(start, end):
        lines[i] = ""
