"""空転ブレーカー: Claude Code フックでドゥームループをセッション中に検知する。

重要契約:
- モジュールトップでは cli / aiwork / promptledger 等を import しない
- あらゆる内部エラーでも exit 0（ユーザーセッションを壊さない）
- transcript への書き込み禁止
- UserPromptSubmit / Stop のみ想定（PostToolUse には登録しない）

live_episodes は通知履歴のみ。トークン会計は夜間ループ税が正（二重計上しない）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# --- stdlib のみ（モジュールトップ） ---

# detect_retry_chains と同値（重いモジュールを import せず定数を持つ）
_RETRY_SIMILARITY = 0.85
_RETRY_WINDOW_MINUTES = 30

_DEFAULT_DEBOUNCE = 30
_DEFAULT_COOLDOWN = 300
_DEFAULT_RETRY_THRESHOLD = 3
_DEFAULT_TOOL_ERROR_STREAK = 3
_MAX_RECENT_PROMPTS = 10

LIVE_EPISODES_FILE = "live_episodes.jsonl"


def state_dir() -> Path:
    """%LOCALAPPDATA%/kaizenlog/guard/ （非 Windows は ~/.local/share 相当）。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local"
        )
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(
            Path.home() / ".local" / "share"
        )
    return Path(base) / "kaizenlog" / "guard"


def state_path(session_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (session_id or "unknown"))
    return state_dir() / f"{safe}.json"


def _default_state() -> dict[str, Any]:
    return {
        "offset": 0,
        "recent_prompts": [],  # [{norm, ts}]
        "chain_len": 0,
        "last_fire_ts": 0.0,
        "last_parse_ts": 0.0,
        "tool_error_streak": 0,
        # 前回完全実行時に解決した debounce 秒。一段目ゲートはこれだけを見る
        # （config 変更は次回完全実行後に反映 = 1回遅れ。docstring 契約）
        "effective_debounce": _DEFAULT_DEBOUNCE,
    }


def load_state(session_id: str) -> dict[str, Any]:
    path = state_path(session_id)
    if not path.is_file():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _default_state()
        base = _default_state()
        base.update(data)
        if not isinstance(base.get("recent_prompts"), list):
            base["recent_prompts"] = []
        return base
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return _default_state()


