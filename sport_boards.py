"""Public event and team-score adapters for the supplemental sports."""

from datetime import datetime

import requests

from report_card import score_projection


ESPN = 'https://site.api.espn.com/apis/site/v2/sports'
MLB = 'https://statsapi.mlb.com/api/v1/schedule'
COLLEGE = {'CBB': 'basketball/mens-college-basketball', 'CFB': 'football/college-football'}
TEAM_MODELS = {*COLLEGE, 'MLB'}
BOARD_SOURCES = {
    'CBB': 'ESPN Division I scoreboard', 'CFB': 'ESPN college football scoreboard',
    'MLB': 'MLB StatsAPI', 'Tennis': 'ESPN ATP and WTA scoreboards',
    'Cricket': 'ESPN cricket scoreboard', 'PGA Golf': 'ESPN PGA TOUR leaderboard',
    'CS2': 'PandaScore Counter-Strike fixtures',
    'Dota 2': 'PandaScore Dota 2 fixtures',
}


def fetch_json(url, params=None, headers=None):
    response = requests.get(url, params=params, headers=headers, timeout=(3, 8))
    response.raise_for_status()
    return response.json()


def espn_score(value):
    if isinstance(value, dict):
        return value.get('displayValue', value.get('value'))
    return value


def espn_row(event, match, league=None):
    status = match.get('status') or event.get('status') or {}
    designation = status.get('type', {}) if isinstance(status, dict) else {}
    competitors = match.get('competitors', [])
    names = [(item.get('team') or item.get('athlete') or {}).get('displayName')
             or item.get('displayName') or item.get('name') for item in competitors]
    scores = [f'{name} {espn_score(item.get("score"))}' for name, item in zip(names, competitors)
              if name and item.get('score') is not None]
    return {
        'Event': event.get('name') if match is event else ' vs '.join(filter(None, names)) or event.get('name'),
        'Competition': league,
        'Start (UTC)': match.get('date') or event.get('date'),
        'Status': designation.get('shortDetail') or designation.get('description')
                  or event.get('summary') or (status if isinstance(status, str) else None),
        'Score': (' · '.join(scores) or None) if designation.get('state') != 'pre' else None,
        '_phase': designation.get('state'),
    }


def mlb_games(payload):
    return [game for date in payload['dates'] for game in date.get('games', [])]


