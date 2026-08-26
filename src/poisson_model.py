import math
import requests


def get_team_stats(season="20242025"):
    """Hakee kaikkien joukkueiden tilastot yhdeltä kaudelta."""
    url = "https://api.nhle.com/stats/rest/en/team/summary"
    params = {"cayenneExp": f"seasonId={season} and gameTypeId=2"}
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json().get("data", [])


def build_team_ratings(team_stats):
    """Laskee jokaiselle joukkueelle hyökkäys- ja puolustusvoiman
    suhteessa liigan keskiarvoon."""
    league_avg_gf = sum(t["goalsForPerGame"] for t in team_stats) / len(team_stats)
    league_avg_ga = sum(t["goalsAgainstPerGame"] for t in team_stats) / len(team_stats)

    ratings = {}
    for t in team_stats:
        ratings[t["teamFullName"]] = {
            "attack": t["goalsForPerGame"] / league_avg_gf,
            "defense": t["goalsAgainstPerGame"] / league_avg_ga,
        }
    return ratings, league_avg_gf


def poisson_pmf(k, lam):
    """Poisson-jakauman pistetodennäköisyys: P(X = k)."""
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def predict_match(home_team, away_team, ratings, league_avg_goals, max_goals=10):
    """Ennustaa ottelun tuloksen Poisson-mallilla.
    Palauttaa odotetut maalimäärät, voittotodennäköisyydet ja
    yli/alle-todennäköisyyksiä varten tarvittavan jakauman."""
    home = ratings[home_team]
    away = ratings[away_team]

    # Odotettu maalimäärä = liigan keskiarvo * oma hyökkäysvoima * vastustajan puolustusheikkous
    lambda_home = league_avg_goals * home["attack"] * away["defense"]
    lambda_away = league_avg_goals * away["attack"] * home["defense"]

    # Rakennetaan täysi todennäköisyysmatriisi kaikille maaliyhdistelmille
    home_win_prob = 0.0
    away_win_prob = 0.0
    tie_prob = 0.0
    total_goals_dist = {}  # yhteismaalimäärä -> todennäköisyys

    for i in range(max_goals + 1):
        p_home_i = poisson_pmf(i, lambda_home)
        for j in range(max_goals + 1):
            p_away_j = poisson_pmf(j, lambda_away)
            p_combo = p_home_i * p_away_j

            if i > j:
                home_win_prob += p_combo
            elif i < j:
                away_win_prob += p_combo
            else:
                tie_prob += p_combo

            total = i + j
            total_goals_dist[total] = total_goals_dist.get(total, 0.0) + p_combo

    # NHL:ssä ei tasapelejä - regulaatioajan tasapeli ratkeaa OT/SO:lla.
    # YKSINKERTAISTUS: koska meillä ei ole tarkkaa dataa joukkueiden
    # OT/SO-menestyksestä, jaetaan tasapelitodennäköisyys 50/50.
    # Tätä voi tarkentaa myöhemmin winsInShootout-datalla.
    final_home_win = home_win_prob + tie_prob * 0.5
    final_away_win = away_win_prob + tie_prob * 0.5

    return {
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
        "home_win_prob": final_home_win,
        "away_win_prob": final_away_win,
        "total_goals_dist": total_goals_dist,
    }


def over_under_prob(total_goals_dist, line):
    """Laskee yli/alle-todennäköisyyden annetulle rajalle (esim. 5.5).
    Rajan pitää olla .5-luku, jotta ei tule tasapelimahdollisuutta."""
    over_prob = sum(p for goals, p in total_goals_dist.items() if goals > line)
    under_prob = 1 - over_prob
    return over_prob, under_prob


def calculate_edge(model_probability, bookmaker_odds):
    """Laskee eron oman arvion ja bookmakerin implisiittisen
    todennäköisyyden välillä."""
    implied_prob = 1 / bookmaker_odds
    return model_probability - implied_prob


def build_h2h_rationale(team_name, model_prob, bookmaker_odds, lambda_for, lambda_against):
    """Rakentaa sanallisen perustelun h2h-vedolle."""
    implied_prob = 1 / bookmaker_odds
    edge = model_prob - implied_prob
    return (
        f"{team_name}: malli ennustaa {model_prob*100:.0f}% voittotodennäköisyyden "
        f"(odotetut maalit {lambda_for:.1f}-{lambda_against:.1f}), "
        f"bookmakerin kerroin implikoi {implied_prob*100:.0f}%. Edge: {edge*100:+.0f}pp."
    )


def build_totals_rationale(line, direction, model_prob, bookmaker_odds, expected_total):
    """Rakentaa sanallisen perustelun yli/alle-vedolle."""
    implied_prob = 1 / bookmaker_odds
    edge = model_prob - implied_prob
    return (
        f"{direction} {line}: malli ennustaa odotetuksi yhteismaalimääräksi {expected_total:.1f} "
        f"({model_prob*100:.0f}% todennäköisyys), bookmakerin kerroin implikoi {implied_prob*100:.0f}%. "
        f"Edge: {edge*100:+.0f}pp."
    )


if __name__ == "__main__":
    stats = get_team_stats()
    ratings, league_avg = build_team_ratings(stats)

    print(f"Liigan keskiarvo maaleja per ottelu: {league_avg:.2f}\n")

    # Testataan kahdella tunnetulla joukkueella
    home, away = "Edmonton Oilers", "Vancouver Canucks"
    if home in ratings and away in ratings:
        result = predict_match(home, away, ratings, league_avg)

        print(f"{home} vs {away}")
        print(f"Odotetut maalit: {result['lambda_home']:.2f} - {result['lambda_away']:.2f}")
        print(f"{home} voittotodennäköisyys: {result['home_win_prob']*100:.1f}%")
        print(f"{away} voittotodennäköisyys: {result['away_win_prob']*100:.1f}%")

        # Esimerkki: h2h edge bookmakerin kertoimeen 1.37 verrattuna
        example_odds = 1.37
        edge = calculate_edge(result["home_win_prob"], example_odds)
        print(f"\nEsimerkki - {home} @ {example_odds}: edge {edge*100:+.1f}pp")
        print(build_h2h_rationale(home, result["home_win_prob"], example_odds,
                                   result["lambda_home"], result["lambda_away"]))

        # Esimerkki: yli/alle 5.5 maalia
        expected_total = result["lambda_home"] + result["lambda_away"]
        over_prob, under_prob = over_under_prob(result["total_goals_dist"], 5.5)
        print(f"\nYli 5.5 maalia: {over_prob*100:.1f}%")
        print(f"Alle 5.5 maalia: {under_prob*100:.1f}%")
    else:
        print(f"Joukkuetta ei löytynyt datasta. Saatavilla: {list(ratings.keys())[:5]}...")