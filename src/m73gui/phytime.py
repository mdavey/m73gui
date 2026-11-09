"""Rough airtime estimates for modem73's PHY modes.

The constants mirror modem73's frame structure (phy/common.hh,
phy/mfsk_modem.hh, phy/robust_modem.hh) so a selected file can show an
"honestly rough" total time. Sender pacing, CSMA waits, and retransmits
are not modelled beyond a flat per-frame slack.

The wide OFDM family (beta modem73_wide: 4/8/16 kHz grid, 1-8 codewords
per preamble) mirrors phy/common.hh Common::setup_wide().
"""

from __future__ import annotations

from .constants import BLOCKS_MAX, ROBUST_MODE_MTU

# per-frame sender pacing / CSMA / PTT-gap slack in seconds (measured on a
# real link: ~0.6 s between consecutive frames beyond pure airtime)
CHUNK_OVERHEAD_S = 0.6

# rsfec block streams need no start/end frames; if that ever changes again,
# account the control traffic here
CONTROL_FRAMES = 0

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
    if _is_wide(config):
        wide = _wide_setup(config)
        if wide is None:
            return _DEFAULT_FRAME_S
        _data_bytes, symbols, blocks = wide
        seconds = (3 + symbols * blocks) * _WIDE_SYMBOL_S
        if config.get("postamble"):
            seconds += 0.4
        return seconds
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


# --------------------------------------------------------------------------
# Payload capacity model, mirroring modem73 (phy/common.hh Common::setup,
# phy/mfsk_modem.hh MFSKParams, phy/robust_modem.hh RobustParams::data_bytes).
# Must be kept in lockstep with modem73, same as the frame timing above.
# --------------------------------------------------------------------------

# modulation -> (data symbol count, polar code order) at short frame size
_OFDM_PARAMS = {
    "BPSK": (8, 11),
    "QPSK": (4, 11),
    "8PSK": (11, 13),
    "QAM16": (4, 12),
    "QAM64": (11, 14),
    "QAM256": (8, 14),
    "QAM1024": (13, 15),
    "QAM4096": (11, 15),
}

# polar data bits at code order 11 per code rate; doubles per order above 11
_OFDM_RATE_DATA_BITS = {
    "1/2": 1024,
    "2/3": 1368,
    "3/4": 1536,
    "5/6": 1704,
    "1/4": 512,
    "1/2x2": 1024,
    "1/4x2": 512,
}
_REPEAT2_RATES = {"1/2x2", "1/4x2"}

# --------------------------------------------------------------------------
# Wide OFDM family (modem73_wide beta). All of this mirrors Common::setup_wide
# in phy/common.hh; keep in lockstep, same as the base tables above.
# --------------------------------------------------------------------------

_WIDE_SYNC_TONES = 640      # the 4 khz sync slice every wide width contains
_WIDE_SYMBOL_S = 49 / 300   # one wide OFDM symbol
_WIDE_BLOCK_LENGTH = 5      # pilot block length (Common::block_length)
_WIDE_PERM_MAX = 1 << 20    # interleaved-bit cap for a whole frame
_WIDE_SYMBOLS_MAX = 88      # symbol_count * blocks cap (symbols_max - 1)
_WIDE_BASE_ORDER = {
    "BPSK": 11, "QPSK": 11, "8PSK": 13, "QAM16": 12,
    "QAM64": 14, "QAM256": 14, "QAM1024": 15, "QAM4096": 15,
}
_WIDE_MOD_BITS = {
    "BPSK": 1, "QPSK": 2, "8PSK": 3, "QAM16": 4,
    "QAM64": 6, "QAM256": 8, "QAM1024": 10, "QAM4096": 12,
}
# the repeated x2 rates are base-family only
_WIDE_RATES = {"1/2", "2/3", "3/4", "5/6", "1/4"}


