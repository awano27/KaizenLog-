# 確認済み仕様: ActivityWatch REST API

- バケット一覧: GET /api/0/buckets/（typeフィールド: currentwindow / afkstatus）
- イベント: GET /api/0/buckets/{id}/events?start=ISO&end=ISO&limit=-1（timestampはUTC、durationは秒）
- AFK除外は自前実装: not-afk区間とウィンドウイベントの区間交差（collector.clip_to_active）。AWのquery APIは使っていない（複雑さ回避）
- AWは履歴を保持するため、過去日の再集計（backfill）が可能
- 日付境界はconfigのtimezone（既定Asia/Tokyo）でローカル日付として扱う
