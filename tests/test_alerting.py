"""Stop-the-bleeding change 5: stale-success alert and optional notify URL."""
import argparse
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

    def test_config_override_only_raises_schedule_floor(self):
        c = {'alert': {'max_success_age': '36h'}}
        self.assertEqual(
            backup_state.success_age_threshold(c, DAILY, NOW), 49 * 3600,
        )
        c = {'alert': {'max_success_age': '72h'}}
        self.assertEqual(
            backup_state.success_age_threshold(c, DAILY, NOW), 72 * 3600,
        )

    def test_global_override_does_not_false_alert_weekly_jobs(self):
        weekly = {'service': 'w', 'schedule': {'cron': '0 4 * * 0'}}
        c = {'alert': {'max_success_age': '36h'}}
        self.assertEqual(
            backup_state.success_age_threshold(c, weekly, NOW),
            14 * 86400 + 3600,
        )
        self.assertFalse(backup_state.success_staleness(
            c, weekly, {'last_success_at': ago(days=6)}, NOW,
        )['stale'])

    def test_without_cron_the_global_value_applies(self):
        no_cron = {'service': 'n', 'schedule': {}}
        c = {'alert': {'max_success_age': '36h'}}
        self.assertEqual(
            backup_state.success_age_threshold(c, no_cron, NOW), 36 * 3600,
        )
        self.assertIsNone(backup_state.success_age_threshold({}, no_cron, NOW))

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
    def _notify(self, url, *, ok=False, recovered=False, opener=None):
        sent = []
        opener = opener or (lambda target, body, headers: sent.append(
            (target, body, headers)))
        with mock.patch.object(alerting, 'notification_url', return_value=url):
            delivered = alerting.notify(
                {}, 'Title', 'body', ok=ok, recovered=recovered, opener=opener,
            )
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
        self.assertIn(
            'WARNING: notification delivery failed: URLError', err.getvalue(),
        )

    def test_any_delivery_exception_is_caught_without_leaking_url(self):
        secret = 'https://ntfy.sh/secret-topic'
        for error in (
            urllib.error.URLError(f'bad {secret}'),
            RuntimeError(f'unexpected {secret}'),
            KeyError(secret),
        ):
            def boom(*_args, error=error):
                raise error

            with self.subTest(error=type(error).__name__), \
                    redirect_stderr(StringIO()) as err:
                delivered, _ = self._notify(secret, opener=boom)
            self.assertFalse(delivered)
            self.assertEqual(
                err.getvalue().strip(),
                f'WARNING: notification delivery failed: {type(error).__name__}',
            )
            self.assertNotIn('secret-topic', err.getvalue())

    def test_ntfy_recovery_is_a_normal_priority_message(self):
        delivered, sent = self._notify(
            'https://ntfy.sh/t', ok=True, recovered=True,
        )
        self.assertTrue(delivered)
        self.assertEqual(sent[0][2]['Priority'], 'default')

    def test_url_is_read_from_protected_file(self):
        c = {'alert': {'notify_url_file': '/etc/homelab-backup/notify-url'}}
        with mock.patch.object(
            alerting, 'read_control_text',
            return_value='\nhttps://ntfy.sh/topic\n',
        ) as read:
            self.assertEqual(alerting.notification_url(c), 'https://ntfy.sh/topic')
        read.assert_called_once_with(
            '/etc/homelab-backup/notify-url', private=True,
        )
        with mock.patch.object(
            alerting, 'read_control_text', return_value='not a url',
        ), redirect_stderr(StringIO()):
            self.assertIsNone(alerting.notification_url(c))
        with mock.patch.object(
            alerting, 'read_control_text', side_effect=FileNotFoundError,
        ), redirect_stderr(StringIO()):
            self.assertIsNone(alerting.notification_url(c))

    def test_plain_http_url_is_rejected(self):
        c = {'alert': {'notify_url_file': '/etc/homelab-backup/notify-url'}}
        with mock.patch.object(
            alerting, 'read_control_text', return_value='http://ntfy.sh/topic\n',
        ), redirect_stderr(StringIO()) as err:
            self.assertIsNone(alerting.notification_url(c))
        self.assertIn('https', err.getvalue())
        self.assertNotIn('ntfy.sh/topic', err.getvalue())

    def _url_file(self, tmp, mode):
        # Temp files are owned by the test user, which read_control_text
        # accepts for non-root callers just like root ownership in production.
        path = Path(tmp) / 'notify-url'
        path.write_text('https://ntfy.sh/secret-topic\n', encoding='utf-8')
        path.chmod(mode)
        return {'alert': {'notify_url_file': str(path)}}

    def test_private_url_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = self._url_file(tmp, 0o600)
            self.assertEqual(
                alerting.notification_url(c), 'https://ntfy.sh/secret-topic',
            )

    def test_group_or_world_readable_url_file_is_refused(self):
        for mode in (0o640, 0o644, 0o604):
            with self.subTest(mode=oct(mode)), \
                    tempfile.TemporaryDirectory() as tmp, \
                    redirect_stderr(StringIO()) as err:
                c = self._url_file(tmp, mode)
                self.assertIsNone(alerting.notification_url(c))
            self.assertIn('WARNING: notification URL file is unusable', err.getvalue())
            self.assertIn('group/others', err.getvalue())
            self.assertNotIn('secret-topic', err.getvalue())


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

    def test_renotify_after_is_accepted(self):
        loaded = self._load({'renotify_after': '12h'})
        self.assertEqual(alerting.renotify_after_seconds(loaded), 12 * 3600)
        self.assertEqual(alerting.renotify_after_seconds({}), 24 * 3600)

    def test_invalid_alert_values_are_rejected(self):
        for alert in (
            {'renotify_after': 'often'},
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

    def test_alert_is_logged_before_config_loading_fails(self):
        def broken_cfg():
            raise SystemExit(1)

        with mock.patch.object(cli.os, 'geteuid', return_value=0), \
                mock.patch.object(cli, 'cfg', side_effect=broken_cfg), \
                mock.patch.object(cli, 'cmd_notify_failure') as handler, \
                redirect_stderr(StringIO()) as err, \
                self.assertRaises(SystemExit):
            cli.main(['notify-failure', 'homelab-backup.service'])
        handler.assert_not_called()
        self.assertIn(
            'ALERT: systemd unit failed: homelab-backup.service', err.getvalue(),
        )

    def test_alert_is_printed_once_through_the_cli(self):
        with mock.patch.object(cli.os, 'geteuid', return_value=0), \
                mock.patch.object(cli, 'cfg', return_value={'host_id': 'h'}), \
                mock.patch.object(
                    maintenance, 'run', return_value=mock.Mock(stdout=''),
                ), mock.patch.object(maintenance, 'notify', return_value=True), \
                redirect_stderr(StringIO()) as err:
            cli.main(['notify-failure', 'homelab-backup.service'])
        self.assertEqual(err.getvalue().count('ALERT: systemd unit failed'), 1)

    def test_help_mentions_journal_lines(self):
        parser = cli.build_parser()
        sub = next(
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        help_text = ' '.join(sub.choices['notify-failure'].format_help().split())
        self.assertIn('last 30 journal lines', help_text)


class AlertThrottleTests(unittest.TestCase):
    """Change-only status notifications and per-unit failure throttling."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_root = Path(tmp.name) / 'state'
        self.c = {'host_id': 'h', 'state_root': str(self.state_root)}
        self.now = NOW

    def _status(self, states, *, at=None):
        """Run status --notify; ``states`` maps service -> backup state."""
        manifests = [
            {'service': name, 'schedule': {'cron': '0 4 * * *'}}
            for name in states
        ]
        args = mock.Mock(notify=True, max_age=None)
        with mock.patch.object(maintenance, 'manifests', return_value=manifests), \
                mock.patch.object(
                    maintenance, 'local_now', return_value=at or self.now,
                ), mock.patch.object(
                    maintenance, 'due_status', return_value=(False, 'next', None),
                ), mock.patch.object(
                    maintenance, 'load_state',
                    side_effect=lambda _c, service: states[service],
                ), mock.patch.object(
                    maintenance, 'notify', return_value=True,
                ) as notify, redirect_stdout(StringIO()), \
                redirect_stderr(StringIO()):
            try:
                maintenance.cmd_status(self.c, args)
            except SystemExit:
                pass
        return notify

    def _notify_failure(self, unit, *, at=None, delivered=True):
        with mock.patch.object(
            maintenance, 'run', return_value=mock.Mock(stdout='tail'),
        ), mock.patch.object(
            maintenance, 'local_now', return_value=at or self.now,
        ), mock.patch.object(
            maintenance, 'notify', return_value=delivered,
        ) as notify, redirect_stderr(StringIO()) as err:
            maintenance.cmd_notify_failure(self.c, mock.Mock(unit=unit))
        self.assertIn(f'ALERT: systemd unit failed: {unit}', err.getvalue())
        return notify

    STALE = {'last_success_at': ago(days=5)}
    FRESH = {'last_success_at': ago(hours=2)}

    def test_alert_state_name_can_never_be_a_service(self):
        from homelab_backup.manifest import valid_service_name
        name = alerting.ALERT_STATE_NAME
        self.assertTrue(name.endswith('.json'))
        self.assertFalse(valid_service_name(name[:-len('.json')]))

    def test_first_alert_sends_and_repeat_is_suppressed(self):
        self.assertEqual(self._status({'a': self.STALE}).call_count, 1)
        self.assertTrue((self.state_root / '_alerts.json').exists())
        later = self.now + dt.timedelta(hours=1)
        self.assertEqual(self._status({'a': self.STALE}, at=later).call_count, 0)

    def test_changed_signature_sends(self):
        self._status({'a': self.STALE, 'b': self.FRESH})
        notify = self._status(
            {'a': self.STALE, 'b': {'last_success_at': ago(days=4)}},
            at=self.now + dt.timedelta(minutes=30),
        )
        notify.assert_called_once()
        self.assertIn('b:', notify.call_args.args[2])

    def test_unchanged_problem_renotifies_after_window(self):
        self._status({'a': self.STALE})
        notify = self._status(
            {'a': self.STALE}, at=self.now + dt.timedelta(hours=25),
        )
        notify.assert_called_once()
        self.c['alert'] = {'renotify_after': '1h'}
        notify = self._status(
            {'a': self.STALE}, at=self.now + dt.timedelta(hours=26, minutes=1),
        )
        notify.assert_called_once()

    def test_recovery_message_is_sent_once(self):
        self._status({'a': self.STALE})
        notify = self._status({'a': self.FRESH}, at=self.now + dt.timedelta(hours=1))
        notify.assert_called_once()
        self.assertTrue(notify.call_args.kwargs['ok'])
        self.assertTrue(notify.call_args.kwargs['recovered'])
        self.assertIn('recovered', notify.call_args.args[1])
        # Later OK runs are plain heartbeats (healthchecks pings, ntfy skips).
        notify = self._status({'a': self.FRESH}, at=self.now + dt.timedelta(hours=2))
        notify.assert_called_once()
        self.assertTrue(notify.call_args.kwargs['ok'])
        self.assertFalse(notify.call_args.kwargs['recovered'])

    def test_problem_after_recovery_alerts_again(self):
        self._status({'a': self.STALE})
        self._status({'a': self.FRESH}, at=self.now + dt.timedelta(hours=1))
        notify = self._status({'a': self.STALE}, at=self.now + dt.timedelta(hours=2))
        notify.assert_called_once()
        self.assertNotIn('ok', notify.call_args.kwargs)

    def test_undelivered_problem_is_retried_next_run(self):
        args =mock.Mock(notify=True, max_age=None)
        with mock.patch.object(
            maintenance, 'manifests',
            return_value=[{'service': 'a', 'schedule': {'cron': '0 4 * * *'}}],
        ), mock.patch.object(maintenance, 'local_now', return_value=self.now), \
                mock.patch.object(
                    maintenance, 'due_status', return_value=(False, 'n', None),
                ), mock.patch.object(
                    maintenance, 'load_state', return_value=self.STALE,
                ), mock.patch.object(maintenance, 'notify', return_value=False), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()), \
                self.assertRaises(SystemExit):
            maintenance.cmd_status(self.c, args)
        self.assertFalse((self.state_root / '_alerts.json').exists())
        self.assertEqual(self._status({'a': self.STALE}).call_count, 1)

    def test_corrupted_state_file_still_notifies(self):
        self.state_root.mkdir(mode=0o700)
        (self.state_root / '_alerts.json').write_text('{not json', encoding='utf-8')
        with redirect_stderr(StringIO()) as err:
            self.assertEqual(alerting.load_alert_state(self.c), {})
        self.assertIn('WARNING', err.getvalue())
        self.assertEqual(self._status({'a': self.STALE}).call_count, 1)
        self._notify_failure('homelab-backup.service').assert_called_once()

    def test_unwritable_state_still_notifies(self):
        with mock.patch.object(
            alerting, 'atomic_write_json', side_effect=OSError('read-only'),
        ):
            notify = self._status({'a': self.STALE})
        notify.assert_called_once()

    def test_unit_failure_is_throttled_per_unit(self):
        self._notify_failure('homelab-backup.service').assert_called_once()
        self._notify_failure(
            'homelab-backup.service', at=self.now + dt.timedelta(hours=1),
        ).assert_not_called()
        self._notify_failure(
            'homelab-backup-check.service', at=self.now + dt.timedelta(hours=1),
        ).assert_called_once()
        self._notify_failure(
            'homelab-backup.service', at=self.now + dt.timedelta(hours=25),
        ).assert_called_once()

    def test_undelivered_unit_failure_is_not_recorded(self):
        self._notify_failure('homelab-backup.service', delivered=False)
        self._notify_failure(
            'homelab-backup.service', at=self.now + dt.timedelta(minutes=5),
        ).assert_called_once()

    def test_status_unit_failure_skips_push_after_status_notified(self):
        self._status({'a': self.STALE})
        self._notify_failure(
            maintenance.STATUS_UNIT, at=self.now + dt.timedelta(minutes=1),
        ).assert_not_called()

    def test_status_unit_failure_pushes_when_status_did_not_notify(self):
        self._notify_failure(maintenance.STATUS_UNIT).assert_called_once()


if __name__ == '__main__':
    unittest.main()
