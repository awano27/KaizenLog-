# 実装判断: プライバシーレダクション

- マスクは**LLMへ送信するプロンプトにのみ**適用（privacy.make_redactor）。ボールト内の日誌・統計は常に原文
- 適用箇所: generate_advice / generate_nippou_llm / advise --dry-run（dry-runはマスク後を表示=監査手段）
- 不正な正規表現は起動時にPrivacyErrorで明確に失敗させる（黙って素通しさせない）
- 未適用の経路に注意: patterns/promptsコマンドはローカル表示のみなので未適用。将来これらをLLMに渡す機能を作るときは必ずredactorを通すこと
- 送信を最小化したいユーザーには system_prompt = "privacy_safe"（固有名詞の引用禁止を指示）を併用
