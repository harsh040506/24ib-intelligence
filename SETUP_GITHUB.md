# Putting this online, for free, forever — a complete beginner's guide

This guide takes you from "I have a folder on my laptop" to "there is a website
on the internet that updates itself every Monday morning and costs nothing."

It assumes you have **never used GitHub before**. Every click is spelled out.
Set aside about 45 minutes for the one-time setup. After that you never touch it
again.

---

## Part 0 — What you are actually building

Right now, your reports live on your laptop. You run `python run.py`, click
buttons, and a folder called `24IB-Private-Market-Research` fills up with
finished web pages.

We are going to move that job to a computer that GitHub owns and runs for free.

Here is the whole arrangement in plain English:

> **GitHub** will keep a copy of your project (that copy is called a
> *repository*, or *repo*). Once a week, at a time you choose, GitHub will start
> up a temporary computer, download your project onto it, run your scraper and
> report builder, save the results back into your repository, and publish the
> finished pages to a public web address. Then it throws the temporary computer
> away. Total cost: nothing.

Three pieces make that work, and all three are already written for you:

| File | What it does |
|---|---|
| `.github/workflows/weekly-update.yml` | The instructions GitHub follows. Contains the schedule. |
| `scripts/weekly_update.py` | The actual work: scrape, build reports, publish pages. |
| `instance/intelligence.db` | Your deal history. Lives in the repo so each week's run remembers last week. |

### Why it is free

GitHub charges for compute time on **private** repositories, but gives it away
**unlimited on public ones**. GitHub Pages (the web hosting) is likewise free for
public repositories. So the plan is a public repository — which is fine, because
the site you are publishing is a public research archive anyway.

**Be aware of what "public" means:** anyone can read your code, your deal
database, and your reports. That is already the intent for the published site.
But do not put anything secret in this folder — no API keys, no client lists,
no `.env` file. (A `.gitignore` file has been added that blocks `.env`
automatically, so you would have to work hard to leak it by accident.)

If you genuinely need this private, see **Part 9**.

---

## Part 1 — Create your GitHub account

1. Open <https://github.com> in your browser.
2. Click **Sign up** (top right).
3. Enter your email, pick a password, pick a username.
   - Your username becomes part of your website address, so choose something
     you are happy to show people. If you pick `harshchhajer`, your site will
     live at `https://harshchhajer.github.io/...`.
4. Verify your email address when GitHub sends you the code.
5. When it asks which plan you want, choose **Free**.

You now have a GitHub account. Nothing is uploaded yet.

---

## Part 2 — Install GitHub Desktop

There are two ways to move files to GitHub: a visual app, or typed commands.
Since you are new to this, use the app. It is far harder to make a mistake.

1. Go to <https://desktop.github.com>.
2. Click **Download for Windows**. Run the installer.
3. Open GitHub Desktop when it finishes.
4. Click **Sign in to GitHub.com** and log in with the account from Part 1.
5. When it asks for your name and email for commits, accept the defaults.

> **Vocabulary you will see:** a *commit* is a saved snapshot of your files with
> a short note attached. *Push* means upload your commits to GitHub. *Pull* means
> download changes from GitHub to your laptop. That is genuinely all you need.

---

## Part 3 — Turn your folder into a repository

1. In GitHub Desktop, click **File → Add local repository**.
2. Click **Choose…** and select your folder:
   `C:\Users\Harsh\Downloads\72IB-Weekly-Intelligence`
3. GitHub Desktop will say *"This directory does not appear to be a Git
   repository. Would you like to create a repository here?"* — click the blue
   **create a repository** link in that message.
4. A form appears. Fill it in:
   - **Name:** `24ib-intelligence` (this becomes part of your web address — use
     lowercase letters and hyphens, no spaces)
   - **Description:** `Weekly Indian private market intelligence`
   - **Git ignore:** leave as **None** — your project already has a `.gitignore`
     file, and this dropdown would overwrite it.
   - **License:** None (unless you want one)
5. Click **Create repository**.

You will now see a long list of files in the left panel. That is everything in
your project, waiting to be saved.

### Sanity check before you upload

Look down the file list and confirm you can see:

- `instance/intelligence.db` — **must be there.** This is your 1,660 deals. If
  it is missing, the automation will have no history to build on.
- `instance/Inc42_Funding_Master_Data.xlsx` — **must be there.**
- `.github/workflows/weekly-update.yml` — **must be there.** This is the
  schedule. (If GitHub Desktop hides it, that is only the display; it will still
  be uploaded.)
- `.env` — **must NOT be there.** If you see it, stop and tell someone; the
  `.gitignore` is not working.

### Upload it

1. At the bottom left, in the **Summary** box, type: `Initial commit`
2. Click **Commit to main**.
3. At the top, click **Publish repository**.
4. In the dialog:
   - **Keep this code private** — **UNTICK THIS BOX.** This is the single most
     important click in the whole guide. Leaving it ticked makes the repository
     private, which means Pages will not work on a free account and your Actions
     minutes become metered.
   - Leave the name as `24ib-intelligence`.
