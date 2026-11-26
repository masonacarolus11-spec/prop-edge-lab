import os, sqlite3, math, json, datetime as dt
import re
from urllib.parse import urlsplit
from collections import Counter
from zoneinfo import ZoneInfo

import requests
from pathlib import Path
import numpy as np
import pandas as pd
from hashlib import sha256
import streamlit as st
from scipy.stats import norm, poisson
from odds_provider import OddsAPI, ProviderError
from report_card import (
    NBA_BOX_STATS, closing_line_gain, grade_nba_pick, grade_score, init_report_card,
    score_projection, observe_pregame_changes, pregame_window, save_closing_quotes,
    save_report_picks,
)
from sport_sources import SOURCES, competitions, source_rows
from sport_boards import BOARD_SOURCES, TEAM_MODELS, load_sport_board, project_team_game
from cs2_props import (MAP_MARKETS, cs2_projection, cs2_start, grade_cs2_pick,
                       load_cs2_history, load_cs2_match_stats, search_cs2_players)
from nba_rebounds import missed_shot_profiles
from sleeper_feed import MAX_CHARGE_USD, MAX_ROWS, SLEEPER_LEAGUES, fetch_sleeper_rows
from dota_props import (DOTA_MAPS, DOTA_MARKETS, DOTA_STATS, dota_final_game, dota_final_match,
                        dota_grade, dota_history, dota_projection, search_dota_players)

st.set_page_config(page_title='Vantage Sports', page_icon='📈', layout='wide')
st.title('📈 Vantage Sports')
st.caption('Research prototype • paper testing only • no automatic bets • no guaranteed returns')
DB = Path(os.getenv('PROP_DB', 'prop_scanner.sqlite'))
SPORTS = {sport: source.odds_key for sport, source in SOURCES.items() if source.odds_key}
PICK_MARKETS = {
    'NBA': 'player_points,player_rebounds,player_assists',
    'CBB': 'player_points,player_rebounds,player_assists',
    'NFL': 'player_pass_yds,player_rush_yds,player_reception_yds',
    'CFB': 'player_pass_yds,player_rush_yds,player_reception_yds',
    'MLB': 'batter_hits,batter_total_bases,pitcher_strikeouts',
}
ALL_SPORTS = list(SOURCES)


@st.cache_resource(max_entries=64, show_spinner=False)
def odds_client(key):
    return OddsAPI(key)


@st.cache_data(ttl=3600, max_entries=64, show_spinner=False)
def fetch_catalog(key):
    return odds_client(key).catalog()


@st.cache_data(ttl=600, max_entries=256, show_spinner=False)
def fetch_events(key, sport):
    return odds_client(key).events(sport)


@st.cache_data(ttl=300, max_entries=256, show_spinner=False)
def fetch_event_odds(key, sport, event_id, regions, markets):
    data, remaining = odds_client(key).get(
        f'sports/{sport}/events/{event_id}/odds',
        regions=regions, markets=markets, oddsFormat='american',
    )
    if not isinstance(data, dict) or not isinstance(data.get('bookmakers'), list):
        raise ProviderError('invalid_response', 'The Odds API returned unexpected odds data.')
    return data, remaining


def configured_key():
    try:
        return st.secrets.get('ODDS_API_KEY', os.getenv('ODDS_API_KEY', ''))
    except FileNotFoundError:
        return os.getenv('ODDS_API_KEY', '')


RESEARCH_LINKS = {
    'NBA': (
        ('Player stats', 'NBA Stats', 'https://www.nba.com/stats/players/traditional', 'Minutes, points, rebounds and assists.'),
        ('Starting lineups', 'RotoWire NBA Lineups', 'https://www.rotowire.com/basketball/nba-lineups.php', 'Check whether the lineup is projected or confirmed.'),
        ('Matchup', 'Hashtag Basketball Defense vs. Position', 'https://hashtagbasketball.com/nba-defense-vs-position', 'Compare the stat allowed against PG, SG, SF, PF and C.'),
        ('Injury report', 'NBA Official Injury Report', 'https://official.nba.com/nba-injury-report-2026-27-season/', '2026–27 league reports. Open the latest report for your game.'),
    ),
    'NFL': (
        ('Player stats', 'NFL Player Stats', 'https://www.nfl.com/stats/player-stats/', 'Passing, rushing and receiving.'),
        ('Injury report', 'NFL Injury Report', 'https://www.nfl.com/injuries/', 'Weekly practice participation and game status by matchup.'),
    ),
    'CBB': (
        ('Player stats', 'NCAA Division I Men’s Basketball Stats', 'https://www.ncaa.com/stats/basketball-men/d1', 'Player and team leaderboards.'),
        ('Availability', 'SEC Student-Athlete Availability Reports', 'https://www.secsports.com/reports', 'SEC coverage. For other games, check the conference and team announcements.'),
    ),
    'CFB': (
        ('Player stats', 'NCAA FBS Stats', 'https://www.ncaa.com/stats/football/fbs', 'Player and team leaderboards.'),
        ('Availability', 'SEC Football Availability Reports', 'https://www.secsports.com/fbreports', 'SEC coverage. For other games, check the conference and team announcements.'),
    ),
    'MLB': (
        ('Player stats', 'Baseball Savant', 'https://baseballsavant.mlb.com/', 'Statcast hitting and pitching metrics.'),
        ('Injury report', 'MLB Injury Report', 'https://www.mlb.com/injury-report', 'Team-by-team injury updates.'),
        ('Lineups', 'MLB Starting Lineups', 'https://www.mlb.com/starting-lineups', 'Batting orders and starting pitchers as posted.'),
    ),
    'CS2': (
        ('Player stats', 'HLTV Stats', 'https://www.hltv.org/stats', 'Player and map statistics.'),
        ('Roster checks', 'HLTV Matches', 'https://www.hltv.org/matches', 'Check match lineups and stand-ins against organizer or team announcements.'),
    ),
    'Tennis': (
        ('Player stats', 'WTA Stats Hub', 'https://www.wtatennis.com/stats', 'Serve and return statistics.'),
        ('Player stats', 'ATP Stats', 'https://www.atptour.com/en/stats', 'ATP tour statistics.'),
        ('Withdrawals and updates', 'WTA News', 'https://www.wtatennis.com/news', 'Check tour news and the tournament draw for withdrawals.'),
        ('Withdrawals and updates', 'ATP News', 'https://www.atptour.com/en/news', 'Check tour news and the tournament draw for withdrawals.'),
    ),
    'Cricket': (
        ('Player stats', 'ICC Player Rankings', 'https://www.icc-cricket.com/rankings/index', 'International player rankings by format.'),
        ('Squad updates', 'ICC News', 'https://www.icc-cricket.com/news', 'Check squad changes and the official match lineup at the toss.'),
    ),
    'PGA Golf': (
        ('Player stats', 'PGA TOUR Stats', 'https://www.pgatour.com/stats', 'Strokes gained, scoring and other tour statistics.'),
        ('Field and withdrawals', 'PGA TOUR Tournaments', 'https://www.pgatour.com/tournaments', 'Open the tournament and check the latest field and leaderboard updates.'),
    ),
}


ADDITIONAL_RESEARCH_LINKS = {
    "NBA": (
        ("Minutes and usage", "NBA Advanced Stats", "https://www.nba.com/stats/players/advanced", "Usage rate, pace and efficiency."),
        ("Rotation history", "NBA Box Scores", "https://www.nba.com/games", "Official box scores and minutes by game."),
        ("Shot profile", "NBA Shooting Dashboard", "https://www.nba.com/stats/players/shots-general", "Shot frequency and accuracy by shot type."),
        ("Play types", "NBA Play Type Stats", "https://www.nba.com/stats/players/isolation", "Possessions and efficiency by play type."),
        ("Schedule and rest", "Basketball Reference NBA", "https://www.basketball-reference.com/leagues/", "Season schedules, box scores and historical splits."),
    ),
    "CBB": (
        ("Team efficiency", "KenPom", "https://kenpom.com/", "Adjusted efficiency, tempo and schedule strength. Some features are paid."),
        ("Tempo and matchups", "Bart Torvik", "https://barttorvik.com/", "Tempo-free team ratings and date filters."),
        ("Team and player history", "Sports Reference College Basketball", "https://www.sports-reference.com/cbb/", "Historical rosters, schedules and game logs."),
        ("Scores and box scores", "ESPN Men’s College Basketball Scoreboard", "https://www.espn.com/mens-college-basketball/scoreboard", "Matchups, box scores and minutes when posted."),
        ("Selection context", "NCAA NET Rankings", "https://www.ncaa.com/rankings/basketball-men/d1/ncaa-mens-basketball-net-rankings", "Team NET rankings and opponent context."),
    ),
    "NFL": (
        ("Tracking", "NFL Next Gen Stats", "https://nextgenstats.nfl.com/stats/", "Tracking metrics for passing, rushing and receiving."),
        ("Usage and snaps", "Pro Football Reference NFL Season", "https://www.pro-football-reference.com/years/2026/", "Navigate to a team for its player snap counts and season stats."),
        ("Play-by-play", "NFL Game Center", "https://www.nfl.com/scores/", "Official gamebooks, box scores and play-by-play."),
        ("Inactive checks", "NFL News", "https://www.nfl.com/news/", "Pregame inactive notices when published."),
        ("Weather", "National Weather Service", "https://www.weather.gov/", "US stadium-area forecasts for wind and precipitation."),
    ),
    "CFB": (
        ("Team efficiency", "ESPN College Football FPI", "https://www.espn.com/college-football/fpi", "Team strength and schedule context."),
        ("Box scores", "ESPN College Football Scoreboard", "https://www.espn.com/college-football/scoreboard", "Game logs, box scores and play-by-play."),
        ("Historical usage", "Sports Reference College Football", "https://www.sports-reference.com/cfb/", "Player and team game history."),
        ("Availability", "Big Ten Football Availability Reporting", "https://bigten.org/news/2026/8/28/big-ten-announces-changes-to-football-availability-reporting-process.aspx", "Explains the conference reporting schedule and where to find game-day updates."),
        ("Weather", "National Weather Service", "https://www.weather.gov/", "US stadium-area forecasts for wind and precipitation."),
    ),
    "MLB": (
        ("Projections and splits", "FanGraphs", "https://www.fangraphs.com/", "Depth charts, player projections and splits. Some tools require membership."),
        ("Historical splits", "Baseball Reference", "https://www.baseball-reference.com/", "Player game logs, splits and historical box scores."),
        ("Roster moves", "MLB Transactions", "https://www.mlb.com/transactions", "Official roster moves and injured-list transactions."),
        ("Prospect call-ups", "MLB Pipeline", "https://www.mlb.com/prospects", "Prospect profiles and call-up context."),
        ("Scores and game logs", "MLB Scores", "https://www.mlb.com/scores", "Official box scores, game status and play-by-play."),
    ),
    "CS2": (
        ("Tournament and roster history", "Liquipedia Counter-Strike", "https://liquipedia.net/counterstrike/Main_Page", "Event brackets and roster history. Confirm stand-ins with organizers."),
        ("Event operations", "ESL Pro Tour Counter-Strike", "https://pro.eslgaming.com/tour/cs/", "ESL event schedules, teams and tournament information."),
        ("Event operations", "BLAST Premier", "https://blast.tv/cs", "BLAST schedules, results and event coverage."),
        ("Game changes", "Valve Counter-Strike News", "https://www.counter-strike.net/news", "Official patch and map-pool updates."),
        ("Team strength", "Valve Regional Standings", "https://github.com/ValveSoftware/counter-strike_regional_standings", "Valve standings and ranking methodology."),
    ),
    "Tennis": (
        ("Surface and matchup", "Tennis Abstract", "https://www.tennisabstract.com/", "Surface splits, player history and matchup research."),
        ("Lower-tour results", "ITF World Tennis Tour", "https://www.itftennis.com/en/tournament-calendar/mens-world-tennis-tour-calendar/", "Men’s ITF tournament calendar and event links."),
        ("Official match scoring", "ProTennisLive", "https://www.protennislive.com/", "Tournament scoring and published draw documents where available."),
        ("Challenger coverage", "ATP Challenger Tour", "https://www.atptour.com/en/atp-challenger-tour", "Challenger schedules, draws and results."),
        ("Team competition", "Davis Cup", "https://www.daviscup.com/", "Official team nominations and match results."),
    ),
    "Cricket": (
        ("Scorecards and squads", "ESPNcricinfo", "https://www.espncricinfo.com/", "Scorecards, player histories and tournament coverage."),
        ("Live scorecards", "Cricbuzz", "https://www.cricbuzz.com/cricket-match/live-scores", "Live scorecards and match updates."),
        ("T20 selection", "Indian Premier League", "https://www.iplt20.com/", "Official IPL fixtures, squads and results."),
        ("Domestic and international", "ECB", "https://www.ecb.co.uk/", "England and domestic fixtures, squad updates and results."),
        ("Australia and Big Bash", "Cricket Australia", "https://www.cricket.com.au/", "Australian fixtures, scorecards and squad updates."),
    ),
    "PGA Golf": (
        ("Field strength", "Official World Golf Ranking", "https://www.owgr.com/", "World rankings and player ranking history."),
        ("Majors and course coverage", "USGA", "https://www.usga.org/", "Official U.S. Open field, scoring and course information when published."),
        ("Player development", "Korn Ferry Tour", "https://www.pgatour.com/korn-ferry-tour", "PGA TOUR feeder-tour results and recent form for graduates and call-ups."),
        ("Cross-tour form", "DP World Tour Results", "https://www.europeantour.com/dpworld-tour/schedule/", "Official event schedules and results for players appearing on both tours."),
        ("Tee-time weather", "National Weather Service", "https://www.weather.gov/", "US course-area forecasts for wind and weather timing."),
    ),
}

def show_research_links(sport):
    if sport == 'Dota 2':
        st.markdown('**Connected data** · [PandaScore coverage and plans](https://developers.pandascore.co/docs/plan-reference)')
        links = (
            ('Patch changes', 'Valve', 'https://www.dota2.com/patches', 'Official balance changes and gameplay updates.'),
            ('Tournament and roster context', 'Liquipedia', 'https://liquipedia.net/dota2/Main_Page', 'Brackets, rosters and stand-ins.'),
            ('Draft and match stats', 'OpenDota', 'https://www.opendota.com/', 'Parsed match histories and draft context where available.'),
            ('Hero and lane trends', 'Dotabuff', 'https://www.dotabuff.com/', 'Hero, role and matchup trends.'),
            ('Event schedules', 'ESL Pro Tour', 'https://pro.eslgaming.com/tour/dota2/', 'Organizer schedules and tournament format.'),
        )
    else:
        links = (*RESEARCH_LINKS[sport], *ADDITIONAL_RESEARCH_LINKS[sport])
    for category, label, url, description in links:
        st.markdown(f'**{category}** · [{label}]({url})  \n{description}')
    st.caption('Manual research links · Open a source for current information. The scan does not fetch these pages or use them to rank props.')


cached_sport_board = st.cache_data(ttl=120, max_entries=32, show_spinner=False)(load_sport_board)
cached_team_projection = st.cache_data(ttl=1800, max_entries=64, show_spinner=False)(project_team_game)
cached_cs2_players = st.cache_data(ttl=1800, max_entries=64, show_spinner=False)(search_cs2_players)
cached_cs2_history = st.cache_data(ttl=1800, max_entries=64, show_spinner=False)(load_cs2_history)
cached_cs2_result = st.cache_data(ttl=300, max_entries=128, show_spinner=False)(load_cs2_match_stats)
cached_dota_players = st.cache_data(ttl=1800, max_entries=64, show_spinner=False)(search_dota_players)
cached_dota_history = st.cache_data(ttl=1800, max_entries=64, show_spinner=False)(dota_history)
cached_dota_match = st.cache_data(ttl=300, max_entries=64, show_spinner=False)(dota_final_match)
cached_dota_game = st.cache_data(ttl=300, max_entries=128, show_spinner=False)(dota_final_game)


def configured_panda_key():
    try:
        return st.secrets.get('PANDASCORE_TOKEN', os.getenv('PANDASCORE_TOKEN', ''))
    except FileNotFoundError:
        return os.getenv('PANDASCORE_TOKEN', '')


def cs2_data_warning(exc):
    response = getattr(exc, 'response', None)
    if response is not None and response.status_code == 403:
        st.warning('PandaScore denied this stats request. Player history and grading require Historical or eligible Live access.')
    elif response is not None and response.status_code == 429:
        st.warning('PandaScore reached its request limit. Wait before trying again.')
    else:
        st.warning('PandaScore player data could not be verified for this request.')


def show_cs2_kills(rows, token):
    st.subheader('CS2 kill props')
    st.caption('Enter a PrizePicks line to model it from PandaScore history and optionally track it on paper.')
    now = dt.datetime.now(dt.timezone.utc)
    upcoming = {
        row['_match_id']: row for row in rows
        if row.get('Status') == 'not_started' and len(row.get('_teams', {})) == 2
        and row.get('_map_limit') and (start := cs2_start(row.get('Start (UTC)'))) and start > now
    }
    if not upcoming:
        st.info('No upcoming CS2 match with two verified teams and a known map format.')
        return
    requested = {part.strip().casefold() for part in st.query_params.get('cs2_match', '').split('|') if part.strip()}
    fixture_ids = list(upcoming)
    presets = [ident for ident, row in upcoming.items()
               if requested and requested == {team.strip().casefold() for team in row['_teams'].values()}]
    match_id = st.selectbox(
        'CS2 matchup', fixture_ids, index=fixture_ids.index(presets[0]) if len(presets) == 1 else 0,
        format_func=lambda ident: f"{upcoming[ident]['Event']} · {upcoming[ident]['Start (UTC)']}",
    )
    match = upcoming[match_id]
    if requested and len(presets) != 1:
        st.info('The shared matchup is unavailable or ambiguous. Select a current matchup to evaluate it manually.')
    prefilled = len(presets) == 1 and match_id == presets[0]
    requested_player = st.query_params.get('cs2_player', '')[:40] if prefilled else ''
    name = st.text_input('CS2 player handle', value=requested_player).strip()
    if len(name) < 2:
        return
    try:
        players = cached_cs2_players(token, name)
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        cs2_data_warning(exc)
        return
    candidates = {
        player['id']: player for player in players
        if player.get('id') and player.get('name')
        and (player.get('current_team') or {}).get('id') in match['_teams']
    }
    if not candidates:
        st.info('No matching player was found on the two teams listed for this match.')
        return
    player_ids = list(candidates)
    exact = [ident for ident, player in candidates.items() if
             player['name'].casefold() == requested_player.casefold()]
    player_id = st.selectbox('CS2 player', player_ids,
                             index=player_ids.index(exact[0]) if len(exact) == 1 else 0,
                             format_func=lambda ident:
                             f"{candidates[ident]['name']} · {candidates[ident]['current_team']['name']}")
    markets = [label for label, maps in MAP_MARKETS.items() if max(maps) <= match['_map_limit']]
    requested_market = st.query_params.get('cs2_market', '') if prefilled else ''
    market = st.selectbox('CS2 kills market', markets,
                          index=markets.index(requested_market) if requested_market in markets else 0)
    try:
        preset_line = float(st.query_params.get('cs2_line', ''))
    except ValueError:
        preset_line = None
    if not (prefilled and preset_line is not None and 0 <= preset_line <= 150):
        preset_line = None
    line = st.number_input('PrizePicks CS2 line', min_value=0.0, value=preset_line, step=0.5)
    scope = match_id, player_id, market, line
    evaluate = st.button('Evaluate CS2 kills', disabled=line is None)
    shared = (prefilled and len(exact) == 1 and player_id == exact[0]
              and market == requested_market and line == preset_line and line is not None)
    if evaluate or shared:
        st.session_state['cs2_evaluation'] = scope
    if st.session_state.get('cs2_evaluation') != scope:
        return
    try:
        history = cached_cs2_history(token, player_id, now.date() - dt.timedelta(days=1))
        projection = cs2_projection(history, player_id, market)
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        cs2_data_warning(exc)
        return
    if projection is None:
        st.info('At least five verified CS2 maps are required for a projection.')
        return
    maps, per_map, estimate = projection
    st.metric('Projected kills', f'{estimate:.2f}')
    st.caption(f'{per_map:.2f} kills per map across {maps} verified recent maps. The projection assumes the selected maps are played.')
    gap = estimate - line
    if abs(gap) < 0.5:
        st.info('Pass · projection is within half a kill of the line.')
        return
    side = 'More' if gap > 0 else 'Less'
    st.write(f'Baseline lean: **{side} {line:g}**')
    if st.button('Track this CS2 paper pick'):
        kickoff = cs2_start(match['Start (UTC)'])
        observed = dt.datetime.now(dt.timezone.utc)
        if observed >= kickoff:
            st.info('This match has already reached its scheduled start.')
            return
        away, home = map(str, match['_teams'])
        count = save_report_picks(DB, [(
            f'panda:{match_id}', player_id, market, 'CS2', candidates[player_id]['name'],
            match['Event'], kickoff.isoformat(), away, home, observed.isoformat(),
            line, side, estimate, None, None, None, None,
        )])
        st.success('Saved to Pick Report Card.' if count else 'This player, match and market are already tracked.')


