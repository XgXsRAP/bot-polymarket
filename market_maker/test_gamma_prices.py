"""
Quick test: verify polymarket_gamma.py is fetching live prices.
Run: python test_gamma_prices.py
Watches for 60 seconds and logs every price update.
"""
import asyncio
import time
from polymarket_gamma import PolymarketGammaFeed


async def main():
    feed = PolymarketGammaFeed(poll_interval=10.0)
    print("Starting Gamma feed...")
    await feed.start()
    print(f"  condition_id: {feed._condition_id}")
    print(f"  yes_token_id: {feed._yes_token_id}")
    print(f"  no_token_id:  {feed._no_token_id}")
    print(f"  ws_available: {not feed._ws_unavailable}")
    print()

    prev_bid, prev_ask = 0.0, 1.0
    updates = 0
    start = time.time()
    duration = 60

    print(f"Monitoring prices for {duration}s...\n")
    print(f"{'Time':>6s}  {'Bid':>8s}  {'Ask':>8s}  {'Spread':>8s}  {'Age':>5s}  {'Source'}")
    print("-" * 58)

    try:
        while time.time() - start < duration:
            bid, ask = feed.best_bid, feed.best_ask
            age = feed.price_age
            tradeable = feed.is_tradeable

            # Print on every check, highlight changes
            changed = (abs(bid - prev_bid) > 0.0001 or abs(ask - prev_ask) > 0.0001)
            marker = " << UPDATED" if changed else ""
            if changed:
                updates += 1

            elapsed = time.time() - start
            spread = ask - bid if bid > 0 and ask < 1 else 0.0
            status = "OK" if tradeable else "STALE" if age > 10 else "INIT"

            print(
                f"{elapsed:5.1f}s  {bid:8.4f}  {ask:8.4f}  {spread:8.4f}  "
                f"{age:4.1f}s  {status}{marker}"
            )

            prev_bid, prev_ask = bid, ask
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\nStopped by user")
    finally:
        await feed.stop()

    elapsed = time.time() - start
    print(f"\n{'=' * 58}")
    print(f"RESULTS ({elapsed:.0f}s)")
    print(f"  Price updates detected: {updates}")
    print(f"  Updates/minute:         {updates / (elapsed / 60):.1f}")
    print(f"  Final status:           {feed.status()}")
    if updates == 0:
        print("  WARNING: No price changes detected — prices may still be stale!")
    elif updates < 5:
        print("  NOTE: Few updates — market may be quiet or feed is slow")
    else:
        print("  OK: Prices are updating in real-time")
    print(f"{'=' * 58}")


if __name__ == "__main__":
    asyncio.run(main())
