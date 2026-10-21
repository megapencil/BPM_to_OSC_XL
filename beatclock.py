# Part of BPM to OSC XL.
# Copyright (C) 2026 Mike Wilson
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version. See LICENSE.

"""Tempo estimation from detected beats and the beat clock that drives all output.

Everything here is causal: it only uses beats that already happened, nothing looks ahead.
Times are in seconds on one clock (time.perf_counter in the app), passed in by the caller,
so the logic can be tested with simulated time.
"""
from collections import deque
from math import floor
from threading import Lock

import numpy as np


def fold_bpm(bpm: float, min_bpm: float) -> float:
    """Folds a tempo into [min_bpm, 2 * min_bpm) by doubling or halving,
    trackers often lock to half or double the tempo"""
    while bpm < min_bpm:
        bpm *= 2
    while bpm >= 2 * min_bpm:
        bpm /= 2
    return bpm


class TempoEstimator:
    """Tempo from the spacing of detected beats.

    The detector's own bpm value is biased, but the beats it reports are evenly spaced,
    so a line fitted through the recent beat times gives a precise period.
    Missed beats are bridged, single stray beats ignored, and a consistently
    different spacing (a real tempo change) restarts the estimate.
    """

    def __init__(self, min_bpm: float = 90.0, history: int = 16, tolerance: float = 0.06, change_after: int = 4):
        """
        Args:
            min_bpm (float): reported tempo is folded into [min_bpm, 2 * min_bpm)
            history (int): number of beats the period is fitted over
            tolerance (float): allowed deviation of a beat interval from the period, as a fraction
            change_after (int): consecutive deviating beats that count as a tempo change
        """
        self.min_bpm = min_bpm
        self.tolerance = tolerance
        self.change_after = change_after
        self.beats = deque(maxlen=history)  # (time, beat number) of accepted beats
        self.period = None  # seconds per detected beat
        self.deviating = []  # times of consecutive beats that didn't fit the period

    def reset(self):
        self.beats.clear()
        self.period = None
        self.deviating = []

    @property
    def bpm(self):
        """Folded tempo, None until enough consistent beats were seen"""
        return None if self.period is None else fold_bpm(60.0 / self.period, self.min_bpm)

    def add_beat(self, t: float):
        """Adds a detected beat and returns the current tempo estimate (or None)"""
        if not self.beats:
            self.beats.append((t, 0))
            return None

        last_t, last_n = self.beats[-1]
        interval = t - last_t
        if interval <= 0:
            return self.bpm

        if self.period is None:
            # bootstrap: wait for 3 consistent intervals in a row
            self.beats.append((t, last_n + 1))
            times = np.array([b[0] for b in self.beats])
            intervals = np.diff(times)[-3:]
            if len(intervals) == 3:
                median = np.median(intervals)
                if np.all(abs(intervals - median) <= self.tolerance * median):
                    self.period = median
                    self._fit()
                else:
                    # drop everything before the inconsistent stretch
                    keep = list(self.beats)[-2:]
                    self.beats.clear()
                    self.beats.extend(keep)
            return self.bpm

        steps = round(interval / self.period)
        if 1 <= steps <= 4 and abs(interval - steps * self.period) <= self.tolerance * self.period:
            # fits the grid, possibly with missed beats in between
            self.beats.append((t, last_n + steps))
            self.deviating = []
            self._fit()
        else:
            # compare deviating beats with each other only, the gap back to the last
            # accepted beat says nothing about a new tempo
            self.deviating.append(t)
            if len(self.deviating) > self.change_after:
                intervals = np.diff(self.deviating)
                median = np.median(intervals)
                if np.all(abs(intervals - median) <= self.tolerance * median):
                    # consistently different spacing: the tempo changed, start over from these beats
                    self.beats.clear()
                    self.beats.extend((bt, n) for n, bt in enumerate(self.deviating))
                    self.period = median
                    self._fit()
                    self.deviating = []
                else:
                    self.deviating.pop(0)
        return self.bpm

    def _fit(self):
        """Least squares line through (beat number, time), its slope is the period"""
        if len(self.beats) >= 3:
            numbers = np.array([b[1] for b in self.beats], dtype=float)
            times = np.array([b[0] for b in self.beats])
            self.period = float(np.polyfit(numbers, times, 1)[0])


class BeatClock:
    """A steady beat clock. Its tempo and phase are set directly (tap, resync)
    or steered by detected beats; everything that is output follows this clock.

    The position is a continuous beat count: whole numbers are beats, beat 0 is the start of a bar.
    """

    def __init__(self):
        self._lock = Lock()
        self.period = None  # seconds per beat, None while the tempo is unknown
        self.anchor = 0.0  # time at which the position is 0
        self.epoch = 0  # increases on every resync, so a restarted bar is noticed even within the same beat
        self._off_beat = []  # phase errors of recent detections that were too far off to steer by

    @property
    def bpm(self):
        return None if self.period is None else 60.0 / self.period

    def position(self, t: float):
        """Continuous beat count at time t, None while the tempo is unknown"""
        with self._lock:
            return self._position(t)

    def _position(self, t):
        return None if self.period is None else (t - self.anchor) / self.period

    def time_of(self, position: float):
        """Time at which the clock reaches the given position"""
        with self._lock:
            return self.anchor + position * self.period

    def set_bpm(self, bpm: float, t: float):
        """Changes the tempo without a jump in position"""
        with self._lock:
            if self.period is None:
                self.period = 60.0 / bpm
                self.anchor = t
                self.epoch += 1
                return
            position = self._position(t)
            self.period = 60.0 / bpm
            self.anchor = t - position * self.period

    def resync(self, t: float):
        """Beat 1 of a new bar starts at t"""
        with self._lock:
            self.anchor = t
            self.epoch += 1
            self._off_beat = []

    def align_beat(self, t: float):
        """Shifts the phase so a beat falls on t (e.g. the last tap), keeping the beat count"""
        with self._lock:
            if self.period is None:
                return
            position = self._position(t)
            self.anchor += (position - round(position)) * self.period

    def nudge(self, t: float, gain: float = 0.25, window: float = 0.25, relock_after: int = 4):
        """Steers the phase towards a detected beat at time t.

        Detections within `window` beats of a clock beat pull the clock a fraction `gain` towards them.
        Detections far off are ignored (e.g. off-beats when the clock runs at half the detected tempo),
        unless several in a row agree, then the clock jumps to them.

        Returns:
            float: phase error of the detection in beats (positive: clock is ahead), None without tempo
        """
        with self._lock:
            position = self._position(t)
            if position is None:
                return None
            error = position - round(position)
            if abs(error) <= window:
                self.anchor += error * gain * self.period
                self._off_beat = []
            else:
                self._off_beat.append(error)
                if len(self._off_beat) >= relock_after:
                    errors = np.array(self._off_beat)
                    # errors near +0.5 and -0.5 are the same offset, compare them on the circle
                    angles = np.exp(2j * np.pi * errors)
                    if abs(angles.mean()) > 0.9:
                        mean_error = np.angle(angles.mean()) / (2 * np.pi)
                        self.anchor += mean_error * self.period
                    self._off_beat = []
            return error

    @staticmethod
    def beat_in_bar(position: float) -> int:
        """0-3 for the beat of a 4/4 bar the position is in"""
        return floor(position) % 4