def save_state(session_id: str, state: dict[str, Any]) -> None:
    path = state_path(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def cleanup_old_states(*, max_age_days: int = 7) -> int:
    """max_age_days 超過の状態ファイルを削除。削除件数を返す。"""
    d = state_dir()
    if not d.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400
    n = 0
    try:
        for p in d.glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except OSError:
                continue
    except OSError:
        return n
    return n


def parse_hook_stdin(raw: str | None) -> dict[str, Any]:
    """フック JSON を寛容パース。不正時は空 dict。"""
    if not raw or not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _should_debounce(state: dict[str, Any], debounce_seconds: float) -> bool:
    """前回解析から debounce_seconds 未満なら True。

    一段目ゲートは state の effective_debounce（前回完全実行で保存した値）を使う。
    config の debounce_seconds 変更は完全実行を1回経た後に効く（1回遅れ）。
    """
    last = float(state.get("last_parse_ts") or 0.0)
    if last <= 0:
        return False
    # debounce_seconds <= 0 はデバウンス無効
    if float(debounce_seconds) <= 0:
        return False
    return (time.time() - last) < float(debounce_seconds)


def _read_new_lines(
    transcript_path: str | Path,
    offset: int,
) -> tuple[list[str], int]:
    """オフセットから完全行のみ読む。戻り値: (lines, new_offset)。

    不完全最終行は捨て、オフセットは完全行末まで。
    ファイル縮小時は先頭から再読。
    """
    path = Path(transcript_path)
    if not path.is_file():
        return [], 0
    try:
        size = path.stat().st_size
    except OSError:
        return [], offset
    if offset > size:
        offset = 0
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    if not data:
        return [], offset
    # 完全行 = 末尾が \\n で終わる部分まで
    last_nl = data.rfind(b"\n")
    if last_nl < 0:
        return [], offset  # 完全行なし（書き込み途中）
    complete_b = data[: last_nl + 1]
    new_offset = offset + len(complete_b)
    lines = complete_b.decode("utf-8", errors="replace").splitlines()
    return [ln for ln in lines if ln.strip()], new_offset


def _parse_jsonl_records(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    for ln in lines:
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _extract_user_text(record: dict) -> str | None:
    """Claude Code JSONL の user レコードからテキストを抜く。"""
    if record.get("type") != "user" and record.get("role") != "user":
        # message ネスト
        msg = record.get("message")
        if not isinstance(msg, dict):
            return None
        if msg.get("role") != "user":
            return None
    else:
        msg = record.get("message") if isinstance(record.get("message"), dict) else record
    if record.get("isMeta") or record.get("isSidechain"):
        return None
    content = None
    if isinstance(msg, dict):
        content = msg.get("content")
    if content is None:
        content = record.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, dict) and item.get("type") == "tool_result":
                return None  # tool result は依頼ではない
        t = " ".join(parts).strip()
        return t or None
    return None


def _tool_result_blocks(record: dict) -> list[dict]:
    """レコード内の tool_result ブロック一覧（トップレベル or message.content）。"""
    blocks: list[dict] = []
    if record.get("type") == "tool_result":
        blocks.append(record)
    msg = record.get("message") if isinstance(record.get("message"), dict) else None
    content = None
    if isinstance(msg, dict):
        content = msg.get("content")
    if content is None:
        content = record.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                blocks.append(item)
    return blocks


def _is_tool_error(record: dict) -> bool:
    """ツール結果エラーのヒューリスティック。

    Claude Code JSONL では tool_result は type=user の message.content 内に入る。
    """
    for item in _tool_result_blocks(record):
        if item.get("is_error"):
            return True
        t = str(item.get("content") or item.get("text") or "").lower()
        if "error" in t[:200] or "failed" in t[:200]:
            return True
    return False


def _has_successful_tool_result(record: dict) -> bool:
    """成功した tool_result ブロックがあるか。

    is_error が truthy でない tool_result が1つでもあれば True。
    同一レコードにエラーと成功が混在する場合: 呼び出し側はエラー優先で
    カウントし、成功があってもリセットしない（エラー含むレコードはリセットしない）。
    """
    for item in _tool_result_blocks(record):
        if item.get("is_error"):
            continue
        # is_error 欠落/false → 成功扱い
        t = str(item.get("content") or item.get("text") or "").lower()
        if "error" in t[:200] or "failed" in t[:200]:
            continue
        return True
    return False


def _assistant_tokens(record: dict) -> tuple[int, str | None]:
    """assistant レコードから output tokens と model を抜く。"""
    msg = record.get("message") if isinstance(record.get("message"), dict) else {}
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    tokens = 0
    for key in ("output_tokens", "completion_tokens", "outputTokens"):
        v = usage.get(key) if usage else None
        if isinstance(v, (int, float)):
            tokens = int(v)
            break
    model = None
    if isinstance(msg, dict) and msg.get("model"):
        model = str(msg.get("model"))
    elif record.get("model"):
        model = str(record.get("model"))
    return tokens, model


def _record_ts(record: dict) -> float:
    ts = record.get("timestamp")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            from datetime import datetime

            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return time.time()
    return time.time()


def count_live_breaker_fires(memory_dir: Path, day, *, tz=None) -> int:
    """当日 live_episodes 件数。会計には使わない（通知履歴）。

    ts は UTC ISO で保存される。比較は config TZ（ローカル日付）へ変換して行う。
    tz 未指定時は UTC。パース不能行はスキップ（fail-closed）。
    """
    from datetime import date as date_cls
    from datetime import datetime
    from zoneinfo import ZoneInfo

    path = Path(memory_dir) / LIVE_EPISODES_FILE
    if not path.is_file():
        return 0
    if not isinstance(day, date_cls):
        try:
            day = date_cls.fromisoformat(str(day)[:10])
        except ValueError:
            return 0
    if tz is None:
        tz = ZoneInfo("UTC")
    n = 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        ts_raw = d.get("ts")
        if not ts_raw:
            continue
        try:
            if isinstance(ts_raw, (int, float)):
                dt = datetime.fromtimestamp(float(ts_raw), tz=ZoneInfo("UTC"))
            else:
                s = str(ts_raw).replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    # naive はスキップ（タイムゾーン不明を当日扱いしない）
                    continue
            local_d = dt.astimezone(tz).date()
        except (ValueError, TypeError, OSError):
            continue
        if local_d == day:
            n += 1
    return n


def append_live_episode(memory_dir: Path | None, event: dict) -> None:
    if memory_dir is None:
        return
    try:
        memory_dir = Path(memory_dir)
        memory_dir.mkdir(parents=True, exist_ok=True)
        path = memory_dir / LIVE_EPISODES_FILE
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _fire_message(
    *,
    kind: str,
    chain_len: int,
    est_usd: float | None,
    excerpt: str | None,
) -> str:
    if kind == "tool_error":
        base = (
            f"⚠️ KaizenLog空転ブレーカー: ツールエラーが{chain_len}回連続しています。"
            "方針変更・手動介入を検討してください。"
        )
    else:
        base = (
            f"⚠️ KaizenLog空転ブレーカー: 同趣旨の依頼が{chain_len}回目です"
        )
        if est_usd is not None:
            base += f"（推定${est_usd:.2f}浪費中）"
        base += "。アプローチを変える・前提を明示する・手動介入を検討してください。"
    if excerpt:
        base += f" 抜粋: {excerpt[:40]}"
    return base


def run_hook(
    raw_stdin: str | None = None,
    *,
    config_path: str | None = None,
    settings: dict[str, Any] | None = None,
) -> int:
    """フック本体。常に 0 を返す。"""
    try:
        return _run_hook_body(
            raw_stdin, config_path=config_path, settings=settings
        )
    except Exception:
        return 0


def _run_hook_body(
    raw_stdin: str | None,
    *,
    config_path: str | None,
    settings: dict[str, Any] | None,
) -> int:
    if raw_stdin is None:
        try:
            raw_stdin = sys.stdin.read()
        except Exception:
            raw_stdin = ""

    payload = parse_hook_stdin(raw_stdin)
    session_id = str(
        payload.get("session_id")
        or payload.get("sessionId")
        or "unknown"
    )
    transcript_path = (
        payload.get("transcript_path")
        or payload.get("transcriptPath")
        or ""
    )
    hook_event = str(
        payload.get("hook_event_name")
        or payload.get("hookEventName")
        or ""
    )
    prompt_direct = payload.get("prompt")

    # 一段目デバウンス: config 読込前。state に保存した effective_debounce を使う
    # （初回は既定30。config 変更は完全実行後に effective_debounce 更新 → 1回遅れ）
    state = load_state(session_id)
    gate_debounce = float(state.get("effective_debounce", _DEFAULT_DEBOUNCE))
    if _should_debounce(state, gate_debounce):
        return 0

    # --- ここから設定・検知用の lazy import ---
    cfg = None
    memory_dir = None
    redactor = None
    pricing = None
    enabled = True
    retry_threshold = _DEFAULT_RETRY_THRESHOLD
    tool_error_streak_th = _DEFAULT_TOOL_ERROR_STREAK
    cooldown = _DEFAULT_COOLDOWN
    debounce = float(_DEFAULT_DEBOUNCE)
    if settings is None:
        try:
            from .config import load_config

            cfg = load_config(config_path)
            g = getattr(cfg, "guard", None)
            if g is not None:
                enabled = bool(getattr(g, "enabled", True))
                retry_threshold = int(getattr(g, "retry_threshold", retry_threshold))
                tool_error_streak_th = int(
                    getattr(g, "tool_error_streak", tool_error_streak_th)
                )
                cooldown = int(getattr(g, "cooldown_seconds", cooldown))
                debounce = float(getattr(g, "debounce_seconds", debounce))
            memory_dir = cfg.memory_path
            pricing = cfg.aiwork.pricing or None
            from .privacy import make_redactor

            redactor = make_redactor(
                cfg.privacy.redact_patterns, cfg.privacy.replacement
            )
        except Exception:
            cfg = None
    else:
        enabled = bool(settings.get("enabled", True))
        retry_threshold = int(settings.get("retry_threshold", retry_threshold))
        tool_error_streak_th = int(
            settings.get("tool_error_streak", tool_error_streak_th)
        )
        cooldown = int(settings.get("cooldown_seconds", cooldown))
        debounce = float(settings.get("debounce_seconds", debounce))
        memory_dir = settings.get("memory_dir")
        pricing = settings.get("pricing")
        redactor = settings.get("redactor")

    if not enabled:
        return 0

    # 二段目: config 解決後の実効値でも再確認（同一実行内の短縮のみ）
    if _should_debounce(state, debounce):
        return 0

    from difflib import SequenceMatcher

    # 正規化・類似度は夜間 detect_retry_chains と同一
    # （promptmine.normalize + similarity_ratio。normalize_prompt_text ではない）
    try:
        from .promptmine import normalize as _normalize
        from .promptledger import similarity_ratio as _sim
    except Exception:
        def _normalize(t: str) -> str:
            return " ".join((t or "").split()).lower()

        def _sim(a: str, b: str) -> float:
            if not a or not b:
                return 0.0
            return SequenceMatcher(None, a, b).ratio()

    try:
        from .aiwork import resolve_output_price
    except Exception:
        def resolve_output_price(model, pricing=None):  # type: ignore
            return None

    records: list[dict] = []
    new_offset = int(state.get("offset") or 0)
    if transcript_path:
        lines, new_offset = _read_new_lines(transcript_path, new_offset)
        records = _parse_jsonl_records(lines)

    # UserPromptSubmit の prompt 直渡し
    if prompt_direct and isinstance(prompt_direct, str) and prompt_direct.strip():
        records.append(
            {
                "type": "user",
                "timestamp": time.time(),
                "message": {"role": "user", "content": prompt_direct},
            }
        )

    state["last_parse_ts"] = time.time()
    state["offset"] = new_offset

    recent: list[dict] = list(state.get("recent_prompts") or [])
    chain_len = int(state.get("chain_len") or 0)
    tool_streak = int(state.get("tool_error_streak") or 0)
    fire_kind: str | None = None
    fire_len = 0
    est_tokens: int | None = 0
    est_usd: float | None = 0.0
    any_tok = False
    any_price = True
    excerpt = None
    model_for_price = None

    for rec in records:
        # ツールエラー streak（Stop 向け）
        # 規約: エラーを含むレコードはカウントし、成功があってもリセットしない。
        # エラーなしで成功 tool_result があるレコードだけリセット。
        # ユーザー/アシスタントの通常発話ではリセットしない。
        if _is_tool_error(rec):
            tool_streak += 1
        elif _has_successful_tool_result(rec):
            tool_streak = 0

        # assistant tokens（連鎖中の浪費推定用）
        if (
            rec.get("type") == "assistant"
            or (
                isinstance(rec.get("message"), dict)
                and rec["message"].get("role") == "assistant"
            )
        ):
            tok, model = _assistant_tokens(rec)
            if tok:
                any_tok = True
                est_tokens = int(est_tokens or 0) + tok
                if model:
                    model_for_price = model
                price = resolve_output_price(model, pricing)
                if price is None:
                    any_price = False
                elif est_usd is not None:
                    est_usd = float(est_usd) + (tok / 1_000_000.0) * float(price)

        text = _extract_user_text(rec)
        if not text or len(text) < 8:
            continue
        ts = _record_ts(rec)
        norm = _normalize(text)
        # 連鎖判定
        if recent:
            last = recent[-1]
            last_ts = float(last.get("ts") or 0)
            delta_min = (ts - last_ts) / 60.0 if last_ts else 999
            ratio = _sim(norm, str(last.get("norm") or ""))
            if delta_min <= _RETRY_WINDOW_MINUTES and ratio >= _RETRY_SIMILARITY:
                chain_len = int(last.get("chain_len") or 1) + 1
            else:
                chain_len = 1
        else:
            chain_len = 1
        recent.append({"norm": norm, "ts": ts, "chain_len": chain_len, "raw": text[:200]})
        if len(recent) > _MAX_RECENT_PROMPTS:
            recent = recent[-_MAX_RECENT_PROMPTS:]
        if chain_len >= retry_threshold:
            fire_kind = "retry"
            fire_len = chain_len
            excerpt = text

    # Stop 時のツールエラー
    if hook_event.lower() == "stop" or "stop" in hook_event.lower():
        if tool_streak >= tool_error_streak_th:
            fire_kind = fire_kind or "tool_error"
            fire_len = max(fire_len, tool_streak)

    # 連続 user プロンプトだけでも UserPromptSubmit で発火
    if fire_kind is None and chain_len >= retry_threshold:
        fire_kind = "retry"
        fire_len = chain_len

    state["recent_prompts"] = [
        {"norm": r["norm"], "ts": r["ts"], "chain_len": r.get("chain_len", 1)}
        for r in recent
    ]
    state["chain_len"] = chain_len
    state["tool_error_streak"] = tool_streak

    now = time.time()
    last_fire = float(state.get("last_fire_ts") or 0)
    if fire_kind and (now - last_fire) >= cooldown:
        if not any_tok:
            est_tokens = None
            est_usd = None
        elif not any_price:
            est_usd = None
        else:
            est_usd = round(float(est_usd or 0), 4)

        ex = excerpt or ""
        if redactor and ex:
            try:
                ex = redactor(ex)
            except Exception:
                pass
        msg = _fire_message(
            kind=fire_kind,
            chain_len=fire_len,
            est_usd=est_usd,
            excerpt=ex or None,
        )
        # stdout フック JSON
        out = {
            "hookSpecificOutput": {
                "hookEventName": hook_event or "UserPromptSubmit",
                "additionalContext": msg,
            }
        }
        try:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        # notify
        try:
            from .notify import notify

            notify("KaizenLog 空転ブレーカー", msg[:200], icon="Warning")
        except Exception:
            pass
        # live_episodes
        from datetime import datetime, timezone

        append_live_episode(
            Path(memory_dir) if memory_dir else None,
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "kind": fire_kind,
                "chain_len": fire_len,
                "est_tokens": est_tokens,
                "est_usd": est_usd,
                "excerpt": (ex or "")[:80],
            },
        )
        state["last_fire_ts"] = now

    # 完全実行後: 次の一段目ゲート用に実効 debounce を保存（config 変更は1回遅れ）
    state["effective_debounce"] = float(debounce)
    save_state(session_id, state)
    return 0


