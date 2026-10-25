"""Window screenshot of ZWCAD via Win32 PrintWindow (adapted from dalingo81/ZWCAD-MCP, MIT).

Changes against the original: the window comes from COM ``Application.HWND`` (no title guessing), the
drawing area can be cropped, the image is downscaled natively (StretchBlt HALFTONE) and encoded as PNG
without Pillow, and only an image block is returned (FINDINGS #3: base64 in a text block overflowed the
client).
"""

from __future__ import annotations

import struct
import sys
import zlib

_dpi_done = False


def _ensure_dpi_awareness() -> None:
    global _dpi_done
    if _dpi_done:
        return
    import ctypes

    user32 = ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            _dpi_done = True
            return
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            _dpi_done = True
            return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    _dpi_done = True


def encode_png(width: int, height: int, bgrx: bytes) -> bytes:
    """Encode top-down 32-bit BGRX pixels as an RGB PNG."""
    stride = width * 4
    if len(bgrx) < stride * height:
        raise ValueError("pixel buffer too small")
    rgb = bytearray(width * height * 3)
    src = memoryview(bgrx)[: stride * height]
    rgb[0::3] = src[2::4]
    rgb[1::3] = src[1::4]
    rgb[2::3] = src[0::4]
    row = width * 3
    raw = b"".join(b"\x00" + bytes(rgb[y * row:(y + 1) * row]) for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def target_size(width: int, height: int, max_width: int) -> tuple[int, int]:
    if max_width <= 0 or width <= max_width:
        return width, height
    scale = max_width / width
    return max_width, max(1, round(height * scale))


def _drawing_view_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Screen rect of the largest visible drawing view (AfxFrameOrView*) or the MDI client."""
    import win32gui

    views: list[tuple[int, tuple[int, int, int, int]]] = []
    mdi: list[tuple[int, int, int, int]] = []

    def cb(child: int, _: object) -> bool:
        try:
            if not win32gui.IsWindowVisible(child):
                return True
            cls = win32gui.GetClassName(child)
            rect = win32gui.GetWindowRect(child)
            area = (rect[2] - rect[0]) * (rect[3] - rect[1])
            if cls.startswith("AfxFrameOrView") and area > 0:
                views.append((area, rect))
            elif cls == "MDIClient":
                mdi.append(rect)
        except Exception:
            pass
        return True

    win32gui.EnumChildWindows(hwnd, cb, None)
    if views:
        return max(views)[1]
    return mdi[0] if mdi else None


def capture_window(hwnd: int, region: str = "drawing", max_width: int = 1280) -> dict:
    """Return ``{"png": bytes, "width", "height", "source_width", "source_height", "region"}``."""
    if sys.platform != "win32":
        raise RuntimeError("Screenshots need Windows.")
    import ctypes

    import win32gui
    import win32ui

    _ensure_dpi_awareness()
    gdi32 = ctypes.windll.gdi32
    if win32gui.IsIconic(hwnd):
        raise RuntimeError("The ZWCAD window is minimized; restore it to take a screenshot.")
    win = win32gui.GetWindowRect(hwnd)
    width, height = win[2] - win[0], win[3] - win[1]
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Bad window size {width}x{height}.")

    crop = (0, 0, width, height)
    used_region = "window"
    if region == "drawing":
        view = _drawing_view_rect(hwnd)
        if view:
            left, top = max(view[0], win[0]) - win[0], max(view[1], win[1]) - win[1]
            right, bottom = min(view[2], win[2]) - win[0], min(view[3], win[3]) - win[1]
            if right - left > 50 and bottom - top > 50:
                crop, used_region = (left, top, right, bottom), "drawing"

    cw, ch = crop[2] - crop[0], crop[3] - crop[1]
    tw, th = target_size(cw, ch, max_width)
    hwnd_dc = mfc_dc = full_dc = out_dc = full_bmp = out_bmp = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        full_dc = mfc_dc.CreateCompatibleDC()
        full_bmp = win32ui.CreateBitmap()
        full_bmp.CreateCompatibleBitmap(mfc_dc, width, height)
        full_dc.SelectObject(full_bmp)
        if ctypes.windll.user32.PrintWindow(hwnd, full_dc.GetSafeHdc(), 2) != 1:  # PW_RENDERFULLCONTENT
            raise RuntimeError("PrintWindow failed.")
        out_dc = mfc_dc.CreateCompatibleDC()
        out_bmp = win32ui.CreateBitmap()
        out_bmp.CreateCompatibleBitmap(mfc_dc, tw, th)
        out_dc.SelectObject(out_bmp)
        dst = out_dc.GetSafeHdc()
        gdi32.SetStretchBltMode(dst, 4)  # HALFTONE
        gdi32.SetBrushOrgEx(dst, 0, 0, None)
        if not gdi32.StretchBlt(dst, 0, 0, tw, th, full_dc.GetSafeHdc(), crop[0], crop[1], cw, ch, 0x00CC0020):
            raise RuntimeError("StretchBlt failed.")
        info = out_bmp.GetInfo()
        bits = out_bmp.GetBitmapBits(True)
        if info.get("bmBitsPixel", 32) != 32:
            raise RuntimeError(f"Unsupported colour depth {info.get('bmBitsPixel')} bpp.")
        png = encode_png(info["bmWidth"], info["bmHeight"], bits)
    finally:
        for bmp in (full_bmp, out_bmp):
            if bmp is not None:
                try:
                    win32gui.DeleteObject(bmp.GetHandle())
                except Exception:
                    pass
        for dc in (full_dc, out_dc, mfc_dc):
            if dc is not None:
                try:
                    dc.DeleteDC()
                except Exception:
                    pass
        if hwnd_dc is not None:
            win32gui.ReleaseDC(hwnd, hwnd_dc)
    return {"png": png, "width": tw, "height": th, "source_width": cw, "source_height": ch, "region": used_region}
