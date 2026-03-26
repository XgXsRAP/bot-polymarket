"""
Polymarket Dynamic Fee Model (March 2026)

Polymarket uses a parabolic taker fee that peaks at 50% probability
and approaches zero at 0% and 100%. Makers receive a rebate (fraction
of the taker fee) for providing liquidity.

Formula:  fee_rate = max_fee * 4 * p * (1 - p)

At p=0.50 crypto:  0.018 * 4 * 0.25 = 1.80%
At p=0.80 crypto:  0.018 * 4 * 0.16 = 1.15%
At p=0.95 crypto:  0.018 * 4 * 0.0475 = 0.34%

Gas on Polygon: ~$0.005 per transaction (flat).
"""

# ── Category-specific maximum taker fee rates (peak at p=0.50) ──
CATEGORY_MAX_FEES = {
    "crypto": 0.018,        # 1.80% peak
    "finance": 0.010,       # 1.00% peak
    "politics": 0.010,      # 1.00% peak
    "tech": 0.010,          # 1.00% peak
    "sports": 0.0075,       # 0.75% peak
    "geopolitics": 0.0,     # Free
}

# ── Maker rebate as fraction of the taker fee ──
CATEGORY_REBATE_FRACTION = {
    "crypto": 0.30,         # 30% of taker fee returned to maker
    "finance": 0.25,
    "politics": 0.25,
    "tech": 0.25,
    "sports": 0.20,
    "geopolitics": 0.0,
}

# ── Polygon gas cost per transaction ──
GAS_COST_PER_TX = 0.005


def polymarket_taker_fee(price: float, category: str = "crypto") -> float:
    """
    Dynamic taker fee rate based on contract probability.

    Parabolic curve: peaks at p=0.50, zero at p=0.0 and p=1.0.

    Args:
        price: Contract price / probability (0.0 to 1.0)
        category: Market category ("crypto", "finance", "sports", etc.)

    Returns:
        Fee rate as a decimal (e.g., 0.018 = 1.8%)
    """
    max_fee = CATEGORY_MAX_FEES.get(category, 0.010)
    p = max(0.0, min(1.0, price))
    return max_fee * 4.0 * p * (1.0 - p)


def polymarket_taker_fee_amount(price: float, shares: float,
                                category: str = "crypto") -> float:
    """
    Absolute taker fee in dollars.

    Args:
        price: Fill price per share
        shares: Number of shares
        category: Market category

    Returns:
        Fee amount in dollars (positive, subtract from P&L)
    """
    return shares * price * polymarket_taker_fee(price, category)


def polymarket_maker_rebate(price: float, category: str = "crypto") -> float:
    """
    Maker rebate rate — you get paid for providing liquidity.

    Returns:
        Rebate rate as a decimal (e.g., 0.0054 = 0.54% at p=0.50 crypto)
    """
    rebate_frac = CATEGORY_REBATE_FRACTION.get(category, 0.25)
    return polymarket_taker_fee(price, category) * rebate_frac


def polymarket_maker_rebate_amount(price: float, shares: float,
                                   category: str = "crypto") -> float:
    """
    Absolute maker rebate in dollars.

    Returns:
        Rebate in dollars (positive, add to P&L)
    """
    return shares * price * polymarket_maker_rebate(price, category)


def net_fill_fee(price: float, shares: float, is_maker: bool = True,
                 category: str = "crypto", include_gas: bool = True) -> float:
    """
    Net fee impact for a single fill.

    Positive return = cost (subtract from P&L).
    Negative return = rebate (add to P&L).

    Args:
        price: Fill price per share
        shares: Number of shares
        is_maker: True for resting order fills (rebate), False for crossing (fee)
        category: Market category
        include_gas: Whether to include Polygon gas cost

    Returns:
        Net fee in dollars. Positive = cost, negative = you earn.
    """
    if is_maker:
        result = -polymarket_maker_rebate_amount(price, shares, category)
    else:
        result = polymarket_taker_fee_amount(price, shares, category)

    if include_gas:
        result += GAS_COST_PER_TX

    return result


def minimum_profitable_spread(price: float, category: str = "crypto") -> float:
    """
    Minimum spread (in dollars) for a profitable round trip.

    Assumes worst case: one side maker, one side taker.
    Both-maker is always profitable (any spread > 0 works).
    Both-taker is: 2 * taker_fee * price.

    Returns:
        Minimum spread in dollars for the mixed maker/taker scenario.
    """
    taker_cost = polymarket_taker_fee(price, category) * price
    maker_earn = polymarket_maker_rebate(price, category) * price
    return taker_cost - maker_earn
