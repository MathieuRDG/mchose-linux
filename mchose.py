#!/usr/bin/env python3
"""MCHOSE A7 V2 Ultra (and its siblings): battery and settings over hidraw.

Standard library only. The protocol comes from MCHOSE's own M HUB web driver
(mchose.com.cn), cross-checked against github.com/alexfrih/mchose-linux, and
verified on an A7 V2 Ultra: dongle 3837:100b, model id 0x4019, firmware 5.44.2.4.

Transport: feature reports on the vendor collection (usage page 0xFF01) of the
third USB interface, report 0x11 (20 payload bytes) or 0x12 (64). The payload is
the command byte plus its arguments, every byte XOR 0xFF, padding included. A
reply echoes cmd ^ 0xFF in byte 1. Until the mouse has answered over the radio,
the dongle hands back the request itself (all-zero once decoded), so a read is
re-sent until a real reply shows up.

That interface is not the one the pointer moves on, and feature reports travel on
the control endpoint, so asking the mouse something does not delay its motion
reports.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import select
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

VIDS = (0x3837, 0x5253)
SHORT, LONG = 0x11, 0x12
LENGTH = {SHORT: 20, LONG: 64}

# Model table from the M HUB bundle: model id (the mouse's own wired PID) ->
# name, receiver PIDs (index 0 is the cable), report-rate ceiling per PID,
# lowest LOD, DPI ceiling. Only the A7 V2 Ultra has been tested here; the G3 and
# A5 families are left out because M HUB drives them with another protocol.
_MODELS = (
    ("MCHOSE M7", (32, 4129, 4128), (1000, 1000, 8000), 1.0, 26000),
    ("MCHOSE M7 Pro", (48, 4129, 4128), (8000, 1000, 8000), 1.0, 26000),
    ("MCHOSE M7 Ultra", (49, 4129, 4128), (8000, 1000, 8000), 0.7, 42000),
    ("MCHOSE L7", (192, 4129, 4128), (1000, 1000, 8000), 1.0, 26000),
    ("MCHOSE L7 Pro", (176, 4129, 4128), (8000, 1000, 8000), 1.0, 26000),
    ("MCHOSE L7 Pro+", (16405, 4129, 4128), (8000, 1000, 8000), 1.0, 26000),
    ("MCHOSE L7 Ultra", (177, 4129, 4128), (8000, 1000, 8000), 0.7, 42000),
    ("MCHOSE L7 Ultra+", (16406, 4129, 4128), (8000, 1000, 8000), 0.7, 42000),
    ("MCHOSE A7", (112, 4129, 4128), (1000, 1000, 8000), 1.0, 26000),
    ("MCHOSE A7 Pro", (16, 4129, 4128), (8000, 1000, 8000), 1.0, 26000),
    ("MCHOSE A7 Ultra", (17, 4129, 4128), (8000, 1000, 8000), 0.7, 42000),
    ("MCHOSE A7 V2 Pro", (16408, 4106, 4107, 4128), (8000, 1000, 8000, 8000), 1.0, 26000),
    ("MCHOSE A7 V2 Pro+", (16419, 4106, 4107, 4128), (8000, 1000, 8000, 8000), 1.0, 26000),
    ("MCHOSE A7 V2 Ultra", (16409, 4106, 4107, 4128), (8000, 1000, 8000, 8000), 0.7, 42000),
    ("MCHOSE A7 V2 Ultra+", (16417, 4106, 4107, 4128), (8000, 1000, 8000, 8000), 0.7, 42000),
    ("MCHOSE A7X Ultra", (16401, 4106, 4105), (8000, 1000, 8000), 0.7, 42000),
    ("MCHOSE A7 Ultra(RE)", (16656, 4129, 4128), (8000, 1000, 8000), 0.7, 42000),
    ("MCHOSE K7 Ultra", (16720, 4097, 4129, 4128), (8000, 8000, 1000, 8000), 0.7, 42000),
    ("MCHOSE AX5 V2", (16400, 161, 4129, 4128), (8000, 8000, 1000, 8000), 0.7, 42000),
)


@dataclass(frozen=True)
class Model:
    name: str
    pids: tuple
    ceilings: tuple
    lod_floor: float
    dpi_max: int

    def rates(self, receiver_pid: int | None) -> tuple:
        """Report rates the given link accepts, by index (None = the cable)."""
        i = self.pids.index(receiver_pid) if receiver_pid in self.pids[1:] else 0
        return RATES[self.ceilings[i]]

    @property
    def lods(self) -> tuple:
        """LOD codes this model offers: 0 = 0.7 mm, 1 = 1 mm, 2 = 2 mm."""
        return (0, 1, 2) if self.lod_floor < 1 else (1, 2)


MODELS = {m[1][0]: Model(*m) for m in _MODELS}
KNOWN_PIDS = {pid for m in MODELS.values() for pid in m.pids}
FALLBACK = Model("MCHOSE (modèle inconnu)", (0,), (1000,), 1.0, 26000)

RATES = {
    1000: (125, 500, 1000),
    4000: (125, 500, 1000, 2000, 4000),
    8000: (125, 500, 1000, 2000, 4000, 8000),
}
LOD_MM = {0: 0.7, 1: 1.0, 2: 2.0}
MODES = {1: "performance", 2: "gaming", 3: "extreme"}
MODE_LABELS = {1: "Performance", 2: "Gaming", 3: "Extreme"}
DPI_MIN = 200
DEBOUNCE_MAX = 20
SLEEP_MAX = 60

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "mchose"

_debug = False


def _log(*parts):
    if _debug:
        print(*parts, file=sys.stderr)


def _hex(b) -> str:
    return " ".join(f"{x:02x}" for x in b)


def _ioc(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr


# ---------------------------------------------------------------- discovery

@dataclass(frozen=True)
class Node:
    path: str
    vid: int
    pid: int
    name: str
    phys: str


def _read(path, mode="r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return b"" if "b" in mode else ""


def _hidraw_nodes():
    base = Path("/sys/class/hidraw")
    if not base.is_dir():
        return
    for entry in sorted(base.iterdir(), key=lambda p: int(p.name[6:] or 0)):
        uevent = _read(entry / "device/uevent")
        info = dict(line.split("=", 1) for line in uevent.splitlines() if "=" in line)
        try:
            _, vid, pid = info.get("HID_ID", "").split(":")
            vid, pid = int(vid, 16), int(pid, 16)
        except ValueError:
            continue
        yield entry, Node(f"/dev/{entry.name}", vid, pid,
                          info.get("HID_NAME", "").strip(), info.get("HID_PHYS", ""))


def find_nodes() -> list[Node]:
    """Config collections of known MCHOSE PIDs, cable first: when the mouse is on
    its cable the dongle goes quiet, and the cable is the one that reports charging."""
    out = []
    for entry, node in _hidraw_nodes():
        if node.vid not in VIDS or node.pid not in KNOWN_PIDS:
            continue
        if b"\x06\x01\xff" not in _read(entry / "device/report_descriptor", "rb"):
            continue
        out.append(node)
    out.sort(key=lambda n: n.pid not in MODELS)
    return out


def motion_node(cfg: Node) -> str | None:
    """The hidraw node the pointer moves on: interface 0 of the same USB device."""
    phys = cfg.phys.rsplit("/input", 1)[0]
    for entry, node in _hidraw_nodes():
        if node.pid == cfg.pid and node.phys == f"{phys}/input0":
            return node.path
    return None


# ------------------------------------------------------------------ records

@dataclass
class Status:
    vid: int
    model_id: int
    firmware: str
    link: str          # "2.4G", "cable", "bluetooth" or "?"
    connected: bool
    battery: int
    charging: bool

    @property
    def wireless(self) -> bool:
        return self.link != "cable"


class Config:
    """The 63-byte block shared by read 0x12 0x67 and write 0x12 0x57. Kept as raw
    bytes so a read-modify-write never drops a field this tool does not model."""

    def __init__(self, body: bytes):
        self.b = bytearray(body[:63])

    def _u16(self, at):
        return self.b[at] | self.b[at + 1] << 8

    # Byte 1 packs the cable's rate (high nibble) and DPI stage (low), byte 2 the
    # radio's. Settled on this mouse: it reported 500 Hz in byte 2 while the
    # measured 2.4 GHz report rate was 500 Hz and byte 1 said 1000.
    def rate_index(self, wireless: bool) -> int:
        return self.b[2 if wireless else 1] >> 4

    def stage(self, wireless: bool) -> int:
        return self.b[2 if wireless else 1] & 0x0F

    @property
    def dpis(self) -> list[int]:
        return [self._u16(4 + 2 * i) for i in range(6)]

    @property
    def dpis_y(self) -> list[int]:
        return [self._u16(51 + 2 * i) for i in range(6)]

    @property
    def stages(self) -> int:
        return self.b[16]

    # The sensor byte, as M HUB reads it: bits 0-1 LOD (00 0.7 mm, 01 1 mm,
    # 10 2 mm), bit 2 ripple control, bit 3 angle snapping, bit 4 motion sync,
    # bits 6-7 the mode (0x = performance, 10 = gaming, 11 = extreme).
    @property
    def sensor(self) -> int:
        return self.b[17]

    @property
    def lod(self) -> int:
        return {0: 0, 1: 1, 2: 2}.get(self.sensor & 0x03, 2)

    @property
    def ripple(self) -> bool:
        return bool(self.sensor & 0x04)

    @property
    def angle_snap(self) -> bool:
        return bool(self.sensor & 0x08)

    @property
    def motion_sync(self) -> bool:
        return bool(self.sensor & 0x10)

    @property
    def mode(self) -> int:
        if not self.sensor & 0x80:
            return 1
        return 3 if self.sensor & 0x40 else 2

    @property
    def debounce(self) -> int:
        return self.b[18]

    @property
    def sleep(self) -> int:
        return self.b[19]


# ------------------------------------------------------------------- device

class Mouse:
    """One open config node. Use as a context manager: it holds a lock shared by
    every mchose process, so the tray and the window never interleave frames."""

    MIN_GAP = 0.025   # the config channel chokes when talked at faster than this

    def __init__(self, node: Node):
        self.node = node
        self.fd = -1
        self._lock = None
        self._last = 0.0
        self.status: Status | None = None
        self.model: Model = MODELS.get(node.pid, FALLBACK)

    def __enter__(self):
        run = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
        self._lock = open(run / "mchose.lock", "w")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        try:
            self.fd = os.open(self.node.path, os.O_RDWR | os.O_CLOEXEC)
        except OSError:
            self._unlock()
            raise
        return self

    def __exit__(self, *exc):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        self._unlock()

    def _unlock(self):
        if self._lock:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    # -- frames

    def _pace(self):
        wait = self._last + self.MIN_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    @staticmethod
    def _frame(report: int, body) -> bytes:
        n = LENGTH[report]
        if len(body) > n:
            raise ValueError("command longer than its report")
        return bytes([report]) + bytes(b ^ 0xFF for b in body) + b"\xff" * (n - len(body))

    def send(self, report: int, body):
        frame = self._frame(report, body)
        self._pace()
        fcntl.ioctl(self.fd, _ioc(0x06, len(frame)), frame)
        _log("->", f"{report:02x}", _hex(body))

    def request(self, report: int, body, tries: int = 8) -> bytes | None:
        """Ask, and re-ask until the reply is a real one. None when the mouse stays
        silent, which is what a sleeping or switched-off mouse looks like."""
        frame = self._frame(report, body)
        n = LENGTH[report]
        for attempt in range(tries):
            self._pace()
            fcntl.ioctl(self.fd, _ioc(0x06, len(frame)), frame)
            time.sleep(0.03 if report == SHORT else 0.06)
            buf = bytearray(n + 1)
            buf[0] = report
            try:
                fcntl.ioctl(self.fd, _ioc(0x07, len(buf)), buf)
            except OSError as e:
                _log(f"<- {report:02x} {body[0]:02x} try {attempt + 1}: {e}")
                time.sleep(0.03)
                continue
            if buf[1] ^ 0xFF == body[0] and bytes(buf) != frame:
                payload = bytes(b ^ 0xFF for b in buf[2:])
                if any(payload):
                    _log(f"<- {report:02x} {body[0]:02x}", _hex(payload))
                    return payload
            _log(f"<- {report:02x} {body[0]:02x} try {attempt + 1}: miss {_hex(buf[:12])}")
            time.sleep(0.03)
        return None

    # -- reads

    def read_status(self, tries: int = 6) -> Status | None:
        """0x11 0x06. The first reply after an idle spell can carry a zero vendor
        id while the mouse wakes up; that is a miss, not an answer."""
        for _ in range(3):
            p = self.request(SHORT, [0x06], tries)
            if p is None:
                return None
            vid = p[0] | p[1] << 8
            if vid == 0:
                time.sleep(0.12)
                continue
            if vid not in VIDS or p[9] > 100:
                return None
            model_id = p[2] | p[3] << 8
            self.model = MODELS.get(model_id, self.model)
            self.status = Status(
                vid=vid,
                model_id=model_id,
                firmware=".".join(str(x) for x in p[4:8]),
                link={0: "cable", 1: "2.4G", 2: "bluetooth"}.get(p[8] & 0x07, "?"),
                connected=bool(p[8] & 0x08),
                battery=p[9],
                charging=bool(p[10]),
            )
            return self.status
        return None

    def read_config(self) -> Config:
        for attempt in range(4):
            p = self.request(LONG, [0x67])
            if p and len(p) >= 63 and any(p[:63]):
                return Config(p)
            time.sleep(0.15 * (attempt + 1))
        raise MchoseError("la souris ne renvoie pas sa configuration (en veille ? bouge-la)")

    def receiver_pid(self) -> int | None:
        """USB PID of the 2.4 GHz receiver, which picks the rate list; None on cable."""
        if self.status and not self.status.wireless:
            return None
        return self.node.pid if self.node.pid not in MODELS else None

    def rates(self) -> tuple:
        return self.model.rates(self.receiver_pid())

    # -- writes, each mirroring what M HUB sends

    def _confirm(self, check, what: str) -> Config:
        """Writes are fire-and-forget; the mouse needs a moment to commit, so read
        the block back until it shows the change."""
        last = None
        for attempt in range(6):
            time.sleep(0.08 * (attempt + 1))
            try:
                last = self.read_config()
            except MchoseError:
                continue
            if check(last):
                return last
        raise MchoseError(f"la souris n'a pas pris le changement ({what})")

    def _backup_once(self, cfg: Config):
        path = STATE_DIR / "config-original.bin"
        if not path.exists():
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(cfg.b))

    def set_dpi(self, dpis: list[int], stage: int, count: int) -> Config:
        """0x12 0x40: six stages, the active one, how many are enabled. The Y axis
        is kept equal to X, as M HUB does unless asked otherwise."""
        cfg = self.read_config()
        self._backup_once(cfg)
        if len(dpis) != 6 or not 1 <= count <= 6 or not 0 <= stage < count:
            raise MchoseError("il faut 6 paliers, 1 à 6 actifs, et un palier actif parmi eux")
        for d in dpis:
            if not DPI_MIN <= d <= self.model.dpi_max:
                raise MchoseError(f"DPI hors limites ({DPI_MIN}-{self.model.dpi_max}) : {d}")
        body = [0x40, stage, stage, 0]
        for d in dpis:
            body += [d & 0xFF, d >> 8]
        body += [count, 0xFF]
        for d in dpis:
            body += [d & 0xFF, d >> 8]
        self.send(LONG, body)
        w = self.status.wireless if self.status else True
        return self._confirm(lambda c: c.dpis == dpis and c.stages == count
                             and c.stage(w) == stage, "DPI")

    def set_rate(self, hz: int) -> Config:
        """0x11 0x41 (cable rate, radio rate). M HUB zeroes the other link's field;
        this sends that link's current index instead, so it stays put whether or
        not the firmware applies the field."""
        rates = self.rates()
        if hz not in rates:
            raise MchoseError(f"taux non supporté sur ce lien : {hz} (choix : {', '.join(map(str, rates))})")
        idx = rates.index(hz)
        w = self.status.wireless if self.status else True
        cfg = self.read_config()
        self._backup_once(cfg)
        self.send(SHORT, [0x41, cfg.rate_index(False), idx] if w else [0x41, idx, cfg.rate_index(True)])
        return self._confirm(lambda c: c.rate_index(w) == idx, "polling rate")

    def _sensor(self, lod=None, ripple=None, angle=None, motion=None, mode=None) -> Config:
        """0x11 0x42. Per field: 0 leaves it alone, 1 turns it on, 2 turns it off;
        mode is 1-3. M HUB always sends the LOD along, so this does too."""
        cfg = self.read_config()
        self._backup_once(cfg)
        flag = lambda v: 0 if v is None else (1 if v else 2)  # noqa: E731
        code = cfg.lod if lod is None else lod
        self.send(SHORT, [0x42, code, flag(ripple), flag(angle), flag(motion), 0, 0,
                          mode or 0, 0, 0])

        def ok(c: Config):
            return (c.lod == code
                    and (ripple is None or c.ripple == ripple)
                    and (angle is None or c.angle_snap == angle)
                    and (motion is None or c.motion_sync == motion)
                    and (mode is None or c.mode == mode))
        return self._confirm(ok, "capteur")

    def set_lod(self, code: int) -> Config:
        if code not in self.model.lods:
            raise MchoseError("LOD non supporté : " + ", ".join(f"{LOD_MM[c]:g}" for c in self.model.lods))
        return self._sensor(lod=code)

    def set_flags(self, ripple=None, angle=None, motion=None) -> Config:
        return self._sensor(ripple=ripple, angle=angle, motion=motion)

    def set_mode(self, mode: int) -> Config:
        if mode not in MODES:
            raise MchoseError("mode : performance, gaming ou extreme")
        return self._sensor(mode=mode)

    def set_debounce(self, ms: int) -> Config:
        if not 0 <= ms <= DEBOUNCE_MAX:
            raise MchoseError(f"debounce entre 0 et {DEBOUNCE_MAX} ms")
        self._backup_once(self.read_config())
        self.send(SHORT, [0x43, ms])
        return self._confirm(lambda c: c.debounce == ms, "debounce")

    def set_sleep(self, minutes: int) -> Config:
        """0x11 0x0A (enabled, minutes); 0 means never."""
        if not 0 <= minutes <= SLEEP_MAX:
            raise MchoseError(f"veille entre 1 et {SLEEP_MAX} min, ou 0 pour jamais")
        self._backup_once(self.read_config())
        self.send(SHORT, [0x0A, 1 if minutes else 0, minutes])
        return self._confirm(lambda c: c.sleep == minutes, "veille")

    def restore(self, body: bytes) -> Config:
        """0x12 0x57, the whole block. Byte 3 belongs to the mouse, so it is not compared."""
        if len(body) != 63:
            raise MchoseError("une sauvegarde fait 63 octets")
        self.send(LONG, [0x57, *body])
        want = bytes(body)
        return self._confirm(lambda c: bytes(c.b[:3]) + bytes(c.b[4:]) == want[:3] + want[4:],
                             "restauration")


class MchoseError(Exception):
    pass


def open_mouse(need_answer: bool = True) -> Mouse:
    """The first config node whose mouse answers. Returned unopened."""
    nodes = find_nodes()
    if not nodes:
        raise MchoseError("aucune souris MCHOSE trouvée (dongle branché ? règle udev ?)")
    errors = []
    for node in nodes:
        m = Mouse(node)
        try:
            with m:
                if m.read_status() or not need_answer:
                    return m
            errors.append(f"{node.path}: pas de réponse")
        except PermissionError:
            errors.append(f"{node.path}: accès refusé (règle udev manquante pour {node.vid:04x}:{node.pid:04x})")
        except OSError as e:
            errors.append(f"{node.path}: {e}")
    raise MchoseError("la souris ne répond pas (en veille ? bouge-la)\n  " + "\n  ".join(errors))


def battery() -> dict:
    """One poll, for the tray and for scripts: never raises."""
    try:
        nodes = find_nodes()
    except OSError as e:
        return {"ok": False, "error": str(e)}
    if not nodes:
        return {"ok": False, "error": "no-device"}
    for node in nodes:
        m = Mouse(node)
        try:
            with m:
                s = m.read_status(tries=4)
        except OSError as e:
            return {"ok": False, "error": str(e), "path": node.path}
        if s:
            return {"ok": True, "name": m.model.name, "battery": s.battery,
                    "charging": s.charging, "link": s.link, "firmware": s.firmware}
    return {"ok": False, "error": "asleep", "name": MODELS.get(nodes[0].pid, FALLBACK).name}


def measure_rate(node: Node, seconds: float = 3.0, wait: float = 15.0) -> float | None:
    """Passive: listen to the pointer's own reports and time them. Needs movement."""
    path = motion_node(node)
    if not path:
        raise MchoseError("interface de mouvement introuvable")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    stamps = []
    try:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.25)
            if not r:
                continue
            try:
                while True:
                    os.read(fd, 64)
                    stamps.append(time.monotonic())
            except BlockingIOError:
                pass
            if stamps and stamps[-1] - stamps[0] > seconds:
                break
    finally:
        os.close(fd)
    gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b - a < 0.02]
    if len(gaps) < 50:
        return None
    return 1 / statistics.median(gaps)


