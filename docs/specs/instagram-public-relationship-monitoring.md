# Instagram Public Relationship Monitoring — Phase One Specification

Status: approved for implementation planning. This document specifies behavior; it does not authorize or perform a live Instagram login.

## Problem Statement

The existing monitor observes public Instagram profile metadata and public media through `insta-stories-viewer.com`, but it only records follower/following counts. It cannot identify which accounts joined or left Followers, Following, or the intersection of both lists.

Phase one adds a guarded authenticated read path with `instagrapi` for public relationship membership. The design must minimize collector-account risk, never turn partial pagination into mass removal events, keep credentials outside the unauthenticated Dashboard, and preserve existing anonymous monitoring behavior.

## Goals

1. Track public-account Followers, Following, and Mutual follow membership.
2. Record and notify additions and removals against the last complete directional baseline.
3. Trigger authenticated work from anonymous follower/following count changes, plus a randomized 30-day reconciliation.
4. Limit each directional list to 1,000 members and make incomplete or out-of-scope states explicit.
5. Reuse one dedicated collector identity, stable OCI IP, device identity, and persisted session.
6. Fail closed on login, challenge, account restriction, and rate-limit signals.
7. Enrich only new, renamed, or manually opened public member profiles through the anonymous viewer.
8. Show relationship state and history in the Dashboard without adding Instagram write actions.
9. Preserve existing anonymous inspection, media, Apify identity resolution, Telegram, and Docker behavior.

## Non-Goals

- Authenticated Posts, Stories, Reels, Highlights, comments, likers, viewers, recommendations, search, or feed collection.
- Private-account relationship or media collection.
- Following, unfollowing, liking, commenting, messaging, posting, Story viewing, or any other Instagram write/interaction action.
- Meta Threads integration or the archived unofficial `threads-api` package.
- Downloading relationship-member media. A member must be promoted to one of the 16 formal monitored accounts before existing media collection applies.
- Exact relationship event timestamps when a target was private or between two periodic snapshots.
- A guarantee that the collector account cannot be challenged or restricted.

## Fixed Product Rules

| Rule | Approved value |
|---|---:|
| Maximum formal monitored accounts | 16 |
| Relationship eligibility | Confirmed public accounts only |
| Maximum complete list size | 1,000 per direction |
| Relationship jobs | 6 per Asia/Taipei calendar day |
| Minimum relationship-job start interval | 4 hours |
| Page size | 200 members |
| Page delay | Random 10–20 seconds |
| Followers/Following direction cooldown | Random 2–5 minutes |
| Reconciliation age | 30 days, randomly dispersed |
| Collector observation period | Minimum 72 hours |
| Canary | One operator-selected low-volume public account for 7 days |
| Anonymous member enrichments | 66 per Asia/Taipei calendar day |
| Enrichment delay | Random 30–90 seconds |
| Enrichment failure retry floor | 6 hours |
| Relationship history retention | 365 days |
| Relationship run retention | 90 days |
| Dashboard page size | 50 members |
| Telegram usernames per category | 20, then “N more” |

Safety values are hard ceilings or floors. Configuration may reduce volume or increase delays, but it cannot increase the limits or shorten the safety intervals above.

## Architecture

The deployment contains four Docker services built from one image:

```text
monitor
  anonymous profile/media inspection
  detects privacy/count transitions
  enqueues relationship work
  delivers the existing persistent Telegram event queue

relationship-worker
  only service with collector credentials/session access
  collector lifecycle and health checks
  instagrapi relationship work, comparison, history, notifications
  no Playwright

member-enrichment-worker
  no Instagram credentials/session
  Playwright profile-only anonymous enrichment
  no member media expansion or download

dashboard
  relationship read model and account-level switches
  no credential/session access and no login endpoint
```

SQLite remains the shared coordination store and uses WAL. Workers communicate through durable tables, not process memory or synchronous HTTP. Every claimed job has a lease so container restarts return abandoned work to pending state without duplicate change events.

