import os
import re
import sys
import json
import urllib.request
from datetime import datetime
from playwright.sync_api import sync_playwright

# ==============================================================================
# KONFIGURATION & UMGEBUNGSVARIABLEN
# ==============================================================================
SCRIPT_VERSION = "v11-secret-keys-fix"

# Liest exakt deine GitHub Secrets (ALDI_NUMMER, ALDI_PASSWORT, DISCORD_WEBHOOK_URL)
PHONE = (
    os.getenv("ALDI_NUMMER")
    or os.getenv("ALDI_PHONE")
    or os.getenv("ALDITALK_PHONE")
    or os.getenv("PHONE_NUMBER")
    or os.getenv("ALDI_USER")
    or os.getenv("USERNAME")
    or ""
).strip()

PASSWORD = (
    os.getenv("ALDI_PASSWORT")
    or os.getenv("ALDI_PASSWORD")
    or os.getenv("ALDITALK_PASSWORD")
    or os.getenv("PASSWORD")
    or os.getenv("ALDI_PASS")
    or ""
).strip()

DISCORD_WEBHOOK = (
    os.getenv("DISCORD_WEBHOOK_URL")
    or os.getenv("DISCORD_WEBHOOK")
    or os.getenv("WEBHOOK_URL")
    or os.getenv("DISCORD_URL")
    or ""
).strip()

# Ab 1024 MB (1.0 GB) ist der +1 GB Button bei ALDI TALK klickbar
THRESHOLD_GB = float(os.getenv("THRESHOLD_GB", "1.05"))
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"

LOGIN_URL = "https://www.alditalk-kundenportal.de/portal/auth/uebersicht/"


# ==============================================================================
# HILFSFUNKTIONEN
# ==============================================================================
def log(msg: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def send_discord(title: str, description: str, color: int = 0x2ECC71):
    """Sendet eine formatierte Nachricht an deinen Discord-Webhook."""
    if not DISCORD_WEBHOOK:
        log("HINWEIS: Kein DISCORD_WEBHOOK_URL gesetzt – überspringe Discord-Nachricht.")
        return

    now_str = datetime.now().strftime("%d.%m.%Y um %H:%M:%S Uhr")
    payload = {
        "username": "ALDI TALK Refill Bot",
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": f"{SCRIPT_VERSION} • {now_str}"},
            }
        ],
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            DISCORD_WEBHOOK,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "AldiRefillBot/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            log(f"Discord-Nachricht erfolgreich gesendet (HTTP {resp.status}).")
    except Exception as e:
        log(f"Fehler beim Senden an Discord: {e}")


def dismiss_cookies(page):
    """Schließt das Usercentrics Cookie-Banner oder Hinweise, falls vorhanden."""
    try:
        page.evaluate(
            """() => {
            const uc = document.querySelector('#usercentrics-root');
            if (uc && uc.shadowRoot) {
                const ucBtns = Array.from(uc.shadowRoot.querySelectorAll('button'));
                const ucAccept = ucBtns.find(b => /akzeptieren|alle akzeptieren|zustimmen|accept all/i.test(b.innerText || ''));
                if (ucAccept) { ucAccept.click(); }
            }
            const buttons = Array.from(document.querySelectorAll('button'));
            const accept = buttons.find(b => /akzeptieren|alle akzeptieren|zustimmen|accept all/i.test(b.innerText || ''));
            if (accept) { accept.click(); }
        }"""
        )
    except Exception:
        pass


def parse_remaining_gb(page) -> float | None:
    """Liest das verbleibende Inlands-Datenvolumen in GB (Basis 1024 MB) aus."""
    try:
        text = page.evaluate(
            """() => {
            const meters = Array.from(document.querySelectorAll('one-usage-meter, one-stack.usage-meter'));
            for (const m of meters) {
                const t = m.innerText || m.textContent || '';
                if (t.includes('Inland') && !t.includes('Inland & EU')) {
                    return t;
                }
            }
            return document.body.innerText || '';
        }"""
        )

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


