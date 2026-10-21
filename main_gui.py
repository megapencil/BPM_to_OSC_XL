#!/usr/bin/env python
# -*- coding: utf-8 -*-
# BPM to OSC XL: detects the beat of incoming audio and sends tempo, beat and phase over OSC.
#
# Based on BPMtoOSC by Leonhard Axtner (d00mfish), 2022: https://github.com/d00mfish/BPM-to-OSC
# Modified by Mike Wilson, 2026: new beat tracking, beat clock, beat / phase OSC output,
# loopback audio input and a new interface (see README.md, "Changes from the original").
#
# Copyright (C) 2022 Leonhard Axtner
# Copyright (C) 2026 Mike Wilson
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version. See LICENSE.
#
# Python 3.13 64-Bit, wxPython 4.3.2
#
# TODO:
# - Other output (Midi clock?)

# Imports:
import re
import sys
from os import remove
from pathlib import Path
from platform import system
from time import perf_counter
from threading import Thread, Event
from math import floor, log10
import ctypes

# local
import beatfinder
import osc_client
from beatclock import BeatClock

import configparser
import pyaudiowpatch as pyaudio  # PyAudio with WASAPI loopback, to listen to what the PC plays


import wx
import widgets


CONF_PATH = Path(Path.home(), "AppData/Roaming/BPM_to_OSC_XL/lastsession.ini")
# settings of the earlier BPMtoOSC versions, read when there are none of our own yet
OLD_CONF_PATH = Path(Path.home(), "AppData/Roaming/BPMtoOSC/lastsession.ini")
APP_NAME = "BPM to OSC XL"

DEFAULT_CONFIG = {
    'OSC': {'IP': '127.0.0.1',
            'PORT': '7000',
            'RESYNC_BAR_ADRESS': '/composition/tempocontroller/resync',
            'BPM_ADRESS': '/composition/tempocontroller/tempo',
            'BPM_SEND_FORMAT': 'resolume',  # 'resolume' (0-1 range) or 'bpm' (raw value)
            'BEAT_PREFIX': '/bpmtoosc',  # <prefix>/beat, /snare, /phase and /bar_phase
            'SEND_BEAT': 'yes'},
    'AUDIO': {'device_name': ''},
    'TEMPO': {'min_bpm': '90',  # detected tempo is folded into min_bpm to 2 * min_bpm
              'output_offset_ms': '0'},  # output runs this much ahead, to make up for latency after the app
    'UI': {'always_on_top': 'no',
           'compact': 'no',
           'settings_open': 'yes',
           'full_width': '480',
           'compact_width': '420',
           'global_resync': 'yes',  # resync hotkey that works while another program has the focus
           'global_resync_key': 'Ctrl+Alt+R'},  # e.g. F9, Pause, Scroll_Lock, Ctrl+Shift+R, KP_5
}

THEME = {'live_on': (0, 190, 200),
         'send_on': (230, 45, 45),
         'running': (150, 40, 40),
         'led_off': (58, 58, 58),
         'led_on': (200, 200, 200),
         'led_downbeat': (240, 55, 45),
         'status_sync': (150, 220, 150),
         'status_manual': (225, 140, 140),
         'status_coasting': (235, 185, 70),
         'button': (52, 52, 52),
         'button_text': (235, 235, 235),
         'sync_fill': (38, 82, 52),
         'toggle_fill': (42, 70, 120),
         'track': (52, 52, 52),
         'heading': (150, 150, 150)}

SEND_FORMATS = [('resolume', "Resolume (0-1)"), ('bpm', "BPM value")]

TEMPO_RANGES = [60, 70, 80, 90, 100]  # lower end of the tempo range, the upper end is twice that

PHASE_INTERVAL = 1 / 60  # seconds between phase messages

SHORTCUTS = """Space\tTap tempo
R\tResync bar
S\tToggle sync
H\tHalf time (1/2)
Up / Down\t+1 / -1 BPM
Right / Left\tx2 / /2 BPM
F5\tStart / Stop

Ctrl+E\tShow / hide settings
Ctrl+M\tCompact mode
Ctrl+T\tAlways on top
F1\tThis help"""

# system wide hotkey for resync, works while another program (e.g. Notch) has the focus
HOTKEY_RESYNC = 1
HOTKEY_MODIFIERS = {wx.ACCEL_ALT: 0x0001, wx.ACCEL_CTRL: 0x0002, wx.ACCEL_SHIFT: 0x0004}  # windows MOD_* flags
MOD_NOREPEAT = 0x4000  # holding the key down doesn't fire again and again
VIRTUAL_KEYS = {wx.WXK_PAUSE: 0x13, wx.WXK_SCROLL: 0x91, wx.WXK_INSERT: 0x2D, wx.WXK_HOME: 0x24, wx.WXK_END: 0x23,
                wx.WXK_PAGEUP: 0x21, wx.WXK_PAGEDOWN: 0x22,
                **{wx.WXK_F1 + i: 0x70 + i for i in range(24)},
                **{wx.WXK_NUMPAD0 + i: 0x60 + i for i in range(10)}}


def parse_hotkey(text: str):
    """'Ctrl+Alt+R', 'F9', 'Pause' ... -> (windows modifier flags, virtual key code), None if not understood"""
    entry = wx.AcceleratorEntry()
    with wx.LogNull():  # wx would report unknown key names in a box of its own
        if not text or not entry.FromString(text):
            return None
    code = entry.GetKeyCode()
    if code in VIRTUAL_KEYS:
        key = VIRTUAL_KEYS[code]
    elif code < 128 and chr(code).isalnum():
        key = ord(chr(code).upper())
    else:
        return None
    modifiers = MOD_NOREPEAT
    for flag, modifier in HOTKEY_MODIFIERS.items():
        if entry.GetFlags() & flag:
            modifiers |= modifier
    return modifiers, key


def clean_host(text: str) -> str:
    """Strips spaces and leading zeros of an ip address (127.000.000.001 -> 127.0.0.1), leaves names alone"""
    text = text.strip().replace(" ", "")
    parts = text.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        return ".".join(str(int(part)) for part in parts)
    return text


def is_valid_host(text: str) -> bool:
    """An IPv4 address or a computer name"""
    parts = text.split(".")
    if all(part.isdigit() for part in parts):
        return len(parts) == 4 and all(0 <= int(part) <= 255 for part in parts)
    return re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*", text) is not None


def read_config() -> configparser.ConfigParser:
    """Reads lastsession.ini and fills in any missing values with defaults"""
    config = configparser.ConfigParser()
    try:
        config.read(CONF_PATH if CONF_PATH.exists() else OLD_CONF_PATH)
    except configparser.Error:
        print("Error reading config file. Deleting and creating new one.")
        if CONF_PATH.exists():
            remove(CONF_PATH)
        config = configparser.ConfigParser()

    for section, values in DEFAULT_CONFIG.items():
        if not config.has_section(section):
            config.add_section(section)
        for key, value in values.items():
            if not config.has_option(section, key):
                config.set(section, key, value)
    return config


