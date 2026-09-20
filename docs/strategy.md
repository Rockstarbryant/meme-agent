# Traction Momentum v1
Weights (initial defaults, configurable, auto-normalised, NOT claimed optimal): momentum 25, buyer growth 20, buy/sell pressure 15,
liquidity quality 15, holder distribution 10, creator behaviour 10, market quality 5. Qualification requires the score threshold AND
traction gates (min unique buyers, buy/sell ratio, min age, positive 5m momentum, required data present). Missing data scores 0 and is
listed in `data_gaps`; an unknown creator is not treated as good. Every decision stores strategy id, version and full config snapshot.
Exits (defaults): +30%->sell 15%, +60%->20%, +100%->25%, +200%->25% of the initial position; remainder on a 20% trailing stop
(armed after the first tier); hard stop -20%; stagnation; momentum/liquidity deterioration; manual/emergency close.
