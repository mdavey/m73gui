# rsfec

A Reed–Solomon erasure-coded file container. Pack a file into a flat stream
of fixed-size blocks carrying whole-file FEC with block interleaving, then
recover it from that stream even when blocks went missing in transit.

This is a standalone package (`src/rsfec`) inside the m73gui repository, with
no dependencies on the GUI. It borrows *semantics* from EasyPal/EasyDRF but is
not compatible with them.

## The idea

A file is prefixed with a small header (filename, size, SHA-1) and split into
fixed-size **blocks**. Blocks are grouped, and each group carries Reed–Solomon
parity computed **column-wise across its blocks**. Because a codeword spans
every block in a group, losing a whole block costs each codeword exactly one
symbol — so any `p` blocks of a group can vanish and the file still recovers,
including runs of consecutive missing blocks.

On the wire the receiver just drops missing blocks in as all-zero bytes; RS
corrects them. If the transport *tells* you which blocks were lost (packet
sequence gaps), pass them as `missing=` so legitimately-zero data blocks are
never mistaken for losses.

## FEC levels

| level | ~data fraction | parity per `d` data blocks | max `d`/group |
|---|---|---|---|
| 1 | 7/8 | `ceil(d/7)` | 223 |
| 2 | 5/6 | `ceil(d/5)` | 212 |
| 3 | 3/4 | `ceil(d/3)` | 191 |
| 4 | 1/2 | `d` | 127 |

The `max d` keeps each column codeword within the GF(256) limit of 255
symbols. Files larger than `max_d × block_size` span multiple independently
recoverable groups.

## Python API

```python
import rsfec

res = rsfec.pack_bytes(data, name="photo.jpg", block_size=1200, fec=2)
# res.stream        -> bytes, exactly res.total_blocks * block_size long
# res.total_blocks  -> what a packetizer stamps as total_packets
# res.data_blocks / res.parity_blocks

# slice res.stream into block_size chunks and send them...

out = rsfec.unpack(rx_stream, block_size=1200, fec=2, missing={4, 17})
out.save("/some/dir")          # -> Path, sanitised basename
out.name, out.data, out.sha1_hex, out.size
```

- `pack(path, *, block_size, fec)` reads a file; the stored name is its basename.
- `missing` declares blocks that are absent *or* suspected corrupt — the
  cheap erasure path. Undeclared corruption is still located and corrected
  when the budget allows (see Limitations). Omit `missing` entirely and
  all-zero blocks are treated as losses.
- Raises `rsfec.CorruptedError` when losses exceed parity or integrity checks
  fail, and `rsfec.HeaderError` for malformed input or wrong `block_size`/`fec`.
- `rsfec.missing_blocks(stream, block_size)` returns the all-zero block indices.

## CLI

```
rsfec pack FILE -b BYTES -l LEVEL [-o OUT] [-v]     # OUT or stdout
rsfec unpack STREAM -b BYTES -l LEVEL [-d DEST] [--missing 3 7] [-v]
```

`block_size` and `level` are required on unpack because both are needed before
the FEC-protected header can be read. This keeps the container itself fully
protected; the transport is expected to carry these two values alongside the
data (see below). `-v` prints geometry and erasure counts to stderr.

## Format

```
stream = total_blocks * block_size bytes, laid out as groups
group  = [ data block x d ][ parity block x p ]
codeword = block[start][j], block[start+1][j], ... over the d+p blocks  (j-th byte)
```

Header (big-endian, at payload offset 0, so it is itself protected):

| field | size | notes |
|---|---|---|
| magic | 4 | `RSF1` |
| version | 1 | = 1 |
| fec_level | 1 | 1..4 |
| block_size | 2 | cross-checked against the arg |
| data_blocks | 4 | cross-checked against derived geometry |
| total_blocks | 4 | cross-checked |
| name_len | 2 | |
| name | name_len | UTF-8, basename only |
| file_size | 8 | |
| sha1 | 20 | over the file bytes only (no header/padding) |

The decoder derives the group layout from `total_blocks` alone, then validates
the header's claimed counts and the SHA-1 as consistency checks.

## Transport mapping (end-use, not part of this library)

The intended radio packetizer lives outside rsfec so the container stays
swappable. It frames each block as:

```
u8 type | u8 fec_level | u16 sequence | u16 total_packets | uchar[8] desc | uchar[block_size]
```

- `fec_level` is the **only** FEC detail the receiver needs before deinterleave;
  repeating it in every packet is self-healing. `total_packets` gives
  `total_blocks`; `block_size` is simply the uniform payload length.
- `block_size` on send = modem max packet payload − 14 (header bytes).
- The receiver maps packet sequence gaps to `missing=set`, zero-filling those
  blocks. A packet that decodes but looks suspect should also go in `missing`
  (costs the repair budget's cheap 1 instead of 2). `desc` can carry an 8-byte
  callsign ("from: VK3ARD") for instant feedback; the authoritative
  filename/size come from the recovered header.
- u16 `total_packets` caps a transfer at 65535 blocks.

## Limitations

- **Erasure + error budget.** Reed–Solomon can both *erase-correct* (you say a
  block is bad) and *error-correct* (it works out which symbols are bad on its
  own). One group's `p` parity blocks buy a mixed budget of
  `missing + 2×corrupted ≤ p`: each declared-missing block costs 1, each
  undeclared corrupted block costs 2. So a stray corrupt block that the
  transport didn't flag is still repaired, but two corrupted blocks eat the
  budget of four missing ones. Declare suspect blocks in `missing=` to spend
  the cheap rate. If the budget is blown, `CorruptedError` is raised — the
  SHA-1 and size checks mean a bad recovery is never silently accepted.
- An intact group decodes without Reed–Solomon work; RS only runs for groups
  that actually lost blocks.
- Pure-Python `reedsolo` backend: fine for the ~100 KB files this targets
  (roughly a second to encode/recover). Not for bulk transfers.

## Development

```
uv run python -m unittest discover -s src/rsfec/tests
```

Backend is `reedsolo>=1.7,<2` (the 2.x line is still beta with breaking
changes; `tests/test_reedsolo_guard.py` pins the 1.x behaviours this code
relies on).