class Main_Frame(wx.Frame):
    '''Frame Class
    '''
    sel_msg_frame = None
    sel_bus_frame = None
    CONF_PATH = CONF_PATH

    def __init__(self, parent=None, config=None):
        """Initialize Config Window
            - Reads config file and sets variables
            - Initializes GUI
        """
        # wx.Frame init
        style_dep = wx.DEFAULT_FRAME_STYLE ^ wx.MAXIMIZE_BOX  # ^ wx.RESIZE_BORDER
        super(Main_Frame, self).__init__(parent, title=APP_NAME, style=style_dep)

        # Config Setup
        self.config = config if config is not None else read_config()
        self.theme = THEME

        # Manage close event
        wx.CloseEvent.SetCanVeto(wx.CloseEvent(), False)

        # Audio Setup
        self.audio = pyaudio.PyAudio()
        self.audio_device_indices = list()  # pyaudio device index for each entry in the device combobox

        # OSC Setup
        self.osc_client = None

        # BPM Setup
        self.beatfinder = None  # audio analysis instance
        self.send_bpm = 128.0  # tempo when sync is disabled (holds last live, tapped or edited value)
        self.beat_divider = 1  # divides beat to get 1/2, 1/4
        self.clock = BeatClock()  # all output follows this clock, steered by detected beats or set by hand
        self.clock_thread = None
        self.clock_wake = Event()  # wakes the clock thread early when tempo or phase changed
        self.last_sent_bpm = None
        self.last_detected = None  # perf_counter time of the last confident detected beat
        self.output_offset = self.config.getfloat('TEMPO', 'output_offset_ms') / 1000

        self.last_tap = list()  # list of taps to determine bpm
        self.buttons_to_disable = list()  # list of buttons to disableon start/stop

        # Flags
        self.sync = True  # sync live bpm to send bpm
        self.running = False
        self.compact = False
        self.settings_open = self.config.getboolean('UI', 'settings_open')
        self.led_counter = 3
        self.detection_state = None

        self.InitUI()
        self.InitMenu()
        self.update_layout()
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)

        # restore window state from last session
        self.set_always_on_top(self.config.getboolean('UI', 'always_on_top'))
        self.set_compact(self.config.getboolean('UI', 'compact'))
        self.restore_position()
        self.button_startstop.SetFocus()  # keep focus out of text fields, so shortcuts work right away
        self.Bind(wx.EVT_HOTKEY, self.on_global_resync, id=HOTKEY_RESYNC)
        if self.config.getboolean('UI', 'global_resync'):
            wx.CallAfter(self.set_global_resync, True)  # after the window is up, in case it has to report a problem

# LEVEL METER
        self.uv_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.OnUVTimer)

        self.uv_timer.Start(20)

    def OnUVTimer(self, event):
        if self.running:
            finder = self.beatfinder
            queue = list(finder.level_queue)
            stale = finder.last_audio is None or perf_counter() - finder.last_audio > 0.3
            if stale or not queue:
                # loopback sends nothing at all while the output is silent
                level = 0.0
            else:
                # meter in dB: -60 dBFS (and below) is empty, 0 dBFS is full
                rms = sum(queue) / len(queue)
                level = max(0.0, min(100.0, (20 * log10(max(rms, 1e-9)) + 60) / 60 * 100))
            self.level_meter.SetLevel(level)
        else:
            level = 0
        self.show_detection_state(level)

    def show_detection_state(self, level):
        """Status above the sync button: is the tempo coming from the music, or is the clock coasting?"""
        if not self.running:
            state = None
        elif level < 1:
            state = "no signal"
        elif self.last_detected is None or self.beatfinder.tracker.period is None:
            state = "listening"
        elif perf_counter() - self.last_detected > 2.5 * self.beatfinder.tracker.period + 0.3:
            state = "coasting"  # no confident beat for a while
        else:
            # how clearly the last few beats stand out; a little hysteresis so it doesn't flicker
            recent = self.beatfinder.tracker.recent_confidence
            if self.detection_state == "locked":
                state = "coasting" if recent < 2.2 else "locked"
            else:
                state = "locked" if recent >= 2.6 else "coasting"
        if state == self.detection_state:
            return
        self.detection_state = state
        colours = {"locked": self.theme['status_sync'], "coasting": self.theme['status_coasting'],
                   "no signal": self.theme['status_manual'], "listening": self.theme['heading']}
        self.status_text.SetLabel(f"\u25cf {state.upper()}" if state else "")
        if state:
            self.status_text.SetForegroundColour(colours[state])
        self.status_text.SetToolTip({
            "locked": "Beats are clearly detected, the tempo follows the music",
            "coasting": "No clear beat right now (breakdown?), the clock carries on at the last tempo",
            "no signal": "The audio input is silent",
            "listening": "Waiting for a few clear beats to find the tempo",
        }.get(state, ""))
        self.status_text.Refresh()

    def InitUI(self):
        """Actual GUI Setup of main config Window
        """

        panel = wx.Panel(self)
        self.panel = panel
        # set before the controls are created, they inherit it
        panel.SetFont(widgets.font(9))
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.main_sizer = sizer
        margin = 16

        buttonfont = widgets.font(10, wx.FONTWEIGHT_SEMIBOLD)
        smallbuttonfont = widgets.font(9)

        def heading(text):
            """Small grey section title instead of a box around the section"""
            label = wx.StaticText(panel, label=text.upper())
            label.SetFont(widgets.font(8, wx.FONTWEIGHT_SEMIBOLD))
            label.SetForegroundColour(self.theme['heading'])
            return label

# SETTINGS HEADER folds OSC and audio settings away
        self.settings_header = widgets.FlatButton(panel, "SETTINGS", self.theme, size=(-1, 24),
                                                  font_=widgets.font(8, wx.FONTWEIGHT_SEMIBOLD),
                                                  subtle=True, align_left=True)
        self.settings_header.SetForegroundColour(self.theme['heading'])
        self.settings_header.SetToolTip("Show or hide the settings (Ctrl+E)")
        self.Bind(wx.EVT_BUTTON, lambda event: self.set_settings_open(not self.settings_open), self.settings_header)
        sizer.Add(self.settings_header, 0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, border=margin - 6)

