# WG-Gesucht checker

Checks new Berlin WG-room offers every minute and sends matches to Telegram with an apply link.

Filters: within 1 km of U Schlesisches Tor / Görlitzer Bhf / Kottbusser Tor / Prinzenstr. / Hallesches Tor / Möckernbrücke,
rent ≤ 650 €, Anmeldung not ruled out, long-term (Zwischenmiete/temporary rejected unless extension is possible), not female-only.
Tune the keyword lists at the top of `wg_checker.py` (`ANMELDUNG_NEGATIVE`, `TEMPORARY`, `EXTENSION`).

## Runs on GitHub Actions (free)

`.github/workflows/check.yml` runs `wg_checker.py --once` every 5 minutes and commits `seen.json` so ads
are only reported once. The repo must be **public** for unlimited free Actions minutes.
Telegram credentials live in the repo's secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
(Settings → Secrets and variables → Actions). Pause it from the Actions tab → "Check WG-Gesucht" → ⋯ → Disable workflow.

## Alternative: deploy on your own Ubuntu/Debian server

```bash
sudo useradd --system --create-home --home-dir /opt/wg-checker wgchecker
sudo cp wg_checker.py requirements.txt .env.example wg-checker.service /opt/wg-checker/
cd /opt/wg-checker
sudo -u wgchecker python3 -m venv .venv
sudo -u wgchecker .venv/bin/pip install -r requirements.txt
sudo -u wgchecker cp .env.example .env && sudo -u wgchecker nano .env   # paste token + chat id
sudo -u wgchecker .venv/bin/python wg_checker.py --telegram-test         # should ping you
sudo -u wgchecker .venv/bin/python wg_checker.py --seed                  # skip the ads already online
sudo cp wg-checker.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now wg-checker
journalctl -u wg-checker -f                                              # watch the log
```

## Handy commands

- `python wg_checker.py --test <ad-url>` – show the verdict for one ad
- `python wg_checker.py --once` – one check, then exit
