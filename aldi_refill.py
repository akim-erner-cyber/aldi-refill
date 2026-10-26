import os
import re
import sys
import time
from datetime import datetime
import urllib.request
import json
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==============================================================================
# KONFIGURATION & UMGEBUNGSVARIABLEN
# ==============================================================================
SCRIPT_VERSION = "v6-shadowdom-fix"

# Unterstützt verschiedene gängige Namen für die GitHub Secrets / Env-Variablen
PHONE = (
    os.getenv("ALDI_PHONE")
    or os.getenv("ALDITALK_PHONE")
    or os.getenv("PHONE_NUMBER")
    or os.getenv("ALDI_USER")
    or os.getenv("USERNAME")
    or ""
).strip()

PASSWORD = (
    os.getenv("ALDI_PASSWORD")
    or os.getenv("ALDITALK_PASSWORD")
    or os.getenv("PASSWORD")
    or os.getenv("ALDI_PASS")
    or ""
).strip()

DISCORD_WEBHOOK = (
    os.getenv("DISCORD_WEBHOOK")
    or os.getenv("DISCORD_WEBHOOK_URL")
    or os.getenv("WEBHOOK_URL")
    or ""
).strip()

# Schwellenwert in GB, unter dem nachgebucht werden soll (z. B. 1.05 für ~1024 MB)
THRESHOLD_GB = float(os.getenv("THRESHOLD_GB", "1.05"))
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

LOGIN_URL = "https://www.alditalk-kundenportal.de/portal/auth/uebersicht/"