# OSC OUTPUT
        self.ip_stuff_sizer = wx.BoxSizer(wx.VERTICAL)
        self.ip_stuff_sizer.Add(heading("OSC output"), 0, wx.BOTTOM, border=6)
        ip_row = wx.BoxSizer(wx.HORIZONTAL)

        # Textbox IP-Adress (or computer name)
        self.text_ip = wx.TextCtrl(panel, value=clean_host(self.config['OSC']['IP']), size=(150, -1))
        self.text_ip.SetHint("IP address or computer name")
        self.text_ip.SetToolTip("IP address or name of the computer receiving the OSC messages")
        self.text_ip.Bind(wx.EVT_TEXT, self.on_ip_text)
        self.text_ip.Bind(wx.EVT_KILL_FOCUS, self.on_ip_done)
        ip_row.Add(self.text_ip, 0, wx.ALIGN_CENTER_VERTICAL)

        # Numberbox Port
        self.text_port = wx.SpinCtrl(panel)
        self.text_port.SetMax(65535)
        self.text_port.SetMin(0)
        self.text_port.SetValue(self.config['OSC']['PORT'])
        ip_row.Add(self.text_port, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, border=6)

        # OSC destination and addresses:  | label | address | format / defaults |
        address_sizer = wx.FlexGridSizer(cols=3, vgap=6, hgap=8)
        address_sizer.AddGrowableCol(1)

        send_to_label = wx.StaticText(panel, label="Send to")
        send_to_label.SetForegroundColour(self.theme['heading'])
        address_sizer.Add(send_to_label, 0, wx.ALIGN_CENTER_VERTICAL)
        address_sizer.Add(ip_row, 0, wx.EXPAND)
        address_sizer.AddSpacer(0)

        self.text_bpm_address = self.add_address_row(panel, address_sizer, "Tempo", 'BPM_ADRESS')

        # Send Value Selection
        self.send_format = wx.Choice(panel, choices=[label for _, label in SEND_FORMATS])
        format_keys = [key for key, _ in SEND_FORMATS]
        current_format = self.config['OSC']['BPM_SEND_FORMAT']
        self.send_format.SetSelection(format_keys.index(current_format) if current_format in format_keys else 0)
        self.send_format.SetToolTip("Resolume (0-1): tempo mapped from 20-500 BPM to 0-1, as Resolume expects\n"
                                    "BPM value: the plain BPM number as a float")
        address_sizer.Add(self.send_format, 0, wx.EXPAND)
        self.Bind(wx.EVT_CHOICE, self.on_send_format, self.send_format)

        self.text_resync_address = self.add_address_row(panel, address_sizer, "Resync", 'RESYNC_BAR_ADRESS')

        self.button_osc_defaults = widgets.FlatButton(panel, "Defaults", self.theme, size=(-1, 26), font_=smallbuttonfont)
        self.button_osc_defaults.SetToolTip("Reset addresses to their defaults")
        address_sizer.Add(self.button_osc_defaults, 0, wx.EXPAND)
        self.Bind(wx.EVT_BUTTON, self.on_button_osc_defaults, self.button_osc_defaults)

        self.text_beat_prefix = self.add_address_row(panel, address_sizer, "Beat", 'BEAT_PREFIX')
        self.text_beat_prefix.SetToolTip("Address prefix for beat and phase messages:\n"
                                         "<prefix>/beat\t1-4 on every beat\n"
                                         "<prefix>/snare\t1 on beats 2 and 4, 0 on 1 and 3\n"
                                         "<prefix>/phase\t0-1 through each beat, 60 times a second\n"
                                         "<prefix>/bar_phase\t0-1 through each bar, 60 times a second")
        self.check_send_beat = wx.CheckBox(panel, label="Send")
        self.check_send_beat.SetValue(self.config.getboolean('OSC', 'SEND_BEAT'))
        self.check_send_beat.SetToolTip("Send beat and phase messages")
        address_sizer.Add(self.check_send_beat, 0, wx.ALIGN_CENTER_VERTICAL)
        self.Bind(wx.EVT_CHECKBOX, self.on_check_send_beat, self.check_send_beat)

        # Output offset, to make up for render and projector latency
        latency_label = wx.StaticText(panel, label="Latency")
        latency_label.SetForegroundColour(self.theme['heading'])
        address_sizer.Add(latency_label, 0, wx.ALIGN_CENTER_VERTICAL)
        latency_row = wx.BoxSizer(wx.HORIZONTAL)
        self.spin_latency = wx.SpinCtrl(panel, size=(80, -1), min=-200, max=500,
                                        initial=round(self.output_offset * 1000))
        self.spin_latency.SetToolTip("Send beats and phase this many milliseconds early,\n"
                                     "to make up for render and projector delay")
        self.spin_latency.Bind(wx.EVT_SPINCTRL, self.on_latency)
        self.spin_latency.Bind(wx.EVT_TEXT, self.on_latency)
        latency_row.Add(self.spin_latency, 0, wx.ALIGN_CENTER_VERTICAL)
        latency_hint = wx.StaticText(panel, label="ms early, for render and projector delay")
        latency_hint.SetForegroundColour(self.theme['heading'])
        latency_row.Add(latency_hint, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, border=8)
        address_sizer.Add(latency_row, 0, wx.EXPAND)
        address_sizer.AddSpacer(0)

        self.ip_stuff_sizer.Add(address_sizer, 0, wx.EXPAND)
        sizer.Add(self.ip_stuff_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, border=margin)

# AUDIO INPUT
        self.audiosizer = wx.BoxSizer(wx.VERTICAL)
        self.audiosizer.Add(heading("Audio input"), 0, wx.BOTTOM, border=6)
        audio_row = wx.BoxSizer(wx.HORIZONTAL)

        # Audio device selection
        self.audio_selection = wx.ComboBox(panel, style=wx.CB_READONLY)
        # long device names shouldn't widen the window, the list still shows them in full
        self.audio_selection.SetMinSize(wx.Size(180, -1))
        self.on_button_reload(None)  # fill combobox with devices
        audio_row.Add(self.audio_selection, 1, wx.EXPAND)

        # Tempo range the detected tempo is folded into
        self.tempo_range = wx.Choice(panel, choices=[f"{low}-{2 * low} BPM" for low in TEMPO_RANGES])
        min_bpm = self.config.getint('TEMPO', 'min_bpm')
        self.tempo_range.SetSelection(TEMPO_RANGES.index(min_bpm) if min_bpm in TEMPO_RANGES else TEMPO_RANGES.index(90))
        self.tempo_range.SetToolTip("Detected tempo is doubled or halved into this range")
        audio_row.Add(self.tempo_range, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, border=8)
        self.Bind(wx.EVT_CHOICE, self.on_tempo_range, self.tempo_range)
        self.audiosizer.Add(audio_row, 0, wx.EXPAND)

        # Input level
        self.level_meter = widgets.LevelMeter(panel, self.theme)
        self.level_meter.SetToolTip("Input level, -60 to 0 dBFS")
        self.audiosizer.Add(self.level_meter, 0, wx.EXPAND | wx.TOP, border=8)

        sizer.Add(self.audiosizer, 0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, border=margin)

# BEAT LIGHTS
        self.beat_lights = widgets.BeatLights(panel, self.theme)
        sizer.Add(self.beat_lights, 0, wx.EXPAND | wx.TOP | wx.LEFT | wx.RIGHT, border=margin)

# BPM DISPLAYS: | LIVE | SYNC / 1/2 | SEND |
        numberfont = widgets.font(30, wx.FONTWEIGHT_SEMIBOLD, display=True)

        # detection status, above the sync button in the middle column
        self.status_text = wx.StaticText(panel, label="", style=wx.ALIGN_CENTRE_HORIZONTAL | wx.ST_NO_AUTORESIZE)
        self.status_text.SetFont(widgets.font(8, wx.FONTWEIGHT_SEMIBOLD))
        self.status_text.SetMinSize(self.status_text.GetTextExtent("\u25cf NO SIGNAL") + wx.Size(4, 0))

        # pulse on the beat, under the live tempo when synced, under the send tempo in manual;
        # sized for the widest tempo so the layout doesn't jump around
        self.live_number = widgets.PulseNumber(panel, self.theme['live_on'], numberfont)
        self.send_number = widgets.PulseNumber(panel, self.theme['send_on'], numberfont)

