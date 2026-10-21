# Part of BPM to OSC XL.
# Copyright (C) 2026 Mike Wilson
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version. See LICENSE.

"""Real-time beat tracker: onset strength, tempo and beat phase with a confidence measure.

Everything is causal, it only uses audio that already arrived.

- Onset strength: spectral flux in three bands (low: kick, mid: snare / guitar attacks, high: hats),
  each band scaled by its own recent level so a kick low in the mix still counts, hats weighted least.
- Tempo: autocorrelation of the onset strength over the last seconds, scored at several multiples of each
  candidate beat period, folded into the tempo range, preferring the current tempo once locked.
- Phase: the onsets of the last few beats are folded onto one beat cycle. The peak is the beat phase,
  how far it sticks out of the average is the confidence. In a breakdown without rhythm the confidence
  drops below a threshold and the beat clock coasts instead of following noise.
- Fine tempo: observed beats with good confidence go through the TempoEstimator (line fit through beat times).
"""
from collections import deque

import numpy as np

from beatclock import TempoEstimator, fold_bpm

# samples per onset frame and analysis window at 44.1 kHz, scaled for other rates so the
# durations (and with them the tuned behaviour) stay the same
HOP = 256
WIN = 1024

# band edges in Hz and weights in the combined onset strength
BANDS = [(30, 150, 1.0), (150, 2500, 1.0), (2500, 11000, 0.5)]

# delay between a sound at the input and its peak in the onset strength: about the window centre (WIN / 2),
# minus a few ms measured on kicks with known timing
ONSET_DELAY_CORRECTION = -0.0048

# tempo is re-estimated this often (s). A tempo jump has to win this long (s) before it is taken,
# much longer for tempos in a metrical relation to the current one
TEMPO_UPDATE = 0.25
TEMPO_SWITCH = 2.0
TEMPO_SWITCH_RELATED = 6.0
RELATED_RATIOS = (3 / 2, 2 / 3, 4 / 3, 3 / 4)

# beat cycle resolution for the phase, and the smoothing across it (gaussian, 4% of a beat)
PHASE_BINS = 100
RECENT_BEATS = 3  # beats the quick confidence for the status display looks at
_d = np.minimum(np.arange(PHASE_BINS), PHASE_BINS - np.arange(PHASE_BINS)) / PHASE_BINS
PHASE_KERNEL_FFT = np.fft.fft(np.exp(-0.5 * (_d / 0.04) ** 2))