def trigger_1gb_refill(page) -> bool:
    """
    Klickt den +1 GB Button mit 3 kombinierten Methoden an:
    1. Playwright Deep-Locator auf den inneren Button von <one-button>
    2. Echter Mausklick auf die exakten Bildschirmkoordinaten des +1 GB Kreises
    3. JavaScript Shadow-DOM Click-Dispatch
    """
    clicked = False

    try:
        btn_locator = (
            page.locator("one-usage-meter")
            .filter(has_text="Inland")
            .locator("one-button")
            .filter(has_text="1 GB")
            .first
        )
        if btn_locator.count() == 0:
            btn_locator = page.locator("one-button[slot='action']").filter(has_text="1 GB").first

        if btn_locator.count() > 0:
            btn_locator.scroll_into_view_if_needed()
            page.wait_for_timeout(500)

            inner_btn = btn_locator.locator("button").first
            if inner_btn.count() > 0:
                inner_btn.click(force=True)
                log("Methode 1a: Innerer <button> per Playwright-Locator geklickt.")
                clicked = True
            else:
                btn_locator.click(force=True)
                log("Methode 1b: <one-button> per Playwright-Locator geklickt.")
                clicked = True

            box = btn_locator.bounding_box()
            if box:
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                page.mouse.move(cx, cy)
                page.wait_for_timeout(200)
                page.mouse.down()
                page.wait_for_timeout(100)
                page.mouse.up()
                log(f"Methode 2: Echter Mausklick auf Koordinaten ({cx:.1f}, {cy:.1f}) ausgeführt.")
                clicked = True
    except Exception as e:
        log(f"Hinweis bei Methode 1/2: {e}")

    try:
        js_res = page.evaluate(
            """() => {
            const oneButtons = Array.from(document.querySelectorAll('one-button'));
            const target = oneButtons.find(b => (b.innerText || b.textContent || '').includes('1 GB'));
            if (!target) return null;

            const inner = target.shadowRoot ? target.shadowRoot.querySelector('button, a, [role="button"]') : null;
            const el = inner || target;

            const opts = { bubbles: true, cancelable: true, composed: true, view: window };
            el.dispatchEvent(new PointerEvent('pointerdown', opts));
            el.dispatchEvent(new MouseEvent('mousedown', opts));
            el.dispatchEvent(new PointerEvent('pointerup', opts));
            el.dispatchEvent(new MouseEvent('mouseup', opts));
            el.click();

            return target.outerHTML.substring(0, 180);
        }"""
        )
        if js_res:
            log(f"Methode 3: JS Shadow-DOM Klick ausgelöst auf: {js_res}")
            clicked = True
    except Exception as e:
        log(f"Hinweis bei Methode 3: {e}")

    return clicked


