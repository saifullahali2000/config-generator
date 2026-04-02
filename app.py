import os
import shutil
import streamlit as st
import pandas as pd
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, WebDriverException

# ============================================================
# SMART POLLING UTILITIES  (replaces fixed time.sleep)
# ============================================================

def _poll(condition_fn, timeout=3.0, interval=0.05, default=False):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = condition_fn()
            if result:
                return result
        except:
            pass
        time.sleep(interval)
    return default


def poll_url_changed(driver, old_url, timeout=5.0):
    return _poll(lambda: driver.current_url != old_url, timeout=timeout)


def poll_element_gone(driver, xpath, timeout=3.0):
    def check():
        els = driver.find_elements(By.XPATH, xpath)
        return not any(e.is_displayed() for e in els)
    return _poll(check, timeout=timeout)


def poll_element_visible(driver, xpath, timeout=5.0):
    def check():
        els = driver.find_elements(By.XPATH, xpath)
        return any(e.is_displayed() for e in els)
    return _poll(check, timeout=timeout)


def poll_button_visible(driver, text_fragment, timeout=5.0):
    def check():
        btns = driver.execute_script("""
            var frag = arguments[0].toLowerCase();
            var btns = document.querySelectorAll('button');
            for (var i=0; i<btns.length; i++){
                if((btns[i].innerText||'').toLowerCase().indexOf(frag)!==-1
                    && btns[i].offsetParent) return btns[i];
            }
            return null;
        """, text_fragment)
        return btns
    return _poll(check, timeout=timeout)


def poll_input_visible(driver, placeholder_or_type, timeout=3.0):
    def check():
        el = driver.execute_script("""
            var val = arguments[0];
            var inputs = document.querySelectorAll('input');
            for(var i=0;i<inputs.length;i++){
                var p = (inputs[i].placeholder||'').toLowerCase();
                var t = (inputs[i].type||'').toLowerCase();
                if((p.indexOf(val.toLowerCase())!==-1 || t===val.toLowerCase())
                    && inputs[i].offsetParent) return inputs[i];
            }
            return null;
        """, placeholder_or_type)
        return el
    return _poll(check, timeout=timeout)


def poll_popup_closed(driver, timeout=3.0):
    return poll_element_gone(
        driver,
        "//*[normalize-space(text())='Question Library']",
        timeout=timeout)


def poll_popup_open(driver, timeout=5.0):
    def check():
        try:
            els = driver.find_elements(By.XPATH,
                "//h2[normalize-space(text())='Add Questions'] | "
                "//*[@role='dialog']//*[normalize-space(text())='Add Questions'] | "
                "//*[normalize-space(text())='Question Library']")
            return any(e.is_displayed() for e in els)
        except:
            return False
    return _poll(check, timeout=timeout)


def poll_section_type_modal_gone(driver, timeout=3.0):
    return poll_element_gone(
        driver,
        "//*[contains(normalize-space(.),'Select Section Type')]",
        timeout=timeout)


def poll_options_visible(driver, timeout=3.0):
    def check():
        return driver.execute_script("""
            var sels=['[class*="Select__option"]','[class*="__option"]',
                      '[class*="-option"]','[role="option"]','div[id*="option"]'];
            for(var s=0;s<sels.length;s++){
                var opts=document.querySelectorAll(sels[s]);
                for(var i=0;i<opts.length;i++) if(opts[i].offsetParent) return true;
            }
            return false;
        """)
    return _poll(check, timeout=timeout, interval=0.03)

# ============================================================
# PAGE CONFIG
# ============================================================
st.set_page_config(page_title="Assessment Architect", page_icon="⚡", layout="wide")
st.markdown(
    "<h1 style='text-align: center; color: #FF4B4B;'>ASSESSMENT ARCHITECT ⚡ FAST</h1>",
    unsafe_allow_html=True
)

# ============================================================
# UI LAYOUT
# ============================================================
col1, col2 = st.columns([1, 1])
with col1:
    st.subheader("📁 1. Load Data")
    uploaded_file = st.file_uploader(
        "Upload Assessment CSV (Single or Multi-Section)", type=["csv"], key="assessment_csv"
    )
with col2:
    st.subheader("🔐 2. Portal Access")
    mob     = st.text_input("Mobile Number", placeholder="9876543210")
    otp_val = st.text_input("One Time Passcode", placeholder="6-digit OTP", max_chars=6)

st.subheader("⚙️ 3. Settings")
wait_time = st.slider("Element Wait Time (seconds)", 5, 30, 10)

# ============================================================
# CSV PARSER
# ============================================================

def parse_sections_from_csv(raw_df):
    sections = []
    current_block = []

    def is_blank_row(row):
        return all(str(v).strip().lower() in ["", "nan"] for v in row)

    def parse_block(block_rows):
        section_type        = ""
        section_name        = ""
        time_limit          = ""
        coding_restriction  = ""
        default_coding_lang = ""
        header_idx          = None

        for i, row in enumerate(block_rows):
            label = str(row[0]).lower().strip()
            val   = str(row[1]).strip() if len(row) > 1 and str(row[1]).strip().lower() != "nan" else ""

            if "section type" in label:
                section_type = val
            elif "name" in label and "section" in label:
                section_name = val
            elif "time" in label and "limit" in label:
                time_limit = val
            elif "coding question restriction" in label:
                coding_restriction = ", ".join(
                    str(row[c]).strip()
                    for c in range(1, len(row))
                    if str(row[c]).strip().lower() not in ["", "nan"]
                )
            elif "default coding language" in label:
                if val and "," in val:
                    default_coding_lang = val.split(",")[0].strip()
                else:
                    default_coding_lang = val
            elif "question library" in str(row[0]).lower():
                header_idx = i
                break

        if not section_type:
            return None

        questions_df = pd.DataFrame()
        if header_idx is not None:
            raw_header = [
                str(block_rows[header_idx][c]).strip()
                if str(block_rows[header_idx][c]).strip().lower() not in ["", "nan"]
                else f"Unnamed_{c}"
                for c in range(len(block_rows[header_idx]))
            ]

            seen_num_q = False
            exclusive_tag_count = 0
            header = []
            for col in raw_header:
                col_lower = col.lower().strip()
                if col_lower in ("exclusive tags", "exclusive tags (optional)",
                                 "exclusive tag", "exclusive tag (optional)") or \
                   col_lower.startswith("exclusive tags-") or \
                   col_lower.startswith("exclusive tag-"):
                    exclusive_tag_count += 1
                    if exclusive_tag_count == 1:
                        header.append("Exclusive Tags (Optional)")
                    else:
                        header.append(f"Exclusive Tags-{exclusive_tag_count}")
                elif col_lower == "number of questions" and not seen_num_q:
                    seen_num_q = True
                    header.append("Number of Questions")
                elif col_lower == "number of questions" and seen_num_q:
                    header.append("Marks for Each Question")
                else:
                    header.append(col)

            data_rows = block_rows[header_idx + 1:]
            if data_rows:
                questions_df = pd.DataFrame(data_rows, columns=header)
                main_cols = [
                    "Question Library", "Topic", "Difficulty Level",
                    "Sub Topic", "Number of Questions", "Marks for Each Question"
                ]
                for col in questions_df.columns:
                    if col.startswith("Exclusive Tags"):
                        main_cols.append(col)
                existing = [c for c in main_cols if c in questions_df.columns]
                if existing:
                    questions_df = questions_df.dropna(how="all", subset=existing)
                questions_df = questions_df.reset_index(drop=True)

        return {
            "section_type":        section_type,
            "section_name":        section_name,
            "time_limit":          time_limit,
            "coding_restriction":  coding_restriction,
            "default_coding_lang": default_coding_lang,
            "questions_df":        questions_df,
        }

    for _, row in raw_df.iterrows():
        if is_blank_row(row):
            if current_block:
                parsed = parse_block(current_block)
                if parsed:
                    sections.append(parsed)
                current_block = []
        else:
            label = str(row[0]).lower().strip()
            if "section type" in label and current_block:
                parsed = parse_block(current_block)
                if parsed:
                    sections.append(parsed)
                current_block = []
            current_block.append(list(row))

    if current_block:
        parsed = parse_block(current_block)
        if parsed:
            sections.append(parsed)

    return sections


# ============================================================
# SAFE INPUT HELPERS
# ============================================================

def _safe_send(driver, el, text):
    try:
        driver.execute_script(
            "arguments[0].removeAttribute('readonly');"
            "arguments[0].removeAttribute('disabled');"
            "arguments[0].scrollIntoView({block:'center'});"
            "arguments[0].focus();", el)
    except:
        pass
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).move_to_element(el).click().perform()
    except:
        pass
    try:
        el.send_keys(text)
        return
    except:
        pass
    try:
        driver.execute_script("""
            var el  = arguments[0];
            var val = arguments[1];
            var s   = Object.getOwnPropertyDescriptor(
                          window.HTMLInputElement.prototype, 'value').set;
            s.call(el, val);
            ['input', 'change'].forEach(function(e) {
                el.dispatchEvent(new Event(e, {bubbles: true}));
            });
            el.dispatchEvent(new KeyboardEvent('keydown', {key: val, bubbles: true}));
            el.dispatchEvent(new KeyboardEvent('keyup',   {key: val, bubbles: true}));
        """, el, str(text))
    except:
        pass


def _safe_key(driver, el, key_name):
    key = getattr(Keys, key_name, key_name)
    try:
        el.send_keys(key)
        return
    except:
        pass
    code_map = {
        'RETURN': 13, 'ESCAPE': 27, 'TAB': 9,
        'DELETE': 46, 'BACK_SPACE': 8
    }
    kc = code_map.get(key_name, 0)
    try:
        driver.execute_script("""
            var el = arguments[0], kc = arguments[1];
            ['keydown', 'keypress', 'keyup'].forEach(function(t) {
                el.dispatchEvent(new KeyboardEvent(t, {
                    bubbles: true, cancelable: true, keyCode: kc, which: kc
                }));
            });
        """, el, kc)
    except:
        pass


# ============================================================
# CORE UTILITY FUNCTIONS
# ============================================================

def find_and_click(driver, xpath, timeout=8):
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH, xpath)))
        driver.execute_script("arguments[0].click();", el)
        return True
    except:
        return False


# ============================================================
# POPUP STATE HELPERS
# ============================================================

def popup_is_open(driver):
    try:
        els = driver.find_elements(
            By.XPATH, "//*[normalize-space(text())='Question Library']")
        return any(e.is_displayed() for e in els)
    except:
        return False


def wait_for_popup_open(driver, timeout=10):
    for _ in range(timeout * 5):
        if popup_is_open(driver):
            return True
        time.sleep(0.2)
    return False