def show_dota_props(rows, token):
    st.subheader('Evaluate Dota 2 prop')
    st.caption('Choose an upcoming match and enter the current PrizePicks line. PandaScore supplies match and player data.')
    now = dt.datetime.now(dt.timezone.utc)
    upcoming = {
        row['_match_id']: row for row in rows
        if row.get('Status') == 'not_started' and len(row['_teams']) == 2
        and row['_map_limit'] and (start := cs2_start(row.get('Start (UTC)'))) and start > now
    }
    if not upcoming:
        st.info('No upcoming Dota 2 match with two listed teams and a known series format.')
        return
    match_id = st.selectbox('Dota 2 matchup', upcoming, format_func=lambda ident:
                            f"{upcoming[ident]['Event']} · {upcoming[ident]['Start (UTC)']}")
    match = upcoming[match_id]
    teams = match['_teams']
    st.caption(f"{match['Competition'] or 'Competition unavailable'} · Up to {match['_map_limit']} maps")
    finished = [row for row in rows if row['Status'] == 'finished' and not row['_forfeit']
                and row['_winner'] in row['_teams']
                and (start := cs2_start(row.get('Start (UTC)'))) and start < now]
    finished.sort(key=lambda row: cs2_start(row['Start (UTC)']), reverse=True)
    for team_id, team_name in teams.items():
        sample = [row for row in finished if team_id in row['_teams']][:5]
        if sample:
            wins = sum(row['_winner'] == team_id for row in sample)
            st.caption(f'{team_name} · {wins}–{len(sample) - wins} over {len(sample)} recent available series')
    meetings = [row for row in finished if row['_teams'].keys() == teams.keys()]
    if meetings:
        st.caption(f"Last available head-to-head · {meetings[0]['Score'] or meetings[0]['Event']}")
    if match['_detailed'] is False:
        st.caption('PandaScore does not list detailed stats for this fixture. Paper grading may remain pending.')

    with st.form('dota_player_search_form'):
        handle = st.text_input('Dota 2 player handle', max_chars=40).strip()
        search = st.form_submit_button('Find Dota 2 player')
    search_scope = match_id, sha256(token.encode()).hexdigest()
    if search and len(handle) >= 2:
        st.session_state['dota_player_search'] = search_scope, handle
    saved_search = st.session_state.get('dota_player_search')
    if not saved_search or saved_search[0] != search_scope:
        return
    try:
        players = cached_dota_players(token, saved_search[1])
        candidates = {player['id']: player for player in players
                      if player.get('id') and player.get('name')
                      and (player.get('current_team') or {}).get('id') in teams}
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        cs2_data_warning(exc)
        return
    if not candidates:
        st.info('No matching handle was found on the two listed rosters.')
        return
    player_id = st.selectbox('Dota 2 player', candidates, format_func=lambda ident:
                             f"{candidates[ident]['name']} · {teams[candidates[ident]['current_team']['id']]}")
    st.caption('Roster association comes from PandaScore. Check the organizer for starters and stand-ins.')
    maps = st.selectbox('Dota 2 maps', [label for label, positions in DOTA_MAPS.items()
                                       if max(positions) <= match['_map_limit']])
    st.caption('The projection assumes every selected map is played.')
    stat = st.selectbox('Dota 2 stat', DOTA_STATS)
    market = f'{maps} {stat.lower()}'
    line = st.number_input('PrizePicks Dota 2 line', min_value=0.0, value=None, step=0.5)
    scope = search_scope, player_id, market, line
    if st.button('Evaluate Dota 2 line', disabled=line is None):
        st.session_state['dota_evaluation'] = scope
    if line is None or st.session_state.get('dota_evaluation') != scope:
        return
    team_id = candidates[player_id]['current_team']['id']
    opponent_id = next(ident for ident in teams if ident != team_id)
    try:
        history = cached_dota_history(token, player_id, now.date() - dt.timedelta(days=1))
        projection = dota_projection(history, player_id, team_id, opponent_id, market)
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        cs2_data_warning(exc)
        return
    if projection is None:
        st.info('The baseline needs at least five verified recent maps with this team.')
        return
    estimate = projection['estimate']
    st.metric(f'Projected {stat.lower()}', f'{estimate:g}', delta=f'{estimate - line:+.2f} vs. line', delta_color='off')
    with st.expander('Why this projection?', expanded=True):
        st.write(f"Across {projection['sample']} verified maps in the last 90 days, the player averaged "
                 f"{projection['average']:.2f} per map. The last five averaged {projection['recent']:.2f}. "
                 f"Map-to-map standard deviation was {projection['deviation']:.2f}.")
        st.caption('The baseline weights the full sample at 65% and the latest five maps at 35%.')
        if projection['opponent_games']:
            st.write(f"Against {teams[opponent_id]}, the player averaged {projection['opponent_average']:.2f} "
                     f"over {projection['opponent_games']} maps in this sample.")
            if projection['opponent_weight']:
                st.caption(f"The opponent split gets {projection['opponent_weight']:.1%} of the final estimate.")
        if projection['heroes']:
            st.write('Most played heroes · ' + ', '.join(f'{hero} ({count})' for hero, count in projection['heroes']))
        pace = projection['pace']
        if pace:
            st.write('Economy and experience · ' + ' · '.join(
                f'{pace[key]:g} {label}' for key, label in
                [('gold_per_min', 'gold/min'), ('xp_per_min', 'XP/min')] if key in pace))
        st.caption('Hero and pace data provide context. Current draft and patch effects are unmodeled.')
    if abs(estimate - line) < 0.5:
        st.info('Pass · the projection is within half a stat of the line.')
        return
    side = 'More' if estimate > line else 'Less'
    st.write(f'Baseline lean · **{side} {line:g}**')
    if st.button('Track this Dota 2 paper pick'):
        kickoff = cs2_start(match['Start (UTC)'])
        observed = dt.datetime.now(dt.timezone.utc)
        if observed >= kickoff:
            st.info('This match has already reached its scheduled start.')
            return
        away, home = map(str, teams)
        count = save_report_picks(DB, [(
            f'dota:{match_id}', player_id, market, 'Dota 2', candidates[player_id]['name'],
            match['Event'], kickoff.isoformat(), away, home, observed.isoformat(),
            line, side, estimate, None, None, None, None,
        )])
        st.success('Saved to Pick Report Card.' if count else 'This player, match and market are already tracked.')


def show_supplemental_sport_board(sport, platform='PrizePicks'):
    if sport not in BOARD_SOURCES:
        return
    shared_cs2 = sport == 'CS2' and bool(st.query_params.get('cs2_match'))
    with st.expander(f'{sport} scoreboard and schedule', expanded=shared_cs2 or sport == 'Dota 2'):
        st.caption(f'{BOARD_SOURCES[sport]} · No Odds API credits.')
        panda_key = ''
        if sport in ('CS2', 'Dota 2'):
            default = st.session_state.get('panda_session_token') or configured_panda_key()
            panda_key = st.text_input('PandaScore token', value=default, type='password').strip()
            st.session_state['panda_session_token'] = panda_key
            if not panda_key:
                st.info(f'Enter a PandaScore token to load scheduled, live, and recent {sport} matches.')
                return
        load = st.button(f'Load {sport} scoreboard')
        scope = sport, sha256(panda_key.encode()).hexdigest()
        if load or shared_cs2:
            st.session_state['supplemental_board'] = scope
        if st.session_state.get('supplemental_board') != scope:
            return
        today = dt.datetime.now(ZoneInfo('America/Los_Angeles')).date()
        day = st.date_input(f'{sport} scoreboard date', value=today) if sport in TEAM_MODELS else today
        end = day + dt.timedelta(days=7 if sport == 'MLB' else 0)
        try:
            rows = cached_sport_board(sport, day, end, panda_key)
        except (requests.RequestException, KeyError, TypeError, ValueError):
            st.warning('The scoreboard source is unavailable for this request.')
            return
        if not rows:
            st.info('The source returned no events for this view.')
            return
        board = pd.DataFrame(rows)
        columns = [name for name in ('Event', 'Competition', 'Golfer', 'Start (UTC)', 'Status', 'Score')
                   if name in board and board[name].notna().any()]
        st.dataframe(board[columns], hide_index=True)
        st.caption('Event scores provide game context. They do not grade player props.')
        if sport == 'CS2':
            if platform == 'PrizePicks':
                show_cs2_kills(rows, panda_key)
        if sport == 'Dota 2':
            if platform == 'PrizePicks':
                show_dota_props(rows, panda_key)
        if sport not in TEAM_MODELS:
            return
        eligible = {
            index: row for index, row in enumerate(rows)
            if row.get('_phase') == 'pre' and all(row.get(key) for key in ('_away', '_home', '_season'))
        }
        if not eligible:
            st.caption('No upcoming matchup with verified team IDs and season data in this view.')
            return
        selected = st.selectbox('Matchup to project', eligible, format_func=lambda index: eligible[index]['Event'],
                                key=f'{sport}:fixture_model')
        row = eligible[selected]
        start = pd.to_datetime(row['Start (UTC)'], utc=True, errors='coerce')
        if pd.isna(start) or start <= pd.Timestamp.now(tz='UTC'):
            return
        try:
            projection = cached_team_projection(
                sport, row['_away'], row['_home'], row['_season'],
                start.tz_convert('America/New_York').to_pydatetime(),
            )
        except (requests.RequestException, KeyError, TypeError, ValueError):
            st.warning('Team history is unavailable for this matchup.')
            return
        if projection is None:
            st.info('Each team needs five verified final games before the baseline can be calculated.')
            return
        away, home, spread, total = projection
        for column, label, value in zip(
            st.columns(4), ('Away projection', 'Home projection', 'Model home spread', 'Model total'),
            (away, home, spread, total),
        ):
            column.metric(label, f'{value:g}')
        st.caption('The team-score baseline blends season and last-five scoring and opponent points or runs allowed. Neutral-site and roster effects are not modeled.')


def show_cs2_review(sport):
    review = st.query_params.get('cs2_review')
    if sport != 'CS2' or review not in (
        '2026-11-23', '2026-11-23-pgl', '2026-11-23-pgl50',
        '2026-11-24-upcoming', '2026-11-24-map10',
    ):
        return
    path = Path(__file__).with_name(f'cs2-review-{review}.json')
    rows = json.loads(path.read_text(encoding='utf-8'))
    report = pd.DataFrame(rows)
    if review in ('2026-11-24-upcoming', '2026-11-24-map10'):
        st.subheader(f'{len(report)} CS2 prop reviews · Upcoming matches snapshot')
        starts = pd.to_datetime(report.pop('Start (UTC)'), utc=True)
        pending = starts.gt(pd.Timestamp.now(tz='UTC'))
        report['Scheduled start (Pacific)'] = starts.dt.tz_convert('America/Los_Angeles').dt.strftime('%b %d, %I:%M %p')
        report.loc[~pending, 'Review'] = 'Past scheduled start'
        if review == '2026-11-24-map10':
            st.caption('Map 1 PrizePicks lines and CS2Props model estimates. Pass flags a gap below one stat or a disagreement with trailing results.')
        else:
            st.caption('These are reported PrizePicks lines and historical two-map averages. The review uses a minimum gap of 2 kills or 1.5 headshots for a lean.')
            st.caption('Pass also covers lines without verified matching history. Matches with no reported PrizePicks lines are absent from the table.')
        selected = selectable_prop_table(report, 'cs2_upcoming_reviews')
        if selected is not None:
            row = report.iloc[selected]
            st.markdown(f"**{row['Player']} · {row['Market']} · {row['PrizePicks line']:g}**")
            st.write(f"**{row['Review']}** · {row['Reason']}")
        if review == '2026-11-24-map10':
            st.markdown('[CS2Props lines and projections](https://cs2props.org/)')
        else:
            st.markdown('[Reported PrizePicks lines](https://pickempartyapp.com/props/cs2) · [Two-map history](https://www.cs2observer.com/props)')
        return
    if review == '2026-11-23-pgl50':
        st.subheader('50 new CS2 prop reviews · PGL Singapore')
        pending = pd.to_datetime(report['Start (UTC)'], utc=True).gt(pd.Timestamp.now(tz='UTC'))
        report.loc[~pending, 'Review'] = 'Past scheduled start · reference'
        st.caption(f'{int(pending.sum())} before scheduled start · {int((~pending).sum())} past scheduled start')
        st.caption('PrizePicks lines and source projections were captured before play. Pass means the projection was close to the line, recent data was sparse, or the source flagged disagreement.')
        report['Scheduled start (Pacific)'] = pd.to_datetime(
            report.pop('Start (UTC)'), utc=True
        ).dt.tz_convert('America/Los_Angeles').dt.strftime('%b %d, %I:%M %p')
    elif review == '2026-11-23-pgl':
        st.subheader('10 new CS2 prop reviews · PGL Singapore')
        st.caption('November 23 Pacific · Map 1 kills · 5 More leans, 2 Less leans, 3 passes.')
        st.caption('Lines and projections are from the 17:06 UTC source snapshot. Vantage Sports marks Pass when the projection is less than one kill from the line or the source flags a disagreement.')
    else:
        st.subheader('10 CS2 prop reviews · November 23')
        st.caption('One pregame lean and nine completed-map results. The completed rows compare published lines with final kills.')
    st.dataframe(report, hide_index=True)
    if review in ('2026-11-23-pgl', '2026-11-23-pgl50'):
        st.markdown('[CS2Props lines and projections](https://cs2props.org/)')
    else:
        st.caption('sk0R’s historical average is the figure reported by PickFinder. These are dated observations and the live lines may move.')
        st.markdown(
            '[Reported upcoming line](https://www.pickfinder.app/best-prizepicks-picks/cs2) · '
            '[Reported two-map lines](https://propavix.com/props) · '
            '[Final map stats](https://www.hltv.org/matches/2400373/virtuspro-vs-iberian-soul-nodwin-clutch-series-14)'
        )
    st.download_button(f'Download the {len(report)} CS2 reviews', report.to_csv(index=False),
                       f'cs2-review-{review}.csv', mime='text/csv')


def show_nba_review(sport):
    review = st.query_params.get('nba_review')
    if sport != 'NBA' or review not in (
        '2026-11-23', '2026-11-23-batch2', '2026-11-24', '2026-11-24-scan40', '2026-11-25-scan60',
    ):
        return
    payload = json.loads(Path(__file__).with_name(f'nba-review-{review}.json').read_text(encoding='utf-8'))
    rows = payload['rows']
    slate = payload.get('date', '2026-11-23')
    title = f"{len(rows)}{' more' if review.endswith('-batch2') else ''} NBA prop evaluations"
    st.subheader(f"{title} · {payload.get('date_label', 'November 23')}")
    counts = Counter(row['lean'] for row in rows)
    scan_snapshot = review in ('2026-11-24-scan40', '2026-11-25-scan60')
    rechecks = f" · {counts['Recheck']} rechecks" if scan_snapshot else ''
    st.caption(f"{counts['More']} More leans · {counts['Less']} Less leans · {counts['Pass']} passes{rechecks}")
    if scan_snapshot:
        st.caption('Saved PrizePicks scan. Lines are from the original review and may have changed.')
    st.caption('Select a table row to see the evidence and the reason for its result.')
    summary = pd.DataFrame([
        {'Player': row['player'], **({'Match': row['match']} if 'match' in row else {}), 'Stat': row['stat'],
         'PrizePicks line': row['line'], 'Projection': row['projection'], 'Review': row['lean']}
        for row in rows
    ])
    table = st.dataframe(summary, hide_index=True, height=410, on_select='rerun',
                         selection_mode='single-row', key=f'nba_review:{review}')
    chosen = table.selection.rows
    if chosen:
        index = chosen[0]
    else:
        index = st.selectbox('Player details', range(len(rows)),
                             format_func=lambda i: f"{rows[i]['player']} · {rows[i]['stat']}")
    row = rows[index]
    st.markdown(f"### {row['player']} · {row['stat']}")
    if scan_snapshot:
        gap = row['projection'] - row['line']
        if row.get('match'):
            st.caption(row['match'])
        for column, label, value in zip(
            st.columns(3), ('PrizePicks line', 'Projection', 'Difference'),
            (f"{row['line']:g}", f"{row['projection']:.2f}", f'{gap:+.2f}'),
        ):
            column.metric(label, value)
        reasons = {
            'More': 'The scan favored More at this line.',
            'Less': 'The scan favored Less at this line.',
            'Recheck': 'The model was sensitive to changes in playing time.',
            'Pass': 'The model and sportsbook consensus were within three percentage points.',
        }
        reason = row.get('reason') or reasons.get(row['lean'], 'See the available scan evidence.')
        st.write(f"**{row['lean']}** · {reason}")
        if row.get('season') is not None and row.get('recent') is not None:
            st.caption(f"Season average {row['season']:g} · Last five {row['recent']:g}")
        if row.get('injury'):
            st.caption(f"Injury report at scan time · {row['injury']}")
        if row.get('updated'):
            st.caption(f"Source line update · {row['updated']}")
        if all(field in row for field in ('minutes', 'low_minutes', 'high_minutes')):
            show_minutes_check(row['projection'], row['minutes'], row['line'],
                               row['low_minutes'], row['high_minutes'], f"review_minutes:{review}:{index}")
        return
    st.caption(row['match'])
    if row.get('line_source'):
        st.caption(f"Line source: {row['line_source']}.")
    metrics = st.columns(4)
    for column, label, value in zip(
        metrics, ('Line', 'Season average', 'Last five average', 'Projection'),
        (row['line'], row['season'], row['recent'], row['projection']),
    ):
        column.metric(label, f'{value:g}')
    explanation = (
        f"The {row['projection']:.2f} projection is {abs(row['gap']):.2f} "
        f"{'above' if row['gap'] > 0 else 'below'} the line."
        if row['gap'] else 'The projection matches the posted line.'
    )
    if row['reason']:
        explanation += ' ' + row['reason'] + '.'
    st.write(f"**{row['lean']}** · {explanation}")
    st.caption('Baseline uses 65% season average and 35% last five. A gap under 0.75 or a reported Out, Doubtful, or Questionable status is a pass.')
    games = pd.DataFrame(row['games'])
    rest = (dt.date.fromisoformat(slate) - dt.date.fromisoformat(games.iloc[0]['date'])).days
    st.write(f"**Recent form and rest** · Last recorded appearance was {rest} calendar day{'s' if rest != 1 else ''} before the slate. Last-five stat standard deviation: {games['value'].std():.2f}.")
    above = int(games['value'].gt(row['line']).sum())
    below = int(games['value'].lt(row['line']).sum())
    st.caption(f"Against this line in the last five: {above} above, {below} below, {len(games) - above - below} tied.")
    st.dataframe(games.rename(columns={'date': 'Game date', 'opp': 'Opponent', 'mins': 'Minutes',
                                      'value': row['stat']}), hide_index=True)
    show_minutes_check(row['projection'], games['mins'].mean(), row['line'],
                       games['mins'].min(), games['mins'].max(), f"review_minutes:{review}:{index}")
    if row['stat'] == 'Rebounds':
        snapshot = json.loads(
            Path(__file__).with_name(f'nba-rebound-misses-{slate}.json').read_text(encoding='utf-8')
        )
        show_rebound_opportunity(snapshot.get(row['opponent'], {}))
    st.markdown('#### Availability and matchup')
    away, _, home = row['match'].partition(' at ')
    venue = 'home' if row['team'] == home else 'away'
    st.write(f"**Venue** · {row['team']} plays {venue}. No home/away split adjustment was applied.")
    st.caption(f"Selected NBA injury report entries from {payload.get('injury_label', 'November 23 at noon ET')}. Lineups and absences can change after the report.")
    st.write(f"**Player status** · {row['injury'] or 'No confirmed designation in this review'}")
    for column, label, team in zip(st.columns(2), ('Teammates', 'Opponents'), (row['team'], row['opponent'])):
        with column:
            st.markdown(f'**{label} · {team}**')
            if team in payload['pending']:
                st.caption('The team had not submitted its report at the time checked.')
            else:
                others = [entry for entry in payload['injuries'].get(team, []) if entry[0] != row['player']]
                if not others:
                    st.caption('No other status verified for this review.')
                for name, status, context in others:
                    st.markdown(f'**{name} · {status}**  \n{context}')
    if row['spread'] is not None and row['total'] is not None:
        risk = ' A spread this large can put late minutes at risk.' if row['spread'] >= 12 else ''
        st.write(f"**Game environment** · PrizePicks game total {row['total']:g} and spread magnitude {row['spread']:g}.{risk}")
    st.caption('Injuries and the game environment provide context. The baseline does not adjust for those factors. Starting lineups, usage, position defense, exact sportsbook prices and line movement were unverified in this snapshot.')
    board_url = payload.get('board_url', 'https://www.prizepicks.com/sport/nba')
    injury_url = payload.get('injury_url', 'https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-23_12_00PM.pdf')
    st.markdown(
        f"[Player game log]({row['stats_url']}) · "
        f'[PrizePicks board]({board_url}) · [NBA injury report]({injury_url})'
    )


@st.cache_data(ttl=86400, max_entries=128, show_spinner=False)
def find_weather_locations(city):
    response = requests.get(
        'https://geocoding-api.open-meteo.com/v1/search',
        params={'name': city, 'count': 10, 'language': 'en'},
        timeout=8,
    )
    response.raise_for_status()
    return [place for place in response.json().get('results', [])
            if place.get('timezone') and 'latitude' in place and 'longitude' in place]


@st.cache_data(ttl=900, max_entries=128, show_spinner=False)
def fetch_game_weather(latitude, longitude):
    response = requests.get(
        'https://api.open-meteo.com/v1/forecast',
        params={
            'latitude': latitude, 'longitude': longitude, 'timeformat': 'unixtime',
            'forecast_days': 8, 'wind_speed_unit': 'mph', 'temperature_unit': 'fahrenheit',
            'precipitation_unit': 'inch',
            'hourly': 'temperature_2m,wind_speed_10m,wind_gusts_10m,precipitation_probability',
        },
        timeout=8,
    )
    response.raise_for_status()
    return pd.DataFrame(response.json()['hourly'])


def football_weather_window(forecast, kickoff):
    # Precipitation probability covers the preceding hour. Use the next four hourly readings.
    start = kickoff.timestamp()
    return forecast.loc[(forecast['time'] > start) & (forecast['time'] <= start + 4 * 3600)].copy()


def passing_weather_note(wind, gusts, rain):
    if wind >= 15 or gusts >= 25:
        return 'Wind may make passing less efficient. A shift toward rushing is possible if the game situation allows it.'
    if rain >= 60:
        return 'Precipitation is likely. Check conditions near kickoff. A wet field or ball may affect execution, but this forecast does not establish a pass-volume lean.'
    return 'No strong weather signal for a change in passing volume. Team tendencies and game situation are more useful here.'


def show_football_weather():
    with st.expander('Game weather and passing outlook'):
        st.caption('Choose the stadium city and enter kickoff in that city’s local time. Weather checks use zero odds credits.')
        roof = st.selectbox('Stadium roof at kickoff', ['Unknown', 'Open-air or roof open', 'Closed roof'])
        if roof == 'Closed roof':
            st.info('A closed roof shields the field from outdoor wind and precipitation. No outdoor weather adjustment.')
            return
        city = st.text_input('Stadium city', placeholder='For example, Green Bay')
        if len(city.strip()) < 2:
            return
        try:
            places = find_weather_locations(city.strip())
            if not places:
                st.info('No matching city found. Try the city name without the state.')
                return
            place = st.selectbox(
                'Forecast location', places,
                format_func=lambda item: ', '.join(
                    str(item[field]) for field in ('name', 'admin1', 'country') if item.get(field)
                ),
            )
            timezone = ZoneInfo(place['timezone'])
            today = dt.datetime.now(timezone).date()
            game_date = st.date_input('Local game date', value=today, min_value=today,
                                      max_value=today + dt.timedelta(days=6))
            game_time = st.time_input('Local kickoff time', value=dt.time(13))
            if not st.button('Check game weather'):
                return
            kickoff = dt.datetime.combine(game_date, game_time, tzinfo=timezone)
            if kickoff <= dt.datetime.now(timezone):
                st.info('Choose an upcoming kickoff time.')
                return
            forecast = fetch_game_weather(place['latitude'], place['longitude'])
            window = football_weather_window(forecast, kickoff)
            readings = window[['wind_speed_10m', 'wind_gusts_10m', 'precipitation_probability']]
            if len(readings) != 4 or readings.isna().any().any():
                st.info('Complete wind and precipitation forecasts are unavailable for this game window.')
                return
            wind, gusts, rain = readings.max()
            st.write(f'Peak sustained wind: **{wind:g} mph** · Peak gusts: **{gusts:g} mph** · Highest hourly precipitation chance: **{rain:g}%**')
            if roof == 'Unknown':
                st.info('Confirm whether the roof will be open before applying outdoor conditions.')
            st.write(passing_weather_note(wind, gusts, rain))
            window['time'] = pd.to_datetime(window['time'], unit='s', utc=True).dt.tz_convert(timezone).dt.strftime('%a %I:%M %p %Z')
            st.dataframe(window.rename(columns={
                'time': 'Local hour', 'temperature_2m': 'Temperature (°F)',
                'wind_speed_10m': 'Wind (mph)', 'wind_gusts_10m': 'Gusts (mph)',
                'precipitation_probability': 'Precipitation chance (%)',
            }), hide_index=True)
            st.caption('City-level forecast from [Open-Meteo](https://open-meteo.com/). The wind thresholds are screening rules. Weather does not change the app’s rankings.')
        except (requests.RequestException, ValueError, KeyError):
            st.error('Weather data is temporarily unavailable for this location.')