# ==============================================================================
# HILFSFUNKTIONEN
# ==============================================================================
def log(msg: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def send_discord(message: str):
    if not DISCORD_WEBHOOK:
        return
    try:
        data = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "AldiRefillBot/1.0"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Discord-Fehler: {e}")


def dismiss_cookies(page):
    """Schließt das Usercentrics Cookie-Banner, falls vorhanden."""
    try:
        page.evaluate("""() => {
            // 1. Direkt im DOM suchen
            const buttons = Array.from(document.querySelectorAll('button'));
            const accept = buttons.find(b => /akzeptieren|alle akzeptieren|zustimmen|accept all/i.test(b.innerText || ''));
            if (accept) { accept.click(); return true; }

            // 2. Im Usercentrics Shadow DOM suchen
            const uc = document.querySelector('#usercentrics-root');
            if (uc && uc.shadowRoot) {
                const ucBtns = Array.from(uc.shadowRoot.querySelectorAll('button'));
                const ucAccept = ucBtns.find(b => /akzeptieren|alle akzeptieren|zustimmen|accept all/i.test(b.innerText || ''));
                if (ucAccept) { ucAccept.click(); return true; }
            }
            return false;
        }""")
    except Exception:
        pass


def parse_remaining_gb(page) -> float | None:
    """Liest das verbleibende Inlands-Datenvolumen in GB aus."""
    try:
        text = page.evaluate("""() => {
            const meters = Array.from(document.querySelectorAll('one-usage-meter, one-stack.usage-meter'));
            for (const m of meters) {
                const t = m.innerText || m.textContent || '';
                if (t.includes('Inland') && !t.includes('Inland & EU')) {
                    return t;
                }
            }
            return document.body.innerText || '';
        }""")

        # Suche nach Muster wie "Noch 1021,9 MB von 25 GB" oder "Noch 4,5 GB von 25 GB"
        match = re.search(r"Noch\s*([\d.,]+)\s*(MB|GB)", text, re.IGNORECASE)
        if match:
            val_str = match.group(1).replace(".", "").replace(",", ".")
            unit = match.group(2).upper()
            val = float(val_str)
            if unit == "MB":
                return val / 1024.0
            return val
    except Exception as e:
        log(f"Fehler beim Auslesen des Volumens: {e}")
    return None


def dump_debug_state(page, prefix=""):
    """Gibt Debug-Informationen zu allen relevanten Elementen aus."""
    p = f"{prefix}: " if prefix else ""
    for i, frame in enumerate(page.frames):
        try:
            hits = frame.evaluate("""() => {
                const res = [];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    const txt = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (txt.includes('1 GB') && ['ONE-STACK', 'ONE-USAGE-METER', 'ONE-BUTTON', 'ONE-TEXT', 'BUTTON'].includes(el.tagName)) {
                        res.push({
                            tag: el.tagName,
                            cls: el.className || '',
                            text: txt.substring(0, 80),
                            aria: el.getAttribute('aria-label') || ''
                        });
                    }
                }
                return res;
            }""")
            log(f"{p}Frame {i} ({frame.url}): Elemente mit '1 GB': {len(hits)}")
            for h in hits:
                log(f"  TREFFER: {h['tag']} | class={h['cls']} | text={h['text']} | aria={h['aria']}")
        except Exception:
            pass


def click_plus_1gb(page) -> dict:
    """
    Sucht den '+1 GB' / '1 GB' Button im Inland-Meter und klickt ihn
    sowohl über Playwright als auch direkt im Shadow DOM an.
    """
    # 1. Status über JS im Shadow DOM prüfen und klicken
    result = page.evaluate("""() => {
        // Suche alle one-button Elemente
        const oneButtons = Array.from(document.querySelectorAll('one-button'));
        
        // Filtere nach dem Button, der "1 GB" oder "+1 GB" enthält
        const target = oneButtons.find(btn => {
            const txt = (btn.innerText || btn.textContent || '').replace(/\\s+/g, ' ').trim();
            return txt === '1 GB' || txt === '+1 GB' || txt.includes('1 GB');
        });

        if (!target) {
            return { found: false, reason: 'Kein <one-button> mit 1 GB gefunden' };
        }

        const outerHtml = target.outerHTML.substring(0, 220);
        target.scrollIntoView({ behavior: 'instant', block: 'center' });

        // Prüfe auf inneren Button im Shadow DOM
        let innerBtn = null;
        if (target.shadowRoot) {
            innerBtn = target.shadowRoot.querySelector('button, a, [role="button"]');
        }

        const isDisabled = (innerBtn && innerBtn.disabled) || target.hasAttribute('disabled') || target.getAttribute('aria-disabled') === 'true';

        // Klick-Events feuern
        const eventOpts = { bubbles: true, cancelable: true, composed: true, view: window };
        
        if (innerBtn) {
            innerBtn.focus();
            innerBtn.dispatchEvent(new PointerEvent('pointerdown', eventOpts));
            innerBtn.dispatchEvent(new MouseEvent('mousedown', eventOpts));
            innerBtn.dispatchEvent(new PointerEvent('pointerup', eventOpts));
            innerBtn.dispatchEvent(new MouseEvent('mouseup', eventOpts));
            innerBtn.click();
        }
        
        target.dispatchEvent(new PointerEvent('pointerdown', eventOpts));
        target.dispatchEvent(new MouseEvent('mousedown', eventOpts));
        target.dispatchEvent(new PointerEvent('pointerup', eventOpts));
        target.dispatchEvent(new MouseEvent('mouseup', eventOpts));
        target.click();

        return {
            found: true,
            outerHtml: outerHtml,
            hasShadowButton: !!innerBtn,
            disabled: isDisabled
        };
    }""")

    # 2. Zusätzlich echten Playwright-Mausklick auf die Koordinaten des Buttons ausführen
    try:
        loc = page.locator("one-usage-meter one-button, one-button[slot='action']").filter(has_text="1 GB").first
        if loc.count() > 0 and loc.is_visible():
            box = loc.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                log("Playwright-Mausklick auf Koordinaten von <one-button> ausgeführt.")
            else:
                loc.click(force=True)
    except Exception as e:
        log(f"Hinweis beim zusätzlichen Locator-Klick: {e}")

    return result


# ==============================================================================
# HAUPTPROGRAMM
# ==============================================================================
def main():
    log(f"Skript-Version: {SCRIPT_VERSION}")

    if not PHONE or not PASSWORD:
        log("FEHLER: Zugangsdaten (Telefonnummer oder Passwort) fehlen in den Umgebungsvariablen!")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="de-DE",
            timezone_id="Europe/Berlin",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        try:
            log("Login laeuft ...")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            dismiss_cookies(page)

            # falls wir nicht eingeloggt sind -> Login-Formular ausfüllen
            if "uebersicht" not in page.url or page.locator("input[type='password']").count() > 0:
                dismiss_cookies(page)

                # Rufnummer-Feld suchen (unterstützt normale Inputs & Shadow DOM von <one-input>)
                user_filled = False
                for sel in [
                    "input[name*='phone' i]",
                    "input[name*='user' i]",
                    "input[id*='phone' i]",
                    "input[id*='user' i]",
                    "input[type='tel']",
                    "input[type='text']",
                ]:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.fill(PHONE)
                        user_filled = True
                        break

                if not user_filled:
                    # Fallback via JS für Web Components
                    page.evaluate(
                        """(phone) => {
                        const inputs = Array.from(document.querySelectorAll('input'));
                        const tel = inputs.find(i => i.type === 'tel' || i.type === 'text');
                        if (tel) {
                            tel.focus();
                            tel.value = phone;
                            tel.dispatchEvent(new Event('input', { bubbles: true }));
                            tel.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }""",
                        PHONE,
                    )

                # Passwort-Feld ausfüllen
                pw_loc = page.locator("input[type='password']").first
                pw_loc.wait_for(state="visible", timeout=15000)
                pw_loc.fill(PASSWORD)

                # Login-Button klicken
                submit_loc = page.locator(
                    "button[type='submit'], one-button[type='submit'], button:has-text('Anmelden'), one-button:has-text('Anmelden'), button:has-text('Login')"
                ).first
                if submit_loc.count() > 0:
                    submit_loc.click()
                else:
                    pw_loc.press("Enter")

                # Warten bis die Übersicht geladen ist
                page.wait_for_url("**/uebersicht**", timeout=30000)

            # Warten bis das Usage-Meter mit den Verbrauchsdaten geladen ist
            page.wait_for_selector("one-usage-meter, one-stack.usage-meter", timeout=30000)
            page.wait_for_timeout(4000)
            dismiss_cookies(page)
            log("Login OK.")

            # Aktuelles Datenvolumen auslesen
            rem_gb = parse_remaining_gb(page)
            log(f"Restvolumen: {rem_gb} GB")

            if rem_gb is not None and rem_gb >= THRESHOLD_GB:
                log(f"Noch genug Datenvolumen vorhanden ({rem_gb:.2f} GB >= {THRESHOLD_GB} GB). Kein Nachbuchen nötig.")
                sys.exit(0)

            # Debug-Zustand vor dem Klick
            dump_debug_state(page)

            # +1 GB Button suchen und klicken
            click_info = click_plus_1gb(page)
            if not click_info.get("found"):
                log(f"Button nicht gefunden: {click_info.get('reason')}")
                page.screenshot(path="debug.png", full_page=True)
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                sys.exit(1)

            log(f"Button gefunden: {click_info.get('outerHtml')}")
            log(f"Shadow-Button vorhanden: {click_info.get('hasShadowButton')} | Disabled: {click_info.get('disabled')}")

            if click_info.get("disabled"):
                log("HINWEIS: Der +1 GB Button ist seitens ALDI TALK noch deaktiviert (disabled=True), da das Volumen noch über der echten 1-GB-Grenze liegt.")
                sys.exit(0)

            log("+1 GB im Shadow-DOM & per Mausklick ausgelöst. Warte auf Reaktion ...")
            page.wait_for_timeout(3000)

            # Prüfen, ob ein Bestätigungs-Popup / Modal erschienen ist und ggf. bestätigen
            modal_clicked = page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('one-button, button'));
                const confirmBtn = btns.find(b => {
                    const txt = (b.innerText || b.textContent || '').trim();
                    return /^(jetzt buchen|bestätigen|ja|kostenpflichtig buchen|buchen|ok)$/i.test(txt);
                });
                if (confirmBtn) {
                    const inner = confirmBtn.shadowRoot ? confirmBtn.shadowRoot.querySelector('button') : null;
                    if (inner) inner.click();
                    confirmBtn.click();
                    return txt;
                }
                return null;
            }""")
            if modal_clicked:
                log(f"Bestätigungs-Dialog erkannt und geklickt: {modal_clicked}")
                page.wait_for_timeout(3000)

            # Warten und Seite neu laden, damit das Portal den neuen Stand vom Server holt
            page.wait_for_timeout(5000)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("one-usage-meter, one-stack.usage-meter", timeout=30000)
            page.wait_for_timeout(4000)

            # Erfolg prüfen
            new_gb = parse_remaining_gb(page)
            log(f"Restvolumen nach Reload: {new_gb} GB")

            # Prüfen, ob das Volumen gestiegen ist oder der +1 GB Button verschwunden ist
            still_has_btn = page.locator("one-usage-meter one-button").filter(has_text="1 GB").count() > 0
import os
import re
import sys
import time
from datetime import datetime
import urllib.request
import json
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==============================================================================
# KONFIGURATION & UMGEBUNGSVARIABLEN
# ==============================================================================
SCRIPT_VERSION = "v6-shadowdom-fix"

# Unterstützt verschiedene gängige Namen für die GitHub Secrets / Env-Variablen
PHONE = (
    os.getenv("ALDI_PHONE")
    or os.getenv("ALDITALK_PHONE")
    or os.getenv("PHONE_NUMBER")
    or os.getenv("ALDI_USER")
    or os.getenv("USERNAME")
    or ""
).strip()

PASSWORD = (
    os.getenv("ALDI_PASSWORD")
    or os.getenv("ALDITALK_PASSWORD")
    or os.getenv("PASSWORD")
    or os.getenv("ALDI_PASS")
    or ""
).strip()

DISCORD_WEBHOOK = (
    os.getenv("DISCORD_WEBHOOK")
    or os.getenv("DISCORD_WEBHOOK_URL")
    or os.getenv("WEBHOOK_URL")
    or ""
).strip()

# Schwellenwert in GB, unter dem nachgebucht werden soll (z. B. 1.05 für ~1024 MB)
THRESHOLD_GB = float(os.getenv("THRESHOLD_GB", "1.05"))
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

LOGIN_URL = "https://www.alditalk-kundenportal.de/portal/auth/uebersicht/"


# ==============================================================================
# HILFSFUNKTIONEN
# ==============================================================================
def log(msg: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def send_discord(message: str):
    if not DISCORD_WEBHOOK:
        return
    try:
        data = json.dumps({"content": message}).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "AldiRefillBot/1.0"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Discord-Fehler: {e}")


def dismiss_cookies(page):
    """Schließt das Usercentrics Cookie-Banner, falls vorhanden."""
    try:
        page.evaluate("""() => {
            // 1. Direkt im DOM suchen
            const buttons = Array.from(document.querySelectorAll('button'));
            const accept = buttons.find(b => /akzeptieren|alle akzeptieren|zustimmen|accept all/i.test(b.innerText || ''));
            if (accept) { accept.click(); return true; }

            // 2. Im Usercentrics Shadow DOM suchen
            const uc = document.querySelector('#usercentrics-root');
            if (uc && uc.shadowRoot) {
                const ucBtns = Array.from(uc.shadowRoot.querySelectorAll('button'));
                const ucAccept = ucBtns.find(b => /akzeptieren|alle akzeptieren|zustimmen|accept all/i.test(b.innerText || ''));
                if (ucAccept) { ucAccept.click(); return true; }
            }
            return false;
        }""")
    except Exception:
        pass


def parse_remaining_gb(page) -> float | None:
    """Liest das verbleibende Inlands-Datenvolumen in GB aus."""
    try:
        text = page.evaluate("""() => {
            const meters = Array.from(document.querySelectorAll('one-usage-meter, one-stack.usage-meter'));
            for (const m of meters) {
                const t = m.innerText || m.textContent || '';
                if (t.includes('Inland') && !t.includes('Inland & EU')) {
                    return t;
                }
            }
            return document.body.innerText || '';
        }""")

        # Suche nach Muster wie "Noch 1021,9 MB von 25 GB" oder "Noch 4,5 GB von 25 GB"
        match = re.search(r"Noch\s*([\d.,]+)\s*(MB|GB)", text, re.IGNORECASE)
        if match:
            val_str = match.group(1).replace(".", "").replace(",", ".")
            unit = match.group(2).upper()
            val = float(val_str)
            if unit == "MB":
                return val / 1024.0
            return val
    except Exception as e:
        log(f"Fehler beim Auslesen des Volumens: {e}")
    return None


def dump_debug_state(page, prefix=""):
    """Gibt Debug-Informationen zu allen relevanten Elementen aus."""
    p = f"{prefix}: " if prefix else ""
    for i, frame in enumerate(page.frames):
        try:
            hits = frame.evaluate("""() => {
                const res = [];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    const txt = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (txt.includes('1 GB') && ['ONE-STACK', 'ONE-USAGE-METER', 'ONE-BUTTON', 'ONE-TEXT', 'BUTTON'].includes(el.tagName)) {
                        res.push({
                            tag: el.tagName,
                            cls: el.className || '',
                            text: txt.substring(0, 80),
                            aria: el.getAttribute('aria-label') || ''
                        });
                    }
                }
                return res;
            }""")
            log(f"{p}Frame {i} ({frame.url}): Elemente mit '1 GB': {len(hits)}")
            for h in hits:
                log(f"  TREFFER: {h['tag']} | class={h['cls']} | text={h['text']} | aria={h['aria']}")
        except Exception:
            pass


def click_plus_1gb(page) -> dict:
    """
    Sucht den '+1 GB' / '1 GB' Button im Inland-Meter und klickt ihn
    sowohl über Playwright als auch direkt im Shadow DOM an.
    """
    # 1. Status über JS im Shadow DOM prüfen und klicken
    result = page.evaluate("""() => {
        // Suche alle one-button Elemente
        const oneButtons = Array.from(document.querySelectorAll('one-button'));
        
        // Filtere nach dem Button, der "1 GB" oder "+1 GB" enthält
        const target = oneButtons.find(btn => {
            const txt = (btn.innerText || btn.textContent || '').replace(/\\s+/g, ' ').trim();
            return txt === '1 GB' || txt === '+1 GB' || txt.includes('1 GB');
        });

        if (!target) {
            return { found: false, reason: 'Kein <one-button> mit 1 GB gefunden' };
        }

        const outerHtml = target.outerHTML.substring(0, 220);
        target.scrollIntoView({ behavior: 'instant', block: 'center' });

        // Prüfe auf inneren Button im Shadow DOM
        let innerBtn = null;
        if (target.shadowRoot) {
            innerBtn = target.shadowRoot.querySelector('button, a, [role="button"]');
        }

        const isDisabled = (innerBtn && innerBtn.disabled) || target.hasAttribute('disabled') || target.getAttribute('aria-disabled') === 'true';

        // Klick-Events feuern
        const eventOpts = { bubbles: true, cancelable: true, composed: true, view: window };
        
        if (innerBtn) {
            innerBtn.focus();
            innerBtn.dispatchEvent(new PointerEvent('pointerdown', eventOpts));
            innerBtn.dispatchEvent(new MouseEvent('mousedown', eventOpts));
            innerBtn.dispatchEvent(new PointerEvent('pointerup', eventOpts));
            innerBtn.dispatchEvent(new MouseEvent('mouseup', eventOpts));
            innerBtn.click();
        }
        
        target.dispatchEvent(new PointerEvent('pointerdown', eventOpts));
        target.dispatchEvent(new MouseEvent('mousedown', eventOpts));
        target.dispatchEvent(new PointerEvent('pointerup', eventOpts));
        target.dispatchEvent(new MouseEvent('mouseup', eventOpts));
        target.click();

        return {
            found: true,
            outerHtml: outerHtml,
            hasShadowButton: !!innerBtn,
            disabled: isDisabled
        };
    }""")

    # 2. Zusätzlich echten Playwright-Mausklick auf die Koordinaten des Buttons ausführen
    try:
        loc = page.locator("one-usage-meter one-button, one-button[slot='action']").filter(has_text="1 GB").first
        if loc.count() > 0 and loc.is_visible():
            box = loc.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                log("Playwright-Mausklick auf Koordinaten von <one-button> ausgeführt.")
            else:
                loc.click(force=True)
    except Exception as e:
        log(f"Hinweis beim zusätzlichen Locator-Klick: {e}")

    return result


# ==============================================================================
# HAUPTPROGRAMM
# ==============================================================================
def main():
    log(f"Skript-Version: {SCRIPT_VERSION}")

    if not PHONE or not PASSWORD:
        log("FEHLER: Zugangsdaten (Telefonnummer oder Passwort) fehlen in den Umgebungsvariablen!")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="de-DE",
            timezone_id="Europe/Berlin",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        try:
            log("Login laeuft ...")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            dismiss_cookies(page)

            # falls wir nicht eingeloggt sind -> Login-Formular ausfüllen
            if "uebersicht" not in page.url or page.locator("input[type='password']").count() > 0:
                dismiss_cookies(page)

                # Rufnummer-Feld suchen (unterstützt normale Inputs & Shadow DOM von <one-input>)
                user_filled = False
                for sel in [
                    "input[name*='phone' i]",
                    "input[name*='user' i]",
                    "input[id*='phone' i]",
                    "input[id*='user' i]",
                    "input[type='tel']",
                    "input[type='text']",
                ]:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        loc.fill(PHONE)
                        user_filled = True
                        break

                if not user_filled:
                    # Fallback via JS für Web Components
                    page.evaluate(
                        """(phone) => {
                        const inputs = Array.from(document.querySelectorAll('input'));
                        const tel = inputs.find(i => i.type === 'tel' || i.type === 'text');
                        if (tel) {
                            tel.focus();
                            tel.value = phone;
                            tel.dispatchEvent(new Event('input', { bubbles: true }));
                            tel.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }""",
                        PHONE,
                    )

                # Passwort-Feld ausfüllen
                pw_loc = page.locator("input[type='password']").first
                pw_loc.wait_for(state="visible", timeout=15000)
                pw_loc.fill(PASSWORD)

                # Login-Button klicken
                submit_loc = page.locator(
                    "button[type='submit'], one-button[type='submit'], button:has-text('Anmelden'), one-button:has-text('Anmelden'), button:has-text('Login')"
                ).first
                if submit_loc.count() > 0:
                    submit_loc.click()
                else:
                    pw_loc.press("Enter")

                # Warten bis die Übersicht geladen ist
                page.wait_for_url("**/uebersicht**", timeout=30000)

            # Warten bis das Usage-Meter mit den Verbrauchsdaten geladen ist
            page.wait_for_selector("one-usage-meter, one-stack.usage-meter", timeout=30000)
            page.wait_for_timeout(4000)
            dismiss_cookies(page)
            log("Login OK.")

            # Aktuelles Datenvolumen auslesen
            rem_gb = parse_remaining_gb(page)
            log(f"Restvolumen: {rem_gb} GB")

            if rem_gb is not None and rem_gb >= THRESHOLD_GB:
                log(f"Noch genug Datenvolumen vorhanden ({rem_gb:.2f} GB >= {THRESHOLD_GB} GB). Kein Nachbuchen nötig.")
                sys.exit(0)

            # Debug-Zustand vor dem Klick
            dump_debug_state(page)

            # +1 GB Button suchen und klicken
            click_info = click_plus_1gb(page)
            if not click_info.get("found"):
                log(f"Button nicht gefunden: {click_info.get('reason')}")
                page.screenshot(path="debug.png", full_page=True)
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                sys.exit(1)

            log(f"Button gefunden: {click_info.get('outerHtml')}")
            log(f"Shadow-Button vorhanden: {click_info.get('hasShadowButton')} | Disabled: {click_info.get('disabled')}")

            if click_info.get("disabled"):
                log("HINWEIS: Der +1 GB Button ist seitens ALDI TALK noch deaktiviert (disabled=True), da das Volumen noch über der echten 1-GB-Grenze liegt.")
                sys.exit(0)

            log("+1 GB im Shadow-DOM & per Mausklick ausgelöst. Warte auf Reaktion ...")
            page.wait_for_timeout(3000)

            # Prüfen, ob ein Bestätigungs-Popup / Modal erschienen ist und ggf. bestätigen
            modal_clicked = page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('one-button, button'));
                const confirmBtn = btns.find(b => {
                    const txt = (b.innerText || b.textContent || '').trim();
                    return /^(jetzt buchen|bestätigen|ja|kostenpflichtig buchen|buchen|ok)$/i.test(txt);
                });
                if (confirmBtn) {
                    const inner = confirmBtn.shadowRoot ? confirmBtn.shadowRoot.querySelector('button') : null;
                    if (inner) inner.click();
                    confirmBtn.click();
                    return txt;
                }
                return null;
            }""")
            if modal_clicked:
                log(f"Bestätigungs-Dialog erkannt und geklickt: {modal_clicked}")
                page.wait_for_timeout(3000)

            # Warten und Seite neu laden, damit das Portal den neuen Stand vom Server holt
            page.wait_for_timeout(5000)
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("one-usage-meter, one-stack.usage-meter", timeout=30000)
            page.wait_for_timeout(4000)

            # Erfolg prüfen
            new_gb = parse_remaining_gb(page)
            log(f"Restvolumen nach Reload: {new_gb} GB")

            # Prüfen, ob das Volumen gestiegen ist oder der +1 GB Button verschwunden ist
            still_has_btn = page.locator("one-usage-meter one-button").filter(has_text="1 GB").count() > 0

            if (new_gb is not None and rem_gb is not None and new_gb > rem_gb + 0.1) or not still_has_btn:
                log(f"ERFOLG! +1 GB wurde erfolgreich gebucht (Neues Volumen: {new_gb} GB).")
                send_discord(f"✅ **ALDI TALK:** +1 GB erfolgreich nachgebucht! Neues Datenvolumen: **{new_gb:.2f} GB**")
                sys.exit(0)
            else:
                log("Kein Erfolg erkennbar - Zustand nach dem Klick:")
                dump_debug_state(page, prefix="NACH KLICK")
                page.screenshot(path="debug.png", full_page=True)
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                log("Siehe auch debug.png und debug.html.")
                log("Ergebnis: unclear")
                sys.exit(1)

        except Exception as e:
            log(f"Ausnahmefehler im Skript: {e}")
            try:
                page.screenshot(path="debug.png", full_page=True)
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
            except Exception:
                pass
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
            if (new_gb is not None and rem_gb is not None and new_gb > rem_gb + 0.1) or not still_has_btn:
                log(f"ERFOLG! +1 GB wurde erfolgreich gebucht (Neues Volumen: {new_gb} GB).")
                send_discord(f"✅ **ALDI TALK:** +1 GB erfolgreich nachgebucht! Neues Datenvolumen: **{new_gb:.2f} GB**")
                sys.exit(0)
            else:
                log("Kein Erfolg erkennbar - Zustand nach dem Klick:")
                dump_debug_state(page, prefix="NACH KLICK")
                page.screenshot(path="debug.png", full_page=True)
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                log("Siehe auch debug.png und debug.html.")
                log("Ergebnis: unclear")
                sys.exit(1)

        except Exception as e:
            log(f"Ausnahmefehler im Skript: {e}")
            try:
                page.screenshot(path="debug.png", full_page=True)
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
            except Exception:
                pass
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