class BeatTracker:

    # defaults tuned on rock, pop, blues and electronic tracks against an offline reference tracker
    def __init__(self, min_bpm: float = 90.0, history_s: float = 8.0, phase_beats: float = 8.0,
                 min_confidence: float = 2.2, band_weights=None, half_life_beats: float = 4.0,
                 samplerate: int = 44100):
        """
        Args:
            min_bpm (float): tempo is folded into [min_bpm, 2 * min_bpm)
            history_s (float): seconds of onset strength the tempo is estimated from
            phase_beats (float): beats of onset strength the phase is measured over
            min_confidence (float): below this phase confidence, beats are not reported (clock coasts)
            band_weights (list): weight of low, mid and high band in the onset strength
            half_life_beats (float): older beats count half as much after this many beats
            samplerate (int): of the audio fed in, everything else is in seconds and beats
        """
        self.sr = samplerate
        self.hop = round(HOP * samplerate / 44100)
        self.win = round(WIN * samplerate / 44100)
        self.fps = samplerate / self.hop  # onset frames per second
        self.onset_delay = (self.win / 2) / samplerate + ONSET_DELAY_CORRECTION
        self.min_bpm = min_bpm
        self.phase_beats = phase_beats
        self.min_confidence = min_confidence

        self.window = np.hanning(self.win).astype(np.float32)
        self.samples = np.zeros(self.win, dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)  # samples not yet analysed
        freqs = np.fft.rfftfreq(self.win, 1 / samplerate)
        weights = band_weights or [w for _, _, w in BANDS]
        self.band_bins = [(np.searchsorted(freqs, lo), np.searchsorted(freqs, hi), w)
                          for (lo, hi, _), w in zip(BANDS, weights)]
        self.half_life_beats = half_life_beats
        self.prev_log = None
        self.band_level = np.ones(len(BANDS))  # running mean of each band's flux
        self.level_alpha = 1 / (4.0 * self.fps)  # ~4 s

        n = int(history_s * self.fps)
        self.onsets = deque(maxlen=n)  # onset strength per frame
        self.times = deque(maxlen=n)  # time of each frame's onset peak
        self.raw = deque(maxlen=int(0.2 * self.fps))  # recent combined flux, for the local mean

        self.period = None  # seconds per beat from the autocorrelation
        self.tempo_confidence = 0.0
        self.candidate = None  # (period, count) of a tempo change waiting for confirmation
        self.next_tempo_update = 0.0
        self.last_reported = -np.inf  # time of the last reported beat
        self.estimator = TempoEstimator(min_bpm, tolerance=0.04)
        self.confidence = 0.0  # phase confidence of the last check
        self.recent_confidence = 0.0  # the same over the last few beats only, for the status display
        self.bpm = None  # best current tempo estimate (folded), for display

    # ---- onset strength

    def add_audio(self, signal: np.ndarray, now: float):
        """Feeds mono audio, `now` is the time the last sample arrived.
        Returns a list of (beat_time, bpm, confidence) for beats found in it."""
        self.pending = np.concatenate([self.pending, signal])
        results = []
        hop = self.hop
        n_frames = len(self.pending) // hop
        for k in range(n_frames):
            frame = self.pending[k * hop:(k + 1) * hop]
            # time of the last sample of this frame
            frame_end = now - (len(self.pending) - (k + 1) * hop) / self.sr
            self._add_frame(frame, frame_end)
            beat = self._check_beat(frame_end)
            if beat is not None:
                results.append(beat)
        self.pending = self.pending[n_frames * hop:]
        return results

    def _add_frame(self, frame, frame_end):
        self.samples = np.concatenate([self.samples[self.hop:], frame])
        spectrum = np.abs(np.fft.rfft(self.samples * self.window))
        log = np.log1p(100 * spectrum)
        if self.prev_log is None:
            self.prev_log = log
            return
        diff = np.maximum(log - self.prev_log, 0)
        self.prev_log = log

        combined = 0.0
        for i, (lo, hi, weight) in enumerate(self.band_bins):
            flux = diff[lo:hi].sum() / (hi - lo)
            self.band_level[i] += self.level_alpha * (flux - self.band_level[i])
            combined += weight * flux / (self.band_level[i] + 1e-3)

        # silence gate, so noise isn't scaled up to onsets
        if np.sqrt(np.mean(self.samples ** 2)) < 1e-3:
            combined = 0.0

        # keep only what sticks out of the local mean
        self.raw.append(combined)
        onset = max(0.0, combined - np.mean(self.raw))
        self.onsets.append(onset)
        self.times.append(frame_end - self.onset_delay)

    # ---- tempo

    def _update_tempo(self):
        o = np.array(self.onsets)
        if len(o) < 3 * self.fps or o.sum() <= 0:
            return
        o = o - o.mean()
        n = len(o)
        spec = np.fft.rfft(o, 2 * n)
        acf = np.fft.irfft(spec * np.conj(spec))[:n]
        acf /= (n - np.arange(n))  # unbiased
        if acf[0] <= 0:
            return
        acf /= acf[0]

        bpms = np.arange(self.min_bpm, 2 * self.min_bpm, 0.25)
        lags = 60 * self.fps / bpms
        score = np.zeros(len(bpms))
        for k, w in ((1, 1.0), (2, 0.5), (3, 0.33), (4, 0.25)):
            score += w * np.interp(k * lags, np.arange(n), acf, right=0)
        if self.period is not None:
            # prefer staying near the current tempo
            current = fold_bpm(60 / self.period, self.min_bpm)
            prior = np.exp(-0.5 * (np.log(bpms / current) / 0.05) ** 2)
            score *= 0.6 + 0.4 * prior
        best = int(np.argmax(score))
        spread = score.std()
        self.tempo_confidence = (score[best] - score.mean()) / spread if spread > 0 else 0.0

        # parabolic interpolation between grid points
        bpm = bpms[best]
        if 0 < best < len(bpms) - 1:
            a, b, c = score[best - 1:best + 2]
            denom = a - 2 * b + c
            if denom < 0:
                bpm += 0.25 * 0.5 * (a - c) / denom
        period = 60 / bpm

        if self.tempo_confidence < 2.0:
            return
        if self.period is None:
            self.period = period
        elif abs(period - self.period) / self.period > 0.03:
            # a different tempo has to win for a while. Rhythms often suggest related tempos
            # (triplet feel: 4/3, half-time feel: 3/2 ...), those have to win much longer
            ratio = self.period / period
            related = any(abs(ratio / r - 1) < 0.02 for r in RELATED_RATIOS)
            if self.candidate is not None and abs(period - self.candidate[0]) / period < 0.02:
                self.candidate = (period, self.candidate[1] + 1)
                needed = TEMPO_SWITCH_RELATED if related else TEMPO_SWITCH
                if self.candidate[1] >= needed / TEMPO_UPDATE:
                    self.period = period
                    self.candidate = None
                    self.estimator.reset()
            else:
                self.candidate = (period, 1)
        else:
            self.candidate = None
            self.period += 0.3 * (period - self.period)

    # ---- phase

    @staticmethod
    def _fold(t, weights, period):
        """Onset weights folded onto one beat cycle, smoothed around the circle"""
        cycle = (t / period) % 1.0
        histogram = np.bincount((cycle * PHASE_BINS).astype(int) % PHASE_BINS, weights, PHASE_BINS)
        return np.real(np.fft.ifft(np.fft.fft(histogram) * PHASE_KERNEL_FFT))

    def _check_beat(self, now):
        if now >= self.next_tempo_update:
            self.next_tempo_update = now + TEMPO_UPDATE
            self._update_tempo()
        if self.period is None:
            return None
        period = self.period
        if now - self.last_reported < period:
            return None

        # fold the onsets of the last few beats onto one beat cycle, recent ones weighted more,
        # squared so strong hits dominate subdivisions. The peak is the beat, how much it
        # sticks out of the average is the confidence (about 1 for onsets without rhythm)
        n = int(self.phase_beats * period * self.fps)
        o = np.array(self.onsets)[-n:]
        t = np.array(self.times)[-n:]
        if o.sum() <= 0:
            self.confidence = 0.0
            self.recent_confidence = 0.0
            self.last_reported = now
            return None
        weights = o ** 2 * 0.5 ** ((t[-1] - t) / (self.half_life_beats * period))
        smooth = self._fold(t, weights, period)
        best = int(np.argmax(smooth))
        mean = smooth.mean()
        self.confidence = smooth[best] / mean if mean > 0 else 0.0

        # the same over just the last few beats reacts quicker when the beat goes (status display only,
        # too jumpy for steering the clock)
        recent = t > t[-1] - RECENT_BEATS * period
        recent_fold = self._fold(t[recent], o[recent] ** 2, period)
        recent_mean = recent_fold.mean()
        self.recent_confidence = recent_fold.max() / recent_mean if recent_mean > 0 else 0.0
        a, b, c = smooth[best - 1], smooth[best], smooth[(best + 1) % PHASE_BINS]
        denom = a - 2 * b + c
        offset = 0.5 * (a - c) / denom if denom < 0 else 0.0
        phase = ((best + 0.5 + offset) / PHASE_BINS) * period  # beats fall on phase + k * period

        # latest beat of that grid that is safely in the past
        safe = now - 0.03
        beat_time = phase + np.floor((safe - phase) / period) * period
        if beat_time <= self.last_reported + 0.5 * period:
            beat_time += period
            if beat_time > safe:
                return None
        self.last_reported = beat_time

        if self.confidence < self.min_confidence:
            return None
        fine = self.estimator.add_beat(beat_time)
        coarse = fold_bpm(60 / period, self.min_bpm)
        # the line fit is more precise, as long as it agrees with the autocorrelation
        self.bpm = fine if fine is not None and abs(fine - coarse) / coarse < 0.02 else coarse
        # confidence for the caller: 0 at the threshold, 1 for a clearly marked beat
        return beat_time, self.bpm, min(1.0, (self.confidence - self.min_confidence) / 2.0)
