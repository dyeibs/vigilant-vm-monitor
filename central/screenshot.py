"""
Capture the VIGILANT window as an image (for the Discord notification attachment).

Uses the Win32 PrintWindow API, so it copies only the VIGILANT window itself — even when
other windows overlap it — and never anything else on the screen. Returns None when a
capture is not possible (minimised window, locked screen, non-Windows system).
"""
from __future__ import annotations

import ctypes
import io
import logging
import sys

log = logging.getLogger("vigilant.screenshot")

PW_RENDERFULLCONTENT = 0x2          # also captures DirectComposition / hardware-rendered content


def capture_window(hwnd: int):
    """PIL image of the window's client area, or None."""
    if sys.platform != "win32":
        return None
    try:
        from ctypes import wintypes

        from PIL import Image, ImageStat
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
            return None
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 50 or h < 50:
            return None
        win_dc = user32.GetWindowDC(hwnd)
        mem_dc = gdi32.CreateCompatibleDC(win_dc)
        bmp = gdi32.CreateCompatibleBitmap(win_dc, w, h)
        old = gdi32.SelectObject(mem_dc, bmp)
        try:
            if not user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
                return None

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                            ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                            ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                            ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                            ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

            info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down
            buf = ctypes.create_string_buffer(w * h * 4)
            if not gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(info), 0):
                return None
        finally:
            gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(hwnd, win_dc)
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        # crop the title bar / borders: keep the client area only
        client = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(client))
        origin = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        left, top = origin.x - rect.left, origin.y - rect.top
        img = img.crop((left, top, left + client.right, top + client.bottom))
        if max(ImageStat.Stat(img.convert("L")).extrema[0]) < 8:     # all black: locked screen / not drawn
            return None
        return img
    except Exception as exc:
        log.info("Window capture failed: %s", type(exc).__name__)
        return None


def to_jpeg(img, max_width: int = 1400, quality: int = 85) -> bytes:
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
