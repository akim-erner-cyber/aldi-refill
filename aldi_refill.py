#!/usr/bin/env python3
"""ALDI TALK Auto-Refill mit DOM-Diagnose."""

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

JS_FIND = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const nodes = [];
  const walk = (root) => {
    root.querySelectorAll('*').forEach(el => {
      nodes.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    });
  };
  walk(document);

  const kand = nodes.filter(el => {
    const t = norm(el.textContent);
    return t && t.length <= 40 && /^\+?\s*1\s*GB$/.test(t);
  });
  kand.sort((a, b) => norm(a.textContent).length - norm(b.textContent).length);

  const btns = Array.from(document.querySelectorAll('button, one-button, [role="button"]'))
    .map(el => el.tagName + ' | class=' + el.className + ' | text=' + norm(el.textContent).slice(0, 50))
    .slice(0, 40);

  return {
    treffer: kand.length,
    liste: kand.slice(0, 10).map(el => el.tagName + ' | class=' + el.className + ' | text=' + norm(el.textContent)),
    buttons: btns
  };
}
"""

JS_CLICK = r"""
(level) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const nodes = [];
  const walk = (root) => {
    root.querySelectorAll('*').forEach(el => {
      nodes.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    });
  };
  walk(document);

  const kand = nodes.filter(el => {
    const t = norm(el.textContent);
    return t && t.length <= 40 && /^\+?\s*1\s*GB$/.test(t);
  });
  kand.sort((a, b) => norm(a.textContent).length - norm(b.textContent).length);

  let z = kand[0];
  if (!z) return 'nichts';
  for (let i = 0; i < level; i++) {
    z = z.parentElement;
    if (!z) return 'nichts';
  }
  z.scrollIntoView({ block: 'center' });
  z.click();
  return z.tagName + ' | class=' + z.className;
}
"""


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
            await page.wait_for_timeout(5000)

            gb, text = await restvolumen(page)
            log(f"Restvolumen: {gb} GB" if gb is not None else "Restvolumen nicht erkannt")

            info = await page.evaluate(JS_FIND)
            log(f"Elemente mit '+1 GB': {info['treffer']}")
            for z in info["liste"]:
                log("  TREFFER: " + z)
            for z in info["buttons"][:25]:
                log("  BTN: " + z)

            if "Bestellung erhalten" in text or "eingegangen" in text:
                log("Nachbuchung ist schon unterwegs - warte.")
                return "pending"

            if gb is not None and gb > SCHWELLE_GB:
                log(f"Ueber Schwelle ({SCHWELLE_GB} GB) - nichts zu tun.")
                return "ok"

            if DRY_RUN:
                log("DRY_RUN aktiv - kein Klick.")
                return "dryrun"

            gebucht = False
            for level in range(4):
                ziel = await page.evaluate(JS_CLICK, level)
                log(f"Klick-Versuch Stufe {level}: {ziel}")
                if ziel == "nichts":
                    break
                await page.wait_for_timeout(3000)

                seite = await page.locator("body").inner_text()
                if "Bestellung erhalten" in seite or "eingegangen" in seite:
                    gebucht = True
                    break

                for sel in [
                    'one-button:has-text("Bestaetigen")',
                    'button:has-text("Bestaetigen")',
                    'one-button:has-text("Nachbuchen")',
                    'button:has-text("Nachbuchen")',
                    'one-button:has-text("Ja")',
                ]:
                    try:
                        await page.locator(sel).last.click(timeout=2000)
                        log(f"Dialog bestaetigt via {sel}")
                        await page.wait_for_timeout(3000)
                        seite = await page.locator("body").inner_text()
                        if "Bestellung erhalten" in seite or "eingegangen" in seite:
                            gebucht = True
                        break
                    except Exception:
                        continue
                if gebucht:
                    break

            if gebucht:
                log("ERFOLG: 1 GB wurde nachgebucht.")
                notify("ALDI TALK: gratis 1 GB automatisch nachgebucht.")
                return "booked"
            log("Kein Erfolg erkennbar - siehe debug.png und debug.html.")
            return "unclear"

        except Exception as e:
            log(f"Fehler: {e}")
            return "error"
        finally:
            try:
                await page.screenshot(path="debug.png", full_page=True)
            except Exception:
                pass
            try:
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(await page.content())
            except Exception:
                pass
            await ctx.close()
            await browser.close()


if __name__ == "__main__":
    ergebnis = asyncio.run(durchlauf())
    log(f"Ergebnis: {ergebnis}")
    if ergebnis in ("error", "unclear"):
        sys.exit(1)
