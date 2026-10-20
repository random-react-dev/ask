#!/usr/bin/env python3
"""Idempotent per-user helper install. No sudo, broad process search, or global changes."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
from html import escape
import subprocess
import sys
import tempfile
import time

LABEL = "com.deviceagent.helper"
HERE = Path(__file__).resolve().parent


def plist_bytes(mapping):
    # The small fixed LaunchAgent schema needs no XML parser/native extension.
    def encode(value):
        if isinstance(value, bool):
            return "<true/>" if value else "<false/>"
        if isinstance(value, int):
            return f"<integer>{value}</integer>"
        if isinstance(value, str):
            return f"<string>{escape(value)}</string>"
        if isinstance(value, list):
            return "<array>" + "".join(encode(item) for item in value) + "</array>"
        return "<dict>" + "".join(f"<key>{escape(key)}</key>" + encode(value[key]) for key in sorted(value)) + "</dict>"
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">' + encode(mapping) + '</plist>\n').encode()


def atomic_write(path: Path, data: bytes, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".device-agent-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def launchctl(*args):
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=15)


def loaded(target):
    return sys.platform == "darwin" and launchctl("print", target).returncode == 0


def status(root, target):
    report = {"label": LABEL, "loaded": loaded(target), "helper_locked": False,
              "fresh": False, "status": None, "pid": None}
    try:
        with (root / "helper.lock").open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream, fcntl.LOCK_UN)
            except BlockingIOError:
                report["helper_locked"] = True
        data = json.loads((root / "helper-status.json").read_text())
        report.update(status=data.get("status"), pid=data.get("pid"))
        report["fresh"] = 0 <= time.time() - data["updated_at"] <= 75
    except (OSError, ValueError, KeyError, TypeError):
        pass
    report["healthy"] = report["loaded"] and report["helper_locked"] and report["fresh"]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--binary", type=Path, default=HERE / "target/release/device-helper")
    parser.add_argument("--home", type=Path, default=Path.home(), help="Alternate home is restricted to --stage-only")
    parser.add_argument("--stage-only", action="store_true", help="Write and validate; never load a job")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    if home != Path.home().resolve() and not args.stage_only:
        parser.error("alternate --home requires --stage-only")
    root = home / "Library/Application Support/DeviceAgent"
    plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    marker = root / ".helper-install.json"
    target = f"gui/{os.getuid()}/{LABEL}"
    if args.status:
        report = status(root, target)
        print(json.dumps(report, sort_keys=True) if args.json else
              f"{LABEL}: loaded={report['loaded']} locked={report['helper_locked']} fresh={report['fresh']} status={report['status']}")
        return 0 if report["healthy"] else 1
    if sys.platform != "darwin" and not args.stage_only:
        parser.error("live installation requires macOS")
    if root.is_symlink() or plist.is_symlink():
        parser.error("refusing symlink install destination")
    if (plist.exists() or (root / "bin/device-helper").exists()) and not marker.is_file():
        parser.error("existing helper paths have no ownership marker; refusing to replace")
    if args.uninstall:
        if not marker.is_file():
            print("No managed helper installation found")
            return 0
        if not args.stage_only and loaded(target):
            result = launchctl("bootout", target)
            if result.returncode:
                raise RuntimeError("could not stop the managed helper")
        plist.unlink(missing_ok=True)
        (root / "bin/device-helper").unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
        print("Helper removed; runtime, credentials, logs and task data retained")
        return 0
    if args.config is None:
        parser.error("--config is required for installation")
    source = args.config.expanduser().resolve()
    if source.stat().st_mode & 0o077:
        parser.error("input config must be private (chmod 600)")
    config = json.loads(source.read_text())
    if config.get("allow_loopback_http") and not args.stage_only:
        parser.error("loopback HTTP is limited to staged integration tests")
    configured_state = Path(config.get("state_dir", "")).expanduser().resolve()
    if configured_state != root:
        parser.error(f"state_dir must be the managed support directory: {root}")
    command = config.get("task_agent_command", [])
    if not command or not Path(command[0]).is_absolute() or not Path(command[0]).is_file():
        parser.error("task_agent_command executable must exist at an absolute path")
    binary = args.binary.expanduser().resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error("build the helper release binary first")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    (root / "bin").mkdir(exist_ok=True, mode=0o700)
    installed_binary = root / "bin/device-helper"
    installed_config = root / "config.json"
    config_data = (json.dumps(config, indent=2) + "\n").encode()
    definition = {
        "Label": LABEL,
        "ProgramArguments": [str(installed_binary), "--config", str(installed_config)],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ExitTimeOut": 15,
        "ProcessType": "Background",
        "Umask": 63,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        # Task agents have independent connections and separate process groups.
        "AbandonProcessGroup": True,
    }
    plist_data = plist_bytes(definition)
    changed = any(not path.exists() or path.read_bytes() != data for path, data in
                  [(installed_binary, binary.read_bytes()), (installed_config, config_data), (plist, plist_data)])
    # Check source configuration before stopping any already-running helper.
    subprocess.run([str(binary), "--config", str(source), "--check"], check=True,
                   capture_output=True, timeout=10)
    if changed and not args.stage_only and loaded(target):
        if launchctl("bootout", target).returncode:
            raise RuntimeError("could not stop the managed helper for update")
    atomic_write(installed_binary, binary.read_bytes(), 0o700)
    atomic_write(installed_config, config_data)
    atomic_write(plist, plist_data)
    atomic_write(marker, json.dumps({"label": LABEL, "version": 1}).encode())
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/plutil", "-lint", str(plist)], check=True, capture_output=True, timeout=10)
    if args.stage_only:
        print(json.dumps({"staged": True, "started": False, "root": str(root), "plist": str(plist)}))
        return 0
    if not loaded(target):
        launchctl("enable", target)
        result = launchctl("bootstrap", f"gui/{os.getuid()}", str(plist))
        if result.returncode:
            raise RuntimeError("launchctl bootstrap failed; inspect the managed plist and user GUI session")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        report = status(root, target)
        if report["healthy"]:
            print(json.dumps({"installed": True, **report}, sort_keys=True))
            return 0
        time.sleep(0.5)
    raise RuntimeError("helper did not publish a fresh status within 45 seconds")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
        # Never print configuration, bearer token, subprocess stderr or server response bodies.
        print(f"Helper installation failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