## Deep Modules and Interfaces

The following are the external seams. Callers and tests use these interfaces; SQL, pagination cursors, event deduplication, and exception mapping remain implementation details.

### Relationship Trigger Module

```python
class RelationshipTrigger:
    def observe_profile(
        self,
        account_id: int,
        previous: ProfileSnapshot | None,
        current: ProfileSnapshot,
        observed_at: datetime,
    ) -> TriggerOutcome: ...
```

`observe_profile` is called after a successful anonymous profile observation. It owns:

- public/private/unknown eligibility transitions;
- directional count-change detection;
- per-account switch enforcement;
- scope-exceeded state;
- queue coalescing and direction widening;
- frozen-state behavior;
- no-op behavior while global enrichment is disabled.

The monitor must not construct relationship job rows directly.

### Relationship Worker Module

```python
class RelationshipWorker:
    def run_once(self, now: datetime) -> WorkOutcome: ...
```

One call performs at most one eligible unit of work or reports why none can run. It owns collector-state checks, job budget/spacing, claiming/leasing, directional collection, complete/incomplete application, Mutual follow derivation, retention, event creation, and sanitized diagnostics.

The worker loop may call `run_once` repeatedly with an interruptible wait. It must not contain policy outside this module.

### Member Enrichment Worker Module

```python
class MemberEnrichmentWorker:
    def run_once(self, now: datetime) -> WorkOutcome: ...
```

It owns priority, the 66/day budget, delay and retry timing, stale-data suppression, job lease, public/profile-only scraping, member-profile persistence, and permanent unavailable/private state.

### Collector Administration Module

```python
class CollectorAdministration:
    def status(self, now: datetime) -> CollectorStatus: ...
    def login(self, now: datetime) -> CollectorStatus: ...
    def approve(self, canary_account_id: int, now: datetime) -> CollectorStatus: ...
    def begin_recovery(self, now: datetime) -> CollectorStatus: ...
```

Only the CLI uses this interface. There is no Dashboard adapter.

### True-External Ports

Two production adapters and deterministic test adapters justify these internal seams:

```python
class InstagramRelationshipSource(Protocol):
    def login_or_validate_saved_session(self) -> CollectorIdentity: ...
    def own_account_health(self) -> None: ...
    def resolve_public_user(self, username: str) -> RelationshipTarget: ...
    def iter_members(
        self, user_id: str, direction: Direction, page_size: int, limit: int
    ) -> Iterator[RelationshipPage]: ...

class AnonymousMemberProfileSource(Protocol):
    async def fetch_profile(self, username: str) -> MemberProfileResult: ...
```

The production relationship adapter wraps a pinned, tested `instagrapi` version. It is the only module allowed to import `instagrapi` or map its exception classes. The production anonymous adapter reuses the existing Playwright scraper in a new profile-only mode that never activates or expands media tabs.

Clock and random-delay dependencies are injected into worker modules so tests never wait in real time.

## Collector Lifecycle

Persistent states:

```text
unconfigured
  └─ CLI login succeeds → observing

observing
  ├─ at most one own-account health check per 24h
  ├─ 72h passes with valid session → awaiting_approval
  └─ collector-fatal signal → risk_hold

awaiting_approval
  ├─ CLI approve --canary ACCOUNT → canary
  └─ collector-fatal signal → risk_hold

canary
  ├─ baseline for exactly one target
  ├─ seven-day observation and final diagnostic refresh
  ├─ succeeds → active
  └─ any collector-fatal signal → risk_hold

active
  ├─ ordinary queued work
  └─ collector-fatal signal → risk_hold

risk_hold
  └─ CLI recovery after manual Instagram handling → observing (new 72h period)
```

The global configuration switch defaults to disabled. A deployed worker may expose health without logging in. Enabling configuration cannot bypass the lifecycle; an Active/canary state cannot bypass a disabled global switch.