# ==============================================================================
# HAUPTPROGRAMM
# ==============================================================================
def main():
    log(f"Skript-Version: {SCRIPT_VERSION}")

    if not PHONE or not PASSWORD:
        log("FEHLER: ALDI_NUMMER oder ALDI_PASSWORT fehlen in den Umgebungsvariablen!")
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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        try:
            log("Login laeuft ...")
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            dismiss_cookies(page)

            # Login-Formular ausfüllen, falls wir noch nicht auf der Übersicht sind
            if "uebersicht" not in page.url or page.locator("input[type='password']:not(.hidden)").count() == 0:
                dismiss_cookies(page)

                # 1. Rufnummer eingeben
                filled_user = False
                for sel in [
                    "input[type='tel']",
                    "input[name*='phone' i]",
                    "input[name*='user' i]",
                    "input[id*='phone' i]",
                    "input[id*='idToken1']",
                    "input[type='text']",
                ]:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        try:
                            loc.fill(PHONE)
                            filled_user = True
                            break
                        except Exception:
                            pass

                if not filled_user:
                    page.evaluate(
                        """(phone) => {
                        const inputs = Array.from(document.querySelectorAll('input'));
                        const tel = inputs.find(i => i.type === 'tel' || i.type === 'text' || i.id.includes('Token1'));
                        if (tel) {
                            tel.focus();
                            tel.value = phone;
                            tel.dispatchEvent(new Event('input', { bubbles: true }));
                            tel.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }""",
                        PHONE,
                    )

                # 2. Auf "Weiter" klicken, damit das Passwortfeld erscheint
                log("Klicke 'Weiter' nach Eingabe der Rufnummer...")
                next_btn = page.locator(
                    "button[type='submit'], one-button[type='submit'], button:has-text('Weiter'), one-button:has-text('Weiter'), button:has-text('Nächste'), one-button:has-text('Nächste')"
                ).first
                if next_btn.count() > 0:
                    next_btn.click(force=True)
                else:
                    page.keyboard.press("Enter")

                page.wait_for_timeout(3000)
                dismiss_cookies(page)

                # 3. Passwort eingeben
                pw_loc = page.locator("input[type='password']:not(.hidden)").first
                pw_loc.wait_for(state="visible", timeout=15000)
                pw_loc.fill(PASSWORD)

                # 4. Anmelden klicken
                submit_btn = page.locator(
                    "button[type='submit'], one-button[type='submit'], button:has-text('Anmelden'), one-button:has-text('Anmelden'), button:has-text('Login')"
                ).first
                if submit_btn.count() > 0:
                    submit_btn.click()
                else:
                    pw_loc.press("Enter")

                page.wait_for_url("**/uebersicht**", timeout=35000)

            # Warten bis die Verbrauchs-Kreise geladen sind
            page.wait_for_selector("one-usage-meter, one-stack.usage-meter", timeout=30000)
            page.wait_for_timeout(4000)
            dismiss_cookies(page)
            log("Login OK.")

            rem_gb = parse_remaining_gb(page)
            log(f"Restvolumen: {rem_gb} GB")

            if rem_gb is not None and rem_gb >= THRESHOLD_GB:
                log(f"Volumen ({rem_gb:.3f} GB) ist noch ueber dem Schwellenwert ({THRESHOLD_GB} GB). Nichts zu tun.")
                sys.exit(0)

            api_responses = []

            def on_response(response):
                if any(k in response.url.lower() for k in ["portal", "api", "book", "refill", "topup", "tariff", "graphql", "rest"]):
                    if response.request.resource_type in ["fetch", "xhr", "document"]:
                        api_responses.append(f"{response.status} {response.request.method} {response.url}")

            page.on("response", on_response)

            # +1 GB Button klicken
            was_clicked = trigger_1gb_refill(page)
            if not was_clicked:
                log("FEHLER: Konnte den +1 GB Button nicht finden oder anklicken.")
                page.screenshot(path="debug.png", full_page=True)
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                sys.exit(1)

            log("Warte 5 Sekunden auf Backend-Reaktion oder Bestaetigungs-Popup ...")
            page.wait_for_timeout(5000)

            modal_btn = page.evaluate(
                """() => {
                const allBtns = Array.from(document.querySelectorAll('one-button, button'));
                for (const b of allBtns) {
                    const t = (b.innerText || b.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (/^(jetzt buchen|bestätigen|ja|kostenpflichtig buchen|buchen|ok|weiter)$/i.test(t)) {
                        const inner = b.shadowRoot ? b.shadowRoot.querySelector('button') : null;
                        if (inner) inner.click();
                        b.click();
                        return t;
                    }
                }
                return null;
            }"""
            )
            if modal_btn:
                log(f"Bestaetigungs-Button gefunden und geklickt: '{modal_btn}'")
                page.wait_for_timeout(5000)

            if api_responses:
                log(f"Erfasste API-Aufrufe nach dem Klick ({len(api_responses)}):")
                for r in api_responses[:10]:
                    log(f"  -> {r}")
            else:
                log("WARNUNG: Kein XHR/Fetch-Netzwerkaufruf direkt nach dem Klick erkannt.")

            # Seite neu laden, um das aktualisierte Datenvolumen zu prüfen
            log("Lade Uebersichtsseite neu, um neues Volumen zu pruefen ...")
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("one-usage-meter, one-stack.usage-meter", timeout=30000)
            page.wait_for_timeout(5000)

            new_gb = parse_remaining_gb(page)
            log(f"Restvolumen nach Reload: {new_gb} GB")

            btn_still_there = (
                page.locator("one-usage-meter")
                .filter(has_text="Inland")
                .locator("one-button")
                .filter(has_text="1 GB")
                .count()
                > 0
            )

            if (new_gb is not None and rem_gb is not None and new_gb > rem_gb + 0.05) or not btn_still_there:
                old_str = f"{rem_gb:.2f} GB ({rem_gb * 1024:.0f} MB)" if rem_gb is not None else "Unbekannt"
                new_str = f"{new_gb:.2f} GB" if new_gb is not None else "+1 GB gebucht"

                log(f"ERFOLG! +1 GB wurde gebucht! (Vorher: {old_str} -> Nachher: {new_str})")

                send_discord(
                    title="✅ +1 GB erfolgreich nachgebucht!",
                    description=(
                        f"Dein **ALDI TALK** Datenvolumen wurde automatisch aufgefüllt.\n\n"
                        f"📉 **Vorher:** `{old_str}`\n"
                        f"📈 **Jetzt:** `{new_str}`"
                    ),
                    color=0x2ECC71,
                )
                sys.exit(0)
            else:
                log("Kein Volumen-Anstieg nach dem Klick erkennbar. Speichere debug.png und debug.html ...")
                page.screenshot(path="debug.png", full_page=True)
                with open("debug.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                sys.exit(1)

        except Exception as e:
            log(f"Ausnahmefehler: {e}")
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
