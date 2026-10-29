#!/usr/bin/env python3
"""Apple silicon sensors without sudo (4 Oct 2026, Mac Studio M1 Max): the AppleSMC user client through ctypes — °C, the two
fans and PSTR, the logic board's whole DC input in watts (the iMac's sys_w; the "System Total" the Stats app shows) — plus the
thermal pressure level from the notify API (OSThermalNotification, no root either).

Why: the temperature monitor read `sudo powermetrics` on Apple silicon. powermetrics has no smc sampler there ("unrecognized
sampler: smc"), so the dashboard had no °C at all, and under the monitor's background QoS it ran past its 25 s timeout in 13 %
of the minutes while Photos analysis filled the efficiency cores (1.5 s in the foreground, 7-18 s and more as a background job).
One pass here takes ~1 s (4 PSTR reads 0.3 s apart, it updates about once a second), a few ms of it CPU, even at background QoS.

Key map, verified on this Mac13,1 on 4 Oct 2026 (a 3-core burst moved every Tp key 11-17 °C):
  Tp0x come in triplets per core group: …0 the cool edge, …1 the core temperature Stats.app shows (8 performance +
  2 efficiency cores — CPU_KEYS), …2 the group's hot spot (+6-10 °C, slower to fall; TCMz is their maximum — HOT_KEYS);
  Tg05/Tg0D/Tg0L/Tg0T the GPU; TH0x the SSD's NAND (= IOHID "NAND CH0 temp"); F<n>Ac/Mn/Mx the fans (FNum = 2);
  PSTR = PDTR = the 12 V rail (VD0R × ID0R). Temperatures are 'flt ' (little-endian float32) on Apple silicon.
The IOHID route (PrimaryUsagePage 0xff00 / 5) gives only PMIC dies there ("PMU tdie*", a constant "PMU tcal" 51.85) — not the
CPU. Another Mac model may publish other keys: a key it lacks reads None, never an exception.
CLI: host_sensors.py → one JSON line;  host_sensors.py --cpu → the hottest core in °C only (homeserver-status.sh)."""
import ctypes, ctypes.util, json, struct, sys, time

_io = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))
_c = ctypes.cdll.LoadLibrary(ctypes.util.find_library("c"))
_U32, _VP = ctypes.c_uint32, ctypes.c_void_p


class _Info(ctypes.Structure):
    _fields_ = [("size", _U32), ("type", _U32), ("attr", ctypes.c_uint8)]


class _Data(ctypes.Structure):          # SMCKeyData_t, 80 bytes: key, vers[6], pLimitData[16], keyInfo, result, status, cmd, data32, bytes[32]
    _fields_ = [("key", _U32), ("vers", ctypes.c_uint8 * 6), ("plimit", ctypes.c_uint8 * 16), ("info", _Info),
                ("result", ctypes.c_uint8), ("status", ctypes.c_uint8), ("cmd", ctypes.c_uint8), ("data32", _U32),
                ("bytes", ctypes.c_uint8 * 32)]


assert ctypes.sizeof(_Data) == 80
_io.IOServiceMatching.restype, _io.IOServiceMatching.argtypes = _VP, [ctypes.c_char_p]
_io.IOServiceGetMatchingService.restype, _io.IOServiceGetMatchingService.argtypes = _U32, [_U32, _VP]
_io.IOServiceOpen.argtypes = [_U32, _U32, _U32, ctypes.POINTER(_U32)]
_io.IOServiceClose.argtypes = _io.IOObjectRelease.argtypes = [_U32]
_io.IOConnectCallStructMethod.argtypes = [_U32, _U32, _VP, ctypes.c_size_t, _VP, ctypes.POINTER(ctypes.c_size_t)]
_c.notify_register_check.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
_c.notify_get_state.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint64)]
_c.notify_cancel.argtypes = [ctypes.c_int]
_READ_BYTES, _READ_KEYINFO = 5, 9

CPU_KEYS = ("Tp01", "Tp05", "Tp09", "Tp0D", "Tp0H", "Tp0L", "Tp0P", "Tp0T", "Tp0X", "Tp0b")   # the cores (Stats.app's list)
HOT_KEYS = ("Tp02", "Tp06", "Tp0A", "Tp0E", "Tp0I", "Tp0M", "Tp0Q", "Tp0U", "Tp0Y", "Tp0c")   # the core groups' hot spots
GPU_KEYS = ("Tg05", "Tg0D", "Tg0L", "Tg0T")
SSD_KEYS = ("TH0x",)
PRESSURE_NOTIFY = b"com.apple.system.thermalpressurelevel"     # 0 Nominal, 1 Moderate, 2 Heavy, 3 Trapping, 4 Sleeping


