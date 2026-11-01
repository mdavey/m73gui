# m73gui

A desktop GUI for [modem73](https://github.com/RFnexus/modem73) - an amateur-radio
OFDM/MFSK/RDM data modem.

![m73gui on the Files tab: Tx Settings, an outgoing file transfer in progress, and an incoming transfer being reassembled](screenshot.png)


## Why

Modem73 is a very interesting piece of software. I wanted to try and wrap
some very simple tools (chat/file sending) around it and see how well it
works.

This is not intended to be a finished program, more like a test bed of ideas.


## Features

- **Chat** - Send free-form text. No addressing, broadcast only.
- **Files** - Send a file (up to 1MiB) wrapped in another layer of
  FEC. This RS FEC *is* interleaved based on transmission frame so
  should be effective. Incoming transfers are reassembled
  automatically into `~/.m73gui/received/<yyyy-mm-dd>/` with SHA-1
  verification.
- **Packet Log** - a list of every decoded frame (mode, SNR, BER, payload)
- **Status** - link-quality trend charts, and other modem metrics

The app never keys the radio by itself - it only frames data and hands it to
modem73, which does its own CSMA before transmitting.


## Missing Features

Some intentional, some not.

- **No ACKs** - Everything is broadcast only, no acknowledgments
- **No Privacy** - Both for information sent on RF and for connecting to modem73.
  Only run modem73 on systems where you are the only user with access.
- **Incoming files are auto-saved** - Don't open random files from random
  users.


## Requirements

- Python 3.14+ and [uv](https://docs.astral.sh/uv/)
- [modem73](https://github.com/RFnexus/modem73) (with control and KISS ports enabled)

Runtime dependencies are `customtkinter` and `reedsolo` (via the bundled `rsfec` package)


## Running

Modem73 listens on two ports:
- `8073` - Control port for changing settings
- `8001` - KISS for sending and receiving data

m73gui needs to be able to connect to both ports to function. If you haven't
changed the modem73 defaults you can run:

```sh
uv sync
uv run m73gui
```

Alternatively, specify IP and ports for both control and KISS:

```sh
uv run m73gui 192.168.1.50
uv run m73gui 192.168.1.50:8073 --kiss 192.168.1.50:8001
```

Note: A remote setup needs modem73 started with `--control-bind` on a reachable address
(it binds to localhost by default).


## State on disk

Everything m73gui keeps lives under `~/.m73gui/`:

| Path | What |
| --- | --- |
| `settings.json` | Application settings |
| `logs/m73gui.log` | rotating log (1 MB × 3) |
| `received/<yyyy-mm-dd>/` | completed incoming files, one folder per day |


## Development

Tests use plain `unittest` (no modem or display required):

```sh
uv run python -m unittest discover -s tests
uv run python -m unittest discover -s src/rsfec/tests
```

The `rsfec` package under `src/` is meant to be an independent system for wrapping
a file into minimal container that also contains error correction.


## License

MIT


## AI Disclosure

This project was heavily assisted by local LLMs.