def nba_context_projection(season_stat, season_minutes, recent_stat, recent_minutes,
                           expected_minutes, recent_share, opponent_allowed=None,
                           league_allowed=None):
    season_rate = season_stat / season_minutes
    recent_rate = recent_stat / recent_minutes
    minutes_adjusted = expected_minutes * (
        season_rate * (1 - recent_share) + recent_rate * recent_share
    )
    factor = 1.0
    if opponent_allowed is not None and league_allowed is not None and league_allowed > 0:
        # Shrink noisy defense-vs-position splits toward average and cap the adjustment.
        factor += max(-0.15, min(0.15, 0.5 * (opponent_allowed / league_allowed - 1)))
    return minutes_adjusted, minutes_adjusted * factor


def nba_projection_inputs(widget_scope='evaluate'):
    with st.expander('NBA lineup, minutes and matchup projection'):
        enabled = st.checkbox('Use this NBA projection in the evaluator', key=widget_scope + ':' + ('Use this NBA projection in the evaluator'))
        if not enabled:
            st.caption('Combine season and recent production per minute with tonight’s expected role.')
            return False, None, ''
        show_research_links('NBA')
        role = st.selectbox('Expected lineup role', ['Unconfirmed', 'Projected starter',
                             'Confirmed starter', 'Bench', 'Out'], key=widget_scope + ':' + ('Expected lineup role'))
        position = st.selectbox('Position for opponent comparison', ['Unspecified', 'PG', 'SG', 'SF', 'PF', 'C'], key=widget_scope + ':' + ('Position for opponent comparison'))
        opponent = st.text_input('Opponent', key=widget_scope + ':' + ('Opponent'))
        rank = st.number_input('Opponent rank vs. this position', min_value=1, max_value=30,
                               value=None, step=1, help='Use 1 for the fewest and 30 for the most allowed for this stat.', key=widget_scope + ':' + ('Opponent rank vs. this position'))
        if role == 'Out':
            st.info('Player marked out. The evaluator will skip this prop.')
            return True, None, ''
        if role in ('Unconfirmed', 'Projected starter'):
            st.caption('Recheck the lineup and adjust expected minutes if the role changes.')
        st.caption('Enter averages for the same stat above. Use the same recent games for the stat and minutes averages.')
        season, recent = st.columns(2)
        with season:
            season_stat = st.number_input('Season average for this stat', min_value=0.0, value=None, key=widget_scope + ':' + ('Season average for this stat'))
            season_minutes = st.number_input('Season minutes per game', min_value=0.1, value=None, key=widget_scope + ':' + ('Season minutes per game'))
        with recent:
            recent_stat = st.number_input('Last 5 games average for this stat', min_value=0.0, value=None, key=widget_scope + ':' + ('Last 5 games average for this stat'))
            recent_minutes = st.number_input('Last 5 games minutes per game', min_value=0.1, value=None, key=widget_scope + ':' + ('Last 5 games minutes per game'))
        expected_minutes = st.number_input('Expected minutes tonight', min_value=0.1, max_value=60.0,
                                           value=None, key=widget_scope + ':' + ('Expected minutes tonight'))
        recent_weight = st.slider('Recent form weight (%)', 0, 100, 40, key=widget_scope + ':' + ('Recent form weight (%)'))
        opponent_allowed = league_allowed = None
        if st.checkbox('Adjust for opponent defense', key=widget_scope + ':' + ('Adjust for opponent defense')):
            st.caption('Use the same stat, position and time window for both averages.')
            opponent_allowed = st.number_input('Opponent average allowed vs. position',
                                               min_value=0.0, value=None, key=widget_scope + ':' + ('Opponent average allowed vs. position'))
            league_allowed = st.number_input('League average allowed vs. position',
                                             min_value=0.1, value=None, key=widget_scope + ':' + ('League average allowed vs. position'))
            if not opponent.strip() or position == 'Unspecified' or None in (opponent_allowed, league_allowed):
                st.info('Enter the opponent, position and both allowed averages to apply the matchup adjustment.')
                return True, None, ''
        if None in (season_stat, season_minutes, recent_stat, recent_minutes, expected_minutes):
            st.info('Enter season, recent and expected minutes values to calculate a projection.')
            return True, None, ''
        baseline, projection = nba_context_projection(
            season_stat, season_minutes, recent_stat, recent_minutes, expected_minutes,
            recent_weight / 100, opponent_allowed, league_allowed,
        )
        st.metric('NBA projection', f'{projection:.2f}')
        st.caption(f'{baseline:.2f} before the matchup adjustment. The matchup adjustment is capped at ±15%.')
        rank_note = f', defense rank {rank}/30 vs. {position}' if rank is not None else ''
        defense_note = (f', opponent allowed {opponent_allowed:g} vs. league {league_allowed:g}'
                        if opponent_allowed is not None else '')
        evidence = (
            f'NBA context: {role}, opponent {opponent or "unspecified"}{rank_note}{defense_note}. '
            f'Season {season_stat:g} in {season_minutes:g} min; last five {recent_stat:g} in '
            f'{recent_minutes:g} min; {recent_weight}% recent weight; {expected_minutes:g} expected min. '
            f'Projection {projection:.2f}.'
        )
        return True, projection, evidence


CONTEXT_FACTORS = (
    ('Opponent defense and matchup', 'Use the defense against this stat or role. In football, a strong run defense may shift volume toward passing, while pass coverage can still reduce efficiency.'),
    ('Game environment', 'Consider pace, expected scoring and outdoor weather where relevant.'),
    ('Home or away split', 'Use comparable home/away samples and account for role changes.'),
    ('Previous meetings', 'Use comparable matchups. Small samples and roster changes reduce their value.'),
    ('Coaching strategy', 'Consider observed play calling, rotation changes and the expected game plan.'),
    ('Fatigue and rest', 'Consider days off, travel, back-to-backs and recent workload.'),
    ('Blowout risk', 'Consider late-game minutes in basketball and expected score-driven play calling in football.'),
)


def research_adjusted_mean(base, independent, reference_usage, expected_usage, adjustments):
    anchor = base if independent is None else (base + independent) / 2
    usage_change = ((expected_usage / reference_usage - 1) * 50
                    if reference_usage is not None and expected_usage is not None else 0)
    usage_change = max(-15, min(15, usage_change))
    total_change = max(-30, min(30, usage_change + sum(adjustments)))
    return anchor, anchor * (1 + total_change / 100), usage_change, total_change


def research_pick_signal(mean, sigma, line, distribution, market_over, moved):
    if float(line).is_integer():
        return 'Pass — whole-number line adds push risk.', None
    over = prob_model(mean, sigma, line, 'Over', distribution)
    gap = (over - market_over) * 100
    if moved:
        return 'Recheck — sportsbook line moved since opening.', gap
    if abs(gap) < 3:
        return 'Pass — model and market are within 3 percentage points.', gap
    return ('Model lean: More' if gap > 0 else 'Model lean: Less'), gap


def pick_research_inputs(sport, baseline, sigma, line, distribution, nba_context_used,
                         widget_scope='evaluate', enabled_by_default=False):
    with st.expander('Research factors, sportsbook odds and line movement'):
        if not st.checkbox('Use research factors in this evaluation', value=enabled_by_default, key=widget_scope + ':' + ('Use research factors in this evaluation')):
            return baseline, None, '', None
        if baseline is None:
            st.info('Finish the baseline projection first.')
            return baseline, None, '', None

        if sport == 'NBA':
            st.markdown('Use [NBA advanced player stats](https://www.nba.com/stats/players/advanced) for usage-rate context.')
        elif sport in ('NFL', 'CFB'):
            st.markdown('Use an opportunity share relevant to the prop, such as targets or carries. See [NFL Next Gen Stats](https://nextgenstats.nfl.com/stats/passing) for NFL passing context.')
        st.caption('Enter evidence-based changes to expected production. Leave unknown factors blank and avoid entering an adjustment already included in the baseline.')
        independent = st.number_input('Second statistical projection (optional)',
                                      min_value=0.0, value=None,
                                      help='Blended equally with the baseline. Leave blank if it relies on the same model.', key=widget_scope + ':' + ('Second statistical projection (optional)'))
        left, right = st.columns(2)
        with left:
            reference = st.number_input('Baseline usage or opportunity share (%)',
                                        min_value=0.1, max_value=100.0, value=None, key=widget_scope + ':' + ('Baseline usage or opportunity share (%)'))
        with right:
            expected = st.number_input('Expected usage or opportunity share (%)',
                                       min_value=0.0, max_value=100.0, value=None, key=widget_scope + ':' + ('Expected usage or opportunity share (%)'))
        if (reference is None) != (expected is None):
            st.info('Enter both usage shares to apply a usage adjustment.')
        adjustments = []
        evidence = []
        if nba_context_used:
            st.caption('If you already applied the NBA defense adjustment above, leave the opponent adjustment blank.')
        for label, help_text in CONTEXT_FACTORS:
            change = st.number_input(f'{label} adjustment (%)', min_value=-20.0,
                                     max_value=20.0, value=None, step=1.0, help=help_text, key=widget_scope + ':' + ('factor:' + label))
            if change is not None:
                adjustments.append(change)
                evidence.append(f'{label} {change:+g}%')
        anchor, adjusted, usage_change, total_change = research_adjusted_mean(
            baseline, independent, reference, expected, adjustments,
        )
        st.metric('Research-adjusted projection', f'{adjusted:.2f}', f'{total_change:+.1f}% vs. baseline blend')
        st.caption(f'Baseline blend {anchor:.2f}. Usage contribution {usage_change:+.1f}%. Combined change capped at ±30%. These weights are screening assumptions.')
        if not distribution.startswith('Poisson'):
            st.caption(f'The evaluator uses the entered standard deviation of {sigma:g} to account for variance.')
        else:
            st.caption('With the Poisson model, variance equals the adjusted mean.')

        opening = st.number_input('Opening sportsbook line', value=None, step=0.5, key=widget_scope + ':' + ('Opening sportsbook line'))
        current = st.number_input('Current sportsbook line', value=None, step=0.5, key=widget_scope + ':' + ('Current sportsbook line'))
        odds_cols = st.columns(2)
        with odds_cols[0]:
            price_over = st.number_input('Current Over odds (American)', value=None, step=1, key=widget_scope + ':' + ('Current Over odds (American)'))
        with odds_cols[1]:
            price_under = st.number_input('Current Under odds (American)', value=None, step=1, key=widget_scope + ':' + ('Current Under odds (American)'))
        moved = opening is not None and current is not None and abs(current - opening) > 1e-9
        if opening is not None and current is not None:
            st.caption(f'Opening {opening:g} → current {current:g} ({current - opening:+g}).')
        market_over = signal = None
        if current is not None and abs(current - line) > 1e-9:
            st.info('Enter sportsbook odds at the exact PrizePicks line for a comparable market estimate.')
        elif current is not None and all(price is not None and abs(price) >= 100
                                         for price in (price_over, price_under)):
            implied_over, implied_under = american_prob(price_over), american_prob(price_under)
            market_over = implied_over / (implied_over + implied_under)
            signal, gap = research_pick_signal(adjusted, sigma, line, distribution, market_over, moved)
            st.write(f'**{signal}**')
            if gap is not None:
                st.caption(f'Model Over estimate differs from the no-vig sportsbook estimate by {gap:+.1f} percentage points.')
        else:
            st.info('Enter two valid sportsbook prices at the current line to generate a research pick signal.')
        recorded = [f'Anchor {anchor:.2f}', f'usage adjustment {usage_change:+.1f}%',
                    f'total adjustment {total_change:+.1f}%', *evidence]
        if opening is not None:
            recorded.append(f'opening line {opening:g}')
        if current is not None:
            recorded.append(f'current line {current:g}')
        if market_over is not None:
            recorded.append(f'sportsbook Over {price_over:+g}, Under {price_under:+g}')
        return adjusted, market_over, 'Research: ' + ', '.join(recorded) + '.', signal


NBA_LOG_RELEASE = ('https://github.com/sportsdataverse/sportsdataverse-data/'
                   'releases/download/nba_stats_player_game_logs')
NBA_STAT_COLUMNS = {'player_points': 'pts', 'player_rebounds': 'reb', 'player_assists': 'ast'}


@st.cache_data(ttl=3600, max_entries=2, show_spinner=False)
def load_nba_game_log(season):
    from io import BytesIO

    response = requests.get(f'{NBA_LOG_RELEASE}/player_game_logs_{season}.csv', timeout=(5, 15))
    response.raise_for_status()
    logs = pd.read_csv(BytesIO(response.content), low_memory=False)
    logs.columns = logs.columns.str.lower()
    required = {'player_id', 'player_name', 'team_name', 'team_abbreviation', 'game_id',
                'game_date', 'matchup', 'min', *NBA_STAT_COLUMNS.values()}
    if not required.issubset(logs.columns):
        raise ValueError('Unexpected NBA game log columns')
    logs['game_id'] = logs['game_id'].astype(str).str.zfill(10)
    logs['game_date'] = pd.to_datetime(logs['game_date'], errors='coerce').dt.date
    for column in ('min', *NBA_STAT_COLUMNS.values()):
        logs[column] = pd.to_numeric(logs[column], errors='coerce')
    logs = logs[logs['game_id'].str.startswith('002') & logs['game_date'].notna()]
    logs = logs[logs['min'] > 0].drop_duplicates(['game_id', 'player_id'])
    logs['opponent'] = logs['matchup'].str.extract(r'(?:@|vs\.?)\s+([A-Z]{2,3})$')[0]
    logs['home'] = ~logs['matchup'].str.contains('@', na=False)
    if {'fga', 'fta', 'tov'}.issubset(logs.columns):
        attempts = logs[['fga', 'fta', 'tov']].apply(pd.to_numeric, errors='coerce')
        possessions = attempts['fga'] + 0.44 * attempts['fta'] + attempts['tov']
        group = [logs['game_id'], logs['team_abbreviation']]
        team_possessions = possessions.groupby(group).transform('sum')
        team_minutes = logs['min'].groupby(group).transform('sum')
        denominator = team_possessions * logs['min']
        logs['estimated_usage'] = (
            100 * possessions * team_minutes / 5 / denominator.where(denominator.gt(0))
        )
    return logs


@st.cache_data(ttl=900, max_entries=2, show_spinner=False)
def load_nba_injury_report(season):
    from io import BytesIO

    url = NBA_LOG_RELEASE.replace('nba_stats_player_game_logs', 'nba_stats_injury_report')
    response = requests.get(f'{url}/injury_report_{season}.csv', timeout=(5, 15))
    response.raise_for_status()
    reports = pd.read_csv(BytesIO(response.content), low_memory=False)
    required = {'game_date', 'report_datetime', 'away_team_abbr', 'home_team_abbr',
                'person_id', 'current_status'}
    if not required.issubset(reports.columns):
        raise ValueError('Unexpected NBA injury report columns')
    reports['game_date'] = pd.to_datetime(reports['game_date'], errors='coerce').dt.date
    reports['report_datetime'] = pd.to_datetime(reports['report_datetime'], errors='coerce')
    reports['person_id'] = pd.to_numeric(reports['person_id'], errors='coerce')
    return reports


def nba_injury_status(reports, date, team_codes, player_id):
    if reports is None:
        return 'Injury feed unavailable', None
    matchup = reports.loc[reports['game_date'].eq(date)
                          & reports['away_team_abbr'].eq(team_codes[0])
                          & reports['home_team_abbr'].eq(team_codes[1])]
    newest = matchup['report_datetime'].max()
    if pd.isna(newest):
        return 'No report found for matchup', None
    statuses = matchup.loc[matchup['report_datetime'].eq(newest)
                           & matchup['person_id'].eq(player_id), 'current_status'].dropna().unique()
    if len(statuses) == 1:
        return str(statuses[0]), str(newest)
    return 'Availability unconfirmed', str(newest)


def nba_matchup_availability(reports, logs, date, team_codes, player_team, player_id):
    if reports is None:
        return {}
    matchup = reports.loc[reports['game_date'].eq(date)
                          & reports['away_team_abbr'].eq(team_codes[0])
                          & reports['home_team_abbr'].eq(team_codes[1])]
    latest = matchup['report_datetime'].max()
    if pd.isna(latest):
        return {}
    roster = (logs.sort_values('game_date').drop_duplicates('player_id', keep='last')
              .set_index('player_id'))
    availability = {'Teammates': [], 'Opponents': []}
    for person_id, entries in matchup[matchup['report_datetime'].eq(latest)].groupby('person_id'):
        if person_id == player_id or person_id not in roster.index:
            continue
        statuses = entries['current_status'].dropna().astype(str).str.strip().unique()
        if len(statuses) != 1 or statuses[0].casefold() not in {'out', 'doubtful', 'questionable', 'probable', 'available'}:
            continue
        player = roster.loc[person_id]
        team = player['team_abbreviation']
        if team not in team_codes:
            continue
        group = 'Teammates' if team == player_team else 'Opponents'
        availability[group].append(f"{player['player_name']} · {statuses[0]}")
    return {f'{group} reported': '; '.join(entries) for group, entries in availability.items() if entries}


def nba_name_key(value):
    import unicodedata

    text = unicodedata.normalize('NFKD', str(value).casefold())
    return ''.join(char for char in text if char.isalnum())


NBA_BOX_URL = ('https://github.com/sportsdataverse/sportsdataverse-data/'
               'releases/download/espn_nba_player_boxscores')


@st.cache_data(ttl=3600, max_entries=2, show_spinner=False)
def load_nba_role_history(season):
    from io import BytesIO

    response = requests.get(f'{NBA_BOX_URL}/player_box_{season}.parquet', timeout=(5, 15))
    response.raise_for_status()
    columns = ['game_id', 'game_date', 'athlete_display_name', 'athlete_id',
               'athlete_position_abbreviation', 'team_display_name', 'opponent_team_display_name',
               'starter', 'minutes', 'points', 'rebounds', 'assists']
    frame = pd.read_parquet(BytesIO(response.content), columns=columns)
    frame['game_date'] = pd.to_datetime(
        frame['game_date'].astype(str).str.slice(0, 10), errors='coerce'
    ).dt.date
    frame['minutes'] = pd.to_numeric(frame['minutes'], errors='coerce')
    frame = frame.loc[frame['minutes'].gt(0)].drop_duplicates(['game_id', 'athlete_id']).copy()
    frame['athlete_key'] = frame['athlete_display_name'].map(nba_name_key)
    frame['team_key'] = frame['team_display_name'].map(nba_name_key)
    frame['opponent_key'] = frame['opponent_team_display_name'].map(nba_name_key)
    frame['position'] = frame['athlete_position_abbreviation'].astype('string').str.upper()
    return frame


def nba_role_context(history, player_name, team_name, opponent_name, game_date, stat):
    if history is None:
        return {}, None
    history = history.loc[history['game_date'].lt(game_date)]
    player = history.loc[history['athlete_key'].eq(nba_name_key(player_name))
                         & history['team_key'].eq(nba_name_key(team_name))]
    player = player.sort_values(['game_date', 'game_id'])
    if player['athlete_id'].nunique() != 1 or len(player) < 3:
        return {}, None
    recent = player.tail(5)
    starter = recent['starter'].astype('string').str.lower().map({'true': True, 'false': False})
    count = int(starter.sum()) if starter.notna().all() else None
    position = recent.iloc[-1]['position']
    projected_minutes = None
    if count in (0, len(recent)):
        projected_minutes = float(recent['minutes'].mean())
    result = {'Starts in recent games': f'{count}/{len(recent)}' if count is not None else None,
              'Estimated role': ('Likely starter' if count == len(recent) else
                                 'Likely bench' if count == 0 else 'Mixed or unverified')}
    if pd.isna(position) or position not in ('G', 'F', 'C'):
        return result, projected_minutes
    result['Listed position group'] = position
    stat_column = {'pts': 'points', 'reb': 'rebounds', 'ast': 'assists'}[stat]
    allowed = (history.loc[history['position'].eq(position)]
               .groupby(['opponent_key', 'game_id'])[stat_column].sum(min_count=1)
               .groupby(level=0).agg(['mean', 'count']))
    eligible = allowed.loc[allowed['count'].ge(5), 'mean'].dropna()
    opponent_key = nba_name_key(opponent_name)
    if opponent_key in eligible.index:
        result['Opponent rank vs position group'] = f'{int(eligible.rank(method="min")[opponent_key])}/{len(eligible)}'
        result['Opponent per-game allowed to position group'] = round(float(eligible[opponent_key]), 2)
        result['League per-game allowed to position group'] = round(float(eligible.mean()), 2)
    return result, projected_minutes