class SMC:
    """One user-client connection to AppleSMC (read only). Use as a context manager or close() it."""

    def __init__(self):
        svc = _io.IOServiceGetMatchingService(0, _io.IOServiceMatching(b"AppleSMC"))
        self.conn = _U32()
        kr = _io.IOServiceOpen(svc, _U32.in_dll(_c, "mach_task_self_").value, 0, ctypes.byref(self.conn)) if svc else -1
        if svc:
            _io.IOObjectRelease(svc)
        if kr:
            raise OSError(f"AppleSMC open failed ({kr})")
        self._info = {}

    def close(self):
        if self.conn.value:
            _io.IOServiceClose(self.conn.value)
            self.conn = _U32()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _call(self, inp):
        out, n = _Data(), ctypes.c_size_t(ctypes.sizeof(_Data))
        kr = _io.IOConnectCallStructMethod(self.conn.value, 2, ctypes.byref(inp), ctypes.sizeof(_Data), ctypes.byref(out), ctypes.byref(n))
        return out if kr == 0 and out.result == 0 else None

    def read(self, key):
        """The key's value as a float — 'flt ' (little-endian on Apple silicon), 'ioft', 'sp78', 'fpe2', 'ui8 '/'ui16'/'ui32';
        None for a key this Mac does not have or a type not listed (the key info is cached per connection)."""
        k = struct.unpack(">I", key.encode("latin-1"))[0]
        if key not in self._info:
            o = self._call(_Data(key=k, cmd=_READ_KEYINFO))
            self._info[key] = (o.info.size, struct.pack(">I", o.info.type).decode("latin-1")) if o else None
        if not self._info[key]:
            return None
        size, typ = self._info[key]
        inp = _Data(key=k, cmd=_READ_BYTES)
        inp.info.size = size
        o = self._call(inp)
        if not o:
            return None
        b = bytes(o.bytes[:size])
        if typ == "flt " and size == 4:
            return struct.unpack("<f", b)[0]
        if typ == "ioft" and size == 8:
            return struct.unpack("<q", b)[0] / 65536.0
        if typ == "sp78" and size >= 2:
            return struct.unpack(">h", b[:2])[0] / 256.0
        if typ == "fpe2" and size >= 2:
            return struct.unpack(">H", b[:2])[0] / 4.0
        if typ in ("ui8 ", "ui16", "ui32") and size:
            return float(int.from_bytes(b, "big"))
        return None


def thermal_pressure_level():
    """macOS's thermal pressure level, 0 Nominal … 4 Sleeping (the level powermetrics printed); None when unreadable."""
    tok, st = ctypes.c_int(), ctypes.c_uint64()
    if _c.notify_register_check(PRESSURE_NOTIFY, ctypes.byref(tok)):
        return None
    try:
        return int(st.value) if _c.notify_get_state(tok, ctypes.byref(st)) == 0 and st.value <= 4 else None
    finally:
        _c.notify_cancel(tok)


def _ok(v):
    return v is not None and 5.0 < v < 130.0              # an unplugged sensor reads 0, -127 or garbage


def read_sensors(power_samples=4, gap_s=0.3):
    """One pass: cpu_c (the hottest core), cpu_avg_c, soc_hot_c, gpu_c, ssd_c (°C, one decimal, None when absent), fans
    [{n, rpm, min, max}], sys_w (PSTR, the mean of `power_samples` reads `gap_s` apart) and pressure_level (0..4 or None).
    Raises OSError only when the SMC cannot be opened at all."""
    smc = SMC()
    try:
        get = lambda keys: [v for v in map(smc.read, keys) if _ok(v)]
        cpu, hot, gpu, ssd = get(CPU_KEYS), get(HOT_KEYS), get(GPU_KEYS), get(SSD_KEYS)
        fans = []
        for n in range(int(smc.read("FNum") or 0)):
            rpm = smc.read(f"F{n}Ac")
            if rpm is not None:
                fans.append({"n": n, "rpm": int(round(rpm)), "min": int(round(smc.read(f"F{n}Mn") or 0)), "max": int(round(smc.read(f"F{n}Mx") or 0))})
        watts = []
        for i in range(max(1, power_samples)):
            v = smc.read("PSTR")
            if v is not None and 0 < v < 1000:
                watts.append(v)
            if i + 1 < power_samples:
                time.sleep(gap_s)
    finally:
        smc.close()
    r1 = lambda v: None if v is None else round(v, 1)
    return {"cpu_c": r1(max(cpu, default=None)), "cpu_avg_c": r1(sum(cpu) / len(cpu) if cpu else None),
            "soc_hot_c": r1(max(hot, default=None)), "gpu_c": r1(max(gpu, default=None)), "ssd_c": r1(max(ssd, default=None)),
            "fans": fans, "sys_w": r1(sum(watts) / len(watts) if watts else None), "pressure_level": thermal_pressure_level()}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--cpu" in argv:                                    # homeserver-status.sh: the hottest core only, no power sampling
        c = read_sensors(power_samples=1)["cpu_c"]
        if c is None:
            return 1
        print(f"{c:.1f}")
        return 0
    print(json.dumps(read_sensors()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