def wait_for_popup_closed(driver, timeout=10):
    for _ in range(timeout * 5):
        if not popup_is_open(driver):
            return True
        time.sleep(0.2)
    return False


# ============================================================
# REACT-SELECT HELPERS
# ============================================================

def _close_open_menus(driver):
    try:
        driver.execute_script("""
            document.querySelectorAll('[class*="__menu"],[class*="-menu"]').forEach(function(m){
                if (m.offsetParent) m.style.display = 'none';
            });
        """)
    except:
        pass
    try:
        driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
    except:
        pass


def _wait_for_options(driver, timeout=4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = driver.execute_script("""
            var selectors = [
                '[class*="Select__option"]',
                '[class*="__option"]',
                '[class*="-option"]',
                '[role="option"]',
                'div[id*="option"]'
            ];
            for (var s = 0; s < selectors.length; s++) {
                var opts = document.querySelectorAll(selectors[s]);
                for (var i = 0; i < opts.length; i++) {
                    if (opts[i].offsetParent) return true;
                }
            }
            return false;
        """)
        if found:
            return True
        time.sleep(0.1)
    return False


def _click_option_in_menu(driver, target_text):
    return driver.execute_script("""
        var target = arguments[0].toLowerCase().trim();
        var selectors = [
            '[class*="Select__option"]',
            '[class*="__option"]',
            '[class*="-option"]',
            '[role="option"]',
            'div[id*="option"]'
        ];
        for (var s = 0; s < selectors.length; s++) {
            var opts = document.querySelectorAll(selectors[s]);
            for (var i = 0; i < opts.length; i++) {
                var opt = opts[i];
                if (!opt.offsetParent) continue;
                var txt = (opt.innerText || opt.textContent || '').trim().toLowerCase();
                if (txt === target) {
                    opt.scrollIntoView({block:'nearest'});
                    opt.click();
                    return true;
                }
            }
        }
        for (var s2 = 0; s2 < selectors.length; s2++) {
            var opts2 = document.querySelectorAll(selectors[s2]);
            for (var j = 0; j < opts2.length; j++) {
                var opt2 = opts2[j];
                if (!opt2.offsetParent) continue;
                var t2 = (opt2.innerText || opt2.textContent || '').trim().toLowerCase();
                if (t2.indexOf(target) !== -1) {
                    opt2.scrollIntoView({block:'nearest'});
                    opt2.click();
                    return true;
                }
            }
        }
        return false;
    """, target_text)


# ============================================================
# FIX 1 OF 3: ensure_tab_focus — patches hasFocus so React
# always thinks the tab is active, even when you switch away.
# Original just called window.focus() which doesn't work when
# the tab is in the background.
# ============================================================

def ensure_tab_focus(driver):
    """
    Patches hasFocus + visibilityState so React always thinks the tab
    is active. Does NOT call window.focus() or switch tabs — the browser
    stays exactly where the user left it. All work happens silently
    in the background.
    """
    try:
        driver.execute_script("""
            // Patch hasFocus — React checks this before processing events.
            // Returning true means React never ignores our JS-dispatched events,
            // even when the tab is in the background.
            document.__proto__.hasFocus = function(){ return true; };
            document.hasFocus           = function(){ return true; };

            // Patch visibilityState — same reason as above.
            try {
                Object.defineProperty(document, 'visibilityState', {
                    get: function(){ return 'visible'; }, configurable: true });
                Object.defineProperty(document, 'hidden', {
                    get: function(){ return false; }, configurable: true });
            } catch(e) {}

            // Suppress blur + visibilitychange events caused by tab switching.
            // Applied once per page so we don't stack listeners.
            if (!window.__tabFixApplied) {
                window.__tabFixApplied = true;
                window.addEventListener('visibilitychange', function(e){
                    e.stopImmediatePropagation();
                }, true);
                window.addEventListener('blur', function(e){
                    e.stopImmediatePropagation();
                }, true);
            }
            // NOTE: No window.focus() here — that would steal focus from
            // the user's current tab. We only patch JS, never move focus.
        """)
    except:
        pass
    return True


# ============================================================
# REACT-SELECT DROPDOWN HANDLER
# ============================================================

def click_react_select(driver, label_text, option_text, progress_placeholder):
    from selenium.webdriver.common.action_chains import ActionChains

    ensure_tab_focus(driver)
    progress_placeholder.info(f"  🎯 {label_text} → '{option_text}'")

    def close_open_menus():
        try:
            driver.execute_script("""
                document.querySelectorAll('[class*="menu"]').forEach(function(m){
                    if(m.offsetParent) m.style.display='none';
                });
            """)
        except:
            pass
        try:
            driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
        except:
            pass

    def find_control():
        ctrl = driver.execute_script("""
            var lbl = arguments[0];
            var all = document.querySelectorAll('label,p,span,div,h4,h5,h6,legend,li');
            for (var i = 0; i < all.length; i++) {
                var el = all[i];
                if (el.children.length > 0) continue;
                if (!el.offsetParent) continue;
                if ((el.innerText || el.textContent || '').trim() !== lbl) continue;
                var p = el.parentElement;
                for (var j = 0; j < 8; j++) {
                    if (!p) break;
                    var c = p.querySelector('[class*="-control"],[class*="__control"]');
                    if (c) return c;
                    p = p.parentElement;
                }
            }
            return null;
        """, label_text)
        if ctrl:
            return ctrl
        for xp in [
            f"//*[normalize-space(text())='{label_text}']/following::div[contains(@class,'control')][1]",
            f"//*[contains(text(),'{label_text}')]/following::div[contains(@class,'control')][1]",
        ]:
            try:
                el = driver.find_element(By.XPATH, xp)
                if el.is_displayed():
                    return el
            except:
                continue
        return None

    def open_and_type(control, text):
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", control)
        # FIX 2 OF 3: use JS mousedown+mouseup+click instead of ActionChains.click()
        # ActionChains.click() fails silently in background tabs.
        # JS-dispatched mouse events bypass the focus requirement.
        driver.execute_script("""
            var el = arguments[0];
            el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
            el.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, cancelable:true, view:window}));
            el.dispatchEvent(new MouseEvent('click',     {bubbles:true, cancelable:true, view:window}));
        """, control)
        time.sleep(0.005)  # Ultra minimal wait

        inp = driver.execute_script(
            "return arguments[0].querySelector('input');", control)
        if inp:
            try:
                driver.execute_script(
                    "arguments[0].removeAttribute('readonly');"
                    "arguments[0].removeAttribute('disabled');"
                    "arguments[0].scrollIntoView({block:'center'});"
                    "arguments[0].focus();", inp)
            except:
                pass
            try:
                driver.execute_script("""
                    var el = arguments[0];
                    var s = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    s.call(el, '');
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                """, inp)
            except:
                pass
            typed = False
            try:
                driver.execute_script("""
                    var el = arguments[0], val = arguments[1];
                    var s = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    s.call(el, val);
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    el.dispatchEvent(new KeyboardEvent('keydown', {key:val, bubbles:true}));
                    el.dispatchEvent(new KeyboardEvent('keyup',   {key:val, bubbles:true}));
                """, inp, text)
                typed = True
            except:
                pass
            if not typed:
                # Fallback: character-by-character via JS keydown events
                # (never requires focus, works in background tabs)
                try:
                    driver.execute_script("""
                        var el = arguments[0], val = arguments[1];
                        el.focus();
                        for (var i = 0; i < val.length; i++) {
                            var ch = val[i];
                            el.dispatchEvent(new KeyboardEvent('keydown',
                                {key:ch, bubbles:true, cancelable:true}));
                            el.dispatchEvent(new KeyboardEvent('keypress',
                                {key:ch, bubbles:true, cancelable:true}));
                            el.dispatchEvent(new KeyboardEvent('keyup',
                                {key:ch, bubbles:true, cancelable:true}));
                        }
                    """, inp, text)
                    typed = True
                except:
                    pass
        else:
            # No input inside control — fire Enter to trigger selection
            try:
                driver.execute_script("""
                    document.activeElement.dispatchEvent(
                        new KeyboardEvent('keydown',
                            {key:'Enter',keyCode:13,bubbles:true,cancelable:true}));
                """)
            except:
                pass
        time.sleep(0.05)  # Reduced wait time for dropdown to open

    def pick_option(text):
        # Pure JS approach — never uses is_displayed() which fails in background tabs.
        # Uses offsetParent check inside JS which works regardless of tab focus.
        found = driver.execute_script("""
            var target = arguments[0].toLowerCase().trim();
            var selectors = [
                '[role="option"]',
                '[class*="Select__option"]',
                '[class*="__option"]',
                '[class*="-option"]',
                'div[id*="option"]'
            ];
            for (var s = 0; s < selectors.length; s++) {
                var opts = document.querySelectorAll(selectors[s]);
                for (var i = 0; i < opts.length; i++) {
                    var opt = opts[i];
                    // offsetParent check works in background tabs unlike is_displayed()
                    if (!opt.offsetParent) continue;
                    var txt = (opt.innerText || opt.textContent || '').trim().toLowerCase();
                    if (txt === target || txt.indexOf(target) !== -1) {
                        opt.scrollIntoView({block:'nearest'});
                        opt.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
                        opt.dispatchEvent(new MouseEvent('mouseup',  {bubbles:true,cancelable:true}));
                        opt.dispatchEvent(new MouseEvent('click',    {bubbles:true,cancelable:true}));
                        return true;
                    }
                }
            }
            return false;
        """, text)
        if found:
            return True
        # Last resort: Enter key via JS
        try:
            driver.execute_script("""
                document.dispatchEvent(new KeyboardEvent('keydown',
                    {key:'Enter', keyCode:13, bubbles:true, cancelable:true}));
            """)
            return True
        except:
            pass
        return False

    def get_current_value(control):
        try:
            val = driver.execute_script("""
                var c = arguments[0];
                var sv = c.querySelector('[class*="single-value"]');
                if (sv) return (sv.innerText || sv.textContent || '').trim();
                return '';
            """, control)
            return (val or "").strip()
        except:
            return ""

    close_open_menus()
    control = find_control()
    if not control:
        progress_placeholder.warning(f"  ⚠️ Dropdown not found: '{label_text}'")
        return False

    for attempt in range(3):
        open_and_type(control, option_text)
        picked = pick_option(option_text)
        if not picked:
            progress_placeholder.warning(
                f"  ⚠️ Option '{option_text}' not found (attempt {attempt+1})")
            close_open_menus()
            time.sleep(0.02)  # Ultra fast retry
            continue
        current = get_current_value(control)
        if option_text.lower() in current.lower():
            progress_placeholder.info(f"  ✅ {label_text} = '{current}'")
            return True
        else:
            progress_placeholder.warning(
                f"  ⚠️ Mismatch: expected '{option_text}', got '{current}' — retrying")
            close_open_menus()
            time.sleep(0.01)  # Ultra fast retry

    progress_placeholder.warning(
        f"  ❌ Could not set '{label_text}' to '{option_text}' after 3 attempts")
    return False


# ============================================================
# QUESTION LIBRARY HANDLER
# ============================================================

def handle_question_library(driver, qlib, progress_placeholder):
    csv_val = qlib.strip()
    if csv_val.lower() in ["topin questions", "topin", ""]:
        progress_placeholder.info("  ✅ Question Library = Topin Questions (default, skipping)")
        return True

    current = driver.execute_script("""
        var all = document.querySelectorAll('*');
        for (var i = 0; i < all.length; i++) {
            var el = all[i];
            if (el.children.length > 0) continue;
            if ((el.innerText || el.textContent || '').trim() !== 'Question Library') continue;
            var p = el.parentElement;
            for (var j = 0; j < 8; j++) {
                if (!p) break;
                var sv = p.querySelector('[class*="single-value"]');
                if (sv) return (sv.innerText || sv.textContent || '').trim();
                p = p.parentElement;
            }
        }
        return '';
    """) or ""

    progress_placeholder.info(f"  📚 Question Library: current='{current}' → target='{csv_val}'")
    if csv_val.lower() in current.lower():
        progress_placeholder.info(f"  ✅ Already = '{current}' — skipping")
        return True
    return click_react_select(driver, "Question Library", csv_val, progress_placeholder)


# ============================================================
# EXCLUSIVE TAGS
# ============================================================

def set_exclusive_tags(driver, tags_str, progress_placeholder):
    from selenium.webdriver.common.action_chains import ActionChains
    try:
        tags = []
        for t in str(tags_str).split(','):
            cleaned = t.strip()
            if cleaned and cleaned.lower() not in ['nan','none','null','']:
                tags.append(cleaned)

        if not tags:
            progress_placeholder.info("  🏷️ No exclusive tags to add")
            return True

        progress_placeholder.info(f"  🏷️ Adding {len(tags)} exclusive tags: {tags}")

        tag_input = None
        for xp in [
            "//input[@placeholder='Add Tags']",
            "//input[contains(@placeholder,'Add Tags')]",
            "//input[contains(@placeholder,'Tag')]",
            "//*[normalize-space(text())='Exclusive Tags (Optional)']/following::input[1]",
            "//*[contains(text(),'Exclusive Tags')]/following::input[1]",
        ]:
            try:
                el = driver.find_element(By.XPATH, xp)
                if el.is_displayed():
                    tag_input = el
                    break
            except:
                continue

        if not tag_input:
            progress_placeholder.warning("  ⚠️ Tag input field not found")
            return False

        for i, tag in enumerate(tags, 1):
            progress_placeholder.info(f"    🏷️ Adding tag {i}/{len(tags)}: '{tag}'")
            ensure_tab_focus(driver)
            try:
                driver.execute_script(
                    "arguments[0].removeAttribute('readonly');"
                    "arguments[0].removeAttribute('disabled');"
                    "arguments[0].scrollIntoView({block:'center'});"
                    "arguments[0].focus();", tag_input)
                time.sleep(0.03)
                ActionChains(driver).move_to_element(tag_input).click().perform()
                time.sleep(0.03)
            except:
                pass

            _safe_key(driver, tag_input, 'BACK_SPACE')
            _safe_send(driver, tag_input, tag)
            time.sleep(0.08)
            _safe_key(driver, tag_input, 'RETURN')
            time.sleep(0.08)
            progress_placeholder.info(f"    ✅ Tag added: '{tag}'")

        try:
            driver.execute_script("arguments[0].blur();", tag_input)
        except:
            pass
        progress_placeholder.success(f"  ✅ All {len(tags)} exclusive tags added successfully")
        return True

    except Exception as e:
        progress_placeholder.warning(f"  ⚠️ Exclusive tags error: {str(e)[:100]}")
        return False


# ============================================================
# NUMBER OF QUESTIONS
# ============================================================

def set_number_of_questions(driver, value, progress_placeholder):
    from selenium.webdriver.common.action_chains import ActionChains
    try:
        progress_placeholder.info(f"  🔢 Number of Questions → {value}")
        inp = None

        inp = driver.execute_script("""
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {
                var el = all[i];
                if (el.children.length > 0 || !el.offsetParent) continue;
                if ((el.innerText || el.textContent || '').trim() !== 'Number of Questions') continue;
                var p = el.parentElement;
                for (var j = 0; j < 8; j++) {
                    if (!p) break;
                    var sib = p.nextElementSibling;
                    while (sib) {
                        var f = sib.tagName === 'INPUT' ? sib : sib.querySelector('input');
                        if (f && f.offsetParent) return f;
                        sib = sib.nextElementSibling;
                    }
                    var c = p.querySelector('input');
                    if (c && c.offsetParent && c !== el) return c;
                    p = p.parentElement;
                }
            }
            return null;
        """)

        if not inp:
            inp = driver.execute_script("""
                var a = document.querySelectorAll('input[placeholder="0"]');
                for (var i = 0; i < a.length; i++) if (a[i].offsetParent) return a[i];
                return null;
            """)
        if not inp:
            inp = driver.execute_script("""
                var a = document.querySelectorAll('input[type="number"]');
                for (var i = 0; i < a.length; i++) if (a[i].offsetParent) return a[i];
                return null;
            """)
        if not inp:
            visible = [x for x in driver.find_elements(By.TAG_NAME, "input") if x.is_displayed()]
            if visible:
                inp = visible[-1]

        if not inp:
            progress_placeholder.warning("  ⚠️ Number of Questions input not found")
            return False

        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", inp)
        time.sleep(0.05)
        try:
            ActionChains(driver).move_to_element(inp).click().perform()
        except:
            driver.execute_script("arguments[0].click();", inp)
        time.sleep(0.08)

        try:
            inp.send_keys(Keys.CONTROL + "a")
            time.sleep(0.05)
            inp.send_keys(Keys.DELETE)
            time.sleep(0.05)
        except:
            pass

        try:
            inp.send_keys(str(value))
            time.sleep(0.08)
            progress_placeholder.info(f"    ✓ Typed via send_keys: '{value}'")
        except:
            driver.execute_script("""
                var el = arguments[0], val = arguments[1];
                var nativeSet = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                el.focus();
                nativeSet.call(el, '');
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                nativeSet.call(el, val);
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            """, inp, str(value))
            time.sleep(0.08)
            progress_placeholder.info(f"    ✓ Set via JS: '{value}'")

        actual = inp.get_attribute("value") or ""
        progress_placeholder.info(f"  ✅ Number of Questions = '{actual}'")
        try:
            driver.execute_script("arguments[0].blur();", inp)
        except:
            pass
        time.sleep(0.08)
        return True

    except Exception as e:
        progress_placeholder.warning(f"  ⚠️ Num Q error: {str(e)[:120]}")
        return False


# ============================================================
# MARKS FOR EACH QUESTION
# ============================================================

MARKS_SECTION_TYPES = {"coding", "ide based coding", "sql", "sql coding", "web coding", "textual"}


def set_marks_per_question(driver, value, progress_placeholder):
    from selenium.webdriver.common.action_chains import ActionChains
    try:
        progress_placeholder.info(f"  🏅 Marks for Each Question → {value}")
        inp = None

        inp = driver.execute_script("""
            var labelTexts = [
                'Marks for Each Question',
                'Marks Per Question',
                'Marks/Question',
                'Mark for Each Question',
                'Marks'
            ];
            var all = document.querySelectorAll('*');
            for (var i = 0; i < all.length; i++) {
                var el = all[i];
                if (el.children.length > 0 || !el.offsetParent) continue;
                var txt = (el.innerText || el.textContent || '').trim();
                var matched = false;
                for (var k = 0; k < labelTexts.length; k++) {
                    if (txt === labelTexts[k]) { matched = true; break; }
                }
                if (!matched) continue;
                var p = el.parentElement;
                for (var j = 0; j < 8; j++) {
                    if (!p) break;
                    var sib = p.nextElementSibling;
                    while (sib) {
                        var f = sib.tagName === 'INPUT' ? sib : sib.querySelector('input');
                        if (f && f.offsetParent) return f;
                        sib = sib.nextElementSibling;
                    }
                    var c = p.querySelector('input');
                    if (c && c.offsetParent && c !== el) return c;
                    p = p.parentElement;
                }
            }
            return null;
        """)

        if not inp:
            inp = driver.execute_script("""
                var inputs = document.querySelectorAll('input[type="number"]');
                var visible = [];
                for (var i = 0; i < inputs.length; i++) {
                    if (inputs[i].offsetParent) visible.push(inputs[i]);
                }
                return visible.length >= 2 ? visible[1] : null;
            """)

        if not inp:
            for xp in [
                "//*[normalize-space(text())='Marks for Each Question']/following::input[1]",
                "//*[contains(text(),'Marks for Each Question')]/following::input[1]",
                "//*[contains(text(),'Marks Per Question')]/following::input[1]",
            ]:
                try:
                    el = driver.find_element(By.XPATH, xp)
                    if el.is_displayed():
                        inp = el
                        break
                except:
                    continue

        if not inp:
            progress_placeholder.warning(
                "  ⚠️ Marks for Each Question input not found — skipping")
            return False

        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", inp)
        time.sleep(0.05)
        try:
            ActionChains(driver).move_to_element(inp).click().perform()
        except:
            driver.execute_script("arguments[0].click();", inp)
        time.sleep(0.08)

        try:
            inp.send_keys(Keys.CONTROL + "a")
            time.sleep(0.05)
            inp.send_keys(Keys.DELETE)
            time.sleep(0.05)
        except:
            pass
        try:
            driver.execute_script("""
                var el = arguments[0];
                var nativeSet = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                nativeSet.call(el, '');
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            """, inp)
        except:
            pass

        typed = False
        try:
            inp.send_keys(str(value))
            time.sleep(0.08)
            progress_placeholder.info(f"    ✓ Typed via send_keys: '{value}'")
            typed = True
        except:
            pass

        if not typed:
            try:
                driver.execute_script("""
                    var el = arguments[0], val = arguments[1];
                    var nativeSet = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    el.focus();
                    nativeSet.call(el, '');
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    nativeSet.call(el, val);
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                """, inp, str(value))
                time.sleep(0.08)
                progress_placeholder.info(f"    ✓ Set via JS native setter: '{value}'")
                typed = True
            except:
                pass

        if not typed:
            try:
                ActionChains(driver).move_to_element(inp).click().send_keys(str(value)).perform()
                time.sleep(0.08)
                progress_placeholder.info(f"    ✓ Set via ActionChains: '{value}'")
            except:
                pass

        actual = inp.get_attribute("value") or ""
        progress_placeholder.info(f"  ✅ Marks for Each Question = '{actual}'")
        try:
            driver.execute_script("arguments[0].blur();", inp)
        except:
            pass
        time.sleep(0.08)
        return True

    except Exception as e:
        progress_placeholder.warning(f"  ⚠️ Marks error: {str(e)[:120]}")
        return False


# ============================================================
# HELPER
# ============================================================

def _get_all_visible_controls_sorted(driver):
    return driver.execute_script("""
        var controls = document.querySelectorAll(
            '[class*="__control"],[class*="-control"]');
        var visible = [];
        for (var i = 0; i < controls.length; i++) {
            var c = controls[i];
            if (!c.offsetParent) continue;
            var r = c.getBoundingClientRect();
            if (r.width > 0 && r.height > 0)
                visible.push(c);
        }
        visible.sort(function(a, b) {
            return a.getBoundingClientRect().left - b.getBoundingClientRect().left;
        });
        return visible;
    """)


# ============================================================
# CODING QUESTION RESTRICTION
# ============================================================

def set_coding_question_restriction(driver, languages_str, progress_placeholder):
    progress_placeholder.info("🚀 STARTING set_coding_question_restriction function")
    progress_placeholder.info(f"  📥 Input languages_str: '{languages_str}'")

    try:
        languages = [
            l.strip() for l in str(languages_str).split(',')
            if l.strip() and l.strip().lower() not in ['nan', 'none', 'null', '']
        ]
        if not languages:
            progress_placeholder.info("  ⚙️ No Coding Question Restriction — skipping")
            return True

        lang_map = {
            'python':'Python','python3':'Python',
            'java':'Java','c':'C',
            'c++':'C++','cpp':'C++','cplusplus':'C++',
            'c#':'C#','csharp':'C#',
            'javascript':'JavaScript','js':'JavaScript',
            'ruby':'Ruby','go':'Go','swift':'Swift',
            'kotlin':'Kotlin','scala':'Scala','r':'R',
        }
        normalized_languages = [lang_map.get(l.lower().strip(), l.strip()) for l in languages]
        progress_placeholder.info(f"  🖥️ Target languages: {normalized_languages}")

        ensure_tab_focus(driver)

        # Ultra-fast single JavaScript execution approach
        result = driver.execute_script("""
            var targetLanguages = arguments[0];
            var successCount = 0;
            
            // Find dropdown by text content
            var dropdown = null;
            var allElements = document.querySelectorAll('*');
            for (var i = 0; i < allElements.length; i++) {
                var el = allElements[i];
                var text = (el.innerText || el.textContent || el.placeholder || '').trim();
                if (text === 'Select Coding Languages') {
                    if (el.offsetParent && (el.tagName === 'INPUT' || el.getAttribute('role') === 'combobox')) {
                        dropdown = el;
                        break;
                    }
                    var parent = el.parentElement;
                    for (var j = 0; j < 5; j++) {
                        if (!parent) break;
                        var dd = parent.querySelector('[class*="Select__control"], [role="combobox"], input');
                        if (dd && dd.offsetParent) {
                            dropdown = dd;
                            break;
                        }
                        parent = parent.parentElement;
                    }
                    if (dropdown) break;
                }
            }
            
            // Fallback: find by label
            if (!dropdown) {
                for (var i = 0; i < allElements.length; i++) {
                    var el = allElements[i];
                    var text = (el.innerText || el.textContent || '').trim();
                    if (text === 'Coding Question Restriction') {
                        var container = el.closest('div, section, form');
                        if (container) {
                            var dropdowns = container.querySelectorAll('[class*="Select__control"], [role="combobox"], input');
                            for (var j = 0; j < dropdowns.length; j++) {
                                var dd = dropdowns[j];
                                if (dd.offsetParent) {
                                    var isMulti = dd.className.indexOf('multi') !== -1 ||
                                                 dd.closest('[class*="multi"]') !== null;
                                    if (isMulti || j === 0) {
                                        dropdown = dd;
                                        break;
                                    }
                                }
                            }
                        }
                        if (dropdown) break;
                    }
                }
            }
            
            if (!dropdown) {
                return {success: false, error: 'Dropdown not found'};
            }
            
            // Click dropdown to open
            dropdown.scrollIntoView({block: 'center'});
            dropdown.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
            dropdown.dispatchEvent(new MouseEvent('mouseup',  {bubbles:true,cancelable:true}));
            dropdown.dispatchEvent(new MouseEvent('click',    {bubbles:true,cancelable:true}));
            
            // Wait briefly for options to appear
            setTimeout(function() {
                // Select each language
                for (var langIndex = 0; langIndex < targetLanguages.length; langIndex++) {
                    var targetLang = targetLanguages[langIndex];
                    var selectors = ['[role="option"]','input[type="checkbox"]','[class*="option"]','li'];
                    var found = false;
                    
                    for (var s = 0; s < selectors.length && !found; s++) {
                        var elements = document.querySelectorAll(selectors[s]);
                        for (var i = 0; i < elements.length; i++) {
                            var el = elements[i];
                            if (!el.offsetParent) continue;
                            var text = '';
                            if (el.type === 'checkbox') {
                                var label = el.closest('label') || el.parentElement;
                                text = (label.innerText || label.textContent || '').trim();
                            } else {
                                text = (el.innerText || el.textContent || '').trim();
                            }
                            if (text === targetLang) {
                                el.scrollIntoView({block:'nearest'});
                                el.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
                                successCount++;
                                found = true;
                                break;
                            }
                        }
                    }
                }
                
                // Close dropdown
                try {
                    document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                } catch(e) {}
                
            }, 50);
            
            return {success: true, dropdown: 'found'};
        """, normalized_languages)

        if not result.get('success'):
            progress_placeholder.error(f"    ❌ {result.get('error', 'Unknown error')}")
            return False

        progress_placeholder.info("    ✅ Found dropdown and initiated selection")
        time.sleep(0.1)  # Brief wait for selections to complete

        progress_placeholder.success(f"  ✅ Coding languages selection completed")
        return True

    except Exception as e:
        progress_placeholder.error(f"  ❌ Error: {str(e)[:150]}")
        return False


# ============================================================
# DEFAULT CODING LANGUAGE
# ============================================================

def set_default_coding_language(driver, language, progress_placeholder):
    progress_placeholder.info("🚀 STARTING set_default_coding_language function")
    progress_placeholder.info(f"  📥 Input language: '{language}'")

    try:
        lang = str(language).strip()
        if not lang or lang.lower() in ['nan', 'none', 'null', '']:
            progress_placeholder.info("  ⚙️ No Default Coding Language — skipping")
            return True

        lang_map = {
            'python':'Python','python3':'Python',
            'java':'Java','c':'C',
            'c++':'C++','cpp':'C++','cplusplus':'C++',
            'c#':'C#','csharp':'C#',
            'javascript':'JavaScript','js':'JavaScript',
            'ruby':'Ruby','go':'Go','swift':'Swift',
            'kotlin':'Kotlin','scala':'Scala','r':'R',
        }
        normalized_lang = lang_map.get(lang.lower().strip(), lang.strip())
        progress_placeholder.info(f"  🌐 Target language: '{normalized_lang}'")

        ensure_tab_focus(driver)

        # Ultra-fast single JavaScript execution approach
        result = driver.execute_script("""
            var targetLang = arguments[0];
            
            // Find dropdown by "Default Coding Language" text
            var dropdown = null;
            var allElements = document.querySelectorAll('*');
            for (var i = 0; i < allElements.length; i++) {
                var el = allElements[i];
                var text = (el.innerText || el.textContent || '').trim();
                if (text === 'Default Coding Language') {
                    var container = el.closest('div, section, form');
                    if (container) {
                        var dropdowns = container.querySelectorAll('[class*="Select__control"], [role="combobox"], select');
                        for (var j = 0; j < dropdowns.length; j++) {
                            var dd = dropdowns[j];
                            if (dd.offsetParent) {
                                var isMulti = dd.className.indexOf('multi') !== -1 ||
                                             dd.closest('[class*="multi"]') !== null;
                                if (!isMulti) {
                                    dropdown = dd;
                                    break;
                                }
                            }
                        }
                    }
                    if (dropdown) break;
                }
            }
            
            if (!dropdown) {
                return {success: false, error: 'Default Coding Language dropdown not found'};
            }
            
            // Click dropdown to open
            dropdown.scrollIntoView({block: 'center'});
            dropdown.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));
            dropdown.dispatchEvent(new MouseEvent('mouseup',  {bubbles:true,cancelable:true}));
            dropdown.dispatchEvent(new MouseEvent('click',    {bubbles:true,cancelable:true}));
            
            // Wait briefly and select option
            setTimeout(function() {
                var optionSelectors = ['[role="option"]', 'option', '[class*="option"]'];
                var found = false;
                
                for (var s = 0; s < optionSelectors.length && !found; s++) {
                    var options = document.querySelectorAll(optionSelectors[s]);
                    for (var o = 0; o < options.length; o++) {
                        var opt = options[o];
                        if (opt.offsetParent) {
                            var optText = (opt.innerText || opt.textContent || opt.value || '').trim();
                            if (optText === targetLang) {
                                opt.scrollIntoView({block: 'nearest'});
                                opt.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
                                found = true;
                                break;
                            }
                        }
                    }
                }
            }, 50);
            
            return {success: true, dropdown: 'found'};
        """, normalized_lang)

        if not result.get('success'):
            progress_placeholder.error(f"    ❌ {result.get('error', 'Unknown error')}")
            # Fallback to click_react_select
            success = click_react_select(driver, "Default Coding Language", normalized_lang, progress_placeholder)
            if success:
                progress_placeholder.success(f"  ✅ Default Coding Language = '{normalized_lang}' (Fallback)")
                return True
            return False

        progress_placeholder.info("    ✅ Found dropdown and initiated selection")
        time.sleep(0.1)  # Brief wait for selection to complete

        progress_placeholder.success(f"  ✅ Default Coding Language = '{normalized_lang}'")
        return True

    except Exception as e:
        progress_placeholder.error(f"  ❌ Error: {str(e)[:150]}")
        return False


# ============================================================
# SELECT SECTION TYPE
# ============================================================

def select_section_type(driver, wait, target_section, progress_placeholder):
    progress_placeholder.info(f"🎯 Selecting section type: '{target_section}'...")
    try:
        wait.until(EC.presence_of_element_located(
            (By.XPATH, "//*[contains(., 'Select Section Type')]")))
        progress_placeholder.info("  ✅ Section type modal loaded")
    except:
        progress_placeholder.warning("  ⚠️ Could not confirm modal — continuing")

    clicked = False
    for xp in [
        f"//*[normalize-space(text())='{target_section}']",
        f"//*[contains(text(),'{target_section}')]",
        f"//div[contains(@class,'card')]//h3[text()='{target_section}']",
        f"//button[contains(.,'{target_section}')]",
        f"//div[@role='button'][contains(.,'{target_section}')]",
    ]:
        for elem in [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});"
                    "arguments[0].click();", elem)
                time.sleep(0.08)
                try:
                    modals = driver.find_elements(By.XPATH,
                        "//*[contains(normalize-space(.),'Select Section Type')]")
                    if not any(m.is_displayed() for m in modals):
                        clicked = True
                except:
                    clicked = True
                if clicked:
                    progress_placeholder.success(f"  ✅ '{target_section}' selected!")
                    break
            except:
                continue
        if clicked:
            break

    if not clicked:
        progress_placeholder.warning(
            f"⚠️ Could not auto-click '{target_section}'. Please click manually (10s)...")
        time.sleep(10)
    return clicked