5. Click **Publish repository**.

Uploading takes a minute or two — there is a 2 MB database and about 5 MB of
report pages to send.

When it finishes, click **Repository → View on GitHub** to see it in your
browser. Your files are now on the internet.

---

## Part 4 — Give the robot permission to save its work

By default, GitHub's automation is only allowed to *read* your repository. Our
weekly job needs to *write* — it saves the newly scraped deals back. You have to
grant that once.

1. On your repository page, click the **Settings** tab (top right, with a gear).
2. In the left sidebar, scroll down and click **Actions**, then **General**.
3. Scroll to the very bottom, to **Workflow permissions**.
4. Select **Read and write permissions**.
5. Click **Save**.

> **If you skip this step**, the weekly job will run, scrape correctly, build the
> reports — and then fail at the last moment with a red error saying
> `Permission to ... denied` or `403`. If you ever see that, this is the fix.

---

## Part 5 — Switch on the website

1. Still in **Settings**, click **Pages** in the left sidebar.
2. Under **Build and deployment**, find the **Source** dropdown.
3. Change it from *Deploy from a branch* to **GitHub Actions**.

That is it — there is no Save button on this screen; it applies immediately.

> **Why this setting matters:** the default option ("Deploy from a branch") can
> only publish from the top level of your repo or a folder called `docs`. Your
> site lives in `24IB-Private-Market-Research/`, which is neither. Choosing
> "GitHub Actions" lets our workflow hand the folder over directly, so nothing
> has to be renamed or moved.

---

## Part 6 — Run it once by hand

Do not wait until Monday to find out whether it works.

1. Click the **Actions** tab at the top of your repository.
2. If you see a banner saying *"Workflows aren't being run on this forked
   repository"* or asking you to enable Actions, click the green button to
   enable them.
3. In the left sidebar, click **Weekly intelligence update**.
4. On the right, click the **Run workflow** dropdown button, then the green
   **Run workflow** button inside it.
5. Wait about 5 seconds and refresh the page. A new run appears with a yellow
   spinning dot.

Click into the run to watch it live. It takes roughly **3 to 6 minutes**, most
of which is installing the Chromium browser the scraper needs.

### Reading the result

- **Green tick ✓** — it worked. Go to Part 7.
- **Yellow dot** — still running. Wait.
- **Red cross ✗** — something failed. Click the run, click the failed step, and
  read the last 20 lines. Jump to the troubleshooting table in Part 10.

While it runs you can click **Scrape Inc42, rebuild reports and publish the
site** to watch the live log. You will see it count the deals in your database,
report what it scraped, and list which reports it rebuilt.

---

## Part 7 — Find your live website

1. Go to **Settings → Pages**.
2. At the top there will be a green box: *"Your site is live at …"*
3. The address will be:

   ```
   https://YOUR-USERNAME.github.io/24ib-intelligence/
   ```

Click it. That is your intelligence site, on the public internet, for free.

> The first deploy can take an extra 2–3 minutes to become reachable even after
> the workflow goes green. If you get a 404 immediately, wait three minutes and
> refresh before worrying.

Bookmark it. Send it to people. It will keep working whether or not your laptop
is switched on.

---

## Part 8 — What happens from now on

**Nothing. That is the entire point.**

Every Monday at 04:30 UTC (10:00 in India), GitHub will:

1. Start a temporary Linux machine.
2. Download your repository onto it, including your deal database.
3. Install Python, the libraries, and Chromium.
4. Scrape the six most recent Inc42 "Funding Galore" editions.
5. Insert any deals it has not seen before (duplicates are ignored
   automatically).
6. Build any weekly or monthly report that is now complete and whose data
   changed.
7. Write the new pages, fix the "Previous / Next" links on the neighbouring
   reports, and refresh the archive and home pages.
8. Commit the updated database and pages back into your repository.
9. Publish the site.
10. Delete the temporary machine.

### The reports do not change unless the data changes

This was designed carefully. Every published page carries a "last updated"
date stamp, so re-rendering a report that had not changed would silently
restamp an old page and make it look edited. The script therefore rebuilds a
period **only** when it has no report yet, or when a new deal has landed inside
that period since the last build. A week where nothing happened produces no
commit at all — the log simply says *"nothing to rebuild"*.

This was verified before handing over: running the script against your current
data produced **zero byte-level changes** across all 112 published files.

### Changing the day or time

Open `.github/workflows/weekly-update.yml` and find this line:

```yaml
    - cron: "30 4 * * 1"
```

The five values are `minute hour day-of-month month day-of-week`. The times are
always **UTC**, which is 5 hours 30 minutes behind India.

| You want | Use | Note |
|---|---|---|
| Monday 10:00 IST | `"30 4 * * 1"` | the current setting |
| Monday 18:00 IST | `"30 12 * * 1"` | |
| Tuesday 09:00 IST | `"30 3 * * 2"` | `2` = Tuesday |
| Every day 08:00 IST | `"30 2 * * *"` | `*` = every day |

Edit it in GitHub Desktop, commit, and push (Part 11 explains how).

---