There is no force-skip option for observation or canary. Collector state notifications are emitted once per transition and never include collector username, IP, cookies, session ID, UUIDs, password, TOTP secret, or raw Instagram responses.

## Error Classification

The `instagrapi` adapter maps library exceptions into three domain results.

### Collector-fatal

Challenge/checkpoint classes, `LoginRequired`, `BadPassword`, `TwoFactorRequired`, `FeedbackRequired`, `PleaseWaitFewMinutes`, `RateLimitError`, HTTP 429/`ClientThrottledError`, `SentryBlock`, account suspension, or terms/consent blocks immediately:

1. stop the current page and job;
2. transition to `risk_hold` transactionally;
3. release no further relationship jobs;
4. enqueue one sanitized Telegram state notification;
5. require CLI recovery and a new observation period.

No automatic password relogin, IP rotation, device regeneration, or tight retry is allowed.

### Transient relationship failure

Timeout, DNS/connection interruption, incomplete response, JSON decode failure, and upstream 5xx make only the current direction incomplete. They do not replace a complete baseline, infer removals, or retry immediately.

### Target ineligible

User-not-found, target-private, or target privacy unknown freezes/stops only that monitored target. It does not place the collector in risk hold.

## Trigger and Queue Semantics

### Anonymous observation

- A private or unknown target never enqueues authenticated collection.
- Public → private/unknown freezes the last complete relationship state, cancels pending target work, and produces no mass-leave events.
- Private/unknown → public enqueues a high-priority two-direction refresh.
- Followers count change enqueues Followers only.
- Following count change enqueues Following only.
- Both count changes coalesce into one two-direction job.
- A direction whose anonymous total exceeds 1,000 becomes `scope_exceeded` and is not paginated.
- Repeated changes while a target has pending/claimed work widen/update the existing job; they never create a burst of duplicates.

### Reconciliation

A target whose complete baselines are at least 30 days old receives a low-priority, randomly dispersed two-direction job. Count-change, reopened-target, canary, and rollout baseline jobs have higher priority. Diagnostic jobs use the same budgets and cannot bypass risk holds.

### Directional refresh

Refreshing one direction compares it with that direction’s last complete baseline. Mutual follow is recomputed with the other direction’s most recent complete baseline. If the other direction has no complete baseline, Mutual follow is `unavailable`, not empty.

### Incomplete collection

Members observed during an incomplete run are saved in run-scoped staging for diagnostics, but the current complete baseline and membership events are unchanged. Staging is retained with the run for up to 90 days. This records positive observations without turning a partial page into a new authoritative list.

### Complete collection

Applying a complete direction is one transaction:

1. validate the claimed job lease and collector state;
2. compare staged IDs with current membership;
3. update current membership and first/last/join/leave timestamps;
4. create deduplicated directional events;
5. derive and create deduplicated Mutual follow events;
6. store complete baseline metadata;
7. enqueue one Telegram digest and member-enrichment jobs;
8. finish the run/job;
9. discard complete-run staging rows.

The first baseline creates a baseline-complete notification only. It does not create one “joined” event per existing member.

### Private interval

When a target reopens, comparison is against the frozen last complete baseline. Resulting events are marked `private_interval=true`, carry the interval start and observation timestamps, and are described as net changes during the private interval rather than exact event times.

## Budgets, Delays, and Leases

- A relationship budget unit is consumed when a job starts, even if it later fails.
- A member-enrichment budget unit is consumed when a browser attempt starts.
- Budget days use `Asia/Taipei`; counters are derived from durable run/attempt rows, not memory.
- Six relationship starts per day and four hours between starts are enforced in the same transaction as job claiming.
- Only one relationship job may be claimed globally.
- Per-page delays are 10–20 seconds; direction cooldown is 2–5 minutes.
- Member-enrichment attempts are single-threaded and separated by 30–90 seconds.
- Relationship leases are renewed after every page and before direction cooldown. Enrichment leases are renewed around browser navigation.
- Expired leases return to pending only if the collector is not in risk hold. Event keys and transaction checks make replay idempotent.

