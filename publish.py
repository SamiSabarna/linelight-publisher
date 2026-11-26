"""Line & Light / «خط وضوء» cloud publisher. Runs on GitHub Actions; completely separate from Yaqeen.

Each queue row is one video for one channel (lang "ar" or "en"). A run posts every due row (at most 2):
YouTube through the YouTube Data API (this project's own OAuth client), Instagram through Buffer.
Buffer needs the video at a public link, so the workflow first deploys the due videos to this repo's GitHub Pages.

State lives in queue.json. After each platform that succeeds, queue.json is committed and pushed at once, so a
re-run never posts the same video twice. Videos travel as GitHub Releases (tag v-<id>), added by the laptop
(wisdom/tools/queue_add.py) and deleted two days after posting.

    python publish.py --due      # print due=<ids> and pages=<true|false> for the workflow
    python publish.py --stage    # download the videos Buffer needs into _site/v/
    python publish.py --post     # post the due rows
    python publish.py --check    # verify secrets and connections; publishes nothing

Secrets: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN_AR, YT_REFRESH_TOKEN_EN (optional until the channel exists),
BUFFER_TOKEN. Set them with wisdom/tools/setup_secrets.py on the laptop.
"""
import json, os, shutil, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "queue.json"
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
REPO = os.environ.get("GITHUB_REPOSITORY") or CFG["repo"]
WORK = HERE / "_work"
SITE = HERE / "_site"
MAX_PER_RUN = 2
MAX_FAILS = 3
BUFFER_MUTATION = ("mutation CreatePost($input: CreatePostInput!) { createPost(input: $input) { "
                   "... on PostActionSuccess { post { id status } } ... on MutationError { message } } }")