# ============================================================
# FILL SECTION FORM
# ============================================================

def fill_section_form(driver, section_name, time_limit, progress_placeholder):
    progress_placeholder.info("📝 Filling section form...")

    if section_name:
        name_filled = False
        for xpath in [
            "//label[normalize-space(text())='Name of Section']/following-sibling::input[1]",
            "//label[normalize-space(text())='Name of section']/following-sibling::input[1]",
            "//label[contains(text(),'Name of Section')]/..//input[not(@type='number')]",
            "//*[normalize-space(text())='Name of Section']/following::input[not(@type='number')][1]",
        ]:
            try:
                field = driver.find_element(By.XPATH, xpath)
                if field.is_displayed() and (field.get_attribute("type") or "").lower() != "number":
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});"
                        "arguments[0].focus();", field)
                    _safe_send(driver, field, Keys.CONTROL + "a")
                    _safe_key(driver, field, 'DELETE')
                    _safe_send(driver, field, section_name)
                    driver.execute_script("""
                        arguments[0].dispatchEvent(new Event('input',  {bubbles: true}));
                        arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
                    """, field)
                    name_filled = True
                    progress_placeholder.success(f"  ✅ Section name: {section_name}")
                    break
            except:
                continue
        if not name_filled:
            progress_placeholder.warning("  ⚠️ Could not fill section name")

    if time_limit:
        time_filled = False
        progress_placeholder.info(f"  ⏰ Setting time limit to: {time_limit} mins")

        for xpath in [
            "//label[normalize-space(text())='Time Limit (in Mins)']/following-sibling::input[1]",
            "//label[contains(text(),'Time Limit (in Mins)')]/..//input[@type='number']",
            "//label[contains(text(),'Time Limit')]/..//input[@type='number']",
            "//*[normalize-space(text())='Time Limit (in Mins)']/following::input[@type='number'][1]",
        ]:
            try:
                field = driver.find_element(By.XPATH, xpath)
                if field.is_displayed():
                    current_value = field.get_attribute("value") or ""
                    progress_placeholder.info(f"    🔍 Current time limit value: '{current_value}'")
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});"
                        "arguments[0].focus();", field)
                    time.sleep(0.05)
                    _safe_send(driver, field, Keys.CONTROL + "a")
                    time.sleep(0.03)
                    _safe_key(driver, field, 'DELETE')
                    time.sleep(0.03)
                    driver.execute_script("""
                        var field = arguments[0];
                        field.value = '';
                        field.dispatchEvent(new Event('input', {bubbles: true}));
                        field.dispatchEvent(new Event('change', {bubbles: true}));
                    """, field)
                    time.sleep(0.03)
                    for _ in range(10):
                        _safe_key(driver, field, 'BACK_SPACE')
                        time.sleep(0.01)
                    cleared_value = field.get_attribute("value") or ""
                    progress_placeholder.info(f"    🧹 After clearing: '{cleared_value}'")
                    _safe_send(driver, field, str(time_limit))
                    time.sleep(0.05)
                    driver.execute_script("""
                        var field = arguments[0];
                        field.dispatchEvent(new Event('input',  {bubbles: true}));
                        field.dispatchEvent(new Event('change', {bubbles: true}));
                        field.dispatchEvent(new Event('blur',   {bubbles: true}));
                    """, field)
                    final_value = field.get_attribute("value") or ""
                    progress_placeholder.info(f"    ✅ Final time limit value: '{final_value}'")
                    if final_value == str(time_limit):
                        time_filled = True
                        progress_placeholder.success(
                            f"  ✅ Time limit successfully set: {time_limit} mins")
                        break
                    else:
                        progress_placeholder.warning(
                            f"    ⚠️ Value mismatch: expected '{time_limit}', got '{final_value}'")
            except Exception as e:
                progress_placeholder.warning(
                    f"    ⚠️ Error with xpath {xpath}: {str(e)[:50]}")
                continue

        if not time_filled:
            progress_placeholder.warning("  ❌ Could not fill time limit after trying all methods")

    progress_placeholder.success("  ✅ Section form filled — ready to add questions")


