"""Vantage Sports: explicit, auditable research-source registry.

A source being listed does not mean that it is integrated or that live stats
are available. No player performance data is invented.
"""
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class SportSource:
    sport: str
    odds_key: Optional[str]
    research_sources: tuple[str, ...]
    source_urls: tuple[str, ...]
    status: str
    notes: str

SOURCES = {
    'NBA': SportSource('NBA','basketball_nba',('hoopR / SportsDataverse',),('https://hoopr.sportsdataverse.org/',),'adapter_needed','Validate game logs, injury feed and API terms.'),
    'CBB': SportSource('CBB','basketball_ncaab',('hoopR / SportsDataverse',),('https://hoopr.sportsdataverse.org/',),'adapter_needed','Verify NCAA player game-log coverage.'),
    'NFL': SportSource('NFL','americanfootball_nfl',('nflverse',),('https://github.com/nflverse/nflverse-data',),'adapter_needed','Validate weekly stats, injury status and update latency.'),
    'CFB': SportSource('CFB','americanfootball_ncaaf',('cfbfastR / CollegeFootballData',),('https://cfbfastr.sportsdataverse.org/','https://collegefootballdata.com/'),'adapter_needed','Some endpoints require an API key.'),
    'MLB': SportSource('MLB','baseball_mlb',('MLB StatsAPI',),('https://statsapi.mlb.com/',),'adapter_needed','Verify player game logs and pitching markets.'),
    'CS2': SportSource('CS2',None,('PandaScore',),('https://www.pandascore.co/',),'adapter_needed','Verify map-specific kills and licensing before ranking.'),
    'Dota 2': SportSource('Dota 2',None,('PandaScore',),('https://developers.pandascore.co/docs/plan-reference',),'connected','Fixtures require a token. Player history and final map stats require eligible plan access.'),
    'Tennis': SportSource('Tennis',None,('Jeff Sackmann tennis datasets',),('https://github.com/JeffSackmann/tennis_atp',),'adapter_needed','Historical data alone is not a live prop feed; verify licensing.'),
    'Cricket': SportSource('Cricket',None,('Cricsheet',),('https://cricsheet.org/',),'adapter_needed','Ball-by-ball data requires match/player mapping.'),
    'PGA Golf': SportSource('PGA Golf',None,('Data Golf',),('https://datagolf.com/api-access',),'adapter_needed','API access may require a paid subscription.'),
}

def source_rows():
    return [
        {
            'sport': sport,
            'odds_coverage': 'Catalog check pending' if source.odds_key or sport in CATALOG_PREFIXES else 'Feed not configured',
            'stats_status': 'PandaScore · plan dependent' if sport == 'Dota 2' else 'Adapter pending',
            'research_sources': ', '.join(source.research_sources),
            'notes': source.notes,
        }
        for sport, source in SOURCES.items()
    ]


# Match only documented categories. Golf entries represent major-winner futures.
CATALOG_PREFIXES = {'Tennis': 'tennis_', 'Cricket': 'cricket_', 'PGA Golf': 'golf_'}


def competitions(sport, catalog):
    exact_key = SOURCES[sport].odds_key
    prefix = CATALOG_PREFIXES.get(sport)
    return [
        row for row in catalog
        if row.get('active') is True
        and (row['key'] == exact_key or (prefix and row['key'].startswith(prefix)))
    ]
