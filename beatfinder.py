# Audio input for BPM to OSC XL.
# Based on beatfinder.py of BPMtoOSC by Leonhard Axtner (d00mfish), 2022.
# Modified by Mike Wilson, 2026: own beat tracker instead of aubio, loopback input, sample rate handling.
# Copyright (C) 2022 Leonhard Axtner
# Copyright (C) 2026 Mike Wilson
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version. See LICENSE.

import numpy as np
import pyaudiowpatch as pyaudio  # PyAudio with WASAPI loopback, to listen to what the PC plays
from collections import deque
from time import perf_counter


#local
import osc_client
from tracker import BeatTracker



class BeatPrinter:
    def __init__(self):
        self.state: int = 0
        self.spinner = "▚▞"

    def print_bpm(self, bpm: float) -> None:
        print(f"{self.spinner[self.state]}\t{bpm:.1f} BPM")

        self.state = (self.state + 1) % len(self.spinner)


class BeatDetector:

    SAMPLERATE: int = 44100

    def __init__(self, client: osc_client.OSCclient, audio_device_index: int = 1, parent=None, buf_size: int = 128,
                 min_bpm: float = 90.0, open_stream: bool = True):
        """
        Args:
            client (osc_client.OSCclient): OSC client for the resync message
            audio_device_index (int): pyaudio input device
            parent (Main_Frame): gets on_detected_beat(time, bpm, confidence) calls, None for console output
                The device can be an input or a loopback device (a copy of what an output plays)
            buf_size (int): samples per audio callback
            min_bpm (float): reported tempo is folded into [min_bpm, 2 * min_bpm)
            open_stream (bool): False for feeding audio through process() directly (tests)
        """
        # arguments
        self.client = client  # OSC client
        self.audio_device_index = audio_device_index
        self.parent = parent  # MainFrame
        self.buf_size: int = buf_size  # buffer size

        # variables
        self.input_latency = 0.0  # seconds between sound at the input and the callback getting it
        self.level_reset = None  # Timer for resetting level
        self.level_queue = deque(maxlen=20)  # RMS level of the recent callbacks
        self.last_audio = None  # perf_counter time of the last callback (loopback sends nothing while silent)
        self.samplerate = self.SAMPLERATE
        self.channels = 1
        self.p = None
        self.stream = None
        if not open_stream:
            self.tracker = BeatTracker(min_bpm, samplerate=self.samplerate)
            return

        self.p: pyaudio.PyAudio = pyaudio.PyAudio()

        # Callback to GUI
        if self.parent is not None:
            device = self.p.get_device_info_by_index(self.audio_device_index)
            if device.get('isLoopbackDevice'):
                # loopback runs at the output's own rate and channel count (WASAPI shared mode)
                self.samplerate = int(device['defaultSampleRate'])
                self.channels = max(1, int(device['maxInputChannels']))
            else:
                # inputs are resampled by Windows. Mix the first two, a stereo pair would otherwise
                # only be heard on its left side
                self.channels = max(1, min(2, int(device.get('maxInputChannels', 1))))
            self.tracker = BeatTracker(min_bpm, samplerate=self.samplerate)
            self.stream: pyaudio.Stream = self.p.open(
                format=pyaudio.paFloat32,
                channels=self.channels,
                rate=self.samplerate,
                input=True,
                input_device_index=self.audio_device_index,
                frames_per_buffer=self.buf_size,
                stream_callback=self._GUI_callback
            )
            self.input_latency = self.stream.get_input_latency()

        # Callback to Console (standalone)
        else:
            self.tracker = BeatTracker(min_bpm, samplerate=self.samplerate)
            self.stream: pyaudio.Stream = self.p.open(
                format=pyaudio.paFloat32,
                channels=1,
                rate=self.SAMPLERATE,
                input=True,
                frames_per_buffer=self.buf_size,
                stream_callback=self._STANDALONE_callback
            )
            # init output class
            self.spinner = BeatPrinter()

    @property
    def estimator(self):
        """Tempo estimator with the recent beats, the GUI snaps resyncs to them"""
        return self.tracker.estimator

    def _GUI_callback(self, in_data, frame_count, time_info, status):
        """Callback for pyaudio stream\n
        Args:
            in_data (array): data from pyaudio stream
            frame_count (int): number of frames in in_data
            time_info (): time info from pyaudio stream
            status (): status from pyaudio stream\n
        Returns:
            None, paContinue: Tells pyaudio to continue streaming
        """
        now = perf_counter()
        signal = np.frombuffer(in_data, dtype=np.float32)
        if self.channels > 1:
            # first two channels (left and right, also of a surround output)
            signal = signal.reshape(-1, self.channels)[:, :2].mean(axis=1, dtype=np.float32)
        self.process(signal, now)

        # terminate stream if running is stopped
        return (None, pyaudio.paContinue) if self.parent.running else (None, pyaudio.paComplete)

    def process(self, signal, now: float):
        """Analyses mono audio\n
        Calculates RMS Level into level_queue to fetch by PeakMeter\n
        Passes every beat the tracker is confident about to the GUI\n
        Args:
            signal (np.ndarray): float32 samples
            now (float): perf_counter time the last sample was received
        """
        # RMS level of every callback into a circular buffer, for the level meter
        self.level_queue.append(float(np.sqrt(signal.dot(signal) / signal.size)))
        self.last_audio = now

        # the last sample was audible at the input input_latency before it arrived here
        for beat_time, bpm, confidence in self.tracker.add_audio(signal, now - self.input_latency):
            if self.parent is not None and self.parent.running:
                self.parent.on_detected_beat(beat_time, bpm, confidence)

    def _STANDALONE_callback(self, in_data, frame_count, time_info, status):
        signal = np.frombuffer(in_data, dtype=np.float32)
        for _, bpm, _ in self.tracker.add_audio(signal, perf_counter()):
            self.spinner.print_bpm(bpm)
        return None, pyaudio.paContinue  # Tell pyAudio to continue

    def resync_bar(self):
        """Send resync command"""
        self.client.send_osc(self.parent.config['OSC']['RESYNC_BAR_ADRESS'], 1)

    def __del__(self):
        """Close pyaudio stream and terminate pyaudio
        """
        if self.level_reset is not None:
            self.level_reset.cancel()
        if self.stream is not None:
            self.stream.close()
        if self.p is not None:
            self.p.terminate()

if __name__ == "__main__":
    import time

    client = osc_client.OSCclient("127.0.0.1", 7000)
    bd = BeatDetector(client, buf_size=128)
    # Audio processing happens in separate thread, so put this thread to sleep
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
