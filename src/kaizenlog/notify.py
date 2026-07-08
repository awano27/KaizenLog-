"""Windows通知（失敗時アラート用）。

追加モジュール不要で動くよう、PowerShell経由のバルーン通知を使う。
Windows以外ではno-op。通知の失敗は本体の動作に影響させない。
"""

from __future__ import annotations

import subprocess
import sys


def notify(title: str, message: str) -> bool:
    if sys.platform != "win32":
        return False
    # PowerShellのシングルクォート文字列としてエスケープ
    t = title.replace("'", "''")[:80]
    m = message.replace("'", "''")[:200]
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Warning; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(10000, '{t}', '{m}', "
        "[System.Windows.Forms.ToolTipIcon]::Warning); "
        "Start-Sleep -Seconds 6; $n.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=30,
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False
