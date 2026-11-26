"""Pregame snapshots and official final-box-score grading."""

import math
import json
import sqlite3
from contextlib import closing
from statistics import mean


NBA_BOX_STATS = {
    'player_points': 'points',
    'player_rebounds': 'reboundsTotal',
    'player_assists': 'assists',
}


REPORT_CARD_SCHEMA = """CREATE TABLE IF NOT EXISTS report_card (
            event_id TEXT, player_id INTEGER, market TEXT, sport TEXT NOT NULL,
            player TEXT NOT NULL, event TEXT NOT NULL, kickoff TEXT NOT NULL,
            away TEXT NOT NULL, home TEXT NOT NULL, observed TEXT NOT NULL,
            line REAL NOT NULL, side TEXT NOT NULL, projection REAL,
            injury TEXT, minutes TEXT, books INTEGER, weather TEXT,
            result TEXT NOT NULL DEFAULT 'Pending', actual REAL,
            platform TEXT NOT NULL DEFAULT 'PrizePicks',
            model_probability REAL,
            PRIMARY KEY (event_id, player_id, market, platform)
        )"""


def init_report_card(connection):
    connection.execute('BEGIN IMMEDIATE')
    connection.execute(REPORT_CARD_SCHEMA)
    columns = [row[1] for row in connection.execute('PRAGMA table_info(report_card)')]
    if 'platform' not in columns:
        connection.execute('ALTER TABLE report_card RENAME TO report_card_legacy')
        connection.execute(REPORT_CARD_SCHEMA)
        fields = ', '.join(columns)
        connection.execute(f'INSERT INTO report_card ({fields}) SELECT {fields} FROM report_card_legacy')
        connection.execute('DROP TABLE report_card_legacy')
    elif 'model_probability' not in columns:
        connection.execute('ALTER TABLE report_card ADD COLUMN model_probability REAL')
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pregame_state (
            prop_key TEXT PRIMARY KEY, evidence TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pregame_changes (
            id INTEGER PRIMARY KEY, prop_key TEXT NOT NULL, observed TEXT NOT NULL,
            player TEXT NOT NULL, event TEXT NOT NULL, market TEXT NOT NULL,
            field TEXT NOT NULL, previous TEXT NOT NULL, current TEXT NOT NULL,
            UNIQUE(prop_key, field, observed, current)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS closing_quotes (
            event_id TEXT, player_id INTEGER, market TEXT, line REAL NOT NULL,
            checked TEXT NOT NULL, updated TEXT NOT NULL,
            PRIMARY KEY (event_id, player_id, market)
        )"""
    )


def save_closing_quotes(db, quotes):
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.executemany(
            """INSERT INTO closing_quotes VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(event_id, player_id, market) DO UPDATE
               SET line=excluded.line, checked=excluded.checked, updated=excluded.updated
               WHERE excluded.checked > closing_quotes.checked
                 AND excluded.updated >= closing_quotes.updated""", quotes,
        )


def closing_line_gain(side, opening, closing):
    return (closing - opening) if side == 'More' else (opening - closing)


def pregame_window(minutes):
    if 0 < minutes <= 10:
        return 'close'
    if 10 < minutes <= 90:
        return 'recheck'
    return None


def grade_score(status, side, value, line):
    if status == 3:
        if value == line:
            return 'Push'
        return 'Hit' if (value > line) == (side == 'More') else 'Miss'
    if value > line:
        return 'Above line' if side == 'More' else 'Below target'
    return 'In play'


def score_projection(away_games, home_games):
    """Each game is (points scored, points allowed), oldest first."""
    if min(len(away_games), len(home_games)) < 5:
        return None

    def scoring(games, column):
        return 0.65 * mean(game[column] for game in games) + 0.35 * mean(
            game[column] for game in games[-5:]
        )

    away = round((scoring(away_games, 0) + scoring(home_games, 1)) / 2, 1)
    home = round((scoring(home_games, 0) + scoring(away_games, 1)) / 2, 1)
    return away, home, round(away - home, 1), round(away + home, 1)


def observe_pregame_changes(db, observations):
    """Accept newer provider evidence and return the latest change per prop."""
    latest = {}
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        for key, player, event, market, evidence in observations:
            saved = conn.execute('SELECT evidence FROM pregame_state WHERE prop_key=?', (key,)).fetchone()
            state = json.loads(saved[0]) if saved else {}
            for field, (timestamp, value) in evidence.items():
                previous = state.get(field)
                prior_report = state.get('_report')
                injury_field = field.startswith(('Player availability', 'Teammates: ', 'Opponents: '))
                if injury_field and prior_report and timestamp < prior_report[0]:
                    continue
                if previous and timestamp <= previous[0]:
                    continue
                state[field] = timestamp, value
                if not previous and injury_field and prior_report and timestamp > prior_report[0]:
                    previous = prior_report[0], 'Previously unconfirmed'
                if previous and value != previous[1]:
                    conn.execute(
                        """INSERT OR IGNORE INTO pregame_changes
                           (prop_key, observed, player, event, market, field, previous, current)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (key, timestamp, player, event, market, field, previous[1], value),
                    )
            conn.execute(
                """INSERT INTO pregame_state VALUES (?, ?)
                   ON CONFLICT(prop_key) DO UPDATE SET evidence=excluded.evidence""",
                (key, json.dumps(state)),
            )
            changes = conn.execute(
                """SELECT field, previous, current FROM pregame_changes
                   WHERE prop_key=? ORDER BY observed DESC, id DESC LIMIT 5""", (key,),
            ).fetchall()
            if changes:
                latest[key] = changes
    return latest


def save_report_picks(db, rows, platform='PrizePicks', *, probabilities=None):
    records = ((*row, platform, None) for row in rows) if probabilities is None else (
        (*row, platform, probability) for row, probability in zip(rows, probabilities, strict=True)
    )
    with closing(sqlite3.connect(db)) as connection, connection:
        before = connection.total_changes
        connection.executemany(
            """INSERT INTO report_card (
                event_id, player_id, market, sport, player, event, kickoff, away, home,
                observed, line, side, projection, injury, minutes, books, weather, platform, model_probability
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (event_id, player_id, market, platform) DO NOTHING""",
            records,
        )
        return connection.total_changes - before


def grade_nba_pick(game, pick):
    if game.get('gameStatus') != 3:
        return None
    away, home = game['awayTeam'], game['homeTeam']
    if (away.get('teamTricode'), home.get('teamTricode')) != (pick['away'], pick['home']):
        return None
    players = [player for team in (away, home) for player in team.get('players', [])
               if player.get('personId') == pick['player_id']]
    if len(players) != 1:
        return None
    player = players[0]
    if player.get('played') in ('0', 0):
        return 'Void', None
    value = player.get('statistics', {}).get(NBA_BOX_STATS[pick['market']])
    if player.get('played') not in ('1', 1) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if value == pick['line']:
        return 'Push', value
    won = (value > pick['line']) == (pick['side'] == 'More')
    return ('Hit' if won else 'Miss'), value