## Member Profile Enrichment

### Enqueue reasons and priority

1. manual Dashboard open of stale/missing data;
2. member username changed;
3. new Mutual follow;
4. new Follower/Following membership.

Jobs coalesce by canonical member Instagram ID. A lower-priority pending job is upgraded rather than duplicated. An unchanged member enriched in the last 30 days is not re-enqueued unless the username changed.

### Saved fields

- canonical Instagram Profile ID;
- current username and prior usernames needed for audit;
- display name;
- avatar URL, local avatar path, and avatar hash;
- posts, followers, and following counts;
- bio;
- public/private/not-found status;
- source URL and last enrichment time.

No Posts, Stories, Highlights, Reels, photos, or videos are collected for a relationship member.

Private/not-found results become explicit terminal profile states. A transient anonymous-site failure is delayed by at least six hours. More than 66 attempts per Taipei day remain pending for later days.

Opening a member page never blocks on Playwright. It returns the last saved data and a queued/refreshing state. Promotion is an explicit POST that reuses existing account URL validation and the 16-account limit.

## Canonical Identity and Apify

- A monitored target continues to own one `instagram_profile_id`.
- If absent, a relationship target ID may populate it.
- If `instagrapi` and the saved/Apify ID agree, store cross-validation metadata.
- A mismatch creates an Identity conflict, preserves the existing ID, stores sanitized diagnostics, and notifies Telegram.
- Apify remains the monthly-5-USD-capped username-resolution fallback when relationship collection is unavailable.
- Relationship member IDs come directly from `instagrapi`; never call Apify per member.

## Persistent Data Model

Existing tables remain compatible. Schema creation/migration is idempotent.

### Existing table changes

`accounts` adds:

- `relationship_tracking INTEGER NOT NULL DEFAULT 1`
- `relationship_status TEXT NOT NULL DEFAULT 'pending_baseline'`
- `relationship_frozen_at TEXT`
- `followers_baseline_at TEXT`
- `following_baseline_at TEXT`
- `relationship_reconciled_at TEXT`
- `identity_verified_source TEXT`
- `identity_verified_at TEXT`
- `identity_conflict_json TEXT` (sanitized only)

### `collector_state` (singleton)

- `id` constrained to 1
- `state`
- observation start/eligible timestamps
- approval timestamp
- canary account/start/end timestamps
- last own-account health time
- risk-hold class, sanitized message, and timestamp
- session generation number
- created/updated timestamps

No credentials, cookies, TOTP, username, IP, or device UUID are stored.

### `relationship_jobs`

- account, reason, requested directions, priority
- pending/claimed/completed/failed/cancelled status
- available time, lease owner/expiry, attempts
- coalesced trigger metadata and observed count values
- created/started/finished timestamps and sanitized error

A partial unique index permits at most one active pending/claimed relationship job per account.

### `relationship_runs`

- job/account/reason/directions and Taipei budget day
- Followers and Following status (`not_requested`, `complete`, `incomplete`, `scope_exceeded`)
- reported totals, observed totals, page counts
- private-interval flag and interval start
- started/finished timestamps and sanitized error class/message

Runs are retained 90 days.

### `relationship_run_members`

- run, direction, member canonical ID, username and minimal returned fields
- unique `(run_id, direction, member_profile_id)`

Rows for complete applied runs are deleted; incomplete staging follows 90-day run retention.

### `relationship_members`

- canonical Instagram Profile ID primary key
- current username/display name/verified/avatar fields
- profile counts, bio, privacy state
- anonymous source URL
- enrichment status/attempt/error and timestamps
- created/updated/last-associated timestamps

### `account_relationships`

- account and member composite key
- current `is_follower` and `is_following`
- first/last seen timestamps
- per-direction joined/left timestamps
- last complete run IDs per direction

