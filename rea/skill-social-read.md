# Skill: read and summarise a link for free (REA team)

Reference-only. **v2, 2026-10-18 (REA-RECONSTRUCTOR).** Supersedes v1 (artifact 8db19302) and the
earlier note `Agent-Reach skill for the REA team` (df432017). Two things changed in v2: a stale
YouTube route was corrected, and a real defect the reader had is now a rule with a regression test
behind it.

Module: `listen_app/social_read.py`. Tests: `tests/test_social_read.py`.
Probes: `readthrough/social_read_probe.py` (route truth), `readthrough/social_window_probe.py`
(the real Tk window end to end), `readthrough/round17/r17_stress.py` (guard and load stress).

## Step 0 — classify the URL before fetching anything
`classify(url)` returns exactly one of: `web`, `youtube`, `github`, `v2ex`, `rss`, `login-only`.
Login-only is decided first, by hostname, so a Reddit or X link can never fall through to a
route that would try to fetch it.

Login-only hosts: reddit.com, x.com, twitter.com, bilibili.com, instagram.com, facebook.com,
xiaohongshu.com, linkedin.com. A login-only URL returns `needs-approval` with **zero** HTTP
requests. It never reads, stores or sends a cookie, token or account header.

## Step 1 — the first route that returns real content
| kind | route order |
|---|---|
| web | direct GET + trafilatura, then `https://r.jina.ai/<url>` |
| youtube | oEmbed title and author, the watch page's `shortDescription`, then subtitles via **youtube-transcript-api 1.2.4** (MIT, pure-Python, no key, no cookie) |
| github | REST API repo metadata, then README (`application/vnd.github.raw`) or issue/PR body |
| v2ex | `/api/topics/show.json?id=<n>` for a topic, `/api/topics/hot.json` for the front page |
| rss | standard-library XML parse; no extra package |

**Correction to v1:** v1 said "yt-dlp subtitles if installed". That is wrong. yt-dlp is not a
dependency and is not used; round 15 replaced that branch with `youtube-transcript-api`. The
dependency is pinned in `pyproject.toml`. Subtitles are still best-effort: YouTube rate-limits
and IP-blocks this endpoint, and a 15-minute cooldown starts after an `IpBlocked`.

No hosted service, no paid API, no key. Jina Reader is called without a token.

## Step 2 — five honest states, and only these
| state | meaning |
|---|---|
| `ok` | real content with the fields a summary needs |
| `blocked` | HTTP 401/403/412/429/451, or Jina's own `AbuseAlleviationError` JSON |
| `login-wall` | HTTP 200 whose body is a sign-in page, not the post |
| `needs-approval` | login-only platform; nothing was fetched |
| `empty` | the route answered but carried no usable text (a title with no body is a stub, not a post) |

Rules that the tests enforce:
- A non-`ok` state always carries **empty text**. `read_text()` raises `ExtractError` instead of
  returning something to summarise. A blocked or walled page is never summarised as if it were the post.
- A long real page that merely mentions "log in" is not a wall; the wall test runs on the
  link-stripped visible text and only under 500 characters.

## Step 3 — a missing page is not content (the rule that was broken)
A page that does not exist is `empty`, never `ok`. This was a real defect, found by
REA-RECONSTRUCTOR on 2026-10-18 and fixed with a regression test that fails on the old code.
Three seams, because three ways exist for a 404 body to reach the summariser as if it were an article:

1. **Status.** HTTP **404 and 410** are `empty` with a note naming the status. They are *not*
   `blocked` — "blocked" means the site refused us, which is a different message to the user.
   (Wikipedia answers a missing article with HTTP 404 plus a "does not have an article with this
   exact name" page, which trafilatura happily extracts as 1118 chars of real text.)
2. **A soft 404.** Some sites answer a missing page with HTTP 200 and a "page not found" body.
   So a short extracted body is checked against `NOT_FOUND_MARKERS` — **only** when the
   link-stripped visible text is under 600 characters. A long article that *discusses* 404s is
   never refused: measured on real MDN and Wikipedia 404 pages at 1199 to 12379 chars, all `ok`.
3. **Jina's own warning.** Jina Reader returns HTTP 200 and hides the target's error in its
   header block: `Warning: Target URL returned error 404: Not Found`. That header is parsed and
   the result is `empty`.

## Step 4 — summarise only `ok` text
Hand `result.text` to the app's own summariser (`listen_app.summarize.key_points`). Cite the URL:
the window appends `- Source: <url>` from the URL whose text actually landed in the box.

## Known anonymous blocks (2026-10-18, this Mac)
- reddit.com: 403 direct and via Jina.
- x.com: 403 via Jina, `AbuseAlleviationError`.
- bilibili: 412 risk control.
- instagram via Jina: HTTP 200 whose body is the sign-in page — a false success, correctly graded `login-wall`.
- YouTube transcript endpoint: `IpBlocked` after repeated use on one IP. The description still
  reads, so summaries survive; treat transcripts as best-effort only.
These are platform limits, not bugs in our code.

## Verified vs assumed
Verified by live probe: Jina Reader on Wikipedia; oEmbed; the YouTube watch-page description;
GitHub repo, README, issue and a missing repo; V2EX topic and hot list; HN RSS; all eight
login-only hosts returning `needs-approval` with **zero** transport calls (proven with a counting
transport, not asserted); malformed links refused with zero calls; the missing-page rule at all
three seams; 42 concurrent reads with no state change and no non-`ok` state carrying text.
Assumed, not verified: YouTube subtitles on the live network (blocked here); Mastodon and Lemmy;
long-run rate limits beyond the bursts measured; Windows and Linux (deferred by the Commander).
