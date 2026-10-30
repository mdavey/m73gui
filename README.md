# m73gui

A desktop GUI for [modem73](https://github.com/RFnexus/modem73) — the amateur-radio
OFDM/MFSK/RDM data modem. modem73 is primarily just that: a modem, driven through a
control port, a KISS port, and a log. m73gui puts a friendly front end on it —
chat, file broadcasting, a live look at what's on the channel, and an editor for the
modem's settings.

## Why

Sending anything meaningful over a ~1 kbps radio link is fiddly: you need to know
the current mode's payload size, slice files into frames by hand, and you have no
visibility into what the modem is doing. m73gui handles the framing, pacing and reassembly
for you, and shows you the channel while it happens. It talks to modem73 purely
over its network ports (JSON control + KISS data), so the GUI can run on a laptop
next to the radio, or across the house from a machine hosting the audio.

## Features

- **Chat** — free-text messages broadcast to everyone on the channel, with a
  scrolling history that is rebuilt from modem73's frame history after
  reconnects. A Broadcast mode card at the top picks the transmit mode
  (OFDM / MFSK / ROBUST); the choice is pushed to the modem when you press
  Send.
- **Files** — pick a file (up to 1 MiB), choose a mode and a Reed–Solomon FEC
  level, see the block count and estimated airtime (both recompute instantly
  when you change either, even before the modem knows about it), and broadcast
  it. Lost packets are repaired by the receiver's error correction — no
  retransmits, no per-chunk bookkeeping. Incoming transfers reassemble
  automatically into `~/.m73gui/received/<yyyy-mm-dd>/` with SHA-1
  verification; the sender can pick from L1 (~14% overhead) to L4 (+100%).
- **Packets** — a live table of every decoded frame (mode, SNR, BER, payload) with
  a detail view, plus a summary of everything you transmit.
- **Channel** — link-quality trend charts fed by every decoded frame (per-frame
  SNR and BER), an occupancy bar, population/TX-queue/PTT metrics, modem error
  counters, and audio/rigctld health.
- **Settings** — callsign, CSMA, TX, and RX-decoder fields in a validated form;
  only changed fields are written back. The modem's live mode appears there as
  a read-only view plus a plain-language guide to the mode families — you
  change modes from the Chat and Files tabs instead.

The app never keys the radio by itself — it only frames data and hands it to
modem73, which does its own CSMA before transmitting.

## Requirements

- Python 3.14+ and [uv](https://docs.astral.sh/uv/)
- A working display (X11 or XWayland)
- [modem73](https://github.com/RFnexus/modem73) running with both its control port
  (default `127.0.0.1:8073`) and KISS port (default port `8001`) enabled
- Amateur radio privileges for whatever you transmit, obviously

Runtime dependencies are `customtkinter` plus `reedsolo` (pulled in by the
bundled `rsfec` workspace package under `src/rsfec/`).

## Running

```sh
uv sync
uv run m73gui
```

That connects to modem73 on localhost. To drive a remote modem:

```sh
m73gui 192.168.1.50                      # control on :8073, KISS on :8001
m73gui 192.168.1.50:8073 --kiss 192.168.1.50:8001
```

A remote setup needs modem73 started with `--control-bind` on a reachable address
(it binds to localhost by default).

Other flags: `--appearance dark|light|system`, `--debug` (protocol logging to
stderr), `--version`.

## State on disk

Everything m73gui keeps lives under `~/.m73gui/`:

| Path | What |
| --- | --- |
| `settings.json` | per-tab mode choices, FEC level, loopback preference (survives restarts) |
| `logs/m73gui.log` | rotating log (1 MB × 3) |
| `received/<yyyy-mm-dd>/` | completed incoming files, one folder per day |

## Things to know

- **Broadcast only.** Chat and files have no addressing, no ACKs, and no resume.
  File losses are repaired by forward error correction up to the chosen level
  (L2 fixes roughly 1 lost packet in 6); beyond that the transfer is marked
  failed and you re-send. A file that completes has passed its SHA-1 check.
- **Nothing is private.** Every connected KISS client — on any m73gui instance —
  sees all decoded payloads.
- **Airtime adds up.** A 100 KB file on a robust mode can take the better part of
  an hour, and the FEC parity blocks add 14–100% on top depending on level. The
  estimated airtime shown when you pick a file ignores CSMA waits, so treat it as
  a lower bound on a busy channel.
- **Transfers are local.** Incoming files land in
  `~/.m73gui/received/<yyyy-mm-dd>/` with duplicate names de-duplicated
  automatically.
- **Mode edits apply on Send.** The Chat and Files Broadcast mode cards are
  independent intentions; pressing Send pushes the mode to the modem first,
  then transmits on it. Chatting while a file transfer runs defers the chat
  tab's mode (changing it mid-broadcast would resize frames under the running
  stream).
- modem73's `tx_frame_count` is broken upstream, so TX counts in the GUI are
  counted locally instead.

## Development

Tests use plain `unittest` (no modem or display required):

```sh
uv run python -m unittest discover -s tests
uv run python -m unittest discover -s src/rsfec/tests
```
