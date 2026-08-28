"""Shell-out helpers for the bits of the OS the appliance needs to touch.

All external commands go through here so that:

* every call has a timeout (a hung ``systemctl`` must not hang the dashboard),
* nothing is executed through a shell, so no quoting or injection surprises,
* development mode can stub the whole lot out, and
* the exact list of privileged commands is visible in one place and matched by
  the sudoers rule that ``scripts/install.sh`` installs.

**Why systemd *user* services.** Chromium needs the graphical session, PipeWire
runs per-user, and UxPlay needs both. Running the appliance as user units of the
desktop user means all three simply work, and — usefully — restarting them needs
no privileges at all. Only rebooting the Pi does, so that is the single sudoers
entry the installer adds.

Set ``ROOM_APPLIANCE_SYSTEMD_SCOPE=system`` to manage system units instead (for
an unusual install, or a headless test).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

from .config import ConfigManager
from .logging_setup import get_logger, log_event

log = get_logger("system")

#: Units this appliance is allowed to control.
MANAGED_UNITS = (
    "room-dashboard.service",
    "room-kiosk.service",
    "room-airplay.service",
    "room-remote.service",
    "room-watchdog.timer",
    "room-watchdog.service",
)


@dataclass
class CommandResult:
    """Outcome of one external command."""

    ok: bool
    code: int
    stdout: str
    stderr: str
    command: str = ""

    @property
    def output(self) -> str:
        return (self.stdout or self.stderr).strip()


def which(binary: str) -> str:
    return shutil.which(binary) or ""


def run(
    args: list[str],
    *,
    timeout: float = 15.0,
    check_binary: bool = True,
    input_text: str | None = None,
) -> CommandResult:
    """Run a command without a shell. Never raises."""
    if not args:
        return CommandResult(False, -1, "", "no command given")
    printable = " ".join(args)

    if check_binary and not (os.path.sep in args[0] or which(args[0])):
        return CommandResult(False, 127, "", f"{args[0]} is not installed", printable)

    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log_event(log, logging.WARNING, "system.command_timeout", command=printable)
        return CommandResult(False, -1, "", f"timed out after {timeout:g}s", printable)
    except (OSError, ValueError) as exc:
        return CommandResult(False, -1, "", str(exc), printable)

    return CommandResult(
        completed.returncode == 0,
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
        printable,
    )


class SystemService:
    """Service restarts, reboots and log reading."""

    def __init__(self, config: ConfigManager) -> None:
        self.config = config
        # None means "never attempted". A zero would be wrong: the monotonic
        # clock starts near boot, so on a freshly started Pi `now - 0` is small
        # and every rate limit would refuse its own first call — exactly when
        # the watchdog most needs to act.
        self._last_restart: dict[str, float] = {}
        self._last_reboot_attempt: float | None = None

    # -- capability probing ---------------------------------------------
    @property
    def dev_mode(self) -> bool:
        return self.config.bool_("DEV_MODE")

    @property
    def has_systemd(self) -> bool:
        if not which("systemctl") or not os.path.isdir("/run/systemd/system"):
            return False
        if self.user_scope and not os.environ.get("XDG_RUNTIME_DIR"):
            # Without a user session bus, `systemctl --user` cannot work.
            return False
        return True

    @property
    def user_scope(self) -> bool:
        """True when the appliance runs as systemd *user* units (the default)."""
        return os.environ.get("ROOM_APPLIANCE_SYSTEMD_SCOPE", "user").lower() != "system"

    def _systemctl(self, *args: str) -> list[str]:
        """Build a systemctl command in the right scope.

        User units need no privileges; system units need sudo unless we are root.
        """
        if self.user_scope:
            return ["systemctl", "--user", *args]
        if os.geteuid() == 0:
            return ["systemctl", *args]
        sudo = which("sudo")
        return [sudo, "-n", "systemctl", *args] if sudo else ["systemctl", *args]

    def _privileged(self, args: list[str]) -> list[str]:
        """Prefix with sudo unless already root (used for reboot only)."""
        if os.geteuid() == 0:
            return args
        sudo = which("sudo")
        return [sudo, "-n", *args] if sudo else args

    # -- systemd --------------------------------------------------------
    def is_active(self, unit: str) -> bool:
        if self.dev_mode or not self.has_systemd:
            return False
        result = run(self._systemctl("is-active", "--quiet", unit), timeout=8)
        return result.code == 0

    def unit_state(self, unit: str) -> str:
        """``active`` / ``inactive`` / ``failed`` / ``unknown``."""
        if self.dev_mode or not self.has_systemd:
            return "unknown"
        result = run(self._systemctl("is-active", unit), timeout=8)
        state = (result.stdout or "").strip()
        return state or "unknown"

    def restart(self, unit: str, *, min_interval: float = 20.0, reason: str = "") -> bool:
        """Restart a managed unit, rate-limited to avoid thrashing."""
        if unit not in MANAGED_UNITS:
            log_event(log, logging.ERROR, "system.restart_refused", unit=unit)
            return False

        now = time.monotonic()
        last = self._last_restart.get(unit)
        if last is not None and now - last < min_interval:
            log_event(
                log, logging.DEBUG, "system.restart_throttled",
                unit=unit, seconds_ago=round(now - last, 1),
            )
            return False
        self._last_restart[unit] = now

        if self.dev_mode:
            log_event(log, logging.INFO, "system.restart_simulated", unit=unit, reason=reason)
            return True
        if not self.has_systemd:
            log_event(log, logging.WARNING, "system.no_systemd", unit=unit)
            return False

        log_event(log, logging.WARNING, "system.restarting_unit", unit=unit, reason=reason)
        result = run(self._systemctl("restart", unit), timeout=45)
        if not result.ok:
            log_event(
                log, logging.ERROR, "system.restart_failed",
                unit=unit, error=result.output[:200],
            )
        return result.ok

    def start(self, unit: str) -> bool:
        if unit not in MANAGED_UNITS or self.dev_mode or not self.has_systemd:
            return False
        return run(self._systemctl("start", unit), timeout=45).ok

    def stop(self, unit: str) -> bool:
        if unit not in MANAGED_UNITS or self.dev_mode or not self.has_systemd:
            return False
        return run(self._systemctl("stop", unit), timeout=45).ok

    def enable(self, unit: str, *, enabled: bool = True) -> bool:
        if unit not in MANAGED_UNITS or self.dev_mode or not self.has_systemd:
            return False
        verb = "enable" if enabled else "disable"
        return run(self._systemctl(verb, unit), timeout=30).ok

    def reboot(self, *, reason: str = "", min_interval: float = 3600.0) -> bool:
        """Reboot the Pi, rate-limited so a fault cannot cause a reboot loop."""
        now = time.monotonic()
        last = self._last_reboot_attempt
        if last is not None and now - last < min_interval:
            log_event(
                log, logging.WARNING, "system.reboot_throttled",
                reason=reason, seconds_ago=round(now - last),
            )
            return False
        self._last_reboot_attempt = now

        if self.dev_mode:
            log_event(log, logging.WARNING, "system.reboot_simulated", reason=reason)
            return True
        log_event(log, logging.CRITICAL, "system.rebooting", reason=reason)
        # Give the journal a moment to flush before the kernel goes down.
        time.sleep(1.0)
        return run(self._privileged(["systemctl", "reboot"]), timeout=30).ok

    # -- logs -----------------------------------------------------------
    def journal(self, unit: str = "", lines: int = 200) -> str:
        """Recent log lines for the diagnostics page."""
        lines = max(10, min(2000, int(lines)))
        if not which("journalctl"):
            return "journalctl is not available on this system."
        args = ["journalctl", "--no-pager", "-n", str(lines)]
        if self.user_scope:
            args.append("--user")
        if unit:
            if unit not in MANAGED_UNITS:
                return "Unknown unit."
            args += ["-u", unit]
        else:
            for managed in MANAGED_UNITS:
                args += ["-u", managed]
        result = run(args, timeout=20)
        return result.output or "No log entries."

    # -- host facts -----------------------------------------------------
    def uptime_seconds(self) -> float:
        try:
            with open("/proc/uptime", "r", encoding="ascii") as handle:
                return float(handle.read().split()[0])
        except (OSError, ValueError, IndexError):
            return 0.0

    def load_average(self) -> tuple[float, float, float]:
        try:
            return os.getloadavg()
        except OSError:
            return (0.0, 0.0, 0.0)

    def temperature_celsius(self) -> float | None:
        """SoC temperature, used to flag a Pi that is overheating."""
        for path in (
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/devices/virtual/thermal/thermal_zone0/temp",
        ):
            try:
                with open(path, "r", encoding="ascii") as handle:
                    raw = float(handle.read().strip())
                return round(raw / 1000.0, 1) if raw > 1000 else round(raw, 1)
            except (OSError, ValueError):
                continue
        return None

    def disk_free_percent(self, path: str = "/") -> float | None:
        try:
            usage = shutil.disk_usage(path)
            return round(100.0 * usage.free / usage.total, 1) if usage.total else None
        except OSError:
            return None

    def memory_available_mb(self) -> float | None:
        try:
            with open("/proc/meminfo", "r", encoding="ascii") as handle:
                for line in handle:
                    if line.startswith("MemAvailable:"):
                        return round(float(line.split()[1]) / 1024.0, 1)
        except (OSError, ValueError, IndexError):
            pass
        return None

    def local_ip_addresses(self) -> list[str]:
        """LAN addresses, so the dashboard can show the control-panel URL."""
        found: list[str] = []
        result = run(["ip", "-4", "-o", "addr", "show", "scope", "global"], timeout=6)
        if result.ok:
            for line in result.stdout.splitlines():
                parts = line.split()
                for index, token in enumerate(parts):
                    if token == "inet" and index + 1 < len(parts):
                        address = parts[index + 1].split("/")[0]
                        if address and address not in found:
                            found.append(address)
        if not found:
            # Fall back to asking the kernel which source address it would use.
            import socket

            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(1.0)
                    sock.connect(("1.1.1.1", 80))
                    found.append(sock.getsockname()[0])
            except OSError:
                pass
        return found

    def hostname(self) -> str:
        import socket

        try:
            return socket.gethostname()
        except OSError:
            return "raspberrypi"
