import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

import visa_actions as actions
from visa_watcher import AuthenticationRejected, CheckFailed


class HostedStateTests(unittest.TestCase):
    def test_encrypted_state_roundtrip_and_tamper_rejection(self):
        key = Fernet.generate_key().decode()
        state = actions.fresh_state()
        state['browser'] = {'cookies': [{'value': 'private-cookie-value'}], 'origins': []}
        state['notifications'] = {'calendar_alerted': True}
        state['login_blocked_for'] = 'fingerprint'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.enc'
            actions.save_checkpoint(path, key, state)
            self.assertNotIn(b'private-cookie-value', path.read_bytes())
            self.assertEqual(actions.load_checkpoint(path, key), state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(CheckFailed):
                actions.load_checkpoint(path, Fernet.generate_key().decode())
            path.write_bytes(path.read_bytes()[:-10] + b'changed')
            with self.assertRaises(CheckFailed):
                actions.load_checkpoint(path, key)

    @patch.dict(os.environ, {'PRENOTAMI_USERNAME': 'fixture', 'PRENOTAMI_PASSWORD': 'fixture'})
    @patch('visa_actions.check_browser', side_effect=AuthenticationRejected('rejected'))
    def test_rejected_login_blocks_future_runs_and_allows_explicit_reset(self, browser):
        state = actions.fresh_state()
        with self.assertRaises(AuthenticationRejected):
            actions.perform_check(state)
        with self.assertRaises(AuthenticationRejected):
            actions.perform_check(state)
        self.assertEqual(browser.call_count, 1)
        with self.assertRaises(AuthenticationRejected):
            actions.perform_check(state, reset_login_block=True)
        self.assertEqual(browser.call_count, 2)
        with patch.dict(os.environ, {'PRENOTAMI_PASSWORD': 'corrected-fixture'}):
            with self.assertRaises(AuthenticationRejected):
                actions.perform_check(state)
        self.assertEqual(browser.call_count, 3)

    def test_only_official_domains_are_saved(self):
        state = actions.official_browser_state({
            'cookies': [{'domain': '.esteri.it'}, {'domain': 'prenotami.esteri.it'},
                        {'domain': 'attackeresteri.it'}, {'domain': 'example.com'}],
            'origins': [{'origin': 'https://iam.esteri.it'},
                        {'origin': 'https://example.com'}]})
        self.assertEqual(len(state['cookies']), 2)
        self.assertEqual(len(state['origins']), 1)

    def test_hosted_errors_never_expose_exception_details(self):
        error = RuntimeError('https://example.com/?secret=fixture-token')
        self.assertNotIn('fixture-token', str(actions.hosted_failure(error)))
        self.assertIn('unknown', str(actions.hosted_failure(error)))

    @patch('visa_watcher.write_json')
    @patch('visa_watcher.notify')
    def test_deduplication_survives_encrypted_checkpoint(self, notify, write):
        state = actions.fresh_state()
        state['notifications']['first_result_sent'] = True
        actions.watcher.report_result(state['notifications'], 'calendar_open')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.enc'
            key = Fernet.generate_key().decode()
            actions.save_checkpoint(path, key, state)
            restored = actions.load_checkpoint(path, key)
            actions.watcher.report_result(restored['notifications'], 'calendar_open')
        self.assertEqual(notify.call_count, 1)

    @patch('visa_actions.load_environment')
    @patch('visa_actions.watcher.write_json')
    @patch('visa_actions.watcher.verify_telegram')
    @patch('visa_actions.watcher.notify')
    @patch('visa_actions.check_browser', side_effect=CheckFailed('security check required'))
    def test_failed_hosted_runs_save_state_and_do_not_repeat_telegram_alerts(
            self, browser, notify, verify, write, load):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            env = {name: 'fixture-value' for name in actions.CREDENTIAL_KEYS}
            env.update(VISA_STATE_KEY=Fernet.generate_key().decode(),
                       VISA_ENCRYPTED_STATE=str(path / 'state.enc'),
                       GITHUB_OUTPUT=str(path / 'output'))
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(actions.main(), 1)
                self.assertEqual(actions.main(), 1)
            self.assertEqual(browser.call_count, 2)
            self.assertEqual(notify.call_count, 1)
            self.assertIn('state_saved=true', (path / 'output').read_text())
            self.assertIn('availability=unknown', (path / 'output').read_text())

    @patch('visa_actions.load_environment')
    @patch('visa_actions.check_browser')
    def test_corrupt_state_never_attempts_login(self, browser, load):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.enc'
            path.write_bytes(b'invalid encrypted checkpoint')
            env = {name: 'fixture-value' for name in actions.CREDENTIAL_KEYS}
            env.update(VISA_STATE_KEY=Fernet.generate_key().decode(),
                       VISA_ENCRYPTED_STATE=str(path))
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(actions.main(), 1)
            browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