Mutual follow is derived from both booleans. Rows with neither relation remain for history and may be pruned after 365 days if no retained event references require them.

### `relationship_history`

- unique event key
- account/member/run
- relation kind (`follower`, `following`, `mutual`)
- change kind (`joined`, `left`)
- observed time and optional private-interval start
- created time

History is retained 365 days.

### `member_enrichment_jobs` and `member_enrichment_attempts`

Durable priority/coalescing, availability, lease, attempt, budget-day, outcome, and sanitized-error records. Attempts are the authoritative 66/day ledger.

## Configuration and Secrets

Example non-secret configuration:

```yaml
accounts:
  - url: https://insta-stories-viewer.com/example/
    enabled: true
    label: example
    relationship_tracking: true

instagram_enrichment:
  enabled: false
  member_limit_per_direction: 1000
  page_size: 200
  page_delay_min_seconds: 10
  page_delay_max_seconds: 20
  direction_delay_min_seconds: 120
  direction_delay_max_seconds: 300
  daily_relationship_jobs: 6
  minimum_job_interval_minutes: 240
  reconciliation_days: 30
  observation_hours: 72
  canary_days: 7
  daily_member_enrichments: 66
  member_delay_min_seconds: 30
  member_delay_max_seconds: 90
  member_retry_min_hours: 6
  member_stale_days: 30
```

Missing `instagram_enrichment` must behave as `enabled: false`, preserving existing installations. Existing accounts missing `relationship_tracking` default to true but cannot run while the global switch is false.

Host `.env` adds:

```dotenv
IG_COLLECTOR_USERNAME=
IG_COLLECTOR_PASSWORD=
IG_COLLECTOR_TOTP_SECRET=
```

The session/device file is stored under host `collector-secrets/`, mode 700 for the directory and 600 for files, ignored by Git/Docker build and excluded from ordinary backups. It is mounted read/write only into `relationship-worker` at a fixed internal path.

Compose must remove the shared `env_file` inheritance. `.env` remains Compose input, but each service receives only required variables:

- monitor: Telegram and Apify variables;
- relationship-worker: collector variables only;
- member-enrichment-worker: no application secrets;
- dashboard: no application secrets.

Collector status notifications are inserted into the shared event table and later delivered by monitor, so relationship-worker does not need Telegram credentials.

## CLI and `igmenu.sh`

Add a console entry point such as `ig-monitor-collector` with:

```text
status
login
approve --canary ACCOUNT
begin-recovery
queues
relationship-summary [--account ACCOUNT]
diagnostic-refresh --account ACCOUNT
```

Rules:

- commands return non-zero on invalid lifecycle transitions;
- no command prints secrets or raw session data;
- `approve` rejects observation periods shorter than 72 hours;
- recovery starts a new observation period and cannot jump to Active;
- diagnostics consume ordinary work budget and obey spacing/risk hold;
- login never loops and writes session settings only after a successful validated login.

Extend `igmenu.sh` with collector status, first login, approve, risk-hold instructions, queues, and relationship summaries. The shell menu only invokes the CLI and never parses/stores credentials itself.

## Dashboard

### Account detail

Add a Relationships section with tabs:

- Followers
- Following
- Mutual
- History

Each list uses server-side pagination with 50 rows, username/display-name search, and filters for current, left, new, and enrichment-pending. It shows avatar, username, display name, verified flag, first/last seen, joined/left times, last complete run, and completeness/scope/frozen status.

History defaults to 30 days and supports an explicit date range within retained history.

### Member detail

Shows last saved Relationship member profile, all relationships to this monitored target, enrichment source/time/status, and a non-blocking refresh status. It offers “Promote to monitored account” only when the 16-account limit and URL validation allow it.

### Management routes

Expected server-rendered routes (exact names may follow existing Flask naming):

