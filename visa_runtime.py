"""Private configuration and Telegram transport for the independent visa monitor."""
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE_ROOT = Path(__file__).resolve().parent
CREDENTIAL_KEYS = ('PRENOTAMI_USERNAME', 'PRENOTAMI_PASSWORD',
                   'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')


def read_dotenv(path):
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip()
    return values


def load_environment():
    config = os.environ.get('VISA_CONFIG_FILE')
    if config:
        try:
            values = json.loads(Path(config).read_text())
            if not isinstance(values, dict) or any(key not in CREDENTIAL_KEYS for key in values):
                raise ValueError
            if any(not isinstance(value, str) for value in values.values()):
                raise ValueError
        except (OSError, ValueError):
            raise RuntimeError('Visa credentials file is missing or invalid.') from None
    else:
        values = read_dotenv(SOURCE_ROOT / '.env')
        values = {key: value for key, value in values.items()
                  if key in CREDENTIAL_KEYS or key == 'PLAYWRIGHT_BROWSERS_PATH'
                  or key.startswith('VISA_')}
    for key, value in values.items():
        os.environ.setdefault(key, value)


def telegram(method, data):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise RuntimeError('Telegram bot token is not configured.')
    request = Request('https://api.telegram.org/bot' + token + '/' + method,
                      data=json.dumps(data).encode(),
                      headers={'Content-Type': 'application/json'})
    # Underlying request exceptions can include the bot token. Never expose them.
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        if not result.get('ok'):
            raise ValueError
        return result['result']
    except Exception:
        raise RuntimeError('Telegram delivery failed; check configuration and connectivity.') from None
