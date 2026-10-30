"""Rough airtime estimates for modem73's PHY modes.

The constants mirror modem73's frame structure (phy/common.hh,
phy/mfsk_modem.hh, phy/robust_modem.hh) so a selected file can show an
"honestly rough" total time. Sender pacing, CSMA waits, and retransmits
are not modelled beyond a flat per-chunk slack.
"""

from __future__ import annotations

# per-chunk sender pacing / CSMA slack in seconds
CHUNK_OVERHEAD_S = 0.5

# used when the mode is unrecognised (same fallback modem73 uses)
_DEFAULT_FRAME_S = 3.0

# OFDM data symbols per modulation before frame-size/repeat multipliers
_OFDM_BASE_SYMBOLS = {
    "BPSK": 8,
    "QPSK": 4,
    "8PSK": 11,
    "QAM16": 4,
    "QAM64": 11,
    "QAM256": 8,
    "QAM1024": 13,
    "QAM4096": 11,
}
_OFDM_FRAME_SIZE = {"short": 0, "normal": 1, "long": 2}

# RDM data rows per robust_mode; index 12 is the microburst mode
_ROBUST_ROWS = (173, 345, 685, 685, 1369, 89, 177, 349, 345, 689, 257, 129, 22)
_ROBUST_ROW_S = 960 / 48000  # one RDM row is a 960-sample OFDM symbol


def frame_seconds(config: dict) -> float:
    """Estimated on-air duration of one full frame in the current mode."""
    try:
        mtype = int(config.get("modem_type", 0) or 0)
    except (TypeError, ValueError):
        mtype = 0
    if mtype == 1:  # MFSK: fixed 138 symbols of 1536 samples
        return 138 * 1536 / 48000
    if mtype == 2:  # ROBUST: fixed frame, rows depend on mode
        try:
            rows = _ROBUST_ROWS[int(config.get("robust_mode", 0))]
        except (IndexError, TypeError, ValueError):
            return _DEFAULT_FRAME_S
        return (3 + rows + 2) * _ROBUST_ROW_S
    # OFDM
    base = _OFDM_BASE_SYMBOLS.get(str(config.get("modulation", "")))
    if base is None:
        return _DEFAULT_FRAME_S
    frame_size = config.get("frame_size", 1)
    if isinstance(frame_size, str):
        frame_size = _OFDM_FRAME_SIZE.get(frame_size.lower(), 1)
    try:
        frame_size = int(frame_size)
    except (TypeError, ValueError):
        frame_size = 1
    if frame_size == 3:  # microburst: lead + sync pair + meta + guard
        return 0.04 + 0.27 + 2 * (41 / 300) + 0.0033
    if frame_size >= 1:
        symbols = base * (4 if base == 4 else 2)
    else:
        symbols = base
    if frame_size == 2:  # long frame doubles a normal one
        symbols *= 2
    if "x2" in str(config.get("code_rate", "")):
        symbols *= 2
    seconds = (3 + symbols) * (41 / 300)
    if config.get("postamble"):
        seconds += 0.4
    return seconds


def estimate_transfer_seconds(config: dict, chunks: int) -> float:
    """Estimated total airtime for a broadcast of *chunks* full chunks."""
    if chunks <= 0:
        return 0.0
    return chunks * (frame_seconds(config) + CHUNK_OVERHEAD_S)


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
