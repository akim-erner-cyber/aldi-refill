#!/usr/bin/env python3
"""ALDI TALK Auto-Refill: bucht das gratis 1 GB automatisch nach."""

import asyncio
import datetime
import json
import os
import re
import sys
import urllib.request

from playwright.async_api import async_playwright

PORTAL = "https://www.alditalk-kundenportal.de/portal/auth/uebersicht/"
NUMMER = os.environ.get("ALDI_NUMMER", "")
PASSWORT = os.environ.get("ALDI_PASSWORT", "")
SCHWELLE_GB = float(os.environ.get("SCHWELLE_GB", "1.0"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notify(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TG_CHAT, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
        log("Telegram-Nachricht gesendet.")
    except Exception as e:
        log(f"Telegram-Fehler: {e}")


async def login(page):
    log("Login laeuft ...")
    try:
        await page.locator('button[data-testid="uc-accept-all-button"]').first.click(timeout=4000)
    except Exception:
        pass
    await page.locator('input[name="callback_2_od"]').fill(NUMMER)
    await page.locator('input[name="callback_3_od"]').fill(PASSWORT)
    try:
        await page.locator('one-checkbox[name="loginRemember_od"]').click(timeout=3000)
    except Exception:
        pass
    await page.locator('one-button[id="IDToken5_4_od_2"]').click()
    await page.wait_for_url(lambda u: "login" not in u, timeout=90_000)
    log("Login OK.")


async def restvolumen(page):
    text = await page.locator("body").inner_text()
    m = re.search(r"Noch\s+([\d.,]+)\s*(GB|MB|KB)", text)
    if not m:
        return None, text
    raw = m.group(1)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    wert = float(raw)
    if m.group(2) == "MB":
        wert /= 1024
    elif m.group(2) == "KB":
        wert /= 1024 * 1024
    return wert, text


async def durchlauf():
    if not NUMMER or not PASSWORT:
        log("ALDI_NUMMER oder ALDI_PASSWORT fehlt.")
        sys.exit(2)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1600, "height": 1000},
            locale="de-DE",
            timezone_id="Europe/Berlin",
        )
        page = await ctx.new_page()
        try:
            await page.goto(PORTAL, wait_until="domcontentloaded", timeout=60_000)
            if "login" in page.url:
                await login(page)
            await page.wait_for_timeout(4000)

            gb, text = await restvolumen(page)
            log(f"Restvolumen: {gb} GB" if gb is not None else "Restvolumen nicht erkannt")

            if gb is not None and gb > SCHWELLE_GB:
                log(f"Ueber Schwelle ({SCHWELLE_GB} GB) - nichts zu tun.")
                return

            if "Bestellung erhalten" in text or "eingegangen" in text:
                log("Nachbuchung ist schon unterwegs - warte auf naechsten Lauf.")
                return

            btn = None
            for sel in [
                'button:has-text("+1 GB")',
                'one-button:has-text("+1 GB")',
                'text="+1 GB"',
                '[class*="refill"]',
            ]:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    btn = loc.first
                    log(f"Button gefunden via {sel}")
                    break

            if btn is None:
                log("Kein +1-GB-Button gefunden.")
                return

            if DRY_RUN:
                log("DRY_RUN aktiv - kein Klick.")
                return

            log("Klicke +1 GB ...")
            await btn.click(force=True)
            await page.wait_for_timeout(2500)

            for sel in [
                'one-button:has-text("Bestaetigen")',
                'button:has-text("Bestaetigen")',
                'one-button:has-text("Nachbuchen")',
                'button:has-text("Nachbuchen")',
            ]:
                try:
                    await page.locator(sel).last.click(timeout=2500)
                    log(f"Dialog bestaetigt via {sel}")
                    await page.wait_for_timeout(2500)
                    break
                except Exception:
                    continue

            nach = await page.locator("body").inner_text()
            if "Bestellung erhalten" in nach or "eingegangen" in nach:
                log("ERFOLG: 1 GB wurde nachgebucht.")
                notify("ALDI TALK: gratis 1 GB automatisch nachgebucht.")
            else:
                log("Ergebnis unklar - siehe debug.png im Artefakt.")
        except Exception as e:
            log(f"Fehler: {e}")
        finally:
            try:
                await page.screenshot(path="debug.png", full_page=True)
            except Exception:
                pass
            await ctx.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(durchlauf())
