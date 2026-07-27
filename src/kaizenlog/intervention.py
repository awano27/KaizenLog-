"""検出した時間泥棒から LeechBlock NG のブロックルールを生成する。

「提案→介入→検証」の閉ループの介入部分:

1. 日次統計（.kaizenlog/stats/）からエンタメ等の時間泥棒サイトを検出
2. LeechBlock NG のインポート形式（key=value 行）のルールファイルを生成
3. 効果測定のカイゼン実験を同時に起票（毎晩の generate が自動計測）

適用は必ず人間がブラウザの LeechBlock NG 設定画面でインポートして行う
（勝手に有効化しない）。生成するセットは 20 番以降の「KZN:」プレフィックス
付きに限定し、ユーザーが手で作った既存セット（1〜19）を上書きしない。

データソースは2系統:
- aw-watcher-web 導入済み: by_site のドメイン別実測値（正確）
- 未導入: ブロックのタイトル/アプリ名から既知サービスを推定（近似）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .classifier import Classifier
from .collector import ActivityEvent

# ブロック対象とみなすカテゴリ（分類ルールで判定）
DEFAULT_TARGET_CATEGORIES = ("エンタメ",)

# LeechBlock のセット番号。1〜19はユーザー領域として触らない
KZN_SET_START = 20
SET_NAME_PREFIX = "KZN: "

# aw-watcher-web が無い場合の、タイトル/アプリ名→ドメインの推定表
SITE_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"youtube", re.IGNORECASE), "youtube.com"),
    (re.compile(r"netflix", re.IGNORECASE), "netflix.com"),
    # Xのウィンドウタイトルは「ホーム / X」等で "x.com" を含まないため \bx\b で拾う
    # （エンタメ分類済みブロックのみが対象なので誤爆リスクは分類器と同等）
    (re.compile(r"twitter|\bx\b|x\.com", re.IGNORECASE), "x.com twitter.com"),
    (re.compile(r"reddit", re.IGNORECASE), "reddit.com"),
    (re.compile(r"tiktok", re.IGNORECASE), "tiktok.com"),
    (re.compile(r"twitch", re.IGNORECASE), "twitch.tv"),
    (re.compile(r"nicovideo|ニコニコ", re.IGNORECASE), "nicovideo.jp"),
    (re.compile(r"instagram", re.IGNORECASE), "instagram.com"),
]

# 時間帯ウィンドウがこの割合以上をカバーするなら「時間帯集中型」とみなす
WINDOW_COVERAGE = 0.7
# ウィンドウがこの時間を超えるなら全日拡散型として日次上限に切り替える
WINDOW_MAX_HOURS = 8


@dataclass
class TimeSink:
    domains: str          # LeechBlock の sites 値（空白区切り）
    label: str            # 表示名
    total_minutes: float
    days_with_data: int   # 統計が存在した日数
    hour_minutes: dict[int, float] = field(default_factory=dict)  # 現地時間の時間帯→分
    source: str = "site"  # "site"（実測） | "title"（推定）

    @property
    def avg_minutes(self) -> float:
        return self.total_minutes / self.days_with_data if self.days_with_data else 0.0


@dataclass
class BlockRule:
    set_name: str
    sites: str
    times: str        # LeechBlockのtimes形式（"1700-1900" / 深夜跨ぎは分割済み）。空なら終日
    limit_mins: int
    limit_period: int  # 秒（3600=毎時 / 86400=毎日）
    metric: str
    target: str       # 効果測定実験の目標（例 "<= 20"）
    evidence: str
    window: tuple[int, int] | None = None  # 表示用の時間帯（開始時, 終了時）

    @property
    def conj_mode(self) -> bool:
        # 時間帯あり = 「その時間帯の中で」上限を超えたらブロック（AND条件）
        return bool(self.times)


def format_times(start_hour: int, end_hour: int) -> str:
    """時間帯ウィンドウを LeechBlock の times 形式にする。

    LeechBlock は start >= end の期間を「継続時間なし」として黙って捨てる
    （cleanTimePeriods）。深夜を跨ぐウィンドウは 2400 で分割し、終了0時は
    2400 と表記しないとルールが無言で無効化される。
    """
    if 0 < end_hour <= start_hour:
        return f"{start_hour:02d}00-2400,0000-{end_hour:02d}00"
    if end_hour == 0:
        return f"{start_hour:02d}00-2400"
    return f"{start_hour:02d}00-{end_hour:02d}00"


def _classify_domain(classifier: Classifier, domain: str) -> str:
    """ドメインを既存の分類ルールでカテゴリ判定する（統計には分類が無いため）。"""
    now = datetime.now().astimezone()
    ev = ActivityEvent(now, now, "chrome.exe", domain, url=f"https://{domain}/")
    return classifier.classify(ev).category


def _block_hours(block: dict) -> dict[int, float]:
    """ブロックの分を現地時間の時間帯に配分する。"""
    out: dict[int, float] = {}
    try:
        start = datetime.fromisoformat(block["start"]).astimezone()
        end = datetime.fromisoformat(block["end"]).astimezone()
    except (KeyError, ValueError):
        return out
    cur = start
    while cur < end:
        hour_end = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        nxt = min(end, hour_end)
        out[cur.hour] = out.get(cur.hour, 0.0) + (nxt - cur).total_seconds() / 60
        cur = nxt
    return out


def detect_time_sinks(
    stats_list: list[dict],
    rules: list[dict],
    min_avg_minutes: float = 15.0,
    categories: tuple[str, ...] = DEFAULT_TARGET_CATEGORIES,
) -> list[TimeSink]:
    """日次統計から時間泥棒サイトを検出する（平均 min_avg_minutes 分/日以上）。"""
    if not stats_list:
        return []
    classifier = Classifier(rules)
    days = len(stats_list)

    # 1) aw-watcher-web の実測（by_site）から
    site_minutes: dict[str, float] = {}
    site_hours: dict[str, dict[int, float]] = {}
    for st in stats_list:
        for site, minutes in (st.get("by_site") or {}).items():
            if _classify_domain(classifier, site) in categories:
                site_minutes[site] = site_minutes.get(site, 0.0) + minutes

    # 2) タイトル/アプリ名からの推定（webデータの無いサイトを補完）
    hint_minutes: dict[str, tuple[str, float]] = {}  # domains -> (label, minutes)
    hint_hours: dict[str, dict[int, float]] = {}
    for st in stats_list:
        for block in st.get("blocks") or []:
            if block.get("category") not in categories:
                continue
            text = f"{block.get('app', '')} | {block.get('title', '')}"
            for pattern, domains in SITE_HINTS:
                if pattern.search(text):
                    label = domains.split()[0]
                    prev = hint_minutes.get(domains, (label, 0.0))
                    hint_minutes[domains] = (label, prev[1] + float(block.get("minutes", 0)))
                    hours = hint_hours.setdefault(domains, {})
                    for h, m in _block_hours(block).items():
                        hours[h] = hours.get(h, 0.0) + m
                    break

    sinks: list[TimeSink] = []
    consumed_sites: set[str] = set()
    # 同じドメインを実測とタイトル推定の両方が指す場合は大きい方を採用する。
    # 実測を無条件優先すると、拡張導入直後（実測が数分だけ）に45分/日の
    # 時間泥棒がしきい値未満となり、検出から丸ごと消えてしまう。
    for domains, (label, minutes) in hint_minutes.items():
        overlap = [d for d in domains.split() if d in site_minutes]
        site_total = sum(site_minutes[d] for d in overlap)
        if overlap and site_total >= minutes:
            continue  # 実測の方が大きい → site ソースに任せる
        consumed_sites.update(overlap)
        sinks.append(TimeSink(domains=domains, label=label, total_minutes=minutes,
                              days_with_data=days,
                              hour_minutes=hint_hours.get(domains, {}), source="title"))
    for site, minutes in site_minutes.items():
        if site in consumed_sites:
            continue
        sinks.append(TimeSink(domains=site, label=site, total_minutes=minutes,
                              days_with_data=days, source="site"))

    return sorted(
        [s for s in sinks if s.avg_minutes >= min_avg_minutes],
        key=lambda s: -s.total_minutes,
    )


def suggest_window(hour_minutes: dict[int, float]) -> tuple[int, int] | None:
    """時間帯ヒストグラムから、大半をカバーする最小の連続ウィンドウを探す。

    WINDOW_MAX_HOURS 以内で全体の WINDOW_COVERAGE 以上をカバーできなければ
    None（全日拡散型 → 日次上限で対応）。
    """
    total = sum(hour_minutes.values())
    if total <= 0:
        return None
    best: tuple[int, int] | None = None
    for length in range(1, WINDOW_MAX_HOURS + 1):
        for start in range(24):
            covered = sum(hour_minutes.get((start + i) % 24, 0.0) for i in range(length))
            if covered / total >= WINDOW_COVERAGE:
                best = (start, (start + length) % 24)
                break
        if best:
            break
    return best


def suggest_rules(sinks: list[TimeSink], max_rules: int = 5) -> list[BlockRule]:
    """時間泥棒ごとに段階的なブロックルールを提案する。

    - 時間帯集中型: その時間帯に限り「1時間あたり10分まで」（完全ブロックにしない）
    - 全日拡散型: 「1日あたり現状平均の半分まで」（最低10分は残す）
    """
    out: list[BlockRule] = []
    for sink in sinks[:max_rules]:
        window = suggest_window(sink.hour_minutes)
        metric = (f"site_minutes:{sink.label}" if sink.source == "site"
                  else "category_minutes:エンタメ")
        # 初回目標: avg×0.7（最低10分・5分丸め）。半減は FAIL 連鎖で意欲を削ぐため段階的に
        target_minutes = max(10, int(round(sink.avg_minutes * 0.7 / 5) * 5))
        target = f"<= {target_minutes}"
        if window:
            end_label = f"{window[1]}時" if window[1] > window[0] else f"翌{window[1]}時"
            evidence = (f"{sink.label}: 平均 {sink.avg_minutes:.0f}分/日、"
                        f"主に {window[0]}時〜{end_label}に集中")
            out.append(BlockRule(
                set_name=f"{SET_NAME_PREFIX}{sink.label}",
                sites=sink.domains, times=format_times(*window),
                limit_mins=10, limit_period=3600,
                metric=metric, target=target, evidence=evidence,
                window=window,
            ))
        else:
            evidence = (f"{sink.label}: 平均 {sink.avg_minutes:.0f}分/日（終日に分散）"
                        f" → 段階目標 {target_minutes}分/日 を上限に")
            out.append(BlockRule(
                set_name=f"{SET_NAME_PREFIX}{sink.label}",
                sites=sink.domains, times="",
                limit_mins=target_minutes, limit_period=86400,
                metric=metric, target=target, evidence=evidence,
            ))
    return out


def render_leechblock_options(rules: list[BlockRule], start_set: int = KZN_SET_START) -> str:
    """LeechBlock NG のインポート形式（key=value 行）を生成する。

    インポートはファイルに含まれるキーだけを上書きするため、start_set 以降の
    セットと numSets のみを出力し、ユーザーの既存セットには触れない。
    days は日曜=bit0 のビットマスク（127 = 毎日）。
    """
    if not rules:
        return ""
    lines = [f"numSets={start_set + len(rules) - 1}"]
    for i, rule in enumerate(rules):
        n = start_set + i
        lines.append(f"setName{n}={rule.set_name}")
        lines.append(f"sites{n}={rule.sites}")
        lines.append(f"times{n}={rule.times}")
        lines.append(f"limitMins{n}={rule.limit_mins}")
        lines.append(f"limitPeriod{n}={rule.limit_period}")
        lines.append(f"conjMode{n}={'true' if rule.conj_mode else 'false'}")
        lines.append(f"days{n}=127")
    return "\n".join(lines) + "\n"


def render_plan(sinks: list[TimeSink], rules: list[BlockRule]) -> str:
    """人間が承認判断するための提案サマリーを生成する。"""
    if not rules:
        return ("対象となる時間泥棒は見つかりませんでした"
                "（平均15分/日以上のエンタメサイトが対象）。")
    lines = ["# LeechBlock 介入プラン", ""]
    for rule in rules:
        if rule.window:
            start_h, end_h = rule.window
            end_label = f"{end_h}時" if end_h > start_h else f"翌{end_h}時"
            action = f"{start_h}時〜{end_label}は1時間あたり{rule.limit_mins}分まで"
        else:
            action = f"1日あたり{rule.limit_mins}分まで"
        lines.append(f"- **{rule.set_name}** — {action}")
        lines.append(f"  - 根拠: {rule.evidence}")
        lines.append(f"  - 効果測定: `{rule.metric}`")
    lines.append("")
    approx = [s for s in sinks if s.source == "title"]
    if approx:
        lines.append("⚠️  一部はウィンドウタイトルからの推定です。"
                     "aw-watcher-web 拡張を入れるとドメイン実測になり精度が上がります。")
    lines.append("")
    lines.append(
        "⚠️  PCでのブロックはスマホ等への移行（風船効果）を測定できない。"
        "実験が成功して見えても体感と合わない場合はデバイス移行を疑うこと。"
    )
    return "\n".join(lines)
