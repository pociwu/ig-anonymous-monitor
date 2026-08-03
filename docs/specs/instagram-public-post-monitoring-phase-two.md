# Instagram Public Feed Post Monitoring — Phase Two Specification

Status: approved for implementation planning. This document specifies behavior; it does not enable authenticated post collection or authorize a production login.

## Problem Statement

The anonymous monitor can save media exposed by `insta-stories-viewer.com`, but it cannot reliably enumerate a public account's Instagram feed by stable Media PK, resume a long history scan, distinguish a carousel's ordered children, or reconcile a post that was edited or became unavailable.

Phase two adds a tightly limited authenticated, read-only feed-post path using the already isolated Instagram collector. It must preserve phase-one relationship safety, use the same collector session and global work envelope, avoid dedicated Reels or interaction endpoints, and keep authenticated listing separate from ordinary CDN downloads.

## Preconditions

Phase two cannot enter canary until all of the following are true:

1. Phase one Collector state is `active`.
2. Phase one has operated for at least 30 consecutive days without a Collector-fatal signal.
3. The production collector uses the persisted session and stable OCI IP already approved for phase one.
4. Existing anonymous monitoring, relationship work, media deduplication, Telegram delivery, Dashboard, backup, and Docker health checks are passing.
5. `instagram_posts.enabled` remains false until the operator explicitly starts the post canary from CLI.

A Collector risk hold suspends this qualification. After recovery, the Collector repeats its required phase-one observation and relationship canary, then phase two repeats its own seven-day post canary. The initial 30-day stability gate does not have to be accumulated again after that approved recovery path, but neither canary may be skipped or force-enabled.

## Goals

1. Record stable post identity, basic metadata, ordered carousel children, and downloaded best-quality media for formally monitored public accounts.
2. Give ordinary accounts a small, persistent recent-post baseline and a low-volume incremental/reconciliation path.
3. Give one designated account, initially `sin_9311`, resumable complete feed backfill when it changes from private to public.
4. Accept Reels only when naturally returned by normal feed enumeration.
5. Detect caption/carousel edits and cautiously represent possible unavailability without deleting evidence.
6. Coalesce count-change triggers and resume pagination without duplicate jobs or downloads.
7. Share one authenticated work budget, session, risk state, and worker with phase-one relationship collection.
8. Expose status and saved posts in the Dashboard while keeping all sensitive activation controls CLI-only.

## Non-Goals

- Private-account posts or media.
- Dedicated Reels, Stories, Highlights, tagged-post, Explore, search, recommendation, comment, liker, commenter, viewer, or follower-feed collection.
- `media_seen`, `clip_seen`, Story views, likes, follows, comments, messages, posting, or any Instagram write/interaction action.
- Historical like/comment analytics. Counts are saved only when already present in the feed response.
- Exact deletion time or proof that Instagram deleted a missing post.
- Full history for ordinary monitored accounts.
- Media collection for relationship members that have not been promoted into the formal monitored-account set.
- Concurrent authenticated clients, IP rotation, device regeneration, automatic relogin loops, or attempts to bypass risk controls.

## Fixed Product Rules

| Rule | Approved value |
|---|---:|
| Maximum formal monitored accounts | 16 |
| Eligibility | Confirmed public accounts only |
| Ordinary initial baseline | Persisted random 1–6 recent posts per account |
| Full-backfill targets | Exactly 0 or 1 system-wide; default `sin_9311` |
| Post batch size | At most 12 posts |
| Global authenticated starts | At most 6 per Asia/Taipei calendar day |
| Post starts | At most 2 per Asia/Taipei calendar day |
| Canary post starts | At most 1 per Asia/Taipei calendar day |
| Minimum interval between any authenticated starts | 4 hours |
| Ordinary reconciliation | Latest 6 posts every 30 days |
| Between post media downloads | Random 10–20 seconds |
| Between carousel child downloads | Random 2–5 seconds |
| Media retry in one batch | At most 2 attempts, random 30–90 seconds apart |
| Phase-two canary | `chaiyi_lili.cos`, 7 days |
| Disk safety floor | Pause below 5 GB or 10% free |
| Post change history | 365 days |
| Post run history | 90 days |
| Failed-attempt detail | 30 days |
| Telegram carousel attachments | First 10, then Dashboard link |

