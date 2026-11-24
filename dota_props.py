"""Dota 2 player history and verified game-level paper settlement."""
from collections import Counter
from datetime import timedelta
from statistics import mean, stdev

from report_card import grade_score
from sport_boards import fetch_json

DOTA_STATS = {'Kills': 'kills', 'Assists': 'assists', 'Deaths': 'deaths',
              'Last hits': 'last_hits'}
DOTA_MAPS = {'Map 1': (1,), 'Map 2': (2,), 'Map 3': (3,), 'Maps 1–2': (1, 2)}
DOTA_MARKETS = {f'{maps} {stat.lower()}': (positions, field)
                for maps, positions in DOTA_MAPS.items() for stat, field in DOTA_STATS.items()}


def dota_get(token, path, params=None):
    return fetch_json(f'https://api.pandascore.co/dota2/{path}', params,
                      {'Authorization': f'Bearer {token}'})


def search_dota_players(token, handle):
    return dota_get(token, 'players', {'search[name]': handle, 'per_page': 20})


def dota_history(token, player_id, cutoff):
    return dota_get(token, f'players/{int(player_id)}/stats', {
        'games_count': 20, 'from': (cutoff - timedelta(days=90)).isoformat(),
        'to': cutoff.isoformat(),
    })


def dota_projection(payload, player_id, team_id, opponent_id, market):
    if payload.get('id') != player_id:
        return None
    positions, field = DOTA_MARKETS[market]
    games = {}
    for game in payload.get('last_games') or []:
        value = game.get(field)
        if (game.get('game_id') and (game.get('player') or {}).get('id') == player_id
                and game.get('team_id') == team_id and type(value) is int and value >= 0):
            games.setdefault(game['game_id'], game)
    sample = list(games.values())[:20]
    if len(sample) < 5:
        return None
    values = [game[field] for game in sample]
    previous = [game[field] for game in sample
                if (game.get('opponent') or {}).get('id') == opponent_id]
    heroes = Counter((game.get('hero') or {}).get('localized_name') for game in sample)
    heroes.pop(None, None)
    pace = {}
    for name in ('gold_per_min', 'xp_per_min'):
        measures = [game[name] for game in sample if type(game.get(name)) in (int, float)]
        if measures:
            pace[name] = round(mean(measures), 1)
    recent = mean(values[:5])
    baseline = 0.65 * mean(values) + 0.35 * recent
    # Shrink the opponent split so that a short series cannot dominate the baseline.
    opponent_weight = min(0.15, len(previous) / 40) if len(previous) >= 3 else 0
    estimate = (1 - opponent_weight) * baseline + opponent_weight * mean(previous) if opponent_weight else baseline
    return {
        'estimate': round(len(positions) * estimate, 2),
        'sample': len(values), 'average': mean(values), 'recent': recent, 'deviation': stdev(values),
        'opponent_games': len(previous), 'opponent_average': mean(previous) if previous else None,
        'heroes': heroes.most_common(3), 'pace': pace, 'opponent_weight': opponent_weight,
    }


def dota_final_match(token, match_id):
    return dota_get(token, f'matches/{int(match_id)}')


def dota_final_game(token, game_id):
    return dota_get(token, f'games/{int(game_id)}')


def dota_grade(match, game_lookup, match_id, player_id, market, line, side):
    if (match.get('id') != match_id or match.get('status') != 'finished'
            or match.get('forfeit') or match.get('game_advantage')):
        return None
    positions, field = DOTA_MARKETS[market]
    total = 0
    for position in positions:
        matches = [game for game in match.get('games') or [] if game.get('position') == position]
        if len(matches) != 1 or not matches[0].get('id') or matches[0].get('forfeit'):
            return None
        ident = matches[0]['id']
        game = game_lookup(ident)
        if (game.get('id') != ident or game.get('match_id') != match_id or game.get('position') != position
                or game.get('status') != 'finished' or game.get('complete') is not True or game.get('forfeit')):
            return None
        values = [item.get(field) for item in game.get('players') or []
                  if (item.get('player') or {}).get('id') == player_id]
        if len(values) != 1 or type(values[0]) is not int or values[0] < 0:
            return None
        total += values[0]
    return grade_score(3, side, total, line), total
