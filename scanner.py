
import os
import requests
from datetime import datetime, timezone

CMC_API_KEY = os.environ["CMC_API_KEY"]

CMC_HEADERS = {
    "X-CMC_PRO_API_KEY": CMC_API_KEY,
    "Accepts": "application/json",
}

BASE_URL = "https://pro-api.coinmarketcap.com"


def cmc_get(endpoint, params=None):
    url = BASE_URL + endpoint

    response = requests.get(
        url,
        headers=CMC_HEADERS,
        params=params or {},
        timeout=30,
    )

    response.raise_for_status()
    return response.json()


def get_top_100():
    data = cmc_get(
        "/v1/cryptocurrency/listings/latest",
        {
            "start": 1,
            "limit": 100,
            "convert": "USD",
        },
    )

    return data["data"]


def get_historical_volume(symbol):
    """
    Attempts to retrieve historical OHLCV data from CMC.
    We use daily data to estimate 7D and 14D average volume.
    """

    data = cmc_get(
        "/v2/cryptocurrency/ohlcv/historical",
        {
            "symbol": symbol,
            "convert": "USD",
            "time_period": "daily",
            "count": 15,
        },
    )

    try:
        quotes = data["data"][symbol][0]["quotes"]
    except (KeyError, IndexError, TypeError):
        return []

    volumes = []

    for item in quotes:
        try:
            volume = item["quote"]["USD"]["volume"]

            if volume is not None:
                volumes.append(float(volume))

        except (KeyError, TypeError, ValueError):
            continue

    return volumes


def average(values):
    if not values:
        return 0

    return sum(values) / len(values)


def analyze_coin(coin):
    quote = coin["quote"]["USD"]

    price = float(quote["price"])
    change_24h = float(quote.get("percent_change_24h") or 0)
    change_7d = float(quote.get("percent_change_7d") or 0)

    volume_24h = float(quote.get("volume_24h") or 0)
    volume_change_24h = float(
        quote.get("volume_change_24h") or 0
    )

    symbol = coin["symbol"]

    historical_volumes = get_historical_volume(symbol)

    avg_7d = average(historical_volumes[-7:])
    avg_14d = average(historical_volumes[-14:])

    volume_ratio_7d = 0

    if avg_7d > 0:
        volume_ratio_7d = volume_24h / avg_7d

    volume_ratio_14d = 0

    if avg_14d > 0:
        volume_ratio_14d = volume_24h / avg_14d

    # Early movement:
    # We want coins that are beginning to move,
    # rather than coins that have already pumped.
    early_move = 2 <= change_24h <= 3.5

    # Volume acceleration.
    volume_acceleration = (
        volume_ratio_7d >= 1.15
        or volume_ratio_14d >= 1.15
        or volume_change_24h >= 10
    )

    # Initial candidate condition.
    candidate = early_move and volume_acceleration

    return {
        "name": coin["name"],
        "symbol": symbol,
        "rank": coin["cmc_rank"],
        "price": price,
        "change_24h": change_24h,
        "change_7d": change_7d,
        "volume_24h": volume_24h,
        "volume_change_24h": volume_change_24h,
        "avg_7d": avg_7d,
        "avg_14d": avg_14d,
        "volume_ratio_7d": volume_ratio_7d,
        "volume_ratio_14d": volume_ratio_14d,
        "candidate": candidate,
    }


def main():
    print("🐋 FIL BEFORE PUMP SCANNER")
    print("=" * 50)

    coins = get_top_100()

    candidates = []

    for index, coin in enumerate(coins, start=1):

        try:
            result = analyze_coin(coin)

            print(
                f"[{index}/100] "
                f"{result['symbol']} "
                f"{result['change_24h']:+.2f}% "
                f"VOL7={result['volume_ratio_7d']:.2f}x "
                f"VOL14={result['volume_ratio_14d']:.2f}x"
            )

            if result["candidate"]:
                candidates.append(result)

        except Exception as error:
            print(
                f"⚠️ {coin.get('symbol', 'UNKNOWN')} "
                f"failed: {error}"
            )

    print()
    print("=" * 50)
    print(f"🎯 Candidates found: {len(candidates)}")
    print("=" * 50)

    for coin in candidates:

        print(
            f"""
🔥 {coin['name']} ({coin['symbol']})
Rank: #{coin['rank']}
Price: ${coin['price']:.8f}
24h: {coin['change_24h']:+.2f}%
7D: {coin['change_7d']:+.2f}%

📊 Volume
24h: ${coin['volume_24h']:,.0f}
7D Avg: ${coin['avg_7d']:,.0f}
14D Avg: ${coin['avg_14d']:,.0f}

Volume / 7D Avg:
{coin['volume_ratio_7d']:.2f}x

Volume / 14D Avg:
{coin['volume_ratio_14d']:.2f}x

24h Volume Change:
{coin['volume_change_24h']:+.2f}%

STATUS: EARLY CANDIDATE
"""
        )


if __name__ == "__main__":
    main()