Safety limits are hard ceilings/floors. Configuration may reduce volume, lengthen waits, or require more free space; it may not raise volume, shorten waits, or lower the disk floors.

## Scope and Identity

Only enabled `Monitored Instagram account` rows are eligible. The 16-account limit remains authoritative.

The full-backfill qualification is configured by `full_post_backfill_on_reopen: true` on an account. Configuration validation rejects more than one enabled flag. Once the account has a Canonical Instagram Profile ID, the qualification is bound to that ID rather than its mutable username. A username change preserves the qualification; the same username resolving to a different Profile ID creates an Identity conflict and does not reconnect history or overwrite identity.

Removing an account cancels or cooperatively stops its pending/claimed post work while preserving posts, media, cursor, progress, and history. Re-adding the same Profile ID reconnects retained history only if the full-backfill flag is still present. Re-adding a different ID starts a new identity.

## Architecture

The existing four-service deployment remains:

```text
monitor
  anonymous profile/media inspection
  observes post-count and privacy transitions
  asks the post trigger module to coalesce work
  delivers persistent Telegram events

relationship-worker
  only service with collector credentials/session
  owns collector lifecycle and shared authenticated coordinator
  performs relationship jobs and authenticated feed listing
  writes post metadata/signed URL candidates and download jobs
  never downloads through Playwright

member-enrichment-worker
  unchanged profile-only anonymous relationship enrichment
  no collector credentials

dashboard
  reads saved post state and local media
  no collector credentials and no activation controls
```

The normal non-credentialed media path consumes post download candidates. It must not receive collector credentials, cookies, authorization headers, session/device data, or import `instagrapi`. Signed CDN URLs are short-lived transport data, not credentials to preserve indefinitely.

## Deep Modules and Interfaces

### Authenticated Work Coordinator

```python
class AuthenticatedWorkCoordinator:
    def claim_next(self, now: datetime) -> AuthenticatedWorkClaim | NoWork: ...
    def finish(self, claim: AuthenticatedWorkClaim, result: WorkResult) -> None: ...
```

It owns the global six/day ledger, four-hour spacing, single active claim, priority, leases, collector state, and fair continuation behavior across relationship and post jobs. Neither feature may claim work or count its budget independently.

### Post Trigger

```python
class PostTrigger:
    def observe_profile(
        self,
        account_id: int,
        previous: ProfileSnapshot | None,
        current: ProfileSnapshot,
        observed_at: datetime,
    ) -> TriggerOutcome: ...
```

It owns privacy and post-count transitions, baseline/reconciliation due checks, full-backfill qualification, job coalescing, priority widening, and no-op behavior while post monitoring is disabled/suspended.

### Post Worker

```python
class PostWorker:
    def run(self, claim: AuthenticatedWorkClaim, now: datetime) -> WorkOutcome: ...
```

It owns public eligibility revalidation, feed pagination, cursor handling, post/item persistence, edit/availability comparison, URL refresh requests, notification state, and sanitized failures. It does not own the long-running worker loop or binary downloading.

### External Port

```python
class InstagramPostSource(Protocol):
    def resolve_public_user(self, username: str) -> PostTarget: ...
    def user_medias_page(
        self,
        user_id: str,
        amount: int,
        end_cursor: str | None,
    ) -> PostPage: ...
```

The production adapter shares the same pinned `instagrapi` client boundary as relationship collection. It is the only code allowed to map `instagrapi` responses/exceptions. Tests use fake adapters, clock, random source, disk probe, and downloader.

## Phase-Two Lifecycle

Persistent states:

