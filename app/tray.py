"""Optional system-tray icon for mlaude (pystray + Pillow, both best-effort).

Nothing here imports at module load: available() probes, build_icon()
draws, run_tray() blocks on the icon loop. Any failure degrades to False
and the app runs headless with the dashboard as its control surface.
"""
import re
import threading

SIZE = 64

# Trefoil-knot brand mark: the same Opus glyph as the dashboard header +
# favicon (24x24 viewBox). Stored as path data and rasterized with Pillow,
# so the tray needs no extra dependencies or image files.
TREFOIL_VIEWBOX = 24.0
TREFOIL_PATH = (
    "M20.08 13.21C20.19 13.35 20.57 13.75 20.77 14.03C20.97 14.32 21.15 "
    "14.61 21.3 14.9C21.45 15.19 21.58 15.49 21.67 15.79C21.76 16.08 21.83 "
    "16.38 21.87 16.67C21.9 16.96 21.91 17.25 21.89 17.53C21.87 17.81 21.82 "
    "18.08 21.74 18.34C21.66 18.6 21.56 18.85 21.42 19.08C21.29 19.32 21.14 "
    "19.54 20.96 19.74C20.78 19.94 20.57 20.12 20.34 20.29C20.12 20.45 19.87 "
    "20.59 19.6 20.71C19.34 20.83 19.05 20.93 18.75 21C18.45 21.07 18.14 21.12 "
    "17.81 21.15C17.49 21.17 17.15 21.16 16.8 21.13C16.46 21.1 16.1 21.05 "
    "15.74 20.96C15.39 20.88 15.03 20.77 14.67 20.64C14.31 20.5 13.94 20.34 "
    "13.59 20.15C13.23 19.96 12.88 19.75 12.54 19.51C12.19 19.28 11.85 19.02 "
    "11.53 18.74C11.2 18.46 10.89 18.15 10.59 17.83C10.29 17.51 10 17.17 9.74 "
    "16.82C9.47 16.46 9.21 16.08 8.99 15.71C8.76 15.34 8.49 14.78 8.39 "
    "14.6M7.54 6.94C7.6 6.78 7.77 6.25 7.91 5.93C8.05 5.61 8.21 5.31 8.39 "
    "5.04C8.57 4.76 8.77 4.5 8.97 4.28C9.18 4.05 9.41 3.84 9.64 3.67C9.87 3.49 "
    "10.12 3.34 10.37 3.22C10.63 3.1 10.89 3 11.15 2.94C11.41 2.88 11.69 2.84 "
    "11.95 2.84C12.22 2.84 12.49 2.86 12.75 2.92C13.02 2.98 13.28 3.06 13.54 "
    "3.18C13.79 3.29 14.04 3.43 14.28 3.6C14.51 3.77 14.74 3.97 14.95 "
    "4.2C15.16 4.42 15.36 4.67 15.55 4.94C15.73 5.21 15.89 5.51 16.04 "
    "5.82C16.19 6.13 16.32 6.47 16.42 6.82C16.53 7.17 16.61 7.54 16.68 "
    "7.92C16.74 8.3 16.78 8.69 16.79 9.09C16.81 9.49 16.8 9.91 16.77 "
    "10.32C16.74 10.74 16.68 11.16 16.6 11.58C16.52 12.01 16.42 12.43 16.29 "
    "12.85C16.16 13.27 16.01 13.69 15.84 14.1C15.66 14.5 15.46 14.92 15.25 "
    "15.3C15.04 15.68 14.7 16.19 14.59 16.37M8.38 20.93C8.2 20.97 7.67 21.09 "
    "7.32 21.12C6.98 21.16 6.63 21.17 6.31 21.15C5.98 21.14 5.66 21.09 5.35 "
    "21.03C5.05 20.96 4.76 20.87 4.49 20.75C4.22 20.64 3.97 20.5 3.74 "
    "20.34C3.51 20.19 3.3 20.01 3.11 19.81C2.92 19.61 2.76 19.39 2.62 "
    "19.17C2.49 18.94 2.37 18.69 2.29 18.43C2.21 18.17 2.15 17.9 2.12 "
    "17.63C2.09 17.35 2.09 17.06 2.12 16.77C2.15 16.48 2.21 16.18 2.3 "
    "15.89C2.38 15.6 2.5 15.3 2.64 15C2.79 14.71 2.96 14.42 3.16 14.13C3.36 "
    "13.85 3.58 13.57 3.83 13.31C4.08 13.04 4.36 12.78 4.66 12.54C4.95 12.29 "
    "5.28 12.06 5.62 11.85C5.96 11.63 6.32 11.43 6.69 11.25C7.07 11.07 7.46 "
    "10.91 7.87 10.77C8.27 10.63 8.7 10.5 9.12 10.4C9.55 10.31 9.99 10.23 "
    "10.43 10.17C10.87 10.12 11.33 10.09 11.76 10.08C12.2 10.07 12.81 10.11 "
    "13.02 10.12"
)