## Part 9 — Editing data by hand

The automation only handles *new* data arriving. The full editing interface —
the ledger, correcting a deal, bulk-reassigning sectors, reviewing duplicates —
is the Flask app, and it still runs on your laptop exactly as before.

The workflow when you need to fix something:

1. Open GitHub Desktop and click **Fetch origin**, then **Pull origin** if it
   offers. This downloads whatever the robot saved since you last looked. **Do
   this first, every time** — otherwise you will edit an out-of-date database.
2. Run `python run.py` on your laptop and make your corrections in the browser.
3. Close the app.
4. Back in GitHub Desktop you will see `instance/intelligence.db` and some
   report pages listed as changed.
5. Type a summary like `Correct Acme Corp round size` and click
   **Commit to main**.
6. Click **Push origin**.

Your correction is now live, and next Monday's automatic run will build on top
of it.

> **The one rule:** always Pull before you edit, always Push when you are done.
> If you edit without pulling, you and the robot will have changed the same
> database file in two different places, and Git will ask you to resolve a
> conflict — which, for a binary database file, is genuinely annoying to
> untangle.

---

## Part 10 — When it breaks

GitHub emails you automatically whenever a scheduled run fails. You do not need
to check anything proactively.

The most likely failure, by a wide margin, is **Inc42 redesigning their
website**. The scraper looks for specific hidden labels in their page structure;
when those change, it finds nothing. The code was deliberately changed so that
this fails loudly with a red cross rather than quietly reporting success while
publishing nothing — a silent failure could have gone unnoticed for months.

| What you see | What it means | What to do |
|---|---|---|
| `No articles found on the Inc42 tag page` | Inc42 changed their page layout. | The selectors in `_discover_article_urls` in `intelligence/ingestion/inc42.py` need updating to match their new markup. |
| `Parsed 0 deal rows from 6 Inc42 article(s)` | The articles loaded, but their deal tables changed shape. | `_parse_article` in the same file needs updating. |
| `Could not reach Inc42 (browser/network unavailable)` | A temporary network blip on GitHub's side. | Do nothing. Next week will almost certainly work. Or re-run it by hand (Part 6). |
| `Permission to ... denied` / `403` | Part 4 was skipped or undone. | Go back and do Part 4. |
| `Your site is live at` never appears | Part 5 was skipped. | Go back and do Part 5. |
| Site shows old content | The deploy step was skipped because an earlier step failed. | Fix the underlying red step; the site stays on the last good version meanwhile. |

**Important reassurance:** a failed run never damages the live site. The commit
and deploy steps only execute if everything before them succeeded, so a broken
scrape leaves last week's site serving happily until you fix it.

### Running it again after a fix

Go to **Actions → Weekly intelligence update → Run workflow**. No need to wait
for Monday.

---

## Part 11 — Pushing a change to the automation itself

If you edit the workflow file, the script, or any code:

1. Open GitHub Desktop.
2. Click **Fetch origin** and pull if offered.
3. Make your edit in your normal editor and save.
4. GitHub Desktop shows the change. Type a summary. Click **Commit to main**.
5. Click **Push origin**.

The next scheduled run uses the new version automatically.

---

## Part 12 — Three things worth knowing long-term

**1. The repository grows.** Every week the job commits a ~2 MB database file,
and Git keeps every version forever. That is roughly 100 MB per year. GitHub
starts warning you above 1 GB, so you have several comfortable years. If it ever
becomes a problem, the fix is to start a fresh repository with only the current
database — the Excel master (`instance/Inc42_Funding_Master_Data.xlsx`) is a
complete, human-readable backup of every deal, so nothing is ever truly at risk.

**2. Scheduled jobs switch off after 60 days of silence.** GitHub disables cron
schedules in repositories that have had no activity for 60 days, to avoid
running abandoned projects forever. **This will not affect you**, because your
job pushes a commit most weeks, and a push counts as activity. It is only worth
knowing in case you ever pause the project for two months — if that happens, go
to the Actions tab and click **Enable workflow** to wake it up.

**3. The scraper is the fragile part, not the hosting.** GitHub Pages and
Actions are extremely reliable. Inc42's HTML is not a contract with you, and
will change eventually. When it does, you will get an email, the site will keep
serving its last good version, and someone will need to spend thirty minutes
updating two functions. That is the realistic ongoing maintenance burden of this
setup — a few times per year at most.

---

## Quick reference

| I want to… | Do this |
|---|---|
| See my website | `https://YOUR-USERNAME.github.io/24ib-intelligence/` |
| Check whether last night's run worked | Repository → **Actions** tab |
| Force an update right now | Actions → Weekly intelligence update → **Run workflow** |
| Change the schedule | Edit the `cron:` line in `.github/workflows/weekly-update.yml` |
| Correct a deal | Pull in GitHub Desktop → `python run.py` → edit → commit → push |
| Rebuild reports without scraping | `python scripts/weekly_update.py --skip-refresh` |
| Force-rebuild recent reports | `python scripts/weekly_update.py --force` |
| Rebuild the entire site from scratch | `python publish_site.py` |
