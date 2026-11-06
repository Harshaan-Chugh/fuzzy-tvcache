"""Filesystem/terminal canonicalization for cat/ls/echo/cd (owner: Etienne). See docs/interfaces.md.

A command is only analyzed if bash would read it as one simple command made of
literal words. Anything that could expand, chain, or redirect in a way this
module does not model is rejected before the command name is looked at, because
`ls>out` and `cat a;rm a` must never look like reads.

Paths are resolved lexically, so resolving `..` needs care: `link/../f` and
`missing/../f` are not `f`. Only leading `..` is collapsed (against the cwd).
A trailing slash or dot on a file operand is rejected: `cat f/` fails, `cat f` does not.

`ls` output depends on more than the entry names when its operand is not a
directory: a file operand prints the path as typed, and `echo > x` must
invalidate `ls x`. So unless the operand is known to be a directory (the cwd or
an ancestor of it), `ls` also reads the file key and keeps the typed spelling.

Assumptions (otherwise a fuzzy hit may be stale), see docs/state-tracking.md:
  - Paths involve only regular files and directories, with no symlinks.
  - `CallContext.cwd` is the shell's real logical cwd, and CDPATH is unset.
  - cat/ls/echo/cd are not aliased or redefined; `ls` formatting and sort order
    are fixed for the session (terminal width, locale).
  - Failed `cat` results are not reused across spellings. The error names the
    path as typed, and this module cannot see whether a file exists.
"""

import posixpath
from shlex import quote
from typing import Any, NoReturn

from fuzzytv.interfaces import CWD, CallAnalysis, CallContext, fs_dir, fs_file, unknown

READ_ONLY_COMMANDS = {"grep", "head", "tail", "wc"}  # unsupported, but cannot write without a redirect
VOLATILE_ROOTS = ("/dev", "/proc", "/sys")

UNQUOTED_REJECTS = {
    **dict.fromkeys(";&|<()", "shell control operator"),
    **dict.fromkeys("$`", "shell expansion"),
    **dict.fromkeys("*?[", "glob"),
    "~": "tilde expansion",
    "#": "shell comment",
    "!": "history expansion",
    "\\": "backslash escape",
}
DOUBLE_QUOTED_REJECTS = {
    **dict.fromkeys("$`", "shell expansion"),
    "!": "history expansion",
    "\\": "backslash escape",
}


class _Rejected(Exception):
    """The command cannot be analyzed; carries why."""


class FsCanonicalizer:
    """Canonicalizer for single shell commands in `call["command"]`."""

    def analyze(self, call: dict[str, Any], ctx: CallContext) -> CallAnalysis:
        command = call.get("command") if isinstance(call, dict) else None
        if not isinstance(command, str):
            return unknown("no command text")
        try:
            words, redirect = _tokenize(command)
        except _Rejected as error:
            return unknown(str(error))
        if not words:
            return unknown("empty command")
        name, args = words[0], words[1:]
        if redirect is not None and name != "echo":
            return unknown(f"redirection on {name}")
        if name == "echo":
            return _echo(args, redirect, ctx)
        if name == "cd":
            return _cd(args, ctx)
        if name == "cat":
            return _cat(args, ctx)
        if name == "ls":
            return _ls(args, ctx)
        if name in READ_ONLY_COMMANDS:
            return _read_unknown(f"unsupported command: {name}")
        return unknown(f"unsupported command: {name}")


def _read_unknown(reason: str) -> CallAnalysis:
    """Read-only but not understood: ineligible, without affecting later calls."""
    return CallAnalysis(None, reads_unknown=True, reason=reason)


def _cat(args: list[str], ctx: CallContext) -> CallAnalysis:
    if not args:
        return _read_unknown("cat without operands reads stdin")
    flag = _first_flag(args)
    if flag:
        return _read_unknown(f"unsupported flag {flag}")
    try:
        paths = [_resolve(arg, ctx.cwd, is_file=True) for arg in args]
    except _Rejected as error:
        return _read_unknown(str(error))
    return CallAnalysis(
        "cat " + " ".join(map(quote, paths)), reads=frozenset(map(fs_file, paths))
    )


def _ls(args: list[str], ctx: CallContext) -> CallAnalysis:
    flag = _first_flag(args)
    if flag:
        return _read_unknown(f"unsupported flag {flag}")
    if len(args) > 1:  # prints a header per operand, spelled as typed
        return _read_unknown("ls with multiple operands")
    operand = args[0] if args else "."
    try:
        path = _resolve(operand, ctx.cwd, is_file=False)
    except _Rejected as error:
        return _read_unknown(str(error))
    if _is_known_directory(path, ctx.cwd):
        return CallAnalysis(f"ls {quote(path)}", reads=frozenset({fs_dir(path)}))
    canonical = f"ls {quote(path)}"
    if operand != path:
        canonical += f" # as {quote(operand)}"
    return CallAnalysis(canonical, reads=frozenset({fs_dir(path), fs_file(path)}))