```text
disabled
  CLI starts eligible canary -> canary

canary
  chaiyi_lili.cos only
  seven days, at most one post job/day, random persisted 1–6 baseline
  success -> awaiting_approval
  collector-fatal -> suspended and Collector risk_hold

awaiting_approval
  CLI approval -> active
  collector-fatal -> suspended

active
  ordinary baselines/increments/reconciliation
  designated full backfill may run
  collector-fatal -> suspended

suspended
  no post listing
  recovery requires phase-one recovery and a new seven-day post canary
```

Canary success requires seven elapsed days, successful eligible post work, no Collector-fatal result, and a healthy final diagnostic under ordinary budget/spacing. Approval is CLI-only. `enabled: true` cannot bypass lifecycle state.

## Trigger and Queue Semantics

- Private or privacy-unknown accounts never start authenticated post work.
- Public to private/unknown cancels pending ordinary work and pauses full backfill after the current atomic unit, retaining cursor and progress.
- Private/unknown to public queues ordinary baseline/incremental work; the designated target additionally queues its first full-backfill batch at the highest post priority.
- An anonymous post-count increase queues incremental work.
- A count decrease records the observation but does not scan history solely to locate a deletion.
- Repeated triggers coalesce into one open post job per account. A higher-priority reason upgrades the job.
- An ordinary account with no baseline chooses one random integer from 1 through 6 once and persists it. Retries and future runs reuse that target size.
- Every 30 days an ordinary public account gets a low-priority latest-six reconciliation even when its count is unchanged.
- Pinned older posts may appear before new posts, so an incremental batch examines up to 12 posts and never stops merely because its first Media PK is known.
- More than 12 unseen posts persist continuation state; the next batch remains subject to all budgets and spacing.

## Priority

The shared coordinator considers work in this order while still preventing indefinite starvation:

1. Collector health/recovery canary work.
2. Designated account's first private-to-public backfill batch.
3. Reopened-account relationship refresh.
4. Relationship count-change work.
5. Genuine new-post work.
6. Designated account's continuation backfill.
7. Ordinary post baseline.
8. Thirty-day post reconciliation.

A continuation batch is lower than genuine new work. Aging may raise a pending job within its class, but cannot bypass Collector state, daily ceilings, or four-hour spacing.

## Shared Budgets, Leases, and Waiting

- A start consumes one global authenticated unit even if the job later fails.
- A post start consumes both one global unit and one post unit.
- During post canary, post starts are capped at one/day; otherwise two/day.
- Relationship work may use remaining global units, but the combined total never exceeds six/day.
- All budget days use `Asia/Taipei` and durable run rows, not process memory.
- The coordinator atomically checks Collector state, budget, four-hour spacing, and absence of another active claim.
- Only one authenticated job runs globally.
- Leases are renewed around each page and deliberate wait. Expired leases can return to pending only when Collector state permits.
- All waits are interruptible. Shutdown, target removal, privacy change, or risk hold stops at the next safe boundary.
- There is no tight or immediate retry.

## Full Backfill

The designated account is normally monitored anonymously while private. When the anonymous run first observes private to public, it durably queues the first full-backfill batch immediately. “Immediately” means queue creation in that observation transaction; actual authenticated start still obeys state, priority, budget, and four-hour spacing. Low disk pauses binary downloads, not safe metadata listing and cursor persistence.

Backfill runs newest to oldest in batches of no more than 12 posts until the source returns no next cursor. Each batch commits metadata, item order, dedup associations, download candidates, and the next cursor atomically. If the target becomes private, the job pauses without resetting the cursor. A later reopening resumes from saved progress after identity validation.

The system sends at most one daily backfill digest per Taipei day with scanned today, cumulative scanned, downloaded, duplicate, failed, pending, and pause status. Completion sends one immediate final digest. Historical baseline/backfill posts do not produce individual “new post” Telegram media messages.

## Pagination and Cursor Recovery

