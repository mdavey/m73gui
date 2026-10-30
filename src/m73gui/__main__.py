"""Command-line entry point for m73gui."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .constants import DEFAULT_CONTROL_PORT, DEFAULT_KISS_PORT, DEFAULT_HOST


def parse_host_port(text: str, default_port: int) -> tuple[str, int]:
    """Parse HOST[:PORT]; IPv6 [::1]:port is supported."""
    text = text.strip()
    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        if not sep:
            raise argparse.ArgumentTypeError(f"invalid address {text!r}")
        if rest:
            if not rest.startswith(":"):
                raise argparse.ArgumentTypeError(f"invalid address {text!r}")
            return host, int(rest[1:])
        return host, default_port
    host, sep, rest = text.rpartition(":")
    if sep and rest and host:
        return host, int(rest)
    if sep:  # ":8073" form
        return DEFAULT_HOST, int(rest)
    return text or DEFAULT_HOST, default_port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="m73gui",
        description="Desktop GUI for modem73: chat, broadcast file transfers, "
        "channel monitoring and settings over the control + KISS ports.",
        epilog=(
            "m73gui needs two modem73 endpoints: the JSON control port "
            f"(default {DEFAULT_CONTROL_PORT}) and the KISS data port "
            f"(default {DEFAULT_KISS_PORT}).\n"
            "A remote GUI requires modem73 started with --control-bind on a "
            "reachable address (the control port binds to localhost by default).\n\n"
            "Examples:\n"
            "  m73gui\n"
            "  m73gui 192.168.1.50\n"
            "  m73gui 192.168.1.50:8073 --kiss 192.168.1.50:8001\n\n"
            "Limitations: file transfers are best-effort broadcast (no ACKs or "
            "resume); CSMA backoff phase is not exposed by modem73; modem73's "
            "tx_frame_count is broken upstream so GUI TX counts are local; every "
            "connected KISS client sees all decoded payloads (nothing is "
            "private); audio device switching, AX.25, and encryption are out of "
            "scope."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "address",
        nargs="?",
        default=f"{DEFAULT_HOST}:{DEFAULT_CONTROL_PORT}",
        metavar="ADDRESS",
        help=f"control endpoint HOST[:PORT] (default {DEFAULT_HOST}:{DEFAULT_CONTROL_PORT})",
    )
    parser.add_argument(
        "--kiss",
        dest="kiss",
        metavar="HOST:PORT",
        default=None,
        help=f"KISS endpoint for raw payload RX/TX (default: control host, port {DEFAULT_KISS_PORT})",
    )
    parser.add_argument(
        "--appearance",
        choices=["dark", "light", "system"],
        default="dark",
        help="customtkinter appearance mode (default: dark)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable DEBUG logging to stderr, including protocol frame summaries",
    )
    parser.add_argument(
        "--version", action="version", version=f"m73gui {__version__}"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    control_host, control_port = parse_host_port(args.address, DEFAULT_CONTROL_PORT)
    if args.kiss:
        kiss_host, kiss_port = parse_host_port(args.kiss, DEFAULT_KISS_PORT)
    else:
        kiss_host, kiss_port = control_host, DEFAULT_KISS_PORT

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        from logging.handlers import RotatingFileHandler

        from .userdirs import log_dir

        file_handler = RotatingFileHandler(
            log_dir() / "m73gui.log", maxBytes=1_000_000, backupCount=3
        )
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(file_handler)
    except OSError:
        pass  # unwritable ~/.m73gui: keep stderr logging only

    from .app import run  # deferred so --help works without tkinter

    return run(
        control=(control_host, control_port),
        kiss=(kiss_host, kiss_port),
        appearance=args.appearance,
        debug=args.debug,
    )


if __name__ == "__main__":
    sys.exit(main())
