#!/usr/bin/env python3
"""Tests for app/main.py + app/tray.py. Real local socket, no mocks."""
import http.server
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main
import tray


def _bind_ok():
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.close()
    except OSError:
        return False
    return True


BIND_OK = _bind_ok()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(BIND_OK, "local port binding blocked in sandbox")
class WaitTests(unittest.TestCase):
    def test_open_port_answers(self):
        server = http.server.HTTPServer(("127.0.0.1", 0),
                                        http.server.SimpleHTTPRequestHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            self.assertTrue(main.wait_for_router(port, timeout=5))
        finally:
            server.shutdown()

    def test_closed_port_times_out(self):
        self.assertFalse(main.wait_for_router(free_port(), timeout=0.3))


class TrayTests(unittest.TestCase):
    def test_available_is_bool(self):
        self.assertIsInstance(tray.available(), bool)

    def test_trefoil_path_parses_clean(self):
        subs = tray._parse_trefoil(tray.TREFOIL_PATH)
        self.assertEqual(len(subs), 3)
        for sub in subs:
            self.assertGreater(len(sub), 10)
            for x, y in sub:
                self.assertGreaterEqual(x, 0)
                self.assertLessEqual(x, tray.TREFOIL_VIEWBOX)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(y, tray.TREFOIL_VIEWBOX)

    def test_trefoil_path_matches_glyph(self):
        self.assertEqual(len(tray.TREFOIL_PATH), 2233)
        self.assertTrue(tray.TREFOIL_PATH.startswith("M20.08 13.21"))
        self.assertTrue(tray.TREFOIL_PATH.endswith("13.02 10.12"))

    def test_garbage_path_rejected(self):
        with self.assertRaises(ValueError):
            tray._parse_trefoil("M1 1 Q2 2 3 3")
        with self.assertRaises(ValueError):
            tray._parse_trefoil("not a path")

    @unittest.skipUnless(tray.available(), "pystray+Pillow installed")
    def test_icon_builds(self):
        img = tray.build_icon()
        self.assertEqual(img.size, (64, 64))
        self.assertEqual(img.getpixel((0, 0))[3], 0)
        white = sum(1 for p in img.getdata()
                    if p[3] > 128 and min(p[:3]) > 200)
        self.assertGreater(white, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