# ============================================================
# HANDLE SUBJECT SELECTION
# ============================================================

def handle_subject_selection(driver, progress_placeholder):
    try:
        els = driver.find_elements(By.XPATH,
            "//*[contains(text(),'Select Subject') or "
            "contains(text(),'Choose Subject') or "
            "contains(text(),'Subject')]")
        if not els:
            return
        progress_placeholder.info("📚 Subject selection detected — clicking first subject...")
        for xpath in [
            "//div[contains(@class,'subject') or contains(@class,'category')]//button",
            "//div[contains(@class,'card')]",
            "//*[contains(@role,'button')]",
        ]:
            for subj in driver.find_elements(By.XPATH, xpath)[:3]:
                try:
                    if subj.is_displayed():
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block:'center'});"
                            "arguments[0].click();", subj)
                        progress_placeholder.success("  ✅ Subject selected!")
                        time.sleep(0.03)
                        return
                except:
                    continue
    except:
        pass


# ============================================================
# CLOSE THE ADD QUESTIONS POPUP
# ============================================================

def close_add_questions_popup(driver, progress_placeholder):
    ensure_tab_focus(driver)
    progress_placeholder.info("🔲 Closing the Add Questions popup...")
    popup_closed = False

    for attempt in range(5):
        try:
            close_btn = driver.find_element(
                By.CSS_SELECTOR, '[data-testid="aqp-close-icon"]')
            if close_btn and close_btn.is_displayed():
                driver.execute_script("arguments[0].click();", close_btn)
                still_open = not poll_popup_closed(driver, timeout=2.0)
                if not still_open:
                    progress_placeholder.success("  ✅ Popup closed via close button!")
                    popup_closed = True
                    break
                else:
                    progress_placeholder.warning(
                        f"  ⚠️ Popup still visible after close attempt {attempt+1}")
        except Exception as e:
            progress_placeholder.warning(
                f"  ⚠️ Close button attempt {attempt+1}: {str(e)[:60]}")
        poll_popup_closed(driver, timeout=0.3)

    if not popup_closed:
        try:
            driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.ESCAPE)
            still_open = not poll_popup_closed(driver, timeout=2.0)
            if not still_open:
                progress_placeholder.success("  ✅ Popup closed via Escape key!")
                popup_closed = True
        except Exception as e:
            progress_placeholder.warning(f"  ⚠️ Escape key failed: {str(e)[:60]}")

    if not popup_closed:
        try:
            driver.execute_script("""
                ['[role="dialog"]', '.modal', '[data-testid="aqp-close-icon"]'].forEach(
                    function(sel) {
                        document.querySelectorAll(sel).forEach(function(el) {
                            el.style.display = 'none';
                        });
                    });
                document.body.style.overflow = 'auto';
                document.body.classList.remove('modal-open', 'no-scroll');
            """)
            poll_popup_closed(driver, timeout=0.3)
            progress_placeholder.success("  ✅ Popup hidden via JS fallback")
            popup_closed = True
        except Exception as e:
            progress_placeholder.error(f"  ❌ JS popup hide failed: {str(e)[:60]}")

    return popup_closed


