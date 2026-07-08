# 実装判断: スキルのパッケージ同梱とインストール

- スキルの正本は `src/kaizenlog/skills/<name>/SKILL.md`（package-dataとしてwheelに同梱、importlib.resourcesで読む）
- ボールトへの配置は `kaizenlog skill install`。**既存ファイルと差分があれば絶対に黙って上書きしない**（diff表示→--force時のみ.bak退避後に上書き）
- 将来スキルを更新したら: パッケージ側を編集 → ユーザーは skill doctor で「差分あり」を検知 → install --force
- リポジトリ直下の skills/ は廃止済み（v1.3で src/ へ移動）。二重管理しないこと
