"""Vision-Verified Interaction Loop — Three-Layer Architecture.

Layers:
  BrowserController  – human-like input with post-type verification + SoM overlays
  IntelligenceLayer  – NVIDIA NIM dual-model (Nemotron text + Llama-3.2 vision)
  CaptchaSolver      – CapSolver with reCAPTCHA Enterprise / pageAction support
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

# ── NVIDIA NIM keys (env vars take priority; fall back to hard-coded defaults) ──
_NIM_TEXT_KEY   = os.environ.get("NVIDIA_NIM_TEXT_KEY",
                  "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")
_NIM_VISION_KEY = os.environ.get("NVIDIA_NIM_VISION_KEY",
                  "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")

_NIM_BASE_URL   = "https://integrate.api.nvidia.com/v1"
_VISION_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

_TEXT_MODEL    = "nvidia/nemotron-super-49b-v1"   # production text model
_VISION_MODEL  = "meta/llama-3.2-11b-vision-instruct"


# ─────────────────────────────────────────────────────────────────────────────
# SoM overlay JS — draws numbered red boxes over every interactable element
# ─────────────────────────────────────────────────────────────────────────────
_SOM_INJECT_JS = """
() => {
    // Remove any previous overlays
    document.querySelectorAll('.__som_overlay__').forEach(e => e.remove());

    const SELECTORS = 'input:not([type=hidden]), button, select, textarea, ' +
                      '[role="button"], [role="combobox"], [role="listbox"], ' +
                      '[role="option"], [role="checkbox"], [role="radio"], a[href]';
    const elements  = document.querySelectorAll(SELECTORS);
    const map       = {};
    let   id        = 1;

    elements.forEach(el => {
        const rect = el.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) return;
        if (rect.top < -200 || rect.top > window.innerHeight + 200) return;

        const box = document.createElement('div');
        box.className = '__som_overlay__';
        box.setAttribute('data-som-id', id);
        box.style.cssText = [
            'position:fixed',
            'left:'   + Math.round(rect.left)   + 'px',
            'top:'    + Math.round(rect.top)     + 'px',
            'width:'  + Math.round(rect.width)   + 'px',
            'height:' + Math.round(rect.height)  + 'px',
            'border:2px solid #e00',
            'background:rgba(220,0,0,0.08)',
            'pointer-events:none',
            'z-index:2147483647',
            'box-sizing:border-box',
        ].join(';');

        const badge = document.createElement('span');
        badge.textContent = id;
        badge.style.cssText = [
            'position:absolute',
            'top:-14px',
            'left:0',
            'background:#e00',
            'color:#fff',
            'font:bold 10px/14px monospace',
            'padding:0 3px',
            'border-radius:2px',
            'white-space:nowrap',
        ].join(';');
        box.appendChild(badge);
        document.body.appendChild(box);

        map[id] = {
            tag:         el.tagName.toLowerCase(),
            type:        el.type        || '',
            id:          el.id          || '',
            name:        el.name        || '',
            placeholder: el.placeholder || '',
            ariaLabel:   el.getAttribute('aria-label') || '',
            text:        (el.innerText || el.value || '').trim().slice(0, 60),
            role:        el.getAttribute('role') || '',
        };
        id++;
    });
    return JSON.stringify(map);
}
"""

_SOM_REMOVE_JS = """
() => { document.querySelectorAll('.__som_overlay__').forEach(e => e.remove()); }
"""


# ─────────────────────────────────────────────────────────────────────────────
# BrowserController
# ─────────────────────────────────────────────────────────────────────────────
class BrowserController:
    """Wraps a Playwright page with human-centric interactions and vision fallback."""

    def __init__(self, page):
        self.page = page
        self._som_map: dict[int, dict] = {}

    # ── Core: type with verification ────────────────────────────────────────

    def type_with_verification(self, element, text: str, retries: int = 2) -> bool:
        """Click → keyboard.type → verify → JS-fallback if empty.

        Args:
            element: Playwright ElementHandle or Locator (.first is called for Locators).
            text:    The string to type.
            retries: Number of times to retry the keyboard.type path before JS fallback.

        Returns True if the field has the expected value after typing, False if all attempts fail.
        """
        page = self.page

        # Normalise: if it's a Locator, unwrap to ElementHandle
        try:
            if hasattr(element, "first"):
                element = element.first
        except Exception:
            pass

        for attempt in range(retries + 1):
            try:
                # 1. Scroll into view & click
                try:
                    element.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                element.click(timeout=3000)
                time.sleep(random.uniform(0.05, 0.15))

                # 2. Clear existing content
                try:
                    element.select_all()
                except Exception:
                    try:
                        element.fill("")
                    except Exception:
                        pass

                # 3. Type with human-like per-key delay
                delay_ms = random.uniform(50, 150)
                page.keyboard.type(text, delay=delay_ms)
                time.sleep(random.uniform(0.15, 0.25))

                # 4. Verify: read back value or innerText
                actual = element.evaluate(
                    "el => (el.value !== undefined ? el.value : el.innerText || '').trim()"
                )
                if actual:
                    log.debug("[BrowserController] type_verify OK (attempt %d): '%s'", attempt, text[:40])
                    return True

                log.debug("[BrowserController] type_verify empty (attempt %d) — retrying", attempt)

            except Exception as exc:
                log.debug("[BrowserController] type attempt %d error: %s", attempt, exc)

        # 5. JS fallback: set value directly via React-compatible setter
        log.debug("[BrowserController] falling back to JS value injection for '%s'", text[:40])
        try:
            safe_text = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
            injected = element.evaluate(f"""el => {{
                const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value') ||
                             Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');
                if (desc && desc.set) {{
                    desc.set.call(el, '{safe_text}');
                }} else {{
                    el.value = '{safe_text}';
                }}
                ['input', 'change', 'blur'].forEach(evt =>
                    el.dispatchEvent(new Event(evt, {{ bubbles: true }}))
                );
                return (el.value || el.innerText || '').trim();
            }}""")
            if injected:
                log.debug("[BrowserController] JS injection succeeded: '%s'", text[:40])
                return True
        except Exception as exc:
            log.warning("[BrowserController] JS fallback failed: %s", exc)

        return False

    # ── SoM helpers ─────────────────────────────────────────────────────────

    def inject_som_overlay(self) -> dict[int, dict]:
        """Overlay numbered red boxes on all interactable elements.

        Returns a dict mapping SoM ID → element metadata.
        """
        try:
            raw = self.page.evaluate(_SOM_INJECT_JS)
            self._som_map = {int(k): v for k, v in json.loads(raw).items()}
            log.debug("[BrowserController] SoM: %d elements annotated", len(self._som_map))
        except Exception as exc:
            log.warning("[BrowserController] SoM inject error: %s", exc)
            self._som_map = {}
        return self._som_map

    def remove_som_overlay(self) -> None:
        """Remove all SoM overlays from the page."""
        try:
            self.page.evaluate(_SOM_REMOVE_JS)
        except Exception:
            pass

    def take_annotated_screenshot(self, path: str | Path) -> str:
        """Inject SoM, take screenshot, remove SoM. Returns base64-encoded PNG."""
        self.inject_som_overlay()
        time.sleep(0.3)  # let overlay render
        try:
            self.page.screenshot(path=str(path), full_page=False)
        finally:
            self.remove_som_overlay()
        with open(str(path), "rb") as f:
            return base64.b64encode(f.read()).decode()

    # ── Vision-fallback click ────────────────────────────────────────────────

    def try_click_vision_fallback(
        self,
        selector: str,
        description: str,
        intelligence: "IntelligenceLayer | None" = None,
        screenshot_path: str | Path | None = None,
    ) -> bool:
        """Try page.locator(selector). On failure, use vision model + SoM to find & click.

        Args:
            selector:     Playwright selector to attempt first.
            description:  Human-readable description of the element (for vision model).
            intelligence: IntelligenceLayer instance. If None, vision fallback is skipped.
            screenshot_path: Where to save the annotated screenshot.

        Returns True if the element was clicked.
        """
        page = self.page

        # ── Path 1: Normal selector ──────────────────────────────────────────
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=2000):
                loc.click(timeout=5000)
                log.debug("[BrowserController] click OK via selector: %s", selector[:60])
                return True
        except Exception as exc:
            log.debug("[BrowserController] selector click failed (%s): %s", selector[:40], exc)

        if intelligence is None:
            return False

        # ── Path 2: Vision model + SoM fallback ─────────────────────────────
        tmp_path = Path(screenshot_path or f"/tmp/som_fallback_{int(time.time())}.png")
        try:
            b64 = self.take_annotated_screenshot(tmp_path)
            som_id = intelligence.find_element_id(b64, description, self._som_map)
            if som_id is None:
                log.debug("[BrowserController] vision fallback: element '%s' not found", description)
                return False

            # Click via the element metadata: try by DOM query using id/name attrs
            meta = self._som_map.get(som_id, {})
            clicked = False
            for attr_sel in _meta_to_selectors(meta):
                try:
                    el = page.locator(attr_sel).first
                    if el.is_visible(timeout=800):
                        el.click(timeout=3000)
                        log.info("[BrowserController] vision fallback clicked SoM#%d (%s): %s",
                                 som_id, description[:30], attr_sel[:50])
                        clicked = True
                        break
                except Exception:
                    pass

            if not clicked:
                # Last resort: click by screen coordinates from bounding box
                try:
                    bb_result = page.evaluate(f"""
                        () => {{
                            const box = document.querySelector('[data-som-id="{som_id}"]');
                            if (!box) return null;
                            const r = box.getBoundingClientRect();
                            return {{ x: r.left + r.width/2, y: r.top + r.height/2 }};
                        }}
                    """)
                    if bb_result:
                        page.mouse.click(bb_result["x"], bb_result["y"])
                        log.info("[BrowserController] vision fallback coordinate-click SoM#%d", som_id)
                        clicked = True
                except Exception as exc:
                    log.warning("[BrowserController] coordinate click error: %s", exc)

            return clicked

        except Exception as exc:
            log.warning("[BrowserController] vision fallback error: %s", exc)
            return False


def _meta_to_selectors(meta: dict) -> list[str]:
    """Build candidate CSS selectors from SoM element metadata."""
    sels = []
    tag  = meta.get("tag", "")
    eid  = meta.get("id", "")
    name = meta.get("name", "")
    ph   = meta.get("placeholder", "")
    al   = meta.get("ariaLabel", "")
    role = meta.get("role", "")

    if eid:
        sels.append(f"#{eid}")
    if name:
        sels.append(f"{tag}[name='{name}']" if tag else f"[name='{name}']")
    if al:
        sels.append(f"[aria-label='{al}']")
    if ph:
        sels.append(f"[placeholder='{ph}']")
    if role:
        sels.append(f"[role='{role}']")
    return sels


# ─────────────────────────────────────────────────────────────────────────────
# IntelligenceLayer
# ─────────────────────────────────────────────────────────────────────────────
class IntelligenceLayer:
    """NVIDIA NIM dual-model layer.

    Text model  (Nemotron)        → field mapping from accessibility tree.
    Vision model (Llama-3.2-11b) → SoM navigation + red-line error scan.
    """

    def __init__(
        self,
        text_api_key: str  = _NIM_TEXT_KEY,
        vision_api_key: str = _NIM_VISION_KEY,
    ):
        self._text_key   = text_api_key
        self._vision_key = vision_api_key

    # ── Text model: field mapping via Nemotron ───────────────────────────────

    def map_fields(
        self,
        form_fields: list[dict],
        profile_data: dict,
    ) -> dict[str, str]:
        """Use Nemotron to map profile_data onto form_fields.

        Args:
            form_fields:  List of dicts with keys: label, type, options (optional), required.
            profile_data: Flat dict of candidate data.

        Returns dict mapping field_label → value_to_fill.
        """
        if not form_fields:
            return {}

        system_prompt = (
            "You are a job-application form-filling assistant. "
            "Given a list of form fields and a candidate profile, return ONLY a valid JSON object "
            'mapping each field label to the best matching value. '
            "Never fabricate data. If no match exists, omit the field. "
            "For yes/no or select fields, use exact option text.\n"
            "RESPONSE FORMAT: {\"fields\": {\"Field Label\": \"value\", ...}}"
        )
        user_msg = (
            f"Form fields:\n{json.dumps(form_fields, indent=2)}\n\n"
            f"Candidate profile:\n{json.dumps(profile_data, indent=2)}"
        )

        raw = self._call_text_model(system_prompt, user_msg, max_tokens=2048)
        if not raw:
            return {}

        # Strip markdown fences if present
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw).strip()

        try:
            parsed = json.loads(raw)
            result = parsed.get("fields", parsed)
            log.info("[IntelligenceLayer] Nemotron mapped %d field(s)", len(result))
            return {str(k): str(v) for k, v in result.items() if v not in (None, "", "null")}
        except Exception as exc:
            log.warning("[IntelligenceLayer] map_fields JSON parse error: %s | raw=%s", exc, raw[:200])
            return {}

    # ── Vision model: find SoM element ID ───────────────────────────────────

    def find_element_id(
        self,
        screenshot_b64: str,
        description: str,
        som_map: dict[int, dict] | None = None,
    ) -> int | None:
        """Look at the SoM-annotated screenshot and return the ID of the described element.

        Args:
            screenshot_b64: Base64-encoded PNG of the annotated screenshot.
            description:    e.g. "the Submit button" or "First Name input"
            som_map:        Optional SoM map to include element hints in the prompt.

        Returns integer SoM ID or None if not found.
        """
        hint = ""
        if som_map:
            # Give the model a compact text summary of visible elements
            summaries = []
            for sid, meta in list(som_map.items())[:60]:
                label = (meta.get("ariaLabel") or meta.get("placeholder") or
                         meta.get("text") or meta.get("name") or "")
                if label:
                    summaries.append(f"#{sid}:{meta.get('tag','')} \"{label[:30]}\"")
            if summaries:
                hint = "\n\nVisible elements:\n" + ", ".join(summaries[:40])

        prompt = (
            f"Look at this annotated screenshot. Each interactive element has a red box with a number.\n"
            f"Find: {description}{hint}\n\n"
            f"Reply with ONLY the integer ID number of that element, nothing else. "
            f"If you cannot find it, reply with 0."
        )

        raw = self._call_vision_model(prompt, screenshot_b64)
        if not raw:
            return None

        # Parse integer from response
        match = re.search(r"\b(\d+)\b", raw.strip())
        if match:
            val = int(match.group(1))
            return val if val > 0 else None
        return None

    # ── Vision model: red-line error scan ───────────────────────────────────

    def scan_for_errors(self, screenshot_b64: str) -> list[str]:
        """Use vision model to find red validation error text on the page.

        Returns a list of error message strings (empty list if none found).
        """
        prompt = (
            "Look at this screenshot of a job application form. "
            "Identify any red-colored validation error messages or field-level error text. "
            "List each error message exactly as it appears, one per line. "
            "If there are no error messages, reply with: NO_ERRORS"
        )
        raw = self._call_vision_model(prompt, screenshot_b64)
        if not raw or "NO_ERRORS" in raw.upper():
            return []
        lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
        return [ln for ln in lines if len(ln) < 300]

    # ── Internal: call text model (Nemotron) ─────────────────────────────────

    def _call_text_model(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> str:
        """Stream Nemotron response and return assembled text."""
        try:
            from openai import OpenAI
            client = OpenAI(base_url=_NIM_BASE_URL, api_key=self._text_key)
            completion = client.chat.completions.create(
                model=_TEXT_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_message},
                ],
                temperature=temperature,
                top_p=0.95,
                max_tokens=max_tokens,
                stream=True,
            )
            parts: list[str] = []
            for chunk in completion:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                # Skip reasoning tokens (thinking budget)
                if getattr(delta, "reasoning_content", None):
                    continue
                if delta.content:
                    parts.append(delta.content)
            return "".join(parts).strip()
        except Exception as exc:
            log.warning("[IntelligenceLayer] text model error: %s", exc)
            return ""

    # ── Internal: call vision model (Llama-3.2-11b-vision) ──────────────────

    def _call_vision_model(
        self,
        prompt: str,
        image_b64: str,
        max_tokens: int = 512,
    ) -> str:
        """Send prompt + base64 image to Llama-3.2-vision via NVIDIA NIM."""
        headers = {
            "Authorization": f"Bearer {self._vision_key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        payload = {
            "model": _VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_b64}",
                            },
                        },
                    ],
                }
            ],
            "max_tokens":        max_tokens,
            "temperature":       0.2,
            "top_p":             1.0,
            "frequency_penalty": 0.0,
            "presence_penalty":  0.0,
            "stream":            True,
        }
        try:
            resp = requests.post(_VISION_API_URL, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            parts: list[str] = []
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace")
                if line.startswith("data: "):
                    line = line[6:]
                if line.strip() in ("[DONE]", ""):
                    continue
                try:
                    obj = json.loads(line)
                    delta = obj.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        parts.append(content)
                except Exception:
                    pass
            return "".join(parts).strip()
        except Exception as exc:
            log.warning("[IntelligenceLayer] vision model error: %s", exc)
            return ""


# ─────────────────────────────────────────────────────────────────────────────
# CaptchaSolver — enhanced with enterprise reCAPTCHA + pageAction/userAgent
# ─────────────────────────────────────────────────────────────────────────────
class CaptchaSolver:
    """CapSolver-backed CAPTCHA solver with enterprise reCAPTCHA support."""

    def __init__(self, api_key: str | None = None):
        self._key = api_key or os.environ.get("CAPSOLVER_API_KEY", "")

    def solve(self, page, page_url: str, max_attempts: int = 3) -> bool:
        """Detect and solve the CAPTCHA on `page`.

        Supports: hCaptcha, reCAPTCHA v2, reCAPTCHA v2 Enterprise, Turnstile.
        Extracts pageAction and userAgent for Enterprise tasks.
        Returns True if solved and token injected, False otherwise.
        """
        if not self._key:
            log.warning("[CaptchaSolver] CAPSOLVER_API_KEY not set")
            return False

        info = self._detect(page)
        if not info:
            log.warning("[CaptchaSolver] No CAPTCHA detected or sitekey not found — check page DOM")
            return False

        ctype   = info["type"]
        sitekey = info["sitekey"]
        log.info("[CaptchaSolver] Detected %s (sitekey=%s...)", ctype, sitekey[:12])

        for attempt in range(1, max_attempts + 1):
            token = self._request_token(page, page_url, ctype, sitekey, info)
            if token:
                self._inject_token(page, ctype, token)
                log.info("[CaptchaSolver] Token injected (attempt %d, %d chars)", attempt, len(token))
                return True
            log.debug("[CaptchaSolver] Attempt %d failed — retrying", attempt)
            time.sleep(3)

        log.warning("[CaptchaSolver] All %d attempts failed for %s", max_attempts, ctype)
        return False

    # ── Detection ────────────────────────────────────────────────────────────

    def _detect(self, page) -> dict | None:
        """Return captcha metadata dict or None if not found/extractable.
        
        Retries up to 3 times with 2s waits to handle CAPTCHAs rendered in modals
        that appear after a short delay (e.g. Lever hCaptcha).
        """
        for _attempt in range(3):
            result = self._detect_once(page)
            if result:
                return result
            if _attempt < 2:
                time.sleep(2)
        return None

    def _detect_once(self, page) -> dict | None:
        """Single detection pass — called by _detect() with retries."""
        try:
            return page.evaluate("""() => {
                // ── hCaptcha (div container) ─────────────────────────────────
                const hcDiv = document.querySelector(
                    '.h-captcha[data-sitekey], [data-hcaptcha-widget-id], div[data-sitekey*="hcaptcha"]'
                );
                if (hcDiv) {
                    const sk = hcDiv.getAttribute('data-sitekey') || '';
                    if (sk) return { type: 'hcaptcha', sitekey: sk, pageAction: '', isInvisible: false };
                }
                // ── hCaptcha (iframe — new URL format: newassets.hcaptcha.com) ─
                const hcFrame = document.querySelector('iframe[src*="hcaptcha.com"]');
                if (hcFrame) {
                    // Try query param first (legacy format)
                    try {
                        const u  = new URL(hcFrame.src);
                        const sk = u.searchParams.get('sitekey') || '';
                        if (sk) return { type: 'hcaptcha', sitekey: sk, pageAction: '', isInvisible: false };
                    } catch(e) {}
                    // New format: sitekey is in parent widget div or inline script
                    const parent = hcFrame.closest('[data-sitekey]');
                    if (parent) {
                        const sk = parent.getAttribute('data-sitekey') || '';
                        if (sk) return { type: 'hcaptcha', sitekey: sk, pageAction: '', isInvisible: false };
                    }
                    // Scan inline scripts for hcaptcha.render({sitekey: '...'})
                    for (const s of document.querySelectorAll('script:not([src])')) {
                        const m = s.textContent.match(/hcaptcha[\s\S]{0,200}sitekey[\s]*[=:\s]+['"]([a-f0-9\-]{36})['"]/);
                        if (m) return { type: 'hcaptcha', sitekey: m[1], pageAction: '', isInvisible: false };
                    }
                }

                // ── reCAPTCHA v2 / Enterprise ────────────────────────────────
                const rcDiv = document.querySelector('.g-recaptcha[data-sitekey]');
                if (rcDiv) {
                    const sk        = rcDiv.getAttribute('data-sitekey') || '';
                    const action    = rcDiv.getAttribute('data-action')  || '';
                    const invisible = rcDiv.getAttribute('data-size') === 'invisible';
                    return { type: 'recaptchav2', sitekey: sk, pageAction: action, isInvisible: invisible };
                }

                const rcFrameSels = [
                    'iframe[src*="recaptcha/api2"]',
                    'iframe[src*="recaptcha/enterprise"]',
                    'iframe[src*="google.com/recaptcha"]',
                ];
                for (const fSel of rcFrameSels) {
                    const rcFrame = document.querySelector(fSel);
                    if (!rcFrame) continue;
                    try {
                        const u          = new URL(rcFrame.src);
                        const sk         = u.searchParams.get('k') || u.searchParams.get('sitekey') || '';
                        const action     = u.searchParams.get('action') || '';
                        const isEnterprise = rcFrame.src.includes('enterprise');
                        const invisible  = u.searchParams.get('size') === 'invisible';
                        if (sk) return {
                            type:        isEnterprise ? 'recaptchav2enterprise' : 'recaptchav2',
                            sitekey:     sk,
                            pageAction:  action,
                            isInvisible: invisible,
                        };
                    } catch(e) {}
                }

                // ── reCAPTCHA Enterprise — script-tag extraction ─────────────
                const scripts = Array.from(document.querySelectorAll('script:not([src])'));
                for (const s of scripts) {
                    const skRe = new RegExp('[\'"]sitekey[\'"][ \\t]*:[ \\t]*[\'"]([0-9A-Za-z_-]{20,80})[\'"]');
                    const m1 = s.textContent.match(skRe);
                    if (m1) {
                        const acRe = new RegExp('action[ \\t]*:[ \\t]*[\'"]([a-zA-Z0-9/_-]{1,80})[\'"]');
                        const m2 = s.textContent.match(acRe);
                        return {
                            type:        'recaptchav2enterprise',
                            sitekey:     m1[1],
                            pageAction:  m2 ? m2[1] : '',
                            isInvisible: true,
                        };
                    }
                    const m3 = s.textContent.match(/grecaptcha[.]execute[(]['"]([0-9A-Za-z_-]{20,80})['"]/);
                    if (m3) {
                        const acRe2 = new RegExp('action[ \\t]*:[ \\t]*[\'"]([a-zA-Z0-9/_-]{1,80})[\'"]');
                        const m4 = s.textContent.match(acRe2);
                        return {
                            type:        'recaptchav2enterprise',
                            sitekey:     m3[1],
                            pageAction:  m4 ? m4[1] : '',
                            isInvisible: true,
                        };
                    }
                }

                // ── Cloudflare Turnstile (div container) ─────────────────────
                const cfDiv = document.querySelector(
                    '.cf-turnstile[data-sitekey], [data-cf-turnstile-sitekey], [data-sitekey*="0x"]'
                );
                if (cfDiv) return {
                    type:        'turnstile',
                    sitekey:     cfDiv.getAttribute('data-sitekey') || cfDiv.getAttribute('data-cf-turnstile-sitekey') || '',
                    pageAction:  cfDiv.getAttribute('data-action')  || '',
                    isInvisible: false,
                };

                // ── Cloudflare Turnstile (iframe-based) ───────────────────────
                for (const f of document.querySelectorAll('iframe')) {
                    const src = (f.src || '').toLowerCase();
                    if (src.includes('challenges.cloudflare.com') || (src.includes('turnstile') && src.includes('sitekey'))) {
                        try {
                            const u = new URL(f.src);
                            const sk = u.searchParams.get('sitekey') || u.searchParams.get('k') || '';
                            if (sk) return { type: 'turnstile', sitekey: sk, pageAction: '', isInvisible: false };
                        } catch(e) {}
                    }
                }

                // ── Turnstile rendered via window.turnstile.render() in scripts ─
                for (const s of document.querySelectorAll('script:not([src])')) {
                    // Matches: turnstile.render('#el', { sitekey: '0x...' })
                    const m1 = s.textContent.match(/turnstile[^)]*sitekey['":\s]+['"](0x[0-9A-Fa-f]{8,})['"]/);
                    if (m1) return { type: 'turnstile', sitekey: m1[1], pageAction: '', isInvisible: false };
                    // Matches explicit 0x-prefixed sitekey anywhere in inline script
                    const m2 = s.textContent.match(/['"]sitekey['"]\s*[=:,]\s*['"]( 0x[0-9A-Fa-f]{8,})['"]/);
                    if (m2) return { type: 'turnstile', sitekey: m2[1].trim(), pageAction: '', isInvisible: false };
                }

                // ── Arkose FunCaptcha ─────────────────────────────────────────
                const arkoseFrame = document.querySelector('iframe[src*="arkoselabs"], iframe[src*="funcaptcha"]');
                if (arkoseFrame) {
                    try {
                        const u = new URL(arkoseFrame.src);
                        const pk = u.searchParams.get('pk') || u.searchParams.get('muid') || '';
                        if (pk) return { type: 'funcaptcha', sitekey: pk, pageAction: '', isInvisible: false };
                    } catch(e) {}
                }

                return null;
            }""")
        except Exception as exc:
            log.warning("[CaptchaSolver] detect error: %s", exc)
            return None

    # ── Token request ─────────────────────────────────────────────────────────

    def _request_token(
        self,
        page,
        page_url: str,
        ctype: str,
        sitekey: str,
        info: dict,
    ) -> str | None:
        """Submit task to CapSolver and poll for the token. Returns token string or None."""
        import urllib.request as _req
        import urllib.error   as _uerr

        # Collect userAgent from the browser
        try:
            user_agent = page.evaluate("() => navigator.userAgent")
        except Exception:
            user_agent = ""

        page_action  = info.get("pageAction", "") or ""
        is_invisible = info.get("isInvisible", False)

        type_map = {
            "hcaptcha":             "HCaptchaTaskProxyless",
            "recaptchav2":          "ReCaptchaV2TaskProxyless",
            "recaptchav2enterprise":"ReCaptchaV2EnterpriseTaskProxyless",
            "recaptchav3":          "ReCaptchaV3TaskProxyless",
            "turnstile":            "AntiTurnstileTaskProxyless",
            "funcaptcha":           "FunCaptchaTaskProxyless",
        }

        task: dict[str, Any] = {
            "type":       type_map.get(ctype, "HCaptchaTaskProxyless"),
            "websiteURL": page_url,
            "websiteKey": sitekey,
        }
        if user_agent:
            task["userAgent"] = user_agent
        if is_invisible:
            task["isInvisible"] = True
        # Enterprise reCAPTCHA: pass pageAction if available
        if ctype in ("recaptchav2enterprise", "recaptchav3") and page_action:
            task["pageAction"] = page_action

        def _post(url: str, body: dict) -> dict:
            data = json.dumps(body).encode()
            req  = _req.Request(url, data=data, headers={"Content-Type": "application/json"})
            return json.loads(_req.urlopen(req, timeout=25).read())

        try:
            resp = _post("https://api.capsolver.com/createTask",
                         {"clientKey": self._key, "task": task})
            if resp.get("errorId", 0) != 0:
                err = resp.get("errorDescription", str(resp))
                # Auto-retry with isInvisible if CapSolver tells us to
                if "invisible" in err.lower() and not task.get("isInvisible"):
                    task["isInvisible"] = True
                    resp = _post("https://api.capsolver.com/createTask",
                                 {"clientKey": self._key, "task": task})
                    if resp.get("errorId", 0) != 0:
                        log.warning("[CaptchaSolver] createTask error: %s",
                                    resp.get("errorDescription", resp))
                        return None
                else:
                    log.warning("[CaptchaSolver] createTask error: %s", err)
                    return None

            task_id = resp["taskId"]
            log.info("[CaptchaSolver] Task %s created — polling…", task_id)

            for _ in range(36):   # max ~180 s
                time.sleep(5)
                result = _post("https://api.capsolver.com/getTaskResult",
                               {"clientKey": self._key, "taskId": task_id})
                if result.get("errorId", 0) != 0:
                    log.warning("[CaptchaSolver] poll error: %s",
                                result.get("errorDescription", result))
                    return None
                if result.get("status") == "ready":
                    sol = result.get("solution", {})
                    token = (sol.get("gRecaptchaResponse") or sol.get("token") or
                             sol.get("userAgent") or "")
                    return token or None

            log.warning("[CaptchaSolver] timeout waiting for task %s", task_id)
            return None

        except (_uerr.URLError, Exception) as exc:
            log.warning("[CaptchaSolver] request error: %s", exc)
            return None

    # ── Token injection ───────────────────────────────────────────────────────

    def _inject_token(self, page, ctype: str, token: str) -> None:
        """Inject the solved token back into the page DOM."""
        try:
            page.evaluate("""([token, ctype]) => {
                if (ctype === 'hcaptcha') {
                    const ta = document.querySelector('textarea[name="h-captcha-response"]');
                    if (ta) {
                        ta.value = token;
                        ta.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    try {
                        if (window.hcaptcha && window.hcaptcha.submit) window.hcaptcha.submit();
                    } catch(e) {}
                    return;
                }
                if (ctype === 'recaptchav2' || ctype === 'recaptchav2enterprise' || ctype === 'recaptchav3') {
                    const ta = document.getElementById('g-recaptcha-response');
                    if (ta) {
                        // Make hidden textarea visible briefly, set value
                        const origDisplay = ta.style.display;
                        ta.style.display = 'block';
                        ta.innerHTML = token;
                        ta.dispatchEvent(new Event('change', { bubbles: true }));
                        ta.style.display = origDisplay;
                    }
                    // Fire registered callbacks
                    try {
                        const widget = document.querySelector('.g-recaptcha');
                        if (widget) {
                            const cb = widget.getAttribute('data-callback');
                            if (cb && window[cb]) window[cb](token);
                        }
                    } catch(e) {}
                    // Fire all ___grecaptcha_cfg callbacks (v2 enterprise)
                    try {
                        if (typeof ___grecaptcha_cfg !== 'undefined') {
                            Object.values(___grecaptcha_cfg.clients || {}).forEach(client => {
                                Object.values(client || {}).forEach(v => {
                                    if (v && v.callback && typeof v.callback === 'function') {
                                        try { v.callback(token); } catch(e) {}
                                    }
                                });
                            });
                        }
                    } catch(e) {}
                    return;
                }
                if (ctype === 'turnstile') {
                    const inp = document.querySelector('[name="cf-turnstile-response"]');
                    if (inp) {
                        inp.value = token;
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    try {
                        const cfDiv = document.querySelector('.cf-turnstile');
                        if (cfDiv) {
                            const cb = cfDiv.getAttribute('data-callback');
                            if (cb && window[cb]) window[cb](token);
                    } catch(e) {}
                }
                
                // Fallback: wait 1000ms then try to submit the parent form or click a visible submit button
                // This ensures external forms proceed even if JS callbacks are obscured or missing
                setTimeout(() => {
                    try {
                        const widget = document.querySelector('.cf-turnstile, .g-recaptcha, .h-captcha');
                        if (widget) {
                            const parentForm = widget.closest('form');
                            if (parentForm) {
                                parentForm.submit();
                                return;
                            }
                        }
                        const submitBtn = document.querySelector('input[type="submit"], button[type="submit"], button:contains("Apply")');
                        if (submitBtn) submitBtn.click();
                    } catch(e) {}
                }, 1000);
            }""", [token, ctype])
        except Exception as exc:
            log.warning("[CaptchaSolver] inject error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Vision-verified form fill (integrates all three layers)
# ─────────────────────────────────────────────────────────────────────────────

def vision_verified_fill(
    page,
    flat_profile: dict,
    intelligence: IntelligenceLayer,
    browser: BrowserController,
    captcha: CaptchaSolver,
    page_url: str,
) -> tuple[int, list[str]]:
    """One form-fill pass using the vision-verified interaction loop.

    Strategy:
      1. Collect all visible inputs via accessibility tree.
      2. Use Nemotron to map labels → values (primary path).
      3. Fill each field with type_with_verification.
      4. After filling, check for error text via red-line scan.
      5. Return (fields_filled, error_messages).

    Returns (filled_count, list_of_visible_errors).
    """
    filled  = 0
    errors: list[str] = []

    # ── Step 1: Collect visible input metadata from the page ─────────────────
    try:
        raw_fields = page.evaluate("""() => {
            const SELS = 'input:not([type=hidden]):not([type=file]):not([type=submit]):not([type=button]),' +
                         'select, textarea';
            const results = [];
            document.querySelectorAll(SELS).forEach(el => {
                if (!el.offsetParent) return;
                const rect = el.getBoundingClientRect();
                if (rect.width < 2 || rect.height < 2) return;

                // Label resolution: aria-label > label[for] > DOM-walk > placeholder
                let label = el.getAttribute('aria-label') || '';
                if (!label && el.id) {
                    const lbl = document.querySelector('label[for="' + el.id + '"]');
                    if (lbl) {
                        const clone = lbl.cloneNode(true);
                        clone.querySelectorAll('input,select,textarea,button').forEach(n => n.remove());
                        label = (clone.innerText || clone.textContent || '').trim();
                    }
                }
                if (!label) {
                    let node = el.parentElement;
                    for (let i=0; i<5 && node; i++, node=node.parentElement) {
                        const lbl = node.querySelector('label');
                        if (lbl) { label = (lbl.innerText||lbl.textContent||'').trim(); break; }
                    }
                }
                if (!label) label = el.placeholder || el.name || el.id || '';

                const options = [];
                if (el.tagName === 'SELECT') {
                    el.querySelectorAll('option').forEach(o => {
                        const t = (o.innerText||'').trim();
                        if (t && t.toLowerCase() !== 'select' && t !== '--') options.push(t);
                    });
                }

                results.push({
                    label:    label.slice(0, 80),
                    type:     el.type || el.tagName.toLowerCase(),
                    required: el.required || el.getAttribute('aria-required') === 'true',
                    options:  options.slice(0, 30),
                    current:  (el.value || '').trim(),
                });
            });
            return results;
        }""")
    except Exception as exc:
        log.warning("[vision_verified_fill] field scan error: %s", exc)
        raw_fields = []

    visible_fields = [f for f in (raw_fields or []) if f.get("label")]
    if not visible_fields:
        log.debug("[vision_verified_fill] no labelled fields found on page")
        return 0, []

    # ── Step 2: Nemotron maps labels → values ─────────────────────────────────
    nim_map = intelligence.map_fields(visible_fields, flat_profile)

    # ── Step 3: Fill each field with type_with_verification ──────────────────
    for field_info in visible_fields:
        label   = field_info["label"]
        current = field_info.get("current", "")
        ftype   = field_info.get("type", "text").lower()

        value = nim_map.get(label) or nim_map.get(label.lower())
        if not value:
            continue
        if current and current.lower() == str(value).lower():
            continue   # already correct — skip

        try:
            # Locate the element by label text (best-effort)
            loc = page.get_by_label(re.compile(re.escape(label), re.IGNORECASE)).first

            if ftype == "select" or ftype in ("combobox",):
                # Native <select>: use select_option
                try:
                    loc.select_option(label=value, timeout=1000)
                    filled += 1
                    log.debug("[fill] select '%s' → '%s'", label[:40], value[:30])
                except Exception:
                    # Fallback to type_with_verification (custom dropdowns)
                    if browser.type_with_verification(loc, value):
                        filled += 1
            else:
                if browser.type_with_verification(loc, str(value)):
                    filled += 1
                    log.debug("[fill] '%s' → '%s'", label[:40], str(value)[:30])
        except Exception as exc:
            log.debug("[fill] error for '%s': %s", label[:40], exc)

    # ── Step 4: Red-line error scan (vision) ────────────────────────────────
    try:
        ss_path = Path(f"/tmp/hireagent_redline_{int(time.time())}.png")
        page.screenshot(path=str(ss_path), full_page=False)
        with open(str(ss_path), "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        errors = intelligence.scan_for_errors(b64)
        if errors:
            log.warning("[vision_verified_fill] Red-line scan found %d error(s): %s",
                        len(errors), errors[:3])
    except Exception as exc:
        log.debug("[vision_verified_fill] red-line scan error: %s", exc)

    return filled, errors


def find_submit_button_vision(
    page,
    browser: BrowserController,
    intelligence: IntelligenceLayer,
) -> bool:
    """Find and click the Submit button using selector-first, then vision fallback.

    If Submit is disabled after all fields are filled, triggers a red-line scan
    to report which field is failing.

    Returns True if Submit was clicked.
    """
    # ── Primary: text/selector-based search ──────────────────────────────────
    submit_selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:text-matches('submit application', 'i')",
        "button:text-matches('submit', 'i')",
        "button:text-matches('apply now', 'i')",
        "button:text-matches('apply', 'i')",
        "button:text-matches('send application', 'i')",
        "[data-automation-id='bottom-navigation-finish-button']",
        "[data-automation-id='wd-CommandButton_uic_submitAction']",
    ]
    for sel in submit_selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1000):
                disabled = loc.get_attribute("disabled")
                aria_dis = loc.get_attribute("aria-disabled")
                if disabled is not None or aria_dis == "true":
                    log.warning("[find_submit] Submit button found but disabled: %s", sel[:50])
                    # Red-line scan to find what's failing
                    try:
                        ss_path = Path(f"/tmp/hireagent_disabled_{int(time.time())}.png")
                        page.screenshot(path=str(ss_path), full_page=False)
                        with open(str(ss_path), "rb") as f:
                            b64 = base64.b64encode(f.read()).decode()
                        errs = intelligence.scan_for_errors(b64)
                        if errs:
                            log.warning("[find_submit] Red-line errors blocking Submit: %s", errs)
                    except Exception:
                        pass
                    continue   # try next selector — maybe there's an enabled one
                loc.click(timeout=5000)
                log.info("[find_submit] Submit clicked via selector: %s", sel[:50])
                return True
        except Exception:
            pass

    # ── Vision fallback: SoM screenshot → Llama-3.2 finds the button ─────────
    log.info("[find_submit] Using vision fallback to locate Submit button")
    return browser.try_click_vision_fallback(
        selector="button[type='submit'], input[type='submit']",
        description="the Submit / Apply Now button that will send the application",
        intelligence=intelligence,
        screenshot_path=Path(f"/tmp/hireagent_submit_som_{int(time.time())}.png"),
    )