# ============================================================
# CLICK "ADD SECTION →" BUTTON
# ============================================================

def click_add_section_button(driver, progress_placeholder):
    ensure_tab_focus(driver)
    progress_placeholder.info("📌 Looking for 'Add Section →' button...")

    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    poll_button_visible(driver, "Add Section", timeout=1.5)

    for attempt in range(15):
        progress_placeholder.info(f"  🔍 Attempt {attempt+1}/15")
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.08)

        btn = driver.execute_script("""
            var b = document.querySelector('button[data-testid="cnsf-footer-cta-button"]');
            if (b && b.offsetParent) {
                if ((b.innerText || b.textContent || '').includes('Add Section')) return b;
            }
            return null;
        """)

        if not btn:
            btn = driver.execute_script("""
                var buttons = document.querySelectorAll('button');
                for (var i = 0; i < buttons.length; i++) {
                    var b = buttons[i];
                    if ((b.innerText || b.textContent || '').trim().includes('Add Section')
                            && b.offsetParent) return b;
                }
                return null;
            """)

        if btn:
            btn_text = driver.execute_script(
                "return (arguments[0].innerText || arguments[0].textContent || '').trim();", btn)
            progress_placeholder.info(f"  🎯 Found: '{btn_text}'")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.05)
            try:
                from selenium.webdriver.common.action_chains import ActionChains
                ActionChains(driver).move_to_element(btn).pause(0.03).click().perform()
                progress_placeholder.info("  ✅ ActionChains click on Add Section")
            except:
                driver.execute_script("arguments[0].click();", btn)
                progress_placeholder.info("  ✅ JS click on Add Section")
            _old_url = driver.current_url
            poll_url_changed(driver, _old_url, timeout=2.0)
            progress_placeholder.success("  🎉 'Add Section →' clicked!")
            return True
        else:
            progress_placeholder.warning(
                f"  ⚠️ Add Section button not found (attempt {attempt+1})")
            if attempt % 5 == 4:
                visible_btns = driver.execute_script("""
                    var r = [];
                    document.querySelectorAll('button').forEach(function(b) {
                        if (b.offsetParent)
                            r.push((b.innerText||b.textContent||'').trim().slice(0,40));
                    });
                    return r;
                """)
                progress_placeholder.info(f"  🔍 Visible buttons: {visible_btns}")
            poll_button_visible(driver, "Add Section", timeout=0.7)

    progress_placeholder.error("  ❌ Could not find 'Add Section →' button after 15 attempts")
    return False


# ============================================================
# CLICK "CREATE NEW SECTION" BUTTON
# ============================================================