- Cursor values are opaque. Code never parses or manufactures them.
- The last durable cursor advances only after the batch transaction succeeds.
- A failed batch retains the prior cursor and deduplicates replay by Media PK/item identity.
- No next cursor means complete only when the page itself completed successfully.
- `WrongCursorError` does not mean completion. Restart at the first page and skip known Media PKs.
- At most one cursor reset is allowed per account in 24 hours.
- A second invalid cursor inside 24 hours pauses that account until the next day and sends one Chinese Telegram notification.
- Cursor reset never redownloads a canonical media binary.

## Post and Media Identity

- Canonical post identity is Instagram Media PK scoped to the owner Canonical Instagram Profile ID.
- Shortcode and original Instagram URL are aliases, not primary identity.
- A carousel is one post with ordered child identities/positions.
- The same Media PK from anonymous and authenticated sources becomes one post/source graph.
- When Media PK is unavailable from an anonymous source, the existing SHA-256 and perceptual-media deduplication may associate the binary without inventing a PK.
- A Reel naturally returned by feed listing is classified under Reels and may keep both feed and Reel source associations; it downloads once.
- Higher-quality media replaces the canonical quality selection without creating a new post.

## Saved Fields

Each post stores:

- owner Canonical Instagram Profile ID and current monitored account association;
- Media PK, shortcode, sanitized original URL;
- taken time, caption, media type, and product type;
- pinned flag when returned;
- ordered child items with item identity, type, dimensions/duration, and local canonical media association;
- like/comment counts only when present in the listing response;
- anonymous/authenticated source associations;
- first observed, last observed, last complete-scan observed;
- current, possibly unavailable, confirmed unavailable, or partial state;
- download, duplicate, failure, and URL-refresh status.

Raw Instagram responses are never stored.

## Edits and Availability

- The same Media PK remains the same post.
- Caption changes store before/after history and send at most one edit summary without resending media.
- Carousel child/order changes store a structural diff. Old files remain retained; missing new items are downloaded.
- Like/comment count changes update current values only and do not create history or notifications.
- One authoritative complete scan that misses a previously observed post changes it to `possibly_unavailable`.
- A later authoritative complete scan at least 24 hours later that still misses it changes it to `confirmed_unavailable`.
- Confirmation occurs only through a later naturally scheduled/authorized complete scan; no unbudgeted verification scan is created.
- Reappearance returns the post to current and records the observation time.
- Partial scans and ordinary 1–6/latest-six windows cannot infer absence outside their covered window.
- No state automatically deletes metadata or files, and `confirmed_unavailable` does not claim that Instagram proved deletion.

## Media Download and Quality

Authenticated listing writes metadata and expiring CDN candidates. The non-credentialed media path downloads sequentially:

1. select the highest-resolution image candidate;
2. rank video candidates by resolution, bitrate, then size;
3. wait a random 10–20 seconds between different posts;
4. wait a random 2–5 seconds between children of one carousel;
5. validate images with Pillow and videos with `ffprobe`;
6. compute SHA-256 and existing perceptual fingerprints;
7. retain only the best-quality canonical binary.

Empty, HTML, truncated, or invalid files never become complete. A carousel is `partial` until every required child is complete; retries target missing children only.

For one media item, attempt at most twice in a batch with an interruptible random 30–90 second delay. An expired/403 signed URL becomes `needs_url_refresh`; the next permitted authenticated post job may refresh it. CDN/download errors do not move the Collector to risk hold. After three post jobs still leave a post incomplete, send one failure summary and retain a retryable state.

Genuine new-post Telegram delivery waits until all media items are complete. For a carousel, attach at most the first 10 items and link to the Dashboard for the rest.

## Disk Guard

Before starting a new binary download, pause when either free space is below 5 GB or free percentage is below 10%. Metadata, cursor, and queued candidates remain durable. Send one low-space notification per transition, display the pause reason, and automatically resume downloads after both thresholds are healthy. Do not delete files automatically. Any future cleanup command must support dry-run before apply.

## Error Classification

