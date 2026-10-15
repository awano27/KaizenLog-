"""Black-box smoke checks for an installed or frozen KaizenLog CLI.

This helper deliberately imports no product modules.  It creates an isolated
temporary profile and Vault, serves deterministic ActivityWatch-compatible data
on a loopback-only HTTP server, and records bounded typed evidence for each
child process.  It is intended for a released wheel/portable executable; the
preparation phase only syntax-checks this file.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import socketserver
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from math import isfinite
from typing import Any
from urllib.parse import unquote, urlsplit


DAY = "2026-10-14"
MAX_TAIL = 2048
COMMAND_TIMEOUT_SECONDS = 90
ALLOWED_BUCKETS = {
    "aw-watcher-window_fixture": "currentwindow",
    "aw-watcher-afk_fixture": "afkstatus",
    "aw-watcher-input_fixture": "os.hid.input",
}
WINDOW_EVENTS = [
    {
        "timestamp": "2026-10-14T09:00:00+09:00",
        "duration": 3600,
        "data": {"app": "Code.exe", "title": "KaizenLog fixture task"},
    }
]
AFK_EVENTS = [
    {
        "timestamp": "2026-10-14T09:00:00+09:00",
        "duration": 3600,
        "data": {"status": "not-afk"},
    }
]
INPUT_EVENTS = [
    {
        "timestamp": "2026-10-14T09:15:00+09:00",
        "duration": 60,
        "data": {"presses": 42, "clicks": 5, "deltaX": 3, "deltaY": 2},
    }
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tail(value: bytes) -> str:
    text = value.decode("utf-8", errors="replace")
    return text[-MAX_TAIL:]


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    """Serve only the ActivityWatch paths needed by the CLI smoke flow."""

    server: "_FixtureServer"

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        target = urlsplit(self.path)
        if target.scheme or target.netloc:
            self.server.unexpected_paths.append(self.path)
            self._send_json(400, {"error": "absolute proxy target is not allowed"})
            return
        path = target.path
        self.server.seen_paths.append(path)
        if path == "/api/0/buckets/":
            payload = {
                bucket_id: {"type": bucket_type, "last_updated": "2026-10-14T09:16:00+09:00"}
                for bucket_id, bucket_type in ALLOWED_BUCKETS.items()
            }
            self._send_json(200, payload)
            return
        match = re.fullmatch(r"/api/0/buckets/([^/]+)/events", path)
        if match:
            bucket_id = unquote(match.group(1))
            if bucket_id == "aw-watcher-window_fixture":
                self._send_json(200, WINDOW_EVENTS)
            elif bucket_id == "aw-watcher-afk_fixture":
                self._send_json(200, AFK_EVENTS)
            elif bucket_id == "aw-watcher-input_fixture":
                self._send_json(200, INPUT_EVENTS)
            else:
                self.server.unexpected_paths.append(path)
                self._send_json(404, {"error": "unknown fixture bucket"})
            return
        self.server.unexpected_paths.append(path)
        self._send_json(404, {"error": "fixture endpoint not allowed"})

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class _FixtureServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FixtureHandler)
        self.seen_paths: list[str] = []
        self.unexpected_paths: list[str] = []

    @property
    def base_url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"


def _write_config(path: Path, vault: Path, base_url: str) -> None:
    def quote(value: Path | str) -> str:
        return json.dumps(str(value).replace("\\", "/"))

    path.write_text(
        "\n".join(
            [
                "[general]",
                'timezone = "Asia/Tokyo"',
                f"vault_dir = {quote(vault)}",
                'daily_notes_dir = "01 Daily Notes"',
                'stats_dir = ".kaizenlog/stats"',
                'logs_dir = ".kaizenlog/logs"',
                'memory_dir = "Kaizen/Memory"',
                "auto_backfill_days = 0",
                "log_retention_days = 7",
                "",
                "[notifications]",
                "on_failure = false",
                "",
                "[activitywatch]",
                f"base_url = {quote(base_url)}",
                "",
                "[aiwork]",
                "enabled = false",
                "",
                "[screenpipe]",
                "enabled = false",
                "",
                "[effort]",
                "enabled = false",
                "",
                "[guard]",
                "enabled = false",
                "",
                "[nippou]",
                "auto_write = false",
                "",
                "[llm]",
                'backend = "none"',
                "fallback_to_local = false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _child_environment(root: Path, fixture_url: str) -> dict[str, str]:
    """Return an explicit safe environment; never copy the parent environment."""

    profile = root / "profile"
    roaming = profile / "AppData" / "Roaming"
    local = profile / "AppData" / "Local"
    for directory in (profile, roaming, local):
        directory.mkdir(parents=True, exist_ok=True)
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    system_path = str(Path(system_root) / "System32")
    fixture = urlsplit(fixture_url)
    fixture_host = fixture.hostname or "127.0.0.1"
    fixture_port = fixture.port
    if fixture_port is None:
        raise ValueError("fixture URL must include an explicit port")
    deny_proxy = "http://127.0.0.1:9"
    # Requests' bypass matcher is host-based here; a host:port NO_PROXY value
    # does not bypass the configured proxy for the loopback fixture.
    no_proxy = fixture_host
    return {
        "PATH": system_path,
        "SystemRoot": system_root,
        "TEMP": str(root / "temp"),
        "TMP": str(root / "temp"),
        "USERPROFILE": str(profile),
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(local),
        "HOME": str(profile),
        "PYTHONNOUSERSITE": "1",
        "HTTP_PROXY": deny_proxy,
        "HTTPS_PROXY": deny_proxy,
        "ALL_PROXY": deny_proxy,
        "NO_PROXY": no_proxy,
        "http_proxy": deny_proxy,
        "https_proxy": deny_proxy,
        "all_proxy": deny_proxy,
        "no_proxy": no_proxy,
    }


def _required_stdout(stdout: bytes, required: tuple[str, ...]) -> dict[str, bool]:
    """Check resource names against complete stdout before retaining only a tail."""

    return {name: name.encode("utf-8") in stdout for name in required}


def _run_child(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    required_stdout: tuple[str, ...] = (),
    capture_full_stdout: bool = False,
) -> dict[str, Any]:
    started = _utc_now()
    stdout = b""
    stderr = b""
    timed_out = False
    exit_code: int | None = None
    error: str | None = None
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        exit_code = int(completed.returncode)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        error = "timeout"
    except OSError as exc:
        error = type(exc).__name__
    record: dict[str, Any] = {
        "argv": list(argv),
        "cwd": str(cwd),
        "started_utc": started,
        "finished_utc": _utc_now(),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "status": "PASS" if exit_code == 0 and not timed_out and error is None else "FAIL",
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "stdout_sha256": _sha256_bytes(stdout),
        "stderr_sha256": _sha256_bytes(stderr),
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }
    if error is not None:
        record["error"] = error
    if required_stdout:
        record["stdout_required"] = _required_stdout(stdout, required_stdout)
        record["stdout_required_all"] = all(record["stdout_required"].values())
    if capture_full_stdout:
        # This is consumed immediately by the package validation path and is
        # removed before the report is serialized; bounded tails remain the
        # only command output evidence in the report.
        record["_stdout_full"] = stdout
    return record


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(float(value))


def _inspect_known_fixture_activity(stats_data: object) -> dict[str, bool]:
    """Validate the known synthetic ActivityWatch day without inventing AI metrics."""

    if not isinstance(stats_data, dict):
        return {
            "total_minutes": False,
            "blocks": False,
            "by_app": False,
            "input": False,
        }
    total = stats_data.get("total_minutes")
    blocks = stats_data.get("blocks")
    by_app = stats_data.get("by_app")
    input_stats = stats_data.get("input")
    block_ok = False
    if isinstance(blocks, list) and blocks:
        block_ok = True
        for block in blocks:
            if not isinstance(block, dict):
                block_ok = False
                break
            if not all(_finite_number(block.get(key)) for key in ("minutes",)):
                block_ok = False
                break
            try:
                start = datetime.fromisoformat(str(block.get("start")))
                end = datetime.fromisoformat(str(block.get("end")))
            except (TypeError, ValueError):
                block_ok = False
                break
            if end <= start or float(block["minutes"]) <= 0:
                block_ok = False
                break
        block_ok = block_ok and any(
            block.get("app") == "Code.exe"
            and block.get("title") == "KaizenLog fixture task"
            for block in blocks
            if isinstance(block, dict)
        )
    by_app_ok = (
        isinstance(by_app, dict)
        and _finite_number(by_app.get("Code.exe"))
        and float(by_app["Code.exe"]) > 0
    )
    input_ok = (
        isinstance(input_stats, dict)
        and input_stats.get("keypresses") == 42
        and input_stats.get("clicks") == 5
        and _finite_number(input_stats.get("active_input_minutes"))
        and float(input_stats["active_input_minutes"]) > 0
    )
    return {
        "total_minutes": _finite_number(total) and abs(float(total) - 60.0) <= 0.1,
        "blocks": block_ok,
        "by_app": by_app_ok,
        "input": input_ok,
    }


def _inspect_outputs(vault: Path) -> dict[str, Any]:
    notes = vault / "01 Daily Notes"
    target = notes / f"{DAY}.md"
    next_note = notes / "2026-10-15.md"
    stats = vault / ".kaizenlog" / "stats" / f"{DAY}.json"
    required = {"daily_note": target, "next_note": next_note, "stats": stats}
    missing = [name for name, path in required.items() if not path.is_file()]
    if missing:
        return {"status": "FAIL", "missing": missing}
    target_text = target.read_text(encoding="utf-8")
    next_text = next_note.read_text(encoding="utf-8")
    try:
        stats_data = json.loads(stats.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        stats_data = None
    markers = {
        "activity": "kaizenlog:activity" in target_text,
        "report": "kaizenlog:nippou" in target_text,
        "resume": "kaizenlog:resume" in next_text,
        "stats_json": isinstance(stats_data, dict) and stats_data.get("day") == DAY,
    }
    activity = _inspect_known_fixture_activity(stats_data)
    return {
        "status": "PASS" if all(markers.values()) and all(activity.values()) else "FAIL",
        "paths": {name: str(path) for name, path in required.items()},
        "markers": markers,
        "known_fixture_activity": activity,
        "sha256": {
            name: _sha256_bytes(path.read_bytes()) for name, path in required.items()
        },
        "sizes": {name: path.stat().st_size for name, path in required.items()},
    }


def _within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _package_python_path_checks(
    package_python: Path,
    imported_file: Path,
    prefix: Path,
    purelib: Path,
    checkout: Path,
) -> dict[str, bool]:
    venv_root = package_python.resolve().parent.parent
    return {
        "pyvenv_cfg": (venv_root / "pyvenv.cfg").is_file(),
        "venv_outside_checkout": not _within(venv_root, checkout),
        "prefix_matches_external_venv": prefix.resolve() == venv_root,
        "purelib_under_external_venv": _within(purelib, venv_root),
        "module_under_purelib": imported_file.is_file() and _within(imported_file, purelib),
        "module_outside_checkout": not _within(imported_file, checkout),
    }


def _check_package_python(package_python: Path, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    code = (
        "import json, sys, sysconfig, kaizenlog; "
        "print(json.dumps({'file': kaizenlog.__file__, 'prefix': sys.prefix, "
        "'purelib': sysconfig.get_paths()['purelib']}, sort_keys=True))"
    )
    result = _run_child([str(package_python), "-c", code], cwd, env, capture_full_stdout=True)
    raw_stdout = result.pop("_stdout_full", b"")
    payload: dict[str, Any] | None = None
    try:
        parsed = json.loads(raw_stdout.decode("utf-8").strip())
        if isinstance(parsed, dict):
            payload = parsed
    except (UnicodeError, ValueError):
        payload = None
    checkout = Path(__file__).resolve().parents[1]
    imported_path = Path(str(payload.get("file", ""))) if payload else Path()
    prefix = Path(str(payload.get("prefix", ""))) if payload else Path()
    purelib = Path(str(payload.get("purelib", ""))) if payload else Path()
    checks = _package_python_path_checks(package_python, imported_path, prefix, purelib, checkout) if payload else {
        "pyvenv_cfg": False,
        "venv_outside_checkout": False,
        "prefix_matches_external_venv": False,
        "purelib_under_external_venv": False,
        "module_under_purelib": False,
        "module_outside_checkout": False,
    }
    result.update(
        {
            "imported_file": str(imported_path) if payload else "",
            "python_prefix": str(prefix) if payload else "",
            "purelib": str(purelib) if payload else "",
            "path_checks": checks,
        }
    )
    if result["status"] == "PASS" and not payload:
        result["status"] = "FAIL"
        result["error"] = "package interpreter did not emit the required JSON record"
    elif result["status"] == "PASS" and not all(checks.values()):
        result["status"] = "FAIL"
        result["error"] = "package module or interpreter is not bound to the supplied external venv"
    return result


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(cli: Path, output: Path, package_python: Path | None = None) -> int:
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "FAIL",
        "cli": str(cli),
        "host_has_python": True,
        "pythonless_status": "NOT_RUN",
        "real_tk_status": "NOT_RUN",
        "commands": [],
        "package_python": None,
    }
    with tempfile.TemporaryDirectory(prefix="kaizenlog-install-smoke-") as temporary:
        root = Path(temporary)
        cwd = root / "external-cwd"
        vault = root / "vault"
        cwd.mkdir()
        vault.mkdir()
        (root / "temp").mkdir()
        server = _FixtureServer()
        env = _child_environment(root, server.base_url)
        server_thread = threading.Thread(target=server.serve_forever, name="kaizenlog-fixture-aw", daemon=True)
        server_thread.start()
        try:
            config = root / "fixture-config.toml"
            _write_config(config, vault, server.base_url)
            if package_python is not None:
                report["package_python"] = _check_package_python(package_python, cwd, env)
            commands = [
                [str(cli), "--help"],
                [str(cli), "--config", str(config), "skill", "show"],
                [str(cli), "--config", str(config), "generate", "--date", DAY, "--skip-catch-up"],
                [str(cli), "--config", str(config), "report", "--date", DAY, "--no-llm", "--write"],
            ]
            for index, argv in enumerate(commands):
                required = (
                    ("daily-kaizen", "weekly-kaizen", "kaizen-autopilot")
                    if index == 1
                    else ()
                )
                report["commands"].append(_run_child(argv, cwd, env, required_stdout=required))
            report["output_inspection"] = _inspect_outputs(vault)
            report["fixture"] = {
                "base_url": server.base_url,
                "seen_paths": list(server.seen_paths),
                "unexpected_paths": list(server.unexpected_paths),
                "proxy_policy": "deny except fixture host 127.0.0.1",
            }
            command_pass = all(item["status"] == "PASS" for item in report["commands"])
            package_pass = report["package_python"] is None or report["package_python"]["status"] == "PASS"
            bucket_seen = "/api/0/buckets/" in server.seen_paths
            event_seen = any(path.startswith("/api/0/buckets/") and path.endswith("/events") for path in server.seen_paths)
            fixture_pass = not server.unexpected_paths and bucket_seen and event_seen
            output_pass = report["output_inspection"]["status"] == "PASS"
            skill_pass = (
                bool(report["commands"][1].get("stdout_required_all"))
                if len(report["commands"]) > 1
                else False
            )
            report["checks"] = {
                "commands": command_pass,
                "package_python": package_pass,
                "fixture_endpoints": fixture_pass,
                "fixture_observed": bucket_seen and event_seen,
                "skill_resources": skill_pass,
                "output_artifacts": output_pass,
            }
            report["status"] = "PASS" if all(report["checks"].values()) else "FAIL"
        except Exception as exc:
            report["error"] = type(exc).__name__
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
    _write_report(output, report)
    return 0 if report["status"] == "PASS" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True, type=Path, help="absolute portable CLI executable path")
    parser.add_argument("--output", required=True, type=Path, help="JSON evidence report path")
    parser.add_argument("--package-python", type=Path, help="optional installed package interpreter")
    args = parser.parse_args(argv)
    if not args.cli.is_absolute() or not args.cli.is_file():
        parser.error("--cli must be an existing absolute executable path")
    if args.package_python is not None and (not args.package_python.is_absolute() or not args.package_python.is_file()):
        parser.error("--package-python must be an existing absolute interpreter path")
    return run(args.cli, args.output, args.package_python)


if __name__ == "__main__":
    raise SystemExit(main())
