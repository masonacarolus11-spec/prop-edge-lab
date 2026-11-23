"""NBA defensive rebound opportunity from verified opponent field-goal misses."""

import pandas as pd


def missed_shot_profiles(logs, before):
    required = {'team_abbreviation', 'game_id', 'game_date', 'min', 'fga', 'fgm'}
    if not required.issubset(logs.columns):
        return {}
    shot_rows = logs.loc[logs['game_date'].lt(before), sorted(required)].copy()
    columns = ['min', 'fga', 'fgm']
    shot_rows[columns] = shot_rows[columns].apply(pd.to_numeric, errors='coerce')
    invalid = (shot_rows[columns].isna().any(axis=1) | shot_rows['min'].le(0)
               | shot_rows['fga'].lt(shot_rows['fgm']) | shot_rows['fgm'].lt(0))
    shot_rows['invalid'] = invalid
    totals = (
        shot_rows.groupby(['game_date', 'game_id', 'team_abbreviation'], as_index=False)
        .agg(minutes=('min', 'sum'), attempts=('fga', 'sum'), makes=('fgm', 'sum'),
             invalid=('invalid', 'any'))
    )
    totals = totals.loc[totals['minutes'].ge(235) & ~totals['invalid'] & totals['attempts'].gt(0)].copy()
    paired = totals.groupby('game_id')['team_abbreviation'].transform('nunique').eq(2)
    totals = totals.loc[paired].sort_values(['game_date', 'game_id'])
    if totals.empty:
        return {}
    totals['misses'] = totals['attempts'] - totals['makes']
    league = round(float(totals['misses'].mean()), 1)
    return {
        team: {
            'Opponent missed field goals per game': round(float(games['misses'].mean()), 1),
            'Opponent missed field goals last five': round(float(games['misses'].tail(5).mean()), 1)
            if len(games) >= 5 else None,
            'Opponent shot sample games': len(games),
            'League missed field goals per game': league,
        }
        for team, games in totals.groupby('team_abbreviation') if len(games) >= 5
    }
