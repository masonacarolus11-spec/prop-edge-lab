import os, sqlite3, math, json, datetime as dt
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
from sport_sources import SOURCES, competitions, source_rows

st.set_page_config(page_title='Prop Edge Lab', page_icon='📈', layout='wide')
st.title('📈 Prop Edge Lab')
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


def show_research_links(sport):
    for category, label, url, description in RESEARCH_LINKS[sport]:
        st.markdown(f'**{category}** · [{label}]({url})  \n{description}')
    st.caption('Open the source for current status. These links use no odds credits and do not change the rankings.')


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


def show_board_research(picks, sport, board_scope):
    with st.expander('Research a line on this board'):
        st.caption('Choose a PrizePicks line and add your research. Saved reviews appear below the board in this session.')
        lines = picks.reset_index(drop=True)

        def describe(index):
            row = lines.iloc[index]
            return f"{row['player']} · {row['market']} {row['line']:g} · {row['event']}"

        board_key = sha256(repr(board_scope).encode()).hexdigest()[:16]
        index = st.selectbox('PrizePicks line to research', range(len(lines)), format_func=describe,
                             key=f'board_research:{board_key}:line')
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


def init_db():
    with sqlite3.connect(DB) as c:
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
    if not event_rankings:
        return board[['event', 'player', 'market', 'line']].assign(
            side=None, market_estimate_pct=None, comparison_books=None
        )
    ranked = pd.concat(event_rankings, ignore_index=True)
    fields = ['event', 'player', 'market', 'line']
    for frame in (ranked, board):
        for field in fields[:-1]:
            frame[field + '_key'] = frame[field].astype('string').str.strip().str.casefold()
    keys = [field + '_key' for field in fields[:-1]] + ['line']
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


def scan_top_picks(key, sport, regions, board, event_limit):
    events = fetch_events(key, SPORTS[sport])
    now = dt.datetime.now(dt.timezone.utc)
    upcoming = []
    for event in events:
        try:
            start = dt.datetime.fromisoformat(event['commence_time'].replace('Z', '+00:00'))
        except (KeyError, ValueError, AttributeError):
            continue
        if start.tzinfo and start > now:
            upcoming.append((start, event))
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