# ------------------------------------------------------------------ helpers
def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def http(url, data=None, headers=None, method=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def now_utc():
    return datetime.now(timezone.utc)


def load():
    return json.loads(QUEUE.read_text(encoding="utf-8"))


def persist(q, msg):
    QUEUE.write_text(json.dumps(q, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    run(["git", "add", "queue.json"], cwd=HERE)
    if run(["git", "diff", "--cached", "--quiet"], cwd=HERE, check=False).returncode == 0:
        return
    run(["git", "commit", "-q", "-m", msg], cwd=HERE)
    for i in range(4):
        if run(["git", "push", "-q"], cwd=HERE, check=False).returncode == 0:
            return
        run(["git", "pull", "-q", "--rebase", "-X", "theirs"], cwd=HERE, check=False)
        time.sleep(3 + i * 3)
    raise RuntimeError("could not push queue.json")


def channel(r):
    return CFG["channels"][r["lang"]]


def missing(r):
    return [p for p in channel(r)["platforms"] if not r.get(f"{p}_id")]


def due_rows(q):
    t = now_utc().strftime("%Y-%m-%dT%H:%M")
    rows = [r for r in q if r["status"] == "pending" and r["publish_at_utc"] <= t and missing(r)]
    return sorted(rows, key=lambda r: r["publish_at_utc"])[:MAX_PER_RUN]


def video_url(vid):
    return f"{CFG['pages_base'].rstrip('/')}/v/{vid}.mp4"


def download(vid):
    WORK.mkdir(exist_ok=True)
    out = WORK / f"{vid}.mp4"
    if not out.exists():
        run(["gh", "release", "download", f"v-{vid}", "-R", REPO, "-p", "*.mp4", "-O", str(out), "--clobber"])
    return out


# ------------------------------------------------------------------ YouTube
def yt_token(secret_name):
    body = urllib.parse.urlencode({"client_id": os.environ["YT_CLIENT_ID"], "client_secret": os.environ["YT_CLIENT_SECRET"],
                                   "refresh_token": os.environ[secret_name], "grant_type": "refresh_token"}).encode()
    st, _, b = http("https://oauth2.googleapis.com/token", body, {"Content-Type": "application/x-www-form-urlencoded"})
    if st != 200:
        raise RuntimeError(f"YouTube token refresh failed ({st}): {b[:200]!r}")
    return json.loads(b)["access_token"]


def yt_upload(r, mp4):
    tok, y, lang = yt_token(channel(r)["yt_secret"]), r["meta"]["youtube"], r["lang"]
    body = {"snippet": {"title": y["title"][:100], "description": y.get("description", "")[:5000], "tags": y.get("tags", []),
                        "categoryId": "27", "defaultLanguage": lang, "defaultAudioLanguage": "zxx"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    size = mp4.stat().st_size
    st, h, b = http("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                    json.dumps(body, ensure_ascii=False).encode(),
                    {"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)}, "POST")
    if st != 200 or "location" not in h:
        raise RuntimeError(f"YouTube upload start failed ({st}): {b[:300]!r}")
    st, _, b = http(h["location"], mp4.read_bytes(), {"Authorization": f"Bearer {tok}", "Content-Type": "video/mp4"}, "PUT", 1200)
    if st not in (200, 201):
        raise RuntimeError(f"YouTube upload failed ({st}): {b[:300]!r}")
    return json.loads(b)["id"]


# ------------------------------------------------------------------ Buffer (Instagram)
def buffer(query, variables=None):
    st, _, b = http("https://api.buffer.com", json.dumps({"query": query, "variables": variables or {}}).encode(),
                    {"Authorization": f"Bearer {os.environ['BUFFER_TOKEN']}", "Content-Type": "application/json"}, "POST", 120)
    d = json.loads(b or b"{}")
    if st != 200 or d.get("errors"):
        raise RuntimeError(f"Buffer {st}: {str(d)[:300]}")
    return d["data"]


def buffer_post(r, plat):
    inp = {"channelId": channel(r)["buffer"][plat], "schedulingType": "automatic", "mode": "shareNow",
           "assets": [{"video": {"url": video_url(r["id"]), "metadata": {"thumbnailOffset": int(CFG.get("thumbnail_ms", 9000))}}}],
           "text": r["meta"]["instagram"]["caption"],
           "metadata": {"instagram": {"type": "reel", "shouldShareToFeed": True}}}
    out = buffer(BUFFER_MUTATION, {"input": inp})["createPost"]
    if not (out.get("post") or {}).get("id"):
        raise RuntimeError(out.get("message") or out)
    return "buffer:" + out["post"]["id"]


def wait_live(vid, size, limit=300):
    url, t0 = video_url(vid), time.time()
    while time.time() - t0 < limit:
        st, h, _ = http(url, method="HEAD", timeout=30)
        if st == 200 and int(h.get("content-length", "0")) == size:
            return url
        time.sleep(10)
    raise RuntimeError(f"{url} not live after {limit}s")


# ------------------------------------------------------------------ steps
def cmd_due():
    q = load()
    rows = due_rows(q)
    pages = any("instagram" in missing(r) for r in rows)
    print(f"due={','.join(r['id'] for r in rows)}")
    print(f"pages={'true' if pages else 'false'}")


def cmd_stage():
    """Build the Pages site: the static pages plus the videos Buffer may still be fetching."""
    q = load()
    if SITE.exists():
        shutil.rmtree(SITE)
    shutil.copytree(HERE / "site", SITE)
    (SITE / "v").mkdir(exist_ok=True)
    recent = (now_utc() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
    keep = [r for r in due_rows(q) if "instagram" in missing(r)]
    keep += [r for r in q if r.get("posted_at", "") >= recent and r not in keep]
    for r in keep:
        try:
            shutil.copyfile(download(r["id"]), SITE / "v" / f"{r['id']}.mp4")
            print("staged", r["id"])
        except Exception as e:
            print("could not stage", r["id"], e)


def cmd_post():
    q = load()
    for r in due_rows(q):
        vid = r["id"]
        fails = r.setdefault("fails", {})

        def failed(plat, err):
            fails[plat] = fails.get(plat, 0) + 1
            r["last_error"] = f"{plat}: {str(err)[:300]}"
            print(f"FAILED {vid} {plat} ({fails[plat]}): {err}")
            if fails[plat] >= MAX_FAILS:
                r["status"] = "failed"
            persist(q, f"fail {vid} {plat}")

        try:
            mp4 = download(vid)
        except Exception as e:
            failed("download", e)
            continue
        if "youtube" in missing(r):
            try:
                r["youtube_id"] = yt_upload(r, mp4)
                print("YouTube:", r["youtube_id"])
                persist(q, f"posted {vid} youtube")
            except Exception as e:
                failed("youtube", e)
        if "instagram" in missing(r) and r["status"] == "pending":
            try:
                wait_live(vid, mp4.stat().st_size)
                r["instagram_id"] = buffer_post(r, "instagram")
                print("Instagram:", r["instagram_id"])
                persist(q, f"posted {vid} instagram")
            except Exception as e:
                failed("instagram", e)
        if r["status"] == "pending" and not missing(r):
            r["status"], r["posted_at"] = "posted", now_utc().strftime("%Y-%m-%dT%H:%M")
            persist(q, f"done {vid}")
    # releases of videos posted more than two days ago are no longer needed
    old = (now_utc() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    for r in q:
        if r["status"] == "posted" and r.get("posted_at", "9") < old and not r.get("release_deleted"):
            run(["gh", "release", "delete", f"v-{r['id']}", "-R", REPO, "-y", "--cleanup-tag"], check=False)
            r["release_deleted"] = True
            persist(q, f"cleanup {r['id']}")


def cmd_check():
    ok = True
    try:
        d = buffer("query { account { organizations { id channels { id name service } } } }")
        have = {c["id"]: c for o in d["account"]["organizations"] for c in o["channels"]}
        for lang, ch in CFG["channels"].items():
            for plat, cid in ch["buffer"].items():
                c = have.get(cid)
                print(f"Buffer {lang}/{plat}: {'OK ' + c['name'] if c else 'MISSING ' + cid}")
                ok &= bool(c)
    except Exception as e:
        print("Buffer: FAILED", e); ok = False
    for lang, ch in CFG["channels"].items():
        if "youtube" not in ch["platforms"]:
            continue
        if not os.environ.get(ch["yt_secret"]):
            print(f"YouTube {lang}: no token yet ({ch['yt_secret']})"); ok = False; continue
        try:
            st, _, b = http("https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                            headers={"Authorization": f"Bearer {yt_token(ch['yt_secret'])}"})
            items = json.loads(b).get("items", []) if st == 200 else []
            print(f"YouTube {lang}: {'OK ' + items[0]['snippet']['title'] if items else f'FAILED ({st})'}")
            ok &= bool(items)
        except Exception as e:
            print(f"YouTube {lang}: FAILED", e); ok = False
    q = load()
    print(f"queue: {sum(r['status'] == 'pending' for r in q)} pending, {sum(r['status'] == 'posted' for r in q)} posted, "
          f"{sum(r['status'] == 'failed' for r in q)} failed")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    arg = (sys.argv[1:] or ["--check"])[0]
    {"--due": cmd_due, "--stage": cmd_stage, "--post": cmd_post, "--check": cmd_check}[arg]()
