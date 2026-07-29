## Problem Statement

巡檢帳號的 Instagram username 可以變更。現有巡檢網址依賴 username，帳號改名後可能無法再取得同一帳號的資料；同時，使用外部 Apify 查詢服務時，必須保存 API token 的機密性，並確保月成本不超過 5 美元。

## Solution

系統對每個啟用帳號保存 Instagram Profile ID，並固定使用官方 Apify Actor `apify/instagram-profile-scraper`。首次建立帳號資料時取得 Profile ID；正常巡檢不呼叫 Apify。只有原監控網站無法定位帳號或名稱不一致時，才以保存的 ID 反查目前 username。

確認更名後，系統將資料庫中的 Effective monitoring URL 更新為新網址並發送 Telegram 通知。Apify token 僅由部署主機 `.env` 的 `APIFY_API_TOKEN` 載入。程式與 Apify 帳戶均設置 5 美元上限；達限後停止新查詢，並在一個 Apify monthly usage cycle 中只通知一次。

## User Stories

1. As a monitor operator, I want every enabled account to receive a saved Instagram Profile ID, so that a username change does not lose the account identity.
2. As a monitor operator, I want the initial identity lookup to run for all enabled accounts, so that existing accounts are enrolled without manual database edits.
3. As a monitor operator, I want normal successful inspections to avoid Apify calls, so that routine monitoring does not consume paid quota.
4. As a monitor operator, I want a failed account lookup to use the saved Profile ID to resolve the current username, so that monitoring can recover from a rename.
5. As a monitor operator, I want the Effective monitoring URL to update automatically after a verified rename, so that later schedules use the current account URL.
6. As a monitor operator, I want Telegram to show the old and new usernames after a verified rename, so that I can audit the automatic update.
7. As a monitor operator, I want `config.yaml` to remain my initial account list, so that the service does not rewrite my source configuration.
8. As a monitor operator, I want the database Effective monitoring URL to take precedence over the initial URL, so that a rename persists over restarts.
9. As a monitor operator, I want to put the Apify API token in `.env`, so that it is not committed, logged, stored in SQLite, or sent to Telegram.
10. As a monitor operator, I want Apify settings to be visible in non-secret configuration, so that I can enable or disable identity resolution without editing code.
11. As a monitor operator, I want the application to reject a configured monthly cap above 5 USD, so that a configuration mistake cannot raise the maximum spend.
12. As a monitor operator, I want the application to use Apify's monthly usage cycle, so that local cost decisions match the upstream billing period.
13. As a monitor operator, I want each paid request recorded in a local usage ledger, so that the application can stop before its allowed budget is exhausted.
14. As a monitor operator, I want the application to stop identity resolution when its 5 USD guard is exhausted, so that it never intentionally starts another paid lookup after the cap.
15. As a monitor operator, I want only one budget-exhausted Telegram alert per monthly usage cycle, so that scheduled inspections do not create alert noise.
16. As a monitor operator, I want identity resolution to recover automatically in the next usage cycle, so that no manual reset is required after a budget pause.
17. As a monitor operator, I want a missing or invalid Apify token to leave the existing inspection result intact and produce a diagnosable failure, so that it cannot corrupt monitoring state.
18. As a monitor operator, I want existing private-account and public-media monitoring behavior to remain unchanged, so that identity resolution is additive.

## Implementation Decisions

- Introduce an Apify configuration section containing an enable switch, the fixed official Actor identifier, a maximum monthly budget of 5 USD, and a conservative per-request reservation. The budget value may be lower but never higher than 5 USD.
- Load `APIFY_API_TOKEN` from `.env` only. Its absence disables paid identity operations with an explicit operational error; it must not be persisted.
- Add an Apify client boundary that obtains account limits, starts the configured actor with username or Profile ID input, waits for completion, reads one dataset item, and returns a small identity result containing Profile ID and current username.
- Persist the Profile ID, Effective monitoring URL, and identity-resolution metadata with each account. Keep `config.yaml` URLs as initial input; synchronization must not overwrite a resolved effective URL.
- Persist usage records by Apify usage-cycle identifier and request. The monitor checks both the local reservation ledger and the remote monthly limit before initiating the actor.
- Treat a first successful identity result as enrolment. Treat an identity result with a different current username as a Username change, update the Effective monitoring URL atomically, and enqueue one change notification with old and new names.
- Attempt identity resolution only after the existing profile scraper has exhausted its normal retry behavior. Successful normal scraping continues to use the Effective monitoring URL and avoids Apify.
- Enqueue one Budget-exhausted notification per usage cycle when the guard refuses a lookup. Persist the notification marker so repeated schedules remain quiet.
- Preserve all existing snapshots, media download, private/public state, retry, heartbeat, backup, and Telegram delivery behavior.

## Testing Decisions

- Tests assert externally observable outcomes: accepted/rejected configuration, persisted account state, queued events, selected lookup calls, and scheduled monitoring results. They do not assert SQL statement text or private helper implementation.
- Configuration tests cover valid Apify defaults, disabled operation without token, enabled operation requiring a token, and rejection of a monthly cap above 5 USD.
- Database tests cover Profile ID persistence, Effective monitoring URL precedence across config synchronization, cycle-scoped usage entries, and duplicate budget-alert suppression.
- Monitor-flow tests use a fake Apify client and existing scraper seams. They cover initial enrolment, no paid lookup after a normal scrape, lookup after scraper failure, verified rename update/notification, no update from an unresolved result, and budget exhaustion.
- The existing configuration and database unit-test style is prior art; the monitoring seam is extended through injected collaborators rather than live browser or network calls.

## Out of Scope

- Replacing the existing anonymous viewer scraper with Apify for ordinary monitoring or media download.
- Uploading media to Apify, scraping followers/posts beyond the identity result, or changing Telegram media behavior.
- Managing or purchasing the user's Apify account, generating its API token, or changing unrelated account-wide billing policies beyond documenting the required 5 USD setting.
- Retrospective recovery of identities for deleted or inaccessible Instagram accounts when Apify cannot return a profile.

## Further Notes

- The implementation follows ADR 001 and the domain terms in `CONTEXT.md`.
- The official Actor pricing and outputs may evolve; the local reservation is deliberately configurable and conservative, while the upstream account limit remains the second enforcement layer.
- This directory has no usable Git remote or connected issue tracker, so this file is the published project specification in lieu of an issue with a `ready-for-agent` label.
