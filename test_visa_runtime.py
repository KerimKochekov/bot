import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from visa_runtime import CREDENTIAL_KEYS, load_environment, telegram


class RuntimeTests(unittest.TestCase):
    def test_json_credentials_support_password_punctuation_without_shell_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'credentials.json'
            values = dict(zip(CREDENTIAL_KEYS, ['user@example.com', 'a!# " $()\\xyz', 'token', '123']))
            config.write_text(json.dumps(values))
            with patch.dict(os.environ, {'VISA_CONFIG_FILE': str(config)}, clear=True):
                load_environment()
                self.assertEqual(os.environ['PRENOTAMI_PASSWORD'], values['PRENOTAMI_PASSWORD'])
                self.assertNotIn('AIRLINE_PASSWORD', os.environ)

    def test_cloud_config_rejects_unrelated_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'credentials.json'
            config.write_text(json.dumps({'AIRLINE_PASSWORD': 'not-for-the-visa-server'}))
            with patch.dict(os.environ, {'VISA_CONFIG_FILE': str(config)}, clear=True):
                with self.assertRaisesRegex(RuntimeError, 'missing or invalid'):
                    load_environment()
                self.assertNotIn('AIRLINE_PASSWORD', os.environ)

    def test_environment_override_takes_precedence_over_saved_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'credentials.json'
            config.write_text(json.dumps({'TELEGRAM_BOT_TOKEN': 'saved'}))
            with patch.dict(os.environ, {'VISA_CONFIG_FILE': str(config),
                                         'TELEGRAM_BOT_TOKEN': 'override'}, clear=True):
                load_environment()
                self.assertEqual(os.environ['TELEGRAM_BOT_TOKEN'], 'override')

    def test_telegram_errors_do_not_expose_bot_tokens(self):
        secret = 'private-test-token'
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': secret}), patch(
                'visa_runtime.urlopen', side_effect=OSError('https://api.telegram.org/bot' + secret)):
            with self.assertRaises(RuntimeError) as result:
                telegram('sendMessage', {'chat_id': '123', 'text': 'test'})
            self.assertNotIn(secret, str(result.exception))


if __name__ == '__main__':
    unittest.main()
