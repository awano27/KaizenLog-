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
) -> bool | None:
    """トースト風バルーン通知を出す。

    戻り値:
      True  — Windows で送出成功
      False — Windows で送出を試みたが失敗（notify_failed 記録対象）
      None  — 非 Windows で送出を試みていない（失敗ではない。記録しない）

    icon: SystemIcons 名（Warning / Information / Error 等）
    既存の失敗通知は icon=Warning のまま。
    例外は外へ漏らさない（通知失敗で本処理を落とさない）。
    """
    if sys.platform != "win32":
        # 送出未試行（旧仕様の no-op）。False だと CI/WSL で偽陽性の notify_failed になる
        return None
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
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=30,
        )
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