# ---- install / settings merge ----

def build_hook_command(*, python_exe: str, config_path: str | None) -> str:
    parts = [f'"{python_exe}"', "-m", "kaizenlog.cli"]
    if config_path:
        parts.extend(["--config", f'"{config_path}"'])
    parts.extend(["guard", "--hook"])
    return " ".join(parts)


def build_hooks_snippet(command: str) -> dict:
    """Claude Code settings.json 用 hooks 断片。"""
    entry = {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": command,
            }
        ],
    }
    return {
        "UserPromptSubmit": [entry],
        "Stop": [json.loads(json.dumps(entry))],  # copy
    }


def _is_kaizenlog_guard_command(cmd: str) -> bool:
    c = (cmd or "").lower()
    return "kaizenlog" in c and "guard" in c and "--hook" in c


def merge_hooks_into_settings(
    settings: dict, snippet: dict
) -> dict:
    """settings に hooks を冪等マージ。他フック不可侵。kaizenlog guard のみ置換。"""
    out = json.loads(json.dumps(settings))  # deep copy
    hooks = out.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        out["hooks"] = hooks
    for event, entries in snippet.items():
        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = []
        # 既存から kaizenlog guard エントリを除去
        kept = []
        for block in existing:
            if not isinstance(block, dict):
                kept.append(block)
                continue
            inner = block.get("hooks")
            if not isinstance(inner, list):
                kept.append(block)
                continue
            new_inner = [
                h
                for h in inner
                if not (
                    isinstance(h, dict)
                    and _is_kaizenlog_guard_command(str(h.get("command") or ""))
                )
            ]
            # 空 hooks ブロックは他人のものとして不可侵（all([]) が True にならないよう
            # 非空かつ全コマンドが guard のときだけブロックごと捨てる）
            if (
                inner
                and not new_inner
                and all(
                    isinstance(h, dict)
                    and _is_kaizenlog_guard_command(str(h.get("command") or ""))
                    for h in inner
                )
            ):
                # ブロック全体が guard のみ → ブロックごと捨てる
                continue
            if new_inner != inner:
                b2 = dict(block)
                b2["hooks"] = new_inner
                if new_inner:
                    kept.append(b2)
            else:
                kept.append(block)
        # 新エントリ追加
        for e in entries:
            kept.append(e)
        hooks[event] = kept
    return out