def nba_automatic_context(logs, row, game_date, injuries=None, role_history=None):
    stat = NBA_STAT_COLUMNS.get(row['market'])
    teams = str(row['event']).split(' at ', 1)
    if stat is None or len(teams) != 2:
        return {'Auto status': 'Unsupported market or matchup format'}
    names = logs['player_name'].map(nba_name_key)
    player = logs.loc[names.eq(nba_name_key(row['player'])) & logs[stat].notna()]
    player = player.sort_values(['game_date', 'game_id'])
    if player['player_id'].nunique() != 1 or len(player) < 5:
        return {'Auto status': 'Insufficient or ambiguous player history'}
    team_codes = []
    for team in teams:
        full_name = nba_name_key(team)
        codes = logs.loc[logs['team_name'].map(
            lambda value: full_name.endswith(nba_name_key(value)) if pd.notna(value) else False
        ), 'team_abbreviation'].dropna().unique()
        if len(codes) != 1:
            return {'Auto status': 'Could not verify both teams'}
        team_codes.append(codes[0])
    team = player.iloc[-1]['team_abbreviation']
    if team not in team_codes or team_codes[0] == team_codes[1]:
        return {'Auto status': 'Player’s last recorded team differs from the matchup'}
    on_current_team = player[player['team_abbreviation'].eq(team)]
    if len(on_current_team) < 5:
        return {'Auto status': 'Fewer than five appearances with the matchup team'}
    home = team == team_codes[1]
    opponent = team_codes[int(not home)]
    recent = on_current_team.tail(5)
    low_minutes = float(recent['min'].min())
    high_minutes = float(recent['min'].max())
    same_venue = on_current_team[on_current_team['home'].eq(home)]
    against = on_current_team[on_current_team['opponent'].eq(opponent)]
    latest_date = recent.iloc[-1]['game_date']
    role, role_minutes = nba_role_context(
        role_history, row['player'], teams[int(home)], teams[int(not home)], game_date, stat,
    )
    mean_minutes = role_minutes if role_minutes is not None else float(recent['min'].mean())
    season_rate = player[stat].sum() / player['min'].sum()
    recent_rate = recent[stat].sum() / recent['min'].sum()
    rate = 0.6 * season_rate + 0.4 * recent_rate
    if len(same_venue) >= 5:
        rate = 0.9 * rate + 0.1 * same_venue[stat].sum() / same_venue['min'].sum()
    defense = logs.groupby(['opponent', 'game_id'])[stat].sum(min_count=1).groupby(level=0).agg(['mean', 'count'])
    eligible = defense[defense['count'] >= 5]['mean'].dropna()
    defense_rank = None
    defense_basis = None
    if opponent in eligible and eligible.mean() > 0:
        defense_rank = int(eligible.rank(method='min')[opponent])
        defense_basis = 'Team defense'
        allowed, average = eligible[opponent], eligible.mean()
        if role.get('League per-game allowed to position group', 0) > 0:
            defense_basis = 'Position group defense'
            allowed = role['Opponent per-game allowed to position group']
            average = role['League per-game allowed to position group']
        rate *= 1 + max(-0.08, min(0.08, 0.25 * (allowed / average - 1)))
    if len(against) >= 3:
        matchup_rate = against[stat].sum() / against['min'].sum()
        rate = 0.95 * rate + 0.05 * matchup_rate
    usage = {}
    if 'estimated_usage' in player:
        season_usage = player['estimated_usage'].dropna()
        recent_usage = recent['estimated_usage'].dropna()
        if not season_usage.empty and not recent_usage.empty:
            usage = {'Season usage estimate (%)': round(float(season_usage.mean()), 1),
                     'Last five usage estimate (%)': round(float(recent_usage.mean()), 1)}
    # Per-minute production already reflects usage, so report it alongside the projection.
    projection = mean_minutes * rate
    deviation = float(player[stat].std())
    injury, injury_timestamp = nba_injury_status(injuries, game_date, team_codes,
                                               player.iloc[-1]['player_id'])
    injury_key = injury.strip().casefold()
    status = 'Pregame model · lineup unverified'
    if injury_key == 'out':
        status = 'Hold · player reported out'
    elif injury_key in ('questionable', 'doubtful'):
        status = 'Hold · player availability uncertain'
    lean = None
    gap = None
    model_over = None
    line = float(row['line'])
    minutes_sensitive = mean_minutes > 0 and (
        minutes_scenario(projection, mean_minutes, line, low_minutes)[1]
        != minutes_scenario(projection, mean_minutes, line, high_minutes)[1]
    )
    if not status.startswith('Hold') and deviation > 0 and not line.is_integer():
        model_over = 100 * prob_model(projection, deviation, line, 'Over',
                                     'Normal (continuous approximation)')
    estimate = row.get('market_estimate_pct')
    market_side = row.get('side')
    if not status.startswith('Hold') and pd.notna(estimate) and market_side in ('Over', 'Under') and deviation > 0:
        market_over = (estimate if market_side == 'Over' else 100 - estimate) / 100
        moved = row.get('PrizePicks line change')
        lean, gap = research_pick_signal(projection, deviation, float(row['line']),
                                         'Normal (continuous approximation)', market_over, False)
        if pd.notna(moved) and moved != 0 and lean.startswith('Model lean'):
            lean = 'Recheck — PrizePicks line changed since first observed.'
        if lean.startswith('Model lean') and row.get('Lopsided game flag') == 'Elevated':
            lean = 'Recheck — large market spread could change minutes.'
        if lean.startswith('Model lean') and minutes_sensitive:
            lean = 'Recheck — sensitive to playing time.'
    return {
        'Auto status': status, 'Injury report status': injury, 'Report time (Eastern)': injury_timestamp,
        'NBA player ID': int(player.iloc[-1]['player_id']),
        'NBA away team': team_codes[0], 'NBA home team': team_codes[1],
        'NBA opponent team': opponent,
        'Season average': round(float(player[stat].mean()), 2),
        'Last five average': round(float(recent[stat].mean()), 2),
        'Expected minutes': round(mean_minutes, 1),
        'Recent low minutes': low_minutes, 'Recent high minutes': high_minutes,
        'Minutes sensitivity': 'Recheck' if minutes_sensitive else None,
        'Home/away average': round(float(same_venue[stat].mean()), 2) if not same_venue.empty else None,
        'Head-to-head average': round(float(against[stat].mean()), 2) if not against.empty else None,
        'Prior meetings': len(against),
        'Days since last appearance': (game_date - latest_date).days,
        'Opponent team defense rank': f'{defense_rank}/{len(eligible)}' if defense_rank else None,
        'Projected stat': round(projection, 2), 'Season standard deviation': round(deviation, 2),
        'Model–market Over gap (pp)': round(gap, 1) if gap is not None else None,
        'Model signal': lean, 'Model More estimate (%)': model_over,
        'Venue split games': len(same_venue), 'Defense factor applied': defense_basis,
        'Last player game in feed': str(latest_date), **role, **usage,
        **nba_matchup_availability(injuries, logs, game_date, team_codes, team,
                                   player.iloc[-1]['player_id']),
    }


def pick_evaluation(row):
    def value(column):
        item = row.get(column)
        return item if pd.notna(item) else None

    market_side = value('side')
    consensus = value('market_estimate_pct')
    books = value('comparison_books')
    model_more = value('Model More estimate (%)')
    offer_line = float(row['line'])
    model_stat = value('Projected stat')
    signal = value('Model signal')
    status = value('Auto status')
    if status and status.startswith('Hold'):
        outcome = status
    elif signal:
        outcome = signal
    elif model_stat is not None and offer_line.is_integer():
        outcome = 'Pass — whole-number line adds push risk.'
    elif model_stat is not None:
        outcome = 'Projection only — no exact sportsbook comparison for a pick signal.'
    elif consensus is not None and market_side in ('Over', 'Under'):
        if math.isclose(float(consensus), 50):
            outcome = 'Sportsbook consensus is even'
        else:
            outcome = 'Sportsbook lean: ' + ('More' if market_side == 'Over' else 'Less')
    else:
        outcome = 'No automatic lean available'

    factors = []
    if consensus is not None and market_side in ('Over', 'Under'):
        if math.isclose(float(consensus), 50):
            summary = 'The sportsbook comparison was evenly split at this exact line'
        else:
            label = 'More' if market_side == 'Over' else 'Less'
            summary = f'Sportsbook consensus at this exact line was {consensus:.1f}% {label}'
        if books is not None:
            summary += f' across {int(books)} books'
        factors.append(summary + '.')
    elif value('status'):
        factors.append(f"Sportsbook comparison status: {value('status')}.")

    if model_stat is not None:
        minutes = value('Expected minutes')
        role = f' at {minutes:.1f} expected minutes' if minutes is not None else ''
        projection_detail = f'Projected {model_stat:.2f}{role}.'
        season, recent = value('Season average'), value('Last five average')
        if season is not None and recent is not None:
            projection_detail += f' Season average {season:.2f}, last five {recent:.2f}.'
        factors.append(projection_detail)
        opportunities = value('Recent opportunities per game')
        if opportunities is not None:
            factors.append(f"Recent opportunities: {opportunities:.1f} per game versus "
                           f"{value('Season opportunities per game'):.1f} this season.")
        if value('Season standard deviation') is not None:
            factors.append(f"Season standard deviation was {value('Season standard deviation'):.2f}.")
        if value('Venue split games') and value('Venue split games') >= 5:
            factors.append(f"The model used the matching home/away split ({int(value('Venue split games'))} games).")
        if value('Prior meetings') and value('Prior meetings') >= 3:
            factors.append(f"The model lightly weighted {int(value('Prior meetings'))} games against this opponent.")
        if value('Defense factor applied'):
            rank_column = ('Opponent rank vs position group' if
                           value('Defense factor applied') == 'Position group defense' else
                           'Opponent team defense rank')
            rank = value(rank_column)
            factors.append(f"{value('Defense factor applied')} adjusted the projection"
                           + (f' (rank {rank}, 1 allows the least).' if rank else '.'))
        if value('Injury report status'):
            factors.append(f"Injury report: {value('Injury report status')}. Starting lineup is unverified.")
        if value('Practice status'):
            factors.append(f"NFL practice report: {value('Practice status')}.")
        if value('Latest depth chart'):
            factors.append(f"Latest team depth chart: {value('Latest depth chart')}.")
        if value('Estimated role'):
            factors.append(f"Estimated role: {value('Estimated role')} based on recent starts.")
    elif status:
        factors.append(f'Player research: {status}.')
    if value('PrizePicks line change'):
        factors.append(f"PrizePicks moved {value('PrizePicks line change'):+g} from the first recorded line.")
    if value('Lopsided game flag') == 'Elevated':
        factors.append('The game spread flagged increased blowout risk.')
    gap = value('Model–market Over gap (pp)')
    if gap is not None:
        factors.append(f'Model More estimate is {gap:+.1f} percentage points from sportsbook consensus.')
    rating, separator, reason = outcome.partition(' — ')
    if not separator and outcome.startswith('Hold'):
        rating, _, reason = outcome.partition(' · ')
        injury = value('Injury report status')
        practice = value('Practice status')
        evidence = ', '.join(str(item) for item in (injury, practice) if item)
        if evidence:
            reason = f'{reason.rstrip(".")} ({evidence})'
    elif outcome.startswith('Model lean:'):
        gap = value('Model–market Over gap (pp)')
        if gap is not None:
            reason = f'The model’s More estimate is {abs(gap):.1f} percentage points {"above" if gap >= 0 else "below"} sportsbook consensus.'
        else:
            reason = f'The model favors this side at the posted {offer_line:g} line.'
    elif outcome.startswith('Sportsbook'):
        reason = factors[0]
    elif not separator:
        reason = (f'{status.rstrip(".")}.' if status else
                  'No verified player projection or exact-line sportsbook comparison is available.')
    return rating, reason.rstrip('.') + '.', model_more, factors


def show_pick_evaluation(row):
    market = str(row['market']).removeprefix('player_').replace('_', ' ')
    st.markdown(f"**{row['player']} · {market} {row['line']:g}**")
    st.caption(row['event'])
    rating, reason, model_more, factors = pick_evaluation(row)
    st.markdown(f'**{rating}** · {reason}')
    if model_more is not None:
        st.caption(f'Model estimates · More {model_more:.1f}% · Less {100 - model_more:.1f}%')
    if factors:
        supporting = [factor for factor in factors if factor != reason]
        if supporting:
            st.markdown('\n'.join(f'- {factor}' for factor in supporting))
    else:
        st.info('No verified data is available to evaluate this line.')


def selectable_prop_table(picks, key, columns=None):
    st.caption('Click a row to see its evaluation.')
    selection = st.dataframe(picks, key=key, on_select='rerun', selection_mode='single-row',
                             hide_index=True, use_container_width=True, height=320, column_order=columns)
    return next(iter(selection.selection.rows), None)


def project_nba_matchup(logs, away, home):
    totals = logs.groupby(['game_date', 'game_id', 'team_abbreviation'])['pts'].sum(min_count=1)
    games = totals.unstack().dropna(how='all')
    games = games.loc[games.notna().sum(axis=1).eq(2)]
    histories = {}
    for team in (away, home):
        if team not in games:
            return {}
        played = games.loc[games[team].notna()]
        histories[team] = list(zip(played[team], played.drop(columns=team).sum(axis=1)))
    projection = score_projection(histories[away], histories[home])
    if projection is None:
        return {}
    return dict(zip(('Projected away score', 'Projected home score',
                     'Model home spread', 'Model game total'), projection))


def automatic_nba_board(picks, board_scope):
    freshness = int(dt.datetime.now(dt.timezone.utc).timestamp()) // 900
    scope = sha256((repr(board_scope) + picks.to_json() + str(freshness)).encode()).hexdigest()[:16]
    saved = st.session_state.get('auto_nba_board')
    if not saved or saved[0] != scope:
        try:
            today = dt.datetime.now(ZoneInfo('America/New_York')).date()
            season = today.year + int(today.month >= 9)
            with st.spinner('Checking NBA game logs…'):
                logs = load_nba_game_log(season)
                try:
                    injuries = load_nba_injury_report(season)
                except (requests.RequestException, ValueError, KeyError, TypeError):
                    injuries = None
                try:
                    role_history = load_nba_role_history(season)
                except (requests.RequestException, ValueError, KeyError, ImportError, TypeError):
                    role_history = None
                rows = []
                game_models = {}
                rebound_inputs = {}
                for _, row in picks.iterrows():
                    start = pd.to_datetime(row.get('starts_utc'), utc=True, errors='coerce')
                    date = today if pd.isna(start) else start.tz_convert('America/New_York').date()
                    history = logs[logs['game_date'] < date]
                    context = nba_automatic_context(history, row, date, injuries, role_history)
                    away, home = context.get('NBA away team'), context.get('NBA home team')
                    matchup = date, away, home
                    if away and home and matchup not in game_models:
                        game_models[matchup] = project_nba_matchup(history, away, home)
                    shot_context = {}
                    if row['market'] == 'player_rebounds' and context.get('NBA opponent team'):
                        if date not in rebound_inputs:
                            rebound_inputs[date] = missed_shot_profiles(history, date)
                        shot_context = rebound_inputs[date].get(context['NBA opponent team'], {})
                    rows.append({**row.to_dict(), **context, **game_models.get(matchup, {}), **shot_context})
            saved = (scope, pd.DataFrame(rows))
            st.session_state['auto_nba_board'] = saved
        except (requests.RequestException, ValueError, KeyError, TypeError):
            st.error('NBA game logs are temporarily unavailable from the source.')
            return picks
    return saved[1]


NFL_STATS = {
    'player_pass_yds': ('passing_yards', 'attempts'),
    'player_rush_yds': ('rushing_yards', 'carries'),
    'player_reception_yds': ('receiving_yards', 'targets'),
}
NFL_TEAM_CODES = dict(zip(
    'cardinals falcons ravens bills panthers bears bengals browns cowboys broncos lions packers '
    'texans colts jaguars chiefs raiders chargers rams dolphins vikings patriots saints giants '
    'jets eagles steelers 49ers seahawks buccaneers titans commanders'.split(),
    'ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LV LAC LA MIA MIN NE NO NYG '
    'NYJ PHI PIT SF SEA TB TEN WAS'.split(),
))

# Match the schedule's venue, including when teams play at a neutral site.
NFL_OUTDOOR_COORDINATES = {
    'Lambeau Field': (44.5013, -88.0622),
    'Soldier Field': (41.8623, -87.6167),
    'Acrisure Stadium': (40.4468, -80.0158),
    'Lincoln Financial Field': (39.9008, -75.1675),
    'GEHA Field at Arrowhead Stadium': (39.0489, -94.4839),
    'Arrowhead Stadium': (39.0489, -94.4839),
    'MetLife Stadium': (40.8135, -74.0745),
    'Gillette Stadium': (42.0909, -71.2643),
    'Levi\'s Stadium': (37.4033, -121.9694),
    'Lumen Field': (47.5952, -122.3316),
}


@st.cache_data(ttl=1800, max_entries=64, show_spinner=False)
def nfl_weather(venue, roof, kickoff):
    if roof in {'dome', 'closed'}:
        return {'Weather review': 'Covered venue'}
    coordinates = NFL_OUTDOOR_COORDINATES.get(venue)
    if roof != 'outdoors' or coordinates is None:
        return {'Weather review': 'Outdoor venue or roof status unverified'}
    if pd.isna(kickoff):
        return {'Weather review': 'Kickoff time unverified'}
    now = dt.datetime.now(dt.timezone.utc)
    if not now <= kickoff < now + dt.timedelta(days=7):
        return {'Weather review': 'Kickoff outside forecast window'}
    headers = {'User-Agent': 'Vantage Sports (github.com/masonacarolus11-spec/prop-edge-lab)',
               'Accept': 'application/geo+json'}
    try:
        with requests.Session() as session:
            root = 'https://api.weather.gov'
            response = session.get(f'{root}/points/{coordinates[0]},{coordinates[1]}',
                                   headers=headers, timeout=(2, 4))
            response.raise_for_status()
            endpoint = urlsplit(response.json()['properties']['forecastHourly'])
            if endpoint.scheme != 'https' or endpoint.netloc != 'api.weather.gov':
                raise ValueError('Unexpected forecast endpoint')
            response = session.get(root + endpoint.path, headers=headers, timeout=(2, 4))
            response.raise_for_status()
            periods = response.json()['properties']['periods']
        relevant = [period for period in periods
                    if pd.Timestamp(period['endTime']) > kickoff
                    and pd.Timestamp(period['startTime']) < kickoff + dt.timedelta(hours=3)]
        if not relevant:
            return {'Weather review': 'No forecast covers kickoff'}
        winds = [int(match.group(1)) for period in relevant
                 if (match := re.fullmatch(r'(\d+) mph', period.get('windSpeed', '')))]
        rain = [period.get('probabilityOfPrecipitation', {}).get('value') for period in relevant]
        rain = [value for value in rain if isinstance(value, (int, float))]
        if not winds and not rain:
            return {'Weather review': 'Forecast missing usable wind and precipitation'}
        review = 'Recheck · wind or precipitation risk' if (
            max(winds, default=0) >= 20 or max(rain, default=0) >= 70
        ) else 'Forecast checked'
        return {'Weather review': review, 'Weather source': 'National Weather Service',
                'Forecast peak wind (mph)': max(winds, default=None),
                'Forecast peak precipitation chance (%)': max(rain, default=None)}
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
        return {'Weather review': 'NWS forecast unavailable'}


@st.cache_data(ttl=3600, max_entries=2, show_spinner=False)
def load_nfl_history(season):
    from io import BytesIO

    root = 'https://github.com/nflverse/nflverse-data/releases/download'
    sources = {
        'stats': f'{root}/stats_player/stats_player_week_{season}.csv',
        'games': f'{root}/schedules/games.csv',
    }
    frames = {}
    for label, url in sources.items():
        response = requests.get(url, timeout=(5, 15))
        response.raise_for_status()
        frames[label] = pd.read_csv(BytesIO(response.content), low_memory=False)
    games = frames['games']
    games = games.loc[games['season'].eq(season) & games['game_type'].eq('REG')].copy()
    games['gameday'] = pd.to_datetime(games['gameday'], errors='coerce').dt.date
    stats = frames['stats']
    stats = stats.loc[stats['season_type'].eq('REG')].merge(
        games[['game_id', 'gameday', 'home_team']], on='game_id', validate='many_to_one',
    )
    stats['name_key'] = stats['player_display_name'].map(nba_name_key)
    return stats, games


@st.cache_data(ttl=900, max_entries=4, show_spinner=False)
def load_nfl_availability(season, kind):
    from io import BytesIO

    root = 'https://github.com/nflverse/nflverse-data/releases/download'
    if kind == 'injuries':
        file = f'injuries_{season}.csv'
        columns = ['team', 'week', 'season_type', 'gsis_id', 'report_status',
                   'practice_status', 'date_modified']
        timestamp = 'date_modified'
    else:
        file = f'depth_charts_{season}.parquet'
        columns = ['team', 'gsis_id', 'dt', 'pos_abb', 'pos_rank']
        timestamp = 'dt'
    response = requests.get(f'{root}/{kind}/{file}', timeout=(5, 15))
    response.raise_for_status()
    data = BytesIO(response.content)
    if kind == 'injuries':
        frame = pd.read_csv(data, usecols=lambda column: column in columns, dtype={'gsis_id': 'string'})
        if not (set(columns) - {timestamp}).issubset(frame.columns):
            raise ValueError('Unexpected NFL injury report columns')
        frame = frame.loc[frame['season_type'].eq('REG')].copy()
    else:
        frame = pd.read_parquet(data, columns=columns)
        frame['pos_rank'] = pd.to_numeric(frame['pos_rank'], errors='coerce')
    frame['published_utc'] = pd.to_datetime(frame.get(timestamp, pd.Series(pd.NaT, index=frame.index)),
                                           utc=True, errors='coerce')
    return frame if kind == 'injuries' else frame.dropna(subset=['published_utc'])


def nfl_availability(injuries, depth, team, player_id, week, cutoff):
    evidence = {}
    if injuries is not None:
        team_reports = injuries.loc[
            injuries['team'].eq(team) & injuries['week'].eq(week)
            & (injuries['published_utc'].isna() | injuries['published_utc'].le(cutoff))
        ]
        reports = team_reports.loc[team_reports['gsis_id'].eq(str(player_id))]
        if not reports.empty:
            dated = reports.loc[reports['published_utc'].notna()]
            if not dated.empty:
                newest = dated['published_utc'].max()
                reports = dated.loc[dated['published_utc'].eq(newest)]
                evidence['NFL injury report time (UTC)'] = newest.isoformat()
            def status_priority(report):
                status = str(report['report_status']).strip().casefold()
                practice = str(report['practice_status']).strip().casefold()
                return (
                    {'out': 4, 'ir': 4, 'injured reserve': 4, 'doubtful': 3, 'questionable': 2}.get(status, 0),
                    'did not participate' in practice or practice == 'dnp',
                )
            latest = max(reports.to_dict('records'), key=status_priority)
            status = latest['report_status']
            practice = latest['practice_status']
            evidence['Injury report status'] = str(status) if pd.notna(status) and str(status).strip() else 'No game designation reported'
            evidence['Practice status'] = str(practice) if pd.notna(practice) else None
        elif team_reports.empty:
            evidence['Injury report status'] = 'No team report found for this matchup week'
        else:
            evidence['Injury report status'] = 'No player entry found for this matchup week'
    else:
        evidence['Injury report status'] = 'Injury feed unavailable'
    if depth is not None:
        charts = depth.loc[depth['team'].eq(team) & depth['published_utc'].le(cutoff)]
        newest = charts['published_utc'].max()
        if pd.notna(newest) and cutoff - newest <= pd.Timedelta(days=7):
            player = charts.loc[charts['published_utc'].eq(newest)
                                & charts['gsis_id'].astype('string').eq(str(player_id))]
            roles = player[['pos_abb', 'pos_rank']].dropna().drop_duplicates()
            if not roles.empty:
                evidence['Latest depth chart'] = ', '.join(
                    f'{position} rank {rank:g}' for position, rank in
                    roles.itertuples(index=False, name=None)
                )
                evidence['NFL depth chart time (UTC)'] = newest.isoformat()
    return evidence


