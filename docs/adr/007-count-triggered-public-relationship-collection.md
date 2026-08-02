# Trigger public-account relationship collection from count changes

Authenticated follower/following collection applies only to accounts confirmed public by anonymous monitoring. Full relationship work is queued and coalesced when follower or following counts change, with a randomized 30-day reconciliation to detect offsetting membership changes that leave totals unchanged; it does not run on every 15-minute anonymous inspection.
