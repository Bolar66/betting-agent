import math


def _pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def scoreline_probs(lambda_home, lambda_away, max_goals=12):
    """Kaikkien (kotimaalit, vierasmaalit) -tulosten todennäköisyydet
    Poisson-mallilla. Malli kuvaa varsinaista peliaikaa: jatkoaika ja
    rankkarit tulkitaan yhden maalin voitoksi (ero 0 -> ratkeaa +-1)."""
    home = [_pmf(i, lambda_home) for i in range(max_goals + 1)]
    away = [_pmf(j, lambda_away) for j in range(max_goals + 1)]
    return {(i, j): home[i] * away[j]
            for i in range(max_goals + 1) for j in range(max_goals + 1)}


def prob_spread_cover(probs, is_home, point):
    """Todennäköisyys että joukkue voittaa tasoituksen (esim. -1.5, +1.5)."""
    total = 0.0
    for (i, j), p in probs.items():
        margin = (i - j) if is_home else (j - i)
        if margin + point > 0:
            total += p
    return total


def prob_total_over(probs, line):
    """Todennäköisyys että maaleja tulee yli rajan (esim. 6.5)."""
    return sum(p for (i, j), p in probs.items() if i + j > line)