def nfl_automatic_context(stats, games, row, today, injuries=None, depth=None, now=None):
    settings = NFL_STATS.get(row['market'])
    teams = str(row['event']).split(' at ', 1)
    codes = [NFL_TEAM_CODES.get(team.lower().strip().split()[-1]) if team.strip() else None
             for team in teams]
    if settings is None or len(codes) != 2 or not all(codes):
        return {'Auto status': 'NFL market or matchup could not be verified'}
    stat, opportunities = settings
    upcoming = games.loc[games['away_team'].eq(codes[0]) & games['home_team'].eq(codes[1])
                         & games['gameday'].ge(today) & games['home_score'].isna()]
    start = pd.to_datetime(row.get('starts_utc'), utc=True, errors='coerce')
    if pd.notna(start):
        date = start.tz_convert('America/New_York').date()
        upcoming = upcoming.loc[upcoming['gameday'].eq(date)]
    if len(upcoming) != 1:
        return {'Auto status': 'Could not verify a single upcoming NFL game'}
    game = upcoming.iloc[0]
    kickoff = start
    if pd.isna(kickoff) and pd.notna(game.get('gametime')):
        kickoff = pd.to_datetime(f"{game['gameday']} {game['gametime']}", errors='coerce')
        if pd.notna(kickoff) and kickoff.tzinfo is None:
            kickoff = kickoff.tz_localize('America/New_York', ambiguous='NaT', nonexistent='NaT')
    weather = nfl_weather(game.get('stadium'), str(game.get('roof')).strip().casefold(), kickoff)
    date = game['gameday']
    history = stats.loc[stats['gameday'].lt(date)]
    player = history.loc[history['name_key'].eq(nba_name_key(row['player'])) & history[stat].notna()]
    player = player.sort_values(['gameday', 'game_id'])
    if len(player) < 5 or player['player_id'].nunique() != 1:
        return {'Auto status': 'Fewer than five verified player games or ambiguous player name', **weather}
    team = player.iloc[-1]['team']
    own_team = player.loc[player['team'].eq(team)]
    if team not in codes or len(own_team) < 5:
        return {'Auto status': 'Insufficient player history with the matchup team', **weather}
    home = team == codes[1]
    opponent = codes[0 if home else 1]
    recent = own_team.tail(5)
    season_mean, recent_mean = float(player[stat].mean()), float(recent[stat].mean())
    projection = 0.6 * season_mean + 0.4 * recent_mean
    venue = own_team.loc[own_team['home_team'].eq(team).eq(home)]
    meetings = own_team.loc[own_team['opponent_team'].eq(opponent)]
    if len(venue) >= 5:
        projection = 0.9 * projection + 0.1 * float(venue[stat].mean())
    if len(meetings) >= 3:
        projection = 0.95 * projection + 0.05 * float(meetings[stat].mean())
    defense = history.groupby(['opponent_team', 'game_id'])[stat].sum(min_count=1)
    eligible = defense.groupby(level=0).agg(['mean', 'count'])
    eligible = eligible.loc[eligible['count'].ge(5), 'mean'].dropna()
    rank = None
    if opponent in eligible.index and eligible.mean() > 0:
        rank = f'{int(eligible.rank(method="min")[opponent])}/{len(eligible)}'
        projection *= 1 + max(-0.08, min(0.08, 0.25 * (eligible[opponent] / eligible.mean() - 1)))
    now = now if now is not None else pd.Timestamp.now(tz='UTC')
    cutoff = min(now, start) if pd.notna(start) else now
    availability = nfl_availability(injuries, depth, team, player.iloc[-1]['player_id'],
                                    game.get('week'), cutoff)
    designation = availability['Injury report status'].strip().casefold()
    hold = designation in {'out', 'doubtful', 'questionable', 'ir', 'injured reserve'}
    if not hold and designation == 'no game designation reported':
        practice = (availability.get('Practice status') or '').casefold()
        hold = 'did not participate' in practice or practice.strip() == 'dnp'
    sigma = float(player[stat].std())
    line = float(row['line'])
    model_over = 100 * prob_model(projection, sigma, line, 'Over', 'Normal') if not hold and sigma > 0 and not line.is_integer() else None
    lean = gap = None
    odds = row.get('market_estimate_pct')
    side = row.get('side')
    if not hold and pd.notna(odds) and side in ('Over', 'Under') and sigma > 0:
        over = (odds if side == 'Over' else 100 - odds) / 100
        lean, gap = research_pick_signal(projection, sigma, line, 'Normal', over, False)
        if lean.startswith('Model lean'):
            if pd.notna(row.get('PrizePicks line change')) and row['PrizePicks line change'] != 0:
                lean = 'Recheck — PrizePicks line changed since first observed.'
            elif row.get('Lopsided game flag') == 'Elevated':
                lean = 'Recheck — large market spread could change late-game play calling.'
            elif weather['Weather review'].startswith('Recheck'):
                lean = 'Recheck — National Weather Service forecast flags weather risk.'
    return {
        'Auto status': ('Hold · check NFL injury or practice status' if hold else
                        'Pregame NFL model · starting lineup unconfirmed'),
        **availability, **weather,
        'NFL player ID': str(player.iloc[-1]['player_id']),
        'NFL away team': codes[0], 'NFL home team': codes[1],
        'Season average': round(season_mean, 2), 'Last five average': round(recent_mean, 2),
        'Season opportunities per game': round(float(player[opportunities].mean()), 2),
        'Recent opportunities per game': round(float(recent[opportunities].mean()), 2),
        'Days since last appearance': (date - recent.iloc[-1]['gameday']).days,
        'Venue split games': len(venue), 'Prior meetings': len(meetings),
        'Opponent team defense rank': rank,
        'Defense factor applied': 'Team defense' if rank else None,
        'Projected stat': round(projection, 2), 'Season standard deviation': round(sigma, 2),
        'Model More estimate (%)': model_over, 'Model signal': lean,
        'Model–market Over gap (pp)': round(gap, 1) if gap is not None else None,
    }


def project_nfl_matchup(games, date, away, home):
    played = games.loc[
        games['gameday'].lt(date) & games['home_score'].notna() & games['away_score'].notna()
    ].sort_values(['gameday', 'game_id'])

    def history(team):
        matches = played.loc[played['home_team'].eq(team) | played['away_team'].eq(team)]
        return [
            (row.home_score, row.away_score) if row.home_team == team else (row.away_score, row.home_score)
            for row in matches.itertuples(index=False)
        ]

    projection = score_projection(history(away), history(home))
    if projection is None:
        return {}
    return dict(zip(('Projected away score', 'Projected home score',
                     'Model home spread', 'Model game total'), projection))


def automatic_nfl_board(picks, board_scope):
    today = dt.datetime.now(ZoneInfo('America/New_York')).date()
    season = today.year - int(today.month < 7)
    freshness = int(dt.datetime.now(dt.timezone.utc).timestamp()) // 900
    scope = sha256((repr(board_scope) + picks.to_json() + str(freshness)).encode()).hexdigest()[:16]
    saved = st.session_state.get('auto_nfl_board')
    if saved and saved[0] == scope:
        return saved[1]
    try:
        with st.spinner('Checking NFL game history…'):
            stats, games = load_nfl_history(season)
            availability_sources = []
            for kind in ('injuries', 'depth_charts'):
                try:
                    availability_sources.append(load_nfl_availability(season, kind))
                except (requests.RequestException, ValueError, KeyError, ImportError, TypeError):
                    availability_sources.append(None)
            injuries, depth = availability_sources
            records = []
            game_models = {}
            for _, row in picks.iterrows():
                context = nfl_automatic_context(stats, games, row, today, injuries, depth)
                away, home = context.get('NFL away team'), context.get('NFL home team')
                start = pd.to_datetime(row.get('starts_utc'), utc=True, errors='coerce')
                date = today if pd.isna(start) else start.tz_convert('America/New_York').date()
                matchup = date, away, home
                if away and home and matchup not in game_models:
                    game_models[matchup] = project_nfl_matchup(games, *matchup)
                records.append({**row.to_dict(), **context, **game_models.get(matchup, {})})
    except (requests.RequestException, ValueError, KeyError, TypeError):
        st.warning('NFL game history is unavailable. Sportsbook comparisons are still shown.')
        return picks
    board = pd.DataFrame(records)
    st.session_state['auto_nfl_board'] = scope, board
    return board


def minutes_scenario(projection, expected_minutes, line, playing_time):
    estimate = projection * playing_time / expected_minutes
    gap = estimate - line
    side = 'Near line' if abs(gap) < 0.5 else 'More' if gap > 0 else 'Less'
    return estimate, side


def show_minutes_check(projection, expected_minutes, line, low, high, key):
    if any(pd.isna(value) for value in (projection, expected_minutes, low, high)):
        return
    if expected_minutes <= 0 or projection <= 0:
        return
    st.markdown('#### Playing-time scenarios')
    for column, label, minutes in zip(
        st.columns(3), ('Recent low', 'Baseline minutes', 'Recent high'), (low, expected_minutes, high),
    ):
        estimate, side = minutes_scenario(projection, expected_minutes, line, minutes)
        column.metric(label, f'{minutes:.1f} min')
        column.caption(f'{estimate:.2f} projected · {side}')
    if minutes_scenario(projection, expected_minutes, line, low)[1] != minutes_scenario(
        projection, expected_minutes, line, high,
    )[1]:
        st.info('This prop changes side or moves near the line within the recent minutes range.')
    minutes = st.slider('Try a playing-time scenario', 0.0, float(max(48.0, high, expected_minutes)),
                        float(expected_minutes), step=0.1, key=key)
    estimate, side = minutes_scenario(projection, expected_minutes, line, minutes)
    st.write(f'**{side}** at {minutes:.1f} minutes · **{estimate:.2f}** projected against a **{line:g}** line.')
    crossing = line * expected_minutes / projection
    st.caption(f'Projection reaches the line at {crossing:.1f} minutes. The scenario holds production per minute steady.')


def show_rebound_opportunity(values):
    st.markdown('#### Opponent missed shots')
    average = values.get('Opponent missed field goals per game')
    if pd.isna(average):
        st.caption('No verified opponent shot sample is available for this game.')
        return
    league = values['League missed field goals per game']
    recent = values.get('Opponent missed field goals last five')
    columns = st.columns(3)
    columns[0].metric('Opponent misses per game', f'{average:.1f}', delta=f'{average - league:+.1f} vs league')
    columns[1].metric('Opponent misses · last five', f'{recent:.1f}' if pd.notna(recent) else '—')
    columns[2].metric('League misses per game', f'{league:.1f}')
    st.caption(
        f"{int(values['Opponent shot sample games'])} opponent games before tip-off. "
        "Field-goal attempts minus makes. More misses can create more defensive-rebound opportunities. "
        "This is matchup context and leaves the player projection unchanged."
    )
    st.markdown('[Basketball game-log source](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nba_stats_player_game_logs)')


def show_scanned_nba_context(row, key):
    show_minutes_check(
        row.get('Projected stat'), row.get('Expected minutes'), float(row['line']),
        row.get('Recent low minutes'), row.get('Recent high minutes'), key,
    )
    if row['market'] == 'player_rebounds':
        show_rebound_opportunity(row)
    st.markdown('#### Matchup availability')
    st.caption(f"Injury report: {row.get('Injury report status', 'unavailable')}")
    timestamp = row.get('Report time (Eastern)')
    if pd.notna(timestamp):
        st.caption(f'Report time: {timestamp} ET')
    for column, group in zip(st.columns(2), ('Teammates', 'Opponents')):
        with column:
            st.markdown(f'**{group}**')
            entries = row.get(f'{group} reported')
            if pd.isna(entries):
                st.caption('No matching status found. Availability is unconfirmed.')
            else:
                for entry in entries.split('; '):
                    st.write(entry)
    context = {
        'player_points': 'Check shot volume and defensive assignments.',
        'player_rebounds': 'Check frontcourt minutes, rebound competition and opponent shot mix.',
        'player_assists': 'Check ball-handling and who is available to finish plays.',
    }
    st.caption(context.get(row['market'], 'Check how the rotation and matchup may change.'))
    st.caption('Reported absences are shown for review. They do not automatically change the projection.')


def pregame_recheck(board, sport):
    observed = pd.Timestamp.now(tz='UTC')
    keys = {}
    observations = []
    confirmed_statuses = {'out', 'doubtful', 'questionable', 'probable', 'available',
                          'ir', 'injured reserve'}
    for index, row in enumerate(board.to_dict('records')):
        kickoff = pd.to_datetime(row.get('starts_utc'), utc=True, errors='coerce')
        if pd.isna(kickoff) or kickoff <= observed or pd.isna(row.get('event_id')):
            continue
        identity = str(row['event_id']), nba_name_key(row['player']), row['market']
        key = json.dumps(identity if sport == 'NBA' else (sport, *identity))
        keys[index] = key
        evidence = {}
        updated = pd.to_datetime(row.get('prizepicks_updated_utc'), utc=True, errors='coerce')
        if pd.notna(updated) and updated <= observed and pd.notna(row.get('line')):
            evidence['PrizePicks line'] = updated.isoformat(), f"{row['line']:g}"
        report = pd.to_datetime(row.get('Report time (Eastern)' if sport == 'NBA'
                                        else 'NFL injury report time (UTC)'), errors='coerce')
        if pd.notna(report):
            if report.tzinfo is None:
                report = report.tz_localize('America/New_York', ambiguous='NaT', nonexistent='NaT')
            if pd.notna(report) and report <= observed:
                stamp = report.tz_convert('UTC').isoformat()
                player_status = str(row.get('Injury report status')).strip()
                if player_status.casefold() in confirmed_statuses:
                    evidence['Player availability'] = stamp, player_status
                for group in ('Teammates', 'Opponents'):
                    entries = row.get(f'{group} reported')
                    if isinstance(entries, str):
                        for entry in entries.split('; '):
                            name, separator, status = entry.partition(' · ')
                            if separator:
                                evidence[f'{group}: {name}'] = stamp, status
                evidence['_report'] = stamp, 'Report checked'
        observations.append((key, row['player'], row['event'], row['market'], evidence))
    if not observations:
        return board
    changes = observe_pregame_changes(DB, observations)
    reviewed = board.copy()
    reviewed['Pregame recheck'] = [
        '\n'.join(f'{field}: {before} → {after}' for field, before, after in changes.get(keys.get(index), []))
        for index in range(len(board))
    ]
    if 'Model signal' in reviewed:
        flagged = reviewed['Pregame recheck'].ne('')
        active_lean = reviewed['Model signal'].isin(['Model lean: More', 'Model lean: Less'])
        reviewed.loc[flagged & active_lean, 'Model signal'] = 'Recheck — line or availability changed.'
    return reviewed


def track_model_scan(board, sport, platform='PrizePicks'):
    observed = pd.Timestamp.now(tz='UTC')
    labels = {'Model lean: More': 'More', 'Model lean: Less': 'Less'}
    snapshots = []
    probabilities = []
    for row in board.to_dict('records'):
        if row.get('Model signal') not in labels:
            continue
        if row.get('Pregame recheck'):
            continue
        kickoff = pd.to_datetime(row.get('starts_utc'), utc=True, errors='coerce')
        required = ('event_id', f'{sport} player ID', f'{sport} away team', f'{sport} home team')
        if pd.isna(kickoff) or kickoff <= observed or any(pd.isna(row.get(key)) for key in required):
            continue
        if row.get('market') not in (NBA_STAT_COLUMNS if sport == 'NBA' else NFL_STATS):
            continue
        player_id = int(row[f'{sport} player ID']) if sport == 'NBA' else str(row[f'{sport} player ID'])
        snapshots.append((
            row['event_id'], player_id, row['market'], sport, row['player'],
            row['event'], kickoff.isoformat(), row[f'{sport} away team'], row[f'{sport} home team'],
            observed.isoformat(), float(row['line']), labels[row['Model signal']],
            row.get('Projected stat'), row.get('Injury report status'),
            row.get('Minutes sensitivity'), row.get('comparison_books'), row.get('Weather review'),
        ))
        model_more = row.get('Model More estimate (%)')
        probability = float(model_more) / 100 if pd.notna(model_more) else None
        if probability is not None and labels[row['Model signal']] == 'Less':
            probability = 1 - probability
        probabilities.append(probability)
    if snapshots:
        save_report_picks(DB, snapshots, platform, probabilities=probabilities)
        st.caption(f'{len(snapshots)} eligible pregame leans on this board. Pick Report Card keeps the first saved line for each site, player, game and stat.')


def fetch_nba_box(game_id):
    response = requests.get(
        f'https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json',
        timeout=(3, 6),
    )
    response.raise_for_status()
    return response.json()['game']


load_nba_final_box = st.cache_data(ttl=900, max_entries=128, show_spinner=False)(fetch_nba_box)


@st.cache_data(ttl=45, max_entries=1, show_spinner=False)
def load_nba_live_scoreboard():
    url = 'https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json'
    response = requests.get(url, timeout=(3, 6))
    response.raise_for_status()
    return response.json()['scoreboard']['games']


@st.cache_data(ttl=45, max_entries=48, show_spinner=False)
def load_nba_live_box(game_id):
    return fetch_nba_box(game_id)


def refresh_tracked_event(key, picks, phase):
    first = picks.iloc[0]
    kickoff = pd.Timestamp(first['kickoff'])
    event_id = first['event_id']
    sport = first['sport']
    markets = sorted(picks['market'].unique())
    data, remaining = odds_client(key).get(
        f'sports/{SPORTS[sport]}/events/{event_id}/odds',
        regions='us_dfs', markets=','.join(markets), oddsFormat='american',
    )
    checked = pd.Timestamp.now(tz='UTC')
    if checked >= kickoff:
        return 0, remaining
    away, _, home = first['event'].partition(' at ')
    quotes = compare_live_event(data, {
        'id': event_id, 'commence_time': kickoff.isoformat(), 'away_team': away, 'home_team': home,
    }, markets)
    if quotes.empty:
        quotes = pd.DataFrame(columns=['name_key', 'market'])
    else:
        quotes['name_key'] = quotes['player'].map(nba_name_key)
    season = kickoff.tz_convert('America/New_York').date()
    football = None
    try:
        injuries = load_nba_injury_report(season.year + int(season.month >= 9)) if sport == 'NBA' else None
        if sport == 'NFL':
            season_year = season.year - int(season.month < 7)
            stats, games = load_nfl_history(season_year)
            injuries = load_nfl_availability(season_year, 'injuries')
            game = games.loc[games['gameday'].eq(season) & games['away_team'].eq(first['away'])
                             & games['home_team'].eq(first['home'])]
            if len(game) == 1:
                football = stats, game.iloc[0]['week']
    except (requests.RequestException, ValueError, KeyError, TypeError):
        injuries = None
    if injuries is not None and sport == 'NBA':
        try:
            logs = load_nba_game_log(season.year + int(season.month >= 9))
            logs = logs.loc[logs['game_date'].lt(season)]
        except (requests.RequestException, ValueError, KeyError, TypeError):
            logs = None
    observed, closing = [], []
    for pick in picks.to_dict('records'):
        matches = quotes.loc[quotes['name_key'].eq(nba_name_key(pick['player']))
                             & quotes['market'].eq(pick['market'])]
        row = matches.iloc[0].to_dict() if len(matches) == 1 else dict(pick)
        row.update(event_id=event_id, starts_utc=kickoff.isoformat())
        if injuries is not None and sport == 'NFL' and football:
            stats, week = football
            prior = stats.loc[stats['gameday'].lt(season) & stats['player_id'].eq(pick['player_id'])]
            if not prior.empty:
                team = prior.sort_values('gameday').iloc[-1]['team']
                if team in (pick['away'], pick['home']):
                    row.update(nfl_availability(injuries, None, team, pick['player_id'], week, checked))
        if injuries is not None and sport == 'NBA':
            codes = pick['away'], pick['home']
            status, timestamp = nba_injury_status(injuries, season, codes, pick['player_id'])
            row.update({'Injury report status': status, 'Report time (Eastern)': timestamp})
            if logs is not None:
                player = logs.loc[logs['player_id'].eq(pick['player_id'])].sort_values('game_date')
                if not player.empty and player.iloc[-1]['team_abbreviation'] in codes:
                    row.update(nba_matchup_availability(
                        injuries, logs, season, codes, player.iloc[-1]['team_abbreviation'], pick['player_id'],
                    ))
        observed.append(row)
        updated = pd.to_datetime(row.get('prizepicks_updated_utc'), utc=True, errors='coerce')
        if (phase == 'close' and len(matches) == 1 and pd.notna(updated)
                and updated <= checked and 0 < (kickoff - checked).total_seconds() <= 600):
            closing.append((event_id, pick['player_id'], pick['market'], float(row['line']),
                            checked.isoformat(), updated.isoformat()))
    pregame_recheck(pd.DataFrame(observed), sport)
    save_closing_quotes(DB, closing)
    return len(closing), remaining


@st.fragment(run_every=60)
def nba_pick_watch(picks, key):
    st.caption('Checks tracked NBA and NFL picks while this session is open. Pregame checks are opt-in and use at most six Odds API credits per session.')
    pregame = st.checkbox('Auto-check around 90 minutes and again in the last 10 minutes', disabled=not key)
    live = st.checkbox('Show live game updates', value=False)
    alerts = st.checkbox('Show in-app change alerts', value=False)
    now = pd.Timestamp.now(tz='UTC')
    state = st.session_state.setdefault('nba_watch', {'attempted': set(), 'credits': 0, 'early_credits': 0, 'messages': [],
                                                     'live_states': {}, 'alert_seen': set()})
    if pregame and key:
        for (event_id, kickoff), group in picks.groupby(['event_id', 'kickoff'], sort=False):
            phase = pregame_window((pd.Timestamp(kickoff) - now).total_seconds() / 60)
            token = event_id, phase
            cost = group['market'].nunique()
            if phase is None or token in state['attempted'] or state['credits'] + cost > 6:
                continue
            if phase == 'recheck' and state['early_credits'] + cost > 3:
                continue
            state['attempted'].add(token)
            state['credits'] += cost
            if phase == 'recheck':
                state['early_credits'] += cost
            try:
                count, remaining = refresh_tracked_event(key, group, phase)
                state['messages'].append(f"{group.iloc[0]['event']}: {phase} checked. {count} closing quotes recorded.")
                if remaining == '0':
                    state['credits'] = 6
                    break
            except (ProviderError, requests.RequestException, KeyError, ValueError, TypeError):
                state['messages'].append(f"{group.iloc[0]['event']}: {phase} source unavailable.")
                break
    if state['messages']:
        st.caption(state['messages'][-1])
    with sqlite3.connect(DB) as conn:
        closing = pd.read_sql_query(
            """SELECT p.player, p.market, p.side, p.line AS tracked_line, q.line AS late_line,
                      q.checked, q.updated
               FROM closing_quotes q JOIN (SELECT * FROM report_card WHERE platform='PrizePicks') p USING(event_id, player_id, market)
               ORDER BY q.checked DESC""", conn,
        )
    if not closing.empty:
        closing['Line advantage'] = [
            closing_line_gain(row.side, row.tracked_line, row.late_line)
            for row in closing.itertuples(index=False)
        ]
        with st.expander('Late PrizePicks lines'):
            st.dataframe(closing, hide_index=True)
            st.caption('Positive means the tracked number was more favorable. This is a verified quote observed during the final 10 minutes, which may differ from the actual closing line.')
    if live:
        nba = picks.loc[picks['sport'].eq('NBA')]
        if not nba.empty:
            show_live_nba_picks(nba, state, alerts)
        nfl = picks.loc[picks['sport'].eq('NFL')]
        if not nfl.empty:
            show_live_nfl_scores(nfl, state, alerts)
    if alerts:
        with sqlite3.connect(DB) as conn:
            changes = conn.execute(
                'SELECT id, player, field, previous, current FROM pregame_changes ORDER BY id DESC LIMIT 10'
            ).fetchall()
        for ident, player, field, before, after in reversed(changes):
            if ident not in state['alert_seen']:
                st.toast(f'{player} · {field}: {before} → {after}')
                state['alert_seen'].add(ident)


