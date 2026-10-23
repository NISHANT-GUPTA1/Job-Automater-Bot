# Job Automater Bot

A personal Telegram bot that helps you apply to jobs. You send it a job link, a screenshot of a job post,
or just a message, and it:

- reads the job and saves the description, a match score and a short summary
- tailors your LaTeX resume for that job and checks it like a recruiter and an ATS would
- opens the application in a browser and fills it in (sign-up, login, forms, uploads, email verification codes)
- asks you on Telegram whenever it needs something it doesn't know, and remembers your answer
- writes cold emails and cover letters, shows them to you, and sends them from your Gmail only after you approve
- keeps track of every job, email and site account in a local database and Excel sheets

Everything runs on your own computer (or your own server). Your data stays in local files that are not part of
this repository.

---

## Contents

1. [What you need](#what-you-need)
2. [Setup on Windows, step by step](#setup-on-windows-step-by-step)
3. [Keeping it running](#keeping-it-running)
4. [Using the bot](#using-the-bot)
5. [How an application is handled](#how-an-application-is-handled)
6. [Prospector: cold outreach to small companies](#prospector-cold-outreach-to-small-companies)
7. [Settings (.env)](#settings-env)
8. [Running costs](#running-costs)
9. [Where your data is stored](#where-your-data-is-stored)
10. [Troubleshooting](#troubleshooting)
11. [Project layout](#project-layout)

---

## What you need

- Windows 10/11 (Linux works too, see [deploy/DEPLOY.md](deploy/DEPLOY.md))
- Python 3.10 or newer (3.13 is what it's developed on)
- A Telegram account
- A Gmail account with 2-Step Verification turned on
- An API key for at least one AI provider: Anthropic (recommended), OpenAI, Google Gemini or Groq
- Your resume as a LaTeX (`.tex`) file. A template is included if you don't have one.

---

## Setup on Windows, step by step

All commands are for **PowerShell**, run from the project folder.

### 1. Get the code

```powershell
git clone https://github.com/NISHANT-GUPTA1/Job-Automater-Bot.git
cd Job-Automater-Bot
```

### 2. Create a virtual environment and install the packages

```powershell
py -3 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

### 3. Install the browser the bot uses

```powershell
.venv\Scripts\python -m playwright install chromium
```

### 4. Download two small tools

**Tectonic** compiles your LaTeX resume into a PDF (no full LaTeX installation needed):

```powershell
New-Item -ItemType Directory -Force tools\tectonic | Out-Null
Invoke-WebRequest "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-x86_64-pc-windows-msvc.zip" -OutFile tectonic.zip
Expand-Archive tectonic.zip tools\tectonic -Force
Remove-Item tectonic.zip
```

**cloudflared** lets you see and control the bot's browser from your phone (optional, but you'll want it for
captchas and logins when you're away from the computer):

```powershell
New-Item -ItemType Directory -Force tools\cloudflared | Out-Null
Invoke-WebRequest "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile tools\cloudflared\cloudflared.exe
```

### 5. Create your Telegram bot

1. In Telegram, open **@BotFather** and send `/newbot`.
2. Pick a name and a username for the bot.
3. BotFather replies with a token like `1234567890:AAE...`. Copy the **whole** token, including the number and
   the colon at the start.

### 6. Create a Gmail App Password

The bot sends your emails and reads verification codes through Gmail.

1. Go to your Google Account → **Security** and make sure **2-Step Verification** is on.
2. Search for **App passwords** in the account settings and create one (any name, e.g. "job bot").
3. Copy the 16-letter password.

### 7. Get an AI API key

Any one of these works. They are tried in this order, so the first one you fill in is the main one:

- Anthropic: https://console.anthropic.com → API Keys (recommended: the default model is the cheapest Claude)
- OpenAI: https://platform.openai.com/api-keys
- Google Gemini: https://aistudio.google.com/apikey
- Groq: https://console.groq.com/keys

### 8. Fill in your settings

```powershell
Copy-Item .env.example .env
notepad .env
```

Fill in at least:

| Setting | What to put |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from step 5 |
| `ANTHROPIC_API_KEY` (or another provider) | the key from step 7 |
| `GMAIL_ADDRESS` | your Gmail address |
| `GMAIL_APP_PASSWORD` | the App Password from step 6 |
| `APPLY_EMAIL` | the email to use for all job-site sign-ups (can be the same Gmail) |

Leave `TELEGRAM_CHAT_ID` empty for now. All other settings are explained in [Settings (.env)](#settings-env).

### 9. Fill in your profile

```powershell
Copy-Item profile.example.yaml profile.yaml
notepad profile.yaml
```

These are the answers used on application forms: name, phone, address, education, links, expected salary and
so on. You don't have to fill everything. When a form asks for something missing, the bot asks you on Telegram
once and remembers it.

### 10. Add your resume

```powershell
Copy-Item resumes\base_resume.example.tex resumes\base_resume.tex
notepad resumes\base_resume.tex
```

Replace the template's content with your own, or paste in your existing LaTeX resume. Two things matter:

- The resume body must **start with** `\section{Education}`. Everything above it (the preamble and your
  name/contact header) is copied unchanged into every tailored resume.
- It should fit on **one A4 page**.

If you edit your resume on Overleaf, set the compiler to **XeLaTeX** (Menu → Compiler) because the template
uses `fontspec`.

### 11. Check the setup

```powershell
.venv\Scripts\python main.py --check
```

Every line should say `OK`, except `TELEGRAM_CHAT_ID`, which you set in the next step. The check also confirms
your Telegram token with Telegram, sends one tiny test request to your AI provider and builds your resume PDF.

### 12. Start the bot and connect your chat

```powershell
.venv\Scripts\python main.py
```

1. In Telegram, open your bot and send `/start`.
2. It replies with your chat id, a number like `812345678`.
3. Put that number in `.env` as `TELEGRAM_CHAT_ID`, stop the bot with `Ctrl + C`, and start it again.
4. The bot messages you: "Job Applier is online".

Only this chat can control the bot. Anyone else who finds it gets ignored.

---

## Keeping it running

To have the bot start by itself and keep running without a terminal window:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install_autostart.ps1
```

This:

- starts the bot hidden whenever you log in to Windows
- checks every 5 minutes and restarts it if it stopped
- keeps the laptop awake with the lid closed **while charging** (on battery it still sleeps as usual)

Useful commands:

| What | Command |
|---|---|
| Restart the bot (after changing `.env`) | `Stop-ScheduledTask JobApplier; Start-ScheduledTask JobApplier` |
| Stop it until the next login | `Stop-ScheduledTask JobApplier` |
| Remove the auto-start | `powershell -ExecutionPolicy Bypass -File deploy\install_autostart.ps1 -Uninstall` |
| Watch the log | `Get-Content data\app.log -Tail 30 -Wait` |

Only run one copy at a time. If a copy is already running, a second one exits with a message.

To run it on a free cloud server instead of your computer, follow [deploy/DEPLOY.md](deploy/DEPLOY.md).

---

## Using the bot

### Sending jobs

| You send | What happens |
|---|---|
| A job link | It reads the job, tailors your resume, applies on the site, and drafts a cold email if the post has a contact email |
| A link plus an email address | Same, and the cold email goes to that address |
| A screenshot of a job post | It reads the image. With a link it applies; without one it drafts a cold email |
| Several screenshots at once | One job per post (screenshots of the same post are merged) |
| A pasted job description | Same as a screenshot |
| Just an email address | Drafts a cold email for your most recent job |

### Just writing to it

You can write normal messages. A few examples:

- `Write a cover letter for this job, highlight my hackathon win: <paste the job description>`
- `Make my resume for these requirements: Go, Kafka, PostgreSQL, Kubernetes`
- `Mail priya@company.com, Priya, HR at Company, for a backend intern role`
- `Skip job 5` / `retry 12` / `mark 9 as interview`
- `From now on always choose LinkedIn when a form asks how I heard about the job`
- `How many jobs did I apply to this week?`

Standing instructions ("from now on…", "never…") are remembered and used in all later work.

### Commands

| Command | What it does |
|---|---|
| `/jobs` or `/jobs applied` | List tracked jobs, optionally by status |
| `/job 5` | Details, timeline, emails and cost for one job |
| `/stats` | Totals, today's progress and AI spend |
| `/export` | Excel file with all jobs, emails, accounts, saved answers and job descriptions |
| `/mailed` | Sheet of everyone you've cold-emailed: name, email, company, position, date |
| `/creds` | Saved job-site logins, plus the accounts sheet |
| `/email 5`, `/resume 5`, `/retry 5` | Redo one step for a job |
| `/status 5 interview` | Record progress: interview, offer, rejected, ghosted… |
| `/mail <email> <company> <name> <role>` | Cold-email someone without a job post |
| `/teach Expected CTC = 6 LPA` | Teach an answer in advance |
| `/memory`, `/forget 3` | See or delete remembered answers |
| `/remote` | Link to see and control the bot's browser from your phone |
| `/open <url>` | Open a page in the bot's browser (for example to log in to a site yourself) |
| `/screen` | Screenshot of what the browser shows right now |
| `/reload` | Reload `profile.yaml` and your resume after editing them |

### When the bot needs you

Messages that need you in the browser (a captcha, a missing file, a stuck page) come with an
**Open the bot's browser** button. It opens a live view of the browser on your phone: tap the picture to click,
type into the text box, scroll, go back. A link to this view is also pinned at the top of the chat.

Image captchas are also sent as a picture with numbered tiles. Reply with the numbers, for example `1 4 7`.

---

## How an application is handled

1. **Read the job.** The page (or screenshot) is turned into text, and you get a card with the role, company,
   skills, match score and any contact email.
2. **Resume.** Your resume is reviewed against the job like a recruiter would ("would I reject this, and why?"),
   rewritten to fix those reasons, checked again, and pushed to at least 85% ATS keyword match. If a similar job
   already has an approved resume, you can reuse it for free. You see the PDF with its scores and approve it,
   ask for changes, or use your base resume.
3. **Apply.** The browser goes through cookie banners, job lists, sign-up or login and the form pages. Known
   answers (your profile, things you've taught it, common questions) are filled without using the AI. The AI is
   used once per page for the rest. Questions it can't answer come to you.
4. **Accounts.** Every job site uses the same email and password (`APPLY_EMAIL` / `APPLY_PASSWORD`). If a login
   fails, it resets the password through the site's "Forgot password" email.
5. **Verification codes** are read from your Gmail automatically.
6. **Submit.** Before the final submit you get a screenshot and decide (unless `AUTO_SUBMIT=true`).
7. **Cold email.** If there's a contact address, you get a draft with your resume attached. Nothing is sent until
   you approve it.
8. **Tracking.** Status, emails, accounts and the AI cost for that job are recorded.

The bot never makes up employers, job titles, dates, grades, numbers or projects. Questions written to catch AI
("if you are an AI, write…") are never followed. You are told about them and decide the answer yourself.

---

## Prospector: cold outreach to small companies

A second, separate bot for reaching people directly instead of applying through job sites. Every day it:

1. finds small companies hiring for the roles you list (software, backend, Python, full-stack, React, AI
   engineers and interns): from the monthly Hacker News "Who is hiring?" thread (filtered to posts open to India /
   worldwide and to juniors or interns), Apollo company search if your plan has it, and companies you send it
2. keeps only companies of the size you're targeting (1–50 employees first), using Apollo's company lookup
3. finds who to write to, cheapest source first: a hiring email in the job post itself, hiring addresses the
   company publishes on its own website (careers@, hr@, jobs@…), then Hunter.io for named HR, engineering-manager
   or founder emails. Order of preference: HR / hiring inbox → engineering manager / CTO → founder → general inbox
4. checks each company's public job board (Greenhouse, Lever, Ashby) for open roles that fit you
5. drafts a personal email for each (with your resume attached) and sends you the day's batch on Telegram
6. after you approve, sends them a few minutes apart, never more than your daily limit
7. watches your inbox: replies are reported to you and stop further emails; one follow-up goes out in the same
   thread after a few days if there's no answer; bounced addresses are marked

Nobody is emailed twice, including people the job bot already emailed.

You can also send it companies yourself (a website, a Wellfound or LinkedIn company link, a list of names, or a
screenshot). Wellfound has no public API and doesn't allow scraping, so the bot can't search it on its own.

### Setup

1. Create a **second** Telegram bot with @BotFather and put its token in `.env` as `PROSPECTOR_TELEGRAM_BOT_TOKEN`.
2. Create an Apollo.io account and copy your API key (Settings → Integrations → API) into `APOLLO_API_KEY`.
   On the free plan only the company lookup (size) works through the API; people and email search need a
   paid plan, which the bot uses automatically if you have one.
3. Optional but recommended: create a free Hunter.io account and put its API key in `HUNTER_API_KEY`.
4. Check what your plans allow:
   ```powershell
   .venv\Scripts\python prospector_main.py --check
   ```
5. Start it, or install it to run in the background:
   ```powershell
   .venv\Scripts\python prospector_main.py
   powershell -ExecutionPolicy Bypass -File deploy\install_autostart.ps1 -Bot prospector
   ```

### Credits

| Step | Cost |
|---|---|
| Hacker News, company websites, public job boards | free |
| Company size check (Apollo company lookup) | 1 Apollo credit per company, only when the size is unknown |
| Named contact from Hunter.io | 1 of your 25 free Hunter searches a month (used only when the website has no hiring address) |
| Apollo people/email search | paid Apollo plans only |

Monthly caps (`APOLLO_MONTHLY_CREDITS`, `HUNTER_MONTHLY_SEARCHES`) stop the bot before it goes over. Only verified
or published emails are used, to keep your Gmail's sender reputation clean.

### Daily batch

At `PROSPECT_SEND_HOUR` you get a summary with **Send all**, and every draft with **Send**, **Edit** (tell it what to
change) and **Drop** (never contact this person). Commands: `/run` (prepare now), `/drafts`, `/add`, `/stats`,
`/export` (Excel sheet of companies, contacts, emails and credits), `/companies`, `/stage 1|2|3` (company size
1–50, 51–200, 201–500), `/pause`, `/resume`, `/check`.

**Edit** understands plain requests: `subject: Backend Engineer – Jane Doe`, `rename the resume to
Jane_Doe_Backend_Engineer`, `make it shorter`. Add "for all emails" to apply the change to every draft in the batch.

Up to `PROSPECT_CONTACTS_PER_COMPANY` people are emailed at each company. When one of them replies, the others
there get nothing more. Stay under about 50 emails a day (`PROSPECT_DAILY_LIMIT`). Above that, a personal Gmail
starts landing in spam.

### LinkedIn (optional)

For companies you've emailed, the bot can also connect on LinkedIn with the people there. It gets their profile
links for free from Hunter results and YC founder pages, so it never searches LinkedIn.

1. Sign in once on the PC:

   ```powershell
   .venv\Scripts\python linkedin_login.py
   ```

   A browser opens on LinkedIn's sign-in page. Sign in yourself; the window closes when your feed loads. The bot
   reuses this session, kept in `data/linkedin_profile`. Your password is never saved anywhere.
2. Every few hours, between `LINKEDIN_ACTIVE_HOURS`, the bot:
   - sends a few connection requests (without a note unless `LINKEDIN_SEND_NOTES=true`; free accounts get about
     5 notes a month), up to `LINKEDIN_DAILY_INVITES` and
     `LINKEDIN_WEEKLY_INVITES`
   - checks who accepted
   - drafts a short first message for each person who accepted and sends it to Telegram for you to approve
     (**Send / Edit / Skip**)
   - tells you when someone replies, so you can take over from there.
3. `/linkedin` shows where everyone is. `/linkedin now` runs a round straight away. `/linkedin pause` and
   `/linkedin resume` stop and restart it.

LinkedIn's terms don't allow automation, and it does restrict accounts that send too many requests. Keep the
limits low; the defaults are 15 a day and 90 a week. If LinkedIn logs you out, shows a security check or hits its
weekly limit, the bot stops and tells you in Telegram.

---

## Settings (.env)

| Setting | Default | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | – | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | – | Your chat id (the bot tells you on `/start`) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY` | – | AI providers, tried in this order |
| `ANTHROPIC_MODEL` | `claude-haiku-5-5` | Claude model to use |
| `ANTHROPIC_EFFORT` | `low` | How much the model thinks (`low` keeps costs down) |
| `OPENAI_MODEL`, `GEMINI_MODEL`, `GROQ_MODEL` | see `.env.example` | Models for the fallback providers |
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | – | Gmail account and its App Password |
| `APPLY_EMAIL` | your Gmail | Email for every job-site sign-up |
| `APPLY_PASSWORD` | generated | Password for every job-site sign-up (created on first use if empty) |
| `HEADLESS` | `false` | `true` hides the browser window |
| `AUTO_SUBMIT` | `false` | `true` submits applications without asking |
| `BROWSER_CHANNEL` | – | `chrome` or `msedge` to use your installed browser |
| `MAX_STEPS_PER_JOB` | `30` | Pages the bot may go through for one application |
| `QUESTION_TIMEOUT_MIN` | `60` | How long it waits for your answer |
| `ALWAYS_TAILOR` | `true` | Tailor the resume for every job |
| `RESUME_MIN_ATS` | `85` | Minimum ATS keyword match (%) for tailored resumes |
| `RESUME_KEYWORD_MODE` | `related` | `related` may add job skills related to your real background; `strict` only uses what your resume says |
| `REMOTE_CONTROL` | `true` | Phone control of the browser |
| `REMOTE_PORT` | `8765` | Local port for that page |
| `USD_TO_INR` | `88` | Only for showing costs in rupees |
| `SECRET_KEY` | generated | Key used to encrypt saved passwords |

---

## Running costs

With the default Claude model (`claude-haiku-5-5`), roughly:

| Task | Cost |
|---|---|
| Reading and analysing a job | ~$0.001 |
| Tailored, recruiter-checked resume | ~$0.01–0.015 |
| Cover letter or cold email | ~$0.0005–0.0015 |
| Filling an application | ~$0.001 per page |
| Reading a screenshot or understanding a message | ~$0.0003–0.0005 |

After each job the bot tells you exactly what it cost. `/stats` shows the totals.

---

## Where your data is stored

Nothing personal is part of this repository. The files below are created on your machine and are listed in
`.gitignore`:

| Path | Contents |
|---|---|
| `.env` | API keys, tokens, Gmail App Password, job-site password |
| `profile.yaml` | Your personal details |
| `resumes/base_resume.tex` | Your resume |
| `resumes/generated/` | Tailored resumes and cover letters, one folder per job |
| `data/tracker.db` | Jobs, emails, remembered answers, AI usage (site passwords are encrypted) |
| `data/secret.key` | Encryption key for the saved passwords. Keep it if you move the bot |
| `data/Mailed_Contacts.xlsx`, `data/Site_Accounts.xlsx` | The tracking sheets (the accounts sheet contains passwords in plain text) |
| `data/browser_profile/` | The bot's browser profile (cookies and logins) |
| `data/app.log` | Log file |

---

## Troubleshooting

**"TELEGRAM_BOT_TOKEN invalid" in `--check`**
Copy the full token from @BotFather (`/mybots` → your bot → API Token). It must start with digits and a colon.

**The bot never answers**
`TELEGRAM_CHAT_ID` must be your own number from `/start`, not a `@username` and not the number at the start of
the bot token.

**Gmail login fails**
Use the 16-letter App Password, not your normal Gmail password. 2-Step Verification has to be on.

**Resume doesn't compile**
Run `--check` again and read the LaTeX error. The first compile downloads LaTeX packages, so it needs internet.
Make sure the body starts with `\section{Education}`.

**"Job Applier is already running"**
Another copy is running (for example the auto-start task). Stop it first, or just use that one.

**A site keeps failing**
Every site is different. Use the **Open the bot's browser** button to finish it yourself, then tell the bot
"I submitted it". Some sites (for example LinkedIn) don't allow automated applications in their terms of service.

---

## Project layout

```
main.py                    entry point (python main.py / python main.py --check)
jobapplier/
  bot.py                   Telegram commands and message handling
  assistant.py             understands free-form messages
  pipeline.py              per-job flow: resume -> apply -> cold email; cover letters
  browser.py               fills applications in the browser
  captcha.py               captcha handling
  remote.py                phone control of the browser
  resume.py                resume tailoring, ATS check, cover letters
  latex.py                 LaTeX to PDF
  jobs.py                  reading job posts and screenshots
  mailer.py                Gmail sending and verification codes
  llm.py                   AI providers with fallback and cost tracking
  memory.py                remembered answers
  tracker.py               stats and Excel sheets
  db.py, config.py, crypto.py, profile.py, asker.py
deploy/
  install_autostart.ps1    keep it running on Windows
  DEPLOY.md, setup_server.sh, make_bundle.ps1   run it on a Linux server
profile.example.yaml       template for profile.yaml
.env.example               template for .env
resumes/base_resume.example.tex   resume template
```
