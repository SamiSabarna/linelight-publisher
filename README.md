# Line & Light publisher

Posts the «خط وضوء» (Arabic) and "Line & Light" (English) shorts to YouTube and Instagram on a schedule.
It runs on GitHub Actions, so the laptop can be off. It is separate from every other channel's tools.

- `queue.json` lists one row per video and channel, with its time in UTC and its captions.
- Videos wait as GitHub Releases (`v-<id>`) until they are posted.
- Instagram goes through Buffer. Buffer needs a public link, so the due videos are put on this repo's GitHub Pages for a short time.
- YouTube goes through the YouTube Data API.
- Run the workflow by hand with mode `check` to verify all connections without posting anything.

The secrets are stored in the repo settings and are never in the code. `site/` holds the public homepage and the privacy policy.
