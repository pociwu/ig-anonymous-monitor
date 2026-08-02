# Isolate authenticated relationship and anonymous member workers

Deployment uses separate `relationship-worker` and `member-enrichment-worker` services alongside the existing monitor and dashboard. Only the relationship worker receives collector credentials/session mounts and it never launches Playwright; anonymous member enrichment has no access to Instagram authentication material, and neither workload can extend or crash the existing 15-minute anonymous monitor cycle.
