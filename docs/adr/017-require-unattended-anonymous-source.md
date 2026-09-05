# Require unattended operation when replacing the anonymous source

Accepted on 2026-09-05. The operator confirmed that the existing scheduled anonymous monitor must remain fully unattended and must not depend on a person completing Turnstile. Source selection and session recovery must satisfy this requirement; a manually verified browser session is not an acceptable production dependency, even if it exposes more content categories.

Mollygram is the initially requested replacement, with Posts, Stories, Highlights, and Reels in scope. On 2026-09-04, two interactive profile searches required Turnstile, and its media endpoint returned a CAPTCHA error without a token. These observations leave unattended feasibility unresolved; they do not establish that every browser or deployment must receive an interactive challenge. Before selecting Mollygram for production, establish a repeatable unattended access path in the deployment environment, including session renewal.

On 2026-09-05, the operator accepted choosing another anonymous source if Mollygram cannot satisfy unattended operation. Stable automated access and coverage of all four content categories take priority over the requested provider's name. This decision does not select a replacement or authorize a paid subscription.

Evidence: [Mollygram frontend request script](https://mollygram.com/assets/js/my.js?32) and [migration investigation](../specs/mollygram-anonymous-source-migration.md).