def _echo(args: list[str], redirect: str | None, ctx: CallContext) -> CallAnalysis:
    if redirect is None:
        return CallAnalysis(" ".join(["echo", *map(quote, args)]))
    try:
        path = _resolve(args[-1], ctx.cwd, is_file=True)
    except _Rejected as error:
        return unknown(str(error))
    text = map(quote, args[:-1])
    return CallAnalysis(
        " ".join(["echo", *text, redirect, quote(path)]),
        writes=frozenset({fs_file(path), fs_dir(posixpath.dirname(path))}),
    )


def _cd(args: list[str], ctx: CallContext) -> CallAnalysis:
    if len(args) != 1 or args[0].startswith("-"):
        return unknown("cd needs exactly one path operand")
    try:
        path = _resolve(args[0], ctx.cwd, is_file=False)
    except _Rejected as error:
        return unknown(str(error))
    return CallAnalysis(f"cd {quote(path)}", writes=frozenset({CWD}))


def _first_flag(args: list[str]) -> str | None:
    return next((arg for arg in args if arg.startswith("-")), None)


def _tokenize(command: str) -> tuple[list[str], str | None]:
    """Split into literal words, as bash would, plus an optional redirect operator.

    With a redirect, the target is the last word. Raises _Rejected unless every
    word is provably literal and any redirect is a trailing `> target`/`>> target`.
    """
    if any((ord(ch) < 32 and ch != "\t") or ord(ch) == 127 for ch in command):
        _reject("control character")

    words: list[str] = []
    redirect: str | None = None
    redirect_at = -1
    word: list[str] | None = None  # None: between words. []: a word, maybe empty ('').
    bare = ""  # the current word's unquoted characters
    quote_char: str | None = None

    def finish_word() -> None:
        nonlocal word, bare
        if word is not None:
            if "{" in bare and ("," in bare or ".." in bare):
                _reject("brace expansion")
            words.append("".join(word))
        word, bare = None, ""

    index = 0
    while index < len(command):
        ch = command[index]
        index += 1
        if quote_char == "'":
            if ch == "'":
                quote_char = None
            else:
                word.append(ch)
        elif quote_char == '"':
            if ch == '"':
                quote_char = None
            elif ch in DOUBLE_QUOTED_REJECTS:
                _reject(DOUBLE_QUOTED_REJECTS[ch])
            else:
                word.append(ch)
        elif ch in " \t":
            finish_word()
        elif ch in "'\"":
            quote_char = ch
            word = [] if word is None else word
        elif ch == ">":
            if word is not None and bare == "".join(word) and bare.isdigit():
                _reject("file descriptor redirection")
            finish_word()
            if redirect is not None:
                _reject("multiple redirections")
            redirect = ">"
            if command[index:index + 1] == ">":
                redirect = ">>"
                index += 1
            redirect_at = len(words)
        elif ch in UNQUOTED_REJECTS:
            _reject(UNQUOTED_REJECTS[ch])
        else:
            word = [] if word is None else word
            word.append(ch)
            bare += ch
    if quote_char is not None:
        _reject("unterminated quote")
    finish_word()
    # `echo a > f` puts the target at redirect_at, which must be the last word.
    if redirect is not None and redirect_at != len(words) - 1:
        _reject("unsupported redirection")
    return words, redirect


def _reject(reason: str) -> NoReturn:
    raise _Rejected(reason)


def _usable_cwd(cwd: str | None) -> bool:
    return bool(
        cwd
        and cwd.startswith("/")
        and not cwd.startswith("//")
        and posixpath.normpath(cwd) == cwd
    )


def _resolve(path: str, cwd: str | None, *, is_file: bool) -> str:
    """Absolute normalized form of `path`, or _Rejected if that can't be determined."""
    if not path:
        _reject("empty path")
    segments = path.split("/")
    if is_file and segments[-1] in ("", ".", ".."):
        _reject(f"not a file path: {path}")
    parts = [segment for segment in segments if segment not in ("", ".")]
    if path.startswith("/"):
        base: list[str] = []
        ups = 0
    else:
        if not _usable_cwd(cwd):
            _reject("relative path without cwd")
        base = [segment for segment in cwd.split("/") if segment]
        ups = 0
        while ups < len(parts) and parts[ups] == "..":
            ups += 1
    if ".." in parts[ups:]:
        _reject(f"'..' inside path: {path}")
    resolved = "/" + "/".join(base[: max(0, len(base) - ups)] + parts[ups:])
    if any(resolved == root or resolved.startswith(root + "/") for root in VOLATILE_ROOTS):
        _reject(f"volatile path: {resolved}")
    return resolved


def _is_known_directory(path: str, cwd: str | None) -> bool:
    if path == "/":
        return True
    return _usable_cwd(cwd) and (path == cwd or cwd.startswith(path + "/"))
