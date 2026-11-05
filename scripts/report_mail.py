#!/usr/bin/env python3
"""
Stuurt op het einde van de werkstroom een mail met het resultaat (geslaagd of mislukt), via Gmail.

Leest:
- MAIL_USERNAME, MAIL_PASSWORD (secrets: het Gmail-adres en een app-wachtwoord van Google). Ontbreken ze, dan
  wordt er niets verstuurd (geen fout).
- RESULT_PLAN, RESULT_PART, RESULT_MERGE: het resultaat van de jobs (success, failure, cancelled, skipped).
- GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_SERVER_URL: om de mislukte jobs op te zoeken en de link.
- report/summary.md (samenvatting van merge), report/merge.log en report/upload.log, als ze er zijn.

De mail gaat naar hetzelfde adres als MAIL_USERNAME. Het adres staat dus nergens in deze openbare repository.

Gebruik (in de werkstroom):  python3 scripts/report_mail.py --dir report
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
import urllib.request
from email.message import EmailMessage


def failed_jobs() -> list[str]:
    """De namen van de jobs die mislukten, bv. 'part (portugal)'."""
    token, repo, run = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if not (token and repo and run):
        return []
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run}/jobs?per_page=100"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            jobs = json.load(r)["jobs"]
    except Exception as e:  # noqa: BLE001 - de mail moet er toch komen
        print(f"jobs niet op te halen: {e}")
        return []
    return [j["name"] for j in jobs if j.get("conclusion") in ("failure", "cancelled", "timed_out")]


def tail(path: str, lines: int) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-lines:])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="report")
    args = ap.parse_args()

    user, password = os.environ.get("MAIL_USERNAME", "").strip(), os.environ.get("MAIL_PASSWORD", "").replace(" ", "")
    if not user or not password:
        print("::warning::Geen mail verstuurd: secrets MAIL_USERNAME en MAIL_PASSWORD ontbreken")
        return

    results = {name: os.environ.get(f"RESULT_{name.upper()}", "?") for name in ("plan", "part", "merge")}
    ok = all(v == "success" for v in results.values())
    failed = failed_jobs()
    run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
               f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")

    lines = []
    if ok:
        lines.append("De tegels zijn gemaakt en opgeladen.")
    else:
        lines.append("De run is MISLUKT. Wat in R2 staat, blijft bruikbaar.")
        if failed:
            lines.append("Mislukte jobs: " + ", ".join(failed))
        lines.append("Resultaat per stap: " + ", ".join(f"{k}: {v}" for k, v in results.items()))
    lines.append(f"Details: {run_url}")
    lines.append("")

    summary = os.path.join(args.dir, "summary.md")
    if os.path.exists(summary):
        with open(summary, encoding="utf-8") as f:
            lines.append(f.read().replace("## ", "").strip())
        lines.append("")
    upload = tail(os.path.join(args.dir, "upload.log"), 20).strip()
    if upload:
        lines += ["Opladen en wissen:", upload, ""]
    if results["merge"] != "success":
        log = tail(os.path.join(args.dir, "merge.log"), 25).strip()
        if log:
            lines += ["Laatste regels van het samenvoegen:", log, ""]

    msg = EmailMessage()
    msg["Subject"] = ("Tegels: geslaagd" if ok else "Tegels: MISLUKT") + f" (run {os.environ.get('GITHUB_RUN_ID', '')})"
    msg["From"] = user
    msg["To"] = user
    msg.set_content("\n".join(lines))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    except Exception as e:  # noqa: BLE001
        print(f"::error::Mail niet verstuurd: {e}")
        sys.exit(1)
    print("Mail verstuurd.")


if __name__ == "__main__":
    main()
