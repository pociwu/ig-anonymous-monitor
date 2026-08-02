# Separate relationship membership from member profile enrichment

`instagrapi` is the authority for follower/following membership IDs and usernames, while `insta-stories-viewer.com` may enrich an individual public member profile only when that member is new, renamed, or explicitly opened by an operator. Baseline and unchanged lists are never batch-expanded into anonymous website visits, preventing a 1,000-member snapshot from becoming 1,000 additional browser loads.