### Collector-fatal

Challenge/checkpoint, `LoginRequired`, `BadPassword`, `TwoFactorRequired`, `FeedbackRequired`, `PleaseWaitFewMinutes`, HTTP 429/throttling, Sentry block, suspension, terms/consent block, or equivalent signals:

1. stop the current authenticated job;
2. transactionally move Collector to `risk_hold` and posts to `suspended`;
3. block all relationship and post starts;
4. enqueue one sanitized Chinese Telegram state notification;
5. require the complete phase-one recovery path followed by a new post canary.

### Transient listing failure

Timeout, DNS/connection error, incomplete response, decode failure, upstream 5xx, or one invalid cursor makes the current batch incomplete and retryable without advancing its cursor. It does not imply deletion or risk hold.

### Target ineligible

Private, not-found, unknown privacy, removal, or Identity conflict stops only that account. Identity conflict sends Telegram and never overwrites the saved Profile ID.

### Download failure

CDN 403/expiry, timeout, disk pause, invalid image/video, or checksum failure affects media state only and never becomes Collector-fatal.

## Telegram

All new messages are Traditional Chinese, durable, deduplicated, and free of collector identity/session/secrets.

Notify once for:

- phase-two lifecycle transitions;
- Collector risk hold affecting posts;
- Identity conflict;
- designated account opened and backfill queued;
- daily backfill summary and final completion;
- cursor paused after repeated invalidation;
- low disk pause/resume;
- post queue blocked for 24 hours;
- a genuine new post after its media completes;
- one edit summary;
- one incomplete-after-three-jobs summary.

Do not send individual historical baseline/backfill posts, likes/comments count changes, or repeated identical blocked-state alerts.

## Dashboard

Home cards display post state, last successful post run, queue/backfill progress, and pause/error reason. They do not expose a post enable/approve/full-target control.

Account detail adds a collapsed `貼文回補狀態` row near `社群趨勢`, showing scanned, downloaded, duplicate, failed, pending, cursor/complete status, and pause reason. Expanding it shows recent post runs and sanitized errors.

Saved media is grouped into:

- 貼文 / 照片
- 貼文 / 影片
- Reels / 影片

One post renders as one card with cover, taken time, caption excerpt, item count, source/state badge, and new/backfill status. Opening the card shows ordered carousel children, full saved caption, observation/edit history, and media status. Filters include new, backfill, partial, and possibly unavailable.

The Dashboard may show lifecycle state and suggest CLI commands. It cannot login, start/approve a canary, recover risk hold, change the full-backfill target, or expose signed CDN URLs/raw responses.

## Persistent Data Model

Schema migrations are additive and idempotent.

### Existing `accounts` additions

- `post_tracking` default true, effective only while the global feature is active;
- `full_post_backfill_on_reopen` default false, unique among configured accounts;
- `post_baseline_target` nullable persisted random 1–6;
- post status, last run/reconciliation, pause and identity-conflict metadata.

### `post_feature_state` singleton

Stores lifecycle state, phase-one stable-since evidence, canary account/start/end, approval, suspension reason/time, and timestamps. No credentials/session data.

### `authenticated_work_runs`

The authoritative shared budget/spacing ledger for both relationship and post work: work kind/reference, Taipei budget day, lease, start/end, outcome, and sanitized error. Existing relationship run history may be referenced/migrated, but claims must consult one authoritative coordinator view.

### `post_jobs`

Account, reason, priority, mode, pending/claimed/paused/completed/cancelled state, availability, lease, attempts, coalesced triggers, cursor/reset metadata, and timestamps. A partial unique constraint allows at most one open post job per account.

### `post_runs`

Job/account/reason/mode, budget reference, requested and observed counts, page/cursor outcome, complete/partial status, and sanitized error. Retained 90 days.

### `posts`

Owner ID, Media PK, shortcode, caption/current counts, type/product type, taken/pinned/source flags, availability, first/last/complete-scan observations, and timestamps. Unique by owner ID and Media PK.

