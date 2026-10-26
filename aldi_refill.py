#!/usr/bin/env python3
"""ALDI TALK Auto-Refill mit robustem Button-Finder, Positions-Fallback und DOM-Diagnose."""

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
# Auf "0" setzen, wenn debug.png/debug.html nicht erzeugt werden sollen
# (enthalten Kontodaten - nie in ein oeffentliches Repo committen!)
DEBUG_FILES = os.environ.get("DEBUG_FILES", "1") == "1"
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

SCRIPT_VERSION = "v4-one-button"

# Passt auf "+1 GB", "+ 1 GB", "1 GB nachbuchen", "1GB hinzubuchen" usw.
PLUS_1GB = re.compile(r"\+?\s*1\s*GB", re.I)
# Exakter Button-Text (das "+" ist im Portal ein Icon, im Text steht nur "1 GB")
PLUS_1GB_EXAKT = re.compile(r"^\s*\+?\s*1\s*GB\s*$", re.I)
DIALOG_BTN = re.compile(r"Best(ä|ae)tigen|Nachbuchen|Kostenpflichtig|^\s*Ja\s*$", re.I)

# Diagnose: listet alle Elemente (inkl. offener Shadow-DOMs) mit "1 GB"
# im Text oder in label/aria-label/title sowie alle Buttons.
JS_DIAG = r"""
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

  const desc = el => el.tagName
    + ' | class=' + (el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className)
    + ' | text=' + norm(el.textContent).slice(0, 60)
    + ' | label=' + (el.getAttribute('label') || '')
    + ' | aria=' + (el.getAttribute('aria-label') || '')
    + ' | title=' + (el.getAttribute('title') || '');

  const treffer = nodes.filter(el => {
    const t = norm(el.textContent);
    const attrs = (el.getAttribute('label') || '') + ' ' + (el.getAttribute('aria-label') || '')
      + ' ' + (el.getAttribute('title') || '');
    return (t.length <= 60 && /1\s*GB/i.test(t)) || /1\s*GB/i.test(attrs);
  });

  const btns = nodes.filter(el =>
    el.matches && el.matches('button, one-button, a, [role="button"]')
  );

  return {
    treffer: treffer.length,
    liste: treffer.slice(0, 15).map(desc),
    buttons: btns.slice(0, 40).map(desc),
  };
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


async def diagnose(page, titel=""):
    """Loggt gefundene Kandidaten und Buttons in allen Frames."""
    for i, frame in enumerate(page.frames):
        try:
            info = await frame.evaluate(JS_DIAG)
        except Exception as e:
            log(f"Frame {i}: Diagnose nicht moeglich ({e})")
            continue
        log(f"{titel}Frame {i} ({frame.url[:80]}): Elemente mit '1 GB': {info['treffer']}")
        for z in info["liste"]:
            log("  TREFFER: " + z)
        for z in info["buttons"][:25]:
            log("  BTN: " + z)


async def finde_button(page, timeout_s=20):
    """Sucht den '+1 GB'-Button in allen Frames, wartet bis timeout_s."""
    for _ in range(timeout_s * 2):
        for frame in page.frames:
            kandidaten = (
                # 0. Exakter Treffer: <one-button> mit Text "1 GB" (so im Portal)
                frame.locator("one-button").filter(has_text=PLUS_1GB_EXAKT),
                # 1. echte Buttons mit passendem Namen
                frame.get_by_role("button", name=PLUS_1GB),
                # 2. Button-artige Elemente mit passendem Text
                frame.locator("one-button, button, a, [role=button]").filter(has_text=PLUS_1GB),
                # 3. Label/aria-label/title-Attribute (Web Components)
                frame.locator(
                    '[label*="1 GB"], [aria-label*="1 GB"], [title*="1 GB"], '
                    '[label*="1GB"], [aria-label*="1GB"]'
                ),
                # 4. Notnagel: irgendein Element mit passendem Text
                frame.get_by_text(PLUS_1GB),
            )
            for loc in kandidaten:
                try:
                    if await loc.count():
                        return loc.first
                except Exception:
                    continue
        await page.wait_for_timeout(500)
    return None


async def klick_per_position(page):
    """Fallback: Der runde +1-GB-Button sitzt rechts neben/ueber dem Label 'Inland'
    des ersten Tachos (~72 px rechts, ~14 px ueber dem Label-Mittelpunkt)."""
    label = page.get_by_text("Inland", exact=True).first
    if not await label.count():
        return False
    await label.scroll_into_view_if_needed()
    box = await label.bounding_box()
    if not box:
        return False
    x = box["x"] + box["width"] / 2 + 72
    y = box["y"] + box["height"] / 2 - 14
    await page.mouse.click(x, y)
    log(f"Fallback-Klick per Position ({x:.0f},{y:.0f}).")
    return True


async def seite_zeigt_bestellung(page):
    seite = await page.locator("body").inner_text()
    return "Bestellung erhalten" in seite or "eingegangen" in seite


async def durchlauf():
    log(f"Skript-Version: {SCRIPT_VERSION}")
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

            # SPA laden lassen
            try:
                await page.wait_for_load_state("networkidle", timeout=20_000)
            except Exception:
                pass
            await page.wait_for_timeout(5000)

            gb, text = await restvolumen(page)
            log(f"Restvolumen: {gb} GB" if gb is not None else "Restvolumen nicht erkannt")

            await diagnose(page)

            if "Bestellung erhalten" in text or "eingegangen" in text:
                log("Nachbuchung ist schon unterwegs - warte.")
                return "pending"

            if gb is not None and gb > SCHWELLE_GB:
                log(f"Ueber Schwelle ({SCHWELLE_GB} GB) - nichts zu tun.")
                return "ok"

            btn = await finde_button(page)

            if DRY_RUN:
                if btn is not None:
                    try:
                        log("DRY_RUN: Button WUERDE geklickt: "
                            + (await btn.evaluate("el => el.outerHTML"))[:300])
                    except Exception:
                        log("DRY_RUN: Button gefunden (outerHTML nicht lesbar).")
                else:
                    log("DRY_RUN: Kein Button per Locator gefunden (Positions-Fallback wuerde greifen).")
                log("DRY_RUN aktiv - kein Klick.")
                return "dryrun"

            if btn is not None:
                try:
                    log("Button gefunden: " + (await btn.evaluate("el => el.outerHTML"))[:200])
                except Exception:
                    pass
                await btn.scroll_into_view_if_needed()
                try:
                    await btn.click(timeout=10_000)
                except Exception as e:
                    log(f"Normaler Klick fehlgeschlagen ({e}) - versuche force/JS-Klick.")
                    try:
                        await btn.click(force=True, timeout=5000)
                    except Exception:
                        await btn.evaluate("el => el.click()")
                log("+1 GB per Locator angeklickt.")
            else:
                log("Kein Button per Locator gefunden - versuche Positions-Fallback.")
                if not await klick_per_position(page):
                    log("Weder Button noch 'Inland'-Label gefunden. Siehe TREFFER/BTN und debug.html.")
                    return "unclear"

            await page.wait_for_timeout(3000)
            gebucht = await seite_zeigt_bestellung(page)

            # Evtl. Bestaetigungsdialog
            if not gebucht:
                for frame in page.frames:
                    try:
                        dlg = frame.get_by_role("button", name=DIALOG_BTN)
                        if await dlg.count():
                            await dlg.last.click(timeout=4000)
                            log("Dialog bestaetigt.")
                            await page.wait_for_timeout(3000)
                            gebucht = await seite_zeigt_bestellung(page)
                            break
                    except Exception:
                        continue

            if gebucht:
                log("ERFOLG: 1 GB wurde nachgebucht.")
                notify("ALDI TALK: gratis 1 GB automatisch nachgebucht.")
                return "booked"

            log("Kein Erfolg erkennbar - Zustand nach dem Klick:")
            await diagnose(page, titel="NACH KLICK: ")
            log("Siehe auch debug.png und debug.html.")
            return "unclear"

        except Exception as e:
            log(f"Fehler: {e}")
            return "error"
        finally:
            if DEBUG_FILES:
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
