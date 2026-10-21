# Part of BPM to OSC XL.
# Copyright (C) 2026 Mike Wilson
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version. See LICENSE.

"""Flat, self drawn controls for a modern look: rounded buttons, a slim level meter and beat lights.

The native Windows buttons and the segmented peak meter look dated next to Windows 11,
these draw themselves with anti-aliased rounded shapes in the colours of the current theme.
"""
import wx


def font(size, weight=wx.FONTWEIGHT_NORMAL, display=False):
    """Segoe UI Variable (Windows 11) when installed, Segoe UI otherwise.
    The Display cut is meant for big numbers, the Text cut for everything else."""
    face = "Segoe UI Variable Display" if display else "Segoe UI Variable Text"
    if face not in _faces():
        face = "Segoe UI"
    return wx.Font(wx.FontInfo(size).FaceName(face).Weight(weight))


_face_cache = None


def _faces():
    global _face_cache
    if _face_cache is None:
        _face_cache = set(wx.FontEnumerator.GetFacenames())
    return _face_cache


def blend(a, b, amount):
    """Colour between a and b (amount 0: a, 1: b)"""
    a, b = wx.Colour(a), wx.Colour(b)
    return wx.Colour(*(round(x + (y - x) * amount) for x, y in zip(a.Get(False), b.Get(False))))