# ---------------------------------------------------------------------- CLI

def _on_off(v: str) -> bool:
    v = v.lower()
    if v in ("on", "1", "oui", "yes", "true"):
        return True
    if v in ("off", "0", "non", "no", "false"):
        return False
    raise argparse.ArgumentTypeError("on ou off")


def _lod_code(v: str) -> int:
    for code, mm in LOD_MM.items():
        if abs(float(v.replace(",", ".")) - mm) < 0.01:
            return code
    raise argparse.ArgumentTypeError("0.7, 1 ou 2")


def _mode(v: str) -> int:
    for k, name in MODES.items():
        if v.lower() in (name, str(k)):
            return k
    raise argparse.ArgumentTypeError("performance, gaming ou extreme")


def describe(m: Mouse, cfg: Config) -> dict:
    s = m.status
    w = s.wireless if s else True
    rates = m.rates()
    ri = cfg.rate_index(w)
    return {
        "model": m.model.name,
        "firmware": s.firmware if s else None,
        "link": s.link if s else None,
        "battery": s.battery if s else None,
        "charging": s.charging if s else None,
        "profile": cfg.b[0],
        "dpi": cfg.dpis,
        "dpi_y": cfg.dpis_y,
        "stages": cfg.stages,
        "active_stage": cfg.stage(w),
        "rate": rates[ri] if ri < len(rates) else None,
        "rates": list(rates),
        "rate_cable_index": cfg.rate_index(False),
        "lod_mm": LOD_MM[cfg.lod],
        "ripple": cfg.ripple,
        "angle_snap": cfg.angle_snap,
        "motion_sync": cfg.motion_sync,
        "mode": MODES[cfg.mode],
        "debounce_ms": cfg.debounce,
        "sleep_min": cfg.sleep,
        "dpi_max": m.model.dpi_max,
        "lods_mm": [LOD_MM[c] for c in m.model.lods],
    }


