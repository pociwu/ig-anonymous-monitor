# Anonymous source and gallery migration

Status: complete design confirmed by the operator; first implementation delivered, source validation gates remain unmet. Updated 2026-09-05. Production media recording/downloads remain disabled and require a separate post-validation approval.

## Confirmed requirements

- Replace the failing `insta-stories-viewer.com` anonymous source and adapt the local Dashboard for Posts, Stories, Highlights, and Reels. Mollygram was the initial preference; on 2026-09-05 the operator accepted another anonymous source if Mollygram cannot meet unattended operation.
- Preserve fully unattended scheduled monitoring. The operator explicitly confirmed on 2026-09-05 that production must not rely on a person completing Turnstile; see [ADR 017](../adr/017-require-unattended-anonymous-source.md).
- The operator accepted the proposed gallery structure: one card per post, ordered carousel media within the post, Highlights grouped by named album, and a separate Reels tab.
- The operator accepted the initial anonymous collection scope: latest 12 Posts, latest 12 Reels, all currently retrievable Stories, and all source-accessible Highlight albums and their contents in batches, followed by incremental updates.
- The operator accepted independent category results, preservation of existing data on failure, source-wide automatic backoff for challenges/rate limits, and deduplicated incident/recovery notifications.
- The operator accepted continuity of existing account/history/media records, a legacy-media area for items without reliable grouping evidence, and preservation of existing quarantine states.
- The operator accepted staged rollout: isolated Docker validation of AnonyIG first, production media recording/downloads disabled during implementation and validation, and separate approval before production enablement.
- The requested `grill-with-docs` interview resolved the planned product decisions, and the operator confirmed the complete shared design before implementation. Implementation approval does not authorize production media enablement.

## Observed source behavior

The following observations were made on 2026-09-04. They describe the observed website version, not a stable API contract.

