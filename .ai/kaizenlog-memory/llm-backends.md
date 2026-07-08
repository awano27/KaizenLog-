# 実装判断: LLMバックエンドの抽象化

- 共通経路は `advisor.generate_text(cfg, system, user)` 一本。リトライ（既定2回/20秒）もここで一元化
- 4バックエンド: claude-code-cli / copilot-cli（どちらもsubprocess -p方式） / openai-compatible（GitHub Models・Ollama・社内ゲートウェイ） / none
- OpenAI互換の疎通確認は GET {base_url}/models（doctorで使用。Ollama/GitHub Models両対応）
- GitHub Models: PATに models:read 権限、無料枠は日次上限あり（夜間バッチには十分）
- 選定指針: 品質=Claude Code/Copilot、完全ローカル=Ollama(qwen3系が日本語良)、企業=社内ゲートウェイをopenai-compatibleで