- `GET /account/<id>/relationships`
- `GET /account/<id>/relationships/history`
- `GET /relationship-member/<profile_id>`
- `POST /relationship-member/<profile_id>/refresh`
- `POST /relationship-member/<profile_id>/promote`
- `POST /account/<id>/relationship-tracking`

All POST routes use the existing same-origin protection. There is no collector login, approve, recovery, global-enable, session download, or raw-diagnostic route.

Home cards show per-account relationship tracking state, last complete refresh, queue state, scope/frozen status, and the account-level toggle. Dashboard service health includes non-secret status for both workers.

## Telegram

### Relationship digest

One message per account per applied comparison contains directional and Mutual joined/left counts. Each category lists at most 20 usernames and links to Dashboard for the complete list. The first baseline reports sizes/status only.

Incomplete runs produce no removal or digest. A later complete run is authoritative. Private-interval digests explicitly say changes occurred sometime during the frozen interval.

### Collector/queue notifications

Send one notification per collector state transition, Identity conflict, relationship queue blocked for 24 hours, or member-enrichment queue blocked for 72 hours. Deduplication markers persist across restarts.

## Retention and Removal

- Current relationship state remains while the monitored account is active.
- Relationship history: 365 days.
- Relationship runs and incomplete staging: 90 days.
- Member profiles no longer associated with any current relationship: eligible for deletion 365 days after the last relationship ended, unless referenced by retained history.
- Removing a monitored account cancels pending work and asks a claimed worker to stop after the current page. It creates no mass-leave events and preserves existing relationship/media history.
- Re-adding the same Canonical Instagram Profile ID reconnects history and creates a new baseline; disabled-period changes are not assigned exact event times.

## Database and Deployment Migration

1. The new feature is globally disabled by default.
2. Stop the existing Compose services before first schema upgrade.
3. Create a normal SQLite backup with the existing backup interface.
4. Build the new image and run the idempotent schema migration once.
5. Create `collector-secrets/` with host ownership matching `PUID`/`PGID`, mode 700.
6. Start all four services. Existing anonymous behavior must pass health checks before any collector login.
7. Run CLI login, complete 72-hour observation, approve one canary, and enable the global switch.
8. Complete seven-day canary; only then release rollout baselines under the six/day budget.

Rollback before collector activation can use the previous image because new tables/columns are additive. After relationship data exists, rollback may ignore new tables but must not delete them. A rollback procedure must never copy `collector-secrets/` into ordinary backups.

## Testing Decisions

The module interfaces above are the test surfaces. CI never contacts Instagram, the anonymous website, Telegram, or Apify.

### Configuration tests

- missing section defaults globally disabled;
- existing account defaults relationship tracking true;
- exact approved values accepted;
- higher volume/shorter safety values rejected;
- collector environment variables required only for CLI login/relationship worker operation, not Dashboard/member worker;
- no configuration serialization includes secrets.

### Collector lifecycle tests

- login → observing → awaiting approval after 72 hours;
- approval before 72 hours rejected;
- canary restricted to one eligible target for seven days;
- canary success unlocks rollout; fatal result moves to risk hold;
- every fatal class maps to risk hold and one notification;
- recovery returns to a fresh observation period;
- no automatic relogin loop.

### Trigger/queue tests

- private/unknown never queues;
- directional count changes queue only the changed direction;
- both changes coalesce;
- repeated triggers do not duplicate work;
- >1,000 marks scope exceeded without collection;
- reopen queues both directions and freezes old baseline;
- reconciliation becomes due at 30 days;
- removal cancels without leave events.

### Worker/database integration tests

- budgets and four-hour spacing survive process restarts;
- atomic claim/lease recovery is idempotent;
- first baseline creates no joined history;
- complete comparisons create correct follower/following/mutual events;
- incomplete pagination never removes or replaces baseline;
- one-direction refresh uses the other complete baseline;
- missing opposite baseline makes Mutual unavailable;
- private-interval events preserve interval semantics;
- identity agreement/mismatch behavior;
- retention deletes only eligible history/staging/member data.