### `post_items`

Post, stable child identity when available, ordered position, type, candidate metadata, canonical media reference, download state, and sanitized error. Unique by post and child identity/position according to source certainty.

### `post_change_history`

Post, change kind, sanitized before/after structured diff, observed time, and run. Caption/carousel/availability/reappearance history is retained 365 days.

### Existing media tables

Reuse canonical media, hashes, quality rank, source associations, download queue, and Telegram attachment state. Do not create duplicate binary ownership.

## Configuration

```yaml
accounts:
  - url: https://insta-stories-viewer.com/sin_9311/
    enabled: true
    label: sin_9311
    post_tracking: true
    full_post_backfill_on_reopen: true

instagram_posts:
  enabled: false
  baseline_min: 1
  baseline_max: 6
  batch_size: 12
  jobs_per_day: 2
  reconcile_days: 30
  min_free_gb: 5
  min_free_percent: 10
  canary_account: chaiyi_lili.cos
```

The per-account flag is the single canonical full-target setting; there is no duplicate `instagram_posts.full_backfill_account` field. Validation rejects multiple flags, unsafe values, a canary that is not a monitored public account, and a full target whose identity conflicts.

Missing `instagram_posts` behaves as disabled. Existing deployments make zero new authenticated post calls after upgrade.

## CLI and Administration

Add CLI operations equivalent to:

```text
--posts-status
--posts-canary chaiyi_lili.cos
--posts-approve
--posts-pause
--posts-full-backfill-account sin_9311
--posts-clear-full-backfill-account
```

Commands validate eligibility and lifecycle, return non-zero on invalid transitions, and print no credentials, raw session, signed URLs, or raw API response. Changing the full target updates the single canonical account flag only after resolving/cross-checking identity. CLI changes cannot bypass 30-day stability, canary, risk hold, budget, or disk guard.

## Retention, Files, and Backups

- Downloaded media and canonical post identity: retained until manual cleanup.
- Caption/carousel/availability history: 365 days.
- Post runs: 90 days.
- Detailed failed attempts: 30 days; final status remains.
- Signed CDN URLs: remove after successful download or expiry; do not retain query strings in long-term history/logs.
- Raw Instagram responses: never retained.
- Filenames use Media PK, child position, and extension; never caption text.
- New phase-two media uses `downloads/<account>/posts/<media-pk>/NN.ext` or `downloads/<account>/reels/<media-pk>/NN.ext`.
- Existing media is not forcibly moved. New source associations may point to existing canonical files.
- SQLite backups include metadata/progress but exclude `collector-secrets/` and media binaries.
- Collector session/device files remain separately mounted only into `relationship-worker`.

## Migration and Rollback

1. Ship schema/config/CLI with `instagram_posts.enabled: false`.
2. Back up SQLite before the additive migration.
3. Verify all existing anonymous and relationship behavior with zero post jobs.
4. Deploy the shared coordinator before enabling any post adapter call.
5. Run phase one for the required stable period.
6. Start the post canary from CLI, wait seven days, then approve from CLI.
7. Roll out ordinary baselines before enabling designated full backfill.

Rollback may run an older image that ignores additive tables, but it must preserve them and must never copy `collector-secrets/` into normal backups. Pausing phase two leaves phase-one relationship and anonymous monitoring operational unless the Collector itself is in risk hold.

## Testing Decisions

CI never contacts Instagram, Telegram, Apify, or the anonymous viewer.

### Configuration and lifecycle

- missing section is disabled and makes no calls;
- unsafe limits and multiple full targets are rejected;
- full-target username change preserves Profile-ID binding;
- different ID creates conflict and does not reconnect;
- phase-one stability below 30 days rejects canary;
- seven-day canary is single-account and one post job/day;
- approval/force paths cannot skip states;
- risk hold suspends posts and requires both recovery sequences.

### Coordinator and queue

