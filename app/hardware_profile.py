"""What kind of machine is this, and how hard should the room push it?

The appliance was written for a Raspberry Pi, and every default in it assumes
one. That is the wrong assumption on a mini-PC or a NUC, where the room ends up
waiting eight seconds for a meeting page that drew in one, and Chromium leaves
the GPU idle because the Pi-safe flags never asked for it.

So the machine is measured once at startup and sorted into one of three
profiles:

``high``
    A real computer: not a Pi, four or more cores, 8 GB or more. Chromium is
    told to use the GPU properly, and the join automation stops padding its
    timings for hardware that does not need it.
``balanced``
    A Pi 4 or Pi 5, or a modest x86 box. This is what the shipped defaults
    have always meant.
``low``
    A Pi 3, or anything under 2 GB. Everything is given more time; see
    ``roomctl slow-device``.

Two rules keep this from becoming a source of mysteries:

* **It never writes to the configuration.** A profile supplies defaults for
  values the administrator has left alone; an explicit setting always wins. If
  someone typed 30 into "wait before pressing anything", they get 30.
* **Detection failing is not an error.** Anything unreadable means
  ``balanced``, which is what the appliance did before this file existed.
"""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass
from functools import lru_cache

#: The profiles an administrator can choose between, plus ``auto``.
PROFILES: tuple[str, ...] = ("auto", "high", "balanced", "low")

HIGH = "high"
BALANCED = "balanced"
LOW = "low"


@dataclass(frozen=True)
class Machine:
    """What we could work out about the hardware."""

    cores: int = 0
    memory_gb: float = 0.0
    model: str = ""
    architecture: str = ""
    is_raspberry_pi: bool = False
    pi_generation: int = 0

    def describe(self) -> str:
        """One line for a status page or a log."""
        parts = []
        if self.model:
            parts.append(self.model)
        elif self.architecture:
            parts.append(self.architecture)
        if self.cores:
            parts.append(f"{self.cores} cores")
        if self.memory_gb:
            parts.append(f"{self.memory_gb:.1f} GB")
        return " · ".join(parts) or "unknown hardware"

    def to_dict(self) -> dict[str, object]:
        return {
            "cores": self.cores,
            "memory_gb": round(self.memory_gb, 1),
            "model": self.model,
            "architecture": self.architecture,
            "raspberry_pi": self.is_raspberry_pi,
            "pi_generation": self.pi_generation,
            "description": self.describe(),
        }


@dataclass(frozen=True)
class Tuning:
    """The settings that follow from a profile."""

    profile: str
    #: Multiplies the per-provider "let the page settle" wait.
    settle_multiplier: float
    #: Default for AUTO_JOIN_TIMEOUT_SECONDS when it has not been set.
    join_timeout_seconds: int
    #: How often the dashboard asks the backend for state, in milliseconds.
    poll_ms: int
    #: Appended to Chromium's command line by scripts/start-kiosk.sh.
    chromium_args: tuple[str, ...]
    #: Appended to Chromium's single --enable-features list.
    enable_features: tuple[str, ...]
    #: Appended to Chromium's single --disable-features list.
    disable_features: tuple[str, ...]
    summary: str