# SYNC & HALF TIME BUTTONS between the displays
        # as tall as the numbers' flash boxes, both buttons the same height
        middle = wx.BoxSizer(wx.VERTICAL)
        self.button_sync = widgets.FlatButton(panel, 'SYNC', self.theme, toggle=True, size=(96, 20),
                                              accent=self.theme['sync_fill'], font_=buttonfont)
        self.button_sync.SetForegroundColour(self.theme['status_sync'])
        self.button_sync.SetValue(True)
        self.button_sync.SetToolTip("Sync send tempo to live tempo (S)")
        self.buttons_to_disable.append(self.button_sync)
        middle.Add(self.button_sync, 1, wx.EXPAND)
        self.Bind(wx.EVT_TOGGLEBUTTON, self.on_button_sync, self.button_sync)

        self.button_halftime = widgets.FlatButton(panel, '1/2', self.theme, toggle=True, size=(96, 20),
                                                  accent=self.theme['toggle_fill'], font_=buttonfont)
        self.button_halftime.SetToolTip("Half time (H)")
        self.buttons_to_disable.append(self.button_halftime)
        middle.AddSpacer(6)  # a spacer, not a border: a border would count towards this button's share
        middle.Add(self.button_halftime, 1, wx.EXPAND)
        self.Bind(wx.EVT_TOGGLEBUTTON, self.on_button_halftime, self.button_halftime)

        # one grid, so numbers, flash boxes and buttons share the same top and bottom edges
        bpm_grid = wx.FlexGridSizer(rows=2, cols=3, vgap=4, hgap=0)
        bpm_grid.AddGrowableCol(0, 1)
        bpm_grid.AddGrowableCol(2, 1)
        bpm_grid.Add(heading("Live"), 0, wx.ALIGN_CENTRE_HORIZONTAL)
        bpm_grid.Add(self.status_text, 0, wx.EXPAND)
        bpm_grid.Add(heading("Send"), 0, wx.ALIGN_CENTRE_HORIZONTAL)
        bpm_grid.Add(self.live_number, 0, wx.EXPAND)
        bpm_grid.Add(middle, 0, wx.EXPAND)
        bpm_grid.Add(self.send_number, 0, wx.EXPAND)
        sizer.Add(bpm_grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, border=margin - 4)

