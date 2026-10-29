"""4 Oct 2026: host_sensors.py reads the Mac Studio's SMC without sudo — °C, fans, PSTR — and the thermal pressure level. The
temperature monitor used `sudo powermetrics`, which has no °C on Apple silicon and timed out in 13 % of the minutes at
background QoS. These tests mock the SMC (decoding, key choice, PSTR averaging); one smoke test reads the real one on arm64."""
import contextlib
import ctypes
import importlib.util
import io
import platform
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("host_sensors", ROOT / "host_sensors.py")
hs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hs)


class FakeSMC:
    """read(key) from a dict; PSTR answers from a list, one value per read (it updates about once a second)."""
    def __init__(self, values, pstr=()):
        self.values, self.pstr, self.closed, self.reads = dict(values), list(pstr), False, []

    def read(self, key):
        self.reads.append(key)
        if key == "PSTR":
            return self.pstr.pop(0) if self.pstr else None
        return self.values.get(key)

    def close(self):
        self.closed = True


STUDIO = {**dict(zip(hs.CPU_KEYS, (46.4, 46.0, 44.7, 47.0, 45.7, 41.5, 42.9, 43.8, 45.7, 43.6))),
          **dict(zip(hs.HOT_KEYS, (52.8, 52.3, 48.6, 53.8, 52.5, 49.2, 50.7, 48.3, 54.7, 51.3))),
          "Tg05": 38.8, "Tg0D": 40.9, "Tg0L": 35.4, "Tg0T": 39.0, "TH0x": 30.2,
          "FNum": 2.0, "F0Ac": 1328.6, "F0Mn": 1100.0, "F0Mx": 3500.0, "F1Ac": 1335.7, "F1Mn": 1100.0, "F1Mx": 3500.0}


class ReadSensors(unittest.TestCase):
    def read(self, values, pstr=(), level=0, **kw):
        fake = FakeSMC(values, pstr)
        with patch.object(hs, "SMC", lambda: fake), patch.object(hs, "thermal_pressure_level", lambda: level), \
             patch.object(hs.time, "sleep", lambda s: None):
            return hs.read_sensors(**kw), fake

    def test_the_studio_map(self):
        s, fake = self.read(STUDIO, pstr=(20.0, 22.0, 18.0, 24.0), level=1)
        self.assertEqual(s["cpu_c"], 47.0)                                     # the hottest of the 10 core sensors (Stats.app's)
        self.assertEqual(s["cpu_avg_c"], round(sum(STUDIO[k] for k in hs.CPU_KEYS) / 10, 1))
        self.assertEqual(s["soc_hot_c"], 54.7); self.assertEqual(s["gpu_c"], 40.9); self.assertEqual(s["ssd_c"], 30.2)
        self.assertEqual(s["fans"], [{"n": 0, "rpm": 1329, "min": 1100, "max": 3500}, {"n": 1, "rpm": 1336, "min": 1100, "max": 3500}])
        self.assertEqual(s["sys_w"], 21.0)                                     # the mean of the four PSTR reads
        self.assertEqual(s["pressure_level"], 1)
        self.assertTrue(fake.closed); self.assertEqual(fake.reads.count("PSTR"), 4)

    def test_unplugged_or_garbage_sensors_are_ignored(self):
        values = dict(STUDIO, Tp01=0.0, Tp05=-127.0, Tp09=255.0, Tg05=None, TH0x=0.0, PSTR=None)
        s, _ = self.read(values, pstr=(0.0, 2000.0, 15.5, None))
        self.assertEqual(s["cpu_c"], 47.0); self.assertEqual(s["ssd_c"], None); self.assertEqual(s["gpu_c"], 40.9)
        self.assertEqual(s["cpu_avg_c"], round(sum(STUDIO[k] for k in hs.CPU_KEYS[3:]) / 7, 1))
        self.assertEqual(s["sys_w"], 15.5)                                     # 0 W and 2000 W are not readings

    def test_a_mac_without_these_keys_reads_none_not_an_error(self):
        s, fake = self.read({}, pstr=())
        self.assertEqual(s, {"cpu_c": None, "cpu_avg_c": None, "soc_hot_c": None, "gpu_c": None, "ssd_c": None,
                             "fans": [], "sys_w": None, "pressure_level": 0})
        self.assertTrue(fake.closed)

    def test_a_fan_without_a_speed_is_left_out(self):
        s, _ = self.read(dict(STUDIO, F1Ac=None))
        self.assertEqual([f["n"] for f in s["fans"]], [0])

    def test_one_power_sample_does_not_sleep(self):
        slept = []
        fake = FakeSMC(STUDIO, (17.25,))
        with patch.object(hs, "SMC", lambda: fake), patch.object(hs, "thermal_pressure_level", lambda: 0), \
             patch.object(hs.time, "sleep", lambda s: slept.append(s)):
            self.assertEqual(hs.read_sensors(power_samples=1)["sys_w"], 17.2)
        self.assertEqual(slept, [])

    def test_the_connection_is_closed_when_a_read_raises(self):
        fake = FakeSMC(STUDIO)
        fake.read = lambda key: (_ for _ in ()).throw(OSError("smc"))
        with patch.object(hs, "SMC", lambda: fake), self.assertRaises(OSError):
            hs.read_sensors()
        self.assertTrue(fake.closed)