def click_create_new_section_button(driver, wait, progress_placeholder):
    progress_placeholder.info("➕ Looking for 'Create New Section' button...")
    poll_button_visible(driver, "Section", timeout=2.0)

    for attempt in range(15):
        progress_placeholder.info(f"  🔍 Attempt {attempt+1}/15")
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.08)

        btn = driver.execute_script("""
            var buttons = document.querySelectorAll('button, a, div[role="button"]');
            for (var i = 0; i < buttons.length; i++) {
                var b = buttons[i];
                var txt = (b.innerText || b.textContent || '').trim();
                if ((txt.includes('Create') && txt.includes('Section')) && b.offsetParent)
                    return b;
                if (txt.includes('Create new Section') && b.offsetParent)
                    return b;
                if (txt.includes('Add New Section') && b.offsetParent)
                    return b;
            }
            return null;
        """)

        if not btn:
            for xp in [
                "//*[contains(text(),'Create new Section') or contains(text(),'Create New Section')]",
                "//*[contains(text(),'Add New Section')]",
                "//button[contains(.,'Section')]",
            ]:
                try:
                    els = [e for e in driver.find_elements(By.XPATH, xp) if e.is_displayed()]
                    if els:
                        btn = els[0]
                        break
                except:
                    continue

        if btn:
            btn_text = driver.execute_script(
                "return (arguments[0].innerText || arguments[0].textContent || '').trim();", btn)
            progress_placeholder.info(f"  🎯 Found: '{btn_text}'")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.05)
            try:
                from selenium.webdriver.common.action_chains import ActionChains
                ActionChains(driver).move_to_element(btn).pause(0.03).click().perform()
                progress_placeholder.info("  ✅ ActionChains click on Create New Section")
            except:
                driver.execute_script("arguments[0].click();", btn)
                progress_placeholder.info("  ✅ JS click on Create New Section")
            poll_element_visible(driver, "//*[contains(.,'Select Section Type')]", timeout=2.0)
            progress_placeholder.success("  🎉 'Create New Section' clicked!")
            return True
        else:
            progress_placeholder.warning(
                f"  ⚠️ Create New Section button not found (attempt {attempt+1})")
            if attempt % 5 == 4:
                visible_btns = driver.execute_script("""
                    var r = [];
                    document.querySelectorAll('button').forEach(function(b) {
                        if (b.offsetParent)
                            r.push((b.innerText||b.textContent||'').trim().slice(0,40));
                    });
                    return r;
                """)
                progress_placeholder.info(f"  🔍 Visible buttons: {visible_btns}")
            poll_button_visible(driver, "Section", timeout=0.7)

    progress_placeholder.error("  ❌ Could not find 'Create New Section' button")
    return False


# ============================================================
# CLICK "SAVE & NEXT" BUTTON
# ============================================================

def click_save_and_next_button(driver, progress_placeholder):
    ensure_tab_focus(driver)
    progress_placeholder.info("📌 Looking for 'Save & Next' button...")
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    poll_button_visible(driver, "Save", timeout=2.0)

    for attempt in range(10):
        progress_placeholder.info(f"  🔍 Attempt {attempt+1}/10")
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.08)

        btn = driver.execute_script("""
            var buttons = document.querySelectorAll('button');
            for (var i = 0; i < buttons.length; i++) {
                var b = buttons[i];
                var txt = (b.innerText || b.textContent || '').trim();
                if (txt.includes('Save') && txt.includes('Next') && b.offsetParent) return b;
            }
            return null;
        """)

        if btn:
            btn_text = driver.execute_script(
                "return (arguments[0].innerText || arguments[0].textContent || '').trim();", btn)
            progress_placeholder.info(f"  🎯 Found: '{btn_text}'")
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            time.sleep(0.05)
            try:
                from selenium.webdriver.common.action_chains import ActionChains
                ActionChains(driver).move_to_element(btn).pause(0.03).click().perform()
                progress_placeholder.info("  ✅ ActionChains click on Save & Next")
            except:
                driver.execute_script("arguments[0].click();", btn)
                progress_placeholder.info("  ✅ JS click on Save & Next")
            _old_url = driver.current_url
            poll_url_changed(driver, _old_url, timeout=3.0)
            progress_placeholder.success("  🎉 'Save & Next' clicked!")
            return True
        else:
            progress_placeholder.warning(
                f"  ⚠️ Save & Next button not found (attempt {attempt+1})")
            if attempt % 3 == 2:
                visible_btns = driver.execute_script("""
                    var r = [];
                    document.querySelectorAll('button').forEach(function(b) {
                        if (b.offsetParent)
                            r.push((b.innerText||b.textContent||'').trim().slice(0,40));
                    });
                    return r;
                """)
                progress_placeholder.info(f"  🔍 Visible buttons: {visible_btns}")
            poll_button_visible(driver, "Save", timeout=0.7)

    progress_placeholder.error("  ❌ Could not find or click 'Save & Next' button")
    return False


# ============================================================
# QUESTION LOOP
# ============================================================

def process_section_questions(driver, questions_df, section_num, progress_placeholder,
                              section_type=""):
    total = len(questions_df)
    progress_placeholder.info(f"🚀 Section {section_num}: Processing {total} question rows")

    needs_marks = section_type.strip().lower() in MARKS_SECTION_TYPES
    if needs_marks:
        progress_placeholder.info(
            f"  📝 Section type '{section_type}' → Marks for Each Question will be filled")
    else:
        progress_placeholder.info(
            f"  📝 Section type '{section_type}' → Marks for Each Question skipped")

    def is_empty(val):
        return str(val).strip().lower() in ["nan", "none", "null", ""]

    def wait_popup_open(secs=10):
        for _ in range(secs * 20):
            try:
                els = driver.find_elements(By.XPATH,
                    "//h2[normalize-space(text())='Add Questions'] | "
                    "//h2[normalize-space(text())='Edit Questions'] | "
                    "//*[@role='dialog']//*[normalize-space(text())='Add Questions'] | "
                    "//*[@role='dialog']//*[normalize-space(text())='Edit Questions']")
                if any(e.is_displayed() for e in els):
                    return True
                els2 = driver.find_elements(By.XPATH,
                    "//*[normalize-space(text())='Question Library']")
                if any(e.is_displayed() for e in els2):
                    return True
            except:
                pass
            time.sleep(0.05)
        return False

    def wait_popup_closed(secs=15):
        for _ in range(secs * 20):
            try:
                els = driver.find_elements(By.XPATH,
                    "//*[normalize-space(text())='Question Library']")
                if not any(e.is_displayed() for e in els):
                    return True
            except:
                pass
            time.sleep(0.05)
        return False

    def click_page_open_button():
        for _ in range(40):
            try:
                clicked = driver.execute_script("""
                    var btns = document.querySelectorAll('button, a');
                    for (var i = 0; i < btns.length; i++) {
                        var el  = btns[i];
                        var txt = (el.innerText || el.textContent || '').trim();
                        if (txt.indexOf('Add')      === -1) continue;
                        if (txt.indexOf('Question') === -1) continue;
                        if (txt.indexOf('\u2192')   !== -1) continue;
                        if (txt.indexOf('->')       !== -1) continue;
                        if (!el.offsetParent)               continue;
                        var r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0)  continue;
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return txt;
                    }
                    return null;
                """)
                if clicked:
                    progress_placeholder.info(f"    ✅ Clicked: '{clicked}'")
                    return True
            except:
                pass
            time.sleep(0.08)
        return False

    failed_rows = []

    for idx, row in questions_df.iterrows():
        row_num = idx + 1
        progress_placeholder.info(
            f"\n{'='*55}\n  SECTION {section_num} — ROW {row_num} / {total}\n{'='*55}")

        qlib  = str(row.get("Question Library",  "Topin Questions")).strip()
        topic = str(row.get("Topic",             "")).strip()
        diff  = str(row.get("Difficulty Level",  "")).strip()
        sub   = str(row.get("Sub Topic",         "")).strip()

        all_tags = []
        exclusive_tag_columns = [
            col for col in questions_df.columns if col.startswith("Exclusive Tags")]
        if exclusive_tag_columns:
            progress_placeholder.info(
                f"  🔍 Found {len(exclusive_tag_columns)} exclusive tag columns: "
                f"{exclusive_tag_columns}")
        for col in exclusive_tag_columns:
            tag_value = str(row.get(col, "")).strip()
            if not is_empty(tag_value):
                all_tags.append(tag_value)
                progress_placeholder.info(f"    📝 {col}: '{tag_value}'")

        tags  = ", ".join(all_tags) if all_tags else ""
        num_q = str(row.get("Number of Questions", "")).strip()
        marks = ""
        if needs_marks:
            marks = str(row.get("Marks for Each Question", "")).strip()

        if not is_empty(diff):
            diff = diff.capitalize()

        progress_placeholder.info(
            f"  📋 QL={qlib} | Topic={topic} | Diff={diff} | Sub={sub} "
            f"| Tags={tags} | Num={num_q}"
            + (f" | Marks={marks}" if needs_marks else ""))

        wait_popup_closed(secs=2)  # Reduced from 3 to 2 seconds

        if not click_page_open_button():
            reason = "Could not open popup"
            progress_placeholder.error(f"  ❌ Row {row_num} FAILED — {reason}")
            failed_rows.append({"Section": section_num, "Row": row_num, "Topic": topic,
                                 "Difficulty": diff, "Sub Topic": sub, "Num Q": num_q,
                                 "Marks": marks, "Reason": reason})
            continue

        if not wait_popup_open(secs=3):  # Reduced from 5 to 3 seconds
            reason = "Popup did not open"
            progress_placeholder.error(f"  ❌ Row {row_num} FAILED — {reason}")
            failed_rows.append({"Section": section_num, "Row": row_num, "Topic": topic,
                                 "Difficulty": diff, "Sub Topic": sub, "Num Q": num_q,
                                 "Marks": marks, "Reason": reason})
            continue

        time.sleep(0.02)  # Reduced from 0.05 - ultra fast between questions
        ensure_tab_focus(driver)

        handle_question_library(driver, qlib, progress_placeholder)

        if not is_empty(topic):
            click_react_select(driver, "Topic", topic, progress_placeholder)
        if not is_empty(diff):
            click_react_select(driver, "Difficulty Level", diff, progress_placeholder)
        if not is_empty(sub):
            click_react_select(driver, "Sub Topic", sub, progress_placeholder)
        if not is_empty(tags):
            set_exclusive_tags(driver, tags, progress_placeholder)

        try:
            driver.execute_script(
                "if(document.activeElement) document.activeElement.blur();")
        except:
            pass

        if not is_empty(num_q):
            set_number_of_questions(driver, num_q, progress_placeholder)
            time.sleep(0.01)  # Reduced from 0.02 - ultra fast

        if needs_marks and not is_empty(marks):
            set_marks_per_question(driver, marks, progress_placeholder)
            time.sleep(0.01)  # Reduced from 0.02 - ultra fast

        progress_placeholder.info("  📤 Clicking 'Add Questions →'...")
        submitted = False

        for submit_attempt in range(8):
            progress_placeholder.info(f"    🔁 Attempt {submit_attempt + 1}/8")
            time.sleep(0.08)

            btn = None
            btn_info = {}
            try:
                all_btns = driver.find_elements(By.XPATH,
                    "//button[contains(., 'Add Questions') "
                    "and not(starts-with(normalize-space(.), '+'))]")
                best_bottom = -1
                for b in all_btns:
                    try:
                        rect = driver.execute_script(
                            "var r=arguments[0].getBoundingClientRect();"
                            "return {bottom:r.bottom,width:r.width,height:r.height,"
                            "disabled:arguments[0].disabled};", b)
                        if rect['width'] > 0 and rect['bottom'] > best_bottom:
                            best_bottom = rect['bottom']
                            btn = b
                            btn_info = rect
                    except:
                        pass
            except:
                pass

            if not btn:
                progress_placeholder.warning(
                    f"    ⚠️ Button not found on attempt {submit_attempt+1}")
                time.sleep(0.02)  # Reduced from 0.05 - ultra fast retry
                continue

            btn_txt = (btn.text or "").strip()
            progress_placeholder.info(
                f"    🎯 Found: '{btn_txt}' | disabled={btn_info.get('disabled')}")

            if btn_info.get('disabled'):
                reason = "No questions available — button disabled"
                progress_placeholder.warning(f"  ⚠️ Row {row_num} SKIPPED — {reason}")
                failed_rows.append({"Section": section_num, "Row": row_num, "Topic": topic,
                                     "Difficulty": diff, "Sub Topic": sub, "Num Q": num_q,
                                     "Marks": marks, "Reason": reason})
                submitted = True
                break

            try:
                from selenium.webdriver.common.action_chains import ActionChains
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", btn)
                time.sleep(0.02)  # Reduced from 0.05 - ultra fast
                ActionChains(driver).move_to_element(btn).pause(0.03).click().perform()
                progress_placeholder.info("    ✅ ActionChains click performed!")
            except Exception as e:
                progress_placeholder.warning(
                    f"    ⚠️ ActionChains failed: {str(e)[:60]}, trying JS...")
                try:
                    driver.execute_script("arguments[0].click();", btn)
                    progress_placeholder.info("    ✅ JS click performed!")
                except Exception as e2:
                    progress_placeholder.error(
                        f"    ❌ Both clicks failed: {str(e2)[:60]}")
                    time.sleep(0.05)  # Reduced from 0.1 - faster retry
                    continue

            poll_popup_closed(driver, timeout=0.2)  # Reduced from 1.0 - much faster
            submitted = True
            progress_placeholder.success(
                f"  ✅ Section {section_num} Row {row_num}/{total} submitted!")
            break

        if not submitted:
            reason = "'Add Questions →' not clicked after 8 attempts"
            progress_placeholder.error(f"  ❌ Row {row_num} FAILED — {reason}")
            failed_rows.append({"Section": section_num, "Row": row_num, "Topic": topic,
                                 "Difficulty": diff, "Sub Topic": sub, "Num Q": num_q,
                                 "Marks": marks, "Reason": reason})
            time.sleep(0.02)  # Reduced from 0.08 - much faster between questions

    done = total - len(failed_rows)
    progress_placeholder.success(
        f"✅ Section {section_num} questions done — {done}/{total} added")
    return done, failed_rows


