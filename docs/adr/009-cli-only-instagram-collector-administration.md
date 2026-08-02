# Keep Instagram collector administration out of the dashboard

Collector credentials live only in the host `.env`, while persisted session/device state lives in a separate `collector-secrets/` host directory mounted only into the relationship worker; the shared `data/` mount must never contain session material. Login, challenge, activation, and risk-hold recovery are CLI-only operations. The unauthenticated Tailscale-scoped Dashboard may display non-secret collector health but cannot receive credentials, expose session material, or trigger password login.
