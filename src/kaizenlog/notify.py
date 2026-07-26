"""Windows通知。

追加モジュール不要で動くよう、PowerShell経由のバルーン通知を使う。
Windows以外ではno-op。通知の失敗は本体の動作に影響させない。
"""

from __future__ import annotations

import subprocess
import sys


def notify(
    title: str,
    message: str,
    *,
    icon: str = "Warning",
    duration_ms: int = 10000,
) -> bool:
    """トースト風バルーン通知を出す。

    icon: SystemIcons 名（Warning / Information / Error 等）
    既存の失敗通知は icon=Warning のまま。
    """
    if sys.platform != "win32":
        return False
    # PowerShellのシングルクォート文字列としてエスケープ
    t = title.replace("'", "''")[:80]
    m = message.replace("'", "''")[:200]
    # 許可アイコン以外は Warning にフォールバック
    icon_name = icon if icon in ("Warning", "Information", "Error", "Question") else "Warning"
    tip = {
        "Warning": "Warning",
        "Information": "Info",
        "Error": "Error",
        "Question": "None",
    }.get(icon_name, "Warning")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        f"$n.Icon = [System.Drawing.SystemIcons]::{icon_name}; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip({int(duration_ms)}, '{t}', '{m}', "
        f"[System.Windows.Forms.ToolTipIcon]::{tip}); "
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
