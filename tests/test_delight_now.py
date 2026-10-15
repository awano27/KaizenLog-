from datetime import datetime
from zoneinfo import ZoneInfo

from kaizenlog.config import Config


class FakeAW:
    def buckets(self):
        return {'window': {'type': 'currentwindow'}, 'afk': {'type': 'afkstatus'}}

    def find_buckets(self, kind):
        return [key for key, val in self.buckets().items() if val['type'] == kind]

    def find_bucket(self, kind):
        return next(iter(self.find_buckets(kind)), None)

    def events(self, bucket, start, end):
        assert end.hour == 10 and end.minute == 0
        return [{'timestamp': '2026-09-06T09:00:00+09:00', 'duration': 7200,
                 'data': {'app': 'editor', 'title': 'customer-secret work'} if bucket == 'window'
                         else {'status': 'not-afk'}}]


def test_now_clips_to_present_redacts_and_never_writes(tmp_path):
    from kaizenlog.now import render_now
    cfg = Config(vault_dir=tmp_path)
    cfg.aiwork.enabled = False
    cfg.privacy.redact_patterns = ['customer-secret']
    before = list(tmp_path.rglob('*'))
    body = render_now(cfg, now=datetime(2026, 9, 6, 10, tzinfo=ZoneInfo('Asia/Tokyo')), client=FakeAW())
    assert '1h00m' in body
    assert 'customer-secret' not in body
    assert '未判定' in body and 'aw-watcher-input' in body
    assert before == list(tmp_path.rglob('*'))


def test_now_cli_is_registered():
    from kaizenlog.cli import build_parser
    assert build_parser().parse_args(['now']).command == 'now'
