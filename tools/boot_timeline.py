"""Measure how long a Brother QL takes from mains-on to genuinely print-ready.

Four signals, watched in parallel, because they do not arrive together and the
gate has to wait for the right one:

  ping        the network stack answers at all
  tcp/9100    the raw print port accepts a connection (a bound socket, which is
              not the same as a print engine)
  tcp/631     the IPP service accepts a connection
  ipp state   the printer answers Get-Printer-Attributes and says what it is

For each it records the first sighting and, more usefully, the point from which
it stayed true. A signal that flickers on and off during boot is exactly what a
gate must not act on.

Read-only throughout: nothing is printed, no webhook is sent, no setting is
changed.

It counts down before t=0 so the printer can be switched on at exactly the
right moment; without that the one number that decides how long the gate should
wait before looking is the one that goes missing.

Usage, from the repository root, using the application image so the app's own
dependencies are present:

    docker run --rm -v "$PWD":/app:ro -e PYTHONPATH=/app \
        brother_ql_app:local python /app/tools/boot_timeline.py

There is no ping binary in that image, so the ICMP row is reported as
unavailable. The three signals the gate actually uses are unaffected.
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

# Import the app's own status check, so what is measured is exactly what the
# print gate will see rather than a second opinion about the same printer.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# How long a signal has to hold before it counts as settled. A printer that
# answers once mid-boot and then drops is not ready, and this is the number the
# gate's own settle period exists to cover.
STABLE_FOR_SECONDS = 10.0

TCP_TIMEOUT = 0.4       # a live host on the LAN answers in milliseconds
POLL_SECONDS = 0.5      # cheap probes
IPP_EVERY_SECONDS = 2.0  # the expensive one


def tcp_open(host: str, port: int) -> bool:
    """True when a TCP connection to host:port completes."""
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            return True
    except OSError:
        return False


PING = shutil.which("ping")


def pingable(host: str) -> bool:
    """True when the host answers a single ICMP echo within a second.

    Always False where there is no ping binary, which is the case inside the
    slim application image. The summary says so rather than reporting a signal
    that was never looked for as one that never arrived.
    """
    if not PING:
        return False
    try:
        return subprocess.run(
            [PING, "-c", "1", "-W", "800", host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2
        ).returncode == 0
    except Exception:
        return False


def ipp_state(host: str, model: str) -> Optional[str]:
    """The printer's own account of itself, or None when it did not answer."""
    try:
        from src.services.printer_service import printer_service
        status = printer_service.check_printer_status(f"tcp://{host}", model)
        if not status.get("reachable"):
            return None
        return status.get("state") or "unknown"
    except Exception:
        return None


class Signal:
    """One observable, with its first and its settled sighting."""

    def __init__(self, name: str):
        self.name = name
        self.first: Optional[float] = None
        self.holding_since: Optional[float] = None
        self.settled: Optional[float] = None
        self.flickers = 0

    def observe(self, ok: bool, t: float) -> Optional[str]:
        """Record one reading; return a line to print when something changed."""
        if ok:
            if self.first is None:
                self.first = t
                self.holding_since = t
                return f"{self.name}: first answer"
            if self.holding_since is None:
                self.holding_since = t
                return f"{self.name}: back after dropping out"
            if self.settled is None and t - self.holding_since >= STABLE_FOR_SECONDS:
                self.settled = t
                return f"{self.name}: steady for {STABLE_FOR_SECONDS:.0f}s"
        else:
            if self.holding_since is not None:
                self.holding_since = None
                self.settled = None
                self.flickers += 1
                return f"{self.name}: dropped out"
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.100")
    ap.add_argument("--model", default="QL-820NWB")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--countdown", type=int, default=5,
                    help="seconds to count down before t=0; 0 to start at once")
    args = ap.parse_args()

    # Count down so the printer is switched on exactly at t=0. Without this the
    # measurement starts at some unknown point after mains-on, and the one
    # number that decides how long to wait blind is the one that goes missing.
    for remaining in range(args.countdown, 0, -1):
        print(f"  switch on in {remaining}...", flush=True)
        time.sleep(1.0)
    if args.countdown:
        print("  SWITCH ON NOW\n", flush=True)

    signals = {
        "ping": Signal("ping"),
        "tcp9100": Signal("tcp/9100"),
        "tcp631": Signal("tcp/631"),
        "ipp": Signal("ipp ready"),
    }
    start = time.time()
    deadline = start + args.minutes * 60
    next_ipp = 0.0
    last_state = None

    print(f"Watching {args.host} from t=0 (mains-on). Ctrl-C to stop early.\n")
    print(f"{'t':>7}  event")
    print(f"{'-'*7}  {'-'*50}")

    try:
        while time.time() < deadline:
            t = time.time() - start
            events = []

            events.append(signals["ping"].observe(pingable(args.host), t))
            events.append(signals["tcp9100"].observe(tcp_open(args.host, 9100), t))
            events.append(signals["tcp631"].observe(tcp_open(args.host, 631), t))

            if t >= next_ipp:
                state = ipp_state(args.host, args.model)
                next_ipp = (time.time() - start) + IPP_EVERY_SECONDS
                if state != last_state:
                    if state is not None:
                        events.append(f"ipp says: {state}")
                    last_state = state
                events.append(signals["ipp"].observe(state == "ready", t))

            for e in events:
                if e:
                    print(f"{t:7.1f}  {e}")

            watched = [s for s in signals.values()
                       if PING or s.name != "ping"]
            if all(s.settled is not None for s in watched):
                print(f"\nEverything steady at t={t:.1f}s.")
                break

            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nstopped")

    print("\nSummary")
    print(f"  {'signal':<12} {'first':>8} {'settled':>9} {'drop-outs':>10}")
    for s in signals.values():
        if s.name == "ping" and not PING:
            print(f"  {s.name:<12} {'n/a':>8} {'n/a':>9} {'-':>10}  (no ping here)")
            continue
        first = f"{s.first:.1f}s" if s.first is not None else "never"
        settled = f"{s.settled:.1f}s" if s.settled is not None else "never"
        print(f"  {s.name:<12} {first:>8} {settled:>9} {s.flickers:>10}")

    ipp = signals["ipp"]
    tcp = signals["tcp9100"]
    if ipp.first is not None and tcp.first is not None:
        print(f"\n  tcp/9100 answered {ipp.first - tcp.first:.1f}s before the printer "
              f"called itself ready.")
    if ipp.settled is not None:
        print(f"  A gate waiting for a steady 'ready' could have released at "
              f"{ipp.settled:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
