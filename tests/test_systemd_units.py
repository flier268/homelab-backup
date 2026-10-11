"""Stop-the-bleeding change 4: separate check unit and OnFailure= alerts."""
import configparser
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / 'systemd'
NOTIFY = 'homelab-backup-notify@.service'
ALERTING_SERVICES = (
    'homelab-backup.service',
    'homelab-backup-maintenance.service',
    'homelab-backup-check.service',
    'homelab-backup-status.service',
)


def unit(name):
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string((SYSTEMD / name).read_text(encoding='utf-8'))
    return parser


def bash_array(script, name):
    body = re.search(rf'^{name}=\(\n(.*?)^\)', script, re.S | re.M).group(1)
    return [line.strip() for line in body.splitlines() if line.strip()]


class SystemdUnitTests(unittest.TestCase):
    def test_backup_and_maintenance_services_alert_on_failure(self):
        for name in ALERTING_SERVICES:
            with self.subTest(unit=name):
                self.assertEqual(
                    unit(name)['Unit']['OnFailure'],
                    'homelab-backup-notify@%n.service',
                )

    def test_oneshot_services_time_out(self):
        expected = {
            'homelab-backup.service': '12h',
            'homelab-backup-maintenance.service': '12h',
            'homelab-backup-check.service': '12h',
            'homelab-backup-status.service': '15min',
            NOTIFY: '5min',
        }
        for name, timeout in expected.items():
            with self.subTest(unit=name):
                service = unit(name)['Service']
                self.assertEqual(service['Type'], 'oneshot')
                self.assertEqual(service['TimeoutStartSec'], timeout)

    def test_check_has_its_own_service_and_timer(self):
        service = unit('homelab-backup-check.service')['Service']
        self.assertEqual(service['ExecStart'], '/usr/local/sbin/backupctl check')
        self.assertEqual(service['Type'], 'oneshot')
        timer = unit('homelab-backup-check.timer')
        self.assertIn('OnCalendar', timer['Timer'])
        self.assertEqual(timer['Install']['WantedBy'], 'timers.target')

    def test_maintenance_no_longer_runs_check(self):
        text = (SYSTEMD / 'homelab-backup-maintenance.service').read_text()
        self.assertNotIn('ExecStopPost', text)
        self.assertNotIn('backupctl check', text)

    def test_status_timer_runs_stale_alert(self):
        service = unit('homelab-backup-status.service')['Service']
        # --notify so healthchecks receives success pings.
        self.assertEqual(
            service['ExecStart'], '/usr/local/sbin/backupctl status --notify',
        )
        self.assertIn('OnCalendar', unit('homelab-backup-status.timer')['Timer'])

    def test_backup_timeout_stays_below_default_max_lock_age(self):
        from homelab_backup import maintenance, schedule
        timeout = unit('homelab-backup.service')['Service']['TimeoutStartSec']
        self.assertLess(
            schedule.parse_duration(timeout, 'TimeoutStartSec'),
            schedule.parse_duration(
                maintenance.DEFAULT_MAX_LOCK_AGE, 'alert.max_lock_age',
            ),
        )

    def test_notify_template_does_not_recurse(self):
        notify = unit(NOTIFY)
        self.assertNotIn('OnFailure', notify['Unit'])
        self.assertEqual(
            notify['Service']['ExecStart'],
            '/usr/local/sbin/backupctl notify-failure %i',
        )

    def test_installer_and_uninstaller_ship_every_unit(self):
        shipped = sorted(path.name for path in SYSTEMD.iterdir())
        install = (ROOT / 'install.sh').read_text(encoding='utf-8')
        uninstall = (ROOT / 'uninstall.sh').read_text(encoding='utf-8')
        self.assertEqual(sorted(bash_array(install, 'UNIT_NAMES')), shipped)
        self.assertEqual(sorted(bash_array(uninstall, 'UNIT_NAMES')), shipped)
        self.assertEqual(
            sorted(bash_array(uninstall, 'TIMER_NAMES')),
            sorted(n for n in shipped if n.endswith('.timer')),
        )
        self.assertIn(
            'homelab-backup-check.service', bash_array(uninstall, 'SERVICE_NAMES'),
        )

    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze missing')
    def test_units_pass_systemd_analyze_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            for path in SYSTEMD.iterdir():
                text = path.read_text(encoding='utf-8').replace(
                    '/usr/local/sbin/backupctl', '/bin/true',
                )
                (Path(tmp) / path.name).write_text(text, encoding='utf-8')
            instance = Path(tmp) / 'homelab-backup-notify@x.service'
            instance.write_text((Path(tmp) / NOTIFY).read_text())
            files = [
                str(p) for p in Path(tmp).iterdir() if not p.name.endswith('@.service')
            ]
            result = subprocess.run(
                ['systemd-analyze', 'verify', '--man=no', *files],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