_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_TOKEN = re.compile(r"[MmCcLlZz]|" + _FLOAT)


def _parse_trefoil(path):
    """SVG M/C/L/Z path data -> list of subpaths (each a point list).

    Relative commands are offset from the pen; close (Z) re-joins the
    subpath start. Raises ValueError on anything else.
    """
    subs, cur, pen, start = [], None, (0.0, 0.0), None
    nums, cmd = [], None

    def flush_number_run(op):
        nonlocal cur, pen, start
        pts = [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]
        if op in ("M", "m"):
            first = True
            for p in pts:
                if op == "m":
                    p = (pen[0] + p[0], pen[1] + p[1])
                if first:
                    cur = [p]
                    subs.append(cur)
                    start = p
                    first = False
                else:
                    cur.append(p)
                pen = p
        elif op in ("L", "l"):
            for p in pts:
                if op == "l":
                    p = (pen[0] + p[0], pen[1] + p[1])
                cur.append(p)
                pen = p
        elif op in ("C", "c"):
            for i in range(0, len(pts), 3):
                p1, p2, p = pts[i:i + 3]
                if op == "c":
                    p1 = (pen[0] + p1[0], pen[1] + p1[1])
                    p2 = (pen[0] + p2[0], pen[1] + p2[1])
                    p = (pen[0] + p[0], pen[1] + p[1])
                for t in range(1, 17):
                    u = t / 16.0
                    cur.append(_bez(pen, p1, p2, p, u))
                pen = p

    parts = _TOKEN.findall(path)
    if "".join(parts).replace(" ", "") != path.replace(" ", "").replace(",", ""):
        raise ValueError("unparseable trefoil path data")
    for tok in parts:
        if tok in "MmCcLlZz":
            if cmd and nums:
                if len(nums) % 2:
                    raise ValueError("odd coordinate count")
                need = {"M": 2, "L": 2, "C": 6}[cmd.upper()]
                if len(nums) % need:
                    raise ValueError("bad coordinate count for %s" % cmd)
                if cur is None and cmd.upper() != "M":
                    raise ValueError("path must start with M")
                flush_number_run(cmd)
                nums = []
            if tok in "Zz":
                if cur:
                    cur.append(start)
                    pen = start
                cmd = None
            else:
                cmd = tok
        else:
            nums.append(float(tok))
    if cmd and nums:
        if len(nums) % 2:
            raise ValueError("odd coordinate count")
        need = {"M": 2, "L": 2, "C": 6}[cmd.upper()]
        if len(nums) % need:
            raise ValueError("bad coordinate count for %s" % cmd)
        if cur is None and cmd.upper() != "M":
            raise ValueError("path must start with M")
        flush_number_run(cmd)
    if not subs or any(len(s) < 2 for s in subs):
        raise ValueError("empty trefoil path")
    return subs


def _bez(p0, p1, p2, p3, t):
    u = 1.0 - t
    return (u * u * u * p0[0] + 3 * u * u * t * p1[0]
            + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1]
            + 3 * u * t * t * p2[1] + t * t * t * p3[1])


def available():
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


def build_icon():
    """White trefoil-knot brand mark on an ink rounded square."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, SIZE - 3, SIZE - 3], radius=15,
                        fill=(22, 33, 45, 255))
    pad, width = 12, 4
    span = SIZE - 2 * pad
    for sub in _parse_trefoil(TREFOIL_PATH):
        flat = []
        for p in sub:
            flat += [pad + p[0] / TREFOIL_VIEWBOX * span,
                     pad + p[1] / TREFOIL_VIEWBOX * span]
        d.line(flat, fill="white", width=width, joint="curve")
        # Round caps (Pillow lines end square): dots at both ends.
        for p in (sub[0], sub[-1]):
            x = pad + p[0] / TREFOIL_VIEWBOX * span
            y = pad + p[1] / TREFOIL_VIEWBOX * span
            d.ellipse([x - width / 2, y - width / 2,
                       x + width / 2, y + width / 2], fill="white")
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
