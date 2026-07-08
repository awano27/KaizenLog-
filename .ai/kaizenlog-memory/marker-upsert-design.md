# 実装判断: マーカーupsert方式（手書きノートを絶対に壊さない）

- デイリーノートへの書き込みは必ず `<!-- kaizenlog:<name>:start/end -->` 区間のみ置換（vault.upsert_section）
- 区間が無ければ末尾に追加。手書き部分・frontmatter・他ツールのセクションには触れない
- マーカー名は後方互換を維持: activity / advice / nippou / measurements。**改名はしない**（過去ノートの重複を生む）
- v1.3でadviceのセクション構成を変えたが、マーカー名はadviceのまま維持した
- 全書き込み経路はE2Eスモークで「手書きメモ保持」「再実行で重複なし」を検証している