class Decoding(unittest.TestCase):
    """SMC.read() over a mocked IOConnectCallStructMethod: key info first (cmd 9), then the bytes (cmd 5)."""
    TABLE = {"Tp01": ("flt ", struct.pack("<f", 46.43)), "TR0Z": ("ioft", struct.pack("<q", int(51.85 * 65536))),
             "TC0P": ("sp78", struct.pack(">h", int(52.5 * 256))), "F0Ac": ("fpe2", struct.pack(">H", 1200 * 4)),
             "FNum": ("ui8 ", b"\x02"), "AC-I": ("ui16", struct.pack(">H", 47)), "ACPO": ("ui32", struct.pack(">I", 1346764800)),
             "DUTC": ("flag", b"\x01"), "D1UI": ("hex_", b"\x3c\x62")}

    def smc(self):
        smc = hs.SMC.__new__(hs.SMC)
        smc.conn, smc._info, calls = hs._U32(1), {}, []

        def call(inp):
            key = struct.pack(">I", inp.key).decode("latin-1")
            calls.append((key, inp.cmd))
            if key not in self.TABLE:
                return None
            typ, data = self.TABLE[key]
            out = hs._Data()
            if inp.cmd == hs._READ_KEYINFO:
                out.info.size, out.info.type = len(data), struct.unpack(">I", typ.encode("latin-1"))[0]
            else:
                self.assertEqual(inp.info.size, len(data))
                ctypes.memmove(out.bytes, data, len(data))
            return out
        smc._call = call
        return smc, calls

    def test_types(self):
        smc, _ = self.smc()
        self.assertAlmostEqual(smc.read("Tp01"), 46.43, places=4)            # Apple silicon: little-endian float32
        self.assertAlmostEqual(smc.read("TR0Z"), 51.85, places=3)
        self.assertEqual(smc.read("TC0P"), 52.5); self.assertEqual(smc.read("F0Ac"), 1200.0)
        self.assertEqual(smc.read("FNum"), 2.0); self.assertEqual(smc.read("AC-I"), 47.0); self.assertEqual(smc.read("ACPO"), 1346764800.0)
        self.assertIsNone(smc.read("DUTC")); self.assertIsNone(smc.read("D1UI"))   # flag / hex: not numbers

    def test_missing_key_is_none_and_its_info_cached(self):
        smc, calls = self.smc()
        self.assertIsNone(smc.read("Tz99")); self.assertIsNone(smc.read("Tz99"))
        self.assertEqual(calls, [("Tz99", hs._READ_KEYINFO)])                 # asked once
        smc.read("Tp01"); smc.read("Tp01")
        self.assertEqual([c for c in calls if c[0] == "Tp01"], [("Tp01", 9), ("Tp01", 5), ("Tp01", 5)])

    def test_struct_layout_is_the_kernel_one(self):
        self.assertEqual(ctypes.sizeof(hs._Data), 80)
        self.assertEqual(hs._Data.info.offset, 28); self.assertEqual(hs._Data.cmd.offset, 42); self.assertEqual(hs._Data.bytes.offset, 48)


class Cli(unittest.TestCase):
    def run_main(self, argv, cpu):
        out = io.StringIO()
        with patch.object(hs, "read_sensors", lambda power_samples=4: {"cpu_c": cpu, "sys_w": 17.2}), contextlib.redirect_stdout(out):
            rc = hs.main(argv)
        return rc, out.getvalue()

    def test_cpu_only_for_the_status_script(self):
        self.assertEqual(self.run_main(["--cpu"], 47.46), (0, "47.5\n"))
        self.assertEqual(self.run_main(["--cpu"], None), (1, ""))
        rc, text = self.run_main([], 47.46)
        self.assertEqual(rc, 0); self.assertIn('"sys_w": 17.2', text)


@unittest.skipUnless(sys.platform == "darwin" and platform.machine() == "arm64", "the real SMC: Apple silicon only")
class LiveSmoke(unittest.TestCase):
    def test_reads_without_root(self):
        try:
            s = hs.read_sensors(power_samples=1)
        except OSError as exc:
            self.skipTest(f"no AppleSMC here: {exc}")
        self.assertEqual(set(s), {"cpu_c", "cpu_avg_c", "soc_hot_c", "gpu_c", "ssd_c", "fans", "sys_w", "pressure_level"})
        for k in ("cpu_c", "gpu_c", "ssd_c"):
            self.assertTrue(s[k] is None or 5 < s[k] < 130, (k, s[k]))
        self.assertTrue(s["sys_w"] is None or 0 < s["sys_w"] < 1000)
        self.assertIn(s["pressure_level"], (None, 0, 1, 2, 3, 4))


if __name__ == "__main__":
    unittest.main()