def compare_live_event(data, event, markets):
    books = data['bookmakers']
    prizepicks = sportsbook_rows({
        'bookmakers': [book for book in books if book.get('key') == 'prizepicks']
    })
    if prizepicks.empty:
        return pd.DataFrame()
    prizepicks['line'] = pd.to_numeric(prizepicks['line'], errors='coerce')
    board = (
        prizepicks.loc[prizepicks['market'].isin(markets) & prizepicks['line'].notna()
                       & prizepicks['player'].fillna('').str.strip().ne(''),
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
        starts_utc=event['commence_time'],
        status=results['market_estimate_pct'].notna().map({
            True: 'Matched', False: 'No exact sportsbook comparison',
        }),
    )


def load_live_prizepicks(key, sport, regions, event_limit, markets, compare):
    events = fetch_events(key, SPORTS[sport])
    now = dt.datetime.now(dt.timezone.utc)
    upcoming = []
    for event in events:
        try:
            start = dt.datetime.fromisoformat(event['commence_time'].replace('Z', '+00:00'))
        except (KeyError, ValueError, AttributeError):
            continue
        if start.tzinfo and start > now:
            upcoming.append((start, event))
    selected = sorted(upcoming, key=lambda item: (item[0], item[1]['id']))[:event_limit]

    results, issues = [], []
    checked = 0
    for _, event in selected:
        try:
            data, remaining = fetch_event_odds(
                key, SPORTS[sport], event['id'], regions, ','.join(markets)
            )
            frame = compare_live_event(data, event, markets)
            if not compare and not frame.empty:
                frame['status'] = 'PrizePicks line only'
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
            issues.append(f'{event_label(event)} — No standard PrizePicks lines returned.')
        if remaining == '0':
            issues.append('The provider reported zero remaining credits at fetch time.')
            break

    board = (pd.concat(results, ignore_index=True)
             .sort_values('market_estimate_pct', ascending=False, kind='stable')
             if results else pd.DataFrame())
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

best,tab1,tab2,tab3,tab4,research=st.tabs(['Best picks', '🔎 Scan markets','🧮 Evaluate prop','🗂️ Paper tracker','📘 Setup','Research sources'])
key_id = sha256(api_key.encode()).hexdigest()
with best:
    st.subheader('PrizePicks board by sport')
    source_mode = st.radio('PrizePicks line source', ['Load from feed', 'Upload CSVs'], horizontal=True)
    live_board = source_mode == 'Load from feed'
    if live_board:
        st.caption('Loads standard PrizePicks lines from The Odds API for the selected upcoming games and markets. Feed coverage can differ from your PrizePicks board.')
    else:
        st.caption('Upload one or more CSVs. Every imported PrizePicks line stays visible.')
    template = pd.DataFrame(columns=['sport', 'event', 'player', 'market', 'line', 'platform'])
    uploads = []
    if not live_board:
        st.download_button('Download PrizePicks CSV template', template.to_csv(index=False),
                           'prizepicks_template.csv', mime='text/csv')
        uploads = st.file_uploader('Upload current PrizePicks lines', type='csv',
                                   accept_multiple_files=True, key='prizepicks_boards')
    boards = []
    board_ids = []
    for upload in uploads:
        try:
            frame = normalize_upload(pd.read_csv(upload))
        except (ValueError, TypeError) as exc:
            st.error(f'{upload.name} — {exc}')
            continue
        frame = frame.dropna(subset=['sport', 'event', 'player', 'market'])
        frame = frame[frame['platform'].astype(str).str.strip().str.casefold().eq('prizepicks')]
        if frame.empty:
            st.info(f'{upload.name} has no valid PrizePicks lines.')
            continue
        boards.append(frame[template.columns])
        board_ids.append(sha256(upload.getvalue()).hexdigest())

    pick_sport = st.selectbox('Pick sport', ALL_SPORTS)
    with st.expander(f'{pick_sport} stats and availability'):
        show_research_links(pick_sport)
    if pick_sport in ('NFL', 'CFB'):
        show_football_weather()
    pick_region = st.selectbox('Sportsbook region', ['us', 'us2', 'uk', 'eu', 'au'])
    board = pd.DataFrame(columns=template.columns)
    if boards:
        all_lines = pd.concat(boards, ignore_index=True)
        identity = all_lines.drop(columns='platform').copy()
        for column in ['sport', 'event', 'player', 'market']:
            identity[column] = identity[column].astype(str).str.strip().str.casefold()
        all_lines = all_lines.loc[~identity.duplicated()]
        st.caption(f'{len(all_lines)} unique PrizePicks lines across all uploads.')
        with st.expander('View every imported line', expanded=True):
            st.dataframe(all_lines, hide_index=True, use_container_width=True)
        board = all_lines[identity.loc[all_lines.index, 'sport'].eq(pick_sport.casefold())]

    if pick_sport not in PICK_MARKETS:
        st.info(f'Automated sportsbook comparisons for {pick_sport} are pending.')
    else:
        allowed_markets = PICK_MARKETS[pick_sport].split(',')
        st.caption(f'Markets scanned for {pick_sport}: {", ".join(allowed_markets)}.')
        if not live_board:
            st.caption('Use Away Team at Home Team for event and PrizePicks for platform.')
        if boards and board.empty:
            st.info(f'The uploads contain no {pick_sport} lines.')
        event_limit = st.slider('Maximum games to scan', min_value=1, max_value=12, value=1)
        compare = True
        scan_markets = allowed_markets
        if live_board:
            scan_markets = st.multiselect('Markets to load', allowed_markets, default=allowed_markets[:1])
            compare = st.checkbox('Compare with sportsbooks', value=True,
                                  help='Turn off to load PrizePicks lines at half the maximum credit cost.')
        regions = (f'{pick_region},us_dfs' if compare else 'us_dfs') if live_board else pick_region
        credits = event_limit * len(scan_markets) * len(regions.split(','))
        st.caption(f'Maximum cost: {credits} provider credits. Identical requests can reuse cached odds for five minutes.')
        pick_scope = (key_id, pick_sport, regions, live_board, tuple(board_ids), event_limit,
                      tuple(scan_markets))
        action = 'Load PrizePicks lines' if live_board else 'Compare PrizePicks lines'
        if st.button(action, disabled=not api_key or not scan_markets or (not live_board and board.empty)):
            st.session_state.pop('prizepicks_matches', None)
            try:
                with st.spinner('Comparing PrizePicks lines…'):
                    result = (load_live_prizepicks(api_key, pick_sport, regions, event_limit, scan_markets, compare)
                              if live_board else
                              scan_top_picks(api_key, pick_sport, pick_region, board, event_limit))
                st.session_state['prizepicks_matches'] = (pick_scope, result)
            except ProviderError as exc:
                st.error(str(exc))
        if not api_key:
            st.info('Enter an Odds API key to compare lines.')
        saved = st.session_state.get('prizepicks_matches')
        if saved and saved[0] == pick_scope:
            picks, checked, targeted, issues = saved[1]
            st.caption(f'{checked} of {targeted} selected games checked.')
            for issue in issues:
                st.warning(issue)
            if picks.empty:
                st.info('No PrizePicks lines returned for the selected games and markets.')
            else:
                matched = picks['status'].eq('Matched').sum()
                if compare:
                    st.caption(f'{matched} of {len(picks)} PrizePicks lines matched.')
                else:
                    st.caption(f'{len(picks)} PrizePicks lines loaded.')
                    picks = picks.drop(columns=['side', 'market_estimate_pct', 'comparison_books'])
                st.dataframe(picks, hide_index=True, use_container_width=True)
                st.download_button('Download all comparison results', picks.to_csv(index=False),
                                   f'{pick_sport.lower()}_prizepicks.csv', mime='text/csv')
                show_board_research(picks, pick_sport, pick_scope)

with tab1:
    st.subheader('Live sportsbook market discovery')
    sport=st.selectbox('Sport',list(SPORTS),key='live_sport')
    st.caption('See Research sources for all nine sports. The player-prop scanner supports the five leagues in this menu.')
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
    st.subheader('Import PrizePicks / Underdog lines')
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
        platform=st.selectbox('Platform',['PrizePicks','Underdog']); line=st.number_input('Platform line',value=19.5,step=.5); side=st.selectbox('Side',['Over','Under']); dist=st.selectbox('Model distribution',['Normal (continuous approximation)','Poisson (counts)'])
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
5. Import PrizePicks/Underdog CSV lines and manually enter independent projections in **Evaluate prop**. Save selections before games start, then grade them in **Paper tracker**.

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
    st.subheader('Research sources and provider coverage')
    st.caption('All nine sports support manual evaluation. Player-stat adapters are pending.')
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
