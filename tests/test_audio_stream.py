"""Headless tests for the audio services: fake capture device + fake KISS
sink exercise the broadcaster; a fake playback device drives the receiver.
Real opus encoding/decoding is used throughout (no audio hardware needed).
"""

import math
import struct
import threading
import time
import unittest

import opuslink
from m73gui import protocol
from m73gui.state import TX_CANCELLED, TX_FAILED
from m73gui.services.audio_rx import AudioReceiver
from m73gui.services.audio_tx import AudioBroadcaster

WIDE_CFG = {
    "modem_type": 0, "modulation": "QAM4096", "code_rate": "5/6",
    "frame_size": 0, "bandwidth": 3, "blocks": 4, "payload_size": 20000,
}
CALLSIGN = "M7GUI"

NARROW_CFG = {  # base OFDM normal QPSK 1/2: ~1.5 kbps budget, no tier fits
    "modem_type": 0, "modulation": "QPSK", "code_rate": "1/2",
    "frame_size": 1, "payload_size": 512,
}


def tone_pcm(n_bytes: int, phase_frames: int = 0) -> bytes:
    """Cheap deterministic 'audio': 440 Hz sine, continuing phase."""
    out = bytearray()
    start = phase_frames * n_bytes // 2
    for i in range(start, start + n_bytes // 2):
        v = int(6000 * math.sin(2 * math.pi * 440 * i / 16000))
        out += struct.pack("<h", v)
    return bytes(out)


class FakeCaptor:
    """Capture seam: the test pushes PCM in via instance.push()."""

    last: "FakeCaptor | None" = None

    def __init__(self, rate, channels, on_pcm):
        self.rate = rate
        self.channels = channels
        self.on_pcm = on_pcm
        self.started = False
        self.stopped = False
        FakeCaptor.last = self

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def push_audio(self, frames: int, per_frame: int):
        for i in range(frames):
            self.on_pcm(tone_pcm(per_frame, phase_frames=i))


class FakeKiss:
    def __init__(self, depth=0):
        self.sent: list[bytes] = []
        self.depth = depth
        self.ok = True
        self.on_send = None  # called (on the worker thread) with new count

    def send(self, payload: bytes) -> bool:
        if not self.ok:
            return False
        self.sent.append(payload)
        if self.on_send is not None:
            self.on_send(len(self.sent))
        return True

    def status(self):
        return {"tx_queue": self.depth}


def make_broadcaster(cfg=WIDE_CFG, kiss=None, sleep=None):
    events: list = []
    kiss = kiss or FakeKiss()
    cfg_holder = {"cfg": dict(cfg)}
    tx = AudioBroadcaster(
        emit=lambda kind, **kw: events.append((kind, kw)),
        send_frame=kiss.send,
        get_status=kiss.status,
        get_config=lambda: dict(cfg_holder["cfg"]),
        outgoing_ids=set(),
        capture_factory=FakeCaptor,
        sleep=sleep or (lambda s: None),
    )
    return tx, kiss, events, cfg_holder


def expected_plan(cfg=WIDE_CFG, callsign=CALLSIGN):
    budget = protocol.audio_budget_for(cfg["payload_size"], len(callsign))
    from m73gui import phytime
    return opuslink.plan_for(budget, phytime.frame_seconds(cfg))


class BroadcasterStart(unittest.TestCase):
    def test_refuses_slow_mode(self):
        tx, _kiss, events, _ = make_broadcaster(NARROW_CFG)
        err = tx.start(NARROW_CFG, CALLSIGN)
        self.assertIsNotNone(err)
        self.assertIn("not enough data rate", err)
        self.assertFalse(tx.busy())

    def test_rejects_nonascii_callsign_chars(self):
        tx, _kiss, events, _ = make_broadcaster()
        self.assertIsNone(tx.start(WIDE_CFG, "\u00e9MRC-1!"))
        tx.stop(); tx.close()

    def test_double_start_refused(self):
        tx, kiss, events, _ = make_broadcaster()
        self.assertIsNone(tx.start(WIDE_CFG, CALLSIGN))
        err = tx.start(WIDE_CFG, CALLSIGN)
        self.assertIn("already running", err)
        tx.stop(); tx.close()


class BroadcasterStream(unittest.TestCase):
    def setUp(self):
        FakeCaptor.last = None
        self.tx, self.kiss, self.events, self.cfg = make_broadcaster()
        self.plan = expected_plan()

    def tearDown(self):
        self.tx.close()

    def _wait_for(self, predicate, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_streams_full_packets_with_seq(self):
        err = self.tx.start(WIDE_CFG, CALLSIGN)
        self.assertIsNone(err)
        captor = self._captor()
        want = 2
        captor.push_audio(self.plan.frames_per_packet * want,
                          self.plan.pcm_bytes_per_frame)
        self.assertTrue(
            self._wait_for(lambda: len(self.kiss.sent) >= want
                           and self.tx.snapshot.packets_sent >= want),
            f"only sent {len(self.kiss.sent)}",
        )
        session = self.tx.snapshot.session
        for i, payload in enumerate(self.kiss.sent[:want]):
            env = protocol.decode_envelope(payload)
            self.assertEqual(env.kind, protocol.KIND_AUDIO)
            self.assertEqual(env.transfer_id, session)
            self.assertEqual(env.seq, i)
            body = protocol.decode_audio(env)
            self.assertEqual(body.callsign, CALLSIGN)
            self.assertEqual(body.frame_count, self.plan.frames_per_packet)
            self.assertEqual(body.tier.bitrate, self.plan.bitrate)
            self.assertEqual(body.tier.channels, self.plan.channels)
        self.assertEqual(self.tx.snapshot.packets_sent, len(self.kiss.sent))

    def _captor(self, timeout=2.0):
        end = time.monotonic() + timeout
        while FakeCaptor.last is None and time.monotonic() < end:
            time.sleep(0.005)
        return FakeCaptor.last

    def test_stop_reports_cancelled(self):
        self.tx.start(WIDE_CFG, CALLSIGN)
        self._captor().push_audio(self.plan.frames_per_packet,
                                  self.plan.pcm_bytes_per_frame)
        self._wait_for(lambda: len(self.kiss.sent) >= 1)
        self.tx.stop()
        self.assertTrue(self._wait_for(
            lambda: self.tx.snapshot.status == TX_CANCELLED))
        self.assertFalse(self.tx.busy())
        self.assertIn("stopped after", self.tx.snapshot.message)

    def test_link_failure_fails_snapshot(self):
        self.tx.start(WIDE_CFG, CALLSIGN)
        self.kiss.ok = False
        self._captor().push_audio(self.plan.frames_per_packet,
                                  self.plan.pcm_bytes_per_frame)
        self.assertTrue(self._wait_for(
            lambda: self.tx.snapshot.status == TX_FAILED))
        self.assertIn("KISS link lost", self.tx.snapshot.message)

    def test_queue_backpressure_paces_sender(self):
        sleeps: list = []

        def sleeper(seconds):
            sleeps.append(seconds)
            time.sleep(min(seconds, 0.01))

        tx, kiss, events, _ = make_broadcaster(sleep=sleeper)
        self.tx = tx  # tearDown closes it

        def hook(count):
            if count == 1:
                kiss.depth = 5  # right after the first packet: block rest

        kiss.on_send = hook
        tx.start(WIDE_CFG, CALLSIGN)
        self._captor().push_audio(self.plan.frames_per_packet * 3,
                                  self.plan.pcm_bytes_per_frame)
        self.assertTrue(self._wait_for(lambda: len(sleeps) >= 3))
        self.assertEqual(len(kiss.sent), 1)  # high water blocks the rest
        kiss.depth = 0
        self.assertTrue(self._wait_for(lambda: len(kiss.sent) >= 2))

    def test_payload_shrink_adapts_then_fails(self):
        tx, kiss, events, cfg = make_broadcaster()
        self.tx = tx
        fb = expected_plan().frame_bytes

        def hook(count):
            if count == 1:  # shrink so exactly 2 frames fit per packet
                cfg["cfg"]["payload_size"] = (
                    2 + protocol.HEADER_LEN + 9 + len(CALLSIGN) + 2 * fb
                )
            elif count == 6:  # shrink below one frame: broadcast must fail
                cfg["cfg"]["payload_size"] = (
                    2 + protocol.HEADER_LEN + 9 + len(CALLSIGN) + fb - 1
                )

        kiss.on_send = hook
        tx.start(WIDE_CFG, CALLSIGN)
        captor = self._captor()
        captor.push_audio(self.plan.frames_per_packet,
                          self.plan.pcm_bytes_per_frame)
        self._wait_for(lambda: len(kiss.sent) >= 1)
        captor.push_audio(self.plan.frames_per_packet,
                          self.plan.pcm_bytes_per_frame)
        self.assertTrue(self._wait_for(
            lambda: tx.snapshot.status == TX_FAILED, timeout=3.0))
        self.assertIn("payload_size", tx.snapshot.message)
        # packets 2..5 went out adapted to 2 frames each
        for payload in kiss.sent[1:5]:
            body = protocol.decode_audio(protocol.decode_envelope(payload))
            self.assertEqual(body.frame_count, 2)

    def test_trim_drops_oldest(self):
        from m73gui.constants import AUDIO_BUFFER_EXTRA_S
        plan = self.plan
        tx = AudioBroadcaster(
            emit=lambda *a, **k: None, send_frame=lambda p: True,
            get_status=lambda: None, get_config=lambda: None,
            outgoing_ids=set(), capture_factory=FakeCaptor,
        )
        tx._plan = plan
        limit = plan.frames_per_packet + round(
            AUDIO_BUFFER_EXTRA_S / (plan.duration_ms / 1000)
        )
        snap = type("S", (), {"frames_dropped": 0})()
        with tx._wake:
            for i in range(limit + 5):
                tx._frames.append(b"\x00" * plan.frame_bytes)
        tx._trim_frames(snap)
        with tx._wake:
            self.assertEqual(len(tx._frames), limit)
        self.assertEqual(snap.frames_dropped, 5)


class ReceiverBasics(unittest.TestCase):
    TIER = opuslink.TIERS[0]  # narrowband 8 kHz mono 6 kbps — cheap frames
    FRAMES = 5

    def setUp(self):
        self.events: list = []
        self.clock = [100.0]
        self.speaker = [True]
        self.players: list = []

        def player_factory(rate, ch, pull):
            class P:
                def __init__(self):
                    self.stopped = False
                    self.rate, self.ch, self.pull = rate, ch, pull
                def start(self):
                    pass
                def stop(self):
                    self.stopped = True
            p = P()
            self.players.append(p)
            return p

        self.rx = AudioReceiver(
            emit=lambda kind, **kw: self.events.append((kind, kw)),
            speaker_enabled=lambda: self.speaker[0],
            playback_factory=player_factory,
            monotonic=lambda: self.clock[0],
        )
        self.enc = opuslink.make_encoder(self.TIER, 60)
        self.frame_samples = self.TIER.sample_rate * 60 // 1000
        self.pcm_frame = self.frame_samples * 2

    def packet(self, session, seq, callsign="ON8X", tier=None):
        tier = tier or self.TIER
        enc = (self.enc if tier is self.TIER
               else opuslink.make_encoder(tier, 60))
        fs = tier.sample_rate * 60 // 1000
        pf = fs * tier.channels * 2
        frames = [
            opuslink.encode_frame(
                enc, tone_pcm(pf, phase_frames=seq * 10 + i), fs,
            )
            for i in range(self.FRAMES)
        ]
        return protocol.encode_audio(session, seq, tier, 60, callsign, frames)

    def feed(self, payload):
        env = protocol.decode_envelope(payload)
        self.rx.handle_packet(env, protocol.decode_audio(env))

    def snaps(self):
        return [kw["snap"] for k, kw in self.events if k == "audio_rx"]

    def test_first_packet_primes_and_plays_audio(self):
        self.feed(self.packet(1, 0))
        snap = self.snaps()[-1]
        self.assertEqual(snap.callsign, "ON8X")
        self.assertEqual(snap.packets, 1)
        self.assertEqual(snap.tier, "narrowband")
        data = self.rx.pull(240, self.TIER.channels)  # 30 ms
        self.assertEqual(len(data), 240 * 2)
        self.assertNotEqual(data, bytes(len(data)))  # real audio, not zeros
        self.assertTrue(self.rx.sessions[("ON8X", 1)].jitter.primed)
        self.assertEqual(len(self.players), 1)
        self.assertEqual(self.players[0].rate, self.TIER.sample_rate)

    def test_stale_packet_counted_not_fed(self):
        self.feed(self.packet(1, 0))
        self.feed(self.packet(1, 2))
        before = self.snaps()[-1]
        self.feed(self.packet(1, 1))  # late: stale, buffer must not grow
        after = self.snaps()[-1]
        self.assertEqual(after.stale, 1)
        self.assertEqual(after.packets, before.packets + 0)
        self.assertEqual(after.gap_packets, 1)  # from the 0->2 jump

    def test_settings_change_resets_session(self):
        self.feed(self.packet(1, 0))
        big = opuslink.TIERS[6]  # wideband 16 kHz mono 18k
        self.feed(self.packet(1, 1, tier=big))
        snap = self.snaps()[-1]
        self.assertEqual(snap.packets, 1)  # session rebuilt
        self.assertEqual(snap.sample_rate, 16000)
        self.assertEqual(len(self.players), 2)  # playback reopened 16 kHz

    def test_jitter_cap_is_one_packet_plus_2s(self):
        from m73gui.constants import AUDIO_BUFFER_EXTRA_S
        self.feed(self.packet(1, 0))
        s = self.rx.sessions[("ON8X", 1)]
        tier = self.TIER
        content = self.FRAMES * (tier.sample_rate * 60 // 1000) * 2
        extra = int(tier.sample_rate * AUDIO_BUFFER_EXTRA_S) * 2
        self.assertEqual(s.jitter.prime_bytes, content)
        self.assertEqual(s.jitter.max_bytes, content + extra)

    def test_speaker_off_drains_silently(self):
        self.feed(self.packet(1, 0))
        self.speaker[0] = False
        data = self.rx.pull(240, self.TIER.channels)
        self.assertEqual(data, bytes(len(data)))
        self.feed(self.packet(1, 1))
        self.speaker[0] = True
        data = self.rx.pull(240, self.TIER.channels)
        self.assertNotEqual(data, bytes(len(data)))

    def test_underrun_uses_limited_plc_then_silence(self):
        self.feed(self.packet(1, 0))
        total = 0
        for _ in range(200):  # way past one packet of audio
            data = self.rx.pull(480, self.TIER.channels)
            total += len(data)
        sess = self.rx.sessions[("ON8X", 1)]
        self.assertLessEqual(sess.plc_frames, 2)
        self.assertGreaterEqual(sess.plc_frames, 1)
        final = self.rx.pull(480, self.TIER.channels)
        self.assertEqual(final, bytes(len(final)))

    def test_sweep_retires_idle_sessions(self):
        self.feed(self.packet(1, 0))
        self.clock[0] += 16.0  # > idle floor, period unknown->0
        self.rx.sweep()
        self.assertEqual(self.rx.sessions, {})
        self.assertIsNone(self.rx.active_key)
        self.assertTrue(self.players[0].stopped)

    def test_second_talker_becomes_active(self):
        self.feed(self.packet(1, 0, callsign="A"))
        self.feed(self.packet(2, 0, callsign="B"))
        self.assertEqual(self.rx.active_key, ("B", 2))
        keys = {(s.callsign, s.session_id) for s in self.rx.sessions.values()}
        self.assertEqual(keys, {("A", 1), ("B", 2)})

    def test_speaker_getter_never_runs_under_the_receiver_lock(self):
        # Regression: tk.BooleanVar.get() from the PortAudio callback thread
        # deadlocked against a Tk thread waiting on this lock (Tcl marshals
        # to the main loop). The getter must be called lock-free.
        violations: list = []

        def getter():
            if not self.rx._lock.acquire(blocking=False):
                violations.append("lock held while getter ran")
                return True
            self.rx._lock.release()
            return True

        rx = AudioReceiver(
            emit=lambda kind, **kw: None, speaker_enabled=getter,
            playback_factory=lambda rate, ch, pull: _NullPlayer(),
            monotonic=lambda: self.clock[0],
        )
        payload = self.packet(1, 0)
        env = protocol.decode_envelope(payload)
        rx.handle_packet(env, protocol.decode_audio(env))
        rx.pull(240, self.TIER.channels)
        rx.pull(240, self.TIER.channels)
        self.assertEqual(violations, [])

    def test_concurrent_pull_and_feed_does_not_wedge(self):
        # background "audio device" pulling while the Tk side feeds and sweeps
        stop = threading.Event()
        pulls: list = []

        def pump():
            while not stop.is_set():
                pulls.append(len(self.rx.pull(240, self.TIER.channels)))
                time.sleep(0.001)

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        try:
            for seq in range(4):
                self.feed(self.packet(1, seq))
                self.clock[0] += 0.2
                self.rx.sweep()
                time.sleep(0.05)  # let the fake device pump get some CPU
        finally:
            stop.set()
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive(), "audio pump wedged")
        self.assertGreater(len(pulls), 10)
        self.assertTrue(all(n == 480 for n in pulls))

    def test_playback_failure_still_counts(self):
        def boom(rate, ch, pull):
            raise RuntimeError("no PortAudio")
        rx = AudioReceiver(
            emit=lambda kind, **kw: self.events.append((kind, kw)),
            speaker_enabled=lambda: True, playback_factory=boom,
            monotonic=lambda: self.clock[0],
        )
        rx.handle_packet(*(lambda e: (e, protocol.decode_audio(e)))(
            protocol.decode_envelope(self.packet(7, 0))))
        snap = self.snaps()[-1]
        self.assertEqual(snap.packets, 1)


class PlanHelper(unittest.TestCase):
    """audio_plan: the merged-config view shared by the app guard and tab."""

    def test_empty_config_no_plan(self):
        from m73gui.services.audio_tx import audio_plan
        self.assertIsNone(audio_plan({}))
        self.assertIsNone(audio_plan({"payload_size": 0}))

    def test_string_payload_size_coerced(self):
        from m73gui.services.audio_tx import audio_plan
        plan = audio_plan(dict(WIDE_CFG, payload_size="20000",
                               callsign=CALLSIGN))
        self.assertIsNotNone(plan)
        self.assertEqual(plan.bitrate, expected_plan().bitrate)

    def test_narrow_mode_no_plan_wide_mode_plan(self):
        from m73gui.services.audio_tx import audio_plan
        self.assertIsNone(audio_plan(dict(NARROW_CFG, callsign=CALLSIGN)))
        plan = audio_plan(dict(WIDE_CFG, callsign=CALLSIGN))
        self.assertEqual(plan, expected_plan())

    def test_pending_mode_uses_predicted_payload(self):
        # regression: live payload_size is still the OLD mode's MTU while a
        # faster mode is merely selected; the estimate must judge the
        # selection via the payload_bytes override (8 kHz wide 8PSK 3/4
        # normal x1 reported "not enough data rate" on 2026-11-10)
        from m73gui import phytime
        from m73gui.services.audio_tx import audio_plan
        sel = {
            "modem_type": 0, "modulation": "8PSK", "code_rate": "3/4",
            "frame_size": 1, "blocks": 1, "bandwidth": 2,
            "payload_size": 512,  # stale: previous mode still applied
            "callsign": CALLSIGN,
        }
        self.assertIsNone(audio_plan(sel))  # old behaviour: judged stale MTU
        payload = phytime.payload_bytes_for(sel)
        self.assertGreater(payload, 512)
        plan = audio_plan(sel, payload_bytes=payload)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.bitrate, 8000)  # raw ~12 k, 0.8 margin -> NB 8k top
        self.assertIsNone(audio_plan(sel, payload_bytes=0))


class EndToEnd(unittest.TestCase):
    """Broadcaster payloads are fed straight into a receiver."""

    def test_tx_rig_to_rx_playback(self):
        FakeCaptor.last = None
        tx, kiss, events, _ = make_broadcaster()
        plan = expected_plan()
        self.assertIsNone(tx.start(WIDE_CFG, CALLSIGN))
        end = time.monotonic() + 2.0
        while FakeCaptor.last is None and time.monotonic() < end:
            time.sleep(0.005)
        FakeCaptor.last.push_audio(
            plan.frames_per_packet * 2 + plan.frames_per_packet // 2,
            plan.pcm_bytes_per_frame,
        )
        end = time.monotonic() + 5.0
        while len(kiss.sent) < 2 and time.monotonic() < end:
            time.sleep(0.01)
        tx.stop(); tx.close()
        self.assertGreaterEqual(len(kiss.sent), 2)

        played: list = []

        def factory(rate, ch, pull):
            played.append((rate, ch))
            return _NullPlayer()

        rx = AudioReceiver(
            emit=lambda kind, **kw: None,
            speaker_enabled=lambda: True,
            playback_factory=factory,
        )
        for payload in kiss.sent:
            env = protocol.decode_envelope(payload)
            rx.handle_packet(env, protocol.decode_audio(env))
        self.assertEqual(len(played), 1)
        data = rx.pull(480, plan.channels)
        self.assertNotEqual(data, bytes(len(data)))


class _NullPlayer:
    def start(self):
        pass

    def stop(self):
        pass


if __name__ == "__main__":
    unittest.main()
