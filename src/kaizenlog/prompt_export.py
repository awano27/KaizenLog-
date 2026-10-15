"""Export observed prompt improvements as editable drafts, without adoption writes."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .file_lock import file_lock, lock_path_for
from .privacy_filter import is_private_block
from .vault import atomic_write_bytes


@dataclass(frozen=True)
class ExportResult:
    path: Path
    content: str
    status: str
    backup: Path | None = None


def export_prompt(project: Path, row, example: str, *, name: str | None = None,
                  write: bool = False, force: bool = False, redactor=None,
                  private_title_patterns=()) -> ExportResult:
    before, after = row.skilled_before, row.skilled_after
    if (row.skilled_pending or row.entry.status != 'skilled'
            or isinstance(before, bool) or isinstance(after, bool)
            or not isinstance(before, int) or not isinstance(after, int)
            or not (before > after >= 0)):
        raise ValueError('前後30日の観測が完了し、再発の減少が確認されたPRMだけを出力できます。')
    if not re.fullmatch(r'PRM-\d{8}-\d{3,}', row.entry.id):
        raise ValueError('PRM-IDが不正です。')
    name = name if name is not None else row.entry.id.lower()
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{prefix}{n}' for prefix in ('COM', 'LPT') for n in range(1, 10)}
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', name) or name.upper() in reserved:
        raise ValueError('名前は英数字・ハイフン・アンダースコアで指定してください。')
    if not isinstance(example, str) or not example.strip():
        raise ValueError('正規化前の依頼例がありません。代表文だけから下書きは生成しません。')
    if is_private_block(title=example, private_title_patterns=private_title_patterns):
        raise ValueError('私的な依頼例は出力しません。')
    example = redactor(example) if redactor else example
    fence = '`' * max(3, 1 + max((len(m[0]) for m in re.finditer(r'`+', example)), default=0))
    content = (f'# プロンプト下書き: {row.entry.id}\n\n'
               '必ず人が編集してから使用してください。出力だけでは採用状態を変更しません。\n\n'
               f'前後30日の観測: 再発 {before}回 → {after}回。因果関係を示すものではありません。\n\n'
               '変数: {{target}} を実際の対象へ置き換え、下の依頼例を編集してください。\n\n'
               f'{fence}text\n{example.rstrip()}\n{fence}\n')
    root = Path(project).expanduser().resolve()
    path = root / '.claude' / 'commands' / f'{name}.md'
    if not path.resolve().is_relative_to(root):
        raise ValueError('出力先がリポジトリの外を指しています。')
    if not write:
        return ExportResult(path, content, 'preview')
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(lock_path_for(path)):
        payload = content.encode('utf-8')
        old = path.read_bytes() if path.exists() else None
        if old == payload:
            return ExportResult(path, content, 'unchanged')
        backup = None
        if old is not None:
            if not force:
                raise ValueError('既存の下書きが異なります。確認後、--force でバックアップ付き置換できます。')
            number = 0
            while True:
                backup = path.with_name(path.name + '.bak' + (f'.{number}' if number else ''))
                try:
                    with backup.open('xb') as stream:
                        stream.write(old)
                    break
                except FileExistsError:
                    number += 1
        atomic_write_bytes(path, payload)
    return ExportResult(path, content, 'written', backup)
