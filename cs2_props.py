"""PandaScore-backed paper projections and exact-map settlement for CS2 kills."""

from datetime import datetime, timedelta, timezone

from report_card import grade_score
from sport_boards import fetch_json


MAP_MARKETS = {'Map 1 kills': (1,), 'Map 2 kills': (2,), 'Map 3 kills': (3,),
               'Maps 1–2 kills': (1, 2)}


def panda_get(path, token, params=None):
    return fetch_json(f'https://api.pandascore.co/csgo/{path}', params,
                      {'Authorization': f'Bearer {token}'})


def search_cs2_players(token, name):
    return panda_get('players', token, {'search[name]': name, 'per_page': 20})


def load_cs2_history(token, player_id, cutoff):
    return panda_get(f'players/{int(player_id)}/stats', token, {
        'videogame_title': 'cs-2', 'games_count': 20,
        'from': (cutoff - timedelta(days=90)).isoformat(), 'to': cutoff.isoformat(),
    })


def load_cs2_match_stats(token, match_id, player_id):
    return panda_get(f'matches/{int(match_id)}/players/{int(player_id)}/stats', token)


def cs2_projection(payload, player_id, market):
    if payload.get('id') != player_id:
        return None
    counts = payload.get('stats', {}).get('counts', {})
    maps, kills = counts.get('games_played'), counts.get('kills')
    if type(maps) is not int or not 5 <= maps <= 20 or type(kills) is not int or kills < 0:
        return None
    return maps, kills / maps, round(kills * len(MAP_MARKETS[market]) / maps, 2)


def grade_cs2_pick(payload, match_id, player_id, market, line, side):
    if payload.get('id') != player_id:
        return None
    match = payload.get('stats', {}).get('match', {})
    if (match.get('id') != match_id or match.get('status') != 'finished'
            or (match.get('videogame_title') or {}).get('slug') != 'cs-2'
            or match.get('forfeit') or match.get('game_advantage')):
        return None
    games = match.get('games') or []
    samples = payload.get('last_games') or []
    total = 0
    for position in MAP_MARKETS[market]:
        matched = [game for game in games if game.get('position') == position]
        if len(matched) != 1:
            return None
        game = matched[0]
        if (game.get('match_id') != match_id or game.get('status') != 'finished'
                or game.get('complete') is not True or game.get('forfeit')):
            return None
        kills = [entry.get('kills') for entry in samples if entry.get('game_id') == game.get('id')
                 and (entry.get('player') or {}).get('id') == player_id]
        if len(kills) != 1 or type(kills[0]) is not int or kills[0] < 0:
            return None
        total += kills[0]
    return grade_score(3, side, total, line), total


def cs2_start(value):
    try:
        start = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return start.astimezone(timezone.utc) if start.tzinfo else None
    except (AttributeError, ValueError):
        return None