def install_hooks_write(
    settings_path: Path,
    command: str,
) -> Path:
    """settings.json にマージ書き込み。bak を返す。JSON不正なら例外。"""
    settings_path = Path(settings_path)
    if settings_path.is_file():
        try:
            raw = settings_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            raise ValueError(f"settings.json が不正な JSON です: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("settings.json のルートはオブジェクトである必要があります")
    else:
        data = {}
        settings_path.parent.mkdir(parents=True, exist_ok=True)

    snippet = build_hooks_snippet(command)
    merged = merge_hooks_into_settings(data, snippet)

    # backup
    bak = settings_path
    if settings_path.is_file():
        ts = time.strftime("%Y%m%d%H%M%S")
        bak = settings_path.with_name(settings_path.name + f".bak-{ts}")
        bak.write_bytes(settings_path.read_bytes())

    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, settings_path)
    return bak


def format_guard_status(
    *,
    enabled: bool,
    retry_threshold: int,
    tool_error_streak: int,
    cooldown_seconds: int,
    debounce_seconds: int,
) -> str:
    d = state_dir()
    sessions = 0
    last_fire = None
    if d.is_dir():
        for p in d.glob("*.json"):
            sessions += 1
            try:
                st = json.loads(p.read_text(encoding="utf-8"))
                lf = float(st.get("last_fire_ts") or 0)
                if lf and (last_fire is None or lf > last_fire):
                    last_fire = lf
            except Exception:
                continue
    lines = [
        "# KaizenLog 空転ブレーカー status",
        f"enabled: {enabled}",
        f"retry_threshold: {retry_threshold}",
        f"tool_error_streak: {tool_error_streak}",
        f"cooldown_seconds: {cooldown_seconds}",
        f"debounce_seconds: {debounce_seconds}",
        f"state_dir: {d}",
        f"cached_sessions: {sessions}",
    ]
    if last_fire:
        from datetime import datetime

        lines.append(
            f"last_fire: {datetime.fromtimestamp(last_fire).isoformat(timespec='seconds')}"
        )
    else:
        lines.append("last_fire: （なし）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """軽量エントリ: python -m kaizenlog.guard [--config PATH]"""
    argv = list(argv or sys.argv[1:])
    config_path = None
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 < len(argv):
            config_path = argv[i + 1]
    return run_hook(config_path=config_path)


if __name__ == "__main__":
    raise SystemExit(main())