def _print_config(d: dict):
    on = lambda v: "oui" if v else "non"  # noqa: E731
    act = d["active_stage"]
    stages = "  ".join((f"[{v}]" if i == act else str(v)) if i < d["stages"] else f"({v})"
                       for i, v in enumerate(d["dpi"]))
    print(f"{d['model']}  ·  firmware {d['firmware']}  ·  {d['link']}")
    if d["battery"] is not None:
        print(f"Batterie        {d['battery']} %{' (en charge)' if d['charging'] else ''}")
    print(f"DPI             {stages}")
    print(f"                {d['stages']} palier(s) actif(s), palier courant {act + 1}"
          "   [] = courant, () = désactivé")
    print(f"Polling         {d['rate']} Hz   (choix : {', '.join(map(str, d['rates']))})")
    print(f"LOD             {d['lod_mm']:g} mm")
    print(f"Mode            {d['mode']}")
    print(f"Ripple control  {on(d['ripple'])}")
    print(f"Angle snapping  {on(d['angle_snap'])}")
    print(f"Motion sync     {on(d['motion_sync'])}")
    print(f"Debounce        {d['debounce_ms']} ms")
    print(f"Veille          {'jamais' if not d['sleep_min'] else str(d['sleep_min']) + ' min'}")


def main(argv=None):
    global _debug
    ap = argparse.ArgumentParser(prog="mchose", description="MCHOSE A7 V2 Ultra : batterie et réglages")
    ap.add_argument("--debug", action="store_true", help="affiche chaque trame envoyée et reçue")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("battery", help="niveau de batterie")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="toute la configuration")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("dpi", help="DPI du palier courant, ou tous les paliers")
    p.add_argument("value", nargs="?", type=int, help="nouvelle valeur du palier courant")
    p.add_argument("--stages", help="liste de 1 à 6 valeurs, ex. 400,800,1600")
    p.add_argument("--active", type=int, help="palier courant (1-6)")
    p = sub.add_parser("stage", help="changer de palier courant (1-6)")
    p.add_argument("n", type=int)
    p = sub.add_parser("rate", help="polling rate en Hz")
    p.add_argument("hz", type=int)
    p = sub.add_parser("lod", help="lift-off distance : 0.7, 1 ou 2 (mm)")
    p.add_argument("mm", type=_lod_code)
    p = sub.add_parser("mode", help="performance, gaming ou extreme")
    p.add_argument("mode", type=_mode)
    for name, help_ in (("ripple", "ripple control"), ("angle", "angle snapping"),
                        ("motion-sync", "motion sync")):
        p = sub.add_parser(name, help=f"{help_} on/off")
        p.add_argument("state", type=_on_off)
    p = sub.add_parser("debounce", help=f"anti-rebond des clics, 0-{DEBOUNCE_MAX} ms")
    p.add_argument("ms", type=int)
    p = sub.add_parser("sleep", help=f"mise en veille en minutes (1-{SLEEP_MAX}, 0 = jamais)")
    p.add_argument("minutes", type=int)
    p = sub.add_parser("backup", help="sauvegarder la configuration dans un fichier")
    p.add_argument("file", nargs="?")
    p = sub.add_parser("restore", help="restaurer une sauvegarde")
    p.add_argument("file")
    p = sub.add_parser("measure", help="mesurer le polling réel (bouge la souris)")
    p.add_argument("--seconds", type=float, default=3.0)
    a = ap.parse_args(argv)
    _debug = a.debug

    if a.cmd == "battery":
        b = battery()
        if a.json:
            print(json.dumps(b))
        elif b["ok"]:
            print(f"{b['name']} : {b['battery']} %{' (en charge)' if b['charging'] else ''}  [{b['link']}]")
        else:
            print({"asleep": "souris en veille (bouge-la)", "no-device": "aucune souris trouvée"}
                  .get(b["error"], b["error"]))
        return 0 if b["ok"] else 1

    try:
        m = open_mouse(need_answer=a.cmd != "measure")
        if a.cmd == "measure":
            print("Bouge la souris en continu...", flush=True)
            hz = measure_rate(m.node, a.seconds)
            print(f"~{hz:.0f} Hz mesurés" if hz else "pas assez de mouvement capté")
            return 0 if hz else 1
        with m:
            m.read_status()
            w = m.status.wireless if m.status else True
            if a.cmd == "show":
                d = describe(m, m.read_config())
                print(json.dumps(d)) if a.json else _print_config(d)
                return 0
            if a.cmd == "dpi":
                cfg = m.read_config()
                dpis, count, stage = cfg.dpis, cfg.stages, cfg.stage(w)
                if a.stages:
                    vals = [int(x) for x in a.stages.replace(" ", "").split(",") if x]
                    if not 1 <= len(vals) <= 6:
                        raise MchoseError("de 1 à 6 valeurs")
                    dpis = vals + dpis[len(vals):]
                    count = len(vals)
                    stage = min(stage, count - 1)
                if a.active is not None:
                    stage = a.active - 1
                if a.value is not None:
                    dpis[stage] = a.value
                if not (a.stages or a.value is not None or a.active is not None):
                    print(f"{dpis[stage]} DPI (palier {stage + 1}/{count})")
                    return 0
                cfg = m.set_dpi(dpis, stage, count)
            elif a.cmd == "stage":
                cfg = m.read_config()
                cfg = m.set_dpi(cfg.dpis, a.n - 1, max(cfg.stages, a.n))
            elif a.cmd == "rate":
                cfg = m.set_rate(a.hz)
            elif a.cmd == "lod":
                cfg = m.set_lod(a.mm)
            elif a.cmd == "mode":
                cfg = m.set_mode(a.mode)
            elif a.cmd == "ripple":
                cfg = m.set_flags(ripple=a.state)
            elif a.cmd == "angle":
                cfg = m.set_flags(angle=a.state)
            elif a.cmd == "motion-sync":
                cfg = m.set_flags(motion=a.state)
            elif a.cmd == "debounce":
                cfg = m.set_debounce(a.ms)
            elif a.cmd == "sleep":
                cfg = m.set_sleep(a.minutes)
            elif a.cmd == "backup":
                cfg = m.read_config()
                path = Path(a.file or STATE_DIR / time.strftime("config-%Y%m%d-%H%M%S.bin"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bytes(cfg.b))
                print(f"sauvegardé dans {path}")
                return 0
            elif a.cmd == "restore":
                cfg = m.restore(Path(a.file).read_bytes())
            _print_config(describe(m, cfg))
            return 0
    except MchoseError as e:
        print(f"erreur : {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
