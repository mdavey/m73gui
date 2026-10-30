"""rsfec command-line interface: pack a file into an FEC stream / unpack one."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import geometry
from .container import missing_blocks, pack, unpack
from .errors import RsfecError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rsfec",
        description="Reed-Solomon erasure-coded file container",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pack = sub.add_parser("pack", help="pack a file into an FEC block stream")
    p_pack.add_argument("file", type=Path, help="input file")
    p_pack.add_argument("-b", "--block-size", type=int, required=True,
                        help="bytes per block (payload per packet)")
    p_pack.add_argument("-l", "--level", type=int, required=True,
                        choices=(1, 2, 3, 4), help="FEC level 1..4")
    p_pack.add_argument("-o", "--output", type=Path,
                        help="write stream here (default: stdout)")
    p_pack.add_argument("-v", "--verbose", action="store_true")

    p_unpack = sub.add_parser("unpack", help="recover a file from an FEC stream")
    p_unpack.add_argument("stream", type=Path, help="packed stream file")
    p_unpack.add_argument("-b", "--block-size", type=int, required=True)
    p_unpack.add_argument("-l", "--level", type=int, required=True,
                          choices=(1, 2, 3, 4))
    p_unpack.add_argument("-d", "--dest-dir", type=Path, default=Path("."))
    p_unpack.add_argument("--missing", type=int, nargs="*", default=None,
                          metavar="BLOCK",
                          help="block indices known to be absent")
    p_unpack.add_argument("-v", "--verbose", action="store_true")

    return parser


def _pack(args) -> int:
    res = pack(args.file, block_size=args.block_size, fec=args.level)
    if args.verbose:
        print(
            f"packed {args.file.name}: {res.data_blocks} data + "
            f"{res.parity_blocks} parity = {res.total_blocks} blocks x "
            f"{res.block_size} B, FEC level {res.fec}",
            file=sys.stderr,
        )
    if args.output:
        args.output.write_bytes(res.stream)
    else:
        sys.stdout.buffer.write(res.stream)
    return 0


def _unpack(args) -> int:
    stream = args.stream.read_bytes()
    missing = None if args.missing is None else set(args.missing)
    out = unpack(stream, block_size=args.block_size, fec=args.level,
                 missing=missing)
    path = out.save(args.dest_dir)
    if args.verbose:
        total = len(stream) // args.block_size
        groups = geometry.decode_groups(args.level, total)
        erasures = (sorted(missing) if missing is not None
                    else sorted(missing_blocks(stream, args.block_size)))
        shown = ", ".join(map(str, erasures[:16]))
        if len(erasures) > 16:
            shown += ", ..."
        print(
            f"{total} blocks in {len(groups)} group(s); "
            f"{len(erasures)} erasure block(s): [{shown}]",
            file=sys.stderr,
        )
        print(f"saved {path} ({out.size} bytes, sha1 {out.sha1_hex})",
              file=sys.stderr)
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "pack":
            return _pack(args)
        return _unpack(args)
    except (RsfecError, ValueError, OSError) as exc:
        print(f"rsfec: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
