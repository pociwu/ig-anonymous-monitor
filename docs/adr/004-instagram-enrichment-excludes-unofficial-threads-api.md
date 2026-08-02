# Instagram enrichment excludes the unofficial Threads API

Authenticated Instagram enrichment will use `instagrapi` only. The archived reverse-engineered `threads-api` package is excluded because it adds a second unstable private integration without helping retrieve Instagram follower/following relationships; any future Threads platform support will be a separate module built on Meta's official Threads API.
