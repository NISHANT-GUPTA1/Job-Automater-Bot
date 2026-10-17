# Run Job Applier 24/7 on a free Oracle Cloud server

About 30 minutes, one time. Afterwards the bot runs even when your laptop is off.

## 1. Create the free server (Oracle Cloud "Always Free")

1. Sign up at **https://www.oracle.com/cloud/free/**. A card is needed for verification only;
   Always Free resources are never charged. Pick a **home region** close to you (e.g. India West
   (Mumbai) or India South (Hyderabad)). It can't be changed later.
2. In the console: **☰ → Compute → Instances → Create instance**.
   - **Image:** click *Edit* → *Change image* → **Canonical Ubuntu 24.04**.
   - **Shape:** *Change shape* → **Ampere** → **VM.Standard.A1.Flex** → **2 OCPUs, 12 GB memory**
     (free up to 4 OCPUs / 24 GB).
   - **Networking:** keep the defaults (it gets a public IP).
   - **SSH keys:** **Generate a key pair for me** → **Save private key**. Keep that file safe.
   - Click **Create**. When it shows *Running*, copy the **Public IP address**.

   *"Out of capacity"?* Free ARM servers are popular. Try again later or pick another
   *Availability domain*. Upgrading to a pay-as-you-go account (still free for these resources)
   usually removes the wait.

## 2. Install Tailscale (private access to the bot's browser)

Install **Tailscale** on your phone (Play Store / App Store) and laptop (https://tailscale.com/download)
and sign in with the same account (Google login works). This lets only your devices open the bot's
browser view — it is never public on the internet.

## 3. Move the bot to the server

In **PowerShell**, in the `C:\Job-Applier` folder (replace the key path and IP):

```powershell
# a) pack the app + your data (also stops the laptop bot - only one bot may run)
powershell -ExecutionPolicy Bypass -File deploy\make_bundle.ps1

# b) let SSH use the key file (Windows is strict about key permissions)
$key = "C:\Users\<you>\Downloads\ssh-key.key"   # the private key file you downloaded from Oracle
icacls $key /inheritance:r /grant:r "$($env:USERNAME):R"

# c) upload, unpack and run the setup (asks for a browser-view password, then shows a Tailscale login link)
scp -i $key job-applier-bundle.tar.gz ubuntu@SERVER_IP:~
ssh -t -i $key ubuntu@SERVER_IP "mkdir -p job-applier && tar -xzf job-applier-bundle.tar.gz -C job-applier && bash job-applier/deploy/setup_server.sh"
```

During setup:
- Choose a password for the browser view (6–8 characters).
- A **Tailscale login link** appears: open it and sign in with the same Tailscale account.
- At the end, the bot messages you **"🤖 Job Applier is online"** on Telegram. Done. You can switch the laptop off.

## Using it from your phone

- Everything works through Telegram as before.
- When the bot needs you in the browser (captcha, upload, manual step), its message includes a
  **🖥 Open the bot's browser** link. Tap it (with Tailscale on), enter your browser-view password,
  and you see and control the real browser.
- `/screen` sends a screenshot of what the browser is showing right now.

## Updating the code later

After a change on the laptop, send **code only**. Your tracker data now lives on the server, so don't
send data again:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\make_bundle.ps1 -CodeOnly
scp -i $key job-applier-bundle.tar.gz ubuntu@SERVER_IP:~
ssh -i $key ubuntu@SERVER_IP "tar -xzf job-applier-bundle.tar.gz -C job-applier && sudo systemctl restart jobapplier"
```

Edit your profile on the server: `ssh -t -i $key ubuntu@SERVER_IP "nano job-applier/profile.yaml"`, then `/reload` in Telegram.

## Handy server commands (inside `ssh -i $key ubuntu@SERVER_IP`)

| What | Command |
|---|---|
| Live log | `journalctl -u jobapplier -f` |
| Restart bot | `sudo systemctl restart jobapplier` |
| Stop bot | `sudo systemctl stop jobapplier` |
| Status | `systemctl status jobapplier` |

## Good to know

- **Don't run the bot on the laptop and the server at the same time.** They'd fight over the same Telegram bot.
- Job sites show captchas a bit more often to cloud servers than to home internet. When that
  happens you get the numbered-tile puzzle on Telegram, or solve it via the browser link.
- Sites you were logged into in the laptop's browser need one fresh login on the server (the bot
  signs in with its saved passwords; `/creds` lists them).