def load_sport_board(sport, day, end, panda_key=''):
    dates = f'{day:%Y%m%d}'
    if sport in COLLEGE:
        source = COLLEGE[sport]
        params = {'dates': dates, 'limit': 250, 'groups': 50 if sport == 'CBB' else 80}
        rows = []
        for event in fetch_json(f'{ESPN}/{source}/scoreboard', params)['events']:
            for match in event.get('competitions', []):
                row = espn_row(event, match)
                teams = {item.get('homeAway'): item.get('team', {}) for item in match.get('competitors', [])}
                if {'away', 'home'}.issubset(teams):
                    row.update(_away=teams['away'].get('id'), _home=teams['home'].get('id'),
                               _season=event.get('season', {}).get('year'))
                rows.append(row)
        return rows
    if sport == 'MLB':
        params = {'sportId': 1, 'startDate': day.isoformat(), 'endDate': end.isoformat()}
        rows = []
        for game in mlb_games(fetch_json(MLB, params)):
            away, home = (game['teams'][side] for side in ('away', 'home'))
            rows.append({
                'Event': f"{away['team']['name']} at {home['team']['name']}",
                'Start (UTC)': game.get('gameDate'), 'Status': game['status'].get('detailedState'),
                'Score': f"{away['score']}–{home['score']}" if 'score' in away and 'score' in home else None,
                '_phase': 'pre' if game['status'].get('abstractGameState') == 'Preview' else None,
                '_season': game.get('season') if game.get('gameType') == 'R' else None,
                '_away': away['team']['id'], '_home': home['team']['id'],
            })
        return rows
    if sport == 'Tennis':
        return [
            espn_row(event, match, f"{tour.upper()} · {event['name']}")
            for tour in ('atp', 'wta')
            for event in fetch_json(f'{ESPN}/tennis/{tour}/scoreboard')['events']
            for grouping in event.get('groupings', [])
            for match in grouping.get('competitions', [])
        ]
    if sport == 'Cricket':
        data = fetch_json('https://site.api.espn.com/apis/personalized/v2/scoreboard/header', {'sport': 'cricket'})
        return [espn_row(event, event, league.get('name'))
                for item in data['sports'] for league in item.get('leagues', [])
                for event in league.get('events', [])]
    if sport == 'PGA Golf':
        rows = []
        for event in fetch_json(f'{ESPN}/golf/pga/scoreboard')['events']:
            for match in event.get('competitions', []):
                row = espn_row(event, event)
                row['Status'] = espn_row(event, match)['Status']
                for golfer in match.get('competitors', []):
                    name = golfer.get('athlete', {}).get('displayName')
                    if name:
                        rows.append({**row, 'Golfer': name, 'Score': espn_score(golfer.get('score'))})
        return rows
    if sport in ('CS2', 'Dota 2') and panda_key:
        games = {}
        slug = 'csgo' if sport == 'CS2' else 'dota2'
        params = {'per_page': 20}
        if sport == 'CS2':
            params['filter[videogame_title]'] = 'cs-2'
        for phase in ('running', 'upcoming', 'past'):
            for event in fetch_json(f'https://api.pandascore.co/{slug}/matches/{phase}', params,
                                    {'Authorization': f'Bearer {panda_key}'}):
                names = {item['opponent']['id']: item['opponent']['name']
                         for item in event.get('opponents', []) if item.get('opponent')}
                scores = [f"{names[item['team_id']]} {item['score']}" for item in event.get('results', [])
                          if item.get('team_id') in names and item.get('score') is not None]
                games[event['id']] = {
                    'Event': event.get('name'), 'Competition': (event.get('league') or {}).get('name'),
                    'Start (UTC)': event.get('begin_at'), 'Status': event.get('status'),
                    'Score': ' · '.join(scores) if phase != 'upcoming' and scores else None,
                    '_match_id': event['id'], '_teams': names,
                    '_map_limit': event.get('number_of_games'),
                    '_winner': event.get('winner_id'), '_forfeit': event.get('forfeit'),
                    '_detailed': event.get('detailed_stats'),
                }
        return list(games.values())
    return []


def team_history(sport, team_id, season, cutoff):
    games = []
    if sport == 'MLB':
        params = {'sportId': 1, 'teamId': team_id, 'season': season, 'gameTypes': 'R',
                  'startDate': f'{season}-03-01', 'endDate': cutoff.date().isoformat()}
        for game in mlb_games(fetch_json(MLB, params)):
            if game['status'].get('abstractGameState') != 'Final':
                continue
            teams = game['teams']
            sides = [side for side in ('away', 'home') if str(teams[side]['team']['id']) == str(team_id)]
            if len(sides) == 1:
                side = sides[0]
                games.append((game['gameDate'], teams[side].get('score'),
                              teams['home' if side == 'away' else 'away'].get('score')))
    else:
        data = fetch_json(f'{ESPN}/{COLLEGE[sport]}/teams/{int(team_id)}/schedule', {'season': season})
        for event in data['events']:
            for match in event.get('competitions', []):
                if match.get('status', {}).get('type', {}).get('completed') is not True:
                    continue
                competitors = match.get('competitors', [])
                scores = [espn_score(team.get('score')) for team in competitors]
                own = [i for i, team in enumerate(competitors) if str(team.get('team', {}).get('id')) == str(team_id)]
                if len(scores) == 2 and len(own) == 1:
                    games.append((match.get('date') or event['date'], scores[own[0]], scores[1-own[0]]))
    scores = []
    for stamp, scored, allowed in games:
        try:
            started = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if started.date() < cutoff.date():
                scores.append((started, int(scored), int(allowed)))
        except (TypeError, ValueError, AttributeError):
            continue
    return [(scored, allowed) for _, scored, allowed in sorted(scores)]


def project_team_game(sport, away, home, season, cutoff):
    return score_projection(team_history(sport, away, season, cutoff),
                            team_history(sport, home, season, cutoff))
