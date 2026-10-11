"""Stop-the-bleeding change 5: stale-success alert and optional notify URL."""
import datetime as dt
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from homelab_backup import alerting, backup_state, cli, config, maintenance

TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 10, 11, 12, 0, tzinfo=TZ)
DAILY = {'service': 'demo', 'schedule': {'cron': '0 4 * * *'}}


def ago(**kwargs):
    return (NOW - dt.timedelta(**kwargs)).isoformat()


class StalenessTests(unittest.TestCase):
    def test_default_threshold_is_two_periods_plus_grace(self):
        self.assertEqual(
            backup_state.success_age_threshold({}, DAILY, NOW), 49 * 3600,
        )
        weekly = {'service': 'w', 'schedule': {'cron': '0 4 * * 0'}}
        self.assertEqual(
            backup_state.success_age_threshold({}, weekly, NOW),
            14 * 86400 + 3600,
        )

    def test_config_override_wins(self):
        c = {'alert': {'max_success_age': '36h'}}
        self.assertEqual(
            backup_state.success_age_threshold(c, DAILY, NOW), 36 * 3600,
        )

    def test_fresh_success_is_not_stale(self):
        result = backup_state.success_staleness(
            {}, DAILY, {'last_success_at': ago(hours=8)}, NOW,
        )
        self.assertFalse(result['stale'])
        self.assertEqual(result['last_success_age_seconds'], 8 * 3600)

    def test_success_older_than_threshold_is_stale(self):
        # The incident: last success 2026-08-11, two months ago.
        result = backup_state.success_staleness(
            {}, DAILY, {'last_success_at': '2026-08-11T04:00:00+08:00',
                        'last_result': 'failed'}, NOW,
        )
        self.assertTrue(result['stale'])

    def test_never_succeeded_counts_from_first_seen(self):
        self.assertTrue(backup_state.success_staleness(
            {}, DAILY, {'first_seen_at': ago(days=3)}, NOW,
        )['stale'])
        self.assertFalse(backup_state.success_staleness(
            {}, DAILY, {'first_seen_at': ago(hours=1)}, NOW,
        )['stale'])
        self.assertFalse(backup_state.success_staleness({}, DAILY, {}, NOW)['stale'])

    def test_disabled_schedule_never_alerts(self):
        m = {'service': 'd', 'schedule': {'cron': '0 4 * * *', 'enabled': False}}
        self.assertFalse(backup_state.success_staleness(
            {}, m, {'last_success_at': ago(days=90)}, NOW,
        )['stale'])

    def test_explicit_max_age_override(self):
        self.assertTrue(backup_state.success_staleness(
            {}, DAILY, {'last_success_at': ago(hours=8)}, NOW,
            max_age_seconds=3600,
        )['stale'])


class StatusCommandTests(unittest.TestCase):
    def _status(self, state, *, notify_flag=False, max_age=None):
        out, err = StringIO(), StringIO()
        args = mock.Mock(notify=notify_flag, max_age=max_age)
        with mock.patch.object(maintenance, 'manifests', return_value=[DAILY]), \
                mock.patch.object(maintenance, 'local_now', return_value=NOW), \
                mock.patch.object(
                    maintenance, 'due_status', return_value=(False, 'next', None),
                ), mock.patch.object(maintenance, 'load_state', return_value=state), \
                mock.patch.object(maintenance, 'notify') as notify, \
                redirect_stdout(out), redirect_stderr(err):
            code = 0
            try:
                maintenance.cmd_status({'host_id': 'h'}, args)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue(), notify

    def test_stale_status_exits_nonzero_and_prints_alert(self):
        code, out, err, notify = self._status(
            {'last_success_at': '2026-08-11T04:00:00+08:00',
             'last_result': 'failed', 'last_error': 'RuntimeError: boom'},
        )
        self.assertEqual(code, maintenance.STALE_EXIT_CODE)
        self.assertNotEqual(code, 0)
        self.assertTrue(json.loads(out)['stale'])
        self.assertIn('ALERT: demo', err)
        self.assertIn('RuntimeError: boom', err)
        notify.assert_not_called()

    def test_stale_status_with_notify_sends_problem(self):
        code, _, _, notify = self._status(
            {'last_success_at': ago(days=5)}, notify_flag=True,
        )
        self.assertEqual(code, 2)
        notify.assert_called_once()
        self.assertIn('stale', notify.call_args.args[1])
        self.assertNotIn('ok', notify.call_args.kwargs)

    def test_fresh_status_exits_zero_and_pings_ok_when_notifying(self):
        code, out, _, notify = self._status(
            {'last_success_at': ago(hours=2)}, notify_flag=True,
        )
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)['stale'])
        notify.assert_called_once()
        self.assertTrue(notify.call_args.kwargs['ok'])

    def test_max_age_flag_overrides_threshold(self):
        code, _, _, _ = self._status(
            {'last_success_at': ago(hours=2)}, max_age='1h',
        )
        self.assertEqual(code, 2)

    def test_status_parser_accepts_new_flags(self):
        args = cli.build_parser().parse_args(['status', '--notify', '--max-age', '36h'])
        self.assertTrue(args.notify)
        self.assertEqual(args.max_age, '36h')


