"""Parse executable documentation examples without executing any command."""
import re
import shlex
from pathlib import Path

import pytest

from kaizenlog.cli import build_parser


def examples(text):
    for block in re.findall(r'```[^\n]*\n(.*?)```', text, re.S):
        block = re.sub(r'[`\\]\s*\n\s*', ' ', block)
        for line in block.splitlines():
            if re.match(r'^\s*kaizenlog(?:\s|$)', line):
                yield shlex.split(line, comments=True, posix=True)[1:]


@pytest.mark.parametrize('document', ['README.md', 'docs/USAGE.md'])
def test_documented_commands_are_accepted_by_real_parser(document):
    root = Path(__file__).resolve().parents[1]
    parser = build_parser()
    commands = list(examples((root / document).read_text(encoding='utf-8')))
    assert commands
    for args in commands:
        if '--all' in args and '--help' in args:
            args.remove('--all')
        try:
            parser.parse_args(args)
        except SystemExit as exc:
            assert exc.code == 0, f'{document}: {args}'


def test_lint_rejects_nonexistent_command_and_unknown_flag():
    parser = build_parser()
    for args in examples('```powershell\nkaizenlog prompts skill PRM-1\nkaizenlog today --imaginary\n```'):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(args)
        assert exc.value.code == 2