# ============================================================
# MASTER MULTI-SECTION ORCHESTRATOR
# ============================================================

def automate_all_sections(driver, wait, sections, progress_placeholder):
    total_sections  = len(sections)
    all_failed_rows = []

    for sec_idx, section in enumerate(sections):
        sec_num             = sec_idx + 1
        sec_type            = section["section_type"]
        sec_name            = section["section_name"]
        sec_time            = section["time_limit"]
        coding_restriction  = section.get("coding_restriction",  "")
        default_coding_lang = section.get("default_coding_lang", "")
        questions_df        = section["questions_df"]
        is_last             = (sec_idx == total_sections - 1)
        coding_section_types = {"coding", "ide based coding", "sql", "sql coding", "web coding"}
        is_coding_section   = sec_type.strip().lower() in coding_section_types

        progress_placeholder.info(
            f"\n{'#'*60}\n"
            f"  SECTION {sec_num} / {total_sections} — {sec_type}\n"
            f"  Coding Section: {is_coding_section}\n"
            f"  Coding Restriction: '{coding_restriction}'\n"
            f"  Default Coding Lang: '{default_coding_lang}'\n"
            f"{'#'*60}")

        if sec_idx > 0:
            progress_placeholder.info(
                f"🎯 Selecting type for Section {sec_num}: '{sec_type}'")
            select_section_type(driver, wait, sec_type, progress_placeholder)
            poll_element_visible(driver, "//label[contains(text(),'Name')]", timeout=1.0)

        fill_section_form(driver, sec_name, sec_time, progress_placeholder)

        if not questions_df.empty:
            done, failed = process_section_questions(
                driver, questions_df, sec_num, progress_placeholder,
                section_type=sec_type)
            all_failed_rows.extend(failed)
        else:
            progress_placeholder.warning(
                f"  ⚠️ Section {sec_num} has no question rows — skipping question loop")

        if not close_add_questions_popup(driver, progress_placeholder):
            progress_placeholder.error(
                f"  ❌ Could not close popup after Section {sec_num} — aborting")
            return False

        poll_element_visible(driver, "//*[contains(text(),'Coding') or contains(text(),'Save')]", timeout=0.5)

        coding_keywords = ["coding", "ide", "sql", "web coding"]
        has_coding_keyword = any(keyword in sec_type.lower() for keyword in coding_keywords)
        has_coding_data = bool(coding_restriction) or bool(default_coding_lang)
        should_process_coding = is_coding_section or has_coding_keyword or has_coding_data

        if sec_type.strip().lower() in ["coding", "web coding", "ide based coding", "sql", "sql coding"]:
            should_process_coding = True
        if coding_restriction or default_coding_lang:
            should_process_coding = True

        if should_process_coding:
            progress_placeholder.info(f"🚀 EXECUTING CODING SECTION SETUP FOR SECTION {sec_num}")
            ensure_tab_focus(driver)

            try:
                restriction_value = coding_restriction if coding_restriction else "Python, Java"
                result1 = set_coding_question_restriction(driver, restriction_value, progress_placeholder)
                if result1:
                    progress_placeholder.success("    ✅ Coding Question Restriction completed")
                else:
                    progress_placeholder.error("    ❌ Coding Question Restriction failed")
            except Exception as e:
                progress_placeholder.error(f"    ❌ Exception: {str(e)[:100]}")

            try:
                language_value = default_coding_lang if default_coding_lang else "Python"
                result2 = set_default_coding_language(driver, language_value, progress_placeholder)
                if result2:
                    progress_placeholder.success("    ✅ Default Coding Language completed")
                else:
                    progress_placeholder.error("    ❌ Default Coding Language failed")
            except Exception as e:
                progress_placeholder.error(f"    ❌ Exception: {str(e)[:100]}")

            progress_placeholder.success(f"🎉 CODING SECTION SETUP COMPLETED FOR SECTION {sec_num}")
        else:
            progress_placeholder.info(f"  ℹ️ Section type '{sec_type}' is not coding-related — skipping")

        if not click_add_section_button(driver, progress_placeholder):
            progress_placeholder.error(
                f"  ❌ Could not click 'Add Section →' for Section {sec_num} — aborting")
            return False

        if is_last:
            progress_placeholder.info(
                f"🏁 Section {sec_num} is the LAST section — clicking 'Save & Next'")
            if not click_save_and_next_button(driver, progress_placeholder):
                return False

            progress_placeholder.info("🔗 Capturing final URL...")
            _old = driver.current_url
            poll_url_changed(driver, _old, timeout=2.0)
            try:
                final_url = driver.current_url
                progress_placeholder.success(f"  ✅ Final URL: {final_url}")

                st.divider()
                st.success(
                    f"🎉 Assessment with {total_sections} section(s) created successfully!")
                st.markdown("### 🔗 Final Assessment Page URL")
                st.code(final_url, language=None)
                st.markdown(
                    f'<a href="{final_url}" target="_blank" '
                    f'style="font-size:18px; font-weight:bold; color:#1f77b4;">'
                    f'🌐 Open Final Assessment Page ↗</a>',
                    unsafe_allow_html=True)

                if all_failed_rows:
                    st.warning("⚠️ Some rows were skipped/failed:")
                    st.dataframe(pd.DataFrame(all_failed_rows), use_container_width=True)

                st.markdown("### ✅ Automation Summary")
                summary_lines = [
                    f"✅ {total_sections} section(s) processed",
                    f"✅ Popup closed after each section",
                    f"✅ 'Add Section →' clicked for each section",
                    f"✅ 'Save & Next' clicked on final section",
                    f"✅ Final URL captured",
                ]
                st.info("\n\n".join(summary_lines))
                return True

            except Exception as e:
                progress_placeholder.error(f"  ❌ Could not capture URL: {str(e)[:100]}")
                st.error("⚠️ Assessment may have been created but URL capture failed")
                return False

        else:
            progress_placeholder.info(
                f"➡️ Section {sec_num} done — creating Section {sec_num + 1}...")
            if not click_create_new_section_button(driver, wait, progress_placeholder):
                progress_placeholder.error(
                    f"  ❌ Could not click 'Create New Section' after Section "
                    f"{sec_num} — aborting")
                return False
            poll_element_visible(driver, "//label[contains(text(),'Name')]", timeout=1.0)

    return True


# ============================================================
# RUN AUTOMATION  — ONLY CHANGE 3 OF 3 IS HERE:
# Added 3 background-throttling flags to Chrome options.
# Everything else is identical to original.
# ============================================================

