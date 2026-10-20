"""Optional system-tray icon for mlaude (pystray + Pillow, both best-effort).

Nothing here imports at module load: available() probes, build_icon()
draws, run_tray() blocks on the icon loop. Any failure degrades to False
and the app runs headless with the dashboard as its control surface.
"""
import threading

SIZE = 64


def available():
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


def build_icon():
    """White knot glyph (three rings) on an ink rounded square."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, SIZE - 3, SIZE - 3], radius=15,
                        fill=(22, 33, 45, 255))
    cx, cy, r, w = SIZE // 2, 24, 11, 5
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline="white", width=w)
    d.ellipse([cx - r - 9, cy + 12 - r, cx - r - 9 + 2 * r, cy + 12 + r],
              outline="white", width=w)
    d.ellipse([cx + r + 9 - 2 * r, cy + 12 - r, cx + r + 9, cy + 12 + r],
              outline="white", width=w)
    return img


def run_tray(callbacks):
    """Run the icon loop. Returns True if the tray was shown, else False."""
    if not available():
        return False
    try:
        import pystray
        image = build_icon()
        items = [
            pystray.MenuItem("Open dashboard",
                             lambda icon, item: callbacks["open"]()),
            pystray.MenuItem("Sync now",
                             lambda icon, item: callbacks["sync"]()),
            pystray.MenuItem("Restart router",
                             lambda icon, item: callbacks["restart"]()),
            pystray.MenuItem("Quit",
                             lambda icon, item: _quit(icon, callbacks)),
        ]
        icon = pystray.Icon("mlaude", image, "mlaude router",
                            pystray.Menu(*items))
        icon.run()
        return True
    except Exception:
        return False


def _quit(icon, callbacks):
    try:
        icon.stop()
    finally:
        threading.Thread(target=callbacks["quit"], daemon=True).start()
