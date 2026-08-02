# Use a dedicated Instagram collector identity

Authenticated Instagram enrichment will run through one dedicated Instagram collector identity bound to a stable OCI IP, device profile, and persisted session. Concurrent phone, browser, other-host, and rotating-proxy use is excluded because session churn and location changes conflict with the design goal of minimizing authentication challenges and automated relogin attempts.