TUNINGS: dict[str, Tuning] = {
    HIGH: Tuning(
        profile=HIGH,
        # A page that draws in a second does not need an eight-second wait
        # before anything is pressed; on this hardware that is just eight
        # seconds of a meeting nobody is in yet.
        settle_multiplier=0.4,
        join_timeout_seconds=60,
        poll_ms=3500,
        chromium_args=(
            # The Pi-safe defaults leave a desktop GPU idle. These are the
            # flags that make a real machine behave like one — and Chromium
            # ignores any it does not recognise, so an older build is safe.
            "--ignore-gpu-blocklist",
            "--enable-gpu-rasterization",
            "--enable-zero-copy",
            # A kiosk window is never "in the background"; letting Chromium
            # throttle it slows the very call it is in.
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ),
        enable_features=(
            "VaapiVideoDecoder",
            "VaapiVideoDecodeLinuxGL",
            "CanvasOopRasterization",
            "AcceleratedVideoDecodeLinuxGL",
        ),
        disable_features=(),
        summary="a proper computer: GPU rasterisation and video decode on, "
        "join timings unpadded",
    ),
    BALANCED: Tuning(
        profile=BALANCED,
        settle_multiplier=1.0,
        join_timeout_seconds=90,
        poll_ms=5000,
        chromium_args=(),
        enable_features=(),
        disable_features=(),
        summary="a Raspberry Pi 4 or 5, or a modest PC: the shipped defaults",
    ),
    LOW: Tuning(
        profile=LOW,
        # A Pi 3 can still be drawing a meeting page half a minute in.
        settle_multiplier=4.0,
        join_timeout_seconds=300,
        poll_ms=8000,
        chromium_args=(
            "--renderer-process-limit=2",
            "--disable-smooth-scrolling",
            "--disable-accelerated-2d-canvas",
        ),
        enable_features=(),
        disable_features=("BackForwardCache",),
        summary="a Pi 3 or similar: everything given far more time",
    ),
}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _read_first_line(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(256).replace("\x00", "").strip()
    except OSError:
        return ""


def _total_memory_gb() -> float:
    """Physical RAM in GB, or 0.0 when it cannot be read."""
    try:
        with open("/proc/meminfo", "r", encoding="ascii", errors="ignore") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    kilobytes = int(re.sub(r"[^0-9]", "", line) or 0)
                    return kilobytes / (1024 * 1024)
    except (OSError, ValueError):
        pass
    # Not Linux, or a container without /proc: ask Python.
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return (pages * page_size) / (1024 ** 3)
    except (AttributeError, ValueError, OSError):
        pass
    return 0.0


def _hardware_model() -> str:
    """The board or product name, as the machine reports it."""
    # A Raspberry Pi says so in the device tree.
    for path in (
        "/proc/device-tree/model",
        "/sys/firmware/devicetree/base/model",
    ):
        model = _read_first_line(path)
        if model:
            return model
    # An x86 box says so in DMI.
    product = _read_first_line("/sys/devices/virtual/dmi/id/product_name")
    vendor = _read_first_line("/sys/devices/virtual/dmi/id/sys_vendor")
    if product and vendor and vendor.lower() not in product.lower():
        return f"{vendor} {product}"
    return product or ""


def _pi_generation(model: str) -> int:
    """3, 4, 5 … for a Raspberry Pi, else 0."""
    match = re.search(r"raspberry pi\s+(\d+)", model, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return 0
    # "Raspberry Pi Model B" — the very old ones carry no number.
    if "raspberry pi" in model.lower():
        return 1
    return 0


def detect_machine() -> Machine:
    """Measure the hardware. Never raises; unknowns come back as zeroes."""
    model = _hardware_model()
    generation = _pi_generation(model)
    return Machine(
        cores=os.cpu_count() or 0,
        memory_gb=_total_memory_gb(),
        model=model,
        architecture=platform.machine() or "",
        is_raspberry_pi=generation > 0,
        pi_generation=generation,
    )


def classify(machine: Machine) -> str:
    """Which profile suits this machine."""
    memory = machine.memory_gb
    cores = machine.cores

    # Genuinely small hardware, whatever it calls itself.
    if 0 < memory < 2.0:
        return LOW
    if machine.is_raspberry_pi and machine.pi_generation <= 3:
        return LOW

    # A proper computer: not a Pi, several cores, and memory to match. The
    # thresholds are deliberately unambitious — the point is to catch the
    # obvious case (a NUC, a mini-PC, a spare desktop) rather than to grade
    # hardware finely.
    if not machine.is_raspberry_pi and cores >= 4 and memory >= 7.0:
        return HIGH

    return BALANCED


@lru_cache(maxsize=1)
def _cached_machine() -> Machine:
    """The hardware does not change while the process runs."""
    return detect_machine()


def resolve(configured: str = "auto") -> tuple[str, Machine]:
    """``(profile, machine)`` for a configured value of ``auto`` or a name."""
    machine = _cached_machine()
    wanted = (configured or "auto").strip().lower()
    if wanted in TUNINGS:
        return wanted, machine
    return classify(machine), machine


def tuning_for(configured: str = "auto") -> Tuning:
    profile, _machine = resolve(configured)
    return TUNINGS.get(profile, TUNINGS[BALANCED])


def report(configured: str = "auto") -> dict[str, object]:
    """Everything the health page and the scripts want to know."""
    profile, machine = resolve(configured)
    tuning = TUNINGS.get(profile, TUNINGS[BALANCED])
    return {
        "configured": (configured or "auto").strip().lower() or "auto",
        "profile": profile,
        "automatic": (configured or "auto").strip().lower() in ("", "auto"),
        "summary": tuning.summary,
        "machine": machine.to_dict(),
        "poll_ms": tuning.poll_ms,
    }