# BUTTONS: | START / STOP | TAP | RESYNC |
#          |  -1  |  +1  |  /2  |  x2  |
        self.button_sizer = wx.BoxSizer(wx.VERTICAL)
        top_row = wx.BoxSizer(wx.HORIZONTAL)
        bottom_row = wx.BoxSizer(wx.HORIZONTAL)

        def button(label, tooltip, handler, row, height, toggle=False, disable=True):
            b = widgets.FlatButton(panel, label, self.theme, toggle=toggle, size=(-1, height), font_=buttonfont)
            b.SetToolTip(tooltip)
            self.Bind(wx.EVT_TOGGLEBUTTON if toggle else wx.EVT_BUTTON, handler, b)
            if disable:
                self.buttons_to_disable.append(b)
            row.Add(b, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, border=3)
            return b

        self.button_startstop = button('START / STOP', "Start / Stop (F5)", self.on_button_startstop, top_row, 42,
                                       toggle=True, disable=False)
        self.button_tap = button('TAP', "Tap tempo (Space)", self.on_button_tap, top_row, 42)
        self.button_resync = button('RESYNC', "Resync bar (R)", self.on_button_resync, top_row, 42)
        self.button_minus_one = button('\u22121', "-1 BPM (Down)", self.on_button_minus_one, bottom_row, 32)
        self.button_plus_one = button('+1', "+1 BPM (Up)", self.on_button_plus_one, bottom_row, 32)
        self.button_half = button('\u00f72', "Halve BPM (Left)", self.on_button_half, bottom_row, 32)
        self.button_double = button('\u00d72', "Double BPM (Right)", self.on_button_double, bottom_row, 32)

        self.button_sizer.Add(top_row, 0, wx.EXPAND)
        self.button_sizer.Add(bottom_row, 0, wx.EXPAND | wx.TOP, border=6)
        sizer.Add(self.button_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, border=margin - 3)

        for b in self.buttons_to_disable:
            b.Disable()

        # some window properties
        self.Bind(wx.EVT_CLOSE, self.close)
        panel.SetSizer(sizer)

    def update_layout(self):
        """Shows the parts of the current view and fits the window height to them.\n
        Full view: settings (unless folded away) and buttons. Compact: beat lights and tempo only.
        Only the width can be changed by hand, extra height would just be empty space.
        """
        settings = self.settings_open and not self.compact
        self.main_sizer.Show(self.settings_header, not self.compact)
        self.main_sizer.Show(self.ip_stuff_sizer, settings, recursive=True)
        self.main_sizer.Show(self.audiosizer, settings, recursive=True)
        self.main_sizer.Show(self.button_sizer, not self.compact, recursive=True)
        self.settings_header.SetLabel(("\u25be  " if self.settings_open else "\u25b8  ") + "SETTINGS")

        self.SetMinSize(wx.DefaultSize)
        self.SetMaxSize(wx.DefaultSize)
        self.panel.Layout()
        min_size = self.ClientToWindowSize(self.main_sizer.GetMinSize())
        if not self.compact:
            min_size.width = max(min_size.width, 480)  # room for the OSC addresses
        width = self.config.getint('UI', 'compact_width' if self.compact else 'full_width')
        self.SetMinSize(min_size)
        self.SetMaxSize(wx.Size(-1, min_size.height))
        self.SetSize(wx.Size(max(min_size.width, width), min_size.height))
        self.panel.Layout()

    def remember_size(self):
        """Keeps the width of the current view for the next time it is shown"""
        self.config['UI']['compact_width' if self.compact else 'full_width'] = str(self.GetSize().width)

    def set_settings_open(self, settings_open: bool):
        self.settings_open = settings_open
        self.config['UI']['settings_open'] = 'yes' if settings_open else 'no'
        self.menu_settings.Check(settings_open)
        self.remember_size()
        self.update_layout()

    def add_address_row(self, parent, address_sizer, label, config_key):
        """Adds a label and an OSC address textbox to the address grid

        Args:
            parent (wx.Window): parent window of the new controls
            address_sizer (wx.FlexGridSizer): grid to add label and textbox to
            label (str): label in front of the textbox
            config_key (str): key in the OSC section of the config the textbox edits
        Returns:
            wx.TextCtrl: the created textbox
        """
        label_text = wx.StaticText(parent, label=label)
        label_text.SetForegroundColour(self.theme['heading'])
        address_sizer.Add(label_text, 0, wx.ALIGN_CENTER_VERTICAL)

        text = wx.TextCtrl(parent, value=self.config['OSC'][config_key])
        address_sizer.Add(text, 1, wx.EXPAND)

        def on_text(event):
            # store valid addresses right away, so a change takes effect with the next sent message
            address = text.GetValue().strip()
            if address.startswith('/') and ' ' not in address:
                self.config['OSC'][config_key] = address
                text.SetForegroundColour(wx.NullColour)
            else:
                text.SetForegroundColour((220, 0, 0))
            text.Refresh()

        text.Bind(wx.EVT_TEXT, on_text)
        return text

    def InitMenu(self):
        """Menu bar with view options and help"""
        menubar = wx.MenuBar()

        view_menu = wx.Menu()
        self.menu_settings = view_menu.AppendCheckItem(wx.ID_ANY, "&Settings\tCtrl+E")
        self.menu_settings.Check(self.settings_open)
        self.menu_on_top = view_menu.AppendCheckItem(wx.ID_ANY, "Always on &top\tCtrl+T")
        self.menu_compact = view_menu.AppendCheckItem(wx.ID_ANY, "&Compact mode\tCtrl+M")
        view_menu.AppendSeparator()
        self.menu_global_resync = view_menu.AppendCheckItem(
            wx.ID_ANY, f"&Resync from any program ({self.config['UI']['global_resync_key']})")
        menubar.Append(view_menu, "&View")

        help_menu = wx.Menu()
        menu_shortcuts = help_menu.Append(wx.ID_ANY, "&Keyboard shortcuts\tF1")
        menu_osc = help_menu.Append(wx.ID_ANY, "&OSC messages")
        menubar.Append(help_menu, "&Help")

        self.SetMenuBar(menubar)

        self.Bind(wx.EVT_MENU, lambda event: self.set_settings_open(self.menu_settings.IsChecked()), self.menu_settings)
        self.Bind(wx.EVT_MENU, lambda event: self.set_always_on_top(self.menu_on_top.IsChecked()), self.menu_on_top)
        self.Bind(wx.EVT_MENU, lambda event: self.set_compact(self.menu_compact.IsChecked()), self.menu_compact)
        self.Bind(wx.EVT_MENU, self.on_menu_global_resync, self.menu_global_resync)
        self.Bind(wx.EVT_MENU, self.on_menu_shortcuts, menu_shortcuts)
        self.Bind(wx.EVT_MENU, self.on_menu_osc, menu_osc)

    def on_char_hook(self, event):
        """Keyboard shortcuts, see SHORTCUTS"""
        focus = wx.Window.FindFocus()
        key = event.GetKeyCode()
        typing = isinstance(focus, (wx.TextCtrl, wx.SpinCtrl, wx.ComboBox, wx.Choice)) or \
            (focus is not None and isinstance(focus.GetParent(), wx.SpinCtrl))
        if (typing and key != wx.WXK_F5) or event.HasAnyModifiers() or event.IsAutoRepeat():
            event.Skip()
            return

        actions = {
            wx.WXK_SPACE: (self.button_tap, self.on_button_tap),
            ord('R'): (self.button_resync, self.on_button_resync),
            ord('S'): (self.button_sync, self.on_button_sync),
            ord('H'): (self.button_halftime, self.on_button_halftime),
            wx.WXK_UP: (self.button_plus_one, self.on_button_plus_one),
            wx.WXK_NUMPAD_ADD: (self.button_plus_one, self.on_button_plus_one),
            wx.WXK_DOWN: (self.button_minus_one, self.on_button_minus_one),
            wx.WXK_NUMPAD_SUBTRACT: (self.button_minus_one, self.on_button_minus_one),
            wx.WXK_RIGHT: (self.button_double, self.on_button_double),
            wx.WXK_NUMPAD_MULTIPLY: (self.button_double, self.on_button_double),
            wx.WXK_LEFT: (self.button_half, self.on_button_half),
            wx.WXK_NUMPAD_DIVIDE: (self.button_half, self.on_button_half),
            wx.WXK_F5: (self.button_startstop, self.on_button_startstop),
        }
        if key not in actions:
            event.Skip()
            return

        button, handler = actions[key]
        if button.IsEnabled():
            if button.toggle:
                button.SetValue(not button.GetValue())
            handler(None)

    def on_menu_shortcuts(self, event):
        text = SHORTCUTS
        if self.menu_global_resync.IsChecked():
            text += f"\n\nWorks while another program has the focus:\n{self.config['UI']['global_resync_key']}\tResync bar"
        wx.MessageBox(text, "Keyboard shortcuts", wx.OK | wx.ICON_INFORMATION, self)

    def on_menu_global_resync(self, event):
        enable = self.menu_global_resync.IsChecked()
        self.config['UI']['global_resync'] = 'yes' if enable else 'no'
        self.set_global_resync(enable)

    def set_global_resync(self, enable: bool):
        """Registers or removes the system wide resync hotkey (Windows only)"""
        if system().lower() != 'windows':
            self.menu_global_resync.Check(False)
            self.menu_global_resync.Enable(False)
            return
        user32 = ctypes.windll.user32
        user32.UnregisterHotKey(self.GetHandle(), HOTKEY_RESYNC)
        registered = False
        if enable:
            key_name = self.config['UI']['global_resync_key']
            hotkey = parse_hotkey(key_name)
            if hotkey is None:
                problem = f'"{key_name}" is not a key {APP_NAME} knows.'
            else:
                registered = bool(user32.RegisterHotKey(self.GetHandle(), HOTKEY_RESYNC, *hotkey))
                problem = f"{key_name} is already used by another program."
            if not registered:
                wx.MessageBox(f"Resync from any program is off: {problem}\n\n"
                              f"Pick another key with global_resync_key in the [UI] section of\n{self.CONF_PATH}\n"
                              f"(e.g. F9, Pause, Scroll_Lock, Ctrl+Shift+R) and restart {APP_NAME}.",
                              "Resync hotkey", wx.OK | wx.ICON_WARNING, self)
        self.menu_global_resync.Check(registered)

    def on_global_resync(self, event):
        """The system wide hotkey, does what the resync button does"""
        if self.button_resync.IsEnabled():
            self.on_button_resync(None)

    def on_menu_osc(self, event):
        """Lists the OSC messages with the current addresses and settings"""
        osc = self.config['OSC']
        prefix = osc['BEAT_PREFIX']
        if osc['BPM_SEND_FORMAT'] == 'bpm':
            tempo_format = "the BPM as a number, e.g. 125.4"
        else:
            tempo_format = "Resolume format: 20-500 BPM mapped to 0-1"
        beat_state = "on" if self.config.getboolean('OSC', 'SEND_BEAT') else "OFF (tick \"Send\" in the Beat row)"
        offset = self.spin_latency.GetValue()
        text = (
            f"Sent to {clean_host(self.text_ip.GetValue())} port {self.text_port.GetValue()}\n\n"
            f"TEMPO\n"
            f"{osc['BPM_ADRESS']}\n"
            f"    {tempo_format}\n"
            f"    sent when it changes and once a bar\n\n"
            f"RESYNC\n"
            f"{osc['RESYNC_BAR_ADRESS']}\n"
            f"    1, when Resync is pressed\n\n"
            f"BEAT AND PHASE (currently {beat_state})\n"
            f"{prefix}/beat\n"
            f"    1-4, the beat of the bar, on every beat\n"
            f"{prefix}/snare\n"
            f"    1 on beats 2 and 4 (the backbeat), 0 on beats 1 and 3\n"
            f"{prefix}/phase\n"
            f"    0-1 through the current beat, 60 times a second\n"
            f"{prefix}/bar_phase\n"
            f"    0-1 through the current bar, 60 times a second\n\n"
            f"Addresses, tempo format and beat prefix are set in the OSC Output box.\n\n"
            f"In Notch: receive the values with an OSC Modifier. Driving animations from "
            f"phase or bar_phase keeps them locked to the music; a BPM Modifier runs its own "
            f"clock, which slowly drifts away even with the right tempo.\n\n"
            f"Latency (in the OSC Output settings, now {offset} ms) sends beats and phase that many "
            f"milliseconds early, to make up for render and projector delay."
        )
        wx.MessageBox(text, "OSC messages", wx.OK | wx.ICON_INFORMATION, self)

    def set_always_on_top(self, on_top: bool):
        style = self.GetWindowStyleFlag()
        self.SetWindowStyleFlag(style | wx.STAY_ON_TOP if on_top else style & ~wx.STAY_ON_TOP)
        self.menu_on_top.Check(on_top)
        self.config['UI']['always_on_top'] = 'yes' if on_top else 'no'

    def set_compact(self, compact: bool):
        """Compact mode only shows beat leds and the bpm displays, controlled by keyboard shortcuts"""
        self.menu_compact.Check(compact)
        self.config['UI']['compact'] = 'yes' if compact else 'no'
        if compact == self.compact:
            return
        self.remember_size()
        self.compact = compact
        self.update_layout()

    def restore_position(self):
        """Moves the window to where it was last time, if that is still on a screen"""
        if self.config.has_option('UI', 'pos_x') and self.config.has_option('UI', 'pos_y'):
            pos = wx.Point(self.config.getint('UI', 'pos_x'), self.config.getint('UI', 'pos_y'))
            # check a point inside the title bar, the corner itself may be just off screen
            if wx.Display.GetFromPoint(pos + wx.Point(50, 10)) != wx.NOT_FOUND:
                self.SetPosition(pos)
                return
        self.Centre()  # centre window on screen

    def on_button_plus_one(self, event):
        self.on_button_halftime(None, reset=True)
        if self.sync:
            self.switch_sync(False)
        if self.send_bpm < 499:
            self.set_manual_bpm(round(self.send_bpm) + 1)

    def on_button_minus_one(self, event):
        self.on_button_halftime(None, reset=True)
        if self.sync:
            self.switch_sync(False)
        if self.send_bpm > 21:
            self.set_manual_bpm(round(self.send_bpm) - 1)

    def on_button_double(self, event):
        self.on_button_halftime(None, reset=True)
        if self.sync:
            self.switch_sync(False)
        if self.send_bpm*2 <= 500:
            self.set_manual_bpm(self.send_bpm * 2)

    def on_button_half(self, event):
        self.on_button_halftime(None, reset=True)
        if self.sync:
            self.switch_sync(False)
        if self.send_bpm/2 >= 20:
            self.set_manual_bpm(self.send_bpm / 2)

    def on_button_halftime(self, event, reset=False):
        if reset:
            self.button_halftime.SetValue(False)

        self.beat_divider = 2 if self.button_halftime.GetValue() else 1


    def on_button_reload(self, event):
        """Lists the audio inputs, then a loopback source for every output (a copy of what the PC plays
        there), and selects the one from last session"""

        self.audio_selection.Clear()
        self.audio_device_indices = list()  # pyaudio device index per combobox entry
        self.audio_device_keys = list()  # name per combobox entry, saved in the config

        def add(label, key, index):
            self.audio_selection.Append(label)
            self.audio_device_keys.append(key)
            self.audio_device_indices.append(index)

        try:
            default_index = self.audio.get_default_input_device_info().get('index')
        except IOError:
            default_index = None  # no input device at all

        mme = self.audio.get_host_api_info_by_type(pyaudio.paMME)
        default_selection = 0
        for i in range(mme.get('deviceCount')):
            device = self.audio.get_device_info_by_host_api_device_index(mme['index'], i)
            if device.get('maxInputChannels') > 0:
                name = device.get('name')
                if device.get('index') == default_index:
                    default_selection = len(self.audio_device_indices)
                    add("DEFAULT: " + name, name, device['index'])
                else:
                    add(name, name, device['index'])

        try:
            wasapi = self.audio.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_output = self.audio.get_device_info_by_index(wasapi['defaultOutputDevice'])['name']
            loopbacks = list(self.audio.get_loopback_device_info_generator())
        except (IOError, OSError, LookupError):
            default_output, loopbacks = None, []  # no WASAPI (very old Windows)
        for device in loopbacks:
            name = device['name'].removesuffix(" [Loopback]")
            label = f"Loopback: {name}" + (" (default output)" if name == default_output else "")
            add(label, f"Loopback: {name}", device['index'])

        if self.audio_device_indices:
            saved = self.config['AUDIO']['device_name']
            selection = self.audio_device_keys.index(saved) if saved in self.audio_device_keys else default_selection
            self.audio_selection.SetSelection(selection)
        self.audio_selection.SetToolTip("Audio inputs, and \"Loopback\" sources: what this PC plays on an output,\n"
                                        "e.g. the music player or Notch, without any cables or extra software")

    def on_button_tap(self, event):
        """count time between taps and calculate bpm\n
        Args:
            event (ex.EVENT): unused
        """

        now = perf_counter()

        # calculate only after 2+ tabs
        if len(self.last_tap) > 1:

            # add time and timedelta to list
            self.last_tap.append((now, now - self.last_tap[-1][0]))

            # Clear if previous taps are too old
            if now - self.last_tap[-2][0] > 1:
                self.last_tap = [self.last_tap[-1]]

            else:
                # disable sync if it was enabled
                if self.sync:
                    self.switch_sync(False)

                # Calculate the BPM and put a beat on this tap
                self.set_manual_bpm(round(60 / (sum([x[1] for x in self.last_tap[1:]]) / len(self.last_tap[1:]))))
                self.clock.align_beat(now)
                self.clock_wake.set()

        elif len(self.last_tap) == 1:
            if now - self.last_tap[0][0] > 1:
                # the first tap is too old to belong to this run, start over with this one
                self.last_tap = [(now, None)]
            else:
                self.last_tap.append((now, now - self.last_tap[-1][0]))

        else:
            # if no taps yet, add first tap
            self.last_tap.append((now, None))

    def on_button_sync(self, event):
        self.switch_sync(self.button_sync.GetValue())
        self.button_resync.SetBackgroundColour(wx.NullColour)

    def on_button_resync(self, event):
        """Restarts the bar: beat 1 starts now and gets its full length.\n
        In sync mode beat 1 snaps to the nearest detected beat, so the bar starts on the music right away
        """
        start = perf_counter()
        if self.sync and self.beatfinder:
            estimator = self.beatfinder.estimator
            if estimator.bpm is not None and estimator.beats and start - estimator.beats[-1][0] < 2.0:
                # grid of the music's beats through the last detection
                period = 60.0 / estimator.bpm
                last = estimator.beats[-1][0]
                start = last + round((start - last) / period) * period
        self.clock.resync(start)
        self.clock_wake.set()

        if self.beatfinder:
            self.beatfinder.resync_bar()

    def on_send_format(self, event):
        self.config['OSC']['BPM_SEND_FORMAT'] = SEND_FORMATS[self.send_format.GetSelection()][0]

    def on_button_osc_defaults(self, event):
        self.text_bpm_address.SetValue(DEFAULT_CONFIG['OSC']['BPM_ADRESS'])
        self.text_resync_address.SetValue(DEFAULT_CONFIG['OSC']['RESYNC_BAR_ADRESS'])
        self.text_beat_prefix.SetValue(DEFAULT_CONFIG['OSC']['BEAT_PREFIX'])
        self.send_format.SetSelection(0)
        self.on_send_format(None)

    def on_ip_text(self, event):
        valid = is_valid_host(clean_host(self.text_ip.GetValue()))
        self.text_ip.SetForegroundColour(wx.NullColour if valid else wx.Colour(235, 80, 70))
        self.text_ip.Refresh()

    def on_ip_done(self, event):
        """Tidies the address when leaving the box (127.000.000.001 -> 127.0.0.1)"""
        cleaned = clean_host(self.text_ip.GetValue())
        if cleaned != self.text_ip.GetValue():
            self.text_ip.ChangeValue(cleaned)
        event.Skip()

    def on_latency(self, event):
        """Takes effect right away, the clock thread reads it on every step"""
        ms = self.spin_latency.GetValue()
        self.output_offset = ms / 1000
        self.config['TEMPO']['output_offset_ms'] = str(ms)

    def on_check_send_beat(self, event):
        self.config['OSC']['SEND_BEAT'] = 'yes' if self.check_send_beat.GetValue() else 'no'

    def on_tempo_range(self, event):
        min_bpm = TEMPO_RANGES[self.tempo_range.GetSelection()]
        self.config['TEMPO']['min_bpm'] = str(min_bpm)
        if self.beatfinder:
            self.beatfinder.tracker.min_bpm = min_bpm
            self.beatfinder.estimator.min_bpm = min_bpm

    def on_detected_beat(self, beat_time, bpm, confidence):
        """Called by the beat detector (audio thread) for every beat it is confident about\n
        Args:
            beat_time (float): perf_counter time the beat was audible at the input
            bpm (float): tempo estimate, folded into the tempo range
            confidence (float): 0 (just good enough) to 1 (clearly marked beat), sets how hard the clock is steered
        """
        self.last_detected = perf_counter()
        self.update_bpm_display(bpm, send_to="live")
        if self.sync:
            self.clock.set_bpm(bpm / self.beat_divider, beat_time)
            self.clock.nudge(beat_time, gain=0.5 * confidence)
            self.clock_wake.set()

    def set_manual_bpm(self, bpm):
        """Sets the tempo by hand (tap, +1, x2, ...)"""
        self.send_bpm = bpm
        self.clock.set_bpm(bpm, perf_counter())
        self.clock_wake.set()
        self.update_bpm_display(bpm, send_to="send")

    def send_tempo(self, bpm):
        """Sends bpm to the tempo OSC address in the selected format"""
        client = self.osc_client
        if client is None:
            return
        if self.config['OSC']['BPM_SEND_FORMAT'] == 'bpm':
            client.send_osc(self.config['OSC']['BPM_ADRESS'], float(bpm))
        else:
            client.send_osc(self.config['OSC']['BPM_ADRESS'], bpm, map_to_resolume=True)

    def on_button_startstop(self, event):
        """Starts and stops the audio and OSC stream

        Args:
            event (wx.EVENT): unused
        """
        if self.button_startstop.GetValue():
            print("Starting")

            selection = self.audio_selection.GetSelection()
            if selection == wx.NOT_FOUND:
                wx.MessageBox("No audio input device found.", APP_NAME, wx.OK | wx.ICON_ERROR, self)
                self.button_startstop.SetValue(False)
                return

            # save input to config
            self.config['AUDIO']['device_name'] = self.audio_device_keys[selection]
            self.config['OSC']['IP'] = clean_host(self.text_ip.GetValue())
            self.config['OSC']['PORT'] = str(self.text_port.GetValue())

            # create worker instances
            try:
                self.osc_client = osc_client.OSCclient(self.config['OSC']['IP'], int(self.config['OSC']['PORT']))
                self.beatfinder = beatfinder.BeatDetector(self.osc_client, self.audio_device_indices[selection], parent=self,
                                                          min_bpm=TEMPO_RANGES[self.tempo_range.GetSelection()])
            except Exception as e:
                self.osc_client = None
                self.beatfinder = None
                wx.MessageBox(f"Could not start:\n{e}", APP_NAME, wx.OK | wx.ICON_ERROR, self)
                self.button_startstop.SetValue(False)
                return

            # gui changes
            self.button_startstop.SetBackgroundColour(self.theme['running'])
            self.audio_selection.Disable()
            for b in self.buttons_to_disable:
                b.Enable()

            # fresh clock, its tempo comes from the first detected beats (or the manual tempo)
            self.clock = BeatClock()
            self.last_sent_bpm = None
            self.last_detected = None
            if not self.sync:
                self.clock.set_bpm(self.send_bpm, perf_counter())

            self.running = True
            self.clock_wake.clear()
            self.clock_thread = Thread(target=self.clock_thread_loop, daemon=True)
            self.clock_thread.start()
            self.button_tap.SetFocus()

        else:
            print("Stopping")

            self.running = False

            # stop the clock thread before the OSC client goes
            self.clock_wake.set()
            if self.clock_thread is not None and self.clock_thread.is_alive():
                self.clock_thread.join(timeout=1)
            self.switch_sync(True)

            # delete worker instances
            del self.beatfinder
            self.beatfinder = None
            del self.osc_client
            self.osc_client = None

            # gui changes
            self.audio_selection.Enable()
            for b in self.buttons_to_disable:
                b.Disable()
            self.button_startstop.SetBackgroundColour(wx.NullColour)
            self.level_meter.Reset()
            self.update_bpm_display("---", send_to="both")
            self.button_startstop.SetFocus()

    def switch_sync(self, state: bool):
        """Changes state of sync flag and button\n
        Starts thread to emit "send bpm"

        Args:
            state (bool): State to switch to
        """
        #stop sync
        if not state and self.sync:
            # keep the clock running at the current tempo, from now on it is set by hand
            self.sync = False
            if self.clock.bpm is not None:
                self.send_bpm = self.clock.bpm
            elif self.running:
                self.clock.set_bpm(self.send_bpm, perf_counter())
                self.clock_wake.set()
            self.button_sync.SetForegroundColour(self.theme['status_manual'])
            self.button_sync.SetLabel('MANUAL')

            self.button_sync.SetValue(False)

            #self.button_halftime.SetValue(False)
            self.button_halftime.Disable()
            #self.on_button_halftime(None)

        #start sync
        elif state and not self.sync:
            self.sync = True
            self.button_sync.SetForegroundColour(self.theme['status_sync'])
            self.button_sync.SetLabel('SYNC')
            self.button_sync.SetValue(True)

            self.button_halftime.SetValue(True if self.beat_divider == 2 else False)
            self.button_halftime.Enable()
        else:
            print("Sync state already set to {}".format(state))

    def update_bpm_display(self, bpm, send_to: str = "both"):
        """Shows a tempo (or a text like "---") on the live and / or send display, callable from any thread

        Args:
            bpm (float | str): bpm value to show, with one decimal
            send_to (str): wich display to update, can be "both", "live" or "send". Defaults to "both."
        """
        text = bpm if isinstance(bpm, str) else f"{bpm:.1f}"

        def update():
            if not self.live_number:  # window already destroyed
                return
            if send_to in ("both", "live"):
                self.live_number.SetLabel(text)
            if send_to in ("both", "send"):
                self.send_number.SetLabel(text)

        wx.CallAfter(update)

    def set_bar_position(self, beat: int):
        """Lights the led of the given beat (0-3) and counts on from there.\n
        Safe to call from the audio and manual tempo threads, the leds are painted by the gui thread
        """
        self.led_counter = beat

        def paint():
            if self.beat_lights:  # not when the window is already destroyed
                self.beat_lights.SetBeat(beat)

        wx.CallAfter(paint)

    def clock_thread_loop(self):
        """Runs while started: beat leds, tempo, beat and phase messages all follow the beat clock.\n
        Wakes on every beat and for every phase message, or early when the clock was changed
        """
        last_beat = None  # (epoch, beat number) of the last beat that was output
        next_phase_send = 0.0
        while self.running:
            now = perf_counter()
            offset = self.output_offset  # can change while running
            position = self.clock.position(now + offset)
            if position is None:
                # no tempo yet
                self.clock_wake.wait(0.05)
                self.clock_wake.clear()
                continue

            beat = (self.clock.epoch, floor(position))
            if last_beat is None or beat[0] != last_beat[0]:
                # new bar from a resync: when it starts slightly in the future, wait for it
                last_beat = beat
                if beat[1] >= 0:
                    self.on_clock_beat(beat[1])
            elif beat[1] > last_beat[1]:
                # only forward: a phase correction pulling the clock back must not repeat a beat
                last_beat = beat
                self.on_clock_beat(beat[1])

            if now >= next_phase_send:
                self.send_phase(position)
                next_phase_send = now + PHASE_INTERVAL

            next_beat_time = self.clock.time_of(floor(position) + 1) - offset
            wait = min(next_beat_time, next_phase_send) - perf_counter()
            if wait > 0:
                self.clock_wake.wait(wait)
            self.clock_wake.clear()

    def on_clock_beat(self, beat_number):
        """Output for every beat of the clock (clock thread)"""
        beat_in_bar = beat_number % 4
        self.set_bar_position(beat_in_bar)
        # pulse under the tempo that drives the output: live when synced, send in manual
        pulsing = self.live_number if self.sync else self.send_number
        wx.CallAfter(lambda: pulsing and pulsing.Pulse())  # not when the window is already destroyed

        client = self.osc_client
        if client is not None and self.config.getboolean('OSC', 'SEND_BEAT'):
            prefix = self.config['OSC']['BEAT_PREFIX']
            client.send_osc(prefix + '/beat', float(beat_in_bar + 1))
            # backbeat: 1 on beats 2 and 4 (where the snare usually is), 0 on 1 and 3
            client.send_osc(prefix + '/snare', 1.0 if beat_in_bar in (1, 3) else 0.0)

        bpm = self.clock.bpm
        # send the tempo when it changed, and once a bar so a receiver that just started catches up
        if round(bpm, 2) != self.last_sent_bpm or beat_in_bar == 0:
            self.last_sent_bpm = round(bpm, 2)
            self.send_tempo(self.last_sent_bpm)
        self.update_bpm_display(bpm, send_to="send")

    def send_phase(self, position):
        """Continuous position within beat and bar, so the receiver can follow without its own clock"""
        client = self.osc_client
        if client is not None and self.config.getboolean('OSC', 'SEND_BEAT'):
            prefix = self.config['OSC']['BEAT_PREFIX']
            client.send_osc(prefix + '/phase', float(position % 1))
            client.send_osc(prefix + '/bar_phase', float((position % 4) / 4))

    def save_config(self):
        """Writes window state and settings to lastsession.ini"""
        self.uv_timer.Stop()  # only called before closing, the timer must not fire into a destroyed window
        if system().lower() == 'windows':
            ctypes.windll.user32.UnregisterHotKey(self.GetHandle(), HOTKEY_RESYNC)
        self.remember_size()
        pos = self.GetPosition()
        self.config['UI']['pos_x'] = str(pos.x)
        self.config['UI']['pos_y'] = str(pos.y)

        # create directory if it does not exist
        self.CONF_PATH.parent.mkdir(parents=True, exist_ok=True)

        # save config
        with open(self.CONF_PATH, 'w') as configfile:
            self.config.write(configfile)

    def close(self, event):  # save settings to ini and close down
        """Ask User if event can be vetoed (No force close event).
        """
        if self.running:
            if wx.MessageBox(f"Do you really want to close {APP_NAME}?",
                             "Please confirm",
                             wx.ICON_QUESTION | wx.YES_NO) != wx.YES:
                try:
                    event.Veto()
                except:
                    pass
                return

            # stop audio and OSC before the window is gone
            self.button_startstop.SetValue(False)
            self.on_button_startstop(None)

        self.save_config()

        # close everything
        self.Destroy()

    def __del__(self):
        """stop all threads and close down
        """
        self.running = False
        self.clock_wake.set()


def main():
    if system().lower() == 'windows':
        # beat messages are timed with thread waits, Windows' default 15.6 ms timer is too coarse for that
        ctypes.windll.winmm.timeBeginPeriod(1)
    app = wx.App(0)
    config = read_config()
    # must happen before any window is created
    app.MSWEnableDarkMode(wx.App.DarkMode_Always)
    frame = Main_Frame(None, config)
    # title bar and taskbar icon, bundled into the exe (PyInstaller unpacks it to sys._MEIPASS)
    icon = Path(getattr(sys, '_MEIPASS', Path(__file__).parent), "BPM_to_OSC_XL.ico")
    if icon.exists():
        frame.SetIcons(wx.IconBundle(str(icon)))
    app.SetTopWindow(frame)
    frame.Show()
    app.MainLoop()


# debug and testing
if __name__ == '__main__':
    #import wx.lib.inspection
    # wx.lib.inspection.InspectionTool().Show()
    main()
