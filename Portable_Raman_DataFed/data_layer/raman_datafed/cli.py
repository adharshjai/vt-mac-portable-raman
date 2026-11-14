"""Command-line front end: `python -m raman_datafed --home HOME <command> [options]`.

The contract (PROTOCOL.md section 6): exactly one JSON object on stdout, logs on stderr, and
exit code 0 only when "ok" is true. The controller parses stdout, so nothing else may ever be
written there, not even by a library.

Commands come from the modules in COMMAND_MODULES. Each module has `register(subparsers)`,
which adds its sub-commands with `parser.set_defaults(func=function)`. A command function takes
`(args, ctx)` and returns a dict; raising any exception makes the command fail.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import logging
import os
import sys
from pathlib import Path

from .catalog import open_catalog
from .config import CommandError, DataLayerError, Paths, load_settings
from .ledger import Ledger

log = logging.getLogger(__name__)

COMMAND_MODULES = [
    "raman_datafed.commands_core",
    "raman_datafed.job",
    "raman_datafed.review",
    "raman_datafed.studio_server",
]


class UsageError(DataLayerError):
    kind = "usage"


class _HelpShown(Exception):
    def __init__(self, text):
        super().__init__("help")
        self.text = text


class _Parser(argparse.ArgumentParser):
    """An argument parser that reports through exceptions: argparse itself would print to
    stdout or stderr and exit, which breaks the one-JSON-object contract."""

    def error(self, message):
        raise UsageError("Invalid arguments: %s" % message, detail=self.format_usage().strip())

    def exit(self, status=0, message=None):
        raise UsageError((message or "The arguments could not be used").strip())

    def print_help(self, file=None):
        raise _HelpShown(self.format_help())


def _command_modules():
    """The command modules of this installation. A module is skipped only when it does not exist;
    one that exists and fails to import is an error worth seeing."""
    modules = []
    for name in COMMAND_MODULES:
        try:
            modules.append(importlib.import_module(name))
        except ModuleNotFoundError as error:
            if error.name != name:
                raise
    return modules


def build_parser():
    parser = _Parser(
        prog="python -m raman_datafed",
        description="Data layer of the Portable Raman controller: registers captures in the catalog. "
        "Prints one JSON object on stdout.",
    )
    parser.add_argument("--home", help="folder the controller runs from (default: the current folder)")
    parser.add_argument("--verbose", action="store_true", help="more detail in the log on stderr")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True
    for module in _command_modules():
        module.register(subparsers)
    return parser


class Context:
    """What a command function gets as `ctx`: HOME, its standard paths, and the settings, ledger
    and catalog, each opened the first time it is used.

    A long-running program makes one Context per request (`with Context(home) as ctx:`), so that
    settings changed by another process are seen.
    """

    def __init__(self, home, catalog=None, stdout=None):
        self.home = Path(home).expanduser().resolve()
        self.paths = Paths(self.home)
        self.emitted = False
        self._stdout = sys.stdout if stdout is None else stdout
        self._config = None
        self._ledger = None
        self._catalog = catalog
        self._catalog_given = catalog is not None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _require_home(self):
        if not self.home.is_dir():
            raise CommandError("The HOME folder does not exist", detail=str(self.home))

    @property
    def config(self):
        """The settings, checked: commands that act on them must not run with invalid ones."""
        if self._config is None:
            self._require_home()
            settings = load_settings(self.home)
            settings.require_valid()
            self._config = settings
        return self._config

    @property
    def ledger(self):
        if self._ledger is None:
            self._require_home()
            self._ledger = Ledger(self.paths.data_catalog)
        return self._ledger

    @property
    def catalog(self):
        """The catalog the settings select. Building it contacts nothing."""
        if self._catalog is None:
            self._catalog = open_catalog(self.config, self.paths)
        return self._catalog

    def reload(self):
        """Forget the settings (and the catalog built from them) after they were changed."""
        self._config = None
        if not self._catalog_given:
            self._catalog = None

    def emit(self, result):
        """Print the command's JSON object now, for a command that keeps running afterwards
        (the Studio server). The front end then prints nothing more on stdout."""
        _write_json(self._stdout, _with_ok(result))
        self.emitted = True

    def close(self):
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None


def _with_ok(result):
    if not isinstance(result, dict):
        raise TypeError("a command must return a dict, not %s" % type(result).__name__)
    payload = {"ok": True}
    payload.update((key, value) for key, value in result.items() if key != "ok")
    return payload


def error_payload(error):
    """The failure object of the contract for any exception."""
    if isinstance(error, DataLayerError):
        return {"ok": False, "error": str(error), "detail": error.detail, "kind": error.kind}
    text = str(error).strip().splitlines()
    summary = "%s: %s" % (type(error).__name__, text[0]) if text else type(error).__name__
    return {"ok": False, "error": summary, "detail": "The traceback is in the log on stderr.", "kind": "internal"}


def _write_json(stream, payload):
    try:
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str)
    except ValueError as error:
        # NaN or infinity is not JSON; the controller could not parse the line.
        text = json.dumps(error_payload(error))
    data = (text + "\n").encode("utf-8")
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        # Bytes, so that the output is UTF-8 whatever the locale of the calling process.
        buffer.write(data)
    else:
        stream.write(data.decode("utf-8"))
    stream.flush()


def run(args, ctx):
    """Run the command chosen by parsed arguments; returns its result with "ok": true."""
    return _with_ok(args.func(args, ctx))


def invoke(ctx, argv):
    """Run one command in this process and return what the command line would print, as a dict.

    Failures come back as the failure object instead of an exception. Used by the Studio server.
    stdout is left alone here: swapping it is not safe when several threads serve requests.
    """
    try:
        return run(build_parser().parse_args([str(item) for item in argv]), ctx)
    except _HelpShown as shown:
        return {"ok": True, "help": shown.text}
    except Exception as error:
        if not isinstance(error, DataLayerError):
            log.exception("command failed: %s", " ".join(str(item) for item in argv))
        return error_payload(error)


def main(argv=None):
    stdout = sys.stdout
    ctx = None
    code = 0
    arguments = sys.argv[1:] if argv is None else list(argv)
    # Set up before anything else, so that a failure while the commands are loaded is logged too.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if "--verbose" in arguments else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # Libraries print on stdout (the datafed package does); while a command runs, that goes to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            args = build_parser().parse_args(arguments)
            ctx = Context(args.home or os.getcwd(), stdout=stdout)
            payload = run(args, ctx)
        except _HelpShown as shown:
            payload = {"ok": True, "help": shown.text}
        except KeyboardInterrupt:
            payload, code = {"ok": False, "error": "Interrupted", "detail": "", "kind": "interrupted"}, 130
        except Exception as error:
            if not isinstance(error, DataLayerError):
                log.exception("the command failed")
            payload = error_payload(error)
            code = 2 if isinstance(error, UsageError) else 1
        finally:
            if ctx is not None:
                ctx.close()
    if ctx is not None and ctx.emitted:
        if code:
            log.error("%s", payload.get("error"))
        return code
    _write_json(stdout, payload)
    return code
