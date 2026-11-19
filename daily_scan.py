"""Build a bounded, timestamped event-discovery report for the nine sports."""

import datetime as dt
import json
import os
from pathlib import Path

from odds_provider import OddsAPI, ProviderError
from sport_sources import CATALOG_PREFIXES, SOURCES, competitions


def scan(client, *, max_competitions=12, max_events=12):
    report = {
        'generated_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'kind': 'event_discovery_only',
        'sports': {},
    }
    catalog = []
    error = 'key_missing'
    if client:
        try:
            catalog = client.catalog()
            error = None
        except ProviderError as exc:
            error = exc.code
    budget = max_competitions
    # Give each covered sport a first request before scanning additional tournaments.
    groups = {sport: competitions(sport, catalog) for sport in SOURCES}
    for sport, source in SOURCES.items():
        supported = source.odds_key or sport in CATALOG_PREFIXES
        report['sports'][sport] = {
            'research_status': source.status,
            'research_sources': dict(zip(source.research_sources, source.source_urls)),
            'odds_status': (error or 'no_active_coverage') if supported else 'provider_not_configured',
            'active_competitions': len(groups[sport]),
            'competitions': [],
        }

    stop_reason = None
    for index in range(max(map(len, groups.values()), default=0)):
        for sport, matches in groups.items():
            if index >= len(matches):
                continue
            entry = report['sports'][sport]
            entry['odds_status'] = 'catalog_listed'
            match = matches[index]
            row = {'key': match['key'], 'title': match.get('title'), 'events': []}
            entry['competitions'].append(row)
            if stop_reason or budget <= 0:
                row['status'] = stop_reason or 'request_budget_reached'
                continue
            budget -= 1
            try:
                events = client.events(match['key'])
            except ProviderError as exc:
                row['status'] = exc.code
                if exc.code in ('rate_limited', 'access_denied'):
                    stop_reason = exc.code
                continue
            row['status'] = 'events_retrieved' if events else 'no_events'
            row['event_count_total'] = len(events)
            row['events'] = [
                {field: event.get(field) for field in ('id', 'commence_time', 'home_team', 'away_team')}
                for event in events[:max_events]
            ]
    return report


def main():
    key = os.getenv('ODDS_API_KEY', '').strip()
    report = scan(OddsAPI(key) if key else None)
    output = Path(os.getenv('SHARPLINE_SCAN_OUTPUT', 'data/latest_scan.json'))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(f'Saved {output}. Event discovery only.')


if __name__ == '__main__':
    main()