@st.cache_data(ttl=45, max_entries=2, show_spinner=False)
def load_nfl_live_scoreboard():
    response = requests.get(
        'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard', timeout=(3, 6),
    )
    response.raise_for_status()
    return response.json()['events']


def show_live_nfl_scores(picks, state, alerts):
    try:
        events = load_nfl_live_scoreboard()
    except (requests.RequestException, KeyError, ValueError, TypeError):
        st.info('NFL scoreboard is unavailable.')
        return
    aliases = {'LAR': 'LA', 'WSH': 'WAS', 'JAC': 'JAX'}
    rows = []
    for event in events:
        start = pd.to_datetime(event.get('date'), utc=True, errors='coerce')
        competitions = event.get('competitions', [])
        if pd.isna(start) or len(competitions) != 1:
            continue
        competition = competitions[0]
        teams = {team.get('homeAway'): team for team in competition.get('competitors', [])}
        if not {'away', 'home'}.issubset(teams):
            continue
        codes = [teams[side].get('team', {}).get('abbreviation') for side in ('away', 'home')]
        away, home = (aliases.get(team, team) for team in codes)
        kickoff = pd.to_datetime(picks['kickoff'], utc=True, errors='coerce')
        matches = picks.loc[picks['away'].eq(away) & picks['home'].eq(home) & kickoff.eq(start)]
        status = competition.get('status', {})
        phase = status.get('type', {})
        if matches.empty or phase.get('state') not in ('in', 'post'):
            continue
        label = f"{away} {teams['away'].get('score', '—')} · {home} {teams['home'].get('score', '—')}"
        complete = phase.get('completed') is True
        key = 'NFL game', str(event.get('id'))
        if alerts and complete and state['live_states'].get(key) != 'Final':
            st.toast(f'{label} · Final')
        state['live_states'][key] = 'Final' if complete else 'In play'
        rows.append({'NFL game': matches.iloc[0]['event'], 'Score': label,
                     'Status': phase.get('shortDetail'), 'Clock': status.get('displayClock')})
    if rows:
        st.dataframe(rows, hide_index=True)
        st.caption('NFL scoreboard via ESPN. Player yardage is graded when the postgame data arrives.')
    else:
        st.caption('No tracked NFL game is verified on the current live scoreboard.')


def show_live_nba_picks(picks, state, alerts):
    try:
        games = load_nba_live_scoreboard()
    except (requests.RequestException, KeyError, ValueError, TypeError):
        st.info('NBA live scoreboard is unavailable.')
        return
    rows = []
    grades = []
    for (kickoff, away, home), group in picks.groupby(['kickoff', 'away', 'home'], sort=False):
        date = pd.Timestamp(kickoff).tz_convert('America/New_York').date()
        matches = []
        for game in games:
            if (game.get('awayTeam', {}).get('teamTricode'), game.get('homeTeam', {}).get('teamTricode')) != (away, home):
                continue
            tip = pd.to_datetime(game.get('gameTimeUTC'), utc=True, errors='coerce')
            if pd.notna(tip) and tip.tz_convert('America/New_York').date() == date:
                matches.append(game)
        if len(matches) != 1 or matches[0].get('gameStatus') not in (2, 3):
            continue
        try:
            box = load_nba_live_box(matches[0]['gameId'])
            teams = box['awayTeam'], box['homeTeam']
            status = box['gameStatus']
            players = {player['personId']: player for team in teams for player in team['players']}
        except (requests.RequestException, KeyError, ValueError, TypeError):
            continue
        for pick in group.to_dict('records'):
            player = players.get(pick['player_id'], {})
            value = player.get('statistics', {}).get(NBA_BOX_STATS[pick['market']])
            if status == 3:
                graded = grade_nba_pick(box, pick)
                result = graded[0] if graded else 'Unverified'
                if graded and pick['result'] == 'Pending':
                    grades.append((*graded, pick['event_id'], pick['player_id'], pick['market'], pick['platform']))
            else:
                result = grade_score(status, pick['side'], value, pick['line']) if isinstance(value, (int, float)) else 'Awaiting stat'
            identity = pick['event_id'], pick['player_id'], pick['market']
            previous = state['live_states'].get(identity)
            if alerts and previous != result and (previous or result in ('Hit', 'Miss', 'Push', 'Void')):
                st.toast(f"{pick['player']} · {pick['market']}: {result}")
            state['live_states'][identity] = result
            rows.append({
                'Player': pick['player'], 'Stat': pick['market'], 'Line': pick['line'],
                'Side': pick['side'], 'Current stat': value, 'Status': result,
                'Started': 'Yes' if player.get('starter') in ('1', 1) else '—',
                'Score': f"{away} {teams[0]['score']} · {home} {teams[1]['score']}",
                'Game status': box.get('gameStatusText'), 'Game clock': box.get('gameClock'),
            })
    if rows:
        st.dataframe(rows, hide_index=True)
    else:
        st.caption('No tracked games have a verified live or final box score on today’s NBA scoreboard.')
    if grades:
        store_report_grades(grades)


def store_report_grades(results):
    with sqlite3.connect(DB) as conn:
        cursor = conn.executemany(
            """UPDATE report_card SET result=?, actual=?
               WHERE event_id=? AND player_id=? AND market=? AND platform=? AND result='Pending'""", results,
        )
        return cursor.rowcount


def refresh_report_grades(picks, panda_key=''):
    now = pd.Timestamp.now(tz='UTC')
    pending = picks.loc[picks['result'].eq('Pending')].copy()
    dates = pd.to_datetime(pending['kickoff'], utc=True).dt.tz_convert('America/New_York')
    pending = pending.loc[dates.le(now - pd.Timedelta(hours=4))]
    matchups = pending.groupby(['sport', 'event_id', 'kickoff', 'away', 'home'], sort=False)
    results = []
    season_logs = {}
    football_logs = {}
    for (sport, _, kickoff, away, home), group in list(matchups)[:12]:
        date = pd.Timestamp(kickoff).tz_convert('America/New_York').date()
        season = date.year + int(date.month >= 9)
        try:
            if sport == 'Dota 2':
                if not panda_key:
                    continue
                match_id = int(group.iloc[0]['event_id'].removeprefix('dota:'))
                match = cached_dota_match(panda_key, match_id)
                for pick in group.to_dict('records'):
                    outcome = dota_grade(
                        match, lambda ident: cached_dota_game(panda_key, ident), match_id,
                        int(pick['player_id']), pick['market'], pick['line'], pick['side'],
                    )
                    if outcome is not None:
                        results.append((*outcome, pick['event_id'], pick['player_id'], pick['market'], pick['platform']))
                continue
            if sport == 'CS2':
                if not panda_key:
                    continue
                for pick in group.to_dict('records'):
                    match_id = int(pick['event_id'].removeprefix('panda:'))
                    player_id = int(pick['player_id'])
                    payload = cached_cs2_result(panda_key, match_id, player_id)
                    outcome = grade_cs2_pick(payload, match_id, player_id, pick['market'],
                                             pick['line'], pick['side'])
                    if outcome is not None:
                        results.append((*outcome, pick['event_id'], pick['player_id'], pick['market'], pick['platform']))
                continue
            if sport == 'NFL':
                football_season = date.year - int(date.month < 7)
                if football_season not in football_logs:
                    football_logs[football_season] = load_nfl_history(football_season)
                stats, games = football_logs[football_season]
                matching = games.loc[games['gameday'].eq(date) & games['home_team'].eq(home)
                                     & games['away_team'].eq(away) & games['home_score'].notna()
                                     & games['away_score'].notna()]
                if len(matching) == 1:
                    game = stats.loc[stats['game_id'].eq(matching.iloc[0]['game_id'])]
                    for pick in group.to_dict('records'):
                        player = game.loc[game['player_id'].eq(pick['player_id'])]
                        if len(player) != 1:
                            continue
                        stat = NFL_STATS[pick['market']][0]
                        actual = pd.to_numeric(player.iloc[0][stat], errors='coerce')
                        if pd.notna(actual) and np.isfinite(actual):
                            result = grade_score(3, pick['side'], actual, pick['line'])
                            results.append((result, float(actual), pick['event_id'], pick['player_id'], pick['market'], pick['platform']))
                continue
            if season not in season_logs:
                season_logs[season] = load_nba_game_log(season)
            logs = season_logs[season]
            ids = logs.loc[
                logs['game_date'].eq(date) & logs['team_abbreviation'].eq(home)
                & logs['opponent'].eq(away) & logs['home'], 'game_id',
            ].unique()
            if len(ids) != 1:
                continue
            game = load_nba_final_box(ids[0])
            for pick in group.to_dict('records'):
                outcome = grade_nba_pick(game, pick)
                if outcome is not None:
                    results.append((*outcome, pick['event_id'], pick['player_id'], pick['market'], pick['platform']))
        except (requests.RequestException, ValueError, KeyError, TypeError):
            continue
    return store_report_grades(results)


def report_calibration(shown):
    probabilities = pd.to_numeric(shown['model_probability'], errors='coerce')
    eligible = shown['result'].isin(['Hit', 'Miss']) & probabilities.between(0, 1)
    graded = shown.loc[eligible, ['Grading']].copy()
    graded['Predicted'] = probabilities.loc[eligible]
    graded['Hit'] = shown.loc[eligible, 'result'].eq('Hit')
    graded['Confidence'] = pd.cut(
        graded['Predicted'], [-0.01, .5, .6, .7, .8, 1],
        labels=['≤50%', '>50–60%', '>60–70%', '>70–80%', '>80%'],
    )
    graded['Squared error'] = (graded['Predicted'] - graded['Hit'].astype(float)) ** 2
    return graded.groupby(['Grading', 'Confidence'], observed=True).agg(
        Decisions=('Hit', 'size'), Predicted=('Predicted', 'mean'),
        Actual=('Hit', 'mean'), Brier=('Squared error', 'mean'),
    ).reset_index()


def show_report_card(key):
    st.subheader('Pick Report Card')
    st.caption('Automatic box-score grading covers supported NBA, NFL, CS2 and Dota 2 props. Paper Tracker results can be entered manually for any sport or site.')
    with sqlite3.connect(DB) as conn:
        picks = pd.read_sql_query('SELECT * FROM report_card ORDER BY kickoff, observed', conn)
        changes = pd.read_sql_query(
            'SELECT observed, player, event, market, field, previous, current '
            'FROM pregame_changes ORDER BY observed DESC, id DESC', conn,
        )
        manual = pd.read_sql_query(
            "SELECT sport, platform, player, event, market, line, side, actual, created_at AS observed, "
            "model_prob AS model_probability, "
            "CASE status WHEN 'Win' THEN 'Hit' WHEN 'Loss' THEN 'Miss' ELSE status END AS result "
            "FROM picks", conn,
        )
    with st.expander('Pregame change history', expanded=not changes.empty):
        if changes.empty:
            st.caption('Compare the same upcoming PrizePicks prop again after a line or injury report updates.')
        else:
            st.dataframe(changes, hide_index=True)
    if picks.empty and manual.empty:
        st.info('No tracked picks yet. Save a paper prediction in Evaluate prop or run a supported Best Picks scan.')
        return
    watchable = picks.loc[picks['sport'].isin(['NBA', 'NFL']) & picks['platform'].eq('PrizePicks')]
    if not watchable.empty:
        nba_pick_watch(watchable, key)
    panda_key = st.session_state.get('panda_session_token') or configured_panda_key()
    if picks['sport'].isin(['CS2', 'Dota 2']).any():
        if not panda_key:
            st.caption('Enter your PandaScore token on the CS2 or Dota 2 scoreboard to grade esports picks.')
        else:
            st.caption('Missing, incomplete or forfeited map results remain pending.')
    if st.button('Check final box scores', disabled=picks.empty):
        with st.spinner('Checking final results…'):
            count = refresh_report_grades(picks, panda_key)
        with sqlite3.connect(DB) as conn:
            picks = pd.read_sql_query('SELECT * FROM report_card ORDER BY kickoff, observed', conn)
        st.info(f'{count} picks graded. Unverified results stay pending.')
    report = pd.concat(
        [picks.assign(Grading='Box score'), manual.assign(Grading='Manual')],
        ignore_index=True,
    )
    report['platform'] = report['platform'].fillna('Unknown')
    filters = st.columns(3)
    shown = report
    for column, field, label in zip(filters, ('sport', 'platform', 'market'), ('Sport', 'Site', 'Stat')):
        options = ['All', *sorted(shown[field].dropna().unique())]
        selection = column.selectbox(label, options, key=f'report_filter:{field}')
        if selection != 'All':
            shown = shown.loc[shown[field].eq(selection)]
    counts = shown['result'].value_counts()
    decisions = counts.get('Hit', 0) + counts.get('Miss', 0)
    columns = st.columns(4)
    columns[0].metric('Tracked', len(shown))
    columns[1].metric('Hits / misses', f"{counts.get('Hit', 0)} / {counts.get('Miss', 0)}")
    columns[2].metric('Hit rate', f"{counts.get('Hit', 0) / decisions:.1%}" if decisions else '—')
    columns[3].metric('Pending', counts.get('Pending', 0))
    st.caption(f"Pushes: {counts.get('Push', 0)} · Voids: {counts.get('Void', 0)}. Hit rate uses hits and misses only.")
    if not shown.empty:
        breakdown = pd.crosstab(
            [shown['sport'], shown['platform'], shown['market']], shown['result'],
        ).reindex(columns=['Hit', 'Miss', 'Push', 'Void', 'Pending'], fill_value=0)
        breakdown['Decisions'] = breakdown['Hit'] + breakdown['Miss']
        breakdown['Hit rate (%)'] = (
            100 * breakdown['Hit'] / breakdown['Decisions'].replace(0, np.nan)
        ).round(1)
        st.dataframe(breakdown.reset_index(), hide_index=True)
    with st.expander('How accurate were the confidence estimates?'):
        validation = report_calibration(shown)
        if validation.empty:
            st.info('Confidence checks appear once a pick with a saved probability has a hit or miss.')
        else:
            validation['Predicted'] = (100 * validation['Predicted']).round(1)
            validation['Actual'] = (100 * validation['Actual']).round(1)
            validation['Brier'] = validation['Brier'].round(3)
            st.dataframe(validation.rename(columns={'Predicted': 'Average model confidence (%)',
                                                   'Actual': 'Actual hit rate (%)'}), hide_index=True)
            st.caption('Brier score measures probability error. Lower is better. Read each confidence band alongside its sample size.')
    st.dataframe(shown[['sport', 'platform', 'Grading', 'player', 'event', 'market',
                        'line', 'side', 'projection', 'actual', 'result', 'observed',
                        'injury', 'minutes', 'books']], hide_index=True)
    st.download_button('Download filtered report card', shown.to_csv(index=False),
                       'pick-report-card.csv', mime='text/csv')