class NotifyTests(unittest.TestCase):
    def _notify(self, url, *, ok=False, opener=None):
        sent = []
        opener = opener or (lambda target, body, headers: sent.append(
            (target, body, headers)))
        with mock.patch.object(alerting, 'notification_url', return_value=url):
            delivered = alerting.notify({}, 'Title', 'body', ok=ok, opener=opener)
        return delivered, sent

    def test_ntfy_problem_is_posted_with_title(self):
        delivered, sent = self._notify('https://ntfy.sh/secret-topic')
        self.assertTrue(delivered)
        target, body, headers = sent[0]
        self.assertEqual(target, 'https://ntfy.sh/secret-topic')
        self.assertEqual(headers['Title'], 'Title')
        self.assertIn(b'body', body)

    def test_ntfy_skips_ok_messages(self):
        delivered, sent = self._notify('https://ntfy.sh/t', ok=True)
        self.assertFalse(delivered)
        self.assertEqual(sent, [])

    def test_healthchecks_fail_and_success_endpoints(self):
        _, sent = self._notify('https://hc-ping.com/uuid')
        self.assertEqual(sent[0][0], 'https://hc-ping.com/uuid/fail')
        _, sent = self._notify('https://hc-ping.com/uuid', ok=True)
        self.assertEqual(sent[0][0], 'https://hc-ping.com/uuid')
        self.assertEqual(
            alerting.notification_kind('https://hc.example.org/ping/abc'),
            'healthchecks',
        )

    def test_no_url_configured_is_a_quiet_noop(self):
        self.assertIsNone(alerting.notification_url({}))
        delivered, sent = self._notify(None)
        self.assertFalse(delivered)
        self.assertEqual(sent, [])

    def test_delivery_failure_is_only_a_warning(self):
        def boom(*_args):
            raise urllib.error.URLError('offline')

        with redirect_stderr(StringIO()) as err:
            delivered, _ = self._notify('https://ntfy.sh/t', opener=boom)
        self.assertFalse(delivered)
        self.assertIn('notification delivery failed', err.getvalue())

    def test_url_is_read_from_protected_file(self):
        c = {'alert': {'notify_url_file': '/etc/homelab-backup/notify-url'}}
        with mock.patch.object(
            alerting, 'read_control_text',
            return_value='\nhttps://ntfy.sh/topic\n',
        ) as read:
            self.assertEqual(alerting.notification_url(c), 'https://ntfy.sh/topic')
        read.assert_called_once_with('/etc/homelab-backup/notify-url')
        with mock.patch.object(
            alerting, 'read_control_text', return_value='not a url',
        ), redirect_stderr(StringIO()):
            self.assertIsNone(alerting.notification_url(c))
        with mock.patch.object(
            alerting, 'read_control_text', side_effect=FileNotFoundError,
        ), redirect_stderr(StringIO()):
            self.assertIsNone(alerting.notification_url(c))


class AlertConfigTests(unittest.TestCase):
    def _load(self, alert):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'config.yaml'
            path.touch()
            data = {
                'version': 1, 'host_id': 'host', 'repository': 'repo',
                'password_file': str(root / 'password'),
                'rclone_config': str(root / 'rclone.conf'),
                'cache_root': str(root / 'cache'),
                'volume_helper_image': 'helper',
                'state_root': str(root / 'state'),
                'lock_file': str(root / 'lock'),
                'services_root': str(root / 'services'),
                'staging_root': str(root / 'staging'),
                'restore_root': str(root / 'restore'),
                'alert': alert,
            }
            with mock.patch.object(config, 'CFG', path), \
                    mock.patch.object(config, 'load_yaml', return_value=data), \
                    redirect_stderr(StringIO()):
                return config.cfg()

    def test_alert_section_is_accepted(self):
        loaded = self._load({
            'max_success_age': '36h',
            'notify_url_file': '/etc/homelab-backup/notify-url',
        })
        self.assertEqual(loaded['alert']['max_success_age'], '36h')

    def test_invalid_alert_values_are_rejected(self):
        for alert in (
            {'max_success_age': 'soon'},
            {'notify_url_file': 'relative/path'},
            {'unknown': 1},
        ):
            with self.subTest(alert=alert), self.assertRaises(SystemExit):
                self._load(alert)


class NotifyFailureCommandTests(unittest.TestCase):
    def test_notify_failure_bypasses_global_lock(self):
        with mock.patch.object(cli.os, 'geteuid', return_value=0), \
                mock.patch.object(cli, 'cfg', return_value={'host_id': 'h'}), \
                mock.patch.object(
                    cli, 'GlobalLock', side_effect=AssertionError('locked'),
                ), mock.patch.object(cli, 'cmd_notify_failure') as handler:
            cli.main(['notify-failure', 'homelab-backup.service'])
        self.assertEqual(handler.call_args.args[1].unit, 'homelab-backup.service')

    def test_notify_failure_sends_unit_and_journal_tail(self):
        with mock.patch.object(
            maintenance, 'run', return_value=mock.Mock(stdout='line1\nline2\n'),
        ) as run, mock.patch.object(maintenance, 'notify', return_value=True) as notify, \
                redirect_stderr(StringIO()):
            maintenance.cmd_notify_failure(
                {'host_id': 'h'}, mock.Mock(unit='homelab-backup.service'),
            )
        self.assertIn('homelab-backup.service', run.call_args.args[0])
        self.assertIn('homelab-backup.service failed', notify.call_args.args[1])
        self.assertIn('line2', notify.call_args.args[2])


if __name__ == '__main__':
    unittest.main()