def _slot_capacity(symbols: int, tones: int, mb: int) -> int:
    """Bits the mapper places over *symbols* full data symbols, reproducing
    the shortening tweaks applied to 8PSK/QAM64/QAM1024/QAM4096."""
    k = 0
    slots = symbols * (tones - tones // _WIDE_BLOCK_LENGTH)
    for _ in range(slots):
        bits = mb
        if mb == 3 and k % 32 == 30:
            bits = 2
        elif mb == 6 and k % 64 == 60:
            bits = 4
        elif mb in (10, 12) and k % 128 == 120:
            bits = 8
        k += bits
    return k


def _wide_setup(config: dict):
    """(data_bytes per codeword, symbols per codeword, blocks) for a valid
    wide OFDM combo; None when invalid or unsupported."""
    try:
        bandwidth = int(config.get("bandwidth") or 0)
    except (TypeError, ValueError):
        return None
    try:
        blocks = int(config.get("blocks") or 1)
    except (TypeError, ValueError):
        return None
    if not 1 <= bandwidth <= 3 or not 1 <= blocks <= BLOCKS_MAX:
        return None
    rate = str(config.get("code_rate", ""))
    mod = str(config.get("modulation", ""))
    base_order = _WIDE_BASE_ORDER.get(mod)
    if rate not in _WIDE_RATES or base_order is None:
        return None
    frame_size = config.get("frame_size", 1)
    if isinstance(frame_size, str):
        frame_size = _OFDM_FRAME_SIZE.get(frame_size.lower(), 1)
    try:
        frame_size = int(frame_size)
    except (TypeError, ValueError):
        frame_size = 1
    if frame_size > 1:  # wide has neither long frames nor microbursts
        return None
    # the normal frame doubles the codeword: no headroom at the 65536-bit cap
    if frame_size == 1 and base_order + bandwidth >= 16:
        return None
    order = min(base_order + bandwidth + frame_size, 16)
    tones = _WIDE_SYNC_TONES << (bandwidth - 1)
    mb = _WIDE_MOD_BITS[mod]
    need = 1 << order
    symbols = 0
    for s in range(1, 45):
        if _slot_capacity(s, tones, mb) >= need:
            symbols = s
            break
    if not symbols:
        return None
    if symbols * blocks > _WIDE_SYMBOLS_MAX:
        return None
    if _slot_capacity(symbols * blocks, tones, mb) > _WIDE_PERM_MAX:
        return None
    return (_OFDM_RATE_DATA_BITS[rate] << (order - 11)) // 8, symbols, blocks


def _is_wide(config: dict) -> bool:
    """True when the config selects a wide OFDM family (beta modem73).
    A missing bandwidth key (older modem73) means base."""
    try:
        return int(config.get("bandwidth") or 0) > 0
    except (TypeError, ValueError):
        return False


def _mfsk_payload(mode: int) -> int:
    """Frame capacity (reported payload_size) for MFSK-8/16/32/32R."""
    bits_per_symbol = (3, 4, 5, 5)[mode]
    coded = 128 * bits_per_symbol
    if mode == 3:  # MFSK-32R uses the rate-3/4 convolutional code
        data_bytes = (coded * 3 // 4 - 6) // 8
    else:
        data_bytes = (coded // 2 - 6) // 8
    return data_bytes - 2


def payload_bytes_for(config: dict) -> int:
    """Frame payload capacity as modem73's get_config would report
    payload_size for the given mode fields; 0 when the combo is invalid
    or unsupported."""
    try:
        mtype = int(config.get("modem_type", 0) or 0)
    except (TypeError, ValueError):
        mtype = 0
    if mtype == 1:  # MFSK
        try:
            return _mfsk_payload(int(config.get("mfsk_mode", 0)))
        except (IndexError, TypeError, ValueError):
            return 0
    if mtype == 2:  # ROBUST
        try:
            return ROBUST_MODE_MTU[int(config.get("robust_mode", 0))] + 2
        except (KeyError, TypeError, ValueError):
            return 0
    # OFDM
    if _is_wide(config):
        wide = _wide_setup(config)
        if wide is None:
            return 0
        data_bytes, _symbols, blocks = wide
        return data_bytes * blocks
    rate = _OFDM_RATE_DATA_BITS.get(str(config.get("code_rate", "")))
    params = _OFDM_PARAMS.get(str(config.get("modulation", "")))
    if rate is None or params is None:
        return 0
    symbols, order = params
    frame_size = config.get("frame_size", 1)
    if isinstance(frame_size, str):
        frame_size = {
            "short": 0, "normal": 1, "long": 2, "micro": 3,
        }.get(frame_size.lower(), 1)
    try:
        frame_size = int(frame_size)
    except (TypeError, ValueError):
        frame_size = 1
    if frame_size == 3:  # microburst family is QPSK + rate 1/2 only
        if (config.get("modulation") == "QPSK"
                and str(config.get("code_rate", "")) == "1/2"):
            return 32
        return 0
    if frame_size >= 1:  # normal (long applies the normal step as well)
        order += 2 if symbols == 4 else 1
    if frame_size == 2:
        order += 1
    if order > 16:
        return 0
    if str(config.get("code_rate", "")) in _REPEAT2_RATES and order > 15:
        return 0
    return (rate << (order - 11)) // 8


def estimate_transfer_seconds(config: dict, frames: int) -> float:
    """Estimated total airtime for a broadcast of *frames* full frames."""
    if frames <= 0:
        return 0.0
    return (frames + CONTROL_FRAMES) * (frame_seconds(config) + CHUNK_OVERHEAD_S)


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
