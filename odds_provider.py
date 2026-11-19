"""The Odds API access shared by Streamlit and the daily scan."""

import threading
import time
from email.utils import parsedate_to_datetime

import requests


class ProviderError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def retry_delay(value):
    if value:
        try:
            return max(30, int(value))
        except ValueError:
            try:
                return max(30, parsedate_to_datetime(value).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
    return 30


class OddsAPI:
    def __init__(self, key):
        self.key = key
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.cooldown_until = 0.0

    def get(self, path, **params):
        with self.lock:
            now = time.monotonic()
            if now < self.cooldown_until:
                raise ProviderError('rate_limited', 'Provider cooldown is active. Try again later.')
            time.sleep(max(0, self.next_request - now))
            self.next_request = time.monotonic() + 0.5
            try:
                response = requests.get(
                    'https://api.the-odds-api.com/v4/' + path,
                    headers={'x-api-key': self.key}, params=params,
                    timeout=15, allow_redirects=False,
                )
            except requests.RequestException:
                raise ProviderError('network_error', 'Could not reach The Odds API.') from None
            if response.status_code == 429:
                self.cooldown_until = time.monotonic() + retry_delay(response.headers.get('Retry-After'))
                raise ProviderError('rate_limited', 'The Odds API limited requests. Try again later.')
            if response.status_code in (401, 403):
                raise ProviderError('access_denied', 'The Odds API denied access. Check the key and plan.')
            if response.status_code != 200:
                raise ProviderError('http_error', f'The Odds API returned HTTP {response.status_code}.')
            try:
                return response.json(), response.headers.get('x-requests-remaining')
            except ValueError:
                raise ProviderError('invalid_response', 'The Odds API returned invalid JSON.') from None

    def catalog(self):
        return self._records('sports/')

    def events(self, sport):
        return self._records(f'sports/{sport}/events')

    def _records(self, path):
        records, _ = self.get(path)
        identity = 'key' if path == 'sports/' else 'id'
        if not isinstance(records, list) or any(
            not isinstance(row, dict) or not isinstance(row.get(identity), str) or not row[identity]
            for row in records
        ):
            raise ProviderError('invalid_response', 'The Odds API returned unexpected records.')
        return records