- relationships plus posts never exceed six starts/day;
- posts never exceed two/day and all starts are four hours apart;
- budget/spacing/lease survive restart;
- one active authenticated claim globally;
- triggers coalesce and priorities widen without duplicates;
- first full-backfill batch outranks continuation;
- shutdown/privacy/removal interrupts waits and preserves progress.

### Pagination and identity

- ordinary baseline random value is selected once and persisted;
- a batch examines up to 12 even when the first post is pinned/known;
- more than 12 unseen posts continues later;
- cursor advances only with atomic batch success;
- wrong cursor resets once/24h and deduplicates known Media PKs;
- second wrong cursor pauses/notifies;
- natural Reel is accepted without calling dedicated Reel methods;
- carousel order and source merge are idempotent.

### Download, disk, and notification

- best-quality selection and canonical dedup;
- Pillow/ffprobe reject invalid payloads;
- carousel partial retries missing items only;
- 403 requests later URL refresh without risk hold;
- fake clock proves all random pacing ranges and interruptibility;
- either disk threshold pauses, both healthy resume;
- genuine new-post notification waits for completeness and truncates carousel at 10;
- baseline/backfill generates summaries rather than individual media alerts;
- notification dedup survives restart.

### Edit and availability

- caption and carousel changes create one history diff;
- like/comment changes create no history/notification;
- partial/windowed scans never infer deletion;
- two authoritative complete scans at least 24h apart move possible to confirmed unavailable;
- reappearance restores current without deleting history/files.

### Security and containers

- only `relationship-worker` receives collector environment/session mount;
- non-credentialed downloader and Dashboard cannot import/access collector material;
- logs, DB, Telegram, Dashboard, and backups omit secrets/raw responses/signed queries;
- Docker image supports pinned `instagrapi`, Pillow, ffmpeg, and ffprobe on Ubuntu ARM64.

## Acceptance Criteria

Phase two is ready for production rollout when:

1. Existing anonymous and phase-one tests remain green with the feature disabled.
2. Shared coordinator tests prove the combined budget, spacing, priority, and single-client rules.
3. Fake-source tests prove baseline, incremental, reconciliation, backfill, cursor recovery, edit, and availability behavior.
4. The Dashboard and Telegram expose no collector/session/raw API/signed-query data.
5. Downloads validate content, deduplicate, keep best quality, honor disk floors, and resume safely.
6. Migration succeeds against a copy of production SQLite and rollback preserves new tables.
7. The operator runbook covers status, canary, approval, pause, recovery, target selection, disk pause, and rollback.
8. `chaiyi_lili.cos` completes seven days with no Collector-fatal signal and receives explicit CLI approval.
9. Ordinary baselines complete under budget before `sin_9311` full backfill is allowed to start.

## Delivery Slices

1. Add disabled config validation, lifecycle/domain models, additive schema, and fake adapters.
2. Introduce the shared authenticated coordinator and migrate relationship claiming to it without behavior change.
3. Add post trigger/coalescing, ordinary persisted baseline, runs, and CLI state controls.
4. Add guarded feed adapter, cursor recovery, natural-Reel classification, and canary mode.
5. Add non-credentialed post download candidates, validation, dedup, quality, disk guard, and URL refresh.
6. Add full-backfill continuation, progress, daily/completion notifications, edits, and availability state.
7. Add Dashboard post cards, carousel detail, Reels grouping, filters, and collapsed progress panel.
8. Add Compose isolation checks, migrations, runbook, ARM64 build tests, and production canary checklist.

Every slice must keep the feature disabled by default and preserve all earlier tests.

## References

- [instagrapi media interfaces and paginated user media](https://subzeroid.github.io/instagrapi/usage-guide/media.html)
- [instagrapi exception taxonomy](https://subzeroid.github.io/instagrapi/exceptions.html)
- [instagrapi project and private-API stability notice](https://github.com/subzeroid/instagrapi)

This specification follows `CONTEXT.md`, ADR 003, ADR 005–013, ADR 014, and ADR 015.
