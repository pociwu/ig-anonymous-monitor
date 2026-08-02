# Share one canonical Instagram Profile ID across identity providers

`instagrapi` and Apify populate and cross-check one canonical Instagram Profile ID rather than owning parallel identities. A mismatch becomes an identity conflict and never overwrites automatically; Apify remains the budget-capped username-resolution fallback when authenticated collection is unavailable, while relationship-member IDs come directly from `instagrapi` without per-member Apify calls.