Use a temporary real SQLite database and fake Instagram adapter, fake clock, and deterministic random adapter. Do not assert SQL text or private helper calls.

### Member enrichment tests

- reason priority and job coalescing;
- 66/day enforcement across restart;
- 30–90 second spacing through fake clock;
- no refresh inside 30 days unless username changed/manual stale request;
- private/not-found terminal states;
- retry no earlier than six hours;
- profile-only result contains no media candidates/downloads;
- promotion reuses account validation and 16-account maximum.

### Dashboard and Telegram tests

- 50-row server pagination, search, filters, frozen/scope/incomplete labels;
- member open queues refresh without blocking;
- same-origin POST enforcement;
- no credential/session routes or rendered values;
- digest truncation at 20 usernames and correct “N more” count;
- baseline, private-interval, collector-state, conflict, and queue-stall formats;
- deduplication across restarts.

### Container/CI tests

- full pytest suite;
- `docker compose config` confirms only relationship-worker receives collector variables/session mount;
- Docker image imports the pinned `instagrapi` on Ubuntu ARM64-compatible Python 3.12;
- all four service commands start and health/status commands work with enrichment disabled;
- `.dockerignore` and `.gitignore` exclude `.env` and `collector-secrets/`.

## Acceptance Criteria

Phase one is ready for canary when:

1. Existing anonymous monitoring and media tests remain green.
2. A fresh upgrade starts with zero authenticated Instagram requests.
3. Dashboard/member worker containers cannot access the collector session path or collector environment variables.
4. CLI lifecycle cannot bypass observation, canary, budgets, or risk hold.
5. Fake-adapter tests prove complete, incomplete, directional, frozen, scope-exceeded, and identity-conflict behavior.
6. Telegram and Dashboard expose no collector secrets or raw API payloads.
7. Schema migration succeeds against a copy of the current SQLite database and preserves existing accounts/media/events.
8. Docker CI builds and validates four services.
9. Operator documentation includes setup, observation, canary, recovery, update, rollback, and secret-file permissions.

Production rollout is complete only after the 72-hour observation, seven-day canary, and remaining baseline rollout finish without collector-fatal signals.

## Delivery Slices

Implementation should proceed in independently testable slices:

1. Add disabled-by-default configuration, schema, domain models, and migrations.
2. Add profile observation trigger and durable/coalescing jobs without any Instagram adapter.
3. Add collector lifecycle, CLI, fake relationship adapter, and risk classification.
4. Add pinned `instagrapi` production adapter and relationship-worker under disabled integration tests.
5. Add complete/incomplete comparison, history, Mutual derivation, Telegram events, and retention.
6. Add profile-only anonymous adapter and member-enrichment-worker.
7. Add Dashboard relationship/member views, account toggle, and promotion.
8. Add Compose isolation, `igmenu.sh`, deployment documentation, migration/rollback tests, and canary runbook.

Each slice must keep existing tests green and add behavior tests at the module interface introduced by that slice.

## References

- [instagrapi user/follower interfaces](https://subzeroid.github.io/instagrapi/usage-guide/user.html)
- [instagrapi session persistence and device settings](https://subzeroid.github.io/instagrapi/usage-guide/interactions.html)
- [instagrapi anti-abuse and rate-limit practices](https://subzeroid.github.io/instagrapi/usage-guide/best-practices.html)
- [instagrapi exception taxonomy](https://subzeroid.github.io/instagrapi/exceptions.html)
- [instagrapi package metadata and supported Python versions](https://pypi.org/pypi/instagrapi/json)
- [Meta official Threads API workspace](https://www.postman.com/meta/threads/overview)
- [Archived unofficial `threads-api`](https://github.com/Danie1/threads-api)
- [InstaStoriesViewer public-profile scope](https://insta-stories-viewer.com/en/)

This specification follows `CONTEXT.md` and ADR 004 through ADR 013.