- The [homepage](https://mollygram.com/) accepts a username or Instagram link. Public searches for `billieeilish` and `nasa` both displayed an interactive Cloudflare Turnstile challenge; neither challenge was completed by the investigation.
- The [public frontend script](https://mollygram.com/assets/js/my.js?32) renders or resets Turnstile for each new search. After verification, it requests `https://media.mollygram.com?url=<input>` with the verification token in `X-Api-Token`.
- An observed request without a token returned HTTP 200 with `{"msg":"Captcha verification failed.","status":"error"}`. HTTP success alone therefore does not establish a successful profile observation.
- The frontend expects a successful initial response containing HTML and a new token. It places the HTML in `#download`, retains the token in page memory, and uses it for subsequent category requests. The script handles `GetProfileInfo`, `AccountPrivate`, `GetMedia`, and `GetHighlights` source values.
- A profile response automatically triggers `method=allstories`. Highlight album contents use `method=highlights` with an album ID. The script replaces `#download_content` or `#download_content_highlight` with returned HTML. Exact Posts and Reels method values and their result fields still require verified response fixtures.
- A later `status=Refresh` response triggers a page reload. Token lifetime and cross-session reuse have not been established.
- The inspected script exposes no cursor or load-more protocol. The amount of available history is unverified; category names do not prove complete historical coverage.

The observations establish a dependency on verification tokens and a real interactive challenge in the tested browser. They do not establish that all environments always require human interaction, nor do they establish a repeatable unattended access path on the deployment host.

On 2026-09-05, a focused follow-up checked the official homepage, the [contact page](https://mollygram.com/contact), site-restricted searches for developer/API documentation, and the current public frontend script. No published Mollygram API-key program, machine-access contract, or unattended session-renewal mechanism was found. The script still used the same Turnstile and temporary-token flow. This is a finding about publicly available evidence, not proof that a private integration does not exist. No support message was sent.

## Pre-migration implementation facts (2026-09-04 baseline)

This section records the original implementation inspected during the interview, before the changes summarized below.

- `ig_monitor/scraper.py` actively visits only Posts and Stories, with Highlights inferred from captured payloads. `ScrapeResult` exposes only Posts and Stories terminal states.
- The current scraper raises an account-wide failure if either Posts or Stories remains unknown, even when another category succeeded. Explicit empty content is already distinguished from unknown. Challenge and rate-limit text is classified, but retries use a fixed two-second delay and the monitor continues to the next account without source-wide cooldown.
- Existing failures preserve prior snapshots and media. Telegram normally notifies once after three consecutive account failures and once on recovery; four-category and source-wide incident grouping are not yet implemented.
- `ig_monitor/dashboard.py` has three category tabs; its category normalizer currently maps unrecognized values, including `reels`, to Posts. Adding a fourth tab also requires updating counts and filtering.
- `media` and `media_sources` accept text categories and can store Reels. The current gallery is a flat list of media items; it does not preserve Highlight album presentation, post captions, or grouped carousel cards.
- `accounts.url` is unique and `Database.sync_accounts()` matches exact URLs. Simply replacing configured domains would create new account rows and disconnect the visible history. Account IDs, downloaded files, existing media associations, and renamed usernames need an explicit migration strategy.
- Media candidate selection currently collapses equal URLs before preserving category associations. One asset returned under both Posts and Reels could lose a category.
- The local configuration omits `schedule.media_download_enabled`, whose current default is false following the anonymous-source contamination incident. Currently this also prevents newly scraped media candidates from being persisted; profile snapshots, profile changes, and avatar downloads continue. The accepted rollout preserves this disabled state until post-validation approval.
- Relationship member enrichment also depends on the anonymous source, but its existing scope remains profile information and avatars only.
- The authenticated post-monitoring skeleton is separate from this anonymous migration; existing collector constraints do not constitute a Mollygram access mechanism.

Baseline validation on 2026-09-04: 87 tests passed in 8.34 seconds, with a clean working tree before and after execution.

## Alternative-source investigation (2026-09-05)

The operator accepted an alternative anonymous source. The first-party and live-browser findings are recorded in [anonymous website candidates](../research/anonymous-viewer-candidates.md), [additional website checks](../research/additional-viewer-live-checks.md), and [machine-access API candidates](../research/anonymous-machine-api-candidates.md).

AnonyIG is the recommended first candidate for an unattended deployment feasibility test. Its [entry page](https://anonyig.com/en/) redirected to [the tested page](https://anonyig.com/en2/). A public NASA lookup returned nonempty Posts, Stories, Highlights (including the Roman album's contents), and Reels without an Instagram login or an interactive CAPTCHA. A second lookup in a new tab in the same browser session also succeeded; this was not an independent clean-cookie test. A normal full-page advertisement appeared and was closed through the website interface.

That initial browser observation did not establish production suitability. The public frontend contains conditional CAPTCHA handling for 422/429 responses. The subsequent implementation and validation described below established two fresh local headless runs, observed author/media/group fields, real Posts pagination, and one verified in-memory image download. Actual Docker execution, long-duration unattended reliability, independent Reels completeness, and continuation beyond the bounded feed window remain unverified. A challenge or failed category request must not be mistaken for a successful empty collection.

HikerAPI has published contracts covering all four categories but needs a provider API key and billed usage. It is a documented contingency, not an approved purchase or the selected source.

## Confirmed gallery structure

The operator answered 「採用」 to the proposed presentation:

- Posts use one card per logical post. A carousel's child media remain within that post and preserve source order; they are not separate post cards.
- Highlights retain their named album grouping, with the album distinct from its contained photos or videos. The term is recorded as **Highlight album** in `CONTEXT.md`.
- Reels have a separate tab alongside Posts, Stories, and Highlights.

Media deduplication and content membership are distinct: one canonical media item may be referenced by multiple categories, posts, or Highlight albums. Preserve each membership and its position without creating redundant canonical files or duplicate records within the same membership. A Posts/Reels overlap must not remove either category; a reused image must not shorten a carousel or erase its album membership.

These presentation requirements are implemented against observed parent-post IDs, ordered carousel children, and named album response fixtures; the evidence does not establish complete source coverage. Legacy flat media follow the confirmed continuity policy below; equal captions or timestamps alone do not establish a shared parent post.

## Confirmed initial anonymous collection scope

The operator answered 「採用」 to the following per-account initial scope:

- Posts: latest 12 logical posts. Count a carousel as one post and retain all its child media in source order.
- Reels: latest 12 Reels.
- Stories: all content currently retrievable from the source.
- Highlights: all source-accessible albums and their contents, completed in batches.
- After establishing the initial collection, continue with incremental updates.

This is the **Anonymous collection baseline**, distinct from the authenticated **Post baseline** and `instagram_posts` settings. It does not promise recovery of expired Stories or history the source does not expose. A batch or page failure does not establish that the requested scope has been fully collected. The confirmed scope does not itself enable production downloads.

## Confirmed anonymous failure policy

The operator answered 「採用」 to this policy:

- Preserve successful categories and all existing data. Record failed or incomplete categories separately and show their last successful update; do not treat them as empty or as proof of deletion.
- A verification challenge or rate limit stops further requests to that anonymous source across accounts. Automatic retry delays increase through 30 minutes, 1 hour, 2 hours, and then a 4-hour cap for continuing failures. Do not wait for manual verification.
- Send one immediate Telegram notification when the source is blocked. Ordinary failures notify after three consecutive failures. Notify once on recovery and suppress repeated unchanged incident notifications.

These concepts are recorded as **Incomplete anonymous collection** and **Anonymous source cooldown** in `CONTEXT.md`. A cooldown skip is not a new failed attempt. A category-specific failure does not discard successful results from other categories. The authenticated collector's existing risk-hold and manual-recovery policy remains unchanged.

## Confirmed existing-data continuity

The operator answered 「採用」 to preserving existing data without guessing groupings:

- Retain the existing account identities, resolved username changes, history, relationships, downloaded files, and media-deduplication records when changing the source. A changed provider URL must not create a fresh account history.
- Legacy media with reliable parent-post or Highlight album evidence may join the structured gallery. Keep other items accessible in a clearly labeled 「舊版媒體」 area, retaining existing categories and files without guessing groupings. This is **Ungrouped legacy media** in `CONTEXT.md`.
- Quarantined media keep their quarantine state and remain outside the ordinary gallery. Switching sources does not automatically restore or delete them.

The existing database stores optional `media.logical_id` and a `position` default of zero; `media_sources` only records media/category associations. These fields do not guarantee recovery of all original parent-post or album memberships. The migration must preserve the original records and their evidence rather than manufacture missing relationships.

## Confirmed staged rollout

The operator answered 「採用」 to the staged enablement process:

- Validate AnonyIG first in the target Docker environment using a separate test database and media directory. Check all four categories, content ownership, carousel/album structure, pagination, actual downloads, and repeated unattended operation.
- Keep production media recording/downloads disabled during implementation and validation.
- Present the validation results and obtain separate operator approval before enabling production media recording/downloads. If source validation fails, leave them disabled and report the unmet criteria.

This is **Anonymous source validation** in `CONTEXT.md`; passing validation is not production enablement. Validation must not write to the production media library, restore quarantined media, or purchase a paid source. AnonyIG is a candidate for this validation, not a verified production replacement.

Validation isolation requirements: the current Compose services share bind-mounted `data` and `downloads`, so changing the Compose project name alone does not isolate them. Use independent configuration, data, downloads, diagnostics, and a separate Dashboard port; enable media recording/downloads only in that test configuration. Do not start relationship or member-enrichment workers, mount collector secrets, or enable Telegram, Apify, or authenticated extensions during the isolated source test. These checks must not trigger unrelated production work.

## Implementation verification checklist

These checks verify the confirmed design; they do not add collection scope or authorize production activation:

- Source evidence: capture reproducible results in the intended Docker/browser environment, including clean-session behavior, four categories, reliable account/media/group identities, child order, at least one real pagination step, and successful test-media downloads. Record the actual duration and outcomes of repeated unattended checks; do not generalize a desktop success into a long-term reliability guarantee.
- Category handling: test successful empty results separately from partial, unknown, failed, and blocked results. Verify preservation of old data, independent successful-category updates, source-wide cooldown and its persistence, and deduplicated failure/recovery notifications without sending real test notifications.
- Identity and migration: verify existing account IDs, resolved usernames, history, files, source associations, and quarantine states remain intact. Verify rerunning the migration does not create duplicate accounts or group memberships. Unknown legacy grouping must remain unknown.
- Gallery and collection: verify four tabs, counts and filters, one card per post, complete ordered carousels, named Highlight albums, legacy-media access, and shared canonical media with all memberships intact. Test the accepted initial scope and incremental updates without treating unfinished batches as complete.
- Isolation and release: verify the test uses no production mounts or collector credentials, production media recording/downloads stay disabled, and the test report lists unresolved checks. Existing authenticated collection behavior remains unchanged.

## Implementation authorization

The operator answered 「確認」 to the complete shared design and authorized implementation. Source feasibility is still an evidence-based validation gate; a failed gate requires reporting the limitation, not silently enabling production or purchasing another source. Production media enablement remains a separate later approval even after the implementation is complete.

## Delivered implementation and remaining gates (2026-09-05)

- AnonyIG adapter uses ordinary browser search, tab selection, and pagination. It validates profile identity and per-item ownership evidence, retains ordered carousel/album memberships, and reports each category independently. It does not complete CAPTCHA challenges or replicate request signing.
- Additive SQLite schema and transactional account matching preserve existing IDs, resolved usernames, history, canonical media, memberships, and quarantine. Unknown grouping remains in the legacy gallery. Shared media files retain distinct category/post/album memberships.
- The monitor and profile-enrichment path honor persisted source-wide cooldowns. Category failures and source incidents have separate deduplicated notifications; tests do not send real notifications. Disabled recording does not advance stored media baselines or checkpoints.
- Dashboard now has Posts, Stories, Highlights, Reels, and legacy access, with carousel navigation, named album groups, per-category status, and Taipei-time presentation. Desktop/mobile browser interaction tests use synthetic local fixtures, not production or live source data.
- The standalone validation Compose file has an independent state volume, test-only configuration, and loopback port 8889. It mounts no production data or collector secrets and enables no Telegram, Apify, or authenticated workers. It has not been executed because this development host has no Docker CLI.
- Two fresh local headless NASA probes and one validated in-memory JPEG download are documented in [adapter validation](../research/anonyig-adapter-validation.md). They are not Docker or long-duration validation. Reels is an observed Posts-video filter, some Posts lack matching owner evidence, and full cross-run history continuation remains unverified. These limitations keep the production gate closed.

See [the validation guide](../anonyig-migration-validation.md) for the delivered test evidence and isolated Docker commands. No production config, database, downloaded library, or running service was changed or started as part of this validation.