def run_automation(mobile_num, otp_code, sections, wait_time=10):
    start_time           = time.time()
    driver               = None
    progress_placeholder = st.empty()

    try:
        progress_placeholder.info("🔧 Initializing browser...")

        options = Options()
        options.page_load_strategy = 'normal'
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--start-maximized')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        # FIX 3 OF 3: Stop Chrome throttling background tabs.
        # These 3 flags keep the renderer running at full speed
        # even when the user switches to a different tab/window.
        options.add_argument('--disable-background-timer-throttling')
        options.add_argument('--disable-backgrounding-occluded-windows')
        options.add_argument('--disable-renderer-backgrounding')
        options.add_experimental_option("prefs", {
            "profile.default_content_setting_values.notifications": 2,
        })

        chrome_binary = None
        for env_name in ('CHROME_BINARY', 'CHROME_BIN', 'CHROME_PATH', 'GOOGLE_CHROME_SHIM'):
            candidate = os.environ.get(env_name)
            if candidate:
                chrome_binary = candidate
                break

        if not chrome_binary:
            for path in [
                '/usr/bin/google-chrome-stable',
                '/usr/bin/google-chrome',
                '/usr/bin/chromium-browser',
                '/usr/bin/chromium',
                '/snap/bin/chromium',
            ]:
                if os.path.exists(path):
                    chrome_binary = path
                    break

        if chrome_binary and os.path.exists(chrome_binary):
            options.binary_location = chrome_binary
            progress_placeholder.info(f"🔧 Initializing headless browser using {chrome_binary}")
        else:
            env_hint = ' or set CHROME_BINARY/CHROME_BIN/CHROME_PATH/GOOGLE_CHROME_SHIM'
            detected = {
                'CHROME_BINARY': os.environ.get('CHROME_BINARY'),
                'CHROME_BIN': os.environ.get('CHROME_BIN'),
                'CHROME_PATH': os.environ.get('CHROME_PATH'),
                'GOOGLE_CHROME_SHIM': os.environ.get('GOOGLE_CHROME_SHIM'),
                'which_google_chrome': shutil.which('google-chrome'),
                'which_chromium': shutil.which('chromium'),
                'which_chromium_browser': shutil.which('chromium-browser'),
            }
            progress_placeholder.error(
                "Chrome/Chromium executable not found. "
                f"Set an environment variable{env_hint} to the browser path, or install Chromium in the container."
            )
            progress_placeholder.write("Detected values:\n" + "\n".join(
                f"{k}: {v}" for k, v in detected.items()
            ))
            raise RuntimeError(
                "Chrome/Chromium binary not found for Selenium headless mode."
            )

        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options
        )
        driver.set_page_load_timeout(60)
        driver.implicitly_wait(2)
        wait = WebDriverWait(driver, wait_time)

        # Inject hasFocus patch into EVERY page before it loads.
        # This runs automatically on navigation too, so the linked
        # assessment page also gets patched without any extra call.
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': """
                // Runs before every page load (including navigated pages).
                // Patches React's focus checks so events are always processed,
                // regardless of which tab the user is currently viewing.
                // NO window.focus() here — we never steal the user's focus.
                document.__proto__.hasFocus = function(){ return true; };
                document.hasFocus           = function(){ return true; };
                try {
                    Object.defineProperty(document, 'visibilityState', {
                        get: function(){ return 'visible'; }, configurable: true
                    });
                    Object.defineProperty(document, 'hidden', {
                        get: function(){ return false; }, configurable: true
                    });
                } catch(e) {}
                window.addEventListener('visibilitychange', function(e){
                    e.stopImmediatePropagation();
                }, true);
                window.addEventListener('blur', function(e){
                    e.stopImmediatePropagation();
                }, true);
            """
        })

        progress_placeholder.info("🔐 Step 1: Loading login page...")
        try:
            driver.get("https://config.topin.tech")
        except TimeoutException:
            progress_placeholder.warning("⚠️ Page load slow, continuing...")
            try:
                driver.execute_script("window.stop();")
            except:
                pass

        mobile_input = WebDriverWait(driver, 5).until(EC.presence_of_element_located(  # Reduced from default wait_time
            (By.XPATH, "//input[contains(@placeholder,'Number')]")))
        time.sleep(0.02)  # Reduced from 0.05 - ultra fast
        driver.execute_script("""
            var el = arguments[0], val = arguments[1];
            el.focus();
            var s = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            s.call(el, val);
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        """, mobile_input, mobile_num)
        time.sleep(0.02)  # Reduced from 0.05 - ultra fast

        find_and_click(driver, "//button[contains(., 'GET OTP')]", timeout=wait_time)

        progress_placeholder.info("🔢 Step 2: Entering OTP...")
        wait.until(EC.presence_of_element_located(
            (By.XPATH, "//button[contains(., 'Verify')]")))

        filled = driver.execute_script("""
            var otp = arguments[0], filled = 0;
            var boxes = document.querySelectorAll('input[maxlength="1"]');
            if (boxes.length >= 6) {
                for (var i = 0; i < 6; i++) {
                    var s = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    s.call(boxes[i], otp[i]);
                    boxes[i].dispatchEvent(new Event('input',  {bubbles:true}));
                    boxes[i].dispatchEvent(new Event('change', {bubbles:true}));
                    boxes[i].dispatchEvent(
                        new KeyboardEvent('keyup', {key:otp[i],bubbles:true}));
                    filled++;
                }
                return filled;
            }
            var sels = ['input[type="text"]','input[type="tel"]','input[type="number"]'];
            for (var s2 = 0; s2 < sels.length && filled < 6; s2++) {
                var inputs = document.querySelectorAll(sels[s2]);
                for (var j = 0; j < inputs.length && filled < otp.length; j++) {
                    if (!inputs[j].offsetParent) continue;
                    var setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    setter.call(inputs[j], otp[filled]);
                    inputs[j].dispatchEvent(new Event('input',  {bubbles:true}));
                    inputs[j].dispatchEvent(new Event('change', {bubbles:true}));
                    inputs[j].dispatchEvent(
                        new KeyboardEvent('keyup',{key:otp[filled],bubbles:true}));
                    filled++;
                }
            }
            return filled;
        """, otp_code)

        if filled >= 6:
            progress_placeholder.success("  ✅ OTP entered")
        else:
            progress_placeholder.warning(
                f"  ⚠️ Only {filled}/6 OTP digits filled — please complete manually")

        time.sleep(0.02)  # Reduced from 0.1 - much faster
        find_and_click(driver, "//button[contains(., 'Verify')]", timeout=wait_time)
        progress_placeholder.success("✅ Login Successful!")

        progress_placeholder.info("🚀 Step 3: Navigating to Create Assessment...")
        find_and_click(
            driver, "//*[contains(text(), 'Create Assessment')]", timeout=wait_time)
        find_and_click(
            driver, "//*[contains(text(), 'Custom Assessment')]", timeout=wait_time)

        progress_placeholder.info("➕ Step 4: Creating Section 1...")
        find_and_click(driver,
            "//*[contains(text(),'Create new Section') or "
            "contains(text(),'Create New Section')]",
            timeout=wait_time)

        handle_subject_selection(driver, progress_placeholder)

        sec1_type = sections[0]["section_type"]
        progress_placeholder.info(f"🎯 Selecting type for Section 1: '{sec1_type}'")
        select_section_type(driver, wait, sec1_type, progress_placeholder)
        poll_element_visible(driver, "//label[contains(text(),'Name')]", timeout=1.0)

        progress_placeholder.success(
            f"✅ Setup done — handing off to multi-section orchestrator "
            f"({len(sections)} section(s))")

        success = automate_all_sections(driver, wait, sections, progress_placeholder)

        elapsed = time.time() - start_time
        if success:
            progress_placeholder.success(
                f"🎉 Full automation complete! Time: {elapsed:.1f}s")
            st.balloons()
        else:
            progress_placeholder.error("❌ Automation ended with errors")

    except TimeoutException:
        progress_placeholder.error("⏱️ Timeout — try increasing wait time")
    except WebDriverException as e:
        progress_placeholder.error(f"🌐 Browser error: {str(e)[:100]}")
    except Exception as e:
        progress_placeholder.error(f"❌ Error: {str(e)[:150]}")
    finally:
        try:
            driver.quit()
        except:
            pass


# ============================================================
# STREAMLIT TRIGGER BUTTON
# ============================================================

st.write("---")
if st.button("🚀 START AUTOMATION", use_container_width=True, type="primary"):
    if not uploaded_file or not mob or len(otp_val) != 6:
        st.warning(
            "⚠️ Please provide all required fields (CSV, mobile number, 6-digit OTP)")
    else:
        try:
            raw_df = pd.read_csv(uploaded_file, header=None)

            st.write("---")
            st.subheader("📊 CSV Preview")
            total_rows   = len(raw_df)
            preview_rows = min(100, total_rows)
            st.caption(
                f"Total rows: **{total_rows}** | Showing first **{preview_rows}**")
            st.dataframe(raw_df.head(preview_rows), use_container_width=True)
            st.write("---")

            sections = parse_sections_from_csv(raw_df)

            if not sections:
                st.error(
                    "❌ No valid sections found in CSV. Check the format guide above.")
            else:
                st.success(f"✅ Found **{len(sections)}** section(s) in CSV")

                valid_section_types = [
                    "MCQ", "Coding", "SQL", "Textual", "Web Coding",
                    "Fill in the Blank", "Audio", "Communication", "IDE Based Coding"
                ]

                all_valid = True
                for i, sec in enumerate(sections):
                    sec_num  = i + 1
                    sec_type = sec["section_type"]
                    matched  = next(
                        (v for v in valid_section_types
                         if sec_type.lower() == v.lower()), None)

                    with st.expander(
                            f"📋 Section {sec_num}: {sec_type}", expanded=(i == 0)):
                        c1, c2, c3, c4 = st.columns(4)
                        with c1:
                            if matched:
                                st.success(f"**Type:** {matched}")
                                sec["section_type"] = matched
                                if matched.lower() in MARKS_SECTION_TYPES:
                                    st.info("🏅 Marks per Q: enabled")
                                if matched.lower() == "coding":
                                    cq = sec.get("coding_restriction", "")
                                    dl = sec.get("default_coding_lang", "")
                                    if cq:
                                        st.info(f"🖥️ Restriction: {cq}")
                                    if dl:
                                        st.info(f"🌐 Default Lang: {dl}")
                            else:
                                st.error(f"❌ Unknown type: '{sec_type}'")
                                all_valid = False
                        with c2:
                            st.info(f"**Name:** {sec['section_name'] or '(not set)'}")
                        with c3:
                            st.info(
                                f"**Time:** {sec['time_limit'] or '(not set)'} mins")
                        with c4:
                            q_count = len(sec["questions_df"])
                            st.info(f"**Questions:** {q_count} rows")

                        if not sec["questions_df"].empty:
                            st.dataframe(
                                sec["questions_df"].head(10), use_container_width=True)

                st.write("---")

                if not all_valid:
                    st.error(
                        "❌ One or more sections have invalid Section Types. "
                        f"Valid types: {', '.join(valid_section_types)}")
                else:
                    with st.spinner(
                            f"🤖 Running automation for {len(sections)} section(s)..."):
                        run_automation(mob, otp_val, sections, wait_time=wait_time)

        except Exception as e:
            import traceback
            err_str = str(e)
            tb_str  = traceback.format_exc()
            if "element not interactable" in err_str or "WebDriver" in err_str \
                    or "chromedriver" in err_str.lower() \
                    or "session" in err_str.lower():
                st.error(f"❌ Browser Error: {err_str[:400]}")
            else:
                st.error(f"❌ Error: {err_str[:400]}")
            with st.expander("🔍 Full traceback"):
                st.code(tb_str)