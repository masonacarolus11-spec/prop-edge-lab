"""Import an explicitly requested Sleeper board from the Apify community actor."""
from datetime import datetime, time, timedelta, timezone
from math import isfinite
from zoneinfo import ZoneInfo

import requests

SLEEPER_LEAGUES = {
    'NBA': 'NBA', 'NFL': 'NFL', 'CFB': 'CFB', 'MLB': 'MLB',
    'CS2': 'CS2', 'Tennis': 'TENNIS', 'PGA Golf': 'GOLF',
}
MAX_ROWS = 200
MAX_CHARGE_USD = 0.05
ACTOR_URL = ('https://api.apify.com/v2/acts/logiover~sleeper-picks-scraper/'
             'run-sync-get-dataset-items')
PACIFIC = ZoneInfo('America/Los_Angeles')
MARKETS = {
    'points': 'player_points', 'rebounds': 'player_rebounds', 'assists': 'player_assists',
    'passing yards': 'player_pass_yds', 'pass yards': 'player_pass_yds',
    'rushing yards': 'player_rush_yds', 'rush yards': 'player_rush_yds',
    'receiving yards': 'player_reception_yds', 'reception yards': 'player_reception_yds',
}


def normalize_sleeper_rows(payload, sport, now, slate_date=None):
    if not isinstance(payload, list):
        raise ValueError('Expected an Apify dataset list.')
    rows = {}
    for item in payload:
        if not isinstance(item, dict) or item.get('recordType') != 'line':
            continue
        if (item.get('league') != SLEEPER_LEAGUES[sport] or item.get('oddsType') != 'standard'
                or item.get('isPromo') or item.get('isLive')):
            continue
        player = item.get('playerName')
        stat = item.get('statType')
        raw_start = item.get('startTime')
        try:
            start = datetime.fromisoformat(raw_start.replace('Z', '+00:00'))
            line = float(item['lineScore'])
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            continue
        if not start.tzinfo or start <= now or not isfinite(line) or isinstance(item['lineScore'], bool):
            continue
        if slate_date is not None and start.astimezone(PACIFIC).date() != slate_date:
            continue
        if not isinstance(player, str) or not player.strip() or not isinstance(stat, str) or not stat.strip():
            continue
        away, home = item.get('awayTeam'), item.get('homeTeam')
        event = f'{away} at {home}' if away and home else item.get('matchup') or 'Matchup unavailable'
        market = MARKETS.get(stat.strip().casefold(), stat.strip())
        identity = item.get('gameId') or (event, start), player.strip().casefold(), market, line
        rows.setdefault(identity, {
            'sport': sport, 'platform': 'Sleeper', 'event': event, 'player': player.strip(),
            'market': market, 'line': line, 'starts_utc': start.isoformat(),
            'Sleeper updated UTC': item.get('updatedAt'), 'Sleeper over multiplier': item.get('overMultiplier'),
            'Sleeper under multiplier': item.get('underMultiplier'),
        })
    return list(rows.values())


def fetch_sleeper_rows(token, sport, slate_date=None):
    window = {'startsWithinHours': 48}
    if slate_date is not None:
        start = datetime.combine(slate_date, time.min, PACIFIC).astimezone(timezone.utc)
        end = datetime.combine(slate_date + timedelta(days=1), time.min, PACIFIC).astimezone(timezone.utc)
        window = {'startDateFrom': start.date().isoformat(),
                  'startDateTo': (end - timedelta(seconds=1)).date().isoformat()}
    response = requests.post(
        ACTOR_URL, headers={'Authorization': f'Bearer {token}'},
        params={'maxTotalChargeUsd': MAX_CHARGE_USD, 'maxItems': MAX_ROWS, 'timeout': 60},
        json={'leagues': [SLEEPER_LEAGUES[sport]], 'maxItems': MAX_ROWS, 'rowShape': 'line', **window},
        timeout=(5, 70),
    )
    response.raise_for_status()
    return normalize_sleeper_rows(response.json(), sport, datetime.now(timezone.utc), slate_date)
