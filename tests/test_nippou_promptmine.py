import json
from datetime import datetime, timedelta, timezone

from kaizenlog.aiwork import UserPrompt, scan_user_prompts
from kaizenlog.nippou import (
    build_nippou_prompt,
    generate_nippou_deterministic,
)
from kaizenlog.promptmine import cluster_prompts, normalize, render_prompt_report

TZ = timezone.utc
DAY_START = datetime(2020, 1, 1, tzinfo=TZ)
DAY_END = DAY_START + timedelta(days=1)


# ---- 日報ドラフト ----

def _stats():
    def block(hour, minutes, category, app, title):
        start = DAY_START.replace(hour=hour)
        return {"start": start.isoformat(), "end": (start + timedelta(minutes=minutes)).isoformat(),
                "category": category, "app": app, "minutes": minutes, "title": title}
    return {
        "day": "2020-01-01", "total_minutes": 240.0, "context_switches": 5,
        "by_category": {}, "by_app": {},
        "blocks": [
            block(9, 60, "開発", "Code.exe", "collector.py - VS Code"),
            block(11, 30, "AI作業", "chrome.exe", "Claude"),
            block(20, 45, "ブラウジング", "chrome.exe", "YouTube - music"),  # ブラウザ経由の私的コンテンツ
            block(21, 30, "エンタメ", "Steam.exe", "ゲーム"),
            block(14, 10, "開発", "Code.exe", "短い作業"),
        ],
        "ai": {"sessions": 2, "fragmented": 0, "tool_errors": 0, "interruptions": 0, "projects": {}},
    }


INTENT = """## Tasks
- [x] スクレイパーのバグ修正
- [ ] READMEの更新"""


def test_deterministic_nippou_structure():
    md = generate_nippou_deterministic(_stats(), TZ, INTENT)
    assert "## 📝 日報ドラフト" in md
    assert "【本日の業務】" in md and "【成果・進捗】" in md and "【明日の予定】" in md
    assert "collector.py" in md
    assert "スクレイパーのバグ修正 を完了" in md
    assert "READMEの更新" in md  # 未完タスク→明日の予定
    assert "Claude Code" in md and "2セッション" in md
    # sources ありなら複数ソース表記
    stats2 = _stats()
    stats2["ai"]["sources"] = {"claude-code": {"sessions": 1}, "codex": {"sessions": 1}}
    md2 = generate_nippou_deterministic(stats2, TZ, INTENT)
    assert "Claude Code / Codex CLI" in md2


def test_deterministic_nippou_excludes_entertainment_and_short():
    md = generate_nippou_deterministic(_stats(), TZ, None)
    assert "YouTube" not in md   # ブラウザ経由の私的コンテンツも日報に載せない
    assert "ゲーム" not in md     # エンタメカテゴリは日報に載せない
    assert "短い作業" not in md   # 15分未満は載せない
    assert "合計 4時間0分" in md  # タスクなし時のフォールバック


def test_build_nippou_prompt_includes_intent():
    p = build_nippou_prompt("ログ本文", INTENT)
    assert "本日の計画" in p and "ログ本文" in p
    assert build_nippou_prompt("ログ本文", None).startswith("# 本日の作業ログ")


# ---- プロンプト資産化 ----

def test_normalize_absorbs_variations():
    a = normalize("AI-NEWSの記事を3件要約して C:\\dev\\news\\a.md に保存")
    b = normalize("ai-newsの記事を10件要約して c:/dev/news/b.md に保存")
    assert a == b


def _prompt(text, day_offset=0, project="ai-news"):
    return UserPrompt(timestamp=DAY_START + timedelta(days=day_offset), project=project, text=text)


def test_cluster_similar_prompts():
    prompts = [
        _prompt("今日のAIニュースを5件要約して", 0),
        _prompt("今日のAIニュースを8件要約して", 1),
        _prompt("今日のAIニュースを3件要約して", 2),
        _prompt("テストを実行してエラーを直して", 0, project="vault"),
    ]
    clusters = cluster_prompts(prompts)
    assert len(clusters) == 2
    top = max(clusters, key=lambda c: c.count)
    assert top.count == 3
    assert top.days == {"2020-01-01", "2020-01-02", "2020-01-03"}


def test_normalize_url_before_path():
    # URL内の / をパス置換に食われて "http<path>" に壊れないこと
    assert normalize("see https://example.com/a/b now") == "see <url> now"


def test_cluster_prompts_order_independent():
    from itertools import permutations
    texts = [
        "今日のAIニュースを要約してノートに保存して",
        "今日のAIニュースを要約して保存",
        "ニュースを保存",
    ]
    results = set()
    for perm in permutations(texts):
        clusters = cluster_prompts([_prompt(t, i) for i, t in enumerate(perm)])
        results.add(tuple(sorted((c.representative, c.count) for c in clusters)))
    assert len(results) == 1  # どの入力順でも同じクラスタになる


def test_render_prompt_report():
    prompts = [_prompt(f"今日のAIニュースを{i}件要約して", i) for i in range(5)]
    md = render_prompt_report(prompts, days=7, min_count=3)
    assert "5回 / 5日 / ai-news" in md
    assert "スキル化を強く推奨" in md


def test_render_prompt_report_empty_and_below_threshold():
    assert "見つかりませんでした" in render_prompt_report([], days=7)
    prompts = [_prompt("単発の依頼です")]
    assert "繰り返された依頼パターンはありません" in render_prompt_report(prompts, days=7)


# ---- scan_user_prompts ----

def test_scan_user_prompts_filters(tmp_path):
    def rec(kind, text_or_content, minute):
        ts = (DAY_START + timedelta(hours=9, minutes=minute)).isoformat()
        return {"type": kind, "sessionId": "s", "timestamp": ts,
                "cwd": "/home/user/myproj",
                "message": {"role": kind, "content": text_or_content}}

    records = [
        rec("user", "バグを直してください", 0),
        rec("user", [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}], 1),  # 除外
        rec("user", "短い", 2),                                     # 8文字未満→除外
        rec("user", "<command-name>/foo</command-name>", 3),        # コマンドラッパー→除外
        rec("user", "[Request interrupted by user]", 4),            # 中断→除外
        rec("assistant", "応答です応答です", 5),                     # assistant→除外
        rec("user", "テストも追加してください", 6),
    ]
    p = tmp_path / "-home-user-myproj" / "s.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

    prompts = scan_user_prompts(tmp_path, DAY_START, DAY_END)
    assert [x.text for x in prompts] == ["バグを直してください", "テストも追加してください"]
    assert prompts[0].project == "myproj"