def describe_board_coverage(row, now):
    updated = pd.to_datetime(row.get('Line updated UTC'), utc=True, errors='coerce')
    if pd.isna(updated) or updated > now:
        freshness = 'Unknown'
    else:
        minutes = int((now - updated).total_seconds() // 60)
        freshness = f'{minutes} min ago' if minutes < 60 else f'{minutes // 60}h {minutes % 60}m ago'
    coverage = []
    if pd.notna(row.get('Projected stat')):
        coverage.append('Player projection')
    if pd.notna(row.get('market_estimate_pct')):
        coverage.append('Exact-line odds')
    if not coverage:
        coverage.append('Line only')
    return freshness, ' · '.join(coverage)


def eligible_model_leans(board):
    if not {'Model signal', 'Model–market Over gap (pp)'}.issubset(board.columns):
        return pd.Series(np.nan, index=board.index)
    eligible = board['Model signal'].isin(['Model lean: More', 'Model lean: Less'])
    if 'Pregame recheck' in board:
        eligible &= board['Pregame recheck'].fillna('').eq('')
    gap = pd.to_numeric(board['Model–market Over gap (pp)'], errors='coerce')
    return gap.abs().where(eligible & np.isfinite(gap))


def rank_model_leans(board):
    order = eligible_model_leans(board).reset_index(drop=True)
    return board.iloc[order.sort_values(ascending=False, kind='stable').index]


def pregame_watchlist(board):
    signals = board.get('Model signal', pd.Series('', index=board.index)).fillna('')
    status = board.get('Auto status', pd.Series('', index=board.index)).fillna('')
    changes = board.get('Pregame recheck', pd.Series('', index=board.index)).fillna('')
    return board.loc[signals.str.startswith('Recheck') | status.str.startswith('Hold') | changes.ne('')]


def filter_prop_board(board, query='', market=None):
    if market is not None:
        board = board.loc[board['market'].eq(market)]
    query = query.strip().casefold()
    if query:
        matches = (board['player'].astype('string').str.casefold().str.contains(query, regex=False, na=False)
                   | board['event'].astype('string').str.casefold().str.contains(query, regex=False, na=False))
        board = board.loc[matches]
    return board


def show_best_picks_board(picks, sport, board_scope, platform='PrizePicks', source='CSV upload'):
    adapters = {'NBA': automatic_nba_board, 'NFL': automatic_nfl_board}
    full_board = adapters[sport](picks, board_scope) if sport in adapters else picks
    if platform == 'PrizePicks':
        full_board = pregame_recheck(full_board, sport)
    if sport in adapters:
        track_model_scan(full_board, sport, platform)
    st.subheader(f'{platform} · {sport} prop review')
    st.caption(f'{len(full_board)} lines. Select a row to review the available evidence.')
    board_key = f'{sport}:{platform}'
    search, market_filter = st.columns(2)
    query = search.text_input('Find a player or matchup', key=f'board_search:{board_key}')
    market = market_filter.selectbox(
        'Market filter', [None, *sorted(full_board['market'].dropna().unique(), key=str)],
        format_func=lambda value: 'All markets' if value is None else str(value).removeprefix('player_').replace('_', ' ').title(),
        key=f'board_market:{board_key}',
    )
    full_board = filter_prop_board(full_board, query, market)
    if full_board.empty:
        st.info('No lines match these filters.')
        return
    watchlist = pregame_watchlist(full_board)
    review_order = st.radio('Board view', ['Scan order', 'Strongest model leans first', 'Top 10 leans', 'Pregame watchlist'],
                            horizontal=True, key=f'board_order:{sport}:{platform}')
    if review_order == 'Pregame watchlist':
        full_board = watchlist
        if full_board.empty:
            st.info('No rechecks or availability holds flagged on this board.')
            return
        st.caption(f'{len(full_board)} lines flagged for another look before game time.')
    if review_order in ('Strongest model leans first', 'Top 10 leans'):
        full_board = rank_model_leans(full_board)
        st.caption('Eligible More and Less leans are ordered by the size of the model’s gap from exact-line sportsbook consensus.')
    if review_order == 'Top 10 leans':
        eligible = eligible_model_leans(full_board).notna().to_numpy()
        full_board = full_board.iloc[eligible].head(10)
        if full_board.empty:
            st.info('No eligible model leans with exact-line sportsbook comparisons on this board.')
            return
        st.caption(f'{len(full_board)} eligible leans shown.')
    explanations = [pick_evaluation(row)[:2] for _, row in full_board.iterrows()]
    timestamp_field = {
        'The Odds API': 'prizepicks_updated_utc',
        'Apify': 'Sleeper updated UTC',
    }.get(source, 'Line updated UTC')
    updated = pd.to_datetime(full_board.get(timestamp_field, pd.Series(index=full_board.index, dtype=object)),
                             utc=True, errors='coerce', format='mixed')
    now = pd.Timestamp.now(tz='UTC')
    full_board = full_board.assign(**{
        'Line source': source, 'Line updated UTC': updated.where(updated.le(now)),
    })
    coverage = [describe_board_coverage(row, now) for _, row in full_board.iterrows()]
    full_board = full_board.assign(
        Rating=[rating for rating, _ in explanations],
        Reason=[reason for _, reason in explanations],
        **{'Line age': [age for age, _ in coverage],
           'Data coverage': [description for _, description in coverage]},
    )
    known = full_board['Line updated UTC'].notna().sum()
    st.caption(f'{source} · {known} of {len(full_board)} lines have a usable update timestamp. Line age is measured when the view renders.')
    columns = [
        'player', 'market', 'line', 'Rating', 'Reason', 'Line age', 'Data coverage', 'Projected stat',
        'side', 'market_estimate_pct', 'comparison_books', 'Auto status', 'Injury report status',
        'Pregame recheck', 'Minutes sensitivity', 'Opponent missed field goals per game',
        'Weather review', 'status', 'event',
    ]
    visible = [column for column in columns if column in full_board]
    game_columns = ['event', 'Projected away score', 'Projected home score',
                    'Model home spread', 'Model game total', 'Market home spread', 'Market game total']
    if 'Model game total' in full_board:
        with st.expander(f'{sport} game projections'):
            st.dataframe(
                full_board.loc[full_board['Model game total'].notna(),
                               [col for col in game_columns if col in full_board]].drop_duplicates('event'),
                hide_index=True,
            )
            st.caption('Baseline blends each team’s season and last-five scoring and opponent points allowed. Negative home spread favors the home team.')
    table_key = sha256((repr(board_scope) + review_order + full_board[['player', 'event', 'market', 'line']].to_json()).encode()).hexdigest()[:18]
    selected = selectable_prop_table(full_board, f'best_picks:{table_key}', visible)
    if selected is None:
        selected = st.selectbox(
            'Player details', range(len(full_board)), key=f'board_details:{table_key}',
            format_func=lambda index: (
                f"{full_board.iloc[index]['player']} · "
                f"{str(full_board.iloc[index]['market']).removeprefix('player_').replace('_', ' ')} "
                f"{full_board.iloc[index]['line']:g}"
            ),
        )
    if selected is not None and selected < len(full_board):
        row = full_board.iloc[selected]
        st.subheader('Selected prop evaluation')
        st.caption(f"{row['Line source']} · {row['Data coverage']}")
        if pd.notna(row['Line updated UTC']):
            st.caption(f"Line updated {row['Line updated UTC']:%Y-%m-%d %H:%M UTC} · {row['Line age']}")
        else:
            st.caption('Line update time unavailable.')
        show_pick_evaluation(row)
        if row.get('Pregame recheck'):
            st.warning('Pregame recheck · ' + row['Pregame recheck'].replace('\n', ' · '))
            st.caption('Review the changed line or availability before using the earlier evaluation.')
        if sport == 'NBA':
            st.caption('Starting-lineup confirmation is unavailable from the connected NBA sources.')
            show_scanned_nba_context(row, f'board_minutes:{table_key}:{selected}')
        with st.expander('Full stats and market details'):
            details = row.drop(labels=['event_id'], errors='ignore').dropna()
            st.dataframe(details.astype(str).rename('Value'), use_container_width=True)
    st.download_button('Download Best picks board with research', full_board.to_csv(index=False),
                       f'{sport.lower()}_{platform.lower()}_best_picks.csv', mime='text/csv')
    if sport == 'NBA':
        st.caption('NBA projections use game logs and injury reports via SportsDataverse. Roles and usage are estimates. Defense rank 1 allows the least. No extra odds credits are used.')
    if sport == 'NFL':
        st.caption('NFL research checks game logs, weekly injury and practice reports, and recent depth charts via nflverse. A missing report leaves availability unconfirmed. Depth chart rank does not confirm the starting lineup. No extra odds credits are used.')
        weather_sources = int(full_board.get('Weather source', pd.Series(dtype=str)).notna().sum())
        st.caption(f'National Weather Service forecast used for review on {weather_sources} of {len(full_board)} props. Coverage requires a verified mapped outdoor venue and a kickoff in the forecast window.')
    if sport not in adapters:
        st.caption(f'{sport} has no automatic player-stat research in this view. Open Research sources to review the listed references.')
    show_board_research(full_board, sport, board_scope, selected)


def show_sleeper_feed(sport, slate_date=None):
    if sport not in SLEEPER_LEAGUES:
        st.info(f'The connected Sleeper feed does not document {sport} coverage. CSV import is available.')
        return
    token = st.text_input('Apify API token', type='password', key='sleeper_apify_token').strip()
    st.caption(f'Up to {MAX_ROWS} upcoming lines and ${MAX_CHARGE_USD:.2f} in Apify usage per refresh. No Odds API credits.')
    boards = st.session_state.setdefault('sleeper_feed_boards', {})
    scope = (sha256(token.encode()).hexdigest(), sport, slate_date)
    action = 'Refresh Sleeper lines' if scope in boards else 'Load Sleeper lines'
    if st.button(action, disabled=not token):
        now = dt.datetime.now(dt.timezone.utc)
        attempts = st.session_state.setdefault('sleeper_feed_attempts', {})
        last = attempts.get(scope)
        if last and (now - last).total_seconds() < 300:
            st.info('A refresh was requested within the last five minutes. Any saved results are shown below.')
        else:
            attempts[scope] = now
            try:
                with st.spinner('Loading Sleeper lines from Apify…'):
                    rows = fetch_sleeper_rows(token, sport, slate_date)
                boards[scope] = (now, rows)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in (401, 403):
                    st.error('Apify denied access. Check the token and account permissions.')
                elif status in (402, 429):
                    st.error('Apify reported a usage or request limit.')
                else:
                    st.error('Apify could not complete the Sleeper run. Check the run in Apify Console.')
            except (requests.RequestException, ValueError):
                st.error('Sleeper data could not be verified. Check the run in Apify Console before refreshing.')
    if scope not in boards:
        return
    fetched, rows = boards[scope]
    st.caption(f'Board requested {fetched:%Y-%m-%d %H:%M UTC}.')
    now = dt.datetime.now(dt.timezone.utc)
    active = [row for row in rows if dt.datetime.fromisoformat(row['starts_utc']) > now]
    if not active:
        st.info(f'No verified upcoming standard Sleeper lines returned for {sport}.')
        return
    show_best_picks_board(pd.DataFrame(active), sport, ('sleeper_feed', sport, fetched), 'Sleeper', 'Apify')


def show_board_research(picks, sport, board_scope, selected_index=None):
    with st.expander('Research a line on this board'):
        st.caption('Add your own research to a board line. Saved reviews appear here for this session.')
        lines = picks.reset_index(drop=True)

        def describe(index):
            row = lines.iloc[index]
            return f"{row['player']} · {row['market']} {row['line']:g} · {row['event']}"

        board_key = sha256(repr(board_scope).encode()).hexdigest()[:16]
        if selected_index is None:
            index = st.selectbox('Platform line to research', range(len(lines)), format_func=describe,
                                 key=f'board_research:{board_key}:line')
        else:
            index = selected_index
            st.caption(describe(index))
        row = lines.iloc[index]
        identifier = (sport, row['event'], row['player'], row['market'], float(row['line']),
                      row.get('starts_utc', ''))
        widget_scope = 'board_research:' + sha256(repr((board_key, identifier)).encode()).hexdigest()[:20]
        st.caption(f"Enter inputs for {row['player']} • {row['market']} • {row['line']:g}.")
        use_context, context_mean, context_notes = (
            nba_projection_inputs(widget_scope) if sport == 'NBA' else (False, None, '')
        )
        baseline = context_mean if use_context else st.number_input(
            'Baseline statistical projection', min_value=0.0, value=None,
            key=widget_scope + ':baseline',
        )
        sigma = st.number_input('Projected standard deviation', min_value=0.1, value=None,
                                key=widget_scope + ':sigma')
        if sigma is None:
            st.info('Enter a standard deviation to account for variance.')
        else:
            _, _, research_notes, signal = pick_research_inputs(
                sport, baseline, sigma, float(row['line']), 'Normal (continuous approximation)',
                use_context, widget_scope, enabled_by_default=True,
            )
            if signal and st.button('Save research review', key=widget_scope + ':save'):
                reviews = st.session_state.setdefault('board_research_reviews', {})
                reviews[identifier] = {
                    'Sport': sport, 'Event': row['event'], 'Player': row['player'],
                    'Market': row['market'], 'Line': row['line'], 'Research signal': signal,
                    'Board estimate at review (%)': row.get('market_estimate_pct'),
                    'Board side at review': row.get('side'),
                    'Evidence': ' '.join(part for part in (context_notes, research_notes) if part),
                }
                st.success('Research review saved.')
    reviews = st.session_state.get('board_research_reviews', {})
    identifiers = {(sport, row.event, row.player, row.market, float(row.line),
                    getattr(row, 'starts_utc', '')) for row in picks.itertuples(index=False)}
    current_reviews = [review for key, review in reviews.items() if key in identifiers]
    if current_reviews:
        st.markdown('**Reviewed lines on this board**')
        reviews_frame = pd.DataFrame(current_reviews)
        st.dataframe(reviews_frame, hide_index=True, use_container_width=True)
        st.download_button('Download research reviews', reviews_frame.to_csv(index=False),
                           'prizepicks_research_reviews.csv', mime='text/csv')


def record_board_observations(board, sport):
    keys = ['event_id', 'player', 'market']
    observations = {}
    with sqlite3.connect(DB) as db:
        rows = board.loc[~board.duplicated(keys, keep=False)]
        for row in rows.itertuples(index=False):
            if pd.isna(row.prizepicks_updated_utc):
                continue
            key = (sport, row.event_id, row.player, row.market)
            db.execute(
                'INSERT INTO prizepicks_first_lines VALUES (?, ?, ?, ?, ?, ?) '
                'ON CONFLICT (sport, event_id, player, market) DO UPDATE SET '
                'line=excluded.line, observed=excluded.observed '
                'WHERE excluded.observed < prizepicks_first_lines.observed',
                (*key, float(row.line), str(row.prizepicks_updated_utc)),
            )
            observations[key[1:]] = db.execute(
                'SELECT line, observed FROM prizepicks_first_lines WHERE '
                'sport=? AND event_id=? AND player=? AND market=?', key
            ).fetchone()
    first = [observations.get(key, (None, None))
             for key in board[keys].itertuples(index=False, name=None)]
    board = board.assign(**{
        'First observed PrizePicks line': [row[0] for row in first],
        'First observation UTC': [row[1] for row in first],
    })
    return board.assign(**{
        'PrizePicks line change': board['line'] - pd.to_numeric(
            board['First observed PrizePicks line'], errors='coerce'
        ),
    })


def game_market_context(data, event):
    rows = sportsbook_rows({
        'bookmakers': [book for book in data['bookmakers']
                       if book.get('key') and book['key'] not in DFS_BOOK_KEYS]
    })
    if rows.empty:
        return {}
    rows['line'] = pd.to_numeric(rows['line'], errors='coerce')
    spreads = rows.loc[rows['market'].eq('spreads') & rows['side'].eq(event.get('home_team'))]
    totals = rows.loc[rows['market'].eq('totals') & rows['side'].eq('Over')]
    spread = spreads.drop_duplicates('book')['line'].median()
    return {
        'Market home spread': spread,
        'Market game total': totals.drop_duplicates('book')['line'].median(),
        'Lopsided game flag': ('Elevated' if abs(spread) >= 12 else 'Below 12-point screen')
                              if pd.notna(spread) else 'Spread unavailable',
    }


def init_db():
    with sqlite3.connect(DB) as c:
        init_report_card(c)
        c.execute(
            'CREATE TABLE IF NOT EXISTS prizepicks_first_lines '
            '(sport TEXT, event_id TEXT, player TEXT, market TEXT, line REAL, observed TEXT, '
            'PRIMARY KEY (sport, event_id, player, market))'
        )
        c.execute('CREATE TABLE IF NOT EXISTS picks (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, sport TEXT, event TEXT, player TEXT, market TEXT, line REAL, side TEXT, model_prob REAL, market_prob REAL, edge REAL, platform TEXT, payout_multiplier REAL, status TEXT DEFAULT "Pending", actual REAL, closing_line REAL, notes TEXT)')
init_db()

def american_prob(odds):
    odds=float(odds)
    return (-odds)/(-odds+100) if odds<0 else 100/(odds+100)

def prob_model(mu, sigma, line, side, distribution):
    if distribution=='Poisson (counts)':
        # integer outcomes; half-point lines are safest; integer lines may push
        if side=='Over': return float(poisson.sf(math.floor(line),mu))
        return float(poisson.cdf(math.ceil(line)-1,mu))
    z=(line-mu)/sigma
    return float(norm.sf(z) if side=='Over' else norm.cdf(z))

def rank_sportsbook_prices(odds_df):
    """Compare each offered price with other books at the identical prop line.

    This is a market-price comparison, NOT a player-performance projection.
    """
    needed = {'book', 'market', 'player', 'side', 'line', 'odds'}
    if not needed.issubset(odds_df.columns):
        return pd.DataFrame()
    df = odds_df.copy()
    df['line'] = pd.to_numeric(df['line'], errors='coerce')
    df['odds'] = pd.to_numeric(df['odds'], errors='coerce')
    df['side'] = df['side'].astype(str).str.title()
    df = df.dropna(subset=['book', 'market', 'player', 'line', 'odds'])
    df = df[df['side'].isin(['Over', 'Under']) & (df['odds'] != 0)]
    if df.empty:
        return pd.DataFrame()

    # Each bookmaker contributes one no-vig estimate per player/market/line.
    paired = []
    keys = ['player', 'market', 'line', 'book']
    for key, group in df.groupby(keys):
        over = group[group['side'] == 'Over']
        under = group[group['side'] == 'Under']
        if over.empty or under.empty:
            continue
        try:
            p_over = american_prob(over.iloc[0]['odds'])
            p_under = american_prob(under.iloc[0]['odds'])
        except (ValueError, ZeroDivisionError):
            continue
        if p_over + p_under <= 0:
            continue
        paired.append(dict(zip(keys, key), fair_over=p_over/(p_over+p_under),
                           fair_under=p_under/(p_over+p_under)))
    if not paired:
        return pd.DataFrame()
    fair = pd.DataFrame(paired)
    results = []
    for _, offer in df.iterrows():
        # Exclude the book being evaluated to avoid comparing it with itself.
        peers = fair[(fair['player'] == offer['player']) &
                     (fair['market'] == offer['market']) &
                     (fair['line'] == offer['line']) &
                     (fair['book'] != offer['book'])]
        if peers.empty:
            continue
        side = offer['side']
        consensus_prob = float(peers['fair_over' if side == 'Over' else 'fair_under'].median())
        implied = american_prob(offer['odds'])
        decimal_odds = 1 + (100/abs(offer['odds']) if offer['odds'] < 0 else offer['odds']/100)
        results.append({
            'player': offer['player'], 'market': offer['market'], 'line': offer['line'],
            'side': side, 'book': offer['book'], 'odds': int(offer['odds']),
            'other_books': int(peers['book'].nunique()),
            'peer_fair_probability': round(100*consensus_prob, 1),
            'offered_implied_probability': round(100*implied, 1),
            'price_gap_pp': round(100*(consensus_prob-implied), 1),
            'market_based_ev_pct': round(100*(consensus_prob*decimal_odds-1), 1),
        })
    if not results:
        return pd.DataFrame()
    return pd.DataFrame(results).sort_values(['market_based_ev_pct', 'other_books'],
                                               ascending=[False, False]).reset_index(drop=True)


def sportsbook_rows(data):
    return pd.DataFrame([
        {'book': book.get('title'), 'market': market.get('key'),
         'player': offer.get('description', ''), 'side': offer.get('name'),
         'line': offer.get('point'), 'odds': offer.get('price'),
         'updated': market.get('last_update')}
        for book in data.get('bookmakers', [])
        for market in book.get('markets', [])
        for offer in market.get('outcomes', [])
    ])


def match_prizepicks(event_rankings, board):
    fields = ['event', 'player', 'market', 'line']
    if 'Line updated UTC' in board:
        fields.append('Line updated UTC')
    if not event_rankings:
        return board[fields].assign(
            side=None, market_estimate_pct=None, comparison_books=None
        )
    ranked = pd.concat(event_rankings, ignore_index=True)
    for frame in (ranked, board):
        for field in ('event', 'player', 'market'):
            frame[field + '_key'] = frame[field].astype('string').str.strip().str.casefold()
    keys = ['event_key', 'player_key', 'market_key', 'line']
    consensus = (
        ranked.drop_duplicates(keys + ['side', 'book'])
        .groupby(keys + ['side'], as_index=False)
        .agg(market_estimate_pct=('peer_fair_probability', 'median'),
             comparison_books=('book', 'nunique'))
    )
    best_sides = (
        consensus.sort_values(['market_estimate_pct', 'comparison_books', 'side'],
                              ascending=[False, False, True])
        .drop_duplicates(keys)
    )
    return (
        board.merge(best_sides, on=keys, how='left', validate='many_to_one')
        .sort_values('market_estimate_pct', ascending=False, kind='stable')
        [fields + ['side', 'market_estimate_pct', 'comparison_books']]
        .reset_index(drop=True)
    )


def event_label(event):
    return f"{event.get('away_team') or '?'} at {event.get('home_team') or '?'}"


def slate_events(events, now, slate_date=None):
    """Keep future events on the selected Pacific calendar day."""
    zone = ZoneInfo('America/Los_Angeles')
    upcoming = []
    for event in events:
        start = pd.to_datetime(event.get('commence_time'), utc=True, errors='coerce')
        if (pd.notna(start) and start > now and
                (slate_date is None or start.tz_convert(zone).date() == slate_date)):
            upcoming.append((start, event))
    return sorted(upcoming, key=lambda item: (item[0], item[1]['id']))


def scan_top_picks(key, sport, regions, board, event_limit, slate_date=None):
    events = fetch_events(key, SPORTS[sport])
    now = dt.datetime.now(dt.timezone.utc)
    upcoming = slate_events(events, now, slate_date)
    board_events = set(board['event'].str.strip().str.casefold())
    upcoming = [
        (start, event) for start, event in upcoming
        if event_label(event).casefold() in board_events
    ]
    upcoming.sort(key=lambda item: (item[0], item[1]['id']))

    rankings, issues = [], []
    scheduled = Counter(event_label(event).casefold() for _, event in upcoming)
    event_status = {
        name: 'Multiple upcoming games have this matchup' if count > 1 else 'Outside scan limit'
        for name, count in scheduled.items()
    }
    selected = [event for _, event in upcoming
                if event_status[event_label(event).casefold()] == 'Outside scan limit'][:event_limit]
    event_status.update({event_label(event).casefold(): 'Scan stopped before this game'
                         for event in selected})
    checked = 0
    for event in selected:
        name = event_label(event)
        try:
            data, remaining = fetch_event_odds(
                key, SPORTS[sport], event['id'], regions, PICK_MARKETS[sport]
            )
            ranked = rank_sportsbook_prices(sportsbook_rows(data))
        except ProviderError as exc:
            event_status[name.casefold()] = 'Odds request failed'
            issues.append(f'{name} — {exc}')
            if exc.code in ('access_denied', 'rate_limited'):
                break
            continue
        except (TypeError, AttributeError):
            event_status[name.casefold()] = 'Unexpected odds data'
            issues.append(f'{name} — Unexpected odds data.')
            continue
        checked += 1
        event_status[name.casefold()] = 'No exact sportsbook comparison'
        if not ranked.empty:
            rankings.append(ranked.assign(event=name))
        if remaining == '0':
            issues.append('The provider reported zero remaining credits at fetch time.')
            break

    results = match_prizepicks(rankings, board.copy())
    results['status'] = results['event'].str.strip().str.casefold().map(event_status).fillna(
        'No upcoming event with this matchup'
    )
    results.loc[~results['market'].str.strip().str.casefold().isin(PICK_MARKETS[sport].split(',')),
                'status'] = 'Market outside this scan'
    results.loc[results['market_estimate_pct'].notna(), 'status'] = 'Matched'
    return results, checked, len(selected), issues


DFS_BOOK_KEYS = {'prizepicks', 'dabble_us_dfs', 'parlayplay', 'pick6', 'underdog'}


def compare_live_event(data, event, markets, platform='PrizePicks'):
    books = data['bookmakers']
    platform_rows = sportsbook_rows({
        'bookmakers': [book for book in books if book.get('key') == platform.casefold()]
    })
    if platform_rows.empty:
        return pd.DataFrame()
    platform_rows['line'] = pd.to_numeric(platform_rows['line'], errors='coerce')
    board = (
        platform_rows.loc[platform_rows['market'].isin(markets) & platform_rows['line'].notna()
                       & platform_rows['player'].fillna('').str.strip().ne(''),
                       ['player', 'market', 'line', 'updated']]
        .drop_duplicates(['player', 'market', 'line'])
        .assign(event=event_label(event))
    )
    if board.empty:
        return pd.DataFrame()
    ranked = rank_sportsbook_prices(sportsbook_rows({
        'bookmakers': [book for book in books if book.get('key') and book['key'] not in DFS_BOOK_KEYS]
    }))
    results = match_prizepicks([ranked.assign(event=event_label(event))] if not ranked.empty else [],
                              board)
    results = results.merge(
        board[['player', 'market', 'line', 'updated']].rename(
            columns={'updated': 'prizepicks_updated_utc'}
        ),
        on=['player', 'market', 'line'], how='left', validate='one_to_one',
    )
    return results.assign(
        event_id=event['id'], starts_utc=event['commence_time'], platform=platform,
        status=results['market_estimate_pct'].notna().map({
            True: 'Matched', False: 'No exact sportsbook comparison',
        }),
    )


def load_live_prizepicks(key, sport, regions, event_limit, markets, compare, environment=False,
                        platform='PrizePicks', slate_date=None):
    events = fetch_events(key, SPORTS[sport])
    now = dt.datetime.now(dt.timezone.utc)
    selected = slate_events(events, now, slate_date)[:event_limit]

    results, issues = [], []
    requested = [*markets, 'spreads', 'totals'] if environment else markets
    checked = 0
    for _, event in selected:
        try:
            data, remaining = fetch_event_odds(
                key, SPORTS[sport], event['id'], regions, ','.join(requested)
            )
            frame = compare_live_event(data, event, markets, platform)
            if environment and not frame.empty:
                frame = frame.assign(**game_market_context(data, event))
            if not compare and not frame.empty:
                frame['status'] = f'{platform} line only'
        except ProviderError as exc:
            issues.append(f'{event_label(event)} — {exc}')
            if exc.code in ('access_denied', 'rate_limited'):
                break
            continue
        except (TypeError, AttributeError):
            issues.append(f'{event_label(event)} — Unexpected odds data.')
            continue
        checked += 1
        if not frame.empty:
            results.append(frame)
        else:
            issues.append(f'{event_label(event)} — No standard {platform} lines returned.')
        if remaining == '0':
            issues.append('The provider reported zero remaining credits at fetch time.')
            break

    board = (pd.concat(results, ignore_index=True)
             .sort_values('market_estimate_pct', ascending=False, kind='stable')
             if results else pd.DataFrame())
    if not board.empty and platform == 'PrizePicks':
        try:
            board = record_board_observations(board, sport)
        except sqlite3.Error:
            issues.append('Could not update first-observed line history.')
    return board, checked, len(selected), issues


def normalize_upload(df):
    required={'sport','event','player','market','line','platform'}
    missing=required-set(df.columns)
    if missing: raise ValueError('Missing CSV columns: '+', '.join(sorted(missing)))
    df=df.copy(); df['line']=pd.to_numeric(df['line'],errors='coerce')
    return df.dropna(subset=['line'])

def add_pick(row):
    cols=['created_at','sport','event','player','market','line','side','model_prob','market_prob','edge','platform','payout_multiplier','notes']
    vals=[dt.datetime.now(dt.timezone.utc).isoformat()]+[row.get(x) for x in cols[1:]]
    with sqlite3.connect(DB) as c:
        c.execute('INSERT INTO picks ('+','.join(cols)+') VALUES ('+','.join(['?']*len(cols))+')',vals)

with st.sidebar:
    st.header('Configuration')
    api_key=st.text_input('The Odds API key (optional)',value=configured_key(),type='password').strip()
    st.caption('Free tier has limited credits and may not include all player props. Check provider coverage and quotas before querying.')
    st.info('Streamlit Community Cloud local SQLite storage is ephemeral. Export CSV regularly. For persistent multi-device history, connect a hosted database later.')
    st.markdown('[Get an API key](https://the-odds-api.com/) · [Deploy Streamlit](https://share.streamlit.io/)')

best,tab1,tab2,tab3,tab4,research,report_card=st.tabs(['Best picks', '🔎 Scan markets','🧮 Evaluate prop','🗂️ Paper tracker','📘 Setup','Research sources','Pick Report Card'])
key_id = sha256(api_key.encode()).hexdigest()
with best:
    st.subheader('Daily Slate')
    pacific_today = dt.datetime.now(ZoneInfo('America/Los_Angeles')).date()
    slate_date = st.date_input(
        'Slate date (Pacific time)', value=pacific_today, min_value=pacific_today,
        max_value=pacific_today + dt.timedelta(days=1),
    )
    st.caption('Feed scans cover upcoming games on the selected day. CSV lines stay visible as uploaded.')
    platform = st.selectbox('Pick site', ['PrizePicks', 'Underdog', 'Sleeper'])
    st.subheader(f'{platform} board by sport')
    if platform == 'Sleeper':
        source_mode = st.radio('Sleeper line source', ['Load with Apify', 'Upload CSVs'], horizontal=True)
    else:
        source_mode = st.radio(f'{platform} line source', ['Load from feed', 'Upload CSVs'], horizontal=True)
    live_board = source_mode == 'Load from feed'
    sleeper_feed = source_mode == 'Load with Apify'
    if live_board:
        st.caption(f'Loads standard {platform} lines from The Odds API for the selected upcoming games and markets. Feed coverage can differ from your {platform} board.')
    elif not sleeper_feed:
        st.caption(f'Upload one or more CSVs. Every imported {platform} line stays visible.')
    template = pd.DataFrame(columns=['sport', 'event', 'player', 'market', 'line', 'platform'])
    uploads = []
    if not live_board and not sleeper_feed:
        st.download_button(f'Download {platform} CSV template', template.to_csv(index=False),
                           f'{platform.lower()}_template.csv', mime='text/csv')
        uploads = st.file_uploader(f'Upload current {platform} lines', type='csv',
                                   accept_multiple_files=True, key=f'{platform.lower()}_boards')
    boards = []
    board_ids = []
    for upload in uploads:
        try:
            frame = normalize_upload(pd.read_csv(upload))
        except (ValueError, TypeError) as exc:
            st.error(f'{upload.name} — {exc}')
            continue
        frame = frame.dropna(subset=['sport', 'event', 'player', 'market'])
        frame = frame[frame['platform'].astype(str).str.strip().str.casefold().eq(platform.casefold())]
        if frame.empty:
            st.info(f'{upload.name} has no valid {platform} lines.')
            continue
        boards.append(frame.reindex(columns=[*template.columns, 'Line updated UTC']))
        board_ids.append(sha256(upload.getvalue()).hexdigest())

    requested_sport = st.query_params.get('sport')
    pick_sport = st.selectbox('Pick sport', ALL_SPORTS,
                             index=ALL_SPORTS.index(requested_sport) if requested_sport in ALL_SPORTS else 0)
    feed_available = (pick_sport in SLEEPER_LEAGUES if platform == 'Sleeper' else pick_sport in PICK_MARKETS)
    if feed_available:
        provider = 'Apify' if platform == 'Sleeper' else 'The Odds API'
        st.caption(f'{platform} · {pick_sport} coverage: {provider} feed available. Line availability depends on the slate and markets.')
    else:
        st.caption(f'{platform} · {pick_sport} coverage: CSV import.')
    if platform == 'PrizePicks':
        show_cs2_review(pick_sport)
        show_nba_review(pick_sport)
    show_supplemental_sport_board(pick_sport, platform)
    with st.expander(f'{pick_sport} research sources'):
        show_research_links(pick_sport)
    if pick_sport in ('NFL', 'CFB'):
        show_football_weather()
    pick_region = st.selectbox('Sportsbook region', ['us', 'us2', 'uk', 'eu', 'au'])
    board = pd.DataFrame(columns=template.columns)
    if boards:
        all_lines = pd.concat(boards, ignore_index=True)
        identity = all_lines[['sport', 'event', 'player', 'market', 'line']].copy()
        for column in ['sport', 'event', 'player', 'market']:
            identity[column] = identity[column].astype(str).str.strip().str.casefold()
        all_lines = all_lines.loc[~identity.duplicated()]
        st.caption(f'{len(all_lines)} unique {platform} lines across all uploads.')
        with st.expander('View every imported line'):
            st.dataframe(all_lines, hide_index=True, use_container_width=True)
        board = all_lines[identity.loc[all_lines.index, 'sport'].eq(pick_sport.casefold())]
        if board.empty:
            st.info(f'The uploads contain no {platform} lines for {pick_sport}.')

    if sleeper_feed:
        show_sleeper_feed(pick_sport, slate_date)
    elif pick_sport not in PICK_MARKETS:
        if not board.empty:
            show_best_picks_board(board, pick_sport, (platform, pick_sport, tuple(board_ids)), platform)
        st.info(f'Upload {platform} lines to open the clickable {pick_sport} review. Live prop scanning is unavailable for this sport.')
    else:
        allowed_markets = PICK_MARKETS[pick_sport].split(',')
        st.caption(f'Markets scanned for {pick_sport}: {", ".join(allowed_markets)}.')
        if not live_board:
            st.caption(f'Use Away Team at Home Team for event and {platform} for platform.')
        event_limit = st.slider('Maximum games to scan', min_value=1, max_value=12, value=1)
        compare = True
        environment = False
        scan_markets = allowed_markets
        if live_board:
            auto_load = st.checkbox('Automatically load the first board', value=True,
                                    help='Loads one game and one market for today per sport in this session. Other boards and refreshes use the button.')
            scan_markets = st.multiselect('Markets to load', allowed_markets, default=allowed_markets[:1])
            compare = st.checkbox('Compare with sportsbooks', value=True,
                                  help=f'Turn off to load {platform} lines at half the maximum credit cost.')
            if compare:
                environment = st.checkbox('Include game spread and total',
                                          help='Uses extra credits and screens large spreads for blowout risk.')
            if platform == 'PrizePicks':
                st.caption('Line changes use the first PrizePicks line saved by Vantage Sports.')
            else:
                st.caption('Underdog supports the same available research and comparisons. Automatic report-card tracking and line history are currently available for PrizePicks.')
        regions = (f'{pick_region},us_dfs' if compare else 'us_dfs') if live_board else pick_region
        credits = event_limit * (len(scan_markets) + 2 * int(environment)) * len(regions.split(','))
        st.caption(f'Up to {credits} Odds API credits per scan. Viewing saved boards uses no credits. Identical requests can reuse cached odds for five minutes.')
        pick_scope = (key_id, platform, pick_sport, regions, live_board, tuple(board_ids), event_limit,
                      tuple(scan_markets), environment, slate_date)
        ready = bool(api_key and scan_markets and (live_board or not board.empty))
        boards_by_scope = st.session_state.setdefault('daily_slate_results', {})
        attempted = st.session_state.setdefault('daily_slate_initial_scans', set())
        initial_scope = key_id, pick_sport, slate_date
        action = (f'Refresh {platform} lines' if pick_scope in boards_by_scope else
                  f'Load {platform} lines') if live_board else f'Compare {platform} lines'
        saved_review_open = (platform == 'PrizePicks' and
                             any(name in st.query_params for name in ('nba_review', 'cs2_review')))
        initial_scan = (
            live_board and auto_load and ready and event_limit == 1 and len(scan_markets) == 1
            and not environment and initial_scope not in attempted and not saved_review_open
            and slate_date == pacific_today
        )
        if st.button(action, disabled=not ready) or initial_scan:
            if live_board:
                attempted.add(initial_scope)
            try:
                with st.spinner(f'Comparing {platform} lines…'):
                    result = (load_live_prizepicks(api_key, pick_sport, regions, event_limit, scan_markets,
                                                 compare, environment, platform, slate_date)
                              if live_board else
                              scan_top_picks(api_key, pick_sport, pick_region, board, event_limit, slate_date))
                boards_by_scope[pick_scope] = (pd.Timestamp.now(tz='UTC'), result)
            except ProviderError as exc:
                st.error(str(exc))
        if not api_key:
            st.info('Enter an Odds API key to compare lines.')
        saved = boards_by_scope.get(pick_scope)
        if not live_board and not board.empty and (saved is None or saved[1][0].empty):
            show_best_picks_board(board, pick_sport, pick_scope, platform)
        if saved is not None:
            loaded, (picks, checked, targeted, issues) = saved
            st.caption(f'Last scan {loaded.tz_convert("America/Los_Angeles"):%b %d at %I:%M %p PT} · {checked} of {targeted} selected games checked.')
            for issue in issues:
                st.warning(issue)
            if live_board and not picks.empty:
                starts = pd.to_datetime(picks['starts_utc'], utc=True, errors='coerce')
                picks = picks.loc[starts.gt(pd.Timestamp.now(tz='UTC'))]
            if picks.empty:
                st.info(f'No upcoming {platform} lines in this scan for the selected day and markets.')
            else:
                matched = picks['status'].eq('Matched').sum()
                if compare:
                    st.caption(f'{matched} of {len(picks)} {platform} lines matched.')
                else:
                    st.caption(f'{len(picks)} {platform} lines loaded.')
                    picks = picks.drop(columns=['side', 'market_estimate_pct', 'comparison_books'])
                show_best_picks_board(picks.assign(platform=platform), pick_sport, pick_scope, platform,
                                      'The Odds API' if live_board else 'CSV upload')

with tab1:
    st.subheader('Live sportsbook market discovery')
    sport=st.selectbox('Sport',list(SPORTS),key='live_sport')
    st.caption('See Research sources for all sports. The sportsbook scanner supports the five leagues in this menu.')
    regions=st.selectbox('Odds region',['us','us2','uk','eu','au'])
    markets=st.text_input('Market keys supported by your plan/provider','player_points,player_rebounds,player_assists')
    event_scope = (key_id, sport)
    if st.button('Fetch live events'):
        st.session_state.pop('scoped_events', None)
        st.session_state.pop('scoped_odds', None)
        if not api_key: st.error('Enter a provider API key first. No demo odds will be presented as live.')
        else:
            try:
                events = fetch_events(api_key, SPORTS[sport])
                st.session_state['scoped_events'] = (event_scope, events)
                st.success(f'{len(events)} events retrieved')
            except ProviderError as exc: st.error(str(exc))
    saved_scope, saved_events = st.session_state.get('scoped_events', (None, []))
    events = saved_events if saved_scope == event_scope else []
    odds_scope = None
    if events and api_key:
        options={f"{e.get('home_team')} vs {e.get('away_team')} • {e.get('commence_time')}":e['id'] for e in events}
        chosen=st.selectbox('Event',list(options))
        odds_scope = (event_scope, options[chosen], regions, markets)
        if st.button('Fetch event player props (may consume API credits)'):
            st.session_state.pop('scoped_odds', None)
            try:
                data,remaining=fetch_event_odds(api_key,SPORTS[sport],options[chosen],regions,markets)
                frame = sportsbook_rows(data)
                st.session_state['scoped_odds'] = (odds_scope, frame)
                st.caption(f'Remaining credits when fetched: {remaining or "not reported"}')
                if frame.empty: st.info('No offers were returned for these markets.')
            except ProviderError as exc: st.error(str(exc))
            except (TypeError, AttributeError): st.error('The provider returned unexpected odds data.')
    saved_scope, saved_odds = st.session_state.get('scoped_odds', (None, None))
    odds_df = saved_odds if saved_scope == odds_scope and saved_odds is not None else pd.DataFrame()
    if not odds_df.empty:
        st.dataframe(odds_df,use_container_width=True)
        st.download_button('Export sportsbook snapshot',odds_df.to_csv(index=False),'sportsbook_snapshot.csv')
        st.markdown('**Consensus estimates from matched over/under pairs**')
        pairs=[]
        for (player,market,line,book),g in odds_df.dropna(subset=['line']).groupby(['player','market','line','book']):
            overs=g[g.side.str.lower()=='over']; unders=g[g.side.str.lower()=='under']
            if len(overs) and len(unders):
                po=american_prob(overs.iloc[0].odds); pu=american_prob(unders.iloc[0].odds)
                pairs.append({'player':player,'market':market,'line':line,'book':book,'fair_over':po/(po+pu),'fair_under':pu/(po+pu)})
        if pairs:
            consensus=pd.DataFrame(pairs).groupby(['player','market','line'],as_index=False).agg(fair_over=('fair_over','median'),fair_under=('fair_under','median'),books=('book','nunique'))
            st.dataframe(consensus.sort_values('books',ascending=False),use_container_width=True)
            st.download_button('Export consensus',consensus.to_csv(index=False),'consensus.csv')
        else: st.info('No matched Over/Under pairs at identical lines were returned.')
    st.divider()
    st.subheader('📊 Automatic sportsbook price rankings')
    st.caption('Compares prices at the SAME player, market and line against OTHER sportsbooks. '
               'These are market-derived estimates, not verified player hit probabilities or guaranteed edges.')
    if odds_df.empty:
        st.info('Fetch event player props above to populate the rankings. No extra API call is needed.')
    else:
        ranked = rank_sportsbook_prices(odds_df)
        if ranked.empty:
            st.info('Rankings require matched Over/Under prices from at least two different sportsbooks '
                    'at the same line. Your current data does not have enough comparable prices.')
        else:
            min_peers = st.selectbox('Minimum other sportsbooks', [1, 2, 3], index=0)
            shown = ranked[ranked['other_books'] >= min_peers]
            if shown.empty:
                st.info('No offers meet that minimum. Try fewer comparison sportsbooks.')
            else:
                st.dataframe(shown, use_container_width=True, hide_index=True)
                st.download_button('Export ranked price comparisons', shown.to_csv(index=False),
                                   'prop_edge_price_rankings.csv', mime='text/csv')
                st.caption('Positive market-based EV is only a hypothetical calculation using other '
                           'books’ median no-vig prices. Differences in timing, limits and stale lines '
                           'can invalidate it. Injuries, game logs and opponent defense are NOT yet included.')
    st.divider()
    st.subheader('Import PrizePicks, Underdog or Sleeper lines')
    template=pd.DataFrame([{'sport':'NBA','event':'Example A vs B','player':'Example Player','market':'points','line':19.5,'platform':'PrizePicks'}])
    st.download_button('Download CSV template',template.to_csv(index=False),'platform_lines_template.csv')
    upload=st.file_uploader('Upload a CSV of platform lines',type='csv')
    if upload:
        try:
            df=normalize_upload(pd.read_csv(upload)); st.dataframe(df,use_container_width=True)
            st.info('Imported lines are not automatically matched to sportsbooks yet. Use the Evaluate tab with verified market odds and projections. No fabricated comparisons.')
        except Exception as e: st.error(str(e))
with tab2:
    st.subheader('Independent projection + market comparison')
    st.caption('All numbers here are user-entered unless explicitly sourced from the live tab. Do not mistake assumptions for verified probabilities.')
    a,b,c=st.columns(3)
    with a:
        es=st.selectbox('Sport',ALL_SPORTS); player=st.text_input('Player'); event=st.text_input('Event'); market=st.text_input('Market / stat','points')
    with b:
        platform=st.selectbox('Platform',['PrizePicks','Underdog','Sleeper']); line=st.number_input('Platform line',value=19.5,step=.5); side=st.selectbox('Side',['Over','Under']); dist=st.selectbox('Model distribution',['Normal (continuous approximation)','Poisson (counts)'])
    with c:
        mu=st.number_input('Independent projection (mean)',value=21.0,step=.5); sigma=st.number_input('Estimated standard deviation',value=5.5,min_value=.01,step=.5,disabled=dist.startswith('Poisson'))
        market_over=st.number_input('No-vig market probability of OVER (%)',value=50.,min_value=0.,max_value=100.,step=.5)
        payout=st.number_input('Net payout per $1 winning selection (illustrative)',value=1.0,min_value=0.0,step=.1)
    use_context, context_mean, context_notes = (
        nba_projection_inputs() if es == 'NBA' else (False, None, '')
    )
    if use_context:
        mu = context_mean
    mu, researched_market_over, research_notes, _ = pick_research_inputs(
        es, mu, sigma, line, dist, use_context,
    )
    if researched_market_over is not None:
        market_over = 100 * researched_market_over
    if mu is not None:
        p=prob_model(mu,sigma,line,side,dist); mp=market_over/100 if side=='Over' else 1-market_over/100
        edge=p-mp; ev=p*payout-(1-p)
        m1,m2,m3,m4=st.columns(4)
        m1.metric('Model hit estimate',f'{p:.1%}');m2.metric('Market estimate',f'{mp:.1%}');m3.metric('Model-market gap',f'{edge:+.1%}');m4.metric('Illustrative binary EV',f'{ev:+.1%}')
        st.warning('This EV assumes a standalone binary payout. PrizePicks/Underdog multi-pick entries require joint probabilities, correlations and the exact payout table. Integer lines may push; this basic calculator does not model pushes.')
        notes=st.text_area('Evidence / injury and lineup checks / source timestamps')
        notes = '\n'.join(part for part in (context_notes, research_notes, notes) if part)
        if st.button('Save as paper prediction'):
            if not player.strip() or not event.strip(): st.error('Player and event required')
            else:
                add_pick({'sport':es,'event':event,'player':player,'market':market,'line':line,'side':side,'model_prob':p,'market_prob':mp,'edge':edge,'platform':platform,'payout_multiplier':payout,'notes':notes})
                st.success('Paper prediction saved')
with report_card:
    show_report_card(api_key)
with tab3:
    st.subheader('Results and validation')
    with sqlite3.connect(DB) as conn: picks=pd.read_sql_query('SELECT * FROM picks ORDER BY id DESC',conn)
    if picks.empty: st.info('No paper predictions yet. Save a projection in Evaluate prop.')
    else:
        graded=picks[picks.status.isin(['Win','Loss'])]
        k1,k2,k3=st.columns(3)
        k1.metric('Predictions',len(picks));k2.metric('Graded',len(graded));k3.metric('Hit rate',f'{(graded.status=="Win").mean():.1%}' if len(graded) else '—')
        if len(graded):
            outcome=(graded.status=='Win').astype(float)
            st.metric('Brier score (lower is better)',f'{np.mean((graded.model_prob-outcome)**2):.3f}')
            st.caption('ROI cannot be inferred reliably from per-leg payout assumptions on multi-pick entries.')
        st.dataframe(picks,use_container_width=True)
        st.download_button('Back up all predictions (CSV)',picks.to_csv(index=False),'prop_predictions_backup.csv')
        ident=st.selectbox('Grade prediction ID',picks.id.tolist(),format_func=lambda i: f'#{i} — '+str(picks.loc[picks.id==i,'player'].iloc[0]))
        status=st.selectbox('Outcome',['Pending','Win','Loss','Push','Void'])
        actual=st.number_input('Actual stat',value=0.,step=.5)
        closing=st.number_input('Closing platform line (if observed)',value=0.,step=.5)
        if st.button('Update result'):
            with sqlite3.connect(DB) as conn: conn.execute('UPDATE picks SET status=?, actual=?, closing_line=? WHERE id=?',(status,actual,closing if closing else None,int(ident)))
            st.success('Updated. Refresh page to see new metrics.')
with tab4:
    st.markdown('''### Zero-cost deployment
1. Create a free GitHub account and a repository; upload `app.py` and `requirements.txt`.
2. Create a free account at [Streamlit Community Cloud](https://share.streamlit.io/), connect GitHub and deploy `app.py`.
3. Optional: register for a free key at [The Odds API](https://the-odds-api.com/). Enter the key in the sidebar, or configure `ODDS_API_KEY` as a Streamlit secret.
4. Fetch events, then request supported player-prop markets. API availability and credits vary; a free tier may not include player props.
5. Import PrizePicks, Underdog or Sleeper CSV lines and manually enter independent projections in **Evaluate prop**. Save selections before games start, then grade them in **Paper tracker**.

### Honest limitations
- No automatic PrizePicks/Underdog scraping, no unauthorized APIs, and no fake live picks.
- No automatic injury feeds or independent projections yet; the evaluator uses your supplied assumptions.
- The Odds API event/player market coverage may require paid access.
- This prototype does **not** continuously scan in the background; Streamlit refreshes only when you open it and request data.
- SQLite on free Streamlit Cloud is not durable across app restarts. Export backups after each session.
- Multi-pick payout/EV modeling and automated settlement are future upgrades.
- The system is a **research prototype**, not a validated predictive edge.
''')


with research:
    st.subheader('Best picks feature coverage')
    st.dataframe([
        {
            'Sport': sport,
            'Line recheck': 'On repeat feed scan' if sport in PICK_MARKETS else 'Feed pending',
            'Timed checks and late quotes': 'Tracked picks, session open' if sport in ('NBA', 'NFL') else 'Pending',
            'Live view': ('Score, clock and player stats' if sport == 'NBA' else
                          'Score and clock' if sport == 'NFL' else
                          'Match scoreboard · token required' if sport == 'CS2' else 'Event scoreboard'),
            'Score, spread and total projections': ('Game-history baseline' if sport in ('NBA', 'NFL')
                                                    or sport in TEAM_MODELS else 'Pending'),
            'Report Card grading': ('Supported player stats' if sport in ('NBA', 'NFL') else
                                    'Map kills · PandaScore Historical or Live' if sport == 'CS2' else 'Pending'),
        }
        for sport in ALL_SPORTS
    ], hide_index=True)
    st.subheader('Research sources and provider coverage')
    st.caption('Use the sport-specific panels in Best Picks for supported player history and projections.')
    if st.button('Check active The Odds API coverage', disabled=not api_key):
        st.session_state.pop('research_catalog', None)
        try:
            st.session_state['research_catalog'] = (key_id, fetch_catalog(api_key))
        except ProviderError as exc:
            st.error(str(exc))
    if not api_key:
        st.info('Enter an API key to check active odds coverage.')
    catalog_key, catalog = st.session_state.get('research_catalog', (None, []))
    coverage = source_rows()
    for row in coverage:
        sport = row['sport']
        if catalog_key == key_id and row['odds_coverage'] == 'Catalog check pending':
            matches = competitions(sport, catalog)
            row['odds_coverage'] = ', '.join(item.get('title') or item['key'] for item in matches)
            if not matches:
                row['odds_coverage'] = 'No active catalog entries'
    st.dataframe(coverage, hide_index=True, use_container_width=True)
    st.caption('Catalog entries describe competitions. PGA Golf entries are major-winner futures. Player-prop availability varies by market and bookmaker.')
    sport = st.selectbox('Research sport', ALL_SPORTS)
    show_research_links(sport)
    st.markdown('**Candidate data integrations**')
    source = SOURCES[sport]
    for label, url in zip(source.research_sources, source.source_urls):
        st.markdown(f'[{label}]({url})')