class FlatButton(wx.Control):
    """Rounded flat button, optionally a toggle. Sends EVT_BUTTON, or EVT_TOGGLEBUTTON when it is a toggle.

    Colours come from a theme dict: 'button', 'button_text'. A toggle that is on, or a button given a
    background colour (SetBackgroundColour), is filled with that colour instead.
    A subtle button has no fill until the mouse is over it, for header like controls.
    """

    def __init__(self, parent, label, theme, toggle=False, size=wx.DefaultSize, accent=None, font_=None,
                 subtle=False, align_left=False):
        super().__init__(parent, size=size, style=wx.BORDER_NONE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.label = label
        self.theme = theme
        self.toggle = toggle
        self.accent = wx.Colour(accent) if accent else None  # fill when a toggle is on
        self.fill = None  # fill set with SetBackgroundColour
        self.value = False
        self.hover = False
        self.pressed = False
        self.text_colour = None
        self.subtle = subtle
        self.align_left = align_left
        if font_ is not None:
            self.SetFont(font_)

        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self.on_down)
        self.Bind(wx.EVT_LEFT_DCLICK, self.on_down)  # fast clicks (tap tempo) arrive as double clicks
        self.Bind(wx.EVT_LEFT_UP, self.on_up)
        self.Bind(wx.EVT_ENTER_WINDOW, lambda e: self._set_hover(True))
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: self._set_hover(False))
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, lambda e: self._release())

    # ---- wx.Button / wx.ToggleButton compatible interface

    def GetValue(self):
        return self.value

    def SetValue(self, value):
        self.value = bool(value)
        self.Refresh()

    def SetLabel(self, label):
        self.label = label
        self.Refresh()

    def GetLabel(self):
        return self.label

    def SetForegroundColour(self, colour):
        self.text_colour = wx.Colour(colour)
        self.Refresh()
        return True

    def SetBackgroundColour(self, colour):
        colour = wx.Colour(colour)
        self.fill = colour if colour.IsOk() else None
        self.Refresh()
        return True

    def Enable(self, enable=True):
        changed = super().Enable(enable)
        self.Refresh()
        return changed

    def Disable(self):
        return self.Enable(False)

    def AcceptsFocus(self):
        return False  # keyboard shortcuts drive the app, a focus ring would only clutter

    def DoGetBestSize(self):
        dc = wx.ClientDC(self)
        dc.SetFont(self.GetFont())
        w, h = dc.GetTextExtent(self.label)
        return wx.Size(w + 24, h + 14)

    # ---- mouse

    def _set_hover(self, hover):
        self.hover = hover
        self.Refresh()

    def _release(self):
        self.pressed = False
        self.Refresh()

    def on_down(self, event):
        if not self.IsEnabled():
            return
        self.pressed = True
        if not self.HasCapture():
            self.CaptureMouse()
        self.Refresh()

    def on_up(self, event):
        if self.HasCapture():
            self.ReleaseMouse()
        was_pressed = self.pressed
        self._release()
        if was_pressed and self.IsEnabled() and self.GetClientRect().Contains(event.GetPosition()):
            if self.toggle:
                self.value = not self.value
            kind = wx.wxEVT_TOGGLEBUTTON if self.toggle else wx.wxEVT_BUTTON
            evt = wx.CommandEvent(kind, self.GetId())
            evt.SetEventObject(self)
            evt.SetInt(int(self.value))
            self.GetEventHandler().ProcessEvent(evt)

    # ---- drawing

    def on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()

        if self.fill is not None:
            fill = self.fill
        elif self.toggle and self.value and self.accent is not None:
            fill = self.accent
        else:
            fill = wx.Colour(self.theme['button'])
        text = self.text_colour or wx.Colour(self.theme['button_text'])
        if self.IsEnabled():
            if self.pressed:
                fill = blend(fill, self.theme['button_text'], 0.18)
            elif self.hover:
                fill = blend(fill, self.theme['button_text'], 0.08)
        else:
            text = blend(text, self.GetParent().GetBackgroundColour(), 0.6)
            fill = blend(fill, self.GetParent().GetBackgroundColour(), 0.4)

        if self.subtle and not (self.hover or self.pressed):
            fill = None
        if fill is not None:
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(fill))
            gc.DrawRoundedRectangle(0, 0, w, h, 6)
        del gc  # finish the shape before drawing text on the same dc

        # text through the dc: the graphics context can't render the weights of variable fonts (Segoe UI Variable)
        dc.SetFont(self.GetFont())
        dc.SetTextForeground(text)
        tw, th = dc.GetTextExtent(self.label)
        dc.DrawText(self.label, 6 if self.align_left else (w - tw) // 2, (h - th) // 2)


class LevelMeter(wx.Control):
    """Slim rounded input level bar (0-100, the caller maps dB to it), green, then yellow, then red towards the top"""

    def __init__(self, parent, theme, height=6):
        super().__init__(parent, size=(-1, height), style=wx.BORDER_NONE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.theme = theme
        self.level = 0.0  # 0-100
        self.Bind(wx.EVT_PAINT, self.on_paint)

    def AcceptsFocus(self):
        return False

    def Reset(self):
        self.level = 0.0
        self.Refresh()

    def SetLevel(self, level):
        """level 0-100, falls back slowly so short peaks stay visible"""
        level = max(0.0, min(100.0, float(level)))
        self.level = level if level > self.level else self.level * 0.85 + level * 0.15
        self.Refresh()

    def on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(self.theme['track']))
        gc.DrawRoundedRectangle(0, 0, w, h, h / 2)
        fill_w = w * self.level / 100
        if fill_w >= 1:
            # with -60..0 dBFS mapped to 0-100: yellow from -18 dB, red from -6 dB
            colour = (90, 200, 120) if self.level < 70 else ((235, 200, 70) if self.level < 90 else (235, 80, 70))
            gc.SetBrush(wx.Brush(wx.Colour(colour)))
            gc.DrawRoundedRectangle(0, 0, max(fill_w, h), h, h / 2)


class BeatLights(wx.Control):
    """Four rounded beat lights, the current beat of the bar lit, beat 1 in its own colour"""

    def __init__(self, parent, theme, height=8):
        super().__init__(parent, size=(-1, height), style=wx.BORDER_NONE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.theme = theme
        self.beat = None
        self.Bind(wx.EVT_PAINT, self.on_paint)

    def AcceptsFocus(self):
        return False

    def SetBeat(self, beat):
        """0-3, None for all off"""
        self.beat = beat
        self.Refresh()

    def on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        w, h = self.GetClientSize()
        gap = 6
        pill = (w - 3 * gap) / 4
        gc.SetPen(wx.TRANSPARENT_PEN)
        for i in range(4):
            if i != self.beat:
                colour = self.theme['led_off']
            else:
                colour = self.theme['led_downbeat'] if i == 0 else self.theme['led_on']
            gc.SetBrush(wx.Brush(colour))
            gc.DrawRoundedRectangle(i * (pill + gap), 0, pill, h, h / 2)
